from django import template
from decimal import Decimal

register = template.Library()


@register.filter
def naira(value):
    """
    Format a number as Nigerian Naira with commas and no trailing zeros.
    e.g. 25000.00 → ₦25,000
         2000.5   → ₦2,000.50
         0        → ₦0
    """
    try:
        value = Decimal(str(value))
        # If it's a whole number, show no decimals
        if value == value.to_integral_value():
            formatted = f"{int(value):,}"
        else:
            # Show up to 2 decimal places, strip trailing zeros
            formatted = f"{value:,.2f}".rstrip('0').rstrip('.')
        return f"₦{formatted}"
    except Exception:
        return f"₦{value}"
