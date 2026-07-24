from datetime import date

from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils import timezone
from django.db.models import Q  # <--- Add this
from .forms import UnifiedCandidateForm
from . import models
from . import models as m
from django.utils import timezone # Make sure this is at the top
from .permissions import get_role, is_hr_or_above, get_employee_profile, ROLE_SUPERADMIN, ROLE_HR



class HRMSLoginView(LoginView):
    template_name = 'hrms/login.html'

    def get_success_url(self):
        return reverse_lazy('hrms:dashboard')

def set_active_company(request):
    if request.method == 'POST':
        company_id = request.POST.get('company_id')
        if company_id:
            request.session['active_company_id'] = company_id
    return redirect(request.META.get('HTTP_REFERER', 'hrms:dashboard'))


@login_required
def dashboard(request):
    user = request.user
    role = get_role(user)
    context = {'active_group': 'dashboard', 'active_item': 'dashboard'}

    if is_hr_or_above(user):
        today = date.today()
        context.update({
            'total_employees': m.Employee.objects.filter(status=m.Employee.Status.ACTIVE).count(),
            'present_today': m.AttendanceRecord.objects.filter(
                attendance_date=today, status=m.AttendanceRecord.Status.PRESENT).count(),
            'pending_leaves': m.LeaveApplication.objects.filter(
                status=m.LeaveApplication.Status.PENDING).count(),
            'open_jobs': m.JobPosting.objects.filter(is_active=True).count(),
            'recent_joiners': m.Employee.objects.order_by('-date_of_joining')[:5],
            'pending_leave_list': m.LeaveApplication.objects.filter(
                status=m.LeaveApplication.Status.PENDING).select_related('employee', 'leave_type')[:5],
        })
        template = 'hrms/dashboard_hr.html'
    else:
        employee = get_employee_profile(user)
        context.update({
            'employee': employee,
            'leave_balances': m.LeaveBalance.objects.filter(
                employee=employee, year=date.today().year) if employee else [],
            'recent_attendance': m.AttendanceRecord.objects.filter(
                employee=employee).order_by('-attendance_date')[:7] if employee else [],
            'recent_notices': m.CompanyNotice.objects.filter(
                company=employee.company, is_active=True).order_by('-notice_date')[:5] if employee else [],
        })
        template = 'hrms/dashboard_employee.html'

    return render(request, template, context)


