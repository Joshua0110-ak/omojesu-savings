from django.core.cache import cache
from django.http import HttpResponseForbidden
from django.conf import settings
from django.utils import timezone
from .models import AuditLog
import logging

logger = logging.getLogger(__name__)


class RateLimitMiddleware:
    """Rate limiting middleware for security."""
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Skip rate limiting for admins
        if request.user.is_authenticated and request.user.is_staff:
            return self.get_response(request)
        
        # Get client IP
        ip = self.get_client_ip(request)
        
        # Define rate limits for different endpoints
        rate_limits = {
            '/login/': ('login_attempts', settings.RATE_LIMIT_LOGIN, 3600),  # 5 per hour
            '/opay/confirm/': ('payment_attempts', settings.RATE_LIMIT_PAYMENT, 3600),  # 10 per hour
            '/withdraw/': ('withdrawal_attempts', settings.RATE_LIMIT_WITHDRAWAL, 3600),  # 3 per hour
        }
        
        for path, (key, limit, period) in rate_limits.items():
            if request.path.startswith(path):
                cache_key = f"{key}:{ip}"
                attempts = cache.get(cache_key, 0)
                
                if attempts >= limit:
                    logger.warning(f"Rate limit exceeded for {ip} on {path}")
                    return HttpResponseForbidden("Too many attempts. Please try again later.")
                
                cache.set(cache_key, attempts + 1, period)
                break
        
        return self.get_response(request)
    
    def get_client_ip(self, request):
        """Get client IP address."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip


class AuditLogMiddleware:
    """Audit logging middleware for security."""
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        response = self.get_response(request)
        
        # Log important actions
        if request.user.is_authenticated and request.method == 'POST':
            sensitive_actions = [
                '/opay/confirm/',
                '/opay/confirm-payment/',
                '/add-contribution/',
                '/give-loan/',
                '/add-repayment/',
                '/withdraw/',
                '/change-password/',
                '/logout/',
            ]
            
            for action in sensitive_actions:
                if request.path.startswith(action):
                    self.log_audit(request, response)
                    break
        
        return response
    
    def log_audit(self, request, response):
        """Create audit log entry."""
        try:
            AuditLog.objects.create(
                user=request.user,
                action=request.path,
                ip_address=self.get_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
                method=request.method,
                data=request.POST.dict() if request.POST else {},
                status_code=response.status_code,
            )
        except Exception as e:
            logger.error(f"Audit log error: {e}")
    
    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip