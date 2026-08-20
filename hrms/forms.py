from django import forms
from django.forms import inlineformset_factory

from . import models as m


class BootstrapModelForm(forms.ModelForm):
    """Adds `form-control` / `form-select` / `form-check-input` to every
    field automatically so templates don't need per-field widget classes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput,)):
                widget.attrs.setdefault('class', 'form-check-input')
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault('class', 'form-select')
            elif isinstance(widget, forms.ClearableFileInput):
                widget.attrs.setdefault('class', 'form-control')
            else:
                widget.attrs.setdefault('class', 'form-control')
            if isinstance(widget, (forms.Textarea,)):
                widget.attrs.setdefault('rows', 3)


# ---------------------------------------------------------------------------
# Organisation Structure
# ---------------------------------------------------------------------------
class CompanyForm(BootstrapModelForm):
    class Meta:
        model = m.Company
        fields = [
            'name', 'logo', 'address', 'cin', 'pan', 'gstin', 'website', 'email', 'phone',
            'office_start_time', 'office_end_time', 'grace_minutes', 'grace_allowed_count',
        ]
        widgets = {
            'office_start_time': forms.TimeInput(attrs={'type': 'time'}),
            'office_end_time': forms.TimeInput(attrs={'type': 'time'}),
            'address': forms.Textarea(attrs={'rows': 2}),
        }


class DepartmentForm(BootstrapModelForm):
    class Meta:
        model = m.Department
        fields = ['company', 'name', 'description']


class DesignationForm(BootstrapModelForm):
    class Meta:
        model = m.Designation
        fields = ['company', 'department', 'title', 'level']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Narrow the department dropdown to the chosen company where possible
        company_id = None
        if self.data.get('company'):
            company_id = self.data.get('company')
        elif self.instance and self.instance.pk:
            company_id = self.instance.company_id
        if company_id:
            self.fields['department'].queryset = m.Department.objects.filter(company_id=company_id)


# ---------------------------------------------------------------------------
# Employee Management
# ---------------------------------------------------------------------------
class EmployeeForm(BootstrapModelForm):
    class Meta:
        model = m.Employee
        fields = [
            'company', 'department', 'designation', 'employee_code', 'first_name', 'last_name',
            'email', 'phone', 'gender', 'date_of_birth','father_name', 'mother_name', 'address',
            'emergency_contact', 'date_of_joining', 'date_of_confirmation','employment_type', 'status',
        ]
        widgets = {
            'date_of_birth': forms.DateInput(attrs={'type': 'date'}),
            'date_of_joining': forms.DateInput(attrs={'type': 'date'}),
            'date_of_confirmation': forms.DateInput(attrs={'type': 'date'}),
            # Use Textarea for address and emergency contact with custom row heights
            'address': forms.Textarea(attrs={'rows': 3, 'placeholder': 'Enter full address...'}),
            'emergency_contact': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Name, Relationship, Phone number'}),

        }

    # def __init__(self, *args, **kwargs):
    #     super().__init__(*args, **kwargs)
    #     # Make it read-only so users know it's handled by the system
    #     if 'employee_code' in self.fields:
    #         self.fields['employee_code'].widget.attrs['readonly'] = True
    #         self.fields['employee_code'].required = False
    #         self.fields['employee_code'].initial = "Auto-generated"
    #

class EmployeeBankDetailForm(BootstrapModelForm):
    class Meta:
        model = m.EmployeeBankDetail
        fields = ['account_holder', 'account_number', 'ifsc_code', 'bank_name', 'branch_name',
                  'account_type', 'is_verified']


class EmployeeDocumentForm(BootstrapModelForm):
    class Meta:
        model = m.EmployeeDocument
        fields = ['document_type', 'file', 'description', 'issued_on', 'expiry_on']
        widgets = {
            'issued_on': forms.DateInput(attrs={'type': 'date'}),
            'expiry_on': forms.DateInput(attrs={'type': 'date'}),
        }


class EmployeeNoticeForm(BootstrapModelForm):
    class Meta:
        model = m.EmployeeNotice
        fields = ['title', 'description', 'notice_date', 'is_active']
        widgets = {
            'notice_date': forms.DateInput(attrs={'type': 'date'}),
        }


# ---------------------------------------------------------------------------
# Attendance & Grace Policy
# ---------------------------------------------------------------------------
class AttendancePolicyForm(BootstrapModelForm):
    class Meta:
        model = m.AttendancePolicy
        fields = [
            'company','office_start_time', 'office_end_time', 'grace_minutes', 'grace_allowed_count','effective_from',
            'half_day_threshold_hours', 'full_day_threshold_hours', 'overtime_threshold_hours',
        ]
        widgets = {
            # HTML5 date and time pickers for better UI
            'effective_from': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'office_start_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'office_end_time': forms.TimeInput(attrs={'class': 'form-control', 'type': 'time'}),
            'company': forms.Select(attrs={'class': 'form-select'}),
            'grace_minutes': forms.NumberInput(attrs={'class': 'form-control'}),
            'grace_allowed_count': forms.NumberInput(attrs={'class': 'form-control'}),
        }


class AttendanceRecordForm(BootstrapModelForm):
    """For HR manual entry / correction of an attendance record."""
    class Meta:
        model = m.AttendanceRecord
        fields = ['employee', 'attendance_date', 'check_in', 'check_out', 'status', 'remarks']
        widgets = {
            'attendance_date': forms.DateInput(attrs={'type': 'date'}),
            'check_in': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'check_out': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        }


class HolidayCalendarForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        # We pass the user from the view to the form
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if user:
            if user.is_superuser:
                # Superadmins see all companies
                self.fields['company'].queryset = m.Company.objects.all()
            else:
                # HR Managers only see companies they are assigned to manage
                self.fields['company'].queryset = user.employee_profile.managed_companies.all()

    class Meta:
        model = m.HolidayCalendar
        fields = ['company', 'name', 'is_default']  # Added 'company'
        widgets = {
            'company': forms.Select(attrs={'class': 'form-select'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. North India Office'}),
            'is_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

class HolidayForm(BootstrapModelForm):
    class Meta:
        model = m.Holiday
        fields = ['calendar', 'date', 'name', 'type', 'description']
        widgets = {
            'date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'type': forms.Select(attrs={'class': 'form-select'}),
        }

class HolidayCalendarForm(forms.ModelForm):
    class Meta:
        model = m.HolidayCalendar
        # Include all necessary fields
        fields = ['company', 'name', 'is_default', 'description']
        widgets = {
            'company': forms.Select(attrs={'class': 'form-select rounded-3'}),
            'name': forms.TextInput(
                attrs={'class': 'form-control rounded-3', 'placeholder': 'e.g. North India Office'}),
            'is_default': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'description': forms.Textarea(attrs={'class': 'form-control rounded-3', 'rows': 2}),
        }

    def __init__(self, *args, **kwargs):
        # Pop user to filter companies
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if user:
            if user.is_superuser:
                self.fields['company'].queryset = m.Company.objects.all()
            else:
                # Only show companies this HR is assigned to
                self.fields['company'].queryset = user.employee_profile.managed_companies.all()

        # Make company mandatory for creation
        self.fields['company'].empty_label = "--- Select Company ---"
        self.fields['company'].required = True


class BulkHolidayForm(forms.ModelForm):
    # This is a virtual field not in the DB, used for bulk selection
    target_calendars = forms.ModelMultipleChoiceField(
        queryset=m.HolidayCalendar.objects.none(),
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'list-unstyled'}),
        label="Apply to these Templates"
    )

    class Meta:
        model = m.Holiday
        fields = ['name', 'date', 'type', 'description']
        widgets = {
            'date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Independence Day'}),
            'type': forms.Select(attrs={'class': 'form-select'}),
            'description': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # Filter the checklists so HR only sees calendars for THEIR companies
        if user:
            if user.is_superuser:
                self.fields['target_calendars'].queryset = m.HolidayCalendar.objects.all().select_related('company')
            else:
                managed_ids = user.employee_profile.managed_companies.values_list('id', flat=True)
                self.fields['target_calendars'].queryset = m.HolidayCalendar.objects.filter(
                    company_id__in=managed_ids
                ).select_related('company')

        # Display the company name next to the calendar name in the checklist
        self.fields['target_calendars'].label_from_instance = lambda obj: f"{obj.name} ({obj.company.name})"

# ---------------------------------------------------------------------------
# Leave Management
# ---------------------------------------------------------------------------
class LeaveTypeForm(BootstrapModelForm):
    class Meta:
        model = m.LeaveType
        fields = [
            'company', 'name', 'code', 'days_per_year', 'is_paid', 'is_carry_forward',
            'max_carry_forward', 'max_consecutive_days', 'requires_approval', 'requires_document',
            'applicable_gender', 'description',
        ]


class LeaveBalanceForm(BootstrapModelForm):
    """HR allocation / correction screen — used, pending included so HR can
    fix a balance manually if something gets out of sync."""
    class Meta:
        model = m.LeaveBalance
        fields = ['employee', 'leave_type', 'year', 'allocated', 'used', 'pending', 'carried_forward']


class LeaveApplicationForm(BootstrapModelForm):
    """Self-service: the employee applying is fixed by the view, not a form field."""
    class Meta:
        model = m.LeaveApplication
        fields = ['leave_type', 'day_type','start_date', 'end_date', 'reason']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
            'reason': forms.Textarea(attrs={'rows': 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        day_type = cleaned_data.get('day_type')
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if day_type == 'half':
            # For half day, end date must be same as start date
            cleaned_data['end_date'] = start_date
        elif start_date and end_date and end_date < start_date:
            raise forms.ValidationError('End date cannot be before start date.')

        return cleaned_data


class LeaveApplicationHRForm(LeaveApplicationForm):
    """Same as above, but HR picks which employee it's for."""
    class Meta(LeaveApplicationForm.Meta):
        fields = ['employee'] + LeaveApplicationForm.Meta.fields