@login_required
def dashboard(request):
    user = request.user
    role = get_role(user)
    today = date.today()
    context = {'active_group': 'dashboard', 'active_item': 'dashboard'}

    if is_hr_or_above(user):
        # --- 1. Get List of Available Companies for the dropdown ---
        if user.is_superuser:
            available_companies = m.Company.objects.all()
        else:
            employee_profile = get_employee_profile(user)
            if employee_profile:
                # HR sees their primary company + any assigned managed_companies
                available_companies = m.Company.objects.filter(
                    Q(id=employee_profile.company_id) |
                    Q(managed_companies__id=employee_profile.id)
                ).distinct()
            else:
                available_companies = m.Company.objects.none()

        # --- 2. Identify the "Active" Company filter from session ---
        active_company_id = request.session.get('active_company_id')

        # Default Filters (Global)
        emp_filter = Q(status=m.Employee.Status.ACTIVE)
        leave_filter = Q(status=m.LeaveApplication.Status.PENDING)
        job_filter = Q(is_active=True)
        attend_filter = Q(attendance_date=today)

        # --- 3. Apply Filtering based on selection or HR restrictions ---
        if active_company_id and active_company_id != "all":
            # Filter everything by the selected company
            emp_filter &= Q(company_id=active_company_id)
            leave_filter &= Q(employee__company_id=active_company_id)
            job_filter &= Q(company_id=active_company_id)
            attend_filter &= Q(employee__company_id=active_company_id)

        elif not user.is_superuser:
            # If HR hasn't selected a specific company, show data from ALL their assigned companies
            allowed_ids = list(available_companies.values_list('id', flat=True))
            emp_filter &= Q(company_id__in=allowed_ids)
            leave_filter &= Q(employee__company_id__in=allowed_ids)
            job_filter &= Q(company_id__in=allowed_ids)
            attend_filter &= Q(employee__company_id__in=allowed_ids)

        # --- 4. Fetch Filtered Data ---
        active_employees = m.Employee.objects.filter(emp_filter).select_related('designation')
        attendance_today = m.AttendanceRecord.objects.filter(attend_filter)

        # --- 5. Attendance Breakdown Logic ---
        # Get IDs of employees who have a record today
        present_ids = attendance_today.filter(
            status=m.AttendanceRecord.Status.PRESENT
        ).values_list('employee_id', flat=True)

        on_leave_ids = attendance_today.filter(
            status=m.AttendanceRecord.Status.ON_LEAVE
        ).values_list('employee_id', flat=True)

        # Map IDs to actual Employee Querysets for the template
        present_list = active_employees.filter(id__in=present_ids)
        on_leave_list = active_employees.filter(id__in=on_leave_ids)

        # Absent = Active employees who are NOT in the present list AND NOT in the leave list
        absent_list = active_employees.exclude(id__in=present_ids).exclude(id__in=on_leave_ids)

        context.update({
            'available_companies': available_companies,
            'active_company_id': active_company_id,
            'total_employees': active_employees.count(),
            'present_today': present_list.count(),
            'pending_leaves': m.LeaveApplication.objects.filter(leave_filter).count(),
            'open_jobs': m.JobPosting.objects.filter(job_filter).count(),

            'recent_joiners': active_employees.order_by('-date_of_joining')[:5],
            'pending_leave_list': m.LeaveApplication.objects.filter(leave_filter).select_related('employee',
                                                                                                 'leave_type')[:5],

            # Data for the Attendance Tabs in your template
            'present_employees': present_list,
            'absent_employees': absent_list,
            'on_leave_employees': on_leave_list,
        })
        template = 'hrms/dashboard_hr.html'

    else:
        # --- 6. Standard Employee Logic ---
        employee = get_employee_profile(user)
        if employee:
            context.update({
                'employee': employee,
                'leave_balances': m.LeaveBalance.objects.filter(employee=employee, year=today.year),
                'recent_attendance': m.AttendanceRecord.objects.filter(employee=employee).order_by('-attendance_date')[
                    :7],
                'recent_notices': m.CompanyNotice.objects.filter(company=employee.company, is_active=True).order_by(
                    '-notice_date')[:5],
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
class DepartmentListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Department
    template_name = 'hrms/org/department_list.html'
    context_object_name = 'departments'
    active_group, active_item = 'organisation', 'department'
    paginate_by = 25

    def get_queryset(self):
        return m.Department.objects.select_related('company').order_by('company__name', 'name')


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
    template_name = 'hrms/org/designation_list.html'
    context_object_name = 'designations'
    active_group, active_item = 'organisation', 'designation'
    paginate_by = 25

    def get_queryset(self):
        return m.Designation.objects.select_related('company', 'department').order_by('company__name', 'level')


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
class EmployeeListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.Employee
    template_name = 'hrms/employee/employee_list.html'
    context_object_name = 'employees'
    active_group, active_item = 'employee', 'employee_list'
    paginate_by = 25

    def get_queryset(self):
        qs = m.Employee.objects.select_related('company', 'department', 'designation')
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
        ctx['q'] = self.request.GET.get('q', '')
        ctx['status'] = self.request.GET.get('status', '')
        ctx['status_choices'] = m.Employee.Status.choices
        return ctx


class EmployeeDetailView(EmployeeSelfOrHRMixin, SidebarContextMixin, DetailView):
    model = m.Employee
    template_name = 'hrms/employee/employee_detail.html'
    context_object_name = 'employee'
    active_group, active_item = 'employee', 'employee_list'

    def get_object_employee(self, obj):
        return obj


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


class EmployeeCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Employee
    form_class = f.EmployeeForm
    template_name = 'hrms/employee/employee_form.html'
    active_group, active_item = 'employee', 'employee_add'

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
        return m.Company.objects.select_related('attendance_policy').order_by('name')


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


# ---------------------------------------------------------------------------
# Attendance Records — HR sees/manages all; employee sees + checks in/out on own
# ---------------------------------------------------------------------------
class AttendanceRecordListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.AttendanceRecord
    template_name = 'hrms/attendance/record_list.html'
    context_object_name = 'records'
    active_group, active_item = 'attendance', 'attendance_records'
    paginate_by = 30

    def get_queryset(self):
        qs = m.AttendanceRecord.objects.select_related('employee').order_by('-attendance_date')
        employee_id = self.request.GET.get('employee')
        status = self.request.GET.get('status')
        d_from = self.request.GET.get('from')
        d_to = self.request.GET.get('to')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        if status:
            qs = qs.filter(status=status)
        if d_from:
            qs = qs.filter(attendance_date__gte=d_from)
        if d_to:
            qs = qs.filter(attendance_date__lte=d_to)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['employees'] = m.Employee.objects.order_by('employee_code')
        ctx['status_choices'] = m.AttendanceRecord.Status.choices
        ctx['filters'] = self.request.GET
        return ctx


from django.views.generic import ListView
from django.utils import timezone
from datetime import timedelta, datetime
import collections


class AttendanceRecordListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.AttendanceRecord
    template_name = 'hrms/attendance/record_list.html'
    context_object_name = 'records'
    active_group, active_item = 'attendance', 'attendance_records'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        # 1. Handle Date Range (Default to current month if not filtered)
        d_from = self.request.GET.get('from')
        d_to = self.request.GET.get('to')

        if d_from:
            start_date = datetime.strptime(d_from, '%Y-%m-%d').date()
        else:
            start_date = timezone.now().date().replace(day=1)

        if d_to:
            end_date = datetime.strptime(d_to, '%Y-%m-%d').date()
        else:
            # End of current month
            next_month = start_date.replace(day=28) + timedelta(days=4)
            end_date = next_month - timedelta(days=next_month.day)

        # Generate list of dates for table headers
        date_list = []
        curr = start_date
        while curr <= end_date:
            date_list.append(curr)
            curr += timedelta(days=1)

        # 2. Filter Employees
        employee_id = self.request.GET.get('employee')
        employees = m.Employee.objects.all().order_by('employee_code')
        if employee_id:
            employees = employees.filter(id=employee_id)

        # 3. Get Records and Map to Employee & Date
        qs = m.AttendanceRecord.objects.filter(
            attendance_date__range=[start_date, end_date]
        ).select_related('employee')

        # Create a nested lookup: attendance_data[emp_id][date] = record
        attendance_matrix = collections.defaultdict(dict)
        for rec in qs:
            attendance_matrix[rec.employee_id][rec.attendance_date] = rec

        ctx.update({
            'employees': employees,
            'date_list': date_list,
            'attendance_matrix': attendance_matrix,
            'filters': self.request.GET,
            'start_date': start_date,
            'end_date': end_date,
            'status_choices': m.AttendanceRecord.Status.choices
        })
        return ctx

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
class HolidayListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.Holiday
    template_name = 'hrms/attendance/holiday_list.html'
    context_object_name = 'holidays'
    active_group, active_item = 'attendance', 'holiday'

    def get_queryset(self):
        return m.Holiday.objects.select_related('company').order_by('date')


class HolidayCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Holiday
    form_class = f.HolidayForm
    template_name = 'hrms/attendance/holiday_form.html'
    success_url = reverse_lazy('hrms:holiday_list')
    active_group, active_item = 'attendance', 'holiday'

    def form_valid(self, form):
        messages.success(self.request, 'Holiday added.')
        return super().form_valid(form)


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
        return m.LeaveType.objects.select_related('company').order_by('company__name', 'name')


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
        if is_hr_or_above(self.request.user):
            ctx['employees'] = m.Employee.objects.order_by('employee_code')
            ctx['selected_employee'] = self.request.GET.get('employee', '')
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
    model = m.LeaveBalance
    form_class = f.LeaveBalanceForm
    template_name = 'hrms/leave/leave_balance_form.html'
    success_url = reverse_lazy('hrms:leave_balance')
    active_group, active_item = 'leave', 'leave_balance'

    def form_valid(self, form):
        messages.success(self.request, 'Leave balance updated.')
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Leave Applications — employee applies for their own; HR sees all + approves
# ---------------------------------------------------------------------------
class LeaveApplicationListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.LeaveApplication
    template_name = 'hrms/leave/leave_application_list.html'
    context_object_name = 'applications'
    active_group, active_item = 'leave', 'my_leave'
    paginate_by = 30

    def get_queryset(self):
        qs = m.LeaveApplication.objects.select_related('employee', 'leave_type')
        if is_hr_or_above(self.request.user):
            status = self.request.GET.get('status')
            employee_id = self.request.GET.get('employee')
            if status:
                qs = qs.filter(status=status)
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
            return qs
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.LeaveApplication.objects.none()
        return qs.filter(employee=employee)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_hr'] = is_hr_or_above(self.request.user)
        if ctx['is_hr']:
            ctx['employees'] = m.Employee.objects.order_by('employee_code')
            ctx['status_choices'] = m.LeaveApplication.Status.choices
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
            employee = form.cleaned_data['employee'] if is_hr_or_above(request.user) else get_employee_profile(request.user)
            if employee is None:
                messages.error(request, "Your login isn't linked to an employee record.")
                return redirect('hrms:dashboard')
            try:
                lv.apply_leave(
                    employee=employee,
                    leave_type=form.cleaned_data['leave_type'],
                    start_date=form.cleaned_data['start_date'],
                    end_date=form.cleaned_data['end_date'],
                    reason=form.cleaned_data.get('reason', ''),
                )
                messages.success(request, 'Leave application submitted.')
                return redirect('hrms:my_leave')
            except lv.LeaveError as e:
                form.add_error(None, str(e))
        return render(request, self.template_name, {
            'form': form, 'active_group': self.active_group, 'active_item': self.active_item,
        })


class LeaveApproveView(HRRequiredMixin, View):
    """Admin/HR only — enforced by HRRequiredMixin, matching the prompt's
    'Only Admins should be able to change the status to Approved.'"""
    def post(self, request, pk):
        application = get_object_or_404(m.LeaveApplication, pk=pk)
        approver = get_employee_profile(request.user)
        try:
            lv.approve_leave(application, approver_employee=approver)
            messages.success(request, f'Leave approved for {application.employee.full_name}.')
        except lv.LeaveError as e:
            messages.error(request, str(e))
        return redirect('hrms:my_leave')


class LeaveRejectView(HRRequiredMixin, View):
    def post(self, request, pk):
        application = get_object_or_404(m.LeaveApplication, pk=pk)
        approver = get_employee_profile(request.user)
        reason = request.POST.get('rejection_reason', '')
        try:
            lv.reject_leave(application, approver_employee=approver, reason=reason)
            messages.success(request, f'Leave rejected for {application.employee.full_name}.')
        except lv.LeaveError as e:
            messages.error(request, str(e))
        return redirect('hrms:my_leave')


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


# ===========================================================================
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
class EmployeeSalaryListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.EmployeeSalary
    template_name = 'hrms/payroll/employee_salary_list.html'
    context_object_name = 'salaries'
    active_group, active_item = 'payroll', 'employee_salary'
    paginate_by = 30

    def get_queryset(self):
        qs = m.EmployeeSalary.objects.select_related('employee', 'structure').order_by('-effective_from')
        employee_id = self.request.GET.get('employee')
        if employee_id:
            qs = qs.filter(employee_id=employee_id)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['employees'] = m.Employee.objects.order_by('employee_code')
        ctx['selected_employee'] = self.request.GET.get('employee', '')
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

import calendar
from datetime import date
from django.views.generic import DetailView
# Assuming your models are in the same directory:
from .models import Employee, AttendanceRecord, LeaveApplication


class EmployeePunchReportView(HRRequiredMixin, DetailView):
    model = Employee  # Tell DetailView which model to use
    template_name = 'hrms/attendance/punch_report.html'
    pk_url_kwarg = 'emp_id'  # If your URL uses <int:emp_id>

    def get_context_data(self, **kwargs):
        # 1. Get the standard context (this includes self.object)
        context = super().get_context_data(**kwargs)

        emp = self.object  # DetailView already fetched the employee for you
        month = int(self.kwargs['month'])
        year = int(self.kwargs['year'])

        days_in_month = calendar.monthrange(year, month)[1]
        report = []

        for d in range(1, days_in_month + 1):
            dt = date(year, month, d)
            punch = AttendanceRecord.objects.filter(employee=emp, attendance_date=dt).first()
            leave = LeaveApplication.objects.filter(
                employee=emp,
                start_date__lte=dt,
                end_date__gte=dt,
                status='approved'
            ).first()

            report.append({
                'date': dt,
                'punch': punch,
                'leave': leave,
                'is_weekend': dt.weekday() == 6  # 6 is Sunday
            })

        # 2. Add your custom data to context
        context['report'] = report
        context['selected_month'] = month
        context['selected_year'] = year

        return context
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
    def process_payroll_logic(company, year, month):
        # Get all active employees for this company
        employees = Employee.objects.filter(company=company, status='active')
        total_days_in_month = calendar.monthrange(year, month)[1]

        for emp in employees:
            salary = emp.salaries.filter(is_active=True).first()
            if not salary: continue

            daily_wage = salary.ctc_annual / 12 / total_days_in_month

            # Initialize counters
            stats = {
                'P': 0, 'HD': 0, 'LWP': 0, 'PaidLeave': 0,
                'Holiday': 0, 'Sunday': 0, 'ExtraWork': 0, 'Penalty': 0
            }

            for day in range(1, total_days_in_month + 1):
                curr_date = date(year, month, day)
                is_holiday = m.Holiday.objects.filter(company=company, date=curr_date).exists()
                is_sunday = curr_date.weekday() == 6
                att = AttendanceRecord.objects.filter(employee=emp, attendance_date=curr_date).first()

                # Rule 1: Holiday/Sunday Paid Logic
                if is_holiday or is_sunday:
                    stats['Holiday' if is_holiday else 'Sunday'] += 1
                    if att and att.check_in: stats['ExtraWork'] += 1
                    continue

                # Rule 2: Attendance & Grace Period
                if att:
                    if att.status == 'present':
                        # Check Late Grace (More than 3 times late = Half Day)
                        grace = m.GraceUsageTracker.objects.filter(employee=emp, month=month, year=year).first()
                        if grace and grace.usage_count > 3 and att.late_minutes > 0:
                            stats['HD'] += 1
                            stats['Penalty'] += (daily_wage * 0.5)  # Deducting half day
                        else:
                            stats['P'] += 1
                    elif att.status == 'half_day':
                        stats['HD'] += 1
                else:
                    # Rule 3: Leave Logic (Intern vs Full-Time)
                    leave = m.LeaveApplication.objects.filter(employee=emp, start_date__lte=curr_date,
                                                            end_date__gte=curr_date, status='approved').first()
                    if emp.employment_type == 'intern':
                        stats['LWP'] += 1  # Interns always get money deducted
                    else:
                        if leave:
                            # Check if balance exists
                            bal = m.LeaveBalance.objects.filter(employee=emp, leave_type=leave.leave_type,
                                                              year=year).first()
                            if bal and bal.available > 0:
                                stats['PaidLeave'] += 1
                            else:
                                stats['LWP'] += 1
                        else:
                            stats['LWP'] += 1

            # Calculate Final Money
            total_paid_days = stats['P'] + (stats['HD'] * 0.5) + stats['PaidLeave'] + stats['Holiday'] + stats['Sunday']
            earned_wages = total_paid_days * daily_wage
            overtime_pay = stats['ExtraWork'] * daily_wage

            # Loan
            loan = emp.loans.filter(is_active=True).first()
            deduction = loan.monthly_installment if loan and loan.remaining_balance > 0 else 0

            # Save to PaySlip model...

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
        ctx['interviews'] = self.object.interviews.select_related('interviewer').order_by('scheduled_on')
        ctx['offer'] = getattr(self.object, 'offer_letter', None)
        ctx['status_choices'] = m.Application.Status.choices
        ctx['audit_logs'] = self.object.audit_logs.select_related('performed_by')[:20]
        return ctx


class ApplicationStatusView(HRRequiredMixin, View):
    """Quick status-change buttons on the pipeline (Shortlist / Reject)."""
    def post(self, request, pk):
        application = get_object_or_404(m.Application, pk=pk)
        action = request.POST.get('status')
        try:
            if action == 'shortlisted':
                hire.shortlist_application(application, request.user)
                messages.success(request, 'Application shortlisted.')
            elif action == 'rejected':
                reason = request.POST.get('reason', '')
                hire.reject_application(application, request.user, reason=reason)
                messages.warning(request, 'Application rejected.')
            else:
                messages.error(request, 'Unknown action.')
        except hire.HiringError as e:
            messages.error(request, str(e))
        return redirect('hrms:application_detail', pk=pk)


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
        form = f.InterviewForm(request.POST)
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


# ===========================================================================
# ASSET MANAGEMENT
# ===========================================================================
class AssetListView(LoginRequiredMixin, SidebarContextMixin, ListView):
    model = m.Asset
    template_name = 'hrms/asset/asset_list.html'
    context_object_name = 'assets'
    active_group, active_item = 'asset', 'asset'
    paginate_by = 30

    def get_queryset(self):
        qs = m.Asset.objects.select_related('employee', 'company').order_by('-created_at')
        if is_hr_or_above(self.request.user):
            status = self.request.GET.get('status')
            if status:
                qs = qs.filter(status=status)
            return qs
        employee = get_employee_profile(self.request.user)
        if employee is None:
            return m.Asset.objects.none()
        return qs.filter(employee=employee)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['is_hr'] = is_hr_or_above(self.request.user)
        if ctx['is_hr']:
            ctx['status_choices'] = m.Asset.Status.choices
        return ctx


class AssetCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.Asset
    form_class = f.AssetForm
    template_name = 'hrms/asset/asset_form.html'
    success_url = reverse_lazy('hrms:asset_list')
    active_group, active_item = 'asset', 'asset'

    def form_valid(self, form):
        messages.success(self.request, 'Asset added.')
        return super().form_valid(form)


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
        ctx['history'] = self.object.history.all().select_related('employee')
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
