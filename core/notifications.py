"""
Central place for all outgoing notification emails.
SMS can be added later by adding a send_sms() helper here and calling it
alongside each email function below — nothing else needs to change.
"""
import logging
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


def _send(subject, template, context, to_email):
    if not to_email:
        return
    html_message = render_to_string(template, context)
    plain_message = strip_tags(html_message)
    try:
        send_mail(
            subject,
            plain_message,
            settings.DEFAULT_FROM_EMAIL,
            [to_email],
            html_message=html_message,
            fail_silently=False,
        )
    except Exception as e:
        logger.error(f"Email send error ({template}): {e}")


def _member_url(member):
    base_url = getattr(settings, "BASE_URL", "https://omojesu-savings.com")
    return f"{base_url}/member/{member.id}/"


def notify_paystack_payment(member, contribution):
    _send(
        "✅ Payment Confirmed - OmoJesu Savings",
        "email/payment_confirmed_paystack.html",
        {
            "member": member,
            "amount": contribution.amount,
            "transaction_ref": contribution.payment_reference,
            "date": contribution.date,
            "member_url": _member_url(member),
        },
        member.email,
    )


def notify_contribution_verified(contribution):
    _send(
        "✅ Payment Verified - OmoJesu Savings",
        "email/contribution_verification_result.html",
        {
            "member": contribution.member,
            "amount": contribution.amount,
            "verified": True,
            "member_url": _member_url(contribution.member),
        },
        contribution.member.email,
    )


def notify_contribution_rejected(contribution):
    _send(
        "🚫 Payment Proof Rejected - OmoJesu Savings",
        "email/contribution_verification_result.html",
        {
            "member": contribution.member,
            "amount": contribution.amount,
            "verified": False,
            "reason": contribution.rejection_reason,
            "member_url": _member_url(contribution.member),
        },
        contribution.member.email,
    )


def notify_loan_approved(loan):
    _send(
        "🎉 Loan Approved - OmoJesu Savings",
        "email/loan_status_update.html",
        {
            "member": loan.member,
            "amount": loan.amount,
            "approved": True,
            "interest_rate": loan.interest_rate,
            "due_date": loan.due_date,
            "member_url": _member_url(loan.member),
        },
        loan.member.email,
    )


def notify_loan_rejected(loan):
    _send(
        "🚫 Loan Rejected - OmoJesu Savings",
        "email/loan_status_update.html",
        {
            "member": loan.member,
            "amount": loan.amount,
            "approved": False,
            "reason": loan.rejection_reason,
            "member_url": _member_url(loan.member),
        },
        loan.member.email,
    )


def notify_compulsory_approver(loan, compulsory_member):
    """Alert the compulsory approver that a loan is waiting on her final sign-off."""
    if not compulsory_member or not compulsory_member.email:
        return
    base_url = getattr(settings, "BASE_URL", "https://omojesu-savings.com")
    _send(
        "✅ A loan needs your final approval - OmoJesu Savings",
        "email/compulsory_approver_alert.html",
        {
            "approver": compulsory_member,
            "member_name": loan.member.full_name,
            "amount": loan.amount,
            "first_approver": loan.approved_by_first.username if loan.approved_by_first else "—",
            "approvals_url": f"{base_url}/loan-approvals/",
        },
        compulsory_member.email,
    )


def send_password_reset_email(member, reset_url):
    _send(
        "🔒 Reset Your Password - OmoJesu Savings",
        "email/password_reset_email.html",
        {"member": member, "reset_url": reset_url},
        member.email,
    )