class LeaveApplicationForm(forms.ModelForm):
    class Meta:
        model = m.LeaveApplication
        fields = ['employee', 'leave_type', 'day_type', 'start_date', 'end_date', 'reason']

    def clean(self):
        cleaned_data = super().clean()
        day_type = cleaned_data.get('day_type')
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if day_type == 'half':
            # 1. Force total_days to be 0.5
            self.instance.total_days = 0.5
            # 2. For half day, end_date must be the same as start_date
            cleaned_data['end_date'] = start_date
        else:
            # 3. Standard calculation for Full Days
            if start_date and end_date:
                delta = (end_date - start_date).days + 1
                self.instance.total_days = delta

        return cleaned_data

class LeaveRejectForm(forms.Form):
    rejection_reason = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'form-control',
                                      'placeholder': 'Reason for rejection (optional)'}),
        required=False,
    )

from django import forms
from .models import EmployeeLeaveBalance

class LeaveBalanceForm(forms.ModelForm):
    class Meta:
        model = EmployeeLeaveBalance
        fields = [
            'bereavement_leave', 'menstrual_leave', 'sick_leave',
            'earned_leave', 'casual_leave', 'comp_off'
        ]
        # widgets = {
        #     'status': forms.Select(choices=[('Active', 'Active'), ('Left', 'Left'), ('Terminated', 'Terminated')]),
        # }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Add Bootstrap classes to all fields
        for field in self.fields.values():
            field.widget.attrs.update({'class': 'form-control form-control-sm'})
            # Ensure Django doesn't block the save if a field is momentarily empty
            field.required = False


