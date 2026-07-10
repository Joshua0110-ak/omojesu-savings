from django import template
from decimal import Decimal

register = template.Library()


@register.filter
def naira(value):
    """Convert a number to Nigerian Naira format."""
    if value is None:
        return "₦0.00"
    if isinstance(value, (int, float, Decimal)):
        return f"₦{value:,.2f}"
    return value


@register.filter
def subtract(value, arg):
    """Subtract arg from value."""
    try:
        return value - arg
    except (TypeError, ValueError):
        return value


@register.filter
def percentage(value, total):
    """Calculate percentage."""
    try:
        if total and total > 0:
            return round((value / total) * 100, 1)
        return 0
    except (TypeError, ZeroDivisionError):
        return 0
    
@register.filter
def sum_amount(contribs):
    """Sum the amounts in a list of contributions."""
    from decimal import Decimal
    total = Decimal('0')
    for c in contribs:
        total += c.amount
    return total    