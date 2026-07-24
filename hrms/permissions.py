"""
Role model used across HRMS, built on top of `inventory.User`:

    is_superuser = True   -> SUPERADMIN  (you) - full Django admin/settings access
    is_accountant = True  -> HR / MANAGER - admin-level access inside the HRMS app
    is_viewer = True      -> EMPLOYEE - self-service / read-only access

A user should realistically only be ONE of these at a time (is_accountant XOR
is_viewer), enforced by `get_role()` below (superuser always wins).
"""
from functools import wraps

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect


ROLE_SUPERADMIN = 'superadmin'
ROLE_HR = 'hr'
ROLE_EMPLOYEE = 'employee'


def get_role(user):
    """Returns one of ROLE_SUPERADMIN / ROLE_HR / ROLE_EMPLOYEE for a logged-in user."""
    if not user.is_authenticated:
        return None
    if user.is_superuser:
        return ROLE_SUPERADMIN
    if getattr(user, 'is_accountant', False):
        return ROLE_HR
    return ROLE_EMPLOYEE


def is_hr_or_above(user):
    return get_role(user) in (ROLE_SUPERADMIN, ROLE_HR)


def is_employee(user):
    return get_role(user) == ROLE_EMPLOYEE


def get_employee_profile(user):
    """Returns the Employee record linked to this login, or None."""
    return getattr(user, 'employee_profile', None)


# ---------------------------------------------------------------------------
# Function-based view decorator
# ---------------------------------------------------------------------------
def hr_required(view_func):
    """Allows SUPERADMIN and HR/Manager only. Employees get redirected."""
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not is_hr_or_above(request.user):
            raise PermissionDenied('HR/Manager or Admin access required.')
        return view_func(request, *args, **kwargs)
    return _wrapped


# ---------------------------------------------------------------------------
# Class-based view mixins
# ---------------------------------------------------------------------------
class SuperAdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Use on CBVs restricted to the superadmin only (e.g. Company/org settings)."""

    def test_func(self):
        return get_role(self.request.user) == ROLE_SUPERADMIN

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied('Super Admin access required.')


class HRRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Use on CBVs that only HR/Manager or Superadmin should reach
    (create/edit/delete of Company, Employee, Payroll, etc)."""

    def test_func(self):
        return is_hr_or_above(self.request.user)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied('HR/Manager or Admin access required.')


class EmployeeSelfOrHRMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Use on CBVs where an employee may view/edit only their own record
    (e.g. their own attendance / leave / payslips), while HR/Admin can see all."""

    #: override in subclass — should return the Employee instance for this object
    def get_object_employee(self, obj):
        return getattr(obj, 'employee', obj)

    def test_func(self):
        user = self.request.user
        if is_hr_or_above(user):
            return True
        obj = self.get_object() if hasattr(self, 'get_object') else None
        if obj is None:
            return True  # list views: filtered in get_queryset instead
        employee = get_employee_profile(user)
        return employee is not None and self.get_object_employee(obj) == employee
