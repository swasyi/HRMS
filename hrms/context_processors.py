from .permissions import get_role, is_hr_or_above, ROLE_SUPERADMIN, ROLE_HR, ROLE_EMPLOYEE


def hrms_role(request):
    """Adds `hrms_role` and `hrms_is_hr` to every template's context."""
    user = getattr(request, 'user', None)
    role = get_role(user) if user else None
    return {
        'hrms_role': role,
        'hrms_is_hr': is_hr_or_above(user) if user else False,
        'ROLE_SUPERADMIN': ROLE_SUPERADMIN,
        'ROLE_HR': ROLE_HR,
        'ROLE_EMPLOYEE': ROLE_EMPLOYEE,
    }
