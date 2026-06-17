from functools import wraps
from django.shortcuts import redirect
from django.http import HttpResponseForbidden


def finance_admin_required(view_func):

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):

        if not request.user.is_authenticated:
            return redirect('login')

        # Allow superusers
        if request.user.is_superuser:
            return view_func(request, *args, **kwargs)

        # Allow staff users
        if request.user.is_staff:
            return view_func(request, *args, **kwargs)

        return HttpResponseForbidden(
            "You don't have permission to access this page. "
            "Only finance admins can perform this action."
        )

    return wrapper