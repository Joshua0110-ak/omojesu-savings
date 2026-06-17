from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import HttpResponseForbidden, HttpResponse
from django.contrib import messages
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import json
import hmac
import hashlib

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from io import BytesIO

from .models import Member, Contribution, Loan, LoanRepayment, PaymentTransaction
from .services import member_summary, contributions_by_month, org_summary
from .forms import (
    ContributionForm, LoanForm, LoanRepaymentForm,
    MemberForm, ProfileUpdateForm, MemberRegistrationForm,
)
from .decorators import finance_admin_required
from .paystack_services import initialize_transaction, verify_transaction


# ── HELPER ────────────────────────────────────────────────────────────────────

def is_finance_admin(user):
    """Check if user has finance admin privileges."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    if user.is_staff:
        return True
    try:
        return user.member.is_finance_admin
    except (Member.DoesNotExist, AttributeError):
        return False


def get_member_or_none(user):
    """Safely get member object or None."""
    try:
        return user.member
    except (Member.DoesNotExist, AttributeError):
        return None


# ── LOGIN ─────────────────────────────────────────────────────────────────────

def login_view(request):
    """Handle user login with proper redirects."""
    # If already logged in, redirect to appropriate dashboard
    if request.user.is_authenticated:
        if is_finance_admin(request.user):
            return redirect("admin_dashboard")
        member = get_member_or_none(request.user)
        if member:
            return redirect("member_detail", member_id=member.id)
        # If user has no member profile but is staff, go to admin dashboard
        if request.user.is_staff:
            return redirect("admin_dashboard")
        # Fallback - logout and show login
        logout(request)
        return render(request, "login.html", {"error": "Your account needs a member profile. Contact admin."})

    error = None
    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")
        user = authenticate(request, username=username, password=password)
        
        if user is not None:
            login(request, user)
            
            # Check if user has member profile
            member = get_member_or_none(user)
            
            # Redirect based on role
            if is_finance_admin(user):
                return redirect("admin_dashboard")
            elif member:
                return redirect("member_detail", member_id=member.id)
            else:
                # User has no member profile - show error and logout
                logout(request)
                error = "Your account is not linked to a member profile. Please contact the administrator."
        else:
            error = "Invalid username or password. Please try again."
    
    return render(request, "login.html", {"error": error})


# ── LOGOUT ────────────────────────────────────────────────────────────────────

def logout_view(request):
    """Handle user logout."""
    if request.method == "POST":
        logout(request)
    return redirect("login")


# ── REGISTER ──────────────────────────────────────────────────────────────────

def register_view(request):
    """Handle new member registration."""
    # If already logged in, redirect
    if request.user.is_authenticated:
        if is_finance_admin(request.user):
            return redirect("admin_dashboard")
        member = get_member_or_none(request.user)
        if member:
            return redirect("member_detail", member_id=member.id)
    
    error = None
    if request.method == "POST":
        form = MemberRegistrationForm(request.POST)
        if form.is_valid():
            card_number = form.cleaned_data["card_number"]
            username = form.cleaned_data["username"]
            password = form.cleaned_data["password"]
            
            try:
                member = Member.objects.get(card_number=card_number)
            except Member.DoesNotExist:
                error = "No member found with that card number. Contact your administrator."
                return render(request, "register.html", {"form": form, "error": error})
            
            # Check if card already has an account
            if member.user is not None:
                error = "This card number already has an account. Please log in."
                return render(request, "register.html", {"form": form, "error": error})
            
            # Check if username already exists
            if User.objects.filter(username=username).exists():
                error = "That username already exists. Please choose another."
                return render(request, "register.html", {"form": form, "error": error})
            
            # Create user and link to member
            user = User.objects.create_user(
                username=username, 
                password=password,
                email=request.POST.get("email", "")  # Optional email
            )
            member.user = user
            member.save()
            
            # Log the user in
            login(request, user)
            messages.success(request, f"Welcome {member.full_name}! Your account has been created successfully.")
            return redirect("member_detail", member_id=member.id)
    else:
        form = MemberRegistrationForm()
    
    return render(request, "register.html", {"form": form, "error": error})


# ── ADMIN DASHBOARD ───────────────────────────────────────────────────────────

@login_required
@finance_admin_required
def admin_dashboard(request):
    """Admin dashboard view - protected by finance_admin_required decorator."""
    stats = org_summary()
    return render(request, "admin_dashboard.html", {
        "stats": stats,
        "chart_labels": json.dumps(stats["chart_labels"]),
        "chart_data": json.dumps(stats["chart_data"]),
    })


# ── MEMBER DETAIL ─────────────────────────────────────────────────────────────

@login_required
def member_detail(request, member_id):
    """View member details - accessible by admin or the member themselves."""
    member = get_object_or_404(Member, id=member_id)
    
    # Check permissions
    if not is_finance_admin(request.user):
        try:
            logged_in_member = request.user.member
            if logged_in_member.id != member.id:
                return HttpResponseForbidden("You can only view your own dashboard.")
        except Member.DoesNotExist:
            return HttpResponseForbidden("You can only view your own dashboard.")

    summary = member_summary(member)
    # Only show verified contributions
    contributions = Contribution.objects.filter(
        member=member
    ).exclude(
        payment_method="Paystack", payment_verified=False
    ).order_by("-date")

    repayments = LoanRepayment.objects.filter(loan__member=member).order_by("-date")
    loans = Loan.objects.filter(member=member).order_by("-date_given")
    chart_labels, chart_data = contributions_by_month(member)

    viewer_is_member = False
    try:
        viewer_is_member = (request.user.member == member)
    except Member.DoesNotExist:
        pass

    return render(request, "member_detail.html", {
        "member": member,
        "summary": summary,
        "contributions": contributions,
        "repayments": repayments,
        "loans": loans,
        "chart_labels": json.dumps(chart_labels),
        "chart_data": json.dumps(chart_data),
        "viewer_is_admin": is_finance_admin(request.user),
        "viewer_is_member": viewer_is_member,
        "last_payment": contributions.first(),
        "paystack_public_key": settings.PAYSTACK_PUBLIC_KEY,
    })


# ── EDIT PROFILE ──────────────────────────────────────────────────────────────

@login_required
def edit_profile(request, member_id):
    """Edit member profile - admin or member themselves."""
    member = get_object_or_404(Member, id=member_id)
    
    # Check permissions
    if not is_finance_admin(request.user):
        try:
            if request.user.member != member:
                return HttpResponseForbidden("You can only edit your own profile.")
        except Member.DoesNotExist:
            return HttpResponseForbidden("You can only edit your own profile.")

    if request.method == "POST":
        form = ProfileUpdateForm(request.POST, request.FILES, instance=member)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully.")
            return redirect("member_detail", member_id=member.id)
    else:
        form = ProfileUpdateForm(instance=member)
    
    return render(request, "edit_profile.html", {"form": form, "member": member})


# ── MEMBER SEARCH ─────────────────────────────────────────────────────────────

@login_required
def member_search(request):
    """Search members - admin only, redirects members to their own page."""
    if not is_finance_admin(request.user):
        try:
            member = request.user.member
            return redirect("member_detail", member_id=member.id)
        except Member.DoesNotExist:
            pass
    
    query = request.GET.get("q", "")
    members = Member.objects.all()
    if query:
        members = members.filter(
            Q(full_name__icontains=query) | Q(card_number__icontains=query)
        )
    
    return render(request, "member_search.html", {"members": members, "query": query})


# ── ADD CONTRIBUTION (manual) ─────────────────────────────────────────────────

@login_required
@finance_admin_required
def add_contribution(request):
    """Add manual contribution - admin only."""
    if request.method == "POST":
        form = ContributionForm(request.POST, user=request.user)
        if form.is_valid():
            contribution = form.save(commit=False)
            contribution.recorded_by = request.user
            contribution.payment_method = "Manual"
            contribution.payment_verified = True  # manual = always verified
            contribution.save()
            messages.success(request, f"Contribution of ₦{contribution.amount:,.2f} recorded.")
            return redirect("member_detail", member_id=contribution.member.id)
    else:
        form = ContributionForm(user=request.user)
    
    return render(request, "add_contribution.html", {"form": form})


# ── ADD LOAN ──────────────────────────────────────────────────────────────────

@login_required
@finance_admin_required
def add_loan(request):
    """Add loan - admin only."""
    if request.method == "POST":
        form = LoanForm(request.POST, user=request.user)
        if form.is_valid():
            loan = form.save(commit=False)
            loan.recorded_by = request.user
            loan.interest_rate = 7 if loan.member.is_finance_admin else 10
            loan.save()
            messages.success(request, f"Loan of ₦{loan.amount:,.2f} recorded.")
            return redirect("member_detail", member_id=loan.member.id)
    else:
        form = LoanForm(user=request.user)
    
    return render(request, "give_loan.html", {"form": form})


# ── ADD REPAYMENT ─────────────────────────────────────────────────────────────

@login_required
@finance_admin_required
def add_repayment(request):
    """Add loan repayment - admin only."""
    if request.method == "POST":
        form = LoanRepaymentForm(request.POST, user=request.user)
        if form.is_valid():
            repayment = form.save(commit=False)
            repayment.recorded_by = request.user
            repayment.save()
            
            loan = repayment.loan
            if loan.balance_remaining <= 0:
                loan.is_paid = True
                loan.save()
                messages.success(request, f"Repayment of ₦{repayment.amount:,.2f} recorded. Loan fully paid! 🎉")
            else:
                messages.success(request, f"Repayment of ₦{repayment.amount:,.2f} recorded. ₦{loan.balance_remaining:,.2f} still outstanding.")
            
            return redirect("member_detail", member_id=loan.member.id)
    else:
        form = LoanRepaymentForm(user=request.user)
    
    return render(request, "add_repayment.html", {"form": form})


# ── ADD MEMBER ────────────────────────────────────────────────────────────────

@login_required
@finance_admin_required
def add_member(request):
    """Add new member - admin only."""
    if request.method == "POST":
        form = MemberForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Member added successfully.")
            return redirect("member_search")
    else:
        form = MemberForm()
    
    return render(request, "add_member.html", {"form": form})


# ── PASSWORD CHANGE DONE ──────────────────────────────────────────────────────

@login_required
def password_change_done_redirect(request):
    """Redirect after password change."""
    if is_finance_admin(request.user):
        return redirect("admin_dashboard")
    try:
        member = request.user.member
        return redirect("member_detail", member_id=member.id)
    except Member.DoesNotExist:
        return redirect("admin_dashboard")


# ── PDF STATEMENT ─────────────────────────────────────────────────────────────

@login_required
def statement_pdf(request, member_id):
    """Generate PDF statement - admin or member themselves."""
    member = get_object_or_404(Member, id=member_id)
    
    if not is_finance_admin(request.user):
        try:
            if request.user.member != member:
                return HttpResponseForbidden("You cannot access this statement.")
        except Member.DoesNotExist:
            return HttpResponseForbidden("You cannot access this statement.")

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                           leftMargin=0.75*inch, rightMargin=0.75*inch,
                           topMargin=0.75*inch, bottomMargin=0.75*inch)
    styles = getSampleStyleSheet()
    elements = []

    title_style = ParagraphStyle('T', parent=styles['Title'], fontSize=18,
                                 textColor=colors.HexColor('#1A6B4A'), spaceAfter=4)
    elements.append(Paragraph("OmoJesu Savings", title_style))
    elements.append(Paragraph("<b>Statement of Account</b>", styles['Normal']))
    elements.append(Spacer(1, 16))

    info = ParagraphStyle('I', parent=styles['Normal'], fontSize=10, leading=16)
    elements.append(Paragraph(f"<b>Member:</b> {member.full_name}", info))
    elements.append(Paragraph(f"<b>Card Number:</b> {member.card_number}", info))
    elements.append(Paragraph(f"<b>Joined:</b> {member.joined_date.strftime('%B %d, %Y')}", info))
    if member.bank_name:
        elements.append(Paragraph(
            f"<b>Bank:</b> {member.bank_name} — {member.account_number} ({member.account_name})", info))
    elements.append(Spacer(1, 20))

    # Summary
    summary = member_summary(member)
    elements.append(Paragraph("<b>Account Summary</b>", styles['Heading2']))
    elements.append(Spacer(1, 8))
    sdata = [
        ["Total Savings", f"₦{summary['total_savings']:,.2f}"],
        ["Total Loans", f"₦{summary['total_loans']:,.2f}"],
        ["Total Repayments", f"₦{summary['total_repayments']:,.2f}"],
        ["Outstanding Loan", f"₦{summary['outstanding_loan']:,.2f}"],
        ["Loan Interest", f"₦{summary['loan_interest']:,.2f}"],
    ]
    st = Table(sdata, colWidths=[3*inch, 2*inch])
    st.setStyle(TableStyle([
        ('FONTNAME', (0,0),(0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (1,0),(1,-1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (1,0),(1,-1), colors.HexColor('#1A6B4A')),
        ('FONTSIZE', (0,0),(-1,-1), 10),
        ('ROWBACKGROUNDS', (0,0),(-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
        ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#cccccc')),
        ('TOPPADDING', (0,0),(-1,-1), 6),
        ('BOTTOMPADDING', (0,0),(-1,-1), 6),
    ]))
    elements.append(st)
    elements.append(Spacer(1, 24))

    # Contributions
    elements.append(Paragraph("<b>Contributions</b>", styles['Heading2']))
    elements.append(Spacer(1, 8))
    contribs = Contribution.objects.filter(member=member, payment_verified=True).order_by('date')
    if contribs.exists():
        cdata = [["Date", "Method", "Reference", "Amount (₦)"]]
        for c in contribs:
            cdata.append([
                c.date.strftime("%b %d, %Y"),
                c.payment_method,
                c.payment_reference or "—",
                f"{c.amount:,.2f}",
            ])
        ct = Table(cdata, colWidths=[1.5*inch, 1.2*inch, 2*inch, 1.2*inch])
        ct.setStyle(TableStyle([
            ('BACKGROUND', (0,0),(-1,0), colors.HexColor('#1A6B4A')),
            ('TEXTCOLOR', (0,0),(-1,0), colors.white),
            ('FONTNAME', (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0),(-1,-1), 10),
            ('ROWBACKGROUNDS', (0,1),(-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
            ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#cccccc')),
            ('TOPPADDING', (0,0),(-1,-1), 6),
            ('BOTTOMPADDING', (0,0),(-1,-1), 6),
            ('ALIGN', (3,0),(3,-1), 'RIGHT'),
        ]))
        elements.append(ct)
    else:
        elements.append(Paragraph("No contributions recorded.", styles['Normal']))
    elements.append(Spacer(1, 20))

    # Loans
    elements.append(Paragraph("<b>Loans</b>", styles['Heading2']))
    elements.append(Spacer(1, 8))
    loans = Loan.objects.filter(member=member).order_by('date_given')
    if loans.exists():
        ldata = [["Date", "Amount", "Interest", "Total Due", "Due Date", "Status"]]
        for loan in loans:
            ldata.append([
                loan.date_given.strftime("%b %d, %Y"),
                f"₦{loan.amount:,.2f}",
                f"{loan.interest_rate}%",
                f"₦{loan.total_with_interest():,.2f}",
                loan.due_date.strftime("%b %d, %Y"),
                "Paid" if loan.is_paid else "Unpaid",
            ])
        lt = Table(ldata, colWidths=[1.1*inch, 1.1*inch, 0.8*inch, 1.2*inch, 1.1*inch, 0.8*inch])
        lt.setStyle(TableStyle([
            ('BACKGROUND', (0,0),(-1,0), colors.HexColor('#1A3A6B')),
            ('TEXTCOLOR', (0,0),(-1,0), colors.white),
            ('FONTNAME', (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0),(-1,-1), 9),
            ('ROWBACKGROUNDS', (0,1),(-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
            ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#cccccc')),
            ('TOPPADDING', (0,0),(-1,-1), 5),
            ('BOTTOMPADDING', (0,0),(-1,-1), 5),
        ]))
        elements.append(lt)
    else:
        elements.append(Paragraph("No loans recorded.", styles['Normal']))
    elements.append(Spacer(1, 20))

    # Repayments
    elements.append(Paragraph("<b>Loan Repayments</b>", styles['Heading2']))
    elements.append(Spacer(1, 8))
    repayments = LoanRepayment.objects.filter(loan__member=member).order_by('date')
    if repayments.exists():
        rdata = [["Date", "Loan Amount", "Repaid", "Recorded By"]]
        for r in repayments:
            rdata.append([
                r.date.strftime("%b %d, %Y"),
                f"₦{r.loan.amount:,.2f}",
                f"₦{r.amount:,.2f}",
                r.recorded_by.username if r.recorded_by else "—",
            ])
        rt = Table(rdata, colWidths=[1.5*inch, 1.8*inch, 1.5*inch, 1.5*inch])
        rt.setStyle(TableStyle([
            ('BACKGROUND', (0,0),(-1,0), colors.HexColor('#6B4A1A')),
            ('TEXTCOLOR', (0,0),(-1,0), colors.white),
            ('FONTNAME', (0,0),(-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0),(-1,-1), 10),
            ('ROWBACKGROUNDS', (0,1),(-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
            ('GRID', (0,0),(-1,-1), 0.3, colors.HexColor('#cccccc')),
            ('TOPPADDING', (0,0),(-1,-1), 6),
            ('BOTTOMPADDING', (0,0),(-1,-1), 6),
        ]))
        elements.append(rt)
    else:
        elements.append(Paragraph("No repayments recorded.", styles['Normal']))

    from datetime import date
    elements.append(Spacer(1, 30))
    elements.append(Paragraph(
        f"<font size='8' color='grey'>Generated {date.today().strftime('%B %d, %Y')} · OmoJesu Savings</font>",
        styles['Normal']
    ))

    doc.build(elements)
    pdf = buffer.getvalue()
    buffer.close()

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{member.full_name}_statement.pdf"'
    response.write(pdf)
    return response


# ── PAYSTACK: INITIALIZE PAYMENT ──────────────────────────────────────────────

@login_required
def initialize_payment(request, member_id):
    """Initialize Paystack payment - member only."""
    member = get_object_or_404(Member, id=member_id)

    # Only the member themselves can pay
    if not is_finance_admin(request.user):
        try:
            if request.user.member != member:
                return HttpResponseForbidden("You can only make payments for yourself.")
        except Member.DoesNotExist:
            return HttpResponseForbidden("You can only make payments for yourself.")

    if request.method == "POST":
        amount = request.POST.get("amount", "0")
        try:
            amount = float(amount)
            if amount < 100:
                messages.error(request, "Minimum payment amount is ₦100.")
                return redirect("member_detail", member_id=member.id)
        except ValueError:
            messages.error(request, "Invalid amount entered.")
            return redirect("member_detail", member_id=member.id)

        # Check member's email
        email = member.email
        if not email:
            messages.error(request, "You need an email address on your profile to pay online. Update your profile.")
            return redirect("edit_profile", member_id=member.id)

        # Check Paystack keys are set
        if not settings.PAYSTACK_SECRET_KEY:
            messages.error(request, "Paystack is not configured properly. Contact admin.")
            return redirect("member_detail", member_id=member.id)

        callback_url = request.build_absolute_uri(f"/paystack/verify/{member.id}/")

        result = initialize_transaction(email, amount, member.id, callback_url)

        if result["status"]:
            # Save transaction log as pending
            PaymentTransaction.objects.create(
                member=member,
                amount=amount,
                reference=result["reference"],
                status="pending",
            )
            # Redirect to Paystack payment page
            return redirect(result["authorization_url"])
        else:
            messages.error(request, f"Could not initialize payment: {result.get('message')}")
            return redirect("member_detail", member_id=member.id)

    return render(request, "make_payment.html", {
        "member": member,
        "paystack_public_key": settings.PAYSTACK_PUBLIC_KEY,
    })


# ── PAYSTACK: VERIFY PAYMENT ──────────────────────────────────────────────────

@login_required
def verify_payment(request, member_id):
    """Verify Paystack payment after callback."""
    member = get_object_or_404(Member, id=member_id)
    reference = request.GET.get("reference", "")

    if not reference:
        messages.error(request, "No payment reference found.")
        return redirect("member_detail", member_id=member.id)

    # Prevent double-recording
    if Contribution.objects.filter(payment_reference=reference).exists():
        messages.warning(request, "This payment has already been recorded.")
        return redirect("member_detail", member_id=member.id)

    result = verify_transaction(reference)

    if result["status"]:
        try:
            # Record the contribution
            contribution = Contribution.objects.create(
                member=member,
                amount=result["amount_naira"],
                payment_method="Paystack",
                payment_reference=reference,
                payment_verified=True,
                recorded_by=None,
            )

            # Update transaction log
            PaymentTransaction.objects.filter(reference=reference).update(
                status="success", verified=True
            )

            messages.success(request, f"Payment of ₦{result['amount_naira']:,.2f} received and recorded! 🎉")
            return redirect("receipt", member_id=member.id, reference=reference)
            
        except Exception as e:
            messages.error(request, "Payment was successful but there was an error recording it. Please contact admin.")
            return redirect("member_detail", member_id=member.id)
    else:
        PaymentTransaction.objects.filter(reference=reference).update(status="failed")
        messages.error(request, f"Payment verification failed: {result.get('message', 'Unknown error')}")
        return redirect("member_detail", member_id=member.id)


# ── RECEIPT PAGE ──────────────────────────────────────────────────────────────

@login_required
def receipt(request, member_id, reference):
    """Show payment receipt."""
    member = get_object_or_404(Member, id=member_id)
    contribution = get_object_or_404(
        Contribution, member=member, payment_reference=reference
    )
    return render(request, "receipt.html", {
        "member": member,
        "contribution": contribution,
    })


# ── PAYSTACK WEBHOOK ─────────────────────────────────────────────────────────

@csrf_exempt
def paystack_webhook(request):
    """Handle Paystack webhook for automatic payment verification."""
    if request.method != "POST":
        return HttpResponse(status=405)

    paystack_sig = request.headers.get("x-paystack-signature", "")
    computed_sig = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(),
        request.body,
        hashlib.sha512
    ).hexdigest()

    if paystack_sig != computed_sig:
        return HttpResponse(status=401)

    payload = json.loads(request.body)

    if payload.get("event") == "charge.success":
        data = payload["data"]
        reference = data.get("reference", "")
        amount = data.get("amount", 0) / 100
        member_id = data.get("metadata", {}).get("member_id")

        if not Contribution.objects.filter(payment_reference=reference).exists():
            try:
                member = Member.objects.get(id=member_id)
                Contribution.objects.create(
                    member=member,
                    amount=amount,
                    payment_method="Paystack",
                    payment_reference=reference,
                    payment_verified=True,
                    recorded_by=None,
                )
                PaymentTransaction.objects.filter(reference=reference).update(
                    status="success", verified=True
                )
            except Member.DoesNotExist:
                pass

    return HttpResponse(status=200)