# ---------------------------------------------------------------------------
# Payroll & Salary Structure
# ---------------------------------------------------------------------------
class SalaryStructureForm(BootstrapModelForm):
    class Meta:
        model = m.SalaryStructure
        fields = ['company', 'name', 'description']




class SalaryComponentForm(BootstrapModelForm):
    class Meta:
        model = m.SalaryComponent
        fields = ['name', 'component_type', 'is_earning', 'is_deduction', 'is_taxable', 'display_order']


class EmployeeSalaryForm(BootstrapModelForm):
    class Meta:
        model = m.EmployeeSalary
        fields = [
            'employee', 'structure', 'ctc_annual', 'basic', 'hra', 'special_allowance',
            'pf_employee', 'pf_employer', 'esic_employee', 'esic_employer', 'professional_tax', 'tds',
            'effective_from', 'effective_to', 'is_active',
        ]
        widgets = {
            'effective_from': forms.DateInput(attrs={'type': 'date'}),
            'effective_to': forms.DateInput(attrs={'type': 'date'}),
        }


class LoanAdvanceForm(BootstrapModelForm):
    class Meta:
        model = m.LoanAdvance
        fields = ['employee', 'description', 'principal_amount', 'monthly_installment',
                  'remaining_balance', 'start_date', 'is_active']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
        }


class PayrollExtraForm(BootstrapModelForm):
    class Meta:
        model = m.PayrollExtra
        fields = ['employee', 'label', 'amount']


MONTH_CHOICES = [(i, m_name) for i, m_name in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June',
     'July', 'August', 'September', 'October', 'November', 'December'], start=1)]


class PayrollProcessForm(forms.Form):
    company = forms.ModelChoiceField(queryset=m.Company.objects.all())
    month = forms.ChoiceField(choices=MONTH_CHOICES)
    year = forms.IntegerField(min_value=2000, max_value=2100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            widget.attrs.setdefault('class', 'form-select' if isinstance(widget, forms.Select) else 'form-control')


# ---------------------------------------------------------------------------
# Hiring / Recruitment Pipeline
# ---------------------------------------------------------------------------
class JobPostingForm(BootstrapModelForm):
    class Meta:
        model = m.JobPosting
        fields = ['company', 'department', 'designation', 'title', 'job_type', 'location',
                  'description', 'requirements', 'is_active', 'closing_date']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 4}),
            'requirements': forms.Textarea(attrs={'rows': 4}),
            'closing_date': forms.DateInput(attrs={'type': 'date'}),
        }


