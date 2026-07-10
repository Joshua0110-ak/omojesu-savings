from decimal import Decimal
from django.db.models import Sum, Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone
import datetime
from .models import Member, Contribution, Loan, LoanRepayment


# ── PER-MEMBER SERVICES ───────────────────────────────────────────────────────

def total_savings(member):
    """Get total savings for a member."""
    result = member.contributions.filter(payment_verified=True).aggregate(t=Sum('amount'))['t']
    return result or Decimal('0')

def total_loans(member):
    """Get total loans for a member."""
    result = member.loans.aggregate(t=Sum('amount'))['t']
    return result or Decimal('0')

def total_loan_interest(member):
    """Calculate total interest on all loans for a member."""
    loans = Loan.objects.filter(member=member)
    return sum(l.amount * l.interest_rate / 100 for l in loans) or Decimal('0')

def total_repayments(member):
    """Get total repayments for a member."""
    result = LoanRepayment.objects.filter(
        loan__member=member
    ).aggregate(t=Sum('amount'))['t']
    return result or Decimal('0')

def outstanding_loan(member):
    """Calculate outstanding loan balance for a member."""
    total_due = Decimal("0")
    loans = Loan.objects.filter(
        member=member,
        is_paid=False
    )
    for loan in loans:
        total_due += loan.total_with_interest()
    outstanding = total_due - total_repayments(member)
    return max(outstanding, Decimal("0"))

def member_summary(member):
    """Get complete summary for a single member."""
    savings = total_savings(member)
    loans = total_loans(member)
    repayments = total_repayments(member)
    outstanding = outstanding_loan(member)
    interest = total_loan_interest(member)
    
    return {
        "total_savings": savings,
        "total_loans": loans,
        "total_repayments": repayments,
        "outstanding_loan": outstanding,
        "loan_interest": interest,
        "available_balance": max(savings - outstanding, Decimal('0')),
    }


# ── MONTHLY CHART DATA (per member) ──────────────────────────────────────────

def contributions_by_month(member):
    """Returns (labels, data) for last 6 months of verified contributions."""
    six_months_ago = timezone.now() - datetime.timedelta(days=180)
    qs = (
        Contribution.objects
        .filter(member=member, date__gte=six_months_ago, payment_verified=True)
        .annotate(month=TruncMonth('date'))
        .values('month')
        .annotate(total=Sum('amount'))
        .order_by('month')
    )
    labels = [r['month'].strftime('%b %Y') for r in qs]
    data   = [float(r['total']) for r in qs]
    return labels, data


# ── ORG-WIDE ADMIN DASHBOARD STATS (OPTIMIZED) ──────────────────────────────

def org_summary():
    """Get organization-wide statistics with optimized queries."""
    
    # Basic counts - these are cheap
    total_members = Member.objects.count()
    registered_members = Member.objects.filter(user__isnull=False).count()
    
    # Aggregated totals - single queries each
    total_savings_all = Contribution.objects.filter(
        payment_verified=True
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0')
    
    total_loans_all = Loan.objects.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    total_repay_all = LoanRepayment.objects.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    outstanding_all = max(total_loans_all - total_repay_all, Decimal('0'))

    # Monthly savings trend (last 6 months, all members, verified only)
    six_months_ago = timezone.now() - datetime.timedelta(days=180)
    monthly_qs = (
        Contribution.objects
        .filter(date__gte=six_months_ago, payment_verified=True)
        .annotate(month=TruncMonth('date'))
        .values('month')
        .annotate(total=Sum('amount'))
        .order_by('month')
    )
    chart_labels = [r['month'].strftime('%b %Y') for r in monthly_qs]
    chart_data = [float(r['total']) for r in monthly_qs]

    # OPTIMIZED: Top 5 savers using a single query with annotations
    top_savers_data = (
        Member.objects
        .annotate(
            total_saved=Sum('contributions__amount', filter=Q(contributions__payment_verified=True))
        )
        .filter(total_saved__isnull=False)
        .order_by('-total_saved')[:5]
    )
    top_savers = [(m, m.total_saved or Decimal('0')) for m in top_savers_data]

    # OPTIMIZED: Members with outstanding loans using annotations
    from django.db.models import Subquery, OuterRef, Value, DecimalField
    from django.db.models.functions import Coalesce
    
    # Get all members with loan balances
    debtors_data = []
    for m in Member.objects.prefetch_related('loans'):
        balance = outstanding_loan(m)
        if balance > 0:
            debtors_data.append((m, balance))
    debtors_data = sorted(debtors_data, key=lambda x: x[1], reverse=True)[:5]

    return {
        "total_members": total_members,
        "registered_members": registered_members,
        "total_savings": total_savings_all,
        "total_loans": total_loans_all,
        "total_repayments": total_repay_all,
        "outstanding_loans": outstanding_all,
        "chart_labels": chart_labels,
        "chart_data": chart_data,
        "top_savers": top_savers,
        "debtors": debtors_data,
    }

def payment_summary():
    """Get breakdown of manual vs Paystack payments."""
    manual = Contribution.objects.filter(
        payment_method="Manual", payment_verified=True
    ).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0")

    paystack = Contribution.objects.filter(
        payment_method="Paystack", payment_verified=True
    ).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0")

    return {
        "manual_total": manual,
        "paystack_total": paystack,
    }
    
    
# ── TOTAL INTEREST ACCRUED (All Members) ─────────────────────────────────────

def total_interest_all():
    """Calculate total interest accrued across ALL members."""
    from decimal import Decimal
    loans = Loan.objects.all()
    total = Decimal('0')
    for loan in loans:
        total += loan.amount * loan.interest_rate / 100
    return total


# ── TOTAL REPAYMENTS (All Members) ───────────────────────────────────────────

def total_repayments_all():
    """Calculate total repayments made across ALL members."""
    result = LoanRepayment.objects.aggregate(t=Sum('amount'))['t']
    return result or Decimal('0')

def has_pending_loan(member):
    """Check if a member has any pending (unpaid) loans."""
    return Loan.objects.filter(member=member, is_paid=False).exists()


def get_pending_loans(member):
    """Get all pending loans for a member."""
    return Loan.objects.filter(member=member, is_paid=False)    