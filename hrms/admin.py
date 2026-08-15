from django.contrib import admin

from . import models as m

#add holiday calendar in admin file
@admin.register(m.Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'created_at')
    search_fields = ('name', 'email')


@admin.register(m.Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'company')
    list_filter = ('company',)
    search_fields = ('name',)


@admin.register(m.Designation)
class DesignationAdmin(admin.ModelAdmin):
    list_display = ('title', 'department', 'company', 'level')
    list_filter = ('company', 'department')


from django.contrib import admin
from .models import Employee


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    # Use filter_horizontal to make the selection much prettier than the box in your screenshot
    filter_horizontal = ('managed_companies',)

    # List all fields you want to show by default
    # Or just use the logic below to exclude the field dynamically

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))

        # If we are editing an existing employee (obj is not None)
        if obj and obj.user:
            # Check if the employee being edited is NOT an HR/Accountant and NOT a Superuser
            is_hr = getattr(obj.user, 'is_accountant', False)
            is_admin = obj.user.is_superuser

            if not (is_hr or is_admin):
                # Remove 'managed_companies' if the person is just a regular employee
                if 'managed_companies' in fields:
                    fields.remove('managed_companies')

        return fields


admin.site.register(m.EmployeeBankDetail)
admin.site.register(m.EmployeeDocument)
admin.site.register(m.EmployeeNotice)

# -------hiring----------
admin.site.register(m.RecruitmentStage)
admin.site.register(m.JobPosting)
admin.site.register(m.JobPipeline)
admin.site.register(m.Candidate)
admin.site.register(m.Application)
admin.site.register(m.RecruitmentAuditLog)
admin.site.register(m.Interview)
admin.site.register(m.OfferLetter)
admin.site.register(m.OfferTemplate)


# -------------------
admin.site.register(m.AttendancePolicy)
@admin.register(m.AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ('employee', 'attendance_date', 'check_in', 'check_out', 'status', 'total_hours')
    list_filter = ('status', 'attendance_date')
    search_fields = ('employee__employee_code', 'employee__first_name')


admin.site.register(m.GraceUsageTracker)
admin.site.register(m.Holiday)

admin.site.register(m.LeaveType)
admin.site.register(m.LeaveBalance)
admin.site.register(m.EmployeeLeaveBalance)



@admin.register(m.LeaveApplication)
class LeaveApplicationAdmin(admin.ModelAdmin):
    list_display = ('employee', 'leave_type', 'start_date', 'end_date', 'status')
    list_filter = ('status', 'leave_type')
admin.site.register(m.EmployeeLeaveBalanceLive)


admin.site.register(m.SalaryStructure)
admin.site.register(m.SalaryComponent)
admin.site.register(m.EmployeeSalary)
admin.site.register(m.PayrollRun)
admin.site.register(m.PaySlip)
admin.site.register(m.LoanAdvance)
admin.site.register(m.PayrollExtra)

admin.site.register(m.Policy)
admin.site.register(m.PolicyAcknowledgement)
admin.site.register(m.CompanyNotice)
admin.site.register(m.NoticeRead)

admin.site.register(m.Asset)
admin.site.register(m.PerformanceReview)