class CandidateForm(BootstrapModelForm):
    class Meta:
        model = m.Candidate
        fields = ['first_name', 'last_name', 'email', 'phone', 'resume', 'current_company', 'experience_years']

class ApplicationForm(BootstrapModelForm):
    class Meta:
        model = m.Application
        fields = ['candidate', 'job_posting', 'status', 'source', 'cover_letter']
        widgets = {
            'cover_letter': forms.Textarea(attrs={'rows': 3}),
        }



# -------this is mixture of two forms ----

class UnifiedCandidateForm(forms.ModelForm):
    # Add application-specific fields manually to the candidate form
    job_posting = forms.ModelChoiceField(
        queryset=m.JobPosting.objects.filter(is_active=True),
        required=True,
        label="Applying For"
    )
    status = forms.ChoiceField(choices=m.Application.Status.choices, initial='applied')
    source = forms.CharField(required=False)
    cover_letter = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)

    class Meta:
        model = m.Candidate
        fields = ['first_name', 'last_name', 'email', 'phone', 'resume', 'current_company', 'experience_years']


# --- UPDATE: InterviewForm — pipeline stage + scorecard --------------------
# --- UPDATE: ApplicationForm — add the new ATS metadata fields ------------
class ApplicationForm(forms.ModelForm):
    class Meta:
        model = m.Application
        fields = [
            'candidate', 'job_posting', 'status', 'source', 'cover_letter',
            'expected_ctc', 'current_ctc', 'notice_period', 'current_stage',
        ]
        widgets = {
            'cover_letter': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        job = None
        if self.instance and self.instance.pk:
            job = self.instance.job_posting
        elif 'job_posting' in self.data:
            job = m.JobPosting.objects.filter(pk=self.data.get('job_posting')).first()
        # Only offer stages that are actually in this job's pipeline
        if job:
            self.fields['current_stage'].queryset = m.RecruitmentStage.objects.filter(
                job_pipelines__job=job).distinct()
        else:
            self.fields['current_stage'].queryset = m.RecruitmentStage.objects.none()
        self.fields['current_stage'].required = False

SCORECARD_CATEGORIES = ['technical', 'communication', 'culture']


class InterviewForm(forms.ModelForm):
    # Individual 1-5 rating widgets that get packed into the `scorecard` JSON
    # field on save — friendlier for HR than hand-typing JSON.
    score_technical = forms.IntegerField(min_value=1, max_value=5, required=False, label='Technical (1-5)')
    score_communication = forms.IntegerField(min_value=1, max_value=5, required=False, label='Communication (1-5)')
    score_culture = forms.IntegerField(min_value=1, max_value=5, required=False, label='Culture Fit (1-5)')

    class Meta:
        model = m.Interview
        fields = [
            'pipeline_stage', 'interview_round', 'scheduled_on', 'interviewer',
            'mode', 'feedback', 'status',
        ]
        widgets = {
            'scheduled_on': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'feedback': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.application = application or getattr(self.instance, 'application', None)
        if self.application:
            self.fields['pipeline_stage'].queryset = m.JobPipeline.objects.filter(
                job=self.application.job_posting).select_related('stage').order_by('order')
        self.fields['pipeline_stage'].required = False
        self.fields['interview_round'].required = False

        if self.instance and self.instance.pk and self.instance.scorecard:
            self.fields['score_technical'].initial = self.instance.scorecard.get('technical')
            self.fields['score_communication'].initial = self.instance.scorecard.get('communication')
            self.fields['score_culture'].initial = self.instance.scorecard.get('culture')

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('pipeline_stage') and not cleaned.get('interview_round'):
            raise forms.ValidationError('Pick a pipeline stage or enter a free-text interview round.')
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        scorecard = {}
        for key in SCORECARD_CATEGORIES:
            value = self.cleaned_data.get(f'score_{key}')
            if value is not None:
                scorecard[key] = value
        instance.scorecard = scorecard
        if commit:
            instance.save()
        return instance



# --- NEW: RecruitmentStage --------------------------------------------------
class RecruitmentStageForm(forms.ModelForm):
    class Meta:
        model = m.RecruitmentStage
        fields = ['name', 'description', 'is_default']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}


# --- NEW: JobPipeline — managed as an inline formset against one JobPosting
JobPipelineFormSet = inlineformset_factory(
    m.JobPosting, m.JobPipeline,
    fields=['stage', 'order'],
    extra=1, can_delete=True,
)


# --- NEW: OfferTemplate -----------------------------------------------------
class OfferTemplateForm(forms.ModelForm):
    class Meta:
        model = m.OfferTemplate
        fields = ['name', 'body_html', 'is_active']
        widgets = {'body_html': forms.Textarea(attrs={'rows': 14, 'class': 'font-monospace'})}


# --- UPDATE: OfferLetterForm — template picker + editable content ---------
class OfferLetterForm(forms.ModelForm):
    class Meta:
        model = m.OfferLetter
        fields = ['template', 'offer_date', 'ctc_offered', 'joining_date', 'expiry_date', 'content']
        widgets = {
            'offer_date': forms.DateInput(attrs={'type': 'date'}),
            'joining_date': forms.DateInput(attrs={'type': 'date'}),
            'expiry_date': forms.DateInput(attrs={'type': 'date'}),
            'content': forms.Textarea(attrs={'rows': 14}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['template'].queryset = m.OfferTemplate.objects.filter(is_active=True)
        self.fields['content'].required = False


class ConvertToEmployeeForm(forms.Form):
    company = forms.ModelChoiceField(queryset=m.Company.objects.all())
    department = forms.ModelChoiceField(queryset=m.Department.objects.all(), required=False)
    designation = forms.ModelChoiceField(queryset=m.Designation.objects.all(), required=False)
    employee_code = forms.CharField(max_length=30)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            widget.attrs.setdefault('class', 'form-select' if isinstance(widget, forms.Select) else 'form-control')


# ---------------------------------------------------------------------------
# Asset Management
# ---------------------------------------------------------------------------
class AssetForm(BootstrapModelForm):
    class Meta:
        model = m.Asset
        fields = ['company', 'employee', 'name', 'asset_type', 'serial_number',
                  'purchase_date', 'status', 'assigned_on', 'remarks']
        widgets = {
            'purchase_date': forms.DateInput(attrs={'type': 'date'}),
            'assigned_on': forms.DateInput(attrs={'type': 'date'}),
        }
# Assuming BootstrapModelForm is your base class that adds 'form-control' classes
class AssetForm(BootstrapModelForm):
    class Meta:
        model = m.Asset
        # Included the user's requested fields + device_password
        fields = [
            'company', 'category', 'name', 'asset_type', 'serial_number',
            'sim', 'phone_number', 'device_password', 'employee',
            'status', 'purchase_date', 'assigned_on', 'remarks'
        ]

        widgets = {
            'purchase_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'assigned_on': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'device_password': forms.TextInput(attrs={
                'placeholder': 'Enter login password or PIN',
                'class': 'form-control'
            }),
            'name': forms.TextInput(attrs={'placeholder': 'e.g. MacBook Pro / Dell Latitude'}),
            'asset_type': forms.TextInput(attrs={'placeholder': 'e.g. Laptop, Mobile, Tablet'}),
            'serial_number': forms.TextInput(attrs={'placeholder': 'Enter Unique Serial Number'}),
            'sim': forms.TextInput(attrs={'placeholder': 'SIM Number or Provider'}),
            'phone_number': forms.TextInput(attrs={'placeholder': 'Phone number associated'}),
            'remarks': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Optional notes about asset condition...'}),
            'company': forms.Select(attrs={'class': 'form-select'}),
            'employee': forms.Select(attrs={'class': 'form-select'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = m.AssetCategory.objects.all()
        # Professional Touch: If status is 'Available', hide assigned_on or make it optional
        self.fields['employee'].empty_label = "--- Select Employee (Leave blank if unassigned) ---"
        # self.fields['asset_type'].help_text = "Enter the category of the device."

# ---------------------------------------------------------------------------
# Performance Reviews
# ---------------------------------------------------------------------------
class PerformanceReviewForm(BootstrapModelForm):
    class Meta:
        model = m.PerformanceReview
        fields = ['employee', 'reviewer', 'review_period_from', 'review_period_to',
                  'review_date', 'rating', 'comments', 'goals', 'status']
        widgets = {
            'review_period_from': forms.DateInput(attrs={'type': 'date'}),
            'review_period_to': forms.DateInput(attrs={'type': 'date'}),
            'review_date': forms.DateInput(attrs={'type': 'date'}),
            'comments': forms.Textarea(attrs={'rows': 3}),
            'goals': forms.Textarea(attrs={'rows': 3}),
        }