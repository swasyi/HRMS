from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.views.generic import TemplateView, UpdateView, ListView, DetailView, FormView

from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse_lazy, reverse
from django.utils import timezone
from django.db.models import Q, Count, Max
from .forms import UnifiedCandidateForm
from . import models
from . import models as m
from .permissions import get_role, is_hr_or_above, get_employee_profile, ROLE_SUPERADMIN, ROLE_HR, ROLE_EMPLOYEE


# mixins.py or views.py
class CompanyFilterMixin:
    """Filters querysets based on the session's active_company_id and user role."""

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return queryset.none()

        active_id = self.request.session.get('active_company_id')

        # 1. If a specific company is selected in session
        if active_id and active_id != 'all':
            if hasattr(self.model, 'company'):
                return queryset.filter(company_id=active_id)
            elif hasattr(self.model, 'employee'):
                return queryset.filter(employee__company_id=active_id)
            elif hasattr(self.model, 'e_name'):
                return queryset.filter(e_name__company_id=active_id)

        # 2. If 'All' / global view is selected and user is Super Admin
        if user.is_superuser:
            return queryset

        # 3. Non-Superuser (HR / Manager / Employee) — filter by managed/assigned companies
        profile = get_employee_profile(user)
        if profile:
            first_managed = profile.managed_companies.first() if hasattr(profile, 'managed_companies') else None
            locked = first_managed or profile.company
            if locked:
                if hasattr(self.model, 'company'):
                    return queryset.filter(company_id=locked.id)
                elif hasattr(self.model, 'employee'):
                    return queryset.filter(employee__company_id=locked.id)
                elif hasattr(self.model, 'e_name'):
                    return queryset.filter(e_name__company_id=locked.id)

        return queryset

class HRMSLoginView(LoginView):
    template_name = 'hrms/login.html'

    def get_success_url(self):
        return reverse_lazy('hrms:dashboard')

def set_active_company(request):
    """Handles both POST and GET company switching."""
    company_id = request.POST.get('company_id') or request.GET.get('company_id')
    if company_id == 'all':
        request.session['active_company_id'] = 'all'
    elif company_id:
        request.session['active_company_id'] = str(company_id)
    return redirect(request.META.get('HTTP_REFERER', 'hrms:dashboard'))


