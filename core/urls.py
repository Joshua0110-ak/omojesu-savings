from django.urls import path
from django.contrib.auth import views as auth_views

from .views import (
    statement_pdf,
    login_view,
    logout_view,
    register_view,
    admin_dashboard,
    member_detail,
    member_search,
    edit_profile,
    add_contribution,
    add_loan,
    add_repayment,
    add_member,
    password_change_done_redirect,
    initialize_payment,
    verify_payment,
    receipt,
    paystack_webhook,
)

urlpatterns = [

    # ── AUTH ──────────────────────────────────────────────────────────────────
    path("",          login_view,    name="login"),
    path("logout/",   logout_view,   name="logout"),
    path("register/", register_view, name="register"),

    # ── DASHBOARDS ────────────────────────────────────────────────────────────
    path("dashboard/",                   admin_dashboard, name="admin_dashboard"),
    path("members/",                     member_search,   name="member_search"),
    path("member/<int:member_id>/",      member_detail,   name="member_detail"),
    path("member/<int:member_id>/edit/", edit_profile,    name="edit_profile"),

    # ── PDF STATEMENT ─────────────────────────────────────────────────────────
    path("member/<int:member_id>/statement/", statement_pdf, name="statement_pdf"),

    # ── TRANSACTIONS ──────────────────────────────────────────────────────────
    path("add-contribution/", add_contribution, name="add_contribution"),
    path("give-loan/",        add_loan,         name="add_loan"),
    path("add-repayment/",    add_repayment,    name="add_repayment"),
    path("add-member/",       add_member,       name="add_member"),

    # ── PAYSTACK ──────────────────────────────────────────────────────────────
    path(
        "paystack/pay/<int:member_id>/",
        initialize_payment,
        name="initialize_payment"
    ),
    path(
        "paystack/verify/<int:member_id>/",
        verify_payment,
        name="verify_payment"
    ),
    path(
        "paystack/receipt/<int:member_id>/<str:reference>/",
        receipt,
        name="receipt"
    ),
    path(
        "paystack/webhook/",
        paystack_webhook,
        name="paystack_webhook"
    ),

    # ── PASSWORD ──────────────────────────────────────────────────────────────
    path(
        "change-password/",
        auth_views.PasswordChangeView.as_view(
            template_name="change_password.html",
            success_url="/password-changed/"
        ),
        name="change_password"
    ),
    path(
        "password-changed/",
        password_change_done_redirect,
        name="password_change_done"
    ),
]