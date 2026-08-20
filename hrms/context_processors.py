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

#if superuser, then can see any companies data and Hr will see their own company
# context_processors.py
from .models import Company
from .permissions import get_employee_profile


def hrms_company_context(request):
    if not request.user.is_authenticated:
        return {}

    user = request.user
    active_company_id = request.session.get('active_company_id')

    # 1. Determine available companies for this user
    if user.is_superuser:
        available_companies = Company.objects.all()
    else:
        profile = get_employee_profile(user)
        if profile:
            # Union of primary company + managed companies
            available_companies = Company.objects.filter(
                id=profile.company_id
            ) | profile.managed_companies.all()
            available_companies = available_companies.distinct()
        else:
            available_companies = Company.objects.none()

    # 2. Get the actual Company object for the "Active" one
    active_company = None
    if active_company_id and active_company_id != 'all':
        active_company = Company.objects.filter(id=active_company_id).first()

    return {
        'available_companies': available_companies,
        'active_company': active_company,  # None means "Global"
        'active_company_id': active_company_id,
    }