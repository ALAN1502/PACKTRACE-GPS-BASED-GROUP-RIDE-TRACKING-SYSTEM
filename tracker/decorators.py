from functools import wraps
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import redirect

def admin_required(view_func):
    """
    Decorator for views that checks if the user is authenticated and is an Admin.
    Rejects normal riders with 403 Forbidden.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')

        profile = getattr(request.user, 'profile', None)
        is_admin_user = (
            request.user.is_staff or
            request.user.is_superuser or
            (profile and profile.is_admin)
        )

        if not is_admin_user:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.path.startswith('/api/'):
                return JsonResponse({'error': 'Admin authorization required. Access denied.'}, status=403)
            return HttpResponseForbidden("Access Denied: You do not have permission to view Admin functionality.")

        return view_func(request, *args, **kwargs)
    return _wrapped_view
