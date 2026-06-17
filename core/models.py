from django.db import models
from django.db.models import Sum
from django.contrib.auth.models import User
from django.db.models.signals import post_delete
from django.dispatch import receiver


class Member(models.Model):

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='member'
    )

    profile_image = models.ImageField(
        upload_to="members/",
        null=True,
        blank=True
    )

    is_finance_admin = models.BooleanField(default=False)

    full_name    = models.CharField(max_length=150)
    email        = models.EmailField(max_length=254, blank=True, null=True)  # ← ADD THIS LINE
    phone        = models.CharField(max_length=20, blank=True)
    address      = models.TextField(blank=True)
    card_number  = models.CharField(max_length=20, unique=True)
    joined_date  = models.DateField(auto_now_add=True)

    # Bank Account Details
    bank_name      = models.CharField(max_length=100, blank=True)
    account_number = models.CharField(max_length=20, blank=True)
    account_name   = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self):
        return f"{self.full_name} ({self.card_number})"


class Contribution(models.Model):

    PAYMENT_METHODS = (
        ("Manual",   "Manual"),
        ("Paystack", "Paystack"),
    )

    member = models.ForeignKey(
        Member,
        on_delete=models.CASCADE,
        related_name="contributions"
    )

    amount = models.DecimalField(max_digits=10, decimal_places=2)

    payment_method = models.CharField(
        max_length=20,
        choices=PAYMENT_METHODS,
        default="Manual"
    )

    payment_reference = models.CharField(max_length=100, blank=True)

    # FIX: added — prevents fake/unverified Paystack callbacks from being counted
    payment_verified = models.BooleanField(default=False)

    date        = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.member.full_name} - ₦{self.amount}"


class Loan(models.Model):

    STATUS_CHOICES = (
        ("Pending",   "Pending"),
        ("Approved",  "Approved"),
        ("Rejected",  "Rejected"),
        ("Completed", "Completed"),
    )

    member = models.ForeignKey(
        Member, 
        on_delete=models.CASCADE,
        related_name='loans'  
    )
    amount        = models.DecimalField(max_digits=10, decimal_places=2)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=10)
    date_given    = models.DateTimeField(auto_now_add=True)
    due_date      = models.DateField()
    is_paid       = models.BooleanField(default=False)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="Pending"
    )

    recorded_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name="recorded_loans"
    )

    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_loans"
    )

    def total_with_interest(self):
        return self.amount + (self.amount * self.interest_rate / 100)

    @property
    def total_repaid(self):
        total = self.repayments.aggregate(total=Sum("amount"))["total"]
        return total or 0

    @property
    def balance_remaining(self):
        return self.total_with_interest() - self.total_repaid

    def __str__(self):
        return f"{self.member.full_name} - Loan ₦{self.amount}"


class LoanRepayment(models.Model):

    loan = models.ForeignKey(
        Loan,
        on_delete=models.CASCADE,
        related_name="repayments"
    )

    amount      = models.DecimalField(max_digits=10, decimal_places=2)
    date        = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"Repayment ₦{self.amount} on {self.loan}"


# ── PAYSTACK TRANSACTION LOG ──────────────────────────────────────────────────
# Tracks every payment attempt for audit + debugging

class PaymentTransaction(models.Model):

    STATUS_CHOICES = (
        ("pending",   "Pending"),
        ("success",   "Success"),
        ("failed",    "Failed"),
    )

    member    = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="transactions")
    amount    = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=100, unique=True)
    status    = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    gateway   = models.CharField(max_length=50, default="Paystack")
    verified  = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.member.full_name} — ₦{self.amount} ({self.status})"


# ── SIGNAL: auto-delete User when Member is deleted ───────────────────────────

@receiver(post_delete, sender=Member)
def delete_linked_user(sender, instance, **kwargs):
    if instance.user:
        instance.user.delete()