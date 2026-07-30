import logging
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db.models import Q, Sum
from django.http import HttpResponseForbidden, HttpResponse
from django.contrib import messages
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import json
import hmac
import hashlib
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from io import BytesIO

from .models import Member, Contribution, Loan, LoanRepayment, PaymentTransaction, default_loan_due_date
from .services import (
    member_summary, 
    contributions_by_month, 
    org_summary,
    total_interest_all,      
    total_repayments_all,
    pending_approvals_count,
)
from .forms import (
    ContributionForm, LoanForm, LoanRepaymentForm,
    MemberForm, ProfileUpdateForm, MemberRegistrationForm,
)
from .decorators import finance_admin_required
from .paystack_services import initialize_transaction, verify_transaction

logger = logging.getLogger(__name__)

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
    
    # Add interest and repayment totals for ALL members
    stats['total_interest_all'] = total_interest_all()
    stats['total_repayments_all'] = total_repayments_all()
    
    return render(request, "admin_dashboard.html", {
        "stats": stats,
        "chart_labels": json.dumps(stats["chart_labels"]),
        "chart_data": json.dumps(stats["chart_data"]),
        "pending_approvals_count": pending_approvals_count(),
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

    # A finance admin viewing their OWN member page sees it like a regular
    # member (no add-contribution/give-loan buttons); they only get admin
    # controls when viewing someone else's page.
    viewer_is_admin = is_finance_admin(request.user) and not viewer_is_member

    return render(request, "member_detail.html", {
        "member": member,
        "summary": summary,
        "contributions": contributions,
        "repayments": repayments,
        "loans": loans,
        "chart_labels": json.dumps(chart_labels),
        "chart_data": json.dumps(chart_data),
        "viewer_is_admin": viewer_is_admin,
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
    """Request a loan for a member - admin only. Goes to Pending status
    and needs a DIFFERENT finance admin to approve/reject it."""
    if request.method == "POST":
        form = LoanForm(request.POST, user=request.user)
        if form.is_valid():
            member = form.cleaned_data.get('member')
            
            # ── CHECK: Does member have an unresolved loan? ──
            # (Pending or Approved-and-unpaid; Rejected doesn't block.)
            pending_loan = Loan.objects.filter(
                member=member, is_paid=False
            ).exclude(status="Rejected").exists()
            if pending_loan:
                messages.error(
                    request, 
                    f"❌ {member.full_name} already has an unresolved loan! "
                    "They cannot request another loan until it's repaid or rejected."
                )
                return redirect("add_loan")
            
            loan = form.save(commit=False)
            loan.recorded_by = request.user
            loan.interest_rate = 7 if loan.member.is_finance_admin else 10
            loan.due_date = default_loan_due_date()
            loan.status = "Pending"
            loan.save()
            messages.success(
                request,
                f"📨 Loan request of ₦{loan.amount:,.2f} for {loan.member.full_name} "
                "has been submitted and is awaiting approval from another finance admin."
            )
            return redirect("member_detail", member_id=loan.member.id)
    else:
        form = LoanForm(user=request.user)
    
    return render(request, "give_loan.html", {"form": form})


# ── LOAN APPROVALS ────────────────────────────────────────────────────────────

@login_required
@finance_admin_required
def loan_approvals(request):
    """List all loans awaiting approval - finance admins only."""
    pending_loans = Loan.objects.filter(status="Pending").select_related(
        "member", "recorded_by"
    ).order_by("date_given")
    return render(request, "loan_approvals.html", {
        "pending_loans": pending_loans,
    })


@login_required
@finance_admin_required
def approve_loan(request, loan_id):
    """Approve a pending loan - cannot approve your own request."""
    loan = get_object_or_404(Loan, id=loan_id, status="Pending")

    if loan.recorded_by_id == request.user.id:
        messages.error(request, "You can't approve a loan you requested yourself. Ask another finance admin to review it.")
        return redirect("loan_approvals")

    if request.method == "POST":
        loan.status = "Approved"
        loan.approved_by = request.user
        loan.save()
        messages.success(request, f"✅ Loan of ₦{loan.amount:,.2f} for {loan.member.full_name} approved.")
    return redirect("loan_approvals")


@login_required
@finance_admin_required
def reject_loan(request, loan_id):
    """Reject a pending loan - cannot reject your own request."""
    loan = get_object_or_404(Loan, id=loan_id, status="Pending")

    if loan.recorded_by_id == request.user.id:
        messages.error(request, "You can't reject a loan you requested yourself. Ask another finance admin to review it.")
        return redirect("loan_approvals")

    if request.method == "POST":
        loan.status = "Rejected"
        loan.approved_by = request.user
        loan.is_paid = True  # so it never shows as "outstanding" or blocks new requests
        loan.save()
        messages.success(request, f"🚫 Loan of ₦{loan.amount:,.2f} for {loan.member.full_name} rejected.")
    return redirect("loan_approvals")


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

# ── FULL STATEMENT (Bank-Style) ──────────────────────────────────────────────

@login_required
@finance_admin_required
def member_full_statement(request, member_id):
    """Generate a complete bank-style statement for a member."""
    from datetime import timedelta
    from decimal import Decimal
    
    member = get_object_or_404(Member, id=member_id)
    
    # Get date range from request
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    date_filter = request.GET.get('date_filter', 'all')
    
    # Default to last 30 days if no date specified
    if date_filter == 'all':
        contributions_all = Contribution.objects.filter(member=member, payment_verified=True)
        repayments_all = LoanRepayment.objects.filter(loan__member=member)
        loans_all = Loan.objects.filter(member=member)
        start_date = member.joined_date
        end_date = timezone.now().date()
    elif date_filter == 'today':
        today = timezone.now().date()
        contributions_all = Contribution.objects.filter(member=member, payment_verified=True, date__date=today)
        repayments_all = LoanRepayment.objects.filter(loan__member=member, date__date=today)
        loans_all = Loan.objects.filter(member=member, date_given__date=today)
        start_date = today
        end_date = today
    elif date_filter == 'week':
        week_ago = timezone.now() - timedelta(days=7)
        contributions_all = Contribution.objects.filter(member=member, payment_verified=True, date__gte=week_ago)
        repayments_all = LoanRepayment.objects.filter(loan__member=member, date__gte=week_ago)
        loans_all = Loan.objects.filter(member=member, date_given__gte=week_ago)
        start_date = week_ago.date()
        end_date = timezone.now().date()
    elif date_filter == 'month':
        month_ago = timezone.now() - timedelta(days=30)
        contributions_all = Contribution.objects.filter(member=member, payment_verified=True, date__gte=month_ago)
        repayments_all = LoanRepayment.objects.filter(loan__member=member, date__gte=month_ago)
        loans_all = Loan.objects.filter(member=member, date_given__gte=month_ago)
        start_date = month_ago.date()
        end_date = timezone.now().date()
    elif date_filter == 'custom':
        if start_date and end_date:
            contributions_all = Contribution.objects.filter(
                member=member, payment_verified=True, date__date__gte=start_date, date__date__lte=end_date
            )
            repayments_all = LoanRepayment.objects.filter(
                loan__member=member, date__date__gte=start_date, date__date__lte=end_date
            )
            loans_all = Loan.objects.filter(
                member=member, date_given__date__gte=start_date, date_given__date__lte=end_date
            )
        else:
            contributions_all = Contribution.objects.filter(member=member, payment_verified=True)
            repayments_all = LoanRepayment.objects.filter(loan__member=member)
            loans_all = Loan.objects.filter(member=member)
            start_date = member.joined_date
            end_date = timezone.now().date()
    else:
        contributions_all = Contribution.objects.filter(member=member, payment_verified=True)
        repayments_all = LoanRepayment.objects.filter(loan__member=member)
        loans_all = Loan.objects.filter(member=member)
        start_date = member.joined_date
        end_date = timezone.now().date()
    
    summary = member_summary(member)
    
    # Combine all transactions
    transactions = []
    
    for c in contributions_all:
        transactions.append({
            'date': c.date,
            'transaction_type': 'Credit - Savings',
            'description': f'Contribution via {c.payment_method}',
            'reference': c.payment_reference or 'N/A',
            'debit': None,
            'credit': c.amount,
            'balance': Decimal('0'),
        })
    
    for l in loans_all:
        transactions.append({
            'date': l.date_given,
            'transaction_type': 'Debit - Loan Disbursed',
            'description': f'Loan granted with {l.interest_rate}% interest',
            'reference': f'LOAN-{l.id}',
            'debit': l.amount,
            'credit': None,
            'balance': Decimal('0'),
        })
    
    for r in repayments_all:
        transactions.append({
            'date': r.date,
            'transaction_type': 'Credit - Loan Repayment',
            'description': f'Repayment for loan #{r.loan.id}',
            'reference': f'REPAY-{r.id}',
            'debit': None,
            'credit': r.amount,
            'balance': Decimal('0'),
        })
    
    # Sort by date
    transactions.sort(key=lambda x: x['date'])
    
    # Calculate running balance
    running_balance = Decimal('0')
    for t in transactions:
        if t['credit']:
            running_balance += t['credit']
        if t['debit']:
            running_balance -= t['debit']
        t['balance'] = running_balance
    
    # Get last payment
    last_payment = contributions_all.order_by('-date').first()
    
    return render(request, "full_statement.html", {
        "member": member,
        "transactions": transactions,
        "summary": summary,
        "start_date": start_date,
        "end_date": end_date,
        "date_filter": date_filter,
        "last_payment": last_payment,
        "total_transactions": len(transactions),
        "print_date": timezone.now(),
    })

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

        callback_url = request.build_absolute_uri(f"/paystack/verify/{member.id}/")

        # Use database transaction with security
        try:
            from django.db import transaction
            with transaction.atomic():
                member = Member.objects.select_for_update().get(id=member.id)
                
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
                    
        except Exception as e:
            logger.error(f"Payment initialization error: {e}")
            messages.error(request, "An error occurred. Please try again.")
            return redirect("member_detail", member_id=member.id)

    return render(request, "make_payment.html", {
        "member": member,
        "paystack_public_key": settings.PAYSTACK_PUBLIC_KEY,
    })
    
# ── ALL MEMBERS STATEMENT (Admin Report) ────────────────────────────────────

@login_required
@finance_admin_required
def all_members_statement(request):
    """Generate a complete statement for ALL members - Admin only."""
    from decimal import Decimal
    from django.db.models import Sum
    
    # Get all members with their data
    members = Member.objects.all().order_by('full_name')
    
    member_data = []
    total_savings_all = Decimal('0')
    total_loans_all = Decimal('0')
    total_outstanding_all = Decimal('0')
    
    for member in members:
        summary = member_summary(member)
        member_data.append({
            'member': member,
            'total_savings': summary['total_savings'],
            'total_loans': summary['total_loans'],
            'total_repayments': summary['total_repayments'],
            'outstanding_loan': summary['outstanding_loan'],
            'loan_interest': summary['loan_interest'],
            'available_balance': summary['available_balance'],
        })
        
        total_savings_all += summary['total_savings']
        total_loans_all += summary['total_loans']
        total_outstanding_all += summary['outstanding_loan']
    
    # Get date range from request
    date_filter = request.GET.get('date_filter', 'all')
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    
    context = {
        'members': member_data,
        'total_members': len(member_data),
        'total_savings_all': total_savings_all,
        'total_loans_all': total_loans_all,
        'total_outstanding_all': total_outstanding_all,
        'date_filter': date_filter,
        'start_date': start_date,
        'end_date': end_date,
        'print_date': timezone.now(),
    }
    
    return render(request, "all_members_statement.html", context)    


# ── PAYSTACK: VERIFY PAYMENT ──────────────────────────────────────────────────

@login_required
def verify_payment(request, member_id):
    """Verify Paystack payment after callback - with security."""
    member = get_object_or_404(Member, id=member_id)
    reference = request.GET.get("reference", "")

    if not reference:
        messages.error(request, "No payment reference found.")
        return redirect("member_detail", member_id=member.id)

    try:
        # Check if already processed (idempotency)
        from django.db import transaction
        with transaction.atomic():
            member = Member.objects.select_for_update().get(id=member.id)
            
            # Prevent double-recording
            if Contribution.objects.filter(payment_reference=reference).exists():
                messages.warning(request, "This payment has already been recorded.")
                return redirect("member_detail", member_id=member.id)

            # Verify with Paystack
            result = verify_transaction(reference)

            if result["status"]:
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

                # Send confirmation email to member
                try:
                    send_member_confirmation_paystack(member, contribution)
                except Exception as e:
                    logger.error(f"Email error: {e}")

                messages.success(request, f"Payment of ₦{result['amount_naira']:,.2f} received and recorded! 🎉")
                return redirect("receipt", member_id=member.id, reference=reference)
            else:
                PaymentTransaction.objects.filter(reference=reference).update(status="failed")
                messages.error(request, f"Payment verification failed: {result.get('message', 'Unknown error')}")
                return redirect("member_detail", member_id=member.id)
                
    except Exception as e:
        logger.error(f"Payment verification error: {e}")
        messages.error(request, "An error occurred. Please try again.")
        return redirect("member_detail", member_id=member.id)


# ── PAYSTACK: RECEIPT ──────────────────────────────────────────────────────────

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


# ── PAYSTACK: WEBHOOK ──────────────────────────────────────────────────────────

@csrf_exempt
def paystack_webhook(request):
    """Handle Paystack webhook for automatic payment verification - with security."""
    if request.method != "POST":
        return HttpResponse(status=405)

    # Verify webhook signature
    paystack_sig = request.headers.get("x-paystack-signature", "")
    computed_sig = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(),
        request.body,
        hashlib.sha512
    ).hexdigest()

    if paystack_sig != computed_sig:
        logger.warning("Invalid webhook signature received")
        return HttpResponse(status=401)

    payload = json.loads(request.body)

    if payload.get("event") == "charge.success":
        data = payload["data"]
        reference = data.get("reference", "")
        amount = data.get("amount", 0) / 100
        member_id = data.get("metadata", {}).get("member_id")

        # Check if already processed (idempotency)
        if Contribution.objects.filter(payment_reference=reference).exists():
            logger.info(f"Webhook: Transaction {reference} already processed")
            return HttpResponse(status=200)

        try:
            from django.db import transaction
            with transaction.atomic():
                member = Member.objects.select_for_update().get(id=member_id)
                
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
                
                logger.info(f"Webhook: Transaction {reference} processed successfully")
                
        except Member.DoesNotExist:
            logger.error(f"Webhook: Member {member_id} not found")
        except Exception as e:
            logger.error(f"Webhook error: {e}")

    return HttpResponse(status=200)


# ── HELPER: Send Paystack Confirmation Email ─────────────────────────────────

def send_member_confirmation_paystack(member, contribution):
    """Send confirmation email for Paystack payment."""
    if not member.email:
        return
    
    subject = f"✅ Payment Confirmed - OmoJesu Savings"
    base_url = getattr(settings, 'BASE_URL', 'https://omojesu-savings.com')
    member_url = f"{base_url}/member/{member.id}/"
    
    html_message = render_to_string('email/payment_confirmed_paystack.html', {
        'member': member,
        'amount': contribution.amount,
        'transaction_ref': contribution.payment_reference,
        'date': contribution.date,
        'member_url': member_url,
    })
    
    plain_message = strip_tags(html_message)
    
    try:
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [member.email],
            html_message=html_message,
            fail_silently=False,
        )
    except Exception as e:
        logger.error(f"Email error: {e}")
        
        
# ── WEEKLY CONTRIBUTIONS REVIEW (Admin) ─────────────────────────────────────

@login_required
@finance_admin_required
def weekly_contributions(request):
    """View and review contributions by date/week."""
    from datetime import datetime, timedelta
    from django.utils import timezone
    from django.db.models import Sum  # ← ADD THIS INSIDE
    from decimal import Decimal
    
    # Get date from request or default to last Sunday
    selected_date = request.GET.get('date')
    date_filter = request.GET.get('date_filter', 'last_sunday')
    
    if selected_date:
        try:
            filter_date = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except ValueError:
            filter_date = timezone.now().date()
    else:
        filter_date = timezone.now().date()
    
    # Calculate date range based on filter
    if date_filter == 'today':
        start_date = timezone.now().date()
        end_date = start_date
    elif date_filter == 'last_sunday':
        # Get last Sunday
        today = timezone.now().date()
        days_since_sunday = today.weekday() + 1  # Monday=0, Sunday=6
        if days_since_sunday == 7:  # If today is Sunday
            start_date = today
        else:
            start_date = today - timedelta(days=days_since_sunday)
        end_date = start_date
    elif date_filter == 'this_week':
        today = timezone.now().date()
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
    elif date_filter == 'last_week':
        today = timezone.now().date()
        start_date = today - timedelta(days=today.weekday() + 7)
        end_date = start_date + timedelta(days=6)
    elif date_filter == 'month':
        start_date = filter_date.replace(day=1)
        next_month = start_date.replace(day=28) + timedelta(days=4)
        end_date = next_month - timedelta(days=next_month.day)
    else:
        start_date = filter_date
        end_date = filter_date
    
    # Get contributions for the date range
    contributions = Contribution.objects.filter(
        payment_verified=True,
        date__date__gte=start_date,
        date__date__lte=end_date
    ).order_by('-date')
    
    # Group by date
    contributions_by_date = {}
    for c in contributions:
        date_key = c.date.strftime('%Y-%m-%d')
        if date_key not in contributions_by_date:
            contributions_by_date[date_key] = []
        contributions_by_date[date_key].append(c)
    
    # Calculate totals
    total_contributions = contributions.count()
    total_amount = contributions.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    
    # Get unique members who contributed
    members_who_contributed = contributions.values_list('member', flat=True).distinct()
    total_members = len(members_who_contributed)
    
    context = {
        'contributions_by_date': contributions_by_date,
        'total_contributions': total_contributions,
        'total_amount': total_amount,
        'total_members': total_members,
        'start_date': start_date,
        'end_date': end_date,
        'date_filter': date_filter,
        'selected_date': filter_date,
        'print_date': timezone.now(),
        'contributions': contributions,
    }
    
    return render(request, "weekly_contributions.html", context)        