@login_required
def dashboard(request):
    user = request.user
    role = get_role(user)

    # 1. Date Filtering (Calendar Support via GET query param)
    target_date_str = request.GET.get('date')
    if target_date_str:
        try:
            target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        except ValueError:
            target_date = date.today()
    else:
        target_date = date.today()

    today = date.today()
    is_today = (target_date == today)

    context = {
        'active_group': 'dashboard',
        'active_item': 'dashboard',
        'selected_date': target_date.strftime('%Y-%m-%d'),
        'display_date': target_date.strftime('%d %b, %Y'),
        'is_today': is_today,
    }

    if is_hr_or_above(user):
        # --- 2. Permission-Based Company List ---
        if user.is_superuser:
            available_companies = m.Company.objects.all()
        else:
            employee_profile = get_employee_profile(user)
            if employee_profile:
                # HR sees primary company + managed companies
                available_companies = m.Company.objects.filter(
                    Q(id=employee_profile.company_id) |
                    Q(managed_companies__id=employee_profile.id)
                ).distinct()
            else:
                available_companies = m.Company.objects.none()

        active_company_id = request.session.get('active_company_id')

        # --- 3. Robust Permission-Based Filters ---
        emp_filter = Q(status=m.Employee.Status.ACTIVE)
        leave_filter = Q(status=m.LeaveApplication.Status.PENDING)
        job_filter = Q(is_active=True)
        attend_filter = Q(attendance_date=target_date)

        if active_company_id and active_company_id != "all":
            emp_filter &= Q(company_id=active_company_id)
            leave_filter &= Q(employee__company_id=active_company_id)
            job_filter &= Q(company_id=active_company_id)
            attend_filter &= Q(employee__company_id=active_company_id)
        elif not user.is_superuser:
            allowed_ids = list(available_companies.values_list('id', flat=True))
            emp_filter &= Q(company_id__in=allowed_ids)
            leave_filter &= Q(employee__company_id__in=allowed_ids)
            job_filter &= Q(company_id__in=allowed_ids)
            attend_filter &= Q(employee__company_id__in=allowed_ids)

        # --- 4. Fetch Optimized Data ---
        active_employees = m.Employee.objects.filter(emp_filter).select_related('designation', 'department', 'company')
        attendance_records = m.AttendanceRecord.objects.filter(attend_filter).select_related('employee')
        attendance_map = {r.employee_id: r for r in attendance_records}

        # Identify employees with approved leave on target_date
        on_leave_emp_ids = set(m.LeaveApplication.objects.filter(
            status=m.LeaveApplication.Status.APPROVED,
            start_date__lte=target_date,
            end_date__gte=target_date
        ).values_list('employee_id', flat=True))

        # --- 5. Categorize Attendance with Grace Period & Live Timing ---
        present_employees_detailed = []
        absent_employees = []
        on_leave_employees = []
        all_employees_detailed = []
        currently_working_count = 0

        for emp in active_employees:
            record = attendance_map.get(emp.id)
            policy = emp.company.get_policy_for_date(target_date) if emp.company else None
            grace_minutes = policy.grace_minutes if policy else 15

            if record and (record.check_in or record.status in [m.AttendanceRecord.Status.PRESENT, m.AttendanceRecord.Status.HALF_DAY]):
                # Status checks: Grace vs Late vs On-Time
                is_grace = False
                is_late = False

                if record.late_minutes > 0:
                    if record.late_minutes <= grace_minutes:
                        is_grace = True
                    else:
                        is_late = True
                elif record.check_in and policy and policy.office_start_time:
                    local_in = timezone.localtime(record.check_in).time()
                    office_start = policy.office_start_time
                    if local_in > office_start:
                        dummy_d = date(2000, 1, 1)
                        diff_sec = (datetime.combine(dummy_d, local_in) - datetime.combine(dummy_d, office_start)).total_seconds()
                        diff_m = int(diff_sec / 60)
                        if diff_m <= grace_minutes:
                            is_grace = True
                        else:
                            is_late = True

                # Check if currently working (checked in, not checked out)
                is_running = False
                if record.check_in and not record.check_out:
                    currently_working_count += 1
                    if is_today:
                        is_running = True

                # Total hours display
                total_hours_display = ""
                if record.total_hours and record.total_hours > 0:
                    hrs = int(record.total_hours)
                    mins = int((record.total_hours - hrs) * 60)
                    total_hours_display = f"{hrs:02d}h {mins:02d}m"
                elif record.check_in and record.check_out:
                    diff_sec = (record.check_out - record.check_in).total_seconds()
                    hrs = int(diff_sec // 3600)
                    mins = int((diff_sec % 3600) // 60)
                    total_hours_display = f"{hrs:02d}h {mins:02d}m"

                emp_item = {
                    'emp': emp,
                    'record': record,
                    'status_category': 'present',
                    'check_in': record.check_in,
                    'check_out': record.check_out,
                    'check_in_iso': record.check_in.isoformat() if record.check_in else '',
                    'total_hours_display': total_hours_display,
                    'is_running': is_running,
                    'is_grace': is_grace,
                    'is_late': is_late,
                    'late_minutes': record.late_minutes,
                }
                present_employees_detailed.append(emp_item)
                all_employees_detailed.append(emp_item)

            elif emp.id in on_leave_emp_ids or (record and record.status == m.AttendanceRecord.Status.ON_LEAVE):
                emp_item = {
                    'emp': emp,
                    'record': record,
                    'status_category': 'on_leave',
                    'check_in': None,
                    'check_out': None,
                    'check_in_iso': '',
                    'total_hours_display': '--',
                    'is_running': False,
                    'is_grace': False,
                    'is_late': False,
                    'late_minutes': 0,
                }
                on_leave_employees.append(emp_item)
                all_employees_detailed.append(emp_item)

            else:
                emp_item = {
                    'emp': emp,
                    'record': record,
                    'status_category': 'absent',
                    'check_in': None,
                    'check_out': None,
                    'check_in_iso': '',
                    'total_hours_display': '--',
                    'is_running': False,
                    'is_grace': False,
                    'is_late': False,
                    'late_minutes': 0,
                }
                absent_employees.append(emp_item)
                all_employees_detailed.append(emp_item)

        # Total counts
        total_count = active_employees.count()
        present_count = len(present_employees_detailed)
        time_off_count = len(on_leave_employees)
        absent_count = len(absent_employees)
        attendance_pct = round((present_count / total_count) * 100) if total_count > 0 else 0

        # Sidebar data
        open_jobs_list = m.JobPosting.objects.filter(job_filter).annotate(
            app_count=Count('applications')
        ).order_by('-created_at')[:4]

        upcoming_birthdays = active_employees.filter(
            date_of_birth__month=target_date.month
        ).order_by('date_of_birth')[:4]

        context.update({
            'available_companies': available_companies,
            'active_company_id': active_company_id,
            'total_employees': total_count,
            'currently_working': currently_working_count,
            'on_break_count': 0,
            'time_off_count': time_off_count,
            'pending_biometrics': 0,
            'present_today': present_count,
            'absent_count': absent_count,
            'attendance_percentage': attendance_pct,
            'pending_leaves': m.LeaveApplication.objects.filter(leave_filter).count(),
            'open_jobs': m.JobPosting.objects.filter(job_filter).count(),

            # Tab data
            'all_employees_detailed': all_employees_detailed,
            'present_employees_detailed': present_employees_detailed,
            'absent_employees': absent_employees,
            'on_leave_employees': on_leave_employees,

            # Sidebar
            'upcoming_birthdays': upcoming_birthdays,
            'open_jobs_list': open_jobs_list,
        })
        template = 'hrms/dashboard_hr.html'

    else:
        # Standard Employee Logic
        employee = get_employee_profile(user)
        if employee:
            context.update({
                'employee': employee,
                'leave_balances': m.LeaveBalance.objects.filter(employee=employee, year=today.year),
                'recent_attendance': m.AttendanceRecord.objects.filter(employee=employee).order_by('-attendance_date')[:7],
                'recent_notices': m.CompanyNotice.objects.filter(company=employee.company, is_active=True).order_by('-notice_date')[:5],
            })
        template = 'hrms/dashboard_employee.html'

    return render(request, template, context)

@login_required
def coming_soon(request, active_group='dashboard', active_item='', title='Coming soon'):
    """Placeholder for modules not yet built out. Sidebar links all resolve
    so the navigation is fully clickable while each module gets built."""
    return render(request, 'hrms/coming_soon.html', {
        'active_group': active_group,
        'active_item': active_item,
        'module_title': title,
    })


# ===========================================================================
# ORGANISATION STRUCTURE + EMPLOYEE MANAGEMENT — Generic Class-Based Views
# ===========================================================================
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView, View

from . import forms as f
from .permissions import HRRequiredMixin, SuperAdminRequiredMixin, EmployeeSelfOrHRMixin, get_employee_profile


class SidebarContextMixin:
    """Injects the active_group/active_item so the sidebar highlights correctly."""
    active_group = 'dashboard'
    active_item = ''

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['active_group'] = self.active_group
        ctx['active_item'] = self.active_item
        return ctx


# ---------------------------------------------------------------------------
# Company (Org Settings) — Super Admin manages, HR can view
# ---------------------------------------------------------------------------
class CompanyListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Company
    template_name = 'hrms/org/company_list.html'
    context_object_name = 'companies'
    active_group, active_item = 'organisation', 'company'

    def get_queryset(self):
        # Prefetch policy history to avoid N+1 database queries
        qs = m.Company.objects.prefetch_related('policy_history').all()
        today = date.today()

        for company in qs:
            # We attach a dynamic property 'active_policy' to each company object
            company.active_policy = company.get_policy_for_date(today)
        return qs


class CompanyUpdateView(SuperAdminRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.Company
    form_class = f.CompanyForm
    template_name = 'hrms/org/company_form.html'
    success_url = reverse_lazy('hrms:company_list')
    active_group, active_item = 'organisation', 'company'

    def form_valid(self, form):
        messages.success(self.request, 'Company profile updated.')
        return super().form_valid(form)


class CompanyCreateView(SuperAdminRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Company
    form_class = f.CompanyForm
    template_name = 'hrms/org/company_form.html'
    success_url = reverse_lazy('hrms:company_list')
    active_group, active_item = 'organisation', 'company'

    def form_valid(self, form):
        messages.success(self.request, 'Company created.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Department — HR / Super Admin
# ---------------------------------------------------------------------------
class DepartmentListView(CompanyFilterMixin,HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Department
    template_name = 'hrms/org/department_list.html'
    context_object_name = 'companies'
    active_group, active_item = 'organisation', 'department'
    paginate_by = 50

    def get_queryset(self):
        # Prefetching everything down to employees in one go
        return m.Company.objects.prefetch_related(
            'departments__designations__employees'
        ).all()


class DepartmentCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Department
    form_class = f.DepartmentForm
    template_name = 'hrms/org/department_form.html'
    success_url = reverse_lazy('hrms:department_list')
    active_group, active_item = 'organisation', 'department'

    def form_valid(self, form):
        messages.success(self.request, 'Department created.')
        return super().form_valid(form)


class DepartmentUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.Department
    form_class = f.DepartmentForm
    template_name = 'hrms/org/department_form.html'
    success_url = reverse_lazy('hrms:department_list')
    active_group, active_item = 'organisation', 'department'

    def form_valid(self, form):
        messages.success(self.request, 'Department updated.')
        return super().form_valid(form)


class DepartmentDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.Department
    template_name = 'hrms/org/department_confirm_delete.html'
    success_url = reverse_lazy('hrms:department_list')
    active_group, active_item = 'organisation', 'department'

    def form_valid(self, form):
        messages.success(self.request, 'Department deleted.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Designation — HR / Super Admin
# ---------------------------------------------------------------------------
class DesignationListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Designation
    template_name = 'hrms/org/designation_list.html' # ensure this matches your path
    context_object_name = 'designations'
    active_group, active_item = 'organisation', 'designation'
    paginate_by = 30

    def get_queryset(self):
        # 1. Start with the base manager
        # 2. Use select_related for ForeignKeys (Company, Dept)
        # 3. Use prefetch_related for the Employee count logic
        # 4. CRITICAL: Put .order_by() HERE, before it leaves this function
        return m.Designation.objects.select_related(
            'company',
            'department'
        ).prefetch_related(
            'employees'
        ).order_by('company', 'department', 'level', 'title')

class DesignationCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Designation
    form_class = f.DesignationForm
    template_name = 'hrms/org/designation_form.html'
    success_url = reverse_lazy('hrms:designation_list')
    active_group, active_item = 'organisation', 'designation'

    def form_valid(self, form):
        messages.success(self.request, 'Designation created.')
        return super().form_valid(form)


class DesignationUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.Designation
    form_class = f.DesignationForm
    template_name = 'hrms/org/designation_form.html'
    success_url = reverse_lazy('hrms:designation_list')
    active_group, active_item = 'organisation', 'designation'

    def form_valid(self, form):
        messages.success(self.request, 'Designation updated.')
        return super().form_valid(form)


class DesignationDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.Designation
    template_name = 'hrms/org/designation_confirm_delete.html'
    success_url = reverse_lazy('hrms:designation_list')
    active_group, active_item = 'organisation', 'designation'

    def form_valid(self, form):
        messages.success(self.request, 'Designation deleted.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Employee — HR/Admin manage all; Employee can view only their own profile
# ---------------------------------------------------------------------------
from django.db.models import Q, Count


class EmployeeListView(CompanyFilterMixin, HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Employee
    template_name = 'hrms/employee/employee_list.html'
    context_object_name = 'employees'
    active_group, active_item = 'employee', 'employee_list'
    paginate_by = 65

    def get_queryset(self):
        # CompanyFilterMixin handles multi-tenancy / active_company_id filtering
        qs = super().get_queryset().select_related('company', 'department', 'designation')

        q = self.request.GET.get('q', '').strip()
        status = self.request.GET.get('status', '').strip()

        if q:
            qs = qs.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) |
                Q(employee_code__icontains=q) | Q(email__icontains=q)
            )
        if status:
            qs = qs.filter(status=status)

        return qs.order_by('employee_code')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Base company-filtered employee queryset for stats (ignoring search filters & pagination)
        base_emp_qs = super().get_queryset()
        user = self.request.user
        active_company_id = self.request.session.get('active_company_id')

        # Calculate Department count based on active company
        if active_company_id and active_company_id != 'all':
            dept_count = m.Department.objects.filter(company_id=active_company_id).count()
        elif not user.is_superuser:
            profile = get_employee_profile(user)
            if profile:
                first_managed = profile.managed_companies.first() if hasattr(profile, 'managed_companies') else None
                locked = first_managed or profile.company
                dept_count = m.Department.objects.filter(company=locked).count() if locked else 0
            else:
                dept_count = 0
        else:
            dept_count = m.Department.objects.count()

        # Stats Cards Data (Reflects selected company)
        ctx['total_count'] = base_emp_qs.count()
        ctx['active_count'] = base_emp_qs.filter(status=m.Employee.Status.ACTIVE).count()
        ctx['dept_count'] = dept_count
        ctx['exited_count'] = base_emp_qs.filter(status__in=[m.Employee.Status.RELIEVED, m.Employee.Status.SUSPENDED]).count()
        ctx['leave_count'] = base_emp_qs.filter(status=m.Employee.Status.ON_LEAVE).count()

        ctx['q'] = self.request.GET.get('q', '')
        ctx['status'] = self.request.GET.get('status', '')
        ctx['status_choices'] = m.Employee.Status.choices
        return ctx

class EmployeeDetailView(EmployeeSelfOrHRMixin, SidebarContextMixin, DetailView):
    model = m.Employee
    template_name = 'hrms/employee/employee_detail.html'
    context_object_name = 'employee'
    active_group, active_item = 'employee', 'employee_list'

    def get_queryset(self):
        # Prefetching everything in one go for speed
        return m.Employee.objects.select_related(
            'company', 'department', 'designation', 'bank_detail'
        ).prefetch_related(
            'documents', 'notices', 'assigned_leaves', 'live_balances', 'salaries'
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # 1. Get current year
        current_year = date.today().year

        # 2. Fetch actual balances from the LeaveBalance model (the source for your Ledger)
        balances = m.LeaveBalance.objects.filter(
            employee=self.object,
            year=current_year
        ).select_related('leave_type')

        # 3. Create a mapping for the template (e.g., {'CL': 7.5, 'SL': 0.0, ...})
        # This makes it easy to fetch values in the HTML
        leave_dict = {b.leave_type.code.upper(): b.available for b in balances}
        ctx['leave_map'] = leave_dict

        # Get latest active salary
        ctx['current_salary'] = self.object.salaries.filter(is_active=True).first()

        return ctx


class MyProfileView(LoginRequiredMixin, SidebarContextMixin, View):
    """Employee's own profile - resolves the Employee linked to the logged-in user
    and reuses the detail template (read-only for the employee)."""
    active_group, active_item = 'employee', 'my_profile'

    def get(self, request, *args, **kwargs):
        employee = get_employee_profile(request.user)
        if employee is None:
            messages.warning(request, "Your login isn't linked to an employee record yet. Contact HR.")
            return redirect('hrms:dashboard')
        return redirect('hrms:employee_detail', pk=employee.pk)

import re
from django.db.models import Max

class EmployeeCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Employee
    form_class = f.EmployeeForm
    template_name = 'hrms/employee/employee_form.html'
    active_group, active_item = 'employee', 'employee_add'

    def get_initial(self):
        initial = super().get_initial()

        # Look for existing codes in the standard format (OHCE + 4 digits)
        standard_codes = m.Employee.objects.filter(
            employee_code__regex=r'^OHCE\d{4}$'
        ).values_list('employee_code', flat=True)

        if standard_codes:
            # Extract numbers and find the next one
            numbers = [int(code[4:]) for code in standard_codes if code[4:].isdigit()]
            next_num = max(numbers) + 1 if numbers else 1
        else:
            next_num = 1

        # This will now correctly show OHCE0099 on the page
        initial['employee_code'] = f'OHCE{next_num:04d}'
        return initial

    def form_valid(self, form):
        messages.success(self.request, 'Employee added.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:employee_detail', kwargs={'pk': self.object.pk})


class EmployeeUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.Employee
    form_class = f.EmployeeForm
    template_name = 'hrms/employee/employee_form.html'
    active_group, active_item = 'employee', 'employee_list'

    def form_valid(self, form):
        messages.success(self.request, 'Employee updated.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:employee_detail', kwargs={'pk': self.object.pk})


class EmployeeDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.Employee
    template_name = 'hrms/employee/employee_confirm_delete.html'
    success_url = reverse_lazy('hrms:employee_list')
    active_group, active_item = 'employee', 'employee_list'

    def form_valid(self, form):
        messages.success(self.request, 'Employee removed.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Employee sub-records: Bank Detail (1:1), Documents, Notices
# ---------------------------------------------------------------------------
class EmployeeBankDetailUpdateView(HRRequiredMixin, SidebarContextMixin, View):
    """Create-or-update since it's a OneToOne — one form handles both."""
    active_group, active_item = 'employee', 'employee_list'
    template_name = 'hrms/employee/bankdetail_form.html'

    def _get_employee(self, pk):
        return get_object_or_404(m.Employee, pk=pk)

    def get(self, request, pk):
        employee = self._get_employee(pk)
        instance = getattr(employee, 'bank_detail', None)
        form = f.EmployeeBankDetailForm(instance=instance)
        return render(request, self.template_name, {
            'form': form, 'employee': employee,
            'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request, pk):
        employee = self._get_employee(pk)
        instance = getattr(employee, 'bank_detail', None)
        form = f.EmployeeBankDetailForm(request.POST, instance=instance)
        if form.is_valid():
            bank_detail = form.save(commit=False)
            bank_detail.employee = employee
            bank_detail.save()
            messages.success(request, 'Bank details saved.')
            return redirect('hrms:employee_detail', pk=employee.pk)
        return render(request, self.template_name, {
            'form': form, 'employee': employee,
            'active_group': self.active_group, 'active_item': self.active_item,
        })


class EmployeeDocumentCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.EmployeeDocument
    form_class = f.EmployeeDocumentForm
    template_name = 'hrms/employee/document_form.html'
    active_group, active_item = 'employee', 'employee_list'

    def dispatch(self, request, *args, **kwargs):
        self.employee = get_object_or_404(m.Employee, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['employee'] = self.employee
        return ctx

    def form_valid(self, form):
        form.instance.employee = self.employee
        messages.success(self.request, 'Document uploaded.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:employee_detail', kwargs={'pk': self.employee.pk})


class EmployeeDocumentDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.EmployeeDocument
    template_name = 'hrms/employee/document_confirm_delete.html'
    active_group, active_item = 'employee', 'employee_list'

    def get_success_url(self):
        messages.success(self.request, 'Document deleted.')
        return reverse('hrms:employee_detail', kwargs={'pk': self.object.employee.pk})


class MyDocumentsView(LoginRequiredMixin, SidebarContextMixin, ListView):
    """Employee's own documents, read-only."""
    model = m.EmployeeDocument
    template_name = 'hrms/employee/my_documents.html'
    context_object_name = 'documents'
    active_group, active_item = 'employee', 'my_documents'

    def get_queryset(self):
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.EmployeeDocument.objects.none()
        return employee.documents.all()


class EmployeeNoticeCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.EmployeeNotice
    form_class = f.EmployeeNoticeForm
    template_name = 'hrms/employee/notice_form.html'
    active_group, active_item = 'employee', 'employee_list'

    def dispatch(self, request, *args, **kwargs):
        self.employee = get_object_or_404(m.Employee, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['employee'] = self.employee
        return ctx

    def form_valid(self, form):
        form.instance.employee = self.employee
        messages.success(self.request, 'Notice added.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:employee_detail', kwargs={'pk': self.employee.pk})


class EmployeeNoticeDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.EmployeeNotice
    template_name = 'hrms/employee/notice_confirm_delete.html'
    active_group, active_item = 'employee', 'employee_list'

    def get_success_url(self):
        messages.success(self.request, 'Notice deleted.')
        return reverse('hrms:employee_detail', kwargs={'pk': self.object.employee.pk})


# ===========================================================================
# ATTENDANCE & GRACE POLICY
# ===========================================================================
from datetime import date as _date

from . import attendance_logic as attn


# ---------------------------------------------------------------------------
# Attendance Policy — one per company, HR/Admin manage (create-or-update pattern)
# ---------------------------------------------------------------------------
class AttendancePolicyListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Company
    template_name = 'hrms/attendance/policy_list.html'
    context_object_name = 'companies'
    active_group, active_item = 'attendance', 'attendance_policy'

    def get_queryset(self):
            # REMOVE .select_related('attendance_policy') from here
        return m.Company.objects.all()

    def get_queryset(self):
        qs = m.Company.objects.prefetch_related('policy_history').all()
        today = date.today()

        for company in qs:
            company.active_policy = company.get_policy_for_date(today)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # ---  THIS LINE to show all new n old policies  ---
        ctx['policy_versions'] = m.AttendancePolicy.objects.select_related('company').order_by('-effective_from')
        return ctx


class AttendancePolicyFormView(HRRequiredMixin, SidebarContextMixin, View):
    active_group, active_item = 'attendance', 'attendance_policy'
    template_name = 'hrms/attendance/policy_form.html'

    def get(self, request, pk):
        company = get_object_or_404(m.Company, pk=pk)
        instance = getattr(company, 'attendance_policy', None)
        form = f.AttendancePolicyForm(instance=instance)
        return render(request, self.template_name, {
            'form': form, 'company': company,
            'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request, pk):
        company = get_object_or_404(m.Company, pk=pk)
        instance = getattr(company, 'attendance_policy', None)
        form = f.AttendancePolicyForm(request.POST, instance=instance)
        if form.is_valid():
            policy = form.save(commit=False)
            policy.company = company
            policy.save()
            messages.success(request, f'Attendance policy saved for {company}.')
            return redirect('hrms:attendance_policy')
        return render(request, self.template_name, {
            'form': form, 'company': company,
            'active_group': self.active_group, 'active_item': self.active_item,
        })
class AttendancePolicyCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    """View to manually add a new timing version (e.g. for August)."""
    model = m.AttendancePolicy
    form_class = f.AttendancePolicyForm
    template_name = 'hrms/attendance/policy_form.html'
    success_url = reverse_lazy('hrms:attendance_policy')
    active_group, active_item = 'attendance', 'attendance_policy'

    def form_valid(self, form):
        messages.success(self.request, 'New timing version added successfully.')
        return super().form_valid(form)
class AttendancePolicyUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    """View to edit an existing historical timing version."""
    model = m.AttendancePolicy
    form_class = f.AttendancePolicyForm
    template_name = 'hrms/attendance/policy_form.html'
    success_url = reverse_lazy('hrms:attendance_policy')
    active_group, active_item = 'attendance', 'attendance_policy'

    def form_valid(self, form):
        messages.success(self.request, 'Timing version updated successfully.')
        return super().form_valid(form)

# ---------------------------------------------------------------------------
# Attendance Records — HR sees/manages all; employee sees + checks in/out on own
# ---------------------------------------------------------------------------


class AttendanceRecordListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.AttendanceRecord
    template_name = 'hrms/attendance/record_list.html'
    context_object_name = 'records'
    active_group, active_item = 'attendance', 'attendance_records'

    def post(self, request, *args, **kwargs):
        """Logic for manual Admin edits of punches."""
        if not request.user.is_superuser:
            return JsonResponse({'status': 'error', 'message': 'Permission denied'}, status=403)
        try:
            data = json.loads(request.body)
            updates = data.get('updates', [])
            tz = timezone.get_current_timezone()
            emp_ids = list(set([item.get('emp_id') for item in updates]))
            employee_cache = {str(e.id): e for e in m.Employee.objects.select_related('company').filter(id__in=emp_ids)}

            with transaction.atomic():
                for item in updates:
                    emp_id, date_str = str(item.get('emp_id')), item.get('date')
                    in_t, out_t = item.get('in_time'), item.get('out_time')
                    if not in_t or emp_id not in employee_cache: continue

                    emp = employee_cache[emp_id]
                    comp = emp.company
                    att_date = datetime.strptime(date_str, '%Y-%m-%d').date()

                    check_in_dt = timezone.make_aware(datetime.strptime(f"{date_str} {in_t}", '%Y-%m-%d %H:%M'), tz)
                    check_out_dt = None
                    if out_t:
                        check_out_dt = timezone.make_aware(datetime.strptime(f"{date_str} {out_t}", '%Y-%m-%d %H:%M'),
                                                           tz)

                    # Rules Logic
                    m.AttendanceRecord.objects.update_or_create(
                        employee_id=emp_id, attendance_date=att_date,
                        defaults={'check_in': check_in_dt, 'check_out': check_out_dt}
                    )
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    def get_context_data(self, **kwargs):
        """The final corrected Smart Brain for the report."""
        ctx = super().get_context_data(**kwargs)
        tz = timezone.get_current_timezone()

        # 1. Date Range Handling
        d_from, d_to = self.request.GET.get('from'), self.request.GET.get('to')
        start_date = datetime.strptime(d_from, '%Y-%m-%d').date() if d_from else timezone.now().date().replace(day=1)
        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            next_m = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_m - timedelta(days=next_m.day)
        date_list = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

        # 2. Fetch Data
        emp_filter = self.request.GET.get('employee')
        employees = m.Employee.objects.select_related('company', 'department').all().order_by('employee_code')
        if emp_filter: employees = employees.filter(id=emp_filter)

        # Order by date is CRITICAL for sequential G1, G2, G3
        records = m.AttendanceRecord.objects.filter(attendance_date__range=[start_date, end_date]).order_by(
            'attendance_date')
        leaves = m.LeaveApplication.objects.filter(status='approved', start_date__lte=end_date,
                                                   end_date__gte=start_date).select_related('leave_type')

        att_lookup = collections.defaultdict(dict)
        for r in records: att_lookup[r.employee_id][r.attendance_date] = r

        leave_lookup = collections.defaultdict(dict)
        for l in leaves:
            curr = max(l.start_date, start_date)
            while curr <= min(l.end_date, end_date):
                leave_lookup[l.employee_id][curr] = l.leave_type.code
                curr += timedelta(days=1)

        # 3. Build Matrix
        attendance_matrix = collections.defaultdict(dict)
        grace_counters = collections.defaultdict(int)

        for emp in employees:
            comp = emp.company
            # off_start, off_end = comp.office_start_time, comp.office_end_time
            grace_limit, grace_min = comp.grace_allowed_count, comp.grace_minutes

            for day in date_list:
                # --- SAHI LOGIC: Loop ke andar har din ki policy fetch karo ---
                active_rule = emp.company.get_policy_for_date(day)
                off_start = active_rule.office_start_time
                off_end = active_rule.office_end_time
                grace_mins = active_rule.grace_minutes
                grace_limit = active_rule.grace_allowed_count
                rec = att_lookup[emp.id].get(day)
                leave_code = leave_lookup[emp.id].get(day)
                display_obj = {'status': 'absent', 'display_label': '', 'check_in': None, 'check_out': None}

                if rec:
                    display_obj['check_in'] = rec.check_in
                    display_obj['check_out'] = rec.check_out
                    local_in = timezone.localtime(rec.check_in) if rec.check_in else None
                    local_out = timezone.localtime(rec.check_out) if rec.check_out else None

                    if local_in:
                        p_in = local_in.time()
                        p_out = local_out.time() if local_out else off_start

                        # Grace deadline (e.g., 10:15)
                        grace_deadline = (datetime.combine(day, off_start) + timedelta(minutes=grace_min)).time()
                        # Strictly checked: Did they stay until office end (6:00 PM)?
                        stayed_until_end = p_out >= off_end

                        # --- REFINED LOGIC GATEWAY ---

                        # Case 1: Arrived On Time (<= 10:00)
                        if p_in <= off_start:
                            # Must stay until 6:00 PM to get FD
                            display_obj['status'] = 'present' if stayed_until_end else 'half_day'

                        # Case 2: Within Grace Window (10:01 - 10:15)
                        elif p_in <= grace_deadline:
                            if stayed_until_end:
                                m_key = (emp.id, day.month)
                                if grace_counters[m_key] < grace_limit:
                                    grace_counters[m_key] += 1
                                    display_obj['status'] = 'present'
                                    display_obj['display_label'] = f"G{grace_counters[m_key]}"
                                else:
                                    display_obj['status'] = 'half_day'
                                    display_obj['display_label'] = "Grace Exhausted"
                            else:
                                # Within window but left early -> HD (Grace is not used/wasted)
                                display_obj['status'] = 'half_day'

                        # Case 3: Late Arrival (> 10:15)
                        else:
                            display_obj['status'] = 'half_day'

                elif leave_code:
                    display_obj['status'] = 'on_leave'
                    display_obj['display_label'] = leave_code

                attendance_matrix[emp.id][day] = display_obj

        ctx.update({'employees': employees, 'date_list': date_list, 'attendance_matrix': attendance_matrix,
                    'filters': self.request.GET, 'start_date': start_date, 'end_date': end_date})
        return ctx


from django.utils import timezone
from datetime import datetime, timedelta, time, date
import collections
import json
from django.http import JsonResponse
from django.db import transaction


class AttendanceRecordListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.AttendanceRecord
    template_name = 'hrms/attendance/record_list.html'
    context_object_name = 'records'
    active_group, active_item = 'attendance', 'attendance_records'

    def post(self, request, *args, **kwargs):
        """Logic for manual Admin edits of punches."""
        if not request.user.is_superuser:
            return JsonResponse({'status': 'error', 'message': 'Permission denied'}, status=403)
        try:
            data = json.loads(request.body)
            updates = data.get('updates', [])
            tz = timezone.get_current_timezone()

            with transaction.atomic():
                for item in updates:
                    emp_id, date_str = str(item.get('emp_id')), item.get('date')
                    in_t, out_t = item.get('in_time'), item.get('out_time')
                    if not in_t: continue

                    att_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    check_in_dt = timezone.make_aware(datetime.strptime(f"{date_str} {in_t}", '%Y-%m-%d %H:%M'), tz)
                    check_out_dt = None
                    if out_t:
                        check_out_dt = timezone.make_aware(datetime.strptime(f"{date_str} {out_t}", '%Y-%m-%d %H:%M'),
                                                           tz)

                    # We save the raw punches. Dynamic logic in get_context_data handles the display.
                    m.AttendanceRecord.objects.update_or_create(
                        employee_id=emp_id, attendance_date=att_date,
                        defaults={'check_in': check_in_dt, 'check_out': check_out_dt}
                    )
                    # After update_or_create for AttendanceRecord:
                    rec, created = m.AttendanceRecord.objects.update_or_create(
                        employee_id=emp_id, attendance_date=att_date,
                        defaults={'check_in': check_in_dt, 'check_out': check_out_dt}
                    )

            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

    def post(self, request, *args, **kwargs):
        """Logic for manual Admin edits of punches with automatic Leave Balance Reset."""
        if not request.user.is_superuser:
            return JsonResponse({'status': 'error', 'message': 'Permission denied'}, status=403)
        try:
            data = json.loads(request.body)
            updates = data.get('updates', [])
            tz = timezone.get_current_timezone()

            with transaction.atomic():
                for item in updates:
                    emp_id = str(item.get('emp_id'))
                    date_str = item.get('date')
                    in_t, out_t = item.get('in_time'), item.get('out_time')
                    if not in_t: continue

                    att_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    check_in_dt = timezone.make_aware(datetime.strptime(f"{date_str} {in_t}", '%Y-%m-%d %H:%M'), tz)

                    check_out_dt = None
                    if out_t:
                        check_out_dt = timezone.make_aware(datetime.strptime(f"{date_str} {out_t}", '%Y-%m-%d %H:%M'),
                                                           tz)

                    # 1. Save the Attendance Record
                    rec, created = m.AttendanceRecord.objects.update_or_create(
                        employee_id=emp_id, attendance_date=att_date,
                        defaults={'check_in': check_in_dt, 'check_out': check_out_dt}
                    )

                    # 2. DYNAMIC CALCULATION FOR REFUND
                    # We determine if this manual entry is FD or HD to know how much to refund
                    employee = rec.employee
                    rule = employee.company.get_policy_for_date(att_date)

                    p_in = timezone.localtime(check_in_dt).time()
                    p_out = timezone.localtime(check_out_dt).time() if check_out_dt else rule.office_start_time

                    # Determine Punch Status
                    is_fd = p_in <= rule.office_start_time and (p_out >= rule.office_end_time)
                    # If not FD but has check-in, it's at least a HD
                    punch_status = 'FD' if is_fd else 'HD'

                    # 3. TRIGGER LEAVE REFUND LOGIC
                    # Look for an approved leave application for this date
                    leave_app = m.LeaveApplication.objects.filter(
                        employee=employee,
                        status=m.LeaveApplication.Status.APPROVED,
                        start_date__lte=att_date,
                        end_date__gte=att_date
                    ).first()

                    if leave_app:
                        # Determine refund amount
                        # If Full Day punch -> refund full day leave (1.0 or 0.5)
                        # If Half Day punch -> refund only 0.5
                        refund_val = 0
                        if punch_status == 'FD':
                            refund_val = 0.5 if leave_app.day_type == 'half' else 1.0
                        else:  # HD Punch
                            refund_val = 0.5

                        # Update Leave Bank (EmployeeLeaveBalance table)
                        mapping = {
                            'EL': 'earned_leave', 'SL': 'sick_leave', 'CL': 'casual_leave',
                            'MTL': 'menstrual_leave', 'BL': 'bereavement_leave', 'CO': 'comp_off',
                        }
                        field_name = mapping.get(leave_app.leave_type.code.upper())

                        if field_name:
                            bank, _ = m.EmployeeLeaveBalance.objects.get_or_create(e_name=employee)
                            current_val = getattr(bank, field_name)
                            # We ADD back the refund_val to the balance
                            setattr(bank, field_name, float(current_val) + float(refund_val))
                            bank.save()

                            # Mark Attendance Record status as FD/HD instead of ON_LEAVE
                            rec.status = 'present' if punch_status == 'FD' else 'half_day'
                            rec.remarks = f"Refunded {refund_val} to {leave_app.leave_type.code}"
                            rec.save()

            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=400)
    def get_context_data(self, **kwargs):
        """The 'Smart Brain' that calculates status based on the Policy of the Day."""
        ctx = super().get_context_data(**kwargs)
        tz = timezone.get_current_timezone()

        # 1. Date Range Handling
        d_from, d_to = self.request.GET.get('from'), self.request.GET.get('to')
        start_date = datetime.strptime(d_from, '%Y-%m-%d').date() if d_from else timezone.now().date().replace(day=1)
        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            next_m = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_m - timedelta(days=next_m.day)
        date_list = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

        # 2. Fetch Employees and pre-fetch related data
        emp_filter = self.request.GET.get('employee')
        employees = m.Employee.objects.select_related('company', 'department').all().order_by('employee_code')
        if emp_filter: employees = employees.filter(id=emp_filter)

        records = m.AttendanceRecord.objects.filter(attendance_date__range=[start_date, end_date]).order_by(
            'attendance_date')
        leaves = m.LeaveApplication.objects.filter(status='approved', start_date__lte=end_date,
                                                   end_date__gte=start_date).select_related('leave_type')

        att_lookup = collections.defaultdict(dict)
        for r in records: att_lookup[r.employee_id][r.attendance_date] = r

        leave_lookup = collections.defaultdict(dict)
        for l in leaves:
            curr = max(l.start_date, start_date)
            while curr <= min(l.end_date, end_date):
                leave_lookup[l.employee_id][curr] = l.leave_type.code
                curr += timedelta(days=1)

        # 3. Build Smart Matrix
        attendance_matrix = collections.defaultdict(dict)

        for emp in employees:
            grace_counters = collections.defaultdict(int)  # Reset grace per employee per month

            for day in date_list:
                # --- DYNAMIC POLICY FETCHING ---
                # Checks what the rules were for THIS specific day (July 10-6 vs Aug 9-5)
                active_rule = emp.company.get_policy_for_date(day)
                off_start = active_rule.office_start_time
                off_end = active_rule.office_end_time
                grace_mins = active_rule.grace_minutes
                grace_limit = active_rule.grace_allowed_count

                rec = att_lookup[emp.id].get(day)
                leave_code = leave_lookup[emp.id].get(day)
                display_obj = {'status': 'absent', 'display_label': '', 'check_in': None, 'check_out': None}

                if rec:
                    display_obj['check_in'] = rec.check_in
                    display_obj['check_out'] = rec.check_out
                    l_in = timezone.localtime(rec.check_in) if rec.check_in else None
                    l_out = timezone.localtime(rec.check_out) if rec.check_out else None

                    if l_in:
                        p_in = l_in.time()
                        p_out = l_out.time() if l_out else off_start

                        grace_deadline = (datetime.combine(day, off_start) + timedelta(minutes=grace_mins)).time()
                        # Strict check: Must stay until office end (or past it)
                        stayed_until_end = p_out >= off_end

                        # LOGIC FLOW
                        if p_in <= off_start:
                            display_obj['status'] = 'present' if stayed_until_end else 'half_day'

                        elif p_in <= grace_deadline:
                            if stayed_until_end:
                                m_key = (emp.id, day.month)
                                if grace_counters[m_key] < grace_limit:
                                    grace_counters[m_key] += 1
                                    display_obj['status'], display_obj[
                                        'display_label'] = 'present', f"G{grace_counters[m_key]}"
                                else:
                                    display_obj['status'], display_obj['display_label'] = 'half_day', "Grace Exhausted"
                            else:
                                # Within grace window but left early -> HD
                                display_obj['status'] = 'half_day'

                        else:
                            # Late Arrival (> 15 mins late) -> HD
                            display_obj['status'] = 'half_day'

                elif leave_code:
                    display_obj['status'], display_obj['display_label'] = 'on_leave', leave_code

                attendance_matrix[emp.id][day] = display_obj

        ctx.update({'employees': employees, 'date_list': date_list, 'attendance_matrix': attendance_matrix,
                    'filters': self.request.GET, 'start_date': start_date, 'end_date': end_date})
        return ctx
    def get_context_data(self, **kwargs):
        """The 'Smart Brain' that calculates status based on the Policy of the Day."""
        ctx = super().get_context_data(**kwargs)
        tz = timezone.get_current_timezone()

        # 1. Date Range Handling
        d_from, d_to = self.request.GET.get('from'), self.request.GET.get('to')
        start_date = datetime.strptime(d_from, '%Y-%m-%d').date() if d_from else timezone.now().date().replace(day=1)
        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            next_m = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_m - timedelta(days=next_m.day)
        date_list = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

        # 2. Fetch Employees and pre-fetch related data
        emp_filter = self.request.GET.get('employee')
        employees = m.Employee.objects.select_related('company', 'department').all().order_by('employee_code')
        if emp_filter: employees = employees.filter(id=emp_filter)

        records = m.AttendanceRecord.objects.filter(attendance_date__range=[start_date, end_date]).order_by(
            'attendance_date')
        leaves = m.LeaveApplication.objects.filter(status='approved', start_date__lte=end_date,
                                                   end_date__gte=start_date).select_related('leave_type')

        att_lookup = collections.defaultdict(dict)
        for r in records: att_lookup[r.employee_id][r.attendance_date] = r

        leave_lookup = collections.defaultdict(dict)
        for l in leaves:
            curr = max(l.start_date, start_date)
            while curr <= min(l.end_date, end_date):
                leave_lookup[l.employee_id][curr] = l.leave_type.code
                curr += timedelta(days=1)

        # 3. Build Smart Matrix
        attendance_matrix = collections.defaultdict(dict)

        for emp in employees:
            grace_counters = collections.defaultdict(int)  # Reset grace per employee per month

            for day in date_list:
                # --- DYNAMIC POLICY FETCHING ---
                # Checks what the rules were for THIS specific day (July 10-6 vs Aug 9-5)
                active_rule = emp.company.get_policy_for_date(day)
                off_start = active_rule.office_start_time
                off_end = active_rule.office_end_time
                grace_mins = active_rule.grace_minutes
                grace_limit = active_rule.grace_allowed_count

                rec = att_lookup[emp.id].get(day)
                leave_code = leave_lookup[emp.id].get(day)
                display_obj = {'status': 'absent', 'display_label': '', 'check_in': None, 'check_out': None}

                if rec and rec.check_in:  # PRIORITIZE PUNCH RECORD
                    display_obj['check_in'] = rec.check_in
                    display_obj['check_out'] = rec.check_out
                    l_in = timezone.localtime(rec.check_in)
                    l_out = timezone.localtime(rec.check_out) if rec.check_out else None

                    p_in = l_in.time()
                    p_out = l_out.time() if l_out else off_start
                    grace_deadline = (datetime.combine(day, off_start) + timedelta(minutes=grace_mins)).time()
                    stayed_until_end = p_out >= off_end

                    # Calculate status based on punch
                    if p_in <= off_start:
                        display_obj['status'] = 'present' if stayed_until_end else 'half_day'
                    elif p_in <= grace_deadline:
                        if stayed_until_end:
                            m_key = (emp.id, day.month)
                            if grace_counters[m_key] < grace_limit:
                                grace_counters[m_key] += 1
                                display_obj['status'], display_obj[
                                    'display_label'] = 'present', f"G{grace_counters[m_key]}"
                            else:
                                display_obj['status'], display_obj['display_label'] = 'half_day', "Grace Exhausted"
                        else:
                            display_obj['status'] = 'half_day'
                    else:
                        display_obj['status'] = 'half_day'

                    # --- ADDED: If it's a Half Day punch, check if they have a leave for the other half ---
                    if display_obj['status'] == 'half_day' and leave_code:
                        display_obj['display_label'] = f"HD + {leave_code}"

                elif leave_code:  # ONLY SHOW LEAVE IF NO PUNCH EXISTS
                    display_obj['status'], display_obj['display_label'] = 'on_leave', leave_code

                attendance_matrix[emp.id][day] = display_obj

        ctx.update({'employees': employees, 'date_list': date_list, 'attendance_matrix': attendance_matrix,
                    'filters': self.request.GET, 'start_date': start_date, 'end_date': end_date})
        return ctx

    def get_context_data(self, **kwargs):
        """The 'Smart Brain' that calculates status based on the Policy of the Day."""
        ctx = super().get_context_data(**kwargs)
        tz = timezone.get_current_timezone()
        today = timezone.now().date()  # Get current date to identify future days

        # 1. Date Range Handling
        d_from, d_to = self.request.GET.get('from'), self.request.GET.get('to')
        start_date = datetime.strptime(d_from, '%Y-%m-%d').date() if d_from else timezone.now().date().replace(day=1)
        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            next_m = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_m - timedelta(days=next_m.day)
        date_list = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

        # 2. Fetch Employees and pre-fetch related data
        emp_filter = self.request.GET.get('employee')
        employees = m.Employee.objects.select_related('company', 'department').all().order_by('employee_code')
        if emp_filter: employees = employees.filter(id=emp_filter)

        records = m.AttendanceRecord.objects.filter(attendance_date__range=[start_date, end_date]).order_by(
            'attendance_date')
        leaves = m.LeaveApplication.objects.filter(status='approved', start_date__lte=end_date,
                                                   end_date__gte=start_date).select_related('leave_type')

        att_lookup = collections.defaultdict(dict)
        for r in records: att_lookup[r.employee_id][r.attendance_date] = r

        leave_lookup = collections.defaultdict(dict)
        for l in leaves:
            curr = max(l.start_date, start_date)
            while curr <= min(l.end_date, end_date):
                leave_lookup[l.employee_id][curr] = l.leave_type.code
                curr += timedelta(days=1)

        # 3. Build Smart Matrix
        attendance_matrix = collections.defaultdict(dict)

        for emp in employees:
            grace_counters = collections.defaultdict(int)

            for day in date_list:
                active_rule = emp.company.get_policy_for_date(day)
                off_start = active_rule.office_start_time
                off_end = active_rule.office_end_time
                grace_mins = active_rule.grace_minutes
                grace_limit = active_rule.grace_allowed_count

                rec = att_lookup[emp.id].get(day)
                leave_code = leave_lookup[emp.id].get(day)

                # Default status is now 'none' to allow specific checks
                display_obj = {'status': '', 'display_label': '', 'check_in': None, 'check_out': None}

                if rec and rec.check_in:
                    display_obj['check_in'] = rec.check_in
                    display_obj['check_out'] = rec.check_out
                    l_in = timezone.localtime(rec.check_in)
                    l_out = timezone.localtime(rec.check_out) if rec.check_out else None

                    p_in = l_in.time()
                    p_out = l_out.time() if l_out else off_start
                    grace_deadline = (datetime.combine(day, off_start) + timedelta(minutes=grace_mins)).time()
                    stayed_until_end = p_out >= off_end

                    if p_in <= off_start:
                        display_obj['status'] = 'present' if stayed_until_end else 'half_day'
                    elif p_in <= grace_deadline:
                        if stayed_until_end:
                            m_key = (emp.id, day.month)
                            if grace_counters[m_key] < grace_limit:
                                grace_counters[m_key] += 1
                                display_obj['status'], display_obj[
                                    'display_label'] = 'present', f"G{grace_counters[m_key]}"
                            else:
                                display_obj['status'], display_obj['display_label'] = 'half_day', "Grace Exhausted"
                        else:
                            display_obj['status'] = 'half_day'
                    else:
                        display_obj['status'] = 'half_day'

                    if display_obj['status'] == 'half_day' and leave_code:
                        display_obj['display_label'] = f"HD + {leave_code}"

                elif leave_code:
                    display_obj['status'], display_obj['display_label'] = 'on_leave', leave_code

                else:
                    # NEW LOGIC FOR EMPTY CELLS
                    if day > today:
                        display_obj['status'] = 'future'
                    elif day.weekday() == 6:  # 6 is Sunday
                        display_obj['status'] = 'sunday'
                    else:
                        display_obj['status'] = 'absent'

                attendance_matrix[emp.id][day] = display_obj

        ctx.update({'employees': employees, 'date_list': date_list, 'attendance_matrix': attendance_matrix,
                    'filters': self.request.GET, 'start_date': start_date, 'end_date': end_date})
        return ctx

    def get_context_data(self, **kwargs):
        """The 'Smart Brain' that calculates status based on the Policy of the Day."""
        ctx = super().get_context_data(**kwargs)
        tz = timezone.get_current_timezone()
        today = timezone.now().date()

        # 1. Date Range Handling
        d_from, d_to = self.request.GET.get('from'), self.request.GET.get('to')
        start_date = datetime.strptime(d_from, '%Y-%m-%d').date() if d_from else timezone.now().date().replace(day=1)
        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            next_m = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_m - timedelta(days=next_m.day)
        date_list = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

        # 2. Fetch ONLY ACTIVE Employees and pre-fetch related data
        emp_filter = self.request.GET.get('employee')

        # UPDATED LINE: Added filter for status='active'
        employees = m.Employee.objects.select_related('company', 'department').filter(
            status=m.Employee.Status.ACTIVE
        ).order_by('employee_code')

        if emp_filter:
            employees = employees.filter(id=emp_filter)

        # 3. Fetch Records and Leaves for the date range
        records = m.AttendanceRecord.objects.filter(attendance_date__range=[start_date, end_date]).order_by(
            'attendance_date')
        leaves = m.LeaveApplication.objects.filter(
            status=m.LeaveApplication.Status.APPROVED,
            start_date__lte=end_date,
            end_date__gte=start_date
        ).select_related('leave_type')

        # Optimization: Lookups
        att_lookup = collections.defaultdict(dict)
        for r in records:
            att_lookup[r.employee_id][r.attendance_date] = r

        leave_lookup = collections.defaultdict(dict)
        for l in leaves:
            curr = max(l.start_date, start_date)
            while curr <= min(l.end_date, end_date):
                leave_lookup[l.employee_id][curr] = l.leave_type.code
                curr += timedelta(days=1)

        # 4. Build Smart Matrix
        attendance_matrix = collections.defaultdict(dict)

        for emp in employees:
            grace_counters = collections.defaultdict(int)

            for day in date_list:
                active_rule = emp.company.get_policy_for_date(day)
                off_start = active_rule.office_start_time
                off_end = active_rule.office_end_time
                grace_mins = active_rule.grace_minutes
                grace_limit = active_rule.grace_allowed_count

                rec = att_lookup[emp.id].get(day)
                leave_code = leave_lookup[emp.id].get(day)

                display_obj = {'status': '', 'display_label': '', 'check_in': None, 'check_out': None}

                if rec and rec.check_in:
                    # ... [Existing Punch Logic Remains the Same] ...
                    display_obj['check_in'] = rec.check_in
                    display_obj['check_out'] = rec.check_out
                    l_in = timezone.localtime(rec.check_in)
                    l_out = timezone.localtime(rec.check_out) if rec.check_out else None

                    p_in = l_in.time()
                    p_out = l_out.time() if l_out else off_start
                    grace_deadline = (datetime.combine(day, off_start) + timedelta(minutes=grace_mins)).time()
                    stayed_until_end = p_out >= off_end

                    if p_in <= off_start:
                        display_obj['status'] = 'present' if stayed_until_end else 'half_day'
                    elif p_in <= grace_deadline:
                        if stayed_until_end:
                            m_key = (emp.id, day.month)
                            if grace_counters[m_key] < grace_limit:
                                grace_counters[m_key] += 1
                                display_obj['status'], display_obj[
                                    'display_label'] = 'present', f"G{grace_counters[m_key]}"
                            else:
                                display_obj['status'], display_obj['display_label'] = 'half_day', "Grace Exhausted"
                        else:
                            display_obj['status'] = 'half_day'
                    else:
                        display_obj['status'] = 'half_day'

                    if display_obj['status'] == 'half_day' and leave_code:
                        display_obj['display_label'] = f"HD + {leave_code}"

                elif leave_code:
                    display_obj['status'], display_obj['display_label'] = 'on_leave', leave_code

                else:
                    if day > today:
                        display_obj['status'] = 'future'
                    elif day.weekday() == 6:
                        display_obj['status'] = 'sunday'
                    else:
                        display_obj['status'] = 'absent'

                attendance_matrix[emp.id][day] = display_obj

        ctx.update({
            'employees': employees,
            'date_list': date_list,
            'attendance_matrix': attendance_matrix,
            'filters': self.request.GET,
            'start_date': start_date,
            'end_date': end_date
        })
        return ctx

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tz = timezone.get_current_timezone()
        today = timezone.now().date()

        # 1. Date Range Handling
        d_from, d_to = self.request.GET.get('from'), self.request.GET.get('to')
        start_date = datetime.strptime(d_from, '%Y-%m-%d').date() if d_from else timezone.now().date().replace(day=1)
        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            next_m = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_m - timedelta(days=next_m.day)
        date_list = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

        # 2. Fetch ACTIVE Employees (Prefetch holiday_calendar for speed)
        emp_filter = self.request.GET.get('employee')
        employees = m.Employee.objects.select_related('company', 'department', 'holiday_calendar').filter(
            status=m.Employee.Status.ACTIVE
        ).order_by('employee_code')
        if emp_filter: employees = employees.filter(id=emp_filter)

        # 3. PRE-FETCH DATA FOR OPTIMIZATION
        records = m.AttendanceRecord.objects.filter(attendance_date__range=[start_date, end_date])
        leaves = m.LeaveApplication.objects.filter(
            status=m.LeaveApplication.Status.APPROVED,
            start_date__lte=end_date, end_date__gte=start_date
        ).select_related('leave_type')

        # NEW: Fetch Holidays across ALL calendars for the range
        holidays = m.Holiday.objects.filter(date__range=[start_date, end_date])

        # Create Lookups
        att_lookup = collections.defaultdict(dict)
        for r in records: att_lookup[r.employee_id][r.attendance_date] = r

        leave_lookup = collections.defaultdict(dict)
        for l in leaves:
            curr = max(l.start_date, start_date);
            while curr <= min(l.end_date, end_date):
                leave_lookup[l.employee_id][curr] = l.leave_type.code
                curr += timedelta(days=1)

        # NEW: Holiday Lookup {Calendar_ID: {Date: Holiday_Name}}
        holiday_lookup = collections.defaultdict(dict)
        for h in holidays:
            holiday_lookup[h.calendar_id][h.date] = h.name

        # 4. Build Smart Matrix
        attendance_matrix = collections.defaultdict(dict)

        for emp in employees:
            grace_counters = collections.defaultdict(int)

            for day in date_list:
                active_rule = emp.company.get_policy_for_date(day)
                off_start, off_end = active_rule.office_start_time, active_rule.office_end_time
                grace_mins, grace_limit = active_rule.grace_minutes, active_rule.grace_allowed_count

                rec = att_lookup[emp.id].get(day)
                leave_code = leave_lookup[emp.id].get(day)

                # Check for holiday based on Employee's SPECIFIC calendar
                holiday_name = None
                if emp.holiday_calendar_id:
                    holiday_name = holiday_lookup[emp.holiday_calendar_id].get(day)

                display_obj = {'status': '', 'display_label': '', 'check_in': None, 'check_out': None}

                if rec and rec.check_in:
                    # --- PUNCH LOGIC ---
                    display_obj['check_in'], display_obj['check_out'] = rec.check_in, rec.check_out
                    l_in = timezone.localtime(rec.check_in)
                    l_out = timezone.localtime(rec.check_out) if rec.check_out else None
                    p_in = l_in.time()
                    p_out = l_out.time() if l_out else off_start
                    grace_deadline = (datetime.combine(day, off_start) + timedelta(minutes=grace_mins)).time()
                    stayed_until_end = p_out >= off_end

                    if p_in <= off_start:
                        display_obj['status'] = 'present' if stayed_until_end else 'half_day'
                    elif p_in <= grace_deadline:
                        if stayed_until_end:
                            m_key = (emp.id, day.month)
                            if grace_counters[m_key] < grace_limit:
                                grace_counters[m_key] += 1
                                display_obj['status'], display_obj[
                                    'display_label'] = 'present', f"G{grace_counters[m_key]}"
                            else:
                                display_obj['status'], display_obj['display_label'] = 'half_day', "Grace Exhausted"
                        else:
                            display_obj['status'] = 'half_day'
                    else:
                        display_obj['status'] = 'half_day'

                    if display_obj['status'] == 'half_day' and leave_code:
                        display_obj['display_label'] = f"HD + {leave_code}"

                elif leave_code:
                    display_obj['status'], display_obj['display_label'] = 'on_leave', leave_code

                elif holiday_name:
                    # NEW: Mark as holiday based on their regional template
                    display_obj['status'], display_obj['display_label'] = 'holiday', holiday_name

                else:
                    # DEFAULT FALLBACKS
                    if day > today:
                        display_obj['status'] = 'future'
                    elif day.weekday() == 6:  # Sunday
                        display_obj['status'] = 'sunday'
                    else:
                        display_obj['status'] = 'absent'

                attendance_matrix[emp.id][day] = display_obj

        ctx.update({
            'employees': employees, 'date_list': date_list,
            'attendance_matrix': attendance_matrix, 'filters': self.request.GET,
            'start_date': start_date, 'end_date': end_date
        })
        return ctx
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tz = timezone.get_current_timezone()
        today = timezone.now().date()

        # 1. Date Range Handling
        d_from, d_to = self.request.GET.get('from'), self.request.GET.get('to')
        start_date = datetime.strptime(d_from, '%Y-%m-%d').date() if d_from else timezone.now().date().replace(day=1)
        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            next_m = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_m - timedelta(days=next_m.day)
        date_list = [start_date + timedelta(days=x) for x in range((end_date - start_date).days + 1)]

        # 2. Fetch ACTIVE Employees (Prefetch holiday_calendar for speed)
        emp_filter = self.request.GET.get('employee')
        employees = m.Employee.objects.select_related('company', 'department', 'holiday_calendar').filter(
            status=m.Employee.Status.ACTIVE
        ).order_by('employee_code')
        if emp_filter: employees = employees.filter(id=emp_filter)

        # 3. PRE-FETCH DATA FOR OPTIMIZATION
        records = m.AttendanceRecord.objects.filter(attendance_date__range=[start_date, end_date])
        leaves = m.LeaveApplication.objects.filter(
            status=m.LeaveApplication.Status.APPROVED,
            start_date__lte=end_date, end_date__gte=start_date
        ).select_related('leave_type')

        # NEW: Fetch Holidays across ALL calendars for the range
        holidays = m.Holiday.objects.filter(date__range=[start_date, end_date])
        holiday_lookup = collections.defaultdict(dict)
        for h in holidays:
            holiday_lookup[h.calendar_id][h.date] = h.name

        # Create Lookups
        att_lookup = collections.defaultdict(dict)
        for r in records: att_lookup[r.employee_id][r.attendance_date] = r

        leave_lookup = collections.defaultdict(dict)
        for l in leaves:
            curr = max(l.start_date, start_date);
            while curr <= min(l.end_date, end_date):
                leave_lookup[l.employee_id][curr] = l.leave_type.code
                curr += timedelta(days=1)

        # NEW: Holiday Lookup {Calendar_ID: {Date: Holiday_Name}}
        holiday_lookup = collections.defaultdict(dict)
        for h in holidays:
            holiday_lookup[h.calendar_id][h.date] = h.name

        # 4. Build Smart Matrix
        attendance_matrix = collections.defaultdict(dict)

        for emp in employees:
            grace_counters = collections.defaultdict(int)

            for day in date_list:
                active_rule = emp.company.get_policy_for_date(day)
                off_start, off_end = active_rule.office_start_time, active_rule.office_end_time
                grace_mins, grace_limit = active_rule.grace_minutes, active_rule.grace_allowed_count

                rec = att_lookup[emp.id].get(day)
                leave_code = leave_lookup[emp.id].get(day)

                # Check for holiday based on Employee's SPECIFIC calendar
                holiday_name = None
                if emp.holiday_calendar_id:
                    holiday_name = holiday_lookup[emp.holiday_calendar_id].get(day)

                display_obj = {'status': '', 'display_label': '', 'check_in': None, 'check_out': None}

                if rec and rec.check_in:
                    # --- PUNCH LOGIC ---
                    display_obj['check_in'], display_obj['check_out'] = rec.check_in, rec.check_out
                    l_in = timezone.localtime(rec.check_in)
                    l_out = timezone.localtime(rec.check_out) if rec.check_out else None
                    p_in = l_in.time()
                    p_out = l_out.time() if l_out else off_start
                    grace_deadline = (datetime.combine(day, off_start) + timedelta(minutes=grace_mins)).time()
                    stayed_until_end = p_out >= off_end

                    if p_in <= off_start:
                        display_obj['status'] = 'present' if stayed_until_end else 'half_day'
                    elif p_in <= grace_deadline:
                        if stayed_until_end:
                            m_key = (emp.id, day.month)
                            if grace_counters[m_key] < grace_limit:
                                grace_counters[m_key] += 1
                                display_obj['status'], display_obj[
                                    'display_label'] = 'present', f"G{grace_counters[m_key]}"
                            else:
                                display_obj['status'], display_obj['display_label'] = 'half_day', "Grace Exhausted"
                        else:
                            display_obj['status'] = 'half_day'
                    else:
                        display_obj['status'] = 'half_day'

                    if display_obj['status'] == 'half_day' and leave_code:
                        display_obj['display_label'] = f"HD + {leave_code}"

                elif leave_code:
                    display_obj['status'], display_obj['display_label'] = 'on_leave', leave_code

                elif holiday_name:
                    # NEW: Mark as holiday based on their regional template
                    display_obj['status'], display_obj['display_label'] = 'holiday', holiday_name

                else:
                    # DEFAULT FALLBACKS
                    if day > today:
                        display_obj['status'] = 'future'
                    elif day.weekday() == 6:  # Sunday
                        display_obj['status'] = 'sunday'
                    else:
                        display_obj['status'] = 'absent'

                attendance_matrix[emp.id][day] = display_obj

        ctx.update({
            'employees': employees, 'date_list': date_list,
            'attendance_matrix': attendance_matrix, 'filters': self.request.GET,
            'start_date': start_date, 'end_date': end_date
        })
        return ctx

import json
import collections
from datetime import datetime, time, timedelta
from django.http import JsonResponse
from django.utils import timezone
from django.views.generic import ListView
# Assuming your models are imported as m
from hrms import models as m




from django.shortcuts import render
from django.views.generic import ListView
from datetime import timedelta, date
from . import models as m



class AttendanceRecordCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.AttendanceRecord
    form_class = f.AttendanceRecordForm
    template_name = 'hrms/attendance/record_form.html'
    success_url = reverse_lazy('hrms:attendance_records')
    active_group, active_item = 'attendance', 'attendance_records'

    def form_valid(self, form):
        record = form.save(commit=False)
        # If a check-in time was given and no check-out yet, apply the same
        # 3-strike grace rules HR would get from a real self-service check-in
        # (e.g. importing a biometric punch). HR's explicit status choice is
        # only overridden when it was left at the default (Present).
        if record.check_in and not record.check_out:
            policy = attn.get_policy_for_employee(record.employee)
            if policy and record.status == m.AttendanceRecord.Status.PRESENT:
                attn.evaluate_arrival(record, policy)
        record.save()
        self.object = record
        messages.success(self.request, 'Attendance record added.')
        return redirect(self.get_success_url())


class AttendanceRecordUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.AttendanceRecord
    form_class = f.AttendanceRecordForm
    template_name = 'hrms/attendance/record_form.html'
    success_url = reverse_lazy('hrms:attendance_records')
    active_group, active_item = 'attendance', 'attendance_records'

    def form_valid(self, form):
        messages.success(self.request, 'Attendance record updated.')
        return super().form_valid(form)


class MyAttendanceView(LoginRequiredMixin, SidebarContextMixin, ListView):
    """Employee's own attendance history + today's check-in/out card."""
    model = m.AttendanceRecord
    template_name = 'hrms/attendance/my_attendance.html'
    context_object_name = 'records'
    active_group, active_item = 'attendance', 'my_attendance'
    paginate_by = 31

    def get_employee(self):
        return get_employee_profile(self.request.user)

    def get_queryset(self):
        employee = self.get_employee()
        if employee is None:
            return m.AttendanceRecord.objects.none()
        return employee.attendance_records.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        employee = self.get_employee()
        ctx['employee'] = employee
        today = timezone.localdate()
        ctx['today_record'] = (
            m.AttendanceRecord.objects.filter(employee=employee, attendance_date=today).first()
            if employee else None
        )
        if employee:
            tracker = m.GraceUsageTracker.objects.filter(
                employee=employee, month=today.month, year=today.year).first()
            policy = attn.get_policy_for_employee(employee)
            ctx['grace_used'] = tracker.usage_count if tracker else 0
            ctx['grace_max'] = policy.max_grace_per_month if policy else None
        return ctx


class CheckInView(LoginRequiredMixin, View):
    def post(self, request):
        employee = get_employee_profile(request.user)
        if employee is None:
            messages.error(request, "Your login isn't linked to an employee record.")
            return redirect('hrms:dashboard')
        try:
            record = attn.check_in(employee)
            if record.late_minutes:
                messages.warning(request, f'Checked in — {record.late_minutes} min late. {record.remarks}')
            else:
                messages.success(request, 'Checked in successfully.')
        except attn.AttendanceError as e:
            messages.error(request, str(e))
        return redirect('hrms:my_attendance')


class CheckOutView(LoginRequiredMixin, View):
    def post(self, request):
        employee = get_employee_profile(request.user)
        if employee is None:
            messages.error(request, "Your login isn't linked to an employee record.")
            return redirect('hrms:dashboard')
        try:
            record = attn.check_out(employee)
            messages.success(request, f'Checked out — {record.total_hours} hrs logged today.')
        except attn.AttendanceError as e:
            messages.error(request, str(e))
        return redirect('hrms:my_attendance')


# ---------------------------------------------------------------------------
# Holidays — everyone can view, HR/Admin manage
# ---------------------------------------------------------------------------
from django.shortcuts import redirect, get_object_or_404
from django.contrib import messages

class HolidayListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.Holiday
    template_name = 'hrms/attendance/holiday_list.html'
    context_object_name = 'holidays'
    active_group, active_item = 'attendance', 'holiday'



    def get_queryset(self):
        qs = m.Holiday.objects.select_related('calendar__company').order_by('date')
        template_id = self.request.GET.get('template')
        query = self.request.GET.get('q')

        if template_id:
            qs = qs.filter(calendar_id=template_id)
        if query:
            qs = qs.filter(name__icontains=query)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Pass all calendars so the dropdown filter works
        if self.request.user.is_superuser:
            ctx['calendars'] = m.HolidayCalendar.objects.all()
        else:
            managed_ids = self.request.user.employee_profile.managed_companies.values_list('id', flat=True)
            ctx['calendars'] = m.HolidayCalendar.objects.filter(company_id__in=managed_ids)

        return ctx


class HolidayCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Holiday
    form_class = f.BulkHolidayForm  # Use the Bulk Form
    template_name = 'hrms/attendance/holiday_form.html'
    success_url = reverse_lazy('hrms:holiday_calendar_manage')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user  # Pass user to form
        return kwargs

    def form_valid(self, form):
        # 1. Get the list of calendars selected in the checkboxes
        calendars = form.cleaned_data['target_calendars']

        # 2. Get holiday details
        name = form.cleaned_data['name']
        h_date = form.cleaned_data['date']
        h_type = form.cleaned_data['type']
        desc = form.cleaned_data.get('description', '')

        # 3. Create a separate database entry for each selected calendar
        for cal in calendars:
            m.Holiday.objects.update_or_create(
                calendar=cal,
                date=h_date,
                defaults={'name': name, 'type': h_type, 'description': desc}
            )

        messages.success(self.request, f'Holiday "{name}" assigned to {calendars.count()} templates.')
        return redirect(self.success_url)

class HolidayUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.Holiday
    form_class = f.HolidayForm
    template_name = 'hrms/attendance/holiday_form.html'
    success_url = reverse_lazy('hrms:holiday_list')
    active_group, active_item = 'attendance', 'holiday'

    def form_valid(self, form):
        messages.success(self.request, 'Holiday updated.')
        return super().form_valid(form)

class HolidayDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.Holiday
    template_name = 'hrms/attendance/holiday_confirm_delete.html'
    success_url = reverse_lazy('hrms:holiday_list')
    active_group, active_item = 'attendance', 'holiday'

    def form_valid(self, form):
        messages.success(self.request, 'Holiday deleted.')
        return super().form_valid(form)

class HolidayCalendarManageView(HRRequiredMixin, SidebarContextMixin, TemplateView):
    template_name = 'hrms/attendance/holiday_calendar_manage.html'
    active_group, active_item = 'attendance', 'holiday'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # Show calendars for ALL companies the user manages
        if self.request.user.is_superuser:
            ctx['calendars'] = m.HolidayCalendar.objects.all().order_by('company__name', 'name')
        else:
            managed_ids = self.request.user.employee_profile.managed_companies.values_list('id', flat=True)
            ctx['calendars'] = m.HolidayCalendar.objects.filter(company_id__in=managed_ids).order_by('name')

        edit_id = self.request.GET.get('edit')
        if edit_id:
            instance = get_object_or_404(m.HolidayCalendar, pk=edit_id)
            # Pass user to form
            ctx['form'] = f.HolidayCalendarForm(instance=instance, user=self.request.user)
            ctx['edit_instance'] = instance
        else:
            # Pass user to form
            ctx['form'] = f.HolidayCalendarForm(user=self.request.user)

        return ctx

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
        pk = request.POST.get('pk')

        if action == 'delete' and pk:
            instance = get_object_or_404(m.HolidayCalendar, pk=pk)
            instance.delete()
            messages.success(request, "Template deleted.")

        elif action == 'save':
            if pk:
                instance = get_object_or_404(m.HolidayCalendar, pk=pk)
                form = f.HolidayCalendarForm(request.POST, instance=instance, user=request.user)
            else:
                form = f.HolidayCalendarForm(request.POST, user=request.user)

            if form.is_valid():
                form.save()  # Company is now handled by the form selection
                messages.success(request, "Template saved successfully.")
            else:
                messages.error(request, "Please correct the errors below.")
                return self.render_to_response(self.get_context_data(form=form))

        return redirect('hrms:holiday_calendar_manage')


from django.db import transaction

from django.db import transaction


class HolidayCalendarManageView(HRRequiredMixin, SidebarContextMixin, TemplateView):
    template_name = 'hrms/attendance/holiday_calendar_manage.html'
    active_group, active_item = 'attendance', 'holiday'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # 1. Existing Logic: Show calendars for managed companies
        if self.request.user.is_superuser:
            ctx['calendars'] = m.HolidayCalendar.objects.all().order_by('company__name', 'name')
        else:
            managed_ids = self.request.user.employee_profile.managed_companies.values_list('id', flat=True)
            ctx['calendars'] = m.HolidayCalendar.objects.filter(company_id__in=managed_ids).order_by('name')

        edit_id = self.request.GET.get('edit')
        if edit_id:
            instance = get_object_or_404(m.HolidayCalendar, pk=edit_id)
            ctx['form'] = f.HolidayCalendarForm(instance=instance, user=self.request.user)
            ctx['edit_instance'] = instance

            # 2. NEW: Get employees belonging to this template's company
            ctx['company_employees'] = m.Employee.objects.filter(
                company=instance.company
            ).select_related('user').order_by('first_name')

            # IDs of employees currently linked to this template
            ctx['assigned_ids'] = list(instance.employees.values_list('id', flat=True))
        else:
            ctx['form'] = f.HolidayCalendarForm(user=self.request.user)

        return ctx

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
        pk = request.POST.get('pk')

        if action == 'delete' and pk:
            instance = get_object_or_404(m.HolidayCalendar, pk=pk)
            instance.delete()
            messages.success(request, "Template deleted.")

        elif action == 'save':
            if pk:
                instance = get_object_or_404(m.HolidayCalendar, pk=pk)
                form = f.HolidayCalendarForm(request.POST, instance=instance, user=request.user)
            else:
                form = f.HolidayCalendarForm(request.POST, user=request.user)

            if form.is_valid():
                with transaction.atomic():
                    instance = form.save()

                    # 3. NEW: Handle Bulk Employee Assignment logic
                    if pk:
                        selected_emp_ids = request.POST.getlist('assigned_employees')

                        # Remove this template from employees who were unchecked
                        m.Employee.objects.filter(holiday_calendar=instance).exclude(id__in=selected_emp_ids).update(
                            holiday_calendar=None)

                        # Assign this template to the newly checked employees
                        m.Employee.objects.filter(id__in=selected_emp_ids).update(holiday_calendar=instance)

                messages.success(request, "Template and assignments updated successfully.")
            else:
                messages.error(request, "Please correct the errors below.")
                return self.render_to_response(self.get_context_data(form=form))

        return redirect('hrms:holiday_calendar_manage')
# ===========================================================================
# LEAVE MANAGEMENT
# ===========================================================================
from . import leave_logic as lv


# ---------------------------------------------------------------------------
# Leave Types — HR/Admin manage
# ---------------------------------------------------------------------------
class LeaveTypeListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.LeaveType
    template_name = 'hrms/leave/leavetype_list.html'
    context_object_name = 'leave_types'
    active_group, active_item = 'leave', 'leavetype'

    def get_queryset(self):
        qs = m.LeaveType.objects.select_related('company')
        company_id = self.request.GET.get('company')
        if company_id:
            qs = qs.filter(company_id=company_id)
        return qs.order_by('company__name', 'name')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['companies'] = m.Company.objects.all()
        ctx['selected_company'] = self.request.GET.get('company')
        return ctx

class LeaveTypeCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.LeaveType
    form_class = f.LeaveTypeForm
    template_name = 'hrms/leave/leavetype_form.html'
    success_url = reverse_lazy('hrms:leavetype_list')
    active_group, active_item = 'leave', 'leavetype'

    def form_valid(self, form):
        messages.success(self.request, 'Leave type created.')
        return super().form_valid(form)


class LeaveTypeUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.LeaveType
    form_class = f.LeaveTypeForm
    template_name = 'hrms/leave/leavetype_form.html'
    success_url = reverse_lazy('hrms:leavetype_list')
    active_group, active_item = 'leave', 'leavetype'

    def form_valid(self, form):
        messages.success(self.request, 'Leave type updated.')
        return super().form_valid(form)


class LeaveTypeDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.LeaveType
    template_name = 'hrms/leave/leavetype_confirm_delete.html'
    success_url = reverse_lazy('hrms:leavetype_list')
    active_group, active_item = 'leave', 'leavetype'

    def form_valid(self, form):
        messages.success(self.request, 'Leave type deleted.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Leave Balance — HR allocates/corrects; everyone can view (own vs all)
# ---------------------------------------------------------------------------
class LeaveBalanceListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.LeaveBalance
    template_name = 'hrms/leave/leave_balance_list.html'
    context_object_name = 'balances'
    active_group, active_item = 'leave', 'leave_balance'

    def get_queryset(self):
        qs = m.LeaveBalance.objects.select_related('employee', 'leave_type')
        if is_hr_or_above(self.request.user):
            employee_id = self.request.GET.get('employee')
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
            return qs.order_by('employee__employee_code', 'leave_type__name')
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.LeaveBalance.objects.none()
        return qs.filter(employee=employee).order_by('leave_type__name')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user_is_hr = is_hr_or_above(self.request.user)
        ctx['hrms_is_hr'] = user_is_hr

        # 1. Fetch all balances (Filtered by HR selection if needed)
        qs = self.get_queryset()

        # 2. Pivot the data: { EmployeeObject: { 'CL': 5, 'SL': 2, ... } }
        employee_map = {}
        for bal in qs:
            emp = bal.employee
            if emp not in employee_map:
                employee_map[emp] = {
                    'details': emp,
                    'year': bal.year,
                    'types': {}
                }
            # Store the available balance for each leave type code
            employee_map[emp]['types'][bal.leave_type.code.upper()] = bal.available

        ctx['pivoted_balances'] = employee_map.values()

        if user_is_hr:
            ctx['employees'] = m.Employee.objects.all()
        return ctx

# ---employee leave detail page
class EmployeeLeaveHistoryView(LoginRequiredMixin, SidebarContextMixin, DetailView):
    model = m.Employee
    template_name = 'hrms/leave/employee_leave_history.html'
    context_object_name = 'employee'
    active_group, active_item = 'leave', 'leave_balance'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Fetch all leave applications for this specific employee
        ctx['leave_history'] = m.LeaveApplication.objects.filter(
            employee=self.get_object()
        ).select_related('leave_type').order_by('-start_date')

        # Calculate summary stats for the header
        ctx['total_leaves'] = ctx['leave_history'].filter(status='approved').count()
        return ctx


class LeaveBalanceCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.LeaveBalance
    form_class = f.LeaveBalanceForm
    template_name = 'hrms/leave/leave_balance_form.html'
    success_url = reverse_lazy('hrms:leave_balance')
    active_group, active_item = 'leave', 'leave_balance'

    def form_valid(self, form):
        messages.success(self.request, 'Leave balance allocated.')
        return super().form_valid(form)


class LeaveBalanceUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    # model = m.LeaveBalance
    model = m.EmployeeLeaveBalance
    form_class = f.LeaveBalanceForm
    template_name = 'hrms/leave/leave_balance_form.html'
    success_url = reverse_lazy('hrms:leave_balance')
    active_group, active_item = 'leave', 'leave_balance'
    def get_object(self, queryset=None):
        # Instead of using the PK of the LeaveBalance, we look it up using the 'pk' from the URL (which will be the Employee ID)
        emp_id = self.kwargs.get('pk')
        # This assumes one LeaveBalance record per employee for the current year,Adjust 'year' logic if you track multiple years
        # return get_object_or_404(m.LeaveBalance, employee_id=emp_id)
        obj, created = m.EmployeeLeaveBalance.objects.get_or_create(e_name_id=emp_id)
        return obj

    def form_valid(self, form):
        messages.success(self.request, f'Leave balance for {self.object.employee} updated.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Leave Applications — employee applies for their own; HR sees all + approves
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Leave Applications — employee applies for their own; HR sees all + approves
# ---------------------------------------------------------------------------
class LeaveApplicationListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.LeaveApplication
    template_name = 'hrms/leave/leave_application_list.html'
    context_object_name = 'applications'
    active_group, active_item = 'leave', 'my_leave'
    paginate_by = 15

    def get_queryset(self):
        qs = m.LeaveApplication.objects.select_related(
            'employee', 'employee__designation', 'employee__department', 'leave_type', 'approved_by'
        ).order_by('-applied_on')

        user = self.request.user
        emp = get_employee_profile(user)

        # 1. Role-based visibility
        if not is_hr_or_above(user):
            if emp is None:
                return m.LeaveApplication.objects.none()
            if emp.is_manager:
                # Manager can see their own leaves AND subordinate leaves
                qs = qs.filter(Q(employee=emp) | Q(employee__reporting_manager=emp))
            else:
                qs = qs.filter(employee=emp)

        # 2. Extract Filter Parameters
        status = self.request.GET.get('status')
        employee_id = self.request.GET.get('employee')
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        sales_filter = self.request.GET.get('sales')
        department_id = self.request.GET.get('department')

        # 3. Apply Filters
        if status:
            qs = qs.filter(status=status)
        if employee_id and (is_hr_or_above(user) or (emp and emp.is_manager)):
            qs = qs.filter(employee_id=employee_id)
        if start_date:
            qs = qs.filter(start_date__gte=start_date)
        if end_date:
            qs = qs.filter(end_date__lte=end_date)
        if sales_filter == '1':
            qs = qs.filter(employee__department__name__icontains='sales')
        elif department_id:
            qs = qs.filter(employee__department_id=department_id)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        user_is_hr = is_hr_or_above(user)
        emp = get_employee_profile(user)
        ctx['is_hr'] = user_is_hr
        ctx['is_manager'] = (emp and emp.is_manager) if emp else False
        ctx['current_employee'] = emp

        # Base QS for full stats
        base_qs = self.get_queryset()

        ctx['stats'] = {
            'total': base_qs.count(),
            'pending_manager': base_qs.filter(status=m.LeaveApplication.Status.PENDING_MANAGER).count(),
            'pending_hr': base_qs.filter(status__in=[m.LeaveApplication.Status.PENDING_HR, m.LeaveApplication.Status.PENDING]).count(),
            'approved': base_qs.filter(status=m.LeaveApplication.Status.APPROVED).count(),
            'rejected': base_qs.filter(status=m.LeaveApplication.Status.REJECTED).count(),
            'sales': base_qs.filter(employee__department__name__icontains='sales').count(),
        }

        # Calculate live remaining balances formatted as CL (Total/Remaining) e.g. CL (10/3)
        field_map = {
            'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
            'ML': 'menstrual_leave', 'MTL': 'menstrual_leave',
            'BL': 'bereavement_leave', 'CO': 'comp_off'
        }

        # Cache balance instances for current page employees
        page_employees = {app.employee_id: app.employee for app in ctx['applications']}
        emp_banks = {b.e_name_id: b for b in m.EmployeeLeaveBalance.objects.filter(e_name_id__in=page_employees.keys())}
        emp_lives = {l.e_name_id: l for l in m.EmployeeLeaveBalanceLive.objects.filter(e_name_id__in=page_employees.keys())}

        for app in ctx['applications']:
            code = (app.leave_type.code or '').upper()
            field_name = field_map.get(code)
            bank = emp_banks.get(app.employee_id)
            live = emp_lives.get(app.employee_id)

            if field_name and bank and live:
                tot = getattr(bank, field_name, 0.0)
                rem = getattr(live, field_name, 0.0)
                tot_str = str(int(tot)) if float(tot).is_integer() else f"{tot:.1f}"
                rem_str = str(int(rem)) if float(rem).is_integer() else f"{rem:.1f}"
                app.balance_formatted = f"{code} ({tot_str}/{rem_str})"
            else:
                app.balance_formatted = app.leave_type.name

        if user_is_hr or (emp and emp.is_manager):
            if user_is_hr:
                ctx['employees'] = m.Employee.objects.filter(status='active').order_by('first_name')
            else:
                ctx['employees'] = m.Employee.objects.filter(reporting_manager=emp, status='active').order_by('first_name')
            ctx['status_choices'] = m.LeaveApplication.Status.choices
            ctx['departments'] = m.Department.objects.all()
            ctx['filters'] = self.request.GET
            ctx['reject_form'] = f.LeaveRejectForm()

        return ctx


class LeaveApplicationCreateView(LoginRequiredMixin, SidebarContextMixin, View):
    """Employees apply for their own leave; HR/Admin can apply on behalf of anyone.
    Balance-sufficiency is enforced by leave_logic.apply_leave() — this view
    just wires the form to it and surfaces any LeaveError as a form error."""
    active_group, active_item = 'leave', 'my_leave'
    template_name = 'hrms/leave/leave_application_form.html'

    def get_form_class(self):
        return f.LeaveApplicationHRForm if is_hr_or_above(self.request.user) else f.LeaveApplicationForm

    def get(self, request):
        form = self.get_form_class()()
        return render(request, self.template_name, {
            'form': form, 'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request):
        form_class = self.get_form_class()
        form = form_class(request.POST)
        if form.is_valid():
            employee = form.cleaned_data.get('employee') if is_hr_or_above(request.user) else get_employee_profile(
                request.user)

            if employee is None:
                messages.error(request, "Your login isn't linked to an employee record.")
                return redirect('hrms:dashboard')

            try:
                lv.apply_leave(
                    employee=employee,
                    leave_type=form.cleaned_data['leave_type'],
                    day_type=form.cleaned_data['day_type'],
                    start_date=form.cleaned_data['start_date'],
                    end_date=form.cleaned_data['end_date'],
                    reason=form.cleaned_data.get('reason', ''),
                )
                messages.success(request, 'Leave application submitted successfully.')
                return redirect('hrms:my_leave')
            except lv.LeaveError as e:
                form.add_error(None, str(e))
        return render(request, self.template_name, {
            'form': form, 'active_group': self.active_group, 'active_item': self.active_item,
        })


class LeaveApproveView(HRRequiredMixin, View):
    """HR/SuperAdmin approves leave -> updates status to approved and deducts leave balance."""
    def post(self, request, pk):
        application = get_object_or_404(m.LeaveApplication, pk=pk)
        try:
            lv.approve_leave(application, approver_user=request.user)
            messages.success(request, f'Leave approved for {application.employee.full_name}.')
        except lv.LeaveError as e:
            messages.error(request, str(e))
        return redirect(request.META.get('HTTP_REFERER', 'hrms:my_leave'))


class LeaveRejectView(HRRequiredMixin, View):
    """HR/SuperAdmin rejects leave -> updates status to rejected with rejection reason."""
    def post(self, request, pk):
        application = get_object_or_404(m.LeaveApplication, pk=pk)
        reason = request.POST.get('rejection_reason', '').strip()
        try:
            lv.reject_leave(application, approver_user=request.user, reason=reason)
            messages.success(request, f'Leave rejected for {application.employee.full_name}.')
        except lv.LeaveError as e:
            messages.error(request, str(e))
        return redirect(request.META.get('HTTP_REFERER', 'hrms:my_leave'))


class ManagerDashboardView(LoginRequiredMixin, SidebarContextMixin, TemplateView):
    """Dashboard specifically for reporting managers and HR to view team leaves & attendance."""
    template_name = 'hrms/manager/manager_dashboard.html'
    active_group, active_item = 'manager', 'manager_dashboard'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        emp = get_employee_profile(user)
        user_is_hr = is_hr_or_above(user)

        # Get subordinates
        if user_is_hr:
            subordinates = m.Employee.objects.filter(status='active').select_related('department', 'designation', 'company')
        elif emp and emp.is_manager:
            subordinates = m.Employee.objects.filter(reporting_manager=emp, status='active').select_related('department', 'designation', 'company')
        else:
            subordinates = m.Employee.objects.none()

        ctx['subordinates'] = subordinates
        ctx['subordinate_count'] = subordinates.count()

        # Subordinates' pending leaves
        if user_is_hr:
            pending_leaves = m.LeaveApplication.objects.filter(
                status__in=[m.LeaveApplication.Status.PENDING_MANAGER, m.LeaveApplication.Status.PENDING_HR, m.LeaveApplication.Status.PENDING]
            ).select_related('employee', 'leave_type')
        elif emp:
            pending_leaves = m.LeaveApplication.objects.filter(
                employee__reporting_manager=emp,
                status=m.LeaveApplication.Status.PENDING_MANAGER
            ).select_related('employee', 'leave_type')
        else:
            pending_leaves = m.LeaveApplication.objects.none()

        ctx['pending_leaves'] = pending_leaves

        # Today's team attendance
        today = timezone.localdate()
        today_attendance = m.AttendanceRecord.objects.filter(
            employee__in=subordinates,
            attendance_date=today
        ).select_related('employee')
        today_att_map = {att.employee_id: att for att in today_attendance}

        # Build attendance status for each subordinate today
        team_today_list = []
        for sub in subordinates:
            att = today_att_map.get(sub.id)
            team_today_list.append({
                'employee': sub,
                'record': att,
                'status': att.get_status_display() if att else 'Not Checked In',
                'check_in': att.check_in if att else None,
                'check_out': att.check_out if att else None,
                'late_minutes': att.late_minutes if att else 0,
                'is_half_day': att.is_half_day if att else False,
            })
        ctx['team_today_list'] = team_today_list

        # Team monthly summary
        month_start = today.replace(day=1)
        month_records = m.AttendanceRecord.objects.filter(
            employee__in=subordinates,
            attendance_date__gte=month_start,
            attendance_date__lte=today
        )
        ctx['team_stats'] = {
            'present': month_records.filter(status=m.AttendanceRecord.Status.PRESENT).count(),
            'half_day': month_records.filter(status=m.AttendanceRecord.Status.HALF_DAY).count(),
            'on_leave': month_records.filter(status=m.AttendanceRecord.Status.ON_LEAVE).count(),
            'absent': month_records.filter(status=m.AttendanceRecord.Status.ABSENT).count(),
        }

        ctx['reject_form'] = f.LeaveRejectForm()
        return ctx


class ManagerLeaveApproveView(LoginRequiredMixin, View):
    """Manager approves subordinate leave -> forwards to HR (status: PENDING_HR)."""
    def post(self, request, pk):
        application = get_object_or_404(m.LeaveApplication, pk=pk)
        emp = get_employee_profile(request.user)
        is_hr = is_hr_or_above(request.user)

        # Check authorization
        if not is_hr and (not emp or application.employee.reporting_manager != emp):
            messages.error(request, "You are not authorized to approve this leave request.")
            return redirect('hrms:my_leave')

        try:
            lv.manager_approve_leave(application, approver_user=request.user)
            messages.success(request, f'Leave for {application.employee.full_name} approved and escalated to HR.')
        except lv.LeaveError as e:
            messages.error(request, str(e))
        return redirect(request.META.get('HTTP_REFERER', 'hrms:my_leave'))


class ManagerLeaveRejectView(LoginRequiredMixin, View):
    """Manager rejects subordinate leave with reason."""
    def post(self, request, pk):
        application = get_object_or_404(m.LeaveApplication, pk=pk)
        emp = get_employee_profile(request.user)
        is_hr = is_hr_or_above(request.user)

        if not is_hr and (not emp or application.employee.reporting_manager != emp):
            messages.error(request, "You are not authorized to reject this leave request.")
            return redirect('hrms:my_leave')

        reason = request.POST.get('rejection_reason', '').strip()
        try:
            lv.reject_leave(application, approver_user=request.user, reason=reason)
            messages.success(request, f'Leave for {application.employee.full_name} rejected.')
        except lv.LeaveError as e:
            messages.error(request, str(e))
        return redirect(request.META.get('HTTP_REFERER', 'hrms:my_leave'))


class PenaltyListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    """Penalties page for tracking late arrival penalties and automatic leave deductions."""
    model = m.AttendancePenalty
    template_name = 'hrms/attendance/penalty_list.html'
    context_object_name = 'penalties'
    active_group, active_item = 'attendance', 'penalties'
    paginate_by = 20

    def get_queryset(self):
        qs = m.AttendancePenalty.objects.select_related(
            'employee', 'employee__department', 'employee__designation', 'attendance_record'
        ).order_by('-penalty_date', '-created_at')

        user = self.request.user
        emp = get_employee_profile(user)
        if not is_hr_or_above(user):
            if emp is None:
                return m.AttendancePenalty.objects.none()
            if emp.is_manager:
                qs = qs.filter(Q(employee=emp) | Q(employee__reporting_manager=emp))
            else:
                qs = qs.filter(employee=emp)

        employee_id = self.request.GET.get('employee')
        start_date = self.request.GET.get('start_date')
        end_date = self.request.GET.get('end_date')
        department_id = self.request.GET.get('department')

        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if start_date:
            qs = qs.filter(penalty_date__gte=start_date)
        if end_date:
            qs = qs.filter(penalty_date__lte=end_date)
        if department_id:
            qs = qs.filter(employee__department_id=department_id)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        user_is_hr = is_hr_or_above(user)
        emp = get_employee_profile(user)
        ctx['is_hr'] = user_is_hr

        base_qs = self.get_queryset()
        ctx['stats'] = {
            'total_penalties': base_qs.count(),
            'total_days_deducted': sum(p.deduction_days for p in base_qs),
            'total_late_minutes': sum(p.late_minutes for p in base_qs),
        }

        if user_is_hr:
            ctx['employees'] = m.Employee.objects.filter(status='active').order_by('first_name')
        elif emp and emp.is_manager:
            ctx['employees'] = m.Employee.objects.filter(reporting_manager=emp, status='active').order_by('first_name')
        ctx['departments'] = m.Department.objects.all()
        ctx['filters'] = self.request.GET
        return ctx


class LeaveCancelView(LoginRequiredMixin, View):
    """The employee themself, or HR on their behalf, can cancel a pending/approved application."""
    def post(self, request, pk):
        application = get_object_or_404(m.LeaveApplication, pk=pk)
        is_hr = is_hr_or_above(request.user)
        employee = get_employee_profile(request.user)
        try:
            lv.cancel_leave(application, requested_by_employee=employee, is_hr=is_hr)
            messages.success(request, 'Leave application cancelled.')
        except lv.LeaveError as e:
            messages.error(request, str(e))
        return redirect('hrms:my_leave')


from django.views.generic import ListView
from django.shortcuts import redirect
from django.contrib.auth.mixins import LoginRequiredMixin
from django.forms import modelformset_factory
from .models import EmployeeLeaveBalance
from .forms import LeaveBalanceForm


class LeaveBankListView(LoginRequiredMixin, ListView):
    model = EmployeeLeaveBalance
    template_name = 'hrms/leave/leave_bank.html'
    context_object_name = 'leave_balances'

    def get_queryset(self):
        user = self.request.user
        active_statuses = [
            Employee.Status.ACTIVE,
            Employee.Status.ON_LEAVE,
            Employee.Status.SUSPENDED
        ]

        # 2. Start with a base queryset filtering out inactive employees
        qs = EmployeeLeaveBalance.objects.filter(
            e_name__status__in=active_statuses
        ).select_related('e_name', 'e_name__department', 'e_name__designation')

        # 1. Superadmin and HR see everyone
        if user.is_superuser or user.groups.filter(name='HR').exists():
            return EmployeeLeaveBalance.objects.all().select_related('e_name')

        # 2. Normal User sees only their own record
        # Note: We filter by the User linked to the Employee
        return EmployeeLeaveBalance.objects.filter(e_name__user=user).select_related('e_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # 3. If Superadmin, provide a Formset for bulk editing
        if self.request.user.is_superuser:
            LeaveFormSet = modelformset_factory(
                EmployeeLeaveBalance, form=LeaveBalanceForm, extra=0
            )
            # Link the formset to the current queryset so it only shows filtered rows
            context['formset'] = LeaveFormSet(queryset=self.get_queryset())

        return context

    def post(self, request, *args, **kwargs):
        # 4. Handle bulk save for Superadmin
        if not request.user.is_superuser:
            return redirect('hrms:leave-bank')

        LeaveFormSet = modelformset_factory(EmployeeLeaveBalance, form=LeaveBalanceForm, extra=0)
        formset = LeaveFormSet(request.POST)

        if formset.is_valid():
            formset.save()
            return redirect('hrms:leave-bank')

        # If invalid, re-render with errors
        return self.render_to_response(self.get_context_data(formset=formset))
    def post(self, request, *args, **kwargs):
        # 1. SECURITY: Only Superadmin can write to this page
        if not request.user.is_superuser:
            from django.contrib import messages
            messages.error(request, "Access Denied.")
            return redirect('hrms:leave-bank')

        # 2. KEY FIX: Check for 'action' which matches your HTML button name
        action = request.POST.get('action')

        if action == 'calculate_all':
            employees = Employee.objects.filter(
                status__in=[Employee.Status.ACTIVE, Employee.Status.ON_LEAVE]
            )
            for emp in employees:
                lv.sync_employee_leave_bank(emp)
            return redirect('hrms:leave-bank')

        # 3. Handle Manual Formset Save (If Unlocked)
        LeaveFormSet = modelformset_factory(m.EmployeeLeaveBalance, form=LeaveBalanceForm, extra=0)
        formset = LeaveFormSet(request.POST)
        if formset.is_valid():
            formset.save()
            return redirect('hrms:leave-bank')

        return self.render_to_response(self.get_context_data(formset=formset))


from django.forms import modelformset_factory
from .models import EmployeeLeaveBalance, Employee


# ... other imports

from django.forms import modelformset_factory
from . import forms as f  # Import your forms file
from . import models as m
from . import leave_logic as lv


class LeaveBankListView(LoginRequiredMixin, ListView):
    model = m.EmployeeLeaveBalance
    template_name = 'hrms/leave/leave_bank.html'
    context_object_name = 'leave_balances'

    def get_queryset(self):
        user = self.request.user
        qs = m.EmployeeLeaveBalance.objects.exclude(e_name__status='relieved').select_related('e_name', 'e_name__department', 'e_name__designation')
        if not (user.is_superuser or user.groups.filter(name='HR').exists()):
            qs = qs.filter(e_name__user=user)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        qs = self.get_queryset()
        context['active_records'] = qs.filter(e_name__status__in=['active', 'on_leave'])
        context['inactive_records'] = qs.exclude(e_name__status__in=['active', 'on_leave'])

        if self.request.user.is_superuser:
            # We use the specific Form class from your forms.py
            LeaveFormSet = modelformset_factory(m.EmployeeLeaveBalance, form=f.LeaveBalanceForm, extra=0)
            context['formset'] = LeaveFormSet(queryset=qs)
        return context

    def post(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            return redirect('hrms:leave-bank')

        action = request.POST.get('action')

        # 1. AUTO CALCULATE
        if action == 'calculate_all':
            employees = m.Employee.objects.filter(status='active')
            for emp in employees:
                lv.sync_employee_leave_bank(emp)
            return redirect('hrms:leave-bank')

        # 2. MANUAL SAVE (Triggered by value="save_manual")
        LeaveFormSet = modelformset_factory(m.EmployeeLeaveBalance, form=f.LeaveBalanceForm, extra=0)
        formset = LeaveFormSet(request.POST)

        if formset.is_valid():
            formset.save()
            return redirect('hrms:leave-bank')

        # If invalid, re-render with errors
        return self.render_to_response(self.get_context_data(formset=formset))

# PAYROLL & SALARY STRUCTURE ENGINE
# ===========================================================================
import calendar as _calendar

from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.http import HttpResponse

from . import payroll_logic as pay


# ---------------------------------------------------------------------------
# Salary Structure + Components — HR/Admin manage
# ---------------------------------------------------------------------------
class SalaryStructureListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.SalaryStructure
    template_name = 'hrms/payroll/salarystructure_list.html'
    context_object_name = 'structures'
    active_group, active_item = 'payroll', 'salarystructure'

    def get_queryset(self):
        return m.SalaryStructure.objects.select_related('company').order_by('company__name', 'name')


class SalaryStructureCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.SalaryStructure
    form_class = f.SalaryStructureForm
    template_name = 'hrms/payroll/salarystructure_form.html'
    active_group, active_item = 'payroll', 'salarystructure'

    def form_valid(self, form):
        messages.success(self.request, 'Salary structure created.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:salarystructure_detail', kwargs={'pk': self.object.pk})


class SalaryStructureUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.SalaryStructure
    form_class = f.SalaryStructureForm
    template_name = 'hrms/payroll/salarystructure_form.html'
    active_group, active_item = 'payroll', 'salarystructure'

    def form_valid(self, form):
        messages.success(self.request, 'Salary structure updated.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:salarystructure_detail', kwargs={'pk': self.object.pk})


class SalaryStructureDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.SalaryStructure
    template_name = 'hrms/payroll/salarystructure_confirm_delete.html'
    success_url = reverse_lazy('hrms:salarystructure_list')
    active_group, active_item = 'payroll', 'salarystructure'

    def form_valid(self, form):
        messages.success(self.request, 'Salary structure deleted.')
        return super().form_valid(form)


class SalaryStructureDetailView(HRRequiredMixin, SidebarContextMixin, DetailView):
    model = m.SalaryStructure
    template_name = 'hrms/payroll/salarystructure_detail.html'
    context_object_name = 'structure'
    active_group, active_item = 'payroll', 'salarystructure'


class SalaryComponentCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.SalaryComponent
    form_class = f.SalaryComponentForm
    template_name = 'hrms/payroll/salarycomponent_form.html'
    active_group, active_item = 'payroll', 'salarystructure'

    def dispatch(self, request, *args, **kwargs):
        self.structure = get_object_or_404(m.SalaryStructure, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['structure'] = self.structure
        return ctx

    def form_valid(self, form):
        form.instance.structure = self.structure
        messages.success(self.request, 'Component added.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:salarystructure_detail', kwargs={'pk': self.structure.pk})


class SalaryComponentDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.SalaryComponent
    template_name = 'hrms/payroll/salarycomponent_confirm_delete.html'
    active_group, active_item = 'payroll', 'salarystructure'

    def get_success_url(self):
        messages.success(self.request, 'Component removed.')
        return reverse('hrms:salarystructure_detail', kwargs={'pk': self.object.structure.pk})


# ---------------------------------------------------------------------------
# Employee Salary — HR/Admin manage; drives payroll processing
# ---------------------------------------------------------------------------
from django.db.models import Sum, Avg


class EmployeeSalaryListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.EmployeeSalary
    template_name = 'hrms/payroll/employee_salary_list.html'
    context_object_name = 'salaries'
    active_group, active_item = 'payroll', 'employee_salary'

    def get_queryset(self):
        qs = m.EmployeeSalary.objects.select_related('employee', 'employee__designation').order_by('-is_active',
                                                                                                   'employee__first_name')
        emp_id = self.request.GET.get('employee')
        if emp_id:
            qs = qs.filter(employee_id=emp_id)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        active_qs = m.EmployeeSalary.objects.filter(is_active=True)

        # Financial Stats
        ctx['total_ctc'] = active_qs.aggregate(Sum('ctc_annual'))['ctc_annual__sum'] or 0
        ctx['avg_ctc'] = active_qs.aggregate(Avg('ctc_annual'))['ctc_annual__avg'] or 0
        ctx['active_count'] = active_qs.count()

        ctx['employees'] = m.Employee.objects.all()
        ctx['selected_employee'] = self.request.GET.get('employee')
        return ctx

class EmployeeSalaryCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.EmployeeSalary
    form_class = f.EmployeeSalaryForm
    template_name = 'hrms/payroll/employee_salary_form.html'
    success_url = reverse_lazy('hrms:employee_salary_list')
    active_group, active_item = 'payroll', 'employee_salary'

    def form_valid(self, form):
        messages.success(self.request, 'Salary structure assigned to employee.')
        return super().form_valid(form)


class EmployeeSalaryUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.EmployeeSalary
    form_class = f.EmployeeSalaryForm
    template_name = 'hrms/payroll/employee_salary_form.html'
    success_url = reverse_lazy('hrms:employee_salary_list')
    active_group, active_item = 'payroll', 'employee_salary'

    def form_valid(self, form):
        messages.success(self.request, 'Employee salary updated.')
        return super().form_valid(form)



from django.utils import timezone
from datetime import datetime, timedelta, date
import calendar


class EmployeePunchReportView(HRRequiredMixin, SidebarContextMixin, DetailView):
    model = m.Employee
    template_name = 'hrms/attendance/punch_report.html'
    context_object_name = 'target_employee'
    pk_url_kwarg = 'emp_id'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        emp = self.object
        month = int(self.kwargs['month'])
        year = int(self.kwargs['year'])
        tz = timezone.get_current_timezone()

        # Company Rules
        comp = emp.company
        off_start, off_end = comp.office_start_time, comp.office_end_time
        grace_deadline_mins = comp.grace_minutes
        grace_limit = comp.grace_allowed_count

        days_in_month = calendar.monthrange(year, month)[1]
        report_data = []
        grace_used = 0

        # Prefetch data for the whole month to avoid DB hits in loop
        records = {r.attendance_date: r for r in m.AttendanceRecord.objects.filter(
            employee=emp, attendance_date__year=year, attendance_date__month=month).order_by('attendance_date')}

        # Build a lookup for leaves covering this month
        leaves = m.LeaveApplication.objects.filter(
            employee=emp, status='approved',
            start_date__lte=date(year, month, days_in_month),
            end_date__gte=date(year, month, 1)
        ).select_related('leave_type')

        leave_map = {}
        for l in leaves:
            curr = l.start_date
            while curr <= l.end_date:
                if curr.month == month and curr.year == year:
                    leave_map[curr] = l.leave_type.code.upper()
                curr += timedelta(days=1)
        # Note: If you don't use the signal, use LeaveApplication.objects.filter(...) logic here

        for d in range(1, days_in_month + 1):
            dt = date(year, month, d)
            rec = records.get(dt)
            leave_code = leave_map.get(dt)
            is_holiday = m.Holiday.objects.filter(company=comp, date=dt).exists()
            is_sunday = dt.weekday() == 6

            day_info = {
                'date': dt, 'in': None, 'out': None, 'hours': 0,
                'status': 'ABS', 'label': '', 'css': 'mark-abs'
            }

            if is_holiday or is_sunday:
                day_info['status'] = 'HOL' if is_holiday else 'SUN'
                day_info['css'] = 'text-muted'
                if rec and rec.check_in:
                    day_info['label'] = 'Extra Work'
            # elif leave_code:
            #     # MARK AS LEAVE (PAID)
            #     day_info.update({'status': leave_code, 'css': 'bg-primary text-white', 'label': 'Approved Leave'})
            #

            elif rec:
                if rec.status == 'on_leave':
                    day_info['status'] = 'LEAVE'
                    day_info['label'] = rec.remarks  # e.g., SL, CL
                    day_info['css'] = 'bg-primary text-white'

                elif rec.check_in:
                    local_in = timezone.localtime(rec.check_in)
                    local_out = timezone.localtime(rec.check_out) if rec.check_out else None
                    day_info['in'] = local_in
                    day_info['out'] = local_out

                    p_in = local_in.time()
                    p_out = local_out.time() if local_out else off_start

                    # Effective Hours Math (10-6 rule)
                    eff_s = max(p_in, off_start)
                    eff_e = min(p_out, off_end)
                    eff_hours = (datetime.combine(dt, eff_e) - datetime.combine(dt, eff_s)).total_seconds() / 3600
                    day_info['hours'] = round(eff_hours, 2)

                    grace_time = (datetime.combine(dt, off_start) + timedelta(minutes=grace_deadline_mins)).time()

                    # SMART GATEWAY LOGIC
                    if p_in <= off_start:
                        if eff_hours >= 8:
                            day_info.update({'status': 'FD', 'css': 'mark-fd'})
                        else:
                            day_info.update({'status': 'HD', 'css': 'mark-hd'})

                    elif p_in <= grace_time:
                        if p_out >= off_end:
                            if grace_used < grace_limit:
                                grace_used += 1
                                day_info.update({'status': 'FD', 'css': 'mark-fd', 'label': f'G{grace_used}'})
                            else:
                                day_info.update({'status': 'HD', 'css': 'mark-hd', 'label': 'Grace Exhausted'})
                        else:
                            day_info.update({'status': 'HD', 'css': 'mark-hd'})
                    else:
                        day_info.update({'status': 'HD', 'css': 'mark-hd'})

            report_data.append(day_info)

        ctx.update({
            'report': report_data,
            'month_name': calendar.month_name[month],
            'year': year,
        })
        return ctx

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        emp = self.object
        month = int(self.kwargs['month'])
        year = int(self.kwargs['year'])
        tz = timezone.get_current_timezone()

        # Company Rules
        comp = emp.company
        off_start, off_end = comp.office_start_time, comp.office_end_time
        grace_deadline_mins = comp.grace_minutes
        grace_limit = comp.grace_allowed_count

        days_in_month = calendar.monthrange(year, month)[1]
        report_data = []
        grace_used = 0

        # Prefetch data for the whole month to avoid DB hits in loop
        records = {r.attendance_date: r for r in m.AttendanceRecord.objects.filter(
            employee=emp, attendance_date__year=year, attendance_date__month=month).order_by('attendance_date')}
        # This replaces the line that was causing the error
        holiday_dates = []
        if emp.holiday_calendar:
            holiday_dates = m.Holiday.objects.filter(
                calendar=emp.holiday_calendar,
                date__year=year,
                date__month=month
            ).values_list('date', flat=True)

        # Build a lookup for leaves covering this month
        leaves = m.LeaveApplication.objects.filter(
            employee=emp, status='approved',
            start_date__lte=date(year, month, days_in_month),
            end_date__gte=date(year, month, 1)
        ).select_related('leave_type')

        leave_map = {}
        for l in leaves:
            curr = l.start_date
            while curr <= l.end_date:
                if curr.month == month and curr.year == year:
                    # Store the code (e.g. SL, CL)
                    leave_map[curr] = l.leave_type.code.upper()
                curr += timedelta(days=1)

        for d in range(1, days_in_month + 1):
            dt = date(year, month, d)
            rec = records.get(dt)
            leave_code = leave_map.get(dt)
            is_holiday = dt in holiday_dates
            is_sunday = dt.weekday() == 6

            day_info = {
                'date': dt, 'in': None, 'out': None, 'hours': 0,
                'status': 'ABS', 'label': '', 'css': 'mark-abs'
            }

            # --- PRIORITY 1: HOLIDAYS / SUNDAYS ---
            if is_holiday or is_sunday:
                day_info['status'] = 'HOL' if is_holiday else 'SUN'
                day_info['css'] = 'text-muted'
                if rec and rec.check_in:
                    day_info['label'] = 'Extra Work'

            # --- PRIORITY 2: ACTUAL PUNCH RECORD (Even if Leave is approved) ---
            elif rec and rec.check_in:
                local_in = timezone.localtime(rec.check_in)
                local_out = timezone.localtime(rec.check_out) if rec.check_out else None
                day_info['in'] = local_in
                day_info['out'] = local_out

                p_in = local_in.time()
                p_out = local_out.time() if local_out else off_start

                # Effective Hours Math (10-6 rule)
                eff_s = max(p_in, off_start)
                eff_e = min(p_out, off_end)
                eff_hours = (datetime.combine(dt, eff_e) - datetime.combine(dt, eff_s)).total_seconds() / 3600
                day_info['hours'] = round(eff_hours, 2)

                grace_time = (datetime.combine(dt, off_start) + timedelta(minutes=grace_deadline_mins)).time()

                # Determine Punch Status
                punch_status = 'HD'
                punch_label = ''

                if p_in <= off_start:
                    if eff_hours >= 8:
                        punch_status = 'FD'
                    else:
                        punch_status = 'HD'
                elif p_in <= grace_time:
                    if local_out and local_out.time() >= off_end:
                        if grace_used < grace_limit:
                            grace_used += 1
                            punch_status = 'FD'
                            punch_label = f'G{grace_used}'
                        else:
                            punch_status = 'HD'
                            punch_label = 'Grace Exhausted'
                    else:
                        punch_status = 'HD'
                else:
                    punch_status = 'HD'

                # SMART MERGE: If punch is HD but they have an approved leave for the day
                if punch_status == 'HD' and leave_code:
                    day_info.update({
                        'status': 'HD',
                        'css': 'mark-hd',
                        'label': f'HD + {leave_code}'  # Highlights the combined status
                    })
                else:
                    day_info.update({
                        'status': punch_status,
                        'css': 'mark-fd' if punch_status == 'FD' else 'mark-hd',
                        'label': punch_label
                    })

            # --- PRIORITY 3: APPROVED LEAVES (Only if no punches found) ---
            elif leave_code:
                day_info.update({
                    'status': leave_code,
                    'css': 'bg-primary text-white',
                    'label': 'Approved Leave'
                })

            # --- FALLBACK: Database 'on_leave' status (for safety) ---
            elif rec and rec.status == 'on_leave':
                day_info.update({
                    'status': 'LEAVE',
                    'label': rec.remarks or 'Approved Leave',
                    'css': 'bg-primary text-white'
                })

            report_data.append(day_info)

        ctx.update({
            'report': report_data,
            'month_name': calendar.month_name[month],
            'year': year,
        })
        return ctx

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        emp = self.object
        month = int(self.kwargs['month'])
        year = int(self.kwargs['year'])
        tz = timezone.get_current_timezone()
        comp = emp.company

        days_in_month = calendar.monthrange(year, month)[1]
        report_data = []
        grace_used = 0

        # Prefetch data
        records = {r.attendance_date: r for r in m.AttendanceRecord.objects.filter(
            employee=emp, attendance_date__year=year, attendance_date__month=month).order_by('attendance_date')}

        holiday_dates = []
        if emp.holiday_calendar:
            holiday_dates = m.Holiday.objects.filter(
                calendar=emp.holiday_calendar, date__year=year, date__month=month
            ).values_list('date', flat=True)

        leaves = m.LeaveApplication.objects.filter(
            employee=emp, status='approved',
            start_date__lte=date(year, month, days_in_month),
            end_date__gte=date(year, month, 1)
        ).select_related('leave_type')

        leave_map = {}
        for l in leaves:
            curr = l.start_date
            while curr <= l.end_date:
                if curr.month == month and curr.year == year:
                    leave_map[curr] = l.leave_type.code.upper()
                curr += timedelta(days=1)

        for d in range(1, days_in_month + 1):
            dt = date(year, month, d)

            # --- SMART LOGIC: FETCH POLICY PER DAY ---
            # This ensures July shows 10-6 and August shows 9-5 automatically
            policy = comp.get_policy_for_date(dt)
            off_start = policy.office_start_time
            off_end = policy.office_end_time
            grace_deadline_mins = policy.grace_minutes
            grace_limit = policy.grace_allowed_count
            full_threshold = float(policy.full_day_threshold_hours)
            # ------------------------------------------

            rec = records.get(dt)
            leave_code = leave_map.get(dt)
            is_holiday = dt in holiday_dates
            is_sunday = dt.weekday() == 6

            day_info = {
                'date': dt, 'in': None, 'out': None, 'hours': 0,
                'status': 'ABS', 'label': '', 'css': 'mark-abs'
            }

            if is_holiday or is_sunday:
                day_info['status'] = 'HOL' if is_holiday else 'SUN'
                day_info['css'] = 'text-muted border'
                if rec and rec.check_in:
                    day_info['label'] = 'Extra Work'

            elif rec and rec.check_in:
                local_in = timezone.localtime(rec.check_in)
                local_out = timezone.localtime(rec.check_out) if rec.check_out else None
                day_info['in'] = local_in
                day_info['out'] = local_out

                p_in = local_in.time()
                p_out = local_out.time() if local_out else off_start

                eff_s = max(p_in, off_start)
                eff_e = min(p_out, off_end)
                eff_hours = (datetime.combine(dt, eff_e) - datetime.combine(dt, eff_s)).total_seconds() / 3600
                day_info['hours'] = round(eff_hours, 2)

                grace_time = (datetime.combine(dt, off_start) + timedelta(minutes=grace_deadline_mins)).time()

                punch_status = 'HD'
                punch_label = ''

                if p_in <= off_start:
                    if eff_hours >= full_threshold:  # Uses policy threshold
                        punch_status = 'FD'
                    else:
                        punch_status = 'HD'
                elif p_in <= grace_time:
                    if local_out and local_out.time() >= off_end:
                        if grace_used < grace_limit:
                            grace_used += 1
                            punch_status = 'FD'
                            punch_label = f'Grace Strike {grace_used}/{grace_limit}'
                        else:
                            punch_status = 'HD'
                            punch_label = 'Grace Exhausted'
                    else:
                        punch_status = 'HD'
                else:
                    punch_status = 'HD'

                if punch_status == 'HD' and leave_code:
                    day_info.update({
                        'status': 'HD', 'css': 'mark-hd', 'label': f'HD + {leave_code}'
                    })
                else:
                    day_info.update({
                        'status': punch_status,
                        'css': 'mark-fd' if punch_status == 'FD' else 'mark-hd',
                        'label': punch_label
                    })

            elif leave_code:
                day_info.update({'status': leave_code,
                                 'css': 'bg-primary bg-opacity-10 text-primary border border-primary border-opacity-25',
                                 'label': 'Approved Leave'})

            elif rec and rec.status == 'on_leave':
                day_info.update(
                    {'status': 'LEAVE', 'label': rec.remarks or 'Approved Leave', 'css': 'text-primary border'})

            report_data.append(day_info)

        ctx.update({
            'report': report_data,
            'month_name': calendar.month_name[month],
            'year': year,
        })
        return ctx
# ---------------------------------------------------------------------------
# Loans/Advances & Payroll Extras (Incentives) — HR/Admin manage
# ---------------------------------------------------------------------------
class LoanAdvanceListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.LoanAdvance
    template_name = 'hrms/payroll/loan_list.html'
    context_object_name = 'loans'
    active_group, active_item = 'payroll', 'loan'

    def get_queryset(self):
        return m.LoanAdvance.objects.select_related('employee').order_by('-is_active', '-start_date')


class LoanAdvanceCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.LoanAdvance
    form_class = f.LoanAdvanceForm
    template_name = 'hrms/payroll/loan_form.html'
    success_url = reverse_lazy('hrms:loan_list')
    active_group, active_item = 'payroll', 'loan'

    def form_valid(self, form):
        messages.success(self.request, 'Loan/advance recorded.')
        return super().form_valid(form)


class LoanAdvanceUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.LoanAdvance
    form_class = f.LoanAdvanceForm
    template_name = 'hrms/payroll/loan_form.html'
    success_url = reverse_lazy('hrms:loan_list')
    active_group, active_item = 'payroll', 'loan'

    def form_valid(self, form):
        messages.success(self.request, 'Loan/advance updated.')
        return super().form_valid(form)


class PayrollExtraListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.PayrollExtra
    template_name = 'hrms/payroll/extra_list.html'
    context_object_name = 'extras'
    active_group, active_item = 'payroll', 'extra'

    def get_queryset(self):
        return m.PayrollExtra.objects.select_related('employee').order_by('is_consumed', '-created_at')


class PayrollExtraCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.PayrollExtra
    form_class = f.PayrollExtraForm
    template_name = 'hrms/payroll/extra_form.html'
    success_url = reverse_lazy('hrms:extra_list')
    active_group, active_item = 'payroll', 'extra'

    def form_valid(self, form):
        messages.success(self.request, 'Incentive/extra queued for the employee\'s next payroll run.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Payroll Runs — the engine
# ---------------------------------------------------------------------------
class PayrollRunListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.PayrollRun
    template_name = 'hrms/payroll/payrollrun_list.html'
    context_object_name = 'runs'
    active_group, active_item = 'payroll', 'payrollrun'

    def get_queryset(self):
        return m.PayrollRun.objects.select_related('company').order_by('-year', '-month')




class PayrollProcessView(HRRequiredMixin, SidebarContextMixin, View):
    """Accountant/Admin selects Company + Month + Year; process_payroll() loops
    every active employee in that company and creates/updates their PaySlip.
    Renders the Bootstrap 5 payroll summary table directly after processing."""
    active_group, active_item = 'payroll', 'payrollrun'
    template_name = 'hrms/payroll/payroll_process.html'

    def test_func(self):
        # Matches the prompt's explicit requirement: is_accountant or is_superuser only.
        user = self.request.user
        return user.is_authenticated and (user.is_superuser or getattr(user, 'is_accountant', False))

    def get(self, request):
        form = f.PayrollProcessForm(initial={
            'month': timezone.localdate().month, 'year': timezone.localdate().year,
        })
        return render(request, self.template_name, {
            'form': form, 'active_group': self.active_group, 'active_item': self.active_item,
        })

    @staticmethod
    def process_payroll_logic(payroll_run, company, year, month):
        """
        Processes each active employee's attendance and calculates their payslip.
        This follows the 'Smart Brain' logic: 8-hour effective time & Sequential Grace.
        """
        from decimal import Decimal, ROUND_HALF_UP

        employees = m.Employee.objects.filter(company=company, status='active')
        total_days_in_month = calendar.monthrange(year, month)[1]
        tz = timezone.get_current_timezone()

        # Office Boundaries from Company Model
        # off_start = company.office_start_time
        active_policy = company.get_policy_for_date(curr_date)
        off_start = active_policy.office_start_time

        off_start = active_policy.office_start_time
        off_end = active_policy.office_end_time
        grace_limit = active_policy.grace_allowed_count

        for emp in employees:
            salary = emp.salaries.filter(is_active=True).first()
            if not salary:
                continue

            # 1. Base Money Math
            monthly_ctc = salary.ctc_annual / Decimal('12')
            daily_wage = (monthly_ctc / Decimal(total_days_in_month)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            # 2. Initialize Counters (Decimal for precision)
            stats = {
                'full_days': Decimal('0'),
                'half_days': Decimal('0'),
                'off_days': Decimal('0'),  # Holidays/Sundays not worked
                'comp_off_days': Decimal('0'),  # Holidays/Sundays worked
                'paid_leaves': Decimal('0'),
                'unpaid_days': Decimal('0'),
                'extra_work_days': Decimal('0')
            }
            grace_used_this_month = 0

            # 3. Daily Loop
            for day_num in range(1, total_days_in_month + 1):
                curr_date = date(year, month, day_num)
                # --- MOVE POLICY FETCHING HERE ---
                # This ensures if the time changed on Aug 1st, it switches correctly!
                active_policy = company.get_policy_for_date(curr_date)

                off_start = active_policy.office_start_time
                off_end = active_policy.office_end_time
                grace_limit = active_policy.grace_allowed_count

                is_holiday = m.Holiday.objects.filter(company=company, date=curr_date).exists()
                is_sunday = curr_date.weekday() == 6

                att = m.AttendanceRecord.objects.filter(employee=emp, attendance_date=curr_date).first()

                # --- RULE A: Holiday/Sunday Logic ---
                if is_holiday or is_sunday:
                    if att and att.check_in:
                        stats['comp_off_days'] += 1
                        stats['extra_work_days'] += 1
                    else:
                        stats['off_days'] += 1
                    continue

                # --- RULE B: Check for Punches (The Smart Logic) ---
                if att and att.check_in:
                    local_in = timezone.localtime(att.check_in)
                    local_out = timezone.localtime(att.check_out) if att.check_out else None

                    p_in = local_in.time()
                    p_out = local_out.time() if local_out else off_start  # Default to start if no checkout

                    # Calculate Effective Time (Only count 10:00 AM to 6:00 PM)
                    eff_s = max(p_in, off_start)
                    eff_e = min(p_out, off_end)
                    eff_hours = (datetime.combine(curr_date, eff_e) - datetime.combine(curr_date,
                                                                                       eff_s)).total_seconds() / 3600

                    grace_deadline = (datetime.combine(curr_date, off_start) + timedelta(minutes=grace_mins)).time()

                    # Logic Path 1: Arrived On Time (<= 10:00)
                    if p_in <= off_start:
                        if eff_hours >= 8:
                            stats['full_days'] += 1
                        elif eff_hours >= 4:
                            stats['half_days'] += 1
                        else:
                            stats['unpaid_days'] += 1

                    # Logic Path 2: Arrived in Grace Window (10:01 - 10:15)
                    elif p_in <= grace_deadline:
                        # Grace applies ONLY if they stayed until the end of office hours (6:00 PM)
                        if p_out >= off_end:
                            if grace_used_this_month < grace_limit:
                                grace_used_this_month += 1
                                stats['full_days'] += 1  # Grace "saves" the Full Day
                            else:
                                stats['half_days'] += 1  # Grace Exhausted
                        else:
                            # Came late, left early -> HD (Grace is not used/wasted)
                            stats['half_days'] += 1

                    # Logic Path 3: Arrived Late (> 10:15)
                    else:
                        stats['half_days'] += 1

                # --- RULE C: Check for Approved Leaves (If no punch) ---
                else:
                    leave = m.LeaveApplication.objects.filter(
                        employee=emp, status='approved',
                        start_date__lte=curr_date, end_date__gte=curr_date
                    ).first()

                    if leave:
                        # Intern Rule + LWP Rule
                        if emp.employment_type != 'intern' and leave.leave_type.code != 'LWP':
                            stats['paid_leaves'] += 1
                        else:
                            stats['unpaid_days'] += 1
                    else:
                        stats['unpaid_days'] += 1

            # 4. Final Money Calculations
            paid_days = stats['full_days'] + (stats['half_days'] * Decimal('0.5')) + \
                        stats['paid_leaves'] + stats['off_days'] + stats['comp_off_days']

            earned_wages = (paid_days * daily_wage).quantize(Decimal('0.01'))
            penalty_deduction = (stats['unpaid_days'] * daily_wage).quantize(Decimal('0.01'))

            # Loans & Extras
            loan = emp.loans.filter(is_active=True).first()
            loan_deduction = min(loan.monthly_installment, loan.remaining_balance) if loan else Decimal('0')

            extras = m.PayrollExtra.objects.filter(employee=emp, is_consumed=False)
            extra_earning = sum(ex.amount for ex in extras)

            total_earnings = (salary.basic + salary.hra + salary.special_allowance + extra_earning).quantize(
                Decimal('0.01'))
            total_deductions = (salary.pf_employee + salary.esic_employee + salary.professional_tax + \
                                salary.tds + loan_deduction).quantize(Decimal('0.01'))

            net_pay = total_earnings - total_deductions - penalty_deduction

            # 5. Save to PaySlip Model
            m.PaySlip.objects.update_or_create(
                payroll_run=payroll_run,
                employee=emp,
                defaults={
                    'full_days': stats['full_days'],
                    'half_days': stats['half_days'],
                    'off_days': stats['off_days'],
                    'comp_off_days': stats['comp_off_days'],
                    'paid_leave_days': stats['paid_leaves'],
                    'absent_days': stats['unpaid_days'],
                    'paid_days': paid_days,
                    'total_days_in_month': total_days_in_month,
                    'daily_wage': daily_wage,
                    'penalty_deduction': penalty_deduction,
                    'loan_deduction': loan_deduction,
                    'extra_earning': extra_earning,
                    'total_earnings': total_earnings,
                    'total_deductions': total_deductions,
                    'net_pay': net_pay,
                }
            )

    @staticmethod
    def process_payroll_logic(payroll_run, company, year, month):
        """
        Calculates payslips. Rules: 8-hour effective time, Sequential Grace, Versioned Policy.
        """
        from decimal import Decimal, ROUND_HALF_UP
        from datetime import date, datetime, timedelta

        employees = m.Employee.objects.filter(company=company, status='active')
        total_days_in_month = calendar.monthrange(year, month)[1]

        for emp in employees:
            salary = emp.salaries.filter(is_active=True).first()
            if not salary:
                continue

            # 1. Money Setup
            monthly_ctc = salary.ctc_annual / Decimal('12')
            daily_wage = (monthly_ctc / Decimal(total_days_in_month)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            stats = {
                'full_days': Decimal('0'), 'half_days': Decimal('0'),
                'off_days': Decimal('0'), 'comp_off_days': Decimal('0'),
                'paid_leaves': Decimal('0'), 'unpaid_days': Decimal('0')
            }
            grace_used_this_month = 0

            # 2. Daily Loop
            for day_num in range(1, total_days_in_month + 1):
                curr_date = date(year, month, day_num)

                # --- FETCH ACTIVE POLICY FOR THIS SPECIFIC DATE ---
                active_policy = company.get_policy_for_date(curr_date)
                off_start = active_policy.office_start_time
                off_end = active_policy.office_end_time
                grace_limit = active_policy.grace_allowed_count
                grace_mins = active_policy.grace_minutes  # FIX: Added this variable
                # --------------------------------------------------

                is_holiday = m.Holiday.objects.filter(company=company, date=curr_date).exists()
                is_sunday = curr_date.weekday() == 6
                att = m.AttendanceRecord.objects.filter(employee=emp, attendance_date=curr_date).first()

                # Rule A: Holiday/Sunday
                if is_holiday or is_sunday:
                    if att and att.check_in:
                        stats['comp_off_days'] += 1
                    else:
                        stats['off_days'] += 1
                    continue

                # Rule B: Attendance Punches
                if att and att.check_in:
                    local_in = timezone.localtime(att.check_in)
                    local_out = timezone.localtime(att.check_out) if att.check_out else None

                    p_in = local_in.time()
                    p_out = local_out.time() if local_out else off_start

                    # Math for effective hours (10-6 rule)
                    eff_s = max(p_in, off_start)
                    eff_e = min(p_out, off_end)
                    eff_hours = (datetime.combine(curr_date, eff_e) - datetime.combine(curr_date,
                                                                                       eff_s)).total_seconds() / 3600
                    grace_deadline = (datetime.combine(curr_date, off_start) + timedelta(minutes=grace_mins)).time()

                    if p_in <= off_start:
                        if eff_hours >= 8:
                            stats['full_days'] += 1
                        elif eff_hours >= 4:
                            stats['half_days'] += 1
                        else:
                            stats['unpaid_days'] += 1

                    elif p_in <= grace_deadline:
                        if p_out >= off_end:
                            if grace_used_this_month < grace_limit:
                                grace_used_this_month += 1
                                stats['full_days'] += 1  # Grace saves FD
                            else:
                                stats['half_days'] += 1  # Exhausted
                        else:
                            stats['half_days'] += 1  # Left early
                    else:
                        stats['half_days'] += 1  # Late > 10:15

                else:
                    # Rule C: Leaves
                    leave = m.LeaveApplication.objects.filter(
                        employee=emp, status='approved',
                        start_date__lte=curr_date, end_date__gte=curr_date
                    ).first()
                    if leave and emp.employment_type != 'intern' and leave.leave_type.code != 'LWP':
                        stats['paid_leaves'] += 1
                    else:
                        stats['unpaid_days'] += 1

            # 3. Final Calculations
            paid_days = stats['full_days'] + (stats['half_days'] * Decimal('0.5')) + \
                        stats['paid_leaves'] + stats['off_days'] + stats['comp_off_days']

            penalty_deduction = (stats['unpaid_days'] * daily_wage).quantize(Decimal('0.01'))

            loan = emp.loans.filter(is_active=True).first()
            loan_deduction = min(loan.monthly_installment, loan.remaining_balance) if loan else Decimal('0')

            extras = m.PayrollExtra.objects.filter(employee=emp, is_consumed=False)
            extra_earning = sum(ex.amount for ex in extras)

            total_earnings = (salary.basic + salary.hra + salary.special_allowance + extra_earning).quantize(
                Decimal('0.01'))
            total_deductions = (salary.pf_employee + salary.esic_employee + salary.professional_tax + \
                                salary.tds + loan_deduction).quantize(Decimal('0.01'))

            net_pay = total_earnings - total_deductions - penalty_deduction

            # 4. Save
            m.PaySlip.objects.update_or_create(
                payroll_run=payroll_run, employee=emp,
                defaults={
                    'full_days': stats['full_days'], 'half_days': stats['half_days'],
                    'off_days': stats['off_days'], 'comp_off_days': stats['comp_off_days'],
                    'paid_leave_days': stats['paid_leaves'], 'absent_days': stats['unpaid_days'],
                    'paid_days': paid_days, 'total_days_in_month': total_days_in_month,
                    'daily_wage': daily_wage, 'penalty_deduction': penalty_deduction,
                    'loan_deduction': loan_deduction, 'extra_earning': extra_earning,
                    'total_earnings': total_earnings, 'total_deductions': total_deductions,
                    'net_pay': net_pay,
                }
            )

    def post(self, request):
        # data = calculate_payroll_for_emp(emp, m, y)

        form = f.PayrollProcessForm(request.POST)
        if form.is_valid():
            company = form.cleaned_data['company']
            month = int(form.cleaned_data['month'])
            year = form.cleaned_data['year']
            payroll_run, warnings = pay.process_payroll(company, year, month, user=request.user)
            for w in warnings:
                messages.warning(request, w)
            messages.success(
                request,
                f'Payroll processed for {company} — {_calendar.month_name[month]} {year} '
                f'({payroll_run.payslips.count()} employee(s)).'
            )
            return redirect('hrms:payrollrun_detail', pk=payroll_run.pk)
        return render(request, self.template_name, {
            'form': form, 'active_group': self.active_group, 'active_item': self.active_item,
        })


class PayrollRunDetailView(HRRequiredMixin, SidebarContextMixin, DetailView):
    """The Bootstrap 5 payroll summary table for one processed run."""
    model = m.PayrollRun
    template_name = 'hrms/payroll/payrollrun_detail.html'
    context_object_name = 'run'
    active_group, active_item = 'payroll', 'payrollrun'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['payslips'] = (
            self.object.payslips
            .select_related('employee', 'employee__designation')
            .order_by('employee__employee_code')
        )
        ctx['totals'] = self.object.payslips.aggregate(
            total_net=Sum('net_pay'), total_earnings=Sum('total_earnings'), total_deductions=Sum('total_deductions'))
        return ctx


# ---------------------------------------------------------------------------
# Payslips — HR sees all (filterable); employee sees only their own
# ---------------------------------------------------------------------------
class PaySlipListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.PaySlip
    template_name = 'hrms/payroll/payslip_list.html'
    context_object_name = 'payslips'
    active_group, active_item = 'payroll', 'my_payslips'
    paginate_by = 30

    def get_queryset(self):
        qs = m.PaySlip.objects.select_related('employee', 'payroll_run').order_by(
            '-payroll_run__year', '-payroll_run__month')
        if is_hr_or_above(self.request.user):
            employee_id = self.request.GET.get('employee')
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
            return qs
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.PaySlip.objects.none()
        return qs.filter(employee=employee)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_hr'] = is_hr_or_above(self.request.user)
        if ctx['is_hr']:
            ctx['employees'] = m.Employee.objects.order_by('employee_code')
        return ctx


class PaySlipDetailView(LoginRequiredMixin, SidebarContextMixin, DetailView):
    model = m.PaySlip
    template_name = 'hrms/payroll/payslip_detail.html'
    context_object_name = 'payslip'
    active_group, active_item = 'payroll', 'my_payslips'

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not is_hr_or_above(self.request.user):
            employee = get_employee_profile(self.request.user)
            if employee is None or obj.employee_id != employee.id:
                raise PermissionDenied("You can only view your own payslips.")
        return obj


class PaySlipPDFView(LoginRequiredMixin, View):
    """Generates a simple, professional payslip PDF with ReportLab."""

    def get(self, request, pk):
        payslip = get_object_or_404(m.PaySlip.objects.select_related('employee', 'payroll_run', 'employee__company'), pk=pk)
        if not is_hr_or_above(request.user):
            employee = get_employee_profile(request.user)
            if employee is None or payslip.employee_id != employee.id:
                raise PermissionDenied("You can only view your own payslips.")

        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.units import mm
            from reportlab.lib import colors
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet
        except ImportError:
            return HttpResponse(
                'PDF export requires ReportLab. Install it with: pip install reportlab --break-system-packages',
                status=501, content_type='text/plain',
            )

        from io import BytesIO
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
        styles = getSampleStyleSheet()
        elements = []

        run = payslip.payroll_run
        employee = payslip.employee
        period = f'{_calendar.month_name[run.month]} {run.year}'

        elements.append(Paragraph(f'<b>{employee.company.name}</b>', styles['Title']))
        elements.append(Paragraph(f'Payslip — {period}', styles['Heading3']))
        elements.append(Spacer(1, 8))

        info_table = Table([
            ['Employee', employee.full_name, 'Employee Code', employee.employee_code],
            ['Designation', str(employee.designation or '—'), 'Department', str(employee.department or '—')],
            ['Paid Days', str(payslip.paid_days), 'Absent Days', str(payslip.absent_days)],
        ], colWidths=[80, 150, 90, 150])
        info_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (2, 0), (2, -1), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(info_table)
        elements.append(Spacer(1, 14))

        breakdown = Table([
            ['Earnings', 'Amount (₹)', 'Deductions', 'Amount (₹)'],
            ['Extra / Incentive', f'{payslip.extra_earning}', 'Loan/Advance', f'{payslip.loan_deduction}'],
            ['', '', 'Absence Penalty', f'{payslip.penalty_deduction}'],
            ['Total Earnings', f'{payslip.total_earnings}', 'Total Deductions', f'{payslip.total_deductions}'],
        ], colWidths=[110, 90, 110, 90])
        breakdown.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#101b2d')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('LINEABOVE', (0, -1), (-1, -1), 0.5, colors.grey),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#e4e8ef')),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(breakdown)
        elements.append(Spacer(1, 14))

        net_table = Table([['Net Pay', f'Rs. {payslip.net_pay}']], colWidths=[300, 100])
        net_table.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 12),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica-Bold'),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor('#2fb8a3')),
        ]))
        elements.append(net_table)

        doc.build(elements)
        buffer.seek(0)
        response = HttpResponse(buffer.read(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="payslip_{employee.employee_code}_{run.year}_{run.month}.pdf"'
        return response


# ===========================================================================
# HIRING / RECRUITMENT PIPELINE
# ===========================================================================
from . import hiring_logic as hire


class JobPostingListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.JobPosting
    template_name = 'hrms/hiring/jobposting_list.html'
    context_object_name = 'postings'
    active_group, active_item = 'hiring', 'jobposting'

    def get_queryset(self):
        return m.JobPosting.objects.select_related('company', 'department').order_by('-posted_on')


class JobPostingCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.JobPosting
    form_class = f.JobPostingForm
    template_name = 'hrms/hiring/jobposting_form.html'
    success_url = reverse_lazy('hrms:jobposting_list')
    active_group, active_item = 'hiring', 'jobposting'

    def form_valid(self, form):
        messages.success(self.request, 'Job posting created.')
        return super().form_valid(form)


class JobPostingUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.JobPosting
    form_class = f.JobPostingForm
    template_name = 'hrms/hiring/jobposting_form.html'
    success_url = reverse_lazy('hrms:jobposting_list')
    active_group, active_item = 'hiring', 'jobposting'

    def form_valid(self, form):
        messages.success(self.request, 'Job posting updated.')
        return super().form_valid(form)


class JobPostingDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.JobPosting
    template_name = 'hrms/hiring/jobposting_confirm_delete.html'
    success_url = reverse_lazy('hrms:jobposting_list')
    active_group, active_item = 'hiring', 'jobposting'

    def form_valid(self, form):
        messages.success(self.request, 'Job posting deleted.')
        return super().form_valid(form)


class CandidateListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Candidate
    template_name = 'hrms/hiring/candidate_list.html'
    context_object_name = 'candidates'
    active_group, active_item = 'hiring', 'candidate'
    paginate_by = 30

    def get_queryset(self):
        qs = m.Candidate.objects.all().order_by('-created_at')
        q = self.request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['q'] = self.request.GET.get('q', '')
        return ctx


class CandidateCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Candidate
    form_class = f.CandidateForm
    template_name = 'hrms/hiring/candidate_form.html'
    success_url = reverse_lazy('hrms:candidate_list')
    active_group, active_item = 'hiring', 'candidate'

    # views.py
    def form_valid(self, form):
        with transaction.atomic():
            # This part MUST save the names into the Candidate object
            candidate = form.save()

            # Then create the application
            m.Application.objects.create(
                candidate=candidate,
                job_posting=form.cleaned_data['job_posting'],
                status=form.cleaned_data['status'],
                # ... other fields
            )
        return super().form_valid(form)

from django.db import transaction


class CandidateCreateView(CreateView):
    model = m.Candidate
    form_class = UnifiedCandidateForm
    template_name = 'hrms/hiring/candidate_form.html'
    success_url = reverse_lazy('hrms:candidate_list')

    def form_valid(self, form):
        with transaction.atomic():
            # 1. Save Candidate
            self.object = form.save()

            # 2. Extract application fields and save Application
            m.Application.objects.create(
                candidate=self.object,
                job_posting=form.cleaned_data['job_posting'],
                status=form.cleaned_data['status'],
                source=form.cleaned_data['source'],
                cover_letter=form.cleaned_data['cover_letter']
            )
        return super().form_valid(form)

class CandidateDetailView(HRRequiredMixin, SidebarContextMixin, DetailView):
    model = m.Candidate
    template_name = 'hrms/hiring/candidate_detail.html'
    context_object_name = 'candidate'
    active_group, active_item = 'hiring', 'candidate'


class ApplicationListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Application
    template_name = 'hrms/hiring/application_list.html'
    context_object_name = 'applications'
    active_group, active_item = 'hiring', 'application'
    paginate_by = 30

    def get_queryset(self):
        qs = m.Application.objects.select_related('candidate', 'job_posting').order_by('-applied_on')
        status = self.request.GET.get('status')
        if status:
            qs = qs.filter(status=status)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['status_choices'] = m.Application.Status.choices
        ctx['selected_status'] = self.request.GET.get('status', '')
        return ctx


from django.views.generic import ListView
from .models import Application, JobPosting


class ApplicationListView(ListView):
    model = Application
    template_name = 'hrms/hiring/application_list.html'  # ensure this path matches your files
    context_object_name = 'applications'

    def get_queryset(self):
        queryset = super().get_queryset().select_related('candidate', 'job_posting')

        # 1. Filter by Job ID (from the link in Job Posting page)
        job_id = self.request.GET.get('job')
        if job_id:
            queryset = queryset.filter(job_posting_id=job_id)

        # 2. Filter by Status (useful for "field wise" filtration)
        status = self.request.GET.get('status')
        if status:
            queryset = queryset.filter(status=status)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Pass extra data for the filter UI
        context['jobs'] = JobPosting.objects.all()
        context['statuses'] = Application.Status.choices
        context['current_job_id'] = self.request.GET.get('job')
        context['current_status'] = self.request.GET.get('status')
        return context

class ApplicationCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Application
    form_class = f.ApplicationForm
    template_name = 'hrms/hiring/application_form.html'
    active_group, active_item = 'hiring', 'application'

    def form_valid(self, form):
        messages.success(self.request, 'Application recorded.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:application_detail', kwargs={'pk': self.object.pk})


class ApplicationDetailView(HRRequiredMixin, SidebarContextMixin, DetailView):
    """The pipeline hub: shows interviews, offer letter (if any), and the
    quick-action buttons that move the candidate through the pipeline —
    including 'Generate Offer Letter', the flow the prompt asked for."""
    model = m.Application
    template_name = 'hrms/hiring/application_detail.html'
    context_object_name = 'application'
    active_group, active_item = 'hiring', 'application'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['interviews'] = self.object.interviews.prefetch_related('interviewer').order_by('scheduled_on')
        ctx['offer'] = getattr(self.object, 'offer_letter', None)
        ctx['status_choices'] = m.Application.Status.choices
        ctx['audit_logs'] = self.object.audit_logs.select_related('performed_by')[:20]
        return ctx

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # 1. Use 'prefetch_related' because it's a Many-to-Many field
        # 2. Use 'interviewer' (singular), NOT 'interviewers'
        ctx['interviews'] = self.object.interviews.prefetch_related('interviewer').order_by('scheduled_on')

        ctx['offer'] = getattr(self.object, 'offer_letter', None)
        ctx['status_choices'] = m.Application.Status.choices
        ctx['audit_logs'] = self.object.audit_logs.select_related('performed_by')[:20]
        return ctx

from django.contrib import messages


class ApplicationStatusView(HRRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(m.Application, pk=pk)
        new_status = request.POST.get('status')
        reason = request.POST.get('reason', '')  # From the Reject modal

        # Check if the submitted status is valid
        valid_statuses = [choice[0] for choice in m.Application.Status.choices]

        if new_status in valid_statuses:
            # Update the status
            application.status = new_status
            application.save()

            # Optional: Add an audit log entry here
            # m.ApplicationAuditLog.objects.create(application=application, action=f"Status changed to {new_status}", note=reason, performed_by=request.user)

            messages.success(request, f"Status updated successfully to {application.get_status_display()}.")
        else:
            messages.error(request, "Unknown action or invalid status.")

        # IMPORTANT: Redirect the user back to where they came from
        # If they were on dashboard, stay on dashboard. If on detail, stay on detail.
        return redirect(request.META.get('HTTP_REFERER', reverse('hrms:application_detail', args=[pk])))


class UnlockApplicationView(SuperAdminRequiredMixin, View):
    """Super Admin only — reopens a locked (Hired/Rejected) application."""
    def post(self, request, pk):
        application = get_object_or_404(m.Application, pk=pk)
        try:
            hire.unlock_application(application, request.user)
            messages.success(request, 'Application unlocked.')
        except hire.HiringError as e:
            messages.error(request, str(e))
        return redirect('hrms:application_detail', pk=pk)


class InterviewCreateView(HRRequiredMixin, SidebarContextMixin, View):
    """Uses hiring_logic.schedule_interview() so locking, the audit trail, and
    the dual candidate/interviewer email notification all happen together."""
    active_group, active_item = 'hiring', 'interview'
    template_name = 'hrms/hiring/interview_form.html'

    def dispatch(self, request, *args, **kwargs):
        self.application = get_object_or_404(m.Application, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        form = f.InterviewForm()
        return render(request, self.template_name, {
            'form': form, 'application': self.application,
            'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request, pk):
        form = f.InterviewForm(request.POST, application=self.application)
        if form.is_valid():
            try:
                interview, email_results = hire.schedule_interview(
                    self.application, request.user,
                    interview_round=form.cleaned_data['interview_round'],
                    scheduled_on=form.cleaned_data['scheduled_on'],
                    interviewer=form.cleaned_data.get('interviewer'),
                    mode=form.cleaned_data['mode'],
                )
                messages.success(request, 'Interview scheduled.')
                _report_email_results(request, email_results)
                return redirect('hrms:application_detail', pk=self.application.pk)
            except hire.HiringError as e:
                form.add_error(None, str(e))
        return render(request, self.template_name, {
            'form': form, 'application': self.application,
            'active_group': self.active_group, 'active_item': self.active_item,
        })


class InterviewUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    """Manual corrections (feedback, status) — doesn't re-fire emails or move
    the pipeline; use the action buttons for that."""
    model = m.Interview
    form_class = f.InterviewForm
    template_name = 'hrms/hiring/interview_form.html'
    active_group, active_item = 'hiring', 'interview'

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        try:
            hire.ensure_unlocked(obj.application, self.request.user)
        except hire.HiringError as e:
            messages.error(self.request, str(e))
            raise PermissionDenied(str(e))
        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['application'] = self.object.application
        return ctx

    def form_valid(self, form):
        messages.success(self.request, 'Interview updated.')
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:application_detail', kwargs={'pk': self.object.application.pk})


class InterviewListView(HRRequiredMixin, SidebarContextMixin, ListView):
    """All upcoming/past interviews across the pipeline (HR view)."""
    model = m.Interview
    template_name = 'hrms/hiring/interview_list.html'
    context_object_name = 'interviews'
    active_group, active_item = 'hiring', 'interview'

    def get_queryset(self):
        return m.Interview.objects.select_related('application__candidate', 'application__job_posting',
                                                    'interviewer').order_by('-scheduled_on')

    def get_queryset(self):
        return m.Interview.objects.select_related(
            'application__candidate',
            'application__job_posting'
        ).prefetch_related(
            'interviewer'  # Use prefetch_related for Many-to-Many fields
        ).order_by('-scheduled_on')


class MyInterviewsView(LoginRequiredMixin, SidebarContextMixin, ListView):
    """Any logged-in employee assigned as an interviewer (not just HR) can see
    and give feedback on the interviews they've been assigned — a department
    manager who isn't 'HR' still needs to see their own interview schedule."""
    model = m.Interview
    template_name = 'hrms/hiring/my_interviews.html'
    context_object_name = 'interviews'
    active_group, active_item = 'hiring', 'my_interviews'

    def get_queryset(self):
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.Interview.objects.none()
        return m.Interview.objects.filter(interviewer=employee).select_related(
            'application__candidate', 'application__job_posting').order_by('-scheduled_on')


class InterviewFeedbackView(LoginRequiredMixin, View):
    """The assigned interviewer submits feedback + marks the interview Completed."""
    def post(self, request, pk):
        interview = get_object_or_404(m.Interview, pk=pk)
        employee = get_employee_profile(request.user)
        if not is_hr_or_above(request.user) and (employee is None or interview.interviewer_id != employee.id):
            raise PermissionDenied('You can only submit feedback for interviews assigned to you.')
        interview.feedback = request.POST.get('feedback', interview.feedback)
        interview.status = m.Interview.Status.COMPLETED
        interview.save(update_fields=['feedback', 'status', 'updated_at'])
        messages.success(request, 'Feedback submitted.')
        return redirect('hrms:my_interviews')


def _report_email_results(request, results):
    """Surfaces per-recipient email send success/failure as Django messages."""
    for role, email, ok, err in results:
        if ok:
            messages.info(request, f'Notification email sent to {role} ({email}).')
        else:
            messages.warning(request, f'Could not email {role} ({email}): {err}')


class OfferLetterCreateView(HRRequiredMixin, SidebarContextMixin, View):
    """THE flow the prompt asked for: move a Candidate/Application to OfferLetter status."""
    active_group, active_item = 'hiring', 'application'
    template_name = 'hrms/hiring/offer_form.html'

    def dispatch(self, request, *args, **kwargs):
        self.application = get_object_or_404(m.Application, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        form = f.OfferLetterForm()
        return render(request, self.template_name, {
            'form': form, 'application': self.application,
            'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request, pk):
        form = f.OfferLetterForm(request.POST)
        if form.is_valid():
            try:
                hire.move_to_offer(
                    self.application, request.user,
                    offer_date=form.cleaned_data['offer_date'],
                    ctc_offered=form.cleaned_data['ctc_offered'],
                    joining_date=form.cleaned_data.get('joining_date'),
                    expiry_date=form.cleaned_data.get('expiry_date'),
                )
                messages.success(request, f'Offer letter created for {self.application.candidate}.')
                return redirect('hrms:application_detail', pk=self.application.pk)
            except hire.HiringError as e:
                form.add_error(None, str(e))
        return render(request, self.template_name, {
            'form': form, 'application': self.application,
            'active_group': self.active_group, 'active_item': self.active_item,
        })


class OfferLetterActionView(HRRequiredMixin, View):
    """Send / Accept / Decline / Expire buttons on the application detail page."""
    def post(self, request, pk, action):
        offer = get_object_or_404(m.OfferLetter, pk=pk)
        try:
            if action == 'send':
                offer, email_results = hire.send_offer(offer, request.user)
                messages.success(request, 'Offer marked as sent.')
                _report_email_results(request, email_results)
            elif action == 'accept':
                hire.accept_offer(offer, request.user)
                messages.success(request, 'Offer accepted — application marked Hired and locked.')
            elif action == 'decline':
                hire.decline_offer(offer, request.user)
                messages.warning(request, 'Offer declined — application marked Rejected and locked.')
            elif action == 'expire':
                hire.expire_offer(offer, request.user)
                messages.info(request, 'Offer marked as expired.')
            else:
                messages.error(request, 'Unknown action.')
        except hire.HiringError as e:
            messages.error(request, str(e))
        return redirect('hrms:application_detail', pk=offer.application.pk)


class ConvertToEmployeeView(HRRequiredMixin, SidebarContextMixin, View):
    """Once an offer is ACCEPTED, turn the candidate into a real Employee record."""
    active_group, active_item = 'hiring', 'application'
    template_name = 'hrms/hiring/convert_form.html'

    def dispatch(self, request, *args, **kwargs):
        self.offer = get_object_or_404(m.OfferLetter, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        form = f.ConvertToEmployeeForm()
        return render(request, self.template_name, {
            'form': form, 'offer': self.offer,
            'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request, pk):
        form = f.ConvertToEmployeeForm(request.POST)
        if form.is_valid():
            try:
                employee = hire.convert_to_employee(
                    self.offer, request.user,
                    company=form.cleaned_data['company'],
                    department=form.cleaned_data.get('department'),
                    designation=form.cleaned_data.get('designation'),
                    employee_code=form.cleaned_data['employee_code'],
                )
                messages.success(request, f'{employee.full_name} onboarded as {employee.employee_code}.')
                return redirect('hrms:employee_detail', pk=employee.pk)
            except hire.HiringError as e:
                form.add_error(None, str(e))
        return render(request, self.template_name, {
            'form': form, 'offer': self.offer,
            'active_group': self.active_group, 'active_item': self.active_item,
        })




# --- NEW: RecruitmentStage CRUD --------------------------------------------
class RecruitmentStageListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.RecruitmentStage
    template_name = 'hrms/hiring/recruitmentstage_list.html'
    context_object_name = 'stages'
    active_group, active_item = 'hiring', 'recruitmentstage'


class RecruitmentStageCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.RecruitmentStage
    form_class = f.RecruitmentStageForm
    template_name = 'hrms/hiring/recruitmentstage_form.html'
    success_url = reverse_lazy('hrms:recruitmentstage_list')
    active_group, active_item = 'hiring', 'recruitmentstage'

    def form_valid(self, form):
        messages.success(self.request, 'Recruitment stage created.')
        return super().form_valid(form)


class RecruitmentStageUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.RecruitmentStage
    form_class = f.RecruitmentStageForm
    template_name = 'hrms/hiring/recruitmentstage_form.html'
    success_url = reverse_lazy('hrms:recruitmentstage_list')
    active_group, active_item = 'hiring', 'recruitmentstage'

    def form_valid(self, form):
        messages.success(self.request, 'Recruitment stage updated.')
        return super().form_valid(form)


class RecruitmentStageDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.RecruitmentStage
    template_name = 'hrms/hiring/recruitmentstage_confirm_delete.html'
    success_url = reverse_lazy('hrms:recruitmentstage_list')
    active_group, active_item = 'hiring', 'recruitmentstage'


# --- NEW: JobPipeline — manage a job's stage sequence on one screen -------
class JobPipelineManageView(HRRequiredMixin, SidebarContextMixin, View):
    """One page per JobPosting: an inline formset to add/reorder/remove the
    RecruitmentStages that make up that job's own hiring flow."""
    template_name = 'hrms/hiring/jobpipeline_manage.html'
    active_group, active_item = 'hiring', 'jobposting'

    def dispatch(self, request, *args, **kwargs):
        self.job = get_object_or_404(m.JobPosting, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        formset = f.JobPipelineFormSet(instance=self.job)
        return render(request, self.template_name, {
            'job': self.job, 'formset': formset,
            'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request, pk):
        formset = f.JobPipelineFormSet(request.POST, instance=self.job)
        if formset.is_valid():
            formset.save()
            messages.success(request, 'Pipeline updated.')
            return redirect('hrms:jobpipeline_manage', pk=self.job.pk)
        return render(request, self.template_name, {
            'job': self.job, 'formset': formset,
            'active_group': self.active_group, 'active_item': self.active_item,
        })


# --- NEW: OfferTemplate CRUD ------------------------------------------------
class OfferTemplateListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.OfferTemplate
    template_name = 'hrms/hiring/offertemplate_list.html'
    context_object_name = 'templates'
    active_group, active_item = 'hiring', 'offertemplate'


class OfferTemplateCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.OfferTemplate
    form_class = f.OfferTemplateForm
    template_name = 'hrms/hiring/offertemplate_form.html'
    success_url = reverse_lazy('hrms:offertemplate_list')
    active_group, active_item = 'hiring', 'offertemplate'

    def form_valid(self, form):
        messages.success(self.request, 'Offer template created.')
        return super().form_valid(form)


class OfferTemplateUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.OfferTemplate
    form_class = f.OfferTemplateForm
    template_name = 'hrms/hiring/offertemplate_form.html'
    success_url = reverse_lazy('hrms:offertemplate_list')
    active_group, active_item = 'hiring', 'offertemplate'

    def form_valid(self, form):
        messages.success(self.request, 'Offer template updated.')
        return super().form_valid(form)


class OfferTemplateDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.OfferTemplate
    template_name = 'hrms/hiring/offertemplate_confirm_delete.html'
    success_url = reverse_lazy('hrms:offertemplate_list')
    active_group, active_item = 'hiring', 'offertemplate'


class OfferTemplatePreviewView(HRRequiredMixin, View):
    """AJAX-ish endpoint: render the chosen template against an application
    so the OfferLetter form can pre-fill `content` before the user tweaks it."""
    def get(self, request, template_pk, application_pk):
        template = get_object_or_404(m.OfferTemplate, pk=template_pk)
        application = get_object_or_404(m.Application, pk=application_pk)
        ctc = request.GET.get('ctc_offered') or 0
        rendered = template.render(application, ctc_offered=ctc, offer_date=None)
        return render(request, 'hrms/hiring/_offer_preview_fragment.html', {'rendered': rendered})


from django.db.models import Prefetch
# from .hiring_logic import HiringService


# ---------------------------------------------------------------------------
# ADVANCED RECRUITMENT PIPELINE
# ---------------------------------------------------------------------------

class JobKanbanView(HRRequiredMixin, SidebarContextMixin, DetailView):
    """
    Advanced Kanban Board for a specific Job.
    Groups applications by their current stage in the pipeline.
    """
    model = m.JobPosting
    template_name = 'hrms/hiring/job_kanban.html'
    context_object_name = 'job'
    active_group, active_item = 'hiring', 'jobposting'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        job = self.object

        # Get the ordered pipeline stages for this specific job
        pipeline_stages = job.pipeline_stages.select_related('stage').order_by('order')

        # Prefetch applications for each stage to avoid N+1 queries
        apps_by_stage = collections.defaultdict(list)
        all_apps = job.applications.select_related('candidate', 'current_stage').all()

        for app in all_apps:
            apps_by_stage[app.current_stage_id].append(app)

        ctx['pipeline'] = [
            {'stage': ps.stage, 'apps': apps_by_stage[ps.stage_id]}
            for ps in pipeline_stages
        ]
        return ctx


class ApplicationDetailView(HRRequiredMixin, SidebarContextMixin, DetailView):
    """
    The 360-Degree Candidate View.
    Aggregates interviews, scores, notes, and timeline.
    """
    model = m.Application
    template_name = 'hrms/hiring/application_detail.html'
    context_object_name = 'application'
    active_group, active_item = 'hiring', 'application'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        app = self.object

        # Dynamic Pipeline Visualization
        ctx['stages'] = app.job_posting.pipeline_stages.select_related('stage').order_by('order')

        # Detailed Interview Scores
        ctx['interviews'] = app.interviews.prefetch_related(
            'interviewers',
            'individual_feedbacks__interviewer'
        ).all()

        # Collaboration & Notes
        ctx['notes'] = app.notes.select_related('author').all()

        # History
        ctx['audit_logs'] = app.audit_logs.select_related('performed_by').all()

        return ctx


class SubmitInterviewFeedbackView(LoginRequiredMixin, SidebarContextMixin, CreateView):
    """
    Class-Based View for interviewers to submit private scorecard ratings.
    Utilizes the evaluation_criteria defined in the RecruitmentStage.
    """
    model = m.InterviewFeedback
    fields = ['recommendation', 'feedback_text', 'scorecard_filled']
    template_name = 'hrms/hiring/feedback_form.html'
    active_group, active_item = 'hiring', 'my_interviews'

    def form_valid(self, form):
        interview = get_object_or_404(m.Interview, pk=self.kwargs['interview_pk'])
        employee = get_employee_profile(self.request.user)

        form.instance.interview = interview
        form.instance.interviewer = employee

        messages.success(self.request, "Your feedback has been recorded.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('hrms:my_interviews')


class FinalizeHiringActionView(HRRequiredMixin, View):
    """
    The 'Trigger' view to execute Candidate-to-Employee conversion.
    """

    def post(self, request, pk):
        application = get_object_or_404(m.Application, pk=pk)

        if application.is_locked:
            messages.error(request, "This application is already locked.")
            return redirect('hrms:application_detail', pk=pk)

        try:
            employee = HiringService.convert_to_employee(application, request.user)
            messages.success(request, f"Success! {employee.full_name} is now an active employee.")
            return redirect('hrms:employee_detail', pk=employee.pk)
        except Exception as e:
            messages.error(request, f"Conversion failed: {str(e)}")
            return redirect('hrms:application_detail', pk=pk)

# ===========================================================================
# ASSET MANAGEMENT
# ===========================================================================
class AssetListView(CompanyFilterMixin,LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.Asset
    template_name = 'hrms/asset/asset_list.html'
    context_object_name = 'assets'
    active_group, active_item = 'asset', 'asset'
    paginate_by = 30

    def get_queryset(self):
        # Start with standard relations
        qs = m.Asset.objects.select_related('employee', 'company')

        # 1. HR/ADMIN LOGIC
        if is_hr_or_above(self.request.user):
            status = self.request.GET.get('status')
            if status:
                qs = qs.filter(status=status)
            return qs.order_by('-created_at')

        # 2. EMPLOYEE LOGIC (Restrict strictly to their own assets)
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.Asset.objects.none()

        # FIX: We filter the queryset so it ONLY contains assets assigned to this employee
        return qs.filter(employee=employee).order_by('asset_type', '-created_at')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base_qs = self.get_queryset()

        # 1. Top Stat Cards
        ctx['total_count'] = base_qs.count()
        ctx['available_count'] = base_qs.filter(employee__isnull=True).count()
        ctx['assigned_count'] = base_qs.filter(employee__isnull=False).count()
        ctx['repair_count'] = base_qs.filter(status='under_repair').count()

        # 2. PROPER GROUPING (Prevents Duplicates)
        # We manually group them here to be 100% safe from casing or ordering issues
        asset_types = base_qs.values_list('asset_type', flat=True).distinct()

        structured_data = []
        for a_type in sorted(list(set(asset_types))):  # Set ensures true uniqueness
            type_qs = base_qs.filter(asset_type=a_type)
            structured_data.append({
                'name': a_type or "General Assets",
                'assets': type_qs,
                'total': type_qs.count(),
                'available': type_qs.filter(employee__isnull=True).count(),
                'assigned': type_qs.filter(employee__isnull=False).count(),
            })

        ctx['structured_assets'] = structured_data
        return ctx


import json


import json

class AssetListView(CompanyFilterMixin, LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.Asset
    template_name = 'hrms/asset/asset_list.html'
    context_object_name = 'assets'
    active_group, active_item = 'asset', 'asset'
    paginate_by = 30

    def get_queryset(self):
        # Optimization: select_related category and employee to avoid N+1 queries
        qs = m.Asset.objects.select_related('employee', 'company', 'category')

        if is_hr_or_above(self.request.user):
            status = self.request.GET.get('status')
            if status:
                qs = qs.filter(status=status)
            return qs.order_by('category__name', '-created_at')

        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.Asset.objects.none()

        return qs.filter(employee=employee).order_by('category__name', '-created_at')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        base_qs = self.get_queryset()

        # 1. Stat Cards using Model Status Choices
        ctx['total_count'] = base_qs.count()
        ctx['available_count'] = base_qs.filter(status='available').count()
        ctx['assigned_count'] = base_qs.filter(status='assigned').count()
        ctx['repair_count'] = base_qs.filter(status='under_repair').count()

        # 2. CATEGORY-WISE GROUPING
        # We loop through existing categories to build the structure
        categories = m.AssetCategory.objects.all()
        structured_data = []

        for cat in categories:
            type_qs = base_qs.filter(category=cat)
            if type_qs.exists():
                structured_data.append({
                    'id': cat.id,
                    'name': cat.name,
                    'assets': type_qs,
                    'total': type_qs.count(),
                    'available': type_qs.filter(status='available').count(),
                    'assigned': type_qs.filter(status='assigned').count(),
                })

        # Catch assets that have no category assigned yet
        uncategorized = base_qs.filter(category__isnull=True)
        if uncategorized.exists():
            structured_data.append({
                'id': 'general',
                'name': "General Assets",
                'assets': uncategorized,
                'total': uncategorized.count(),
                'available': uncategorized.filter(status='available').count(),
                'assigned': uncategorized.filter(status='assigned').count(),
            })

        ctx['structured_assets'] = structured_data

        # 3. For the Create Category Modal to work on this page
        ctx['category_configs_json'] = json.dumps({
            str(c.id): c.required_fields for c in categories
        })
        return ctx
class AssetCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Asset
    form_class = f.AssetForm
    template_name = 'hrms/asset/asset_form.html'
    success_url = reverse_lazy('hrms:asset_list')
    active_group, active_item = 'asset', 'asset'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Create a dictionary mapping Category ID to its required_fields string
        # Example: { "1": "serial_number,device_password", "2": "sim,phone_number" }
        categories = m.AssetCategory.objects.all()
        config_map = {str(cat.id): cat.required_fields for cat in categories}

        context['category_configs_json'] = json.dumps(config_map)
        return context

    def form_valid(self, form):
        messages.success(self.request, 'Asset added.')
        return super().form_valid(form)


import json
from django.http import JsonResponse


def create_category_ajax(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            name = data.get('name')
            fields = data.get('fields', '')

            if not name:
                return JsonResponse({'success': False, 'error': 'Name is required'})

            category = m.AssetCategory.objects.create(
                name=name,
                required_fields=fields
            )
            return JsonResponse({
                'success': True,
                'id': category.id,
                'name': category.name,
                'fields': category.required_fields
            })
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False, 'error': 'Invalid request'}, status=400)

class AssetUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.Asset
    form_class = f.AssetForm
    template_name = 'hrms/asset/asset_form.html'
    success_url = reverse_lazy('hrms:asset_list')
    active_group, active_item = 'asset', 'asset'

    def form_valid(self, form):
        messages.success(self.request, 'Asset updated.')
        return super().form_valid(form)
class AssetDetailView(LoginRequiredMixin, SidebarContextMixin, DetailView):
    model = m.Asset
    template_name = 'hrms/asset/asset_detail.html'
    context_object_name = 'asset'
    active_group, active_item = 'asset', 'asset'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['history'] = self.object.history.all().select_related('employee')
        return context


class AssetDetailView(LoginRequiredMixin, SidebarContextMixin, DetailView):
    model = m.Asset
    template_name = 'hrms/asset/asset_detail.html'
    context_object_name = 'asset'
    active_group, active_item = 'asset', 'asset'

    def get_queryset(self):
        qs = super().get_queryset()

        # If user is HR/Admin, let them see everything
        if self.request.user.is_staff:
            return qs

        # If regular employee, only allow access if the asset is linked to their User
        return qs.filter(employee__user=self.request.user)

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        user = self.request.user

        # PERMISSION CHECK:
        # If user is NOT HR/Superadmin AND the asset is NOT assigned to them, block access.
        if not is_hr_or_above(user):
            employee = getattr(user, 'employee_profile', None)
            if obj.employee != employee:
                raise PermissionDenied("You do not have permission to view this asset's details.")
        return obj

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Pass a boolean to the template to handle sensitive UI elements
        ctx['is_hr'] = is_hr_or_above(self.request.user)
        # ctx['history'] = self.object.history.all().select_related('employee')
        ctx['history'] = self.object.history.all().order_by('-assigned_date')

        return ctx


class AssetReturnView(HRRequiredMixin, View):
    """Quick action: unassign an asset and mark it Available again."""
    def post(self, request, pk):
        asset = get_object_or_404(m.Asset, pk=pk)
        asset.employee = None
        asset.status = m.Asset.Status.AVAILABLE
        asset.assigned_on = None
        asset.save()
        messages.success(request, f'{asset.name} returned and marked available.')
        return redirect('hrms:asset_list')

# -------
# hrms/views.py

# hrms/views.py

class AssetReturnView(HRRequiredMixin, View):
    def post(self, request, pk):
        asset = get_object_or_404(m.Asset, pk=pk)

        if asset.employee:
            asset.employee = None  # Removing the user
            asset.status = m.Asset.Status.AVAILABLE
            asset.assigned_on = None
            asset.save()  # This triggers the history closing logic we wrote in models.py

            messages.success(request, "Asset returned successfully. History updated.")

        return redirect('hrms:asset_list')

    # Inside class AssetReturnView
    def post(self, request, pk):
        asset = get_object_or_404(m.Asset, pk=pk)

        if asset.employee:
            m.AssetAssignmentHistory.objects.filter(
                asset=asset,
                employee=asset.employee,
                is_still_using=True
            ).update(
                returned_date=timezone.now().date(),  # Sets the date
                is_still_using=False  # Closes the record
            )
            # ------------------------------

            asset.employee = None
            asset.status = m.Asset.Status.AVAILABLE
            asset.assigned_on = None
            asset.save()

            messages.success(request, "Asset returned successfully. History updated.")
        return redirect('hrms:asset_list')



# ===========================================================================
# PERFORMANCE REVIEWS
# ===========================================================================
class PerformanceReviewListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.PerformanceReview
    template_name = 'hrms/performance/performance_list.html'
    context_object_name = 'reviews'
    active_group, active_item = 'performance', 'performance'
    paginate_by = 30

    def get_queryset(self):
        qs = m.PerformanceReview.objects.select_related('employee', 'reviewer').order_by('-review_date')
        if is_hr_or_above(self.request.user):
            employee_id = self.request.GET.get('employee')
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
            return qs
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.PerformanceReview.objects.none()
        return qs.filter(employee=employee)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_hr'] = is_hr_or_above(self.request.user)
        if ctx['is_hr']:
            ctx['employees'] = m.Employee.objects.order_by('employee_code')
        return ctx


class PerformanceReviewCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.PerformanceReview
    form_class = f.PerformanceReviewForm
    template_name = 'hrms/performance/performance_form.html'
    success_url = reverse_lazy('hrms:performance_list')
    active_group, active_item = 'performance', 'performance'

    def form_valid(self, form):
        messages.success(self.request, 'Performance review created.')
        return super().form_valid(form)


class PerformanceReviewUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.PerformanceReview
    form_class = f.PerformanceReviewForm
    template_name = 'hrms/performance/performance_form.html'
    success_url = reverse_lazy('hrms:performance_list')
    active_group, active_item = 'performance', 'performance'

    def form_valid(self, form):
        messages.success(self.request, 'Performance review updated.')
        return super().form_valid(form)


class PerformanceReviewDetailView(EmployeeSelfOrHRMixin, SidebarContextMixin, DetailView):
    model = m.PerformanceReview
    template_name = 'hrms/performance/performance_detail.html'
    context_object_name = 'review'
    active_group, active_item = 'performance', 'performance'

    def get_object_employee(self, obj):
        return obj.employee


class PerformanceAcknowledgeView(LoginRequiredMixin, View):
    """The reviewed employee acknowledges their own submitted review."""
    def post(self, request, pk):
        review = get_object_or_404(m.PerformanceReview, pk=pk)
        employee = get_employee_profile(request.user)
        if not is_hr_or_above(request.user) and (employee is None or review.employee_id != employee.id):
            raise PermissionDenied("You can only acknowledge your own review.")
        if review.status != m.PerformanceReview.Status.SUBMITTED:
            messages.error(request, 'Only a submitted review can be acknowledged.')
        else:
            review.status = m.PerformanceReview.Status.ACKNOWLEDGED
            review.save(update_fields=['status', 'updated_at'])
            messages.success(request, 'Review acknowledged.')
        return redirect('hrms:performance_detail', pk=pk)