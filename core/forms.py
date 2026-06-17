from django import forms
from django.core.exceptions import ValidationError
from decimal import Decimal
from .models import Contribution, Loan, LoanRepayment, Member

_INPUT  = "w-full rounded-2xl bg-white/10 border border-white/20 p-4 text-white outline-none focus:border-emerald-400 transition"
_SELECT = _INPUT


# ── CONTRIBUTION ──────────────────────────────────────────────────────────────

class ContributionForm(forms.ModelForm):

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            try:
                self.fields['member'].queryset = Member.objects.exclude(user=user)
            except Exception:
                pass

    class Meta:
        model  = Contribution
        fields = ["member", "amount"]
        widgets = {
            "member": forms.Select(attrs={"class": _SELECT}),
            "amount": forms.NumberInput(attrs={
                "class": _INPUT, "placeholder": "Enter amount (₦)",
                "min": "0.01", "step": "0.01"
            }),
        }

    def clean_amount(self):
        v = self.cleaned_data["amount"]

        if v <= 0:
           raise ValidationError("Amount must be greater than zero.")

        if v < Decimal("100"):
           raise ValidationError(
            "Minimum contribution amount is ₦100."
        )

        return v


# ── LOAN ──────────────────────────────────────────────────────────────────────

class LoanForm(forms.ModelForm):

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            try:
                self.fields['member'].queryset = Member.objects.exclude(user=user)
            except Exception:
                pass

    class Meta:
        model  = Loan
        fields = ["member", "amount", "due_date"]
        widgets = {
            "member":   forms.Select(attrs={"class": _SELECT}),
            "amount":   forms.NumberInput(attrs={
                "class": _INPUT, "placeholder": "Loan amount (₦)",
                "min": "0.01", "step": "0.01"
            }),
            "due_date": forms.DateInput(attrs={"class": _INPUT, "type": "date"}),
        }

    def clean_amount(self):
        v = self.cleaned_data["amount"]
        if v <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return v

    def clean_due_date(self):
        from django.utils import timezone
        v = self.cleaned_data["due_date"]
        if v <= timezone.now().date():
            raise ValidationError("Due date must be in the future.")
        return v


# ── LOAN REPAYMENT ────────────────────────────────────────────────────────────

class LoanRepaymentForm(forms.ModelForm):

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user:
            try:
                # Only show unpaid loans that belong to other members (for admins)
                self.fields['loan'].queryset = Loan.objects.exclude(
                    member__user=user
                ).filter(is_paid=False)
            except Exception:
                pass

    class Meta:
        model  = LoanRepayment
        fields = ["loan", "amount"]
        widgets = {
            "loan":   forms.Select(attrs={"class": _SELECT}),
            "amount": forms.NumberInput(attrs={
                "class": _INPUT, "placeholder": "Repayment amount (₦)",
                "min": "0.01", "step": "0.01"
            }),
        }

    def clean_amount(self):
        v = self.cleaned_data["amount"]
        if v <= 0:
            raise ValidationError("Amount must be greater than zero.")
        return v

    def clean(self):
        """FIXED: Validate repayment doesn't exceed balance"""
        cleaned_data = super().clean()
        loan = cleaned_data.get("loan")
        amount = cleaned_data.get("amount")
        
        if loan and amount:
            # Check if the loan exists and calculate remaining balance
            remaining = loan.balance_remaining
            if amount > remaining:
                raise ValidationError(
                    f"Repayment exceeds remaining balance of ₦{remaining:,.2f}"
                )
        return cleaned_data


# ── ADD MEMBER (admin) ────────────────────────────────────────────────────────

class MemberForm(forms.ModelForm):
    class Meta:
        model  = Member
        fields = ["full_name", "email", "phone", "address", "card_number"]  # ← ADD 'email'
        widgets = {
            "full_name":   forms.TextInput(attrs={"class": _INPUT, "placeholder": "Full name"}),
            "email":       forms.EmailInput(attrs={"class": _INPUT, "placeholder": "Email address"}),  # ← ADD THIS
            "phone":       forms.TextInput(attrs={"class": _INPUT, "placeholder": "Phone number"}),
            "address":     forms.Textarea(attrs={"class": _INPUT, "placeholder": "Address", "rows": 3}),
            "card_number": forms.TextInput(attrs={"class": _INPUT, "placeholder": "Card number"}),
        }


# ── PROFILE UPDATE (member edits own profile) ─────────────────────────────────

class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model  = Member
        fields = [
            "profile_image",
            "email", 
            "phone",
            "address",
            "bank_name",
            "account_number",
            "account_name",
        ]
        widgets = {
            "email":          forms.EmailInput(attrs={"class": _INPUT, "placeholder": "Email address"}),
            "phone":          forms.TextInput(attrs={"class": _INPUT, "placeholder": "Phone number"}),
            "address":        forms.Textarea(attrs={"class": _INPUT, "placeholder": "Address", "rows": 3}),
            "bank_name":      forms.TextInput(attrs={"class": _INPUT, "placeholder": "e.g. First Bank, GTBank, Zenith"}),
            "account_number": forms.TextInput(attrs={"class": _INPUT, "placeholder": "10-digit account number"}),
            "account_name":   forms.TextInput(attrs={"class": _INPUT, "placeholder": "Account name (as on bank records)"}),
        }

    def clean_account_number(self):
        v = self.cleaned_data.get("account_number", "").strip()
        if v and not v.isdigit():
            raise ValidationError("Account number must contain digits only.")
        if v and len(v) != 10:
            raise ValidationError("Nigerian account numbers must be exactly 10 digits.")
        return v


# ── MEMBER REGISTRATION ───────────────────────────────────────────────────────

class MemberRegistrationForm(forms.Form):
    card_number = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={
            "class": _INPUT,
            "placeholder": "Your card number (e.g. OMJ-001)",
            "autocomplete": "off",
        })
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": _INPUT,
            "placeholder": "Choose a username",
            "autocomplete": "username",
        })
    )
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={
            "class": _INPUT,
            "placeholder": "Password (min. 8 characters)",
            "autocomplete": "new-password",
        })
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "class": _INPUT,
            "placeholder": "Confirm password",
            "autocomplete": "new-password",
        })
    )
    
    def clean_card_number(self):
        card_number = self.cleaned_data.get("card_number", "").strip()
        if not card_number:
            raise ValidationError("Card number is required.")
        if not Member.objects.filter(
            card_number=card_number
        ).exists():
            raise ValidationError("Invalid card number.")
        return card_number

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password")
        p2 = cleaned.get("confirm_password")
        if p1 and p2 and p1 != p2:
            raise ValidationError("Passwords do not match.")
        return cleaned