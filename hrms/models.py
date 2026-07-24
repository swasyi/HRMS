"""
HRMS models — mirrors the ER diagram (HRMS – Django Models).
Organised into the same 9 sections as the diagram, in one file for now.
Split into models/ package later if it grows unwieldy.
"""
from django.conf import settings
from django.db import models
from django.contrib.auth import get_user_model


User = get_user_model()

# ---------------------------------------------------------------------------
# 0. ABSTRACT BASE
# ---------------------------------------------------------------------------
class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# 1. ORGANISATION STRUCTURE
# ---------------------------------------------------------------------------
class Company(TimeStampedModel):
    name = models.CharField(max_length=255)
    logo = models.ImageField(upload_to='company/logo/', blank=True, null=True)
    address = models.TextField(blank=True)
    cin = models.CharField('CIN', max_length=50, blank=True)
    pan = models.CharField('PAN', max_length=20, blank=True)
    gstin = models.CharField('GSTIN', max_length=20, blank=True)
    website = models.URLField(blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    office_start_time = models.TimeField(default='09:30')
    office_end_time = models.TimeField(default='18:30')
    grace_minutes = models.PositiveIntegerField(default=15)
    grace_allowed_count = models.PositiveIntegerField(default=3)

    class Meta:
        verbose_name_plural = 'Companies'

    def __str__(self):
        return self.name


class Department(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='departments')
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)

    class Meta:
        unique_together = ('company', 'name')

    def __str__(self):
        return f'{self.name} ({self.company})'


class Designation(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='designations')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='designations')
    title = models.CharField(max_length=150)
    level = models.PositiveSmallIntegerField(default=1)

    def __str__(self):
        return self.title


# ---------------------------------------------------------------------------
# 2. EMPLOYEE
# ---------------------------------------------------------------------------
class Employee(TimeStampedModel):
    class EmploymentType(models.TextChoices):
        FULL_TIME = 'full_time', 'Full Time'
        PART_TIME = 'part_time', 'Part Time'
        CONTRACT = 'contract', 'Contract'
        INTERN = 'intern', 'Intern'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        ON_LEAVE = 'on_leave', 'On Leave'
        SUSPENDED = 'suspended', 'Suspended'
        RELIEVED = 'relieved', 'Relieved'

    class Gender(models.TextChoices):
        MALE = 'M', 'Male'
        FEMALE = 'F', 'Female'
        OTHER = 'O', 'Other'

    # Link to the login account (inventory.User). Optional: HR can create the
    # HR record before an employee is issued login credentials.
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name='employee_profile')

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='employees')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='employees')
    designation = models.ForeignKey(Designation, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='employees')

    employee_code = models.CharField(max_length=30, unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    date_of_joining = models.DateField()
    employment_type = models.CharField(max_length=20, choices=EmploymentType.choices,
                                        default=EmploymentType.FULL_TIME)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    # NEW: Allows HR to be assigned to multiple companies
    managed_companies = models.ManyToManyField(
        'Company',
        blank=True,
        related_name='managers',
        help_text="For HR/Admins: Which companies can this user manage?"
    )


    class Meta:
        ordering = ['employee_code']

    def __str__(self):
        return f'{self.employee_code} - {self.first_name} {self.last_name}'.strip()

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

@property
def full_name(self):
    name = f"{self.first_name} {self.last_name}".strip()
    return name if name else self.email  # Returns email if name is blank


class EmployeeBankDetail(TimeStampedModel):
    employee = models.OneToOneField(Employee, on_delete=models.CASCADE, related_name='bank_detail')
    account_holder = models.CharField(max_length=150)
    account_number = models.CharField(max_length=40)
    ifsc_code = models.CharField('IFSC Code', max_length=15)
    bank_name = models.CharField(max_length=150)
    branch_name = models.CharField(max_length=150, blank=True)
    account_type = models.CharField(max_length=20, blank=True)
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.employee} bank detail'


class EmployeeDocument(TimeStampedModel):
    class DocumentType(models.TextChoices):
        ID_PROOF = 'id_proof', 'ID Proof'
        ADDRESS_PROOF = 'address_proof', 'Address Proof'
        EDUCATION = 'education', 'Education Certificate'
        OFFER_LETTER = 'offer_letter', 'Offer Letter'
        RESUME = 'resume', 'Resume'
        OTHER = 'other', 'Other'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='documents')
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    file = models.FileField(upload_to='employees/documents/')
    description = models.CharField(max_length=255, blank=True)
    issued_on = models.DateField(null=True, blank=True)
    expiry_on = models.DateField(null=True, blank=True)

    def __str__(self):
        return f'{self.employee} - {self.get_document_type_display()}'


class EmployeeNotice(TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='notices')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    notice_date = models.DateField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.title} - {self.employee}'


# ---------------------------------------------------------------------------
# 3. HIRING / RECRUITMENT PIPELINE
# ---------------------------------------------------------------------------
class JobPosting(TimeStampedModel):
    class JobType(models.TextChoices):
        FULL_TIME = 'full_time', 'Full Time'
        PART_TIME = 'part_time', 'Part Time'
        CONTRACT = 'contract', 'Contract'
        INTERN = 'intern', 'Intern'

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='job_postings')
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    designation = models.ForeignKey(Designation, on_delete=models.SET_NULL, null=True, blank=True)
    title = models.CharField(max_length=200)
    job_type = models.CharField(max_length=20, choices=JobType.choices, default=JobType.FULL_TIME)
    location = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)
    requirements = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    posted_on = models.DateField(auto_now_add=True)
    closing_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.title


class Candidate(TimeStampedModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    resume = models.FileField(upload_to='candidates/resumes/', blank=True, null=True)
    current_company = models.CharField(max_length=150, blank=True)
    experience_years = models.DecimalField(max_digits=4, decimal_places=1, default=0)

    def __str__(self):
        return f'{self.first_name} {self.last_name}'.strip()


class Application(TimeStampedModel):
    class Status(models.TextChoices):
        APPLIED = 'applied', 'Applied'
        SHORTLISTED = 'shortlisted', 'Shortlisted'
        INTERVIEWING = 'interviewing', 'Interviewing'
        OFFERED = 'offered', 'Offered'
        REJECTED = 'rejected', 'Rejected'
        HIRED = 'hired', 'Hired'

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='applications')
    job_posting = models.ForeignKey(JobPosting, on_delete=models.CASCADE, related_name='applications')
    applied_on = models.DateField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.APPLIED)
    source = models.CharField(max_length=100, blank=True)
    cover_letter = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        old_status = None

        if not is_new:
            old_instance = Application.objects.get(pk=self.pk)
            old_status = old_instance.status

        super().save(*args, **kwargs)

        # Create audit log if status changed or it's a new record
        if is_new or old_status != self.status:
            RecruitmentAuditLog.objects.create(
                application=self,
                from_status=old_status or "New",
                to_status=self.status,
                action=f"Status changed to {self.get_status_display()}",
                note="Automatic system log"
            )
            # --- Locking / confirmed-outcome protection ---
    is_locked = models.BooleanField(
        default=False,
        help_text='Set autoAssetCategorymatically once the outcome is confirmed (Hired/Rejected). '
                   'A locked application can only be modified by a Super Admin.')
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='locked_applications')
    locked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('candidate', 'job_posting')

    def __str__(self):
        return f'{self.candidate} -> {self.job_posting}'



class RecruitmentAuditLog(TimeStampedModel):
    """Every status transition / significant action on an Application, with
    who did it — the audit trail the prompt asked for."""
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='audit_logs')
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20, blank=True)
    action = models.CharField(max_length=150)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='recruitment_actions')
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.application} — {self.action}'


class Interview(TimeStampedModel):
    class Mode(models.TextChoices):
        ONLINE = 'online', 'Online'
        IN_PERSON = 'in_person', 'In Person'
        PHONE = 'phone', 'Phone'

    class Status(models.TextChoices):
        SCHEDULED = 'scheduled', 'Scheduled'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'
        NO_SHOW = 'no_show', 'No Show'

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='interviews')
    interview_round = models.CharField(max_length=100)
    scheduled_on = models.DateTimeField()
    interviewer = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='interviews_conducted')
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.ONLINE)
    feedback = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)

    def __str__(self):
        return f'{self.application} - {self.interview_round}'


class OfferLetter(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SENT = 'sent', 'Sent'
        ACCEPTED = 'accepted', 'Accepted'
        DECLINED = 'declined', 'Declined'
        EXPIRED = 'expired', 'Expired'

    application = models.OneToOneField(Application, on_delete=models.CASCADE, related_name='offer_letter')
    offer_date = models.DateField()
    ctc_offered = models.DecimalField(max_digits=12, decimal_places=2)
    joining_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    expiry_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f'Offer - {self.application.candidate}'


# ---------------------------------------------------------------------------
# 4. ATTENDANCE & GRACE POLICY
# ---------------------------------------------------------------------------
class AttendancePolicy(TimeStampedModel):
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name='attendance_policy')
    work_start_time = models.TimeField()
    work_end_time = models.TimeField()
    grace_window_minutes = models.PositiveIntegerField(default=15)
    max_grace_per_month = models.PositiveIntegerField(default=3)
    half_day_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=4)
    full_day_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=8)
    overtime_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=9)

    def __str__(self):
        return f'Attendance Policy - {self.company}'


class AttendanceRecord(TimeStampedModel):
    class Status(models.TextChoices):
        PRESENT = 'present', 'Present'
        ABSENT = 'absent', 'Absent'
        HALF_DAY = 'half_day', 'Half Day'
        ON_LEAVE = 'on_leave', 'On Leave'
        HOLIDAY = 'holiday', 'Holiday'
        WEEK_OFF = 'week_off', 'Week Off'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='attendance_records')
    attendance_date = models.DateField()
    check_in = models.DateTimeField(null=True, blank=True)
    check_out = models.DateTimeField(null=True, blank=True)
    total_hours = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PRESENT)
    early_minutes = models.PositiveIntegerField(default=0)
    late_minutes = models.PositiveIntegerField(default=0)
    is_half_day = models.BooleanField(default=False)
    is_overtime = models.BooleanField(default=False)
    remarks = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ('employee', 'attendance_date')
        ordering = ['-attendance_date']

    def __str__(self):
        return f'{self.employee} - {self.attendance_date}'


class GraceUsageTracker(TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='grace_usages')
    month = models.PositiveSmallIntegerField()  # 1-12
    year = models.PositiveSmallIntegerField()
    usage_count = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('employee', 'month', 'year')

    def __str__(self):
        return f'{self.employee} - {self.month}/{self.year}'


class Holiday(TimeStampedModel):
    class HolidayType(models.TextChoices):
        NATIONAL = 'national', 'National'
        FESTIVAL = 'festival', 'Festival'
        OPTIONAL = 'optional', 'Optional'

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='holidays')
    date = models.DateField()
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=20, choices=HolidayType.choices, default=HolidayType.NATIONAL)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ('company', 'date', 'name')

    def __str__(self):
        return f'{self.name} - {self.date}'


# ---------------------------------------------------------------------------
# 5. LEAVE MANAGEMENT
# ---------------------------------------------------------------------------
class LeaveType(TimeStampedModel):
    class Gender(models.TextChoices):
        ALL = 'all', 'All'
        MALE = 'M', 'Male'
        FEMALE = 'F', 'Female'

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='leave_types')
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    days_per_year = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    is_paid = models.BooleanField(default=True)
    is_carry_forward = models.BooleanField(default=False)
    max_carry_forward = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    max_consecutive_days = models.PositiveIntegerField(null=True, blank=True)
    requires_approval = models.BooleanField(default=True)
    requires_document = models.BooleanField(default=False)
    applicable_gender = models.CharField(max_length=5, choices=Gender.choices, default=Gender.ALL)
    description = models.TextField(blank=True)

    class Meta:
        unique_together = ('company', 'code')

    def __str__(self):
        return self.name


class LeaveBalance(TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_balances')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name='balances')
    year = models.PositiveSmallIntegerField()
    allocated = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    used = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    pending = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    carried_forward = models.DecimalField(max_digits=5, decimal_places=1, default=0)

    class Meta:
        unique_together = ('employee', 'leave_type', 'year')

    def __str__(self):
        return f'{self.employee} - {self.leave_type} ({self.year})'

    @property
    def available(self):
        return self.allocated + self.carried_forward - self.used - self.pending


class LeaveApplication(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_applications')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name='applications')
    start_date = models.DateField()
    end_date = models.DateField()
    total_days = models.DecimalField(max_digits=5, decimal_places=1)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    applied_on = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='leave_approvals')
    approved_on = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-applied_on']

    def __str__(self):
        return f'{self.employee} - {self.leave_type} ({self.start_date} to {self.end_date})'


# ---------------------------------------------------------------------------
# 6. PAYROLL
# ---------------------------------------------------------------------------
class SalaryStructure(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='salary_structures')
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


class SalaryComponent(TimeStampedModel):
    class ComponentType(models.TextChoices):
        EARNING = 'earning', 'Earning'
        DEDUCTION = 'deduction', 'Deduction'

    structure = models.ForeignKey(SalaryStructure, on_delete=models.CASCADE, related_name='components')
    name = models.CharField(max_length=100)
    component_type = models.CharField(max_length=20, choices=ComponentType.choices)
    is_earning = models.BooleanField(default=True)
    is_deduction = models.BooleanField(default=False)
    is_taxable = models.BooleanField(default=True)
    display_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['display_order']

    def __str__(self):
        return f'{self.name} ({self.structure})'


class EmployeeSalary(TimeStampedModel):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='salaries')
    structure = models.ForeignKey(SalaryStructure, on_delete=models.SET_NULL, null=True, related_name='employee_salaries')
    ctc_annual = models.DecimalField(max_digits=12, decimal_places=2)
    basic = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    hra = models.DecimalField('HRA', max_digits=12, decimal_places=2, default=0)
    special_allowance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pf_employee = models.DecimalField('PF (Employee)', max_digits=12, decimal_places=2, default=0)
    pf_employer = models.DecimalField('PF (Employer)', max_digits=12, decimal_places=2, default=0)
    esic_employee = models.DecimalField('ESIC (Employee)', max_digits=12, decimal_places=2, default=0)
    esic_employer = models.DecimalField('ESIC (Employer)', max_digits=12, decimal_places=2, default=0)
    professional_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tds = models.DecimalField('TDS', max_digits=12, decimal_places=2, default=0)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)



    class Meta:
        ordering = ['-effective_from']

    def __str__(self):
        return f'{self.employee} - CTC {self.ctc_annual}'


class PayrollRun(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PROCESSING = 'processing', 'Processing'
        COMPLETED = 'completed', 'Completed'
        PAID = 'paid', 'Paid'

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='payroll_runs')
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()
    payroll_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    note = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = ('company', 'month', 'year')
        ordering = ['-year', '-month']

    def __str__(self):
        return f'{self.company} Payroll {self.month}/{self.year}'


class PaySlip(TimeStampedModel):
    class PaymentStatus(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        FAILED = 'failed', 'Failed'

    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name='payslips')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='payslips')

    # --- Attendance breakdown for the period (all in days) ---
    full_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    half_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    comp_off_days = models.DecimalField(
        max_digits=5, decimal_places=1, default=0,
        help_text='Worked on a Sunday/Holiday — credited as a paid day.')
    off_days = models.DecimalField(
        max_digits=5, decimal_places=1, default=0,
        help_text='Weekly-offs/holidays the employee did not have to work — paid, non-working.')
    paid_leave_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    absent_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    paid_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    total_days_in_month = models.PositiveSmallIntegerField(default=30)

    # --- Money breakdown ---
    daily_wage = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    penalty_deduction = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    loan_deduction = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    extra_earning = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    net_pay = models.DecimalField(max_digits=12, decimal_places=2)
    total_earnings = models.DecimalField(max_digits=12, decimal_places=2)
    total_deductions = models.DecimalField(max_digits=12, decimal_places=2)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices,
                                       default=PaymentStatus.PENDING)
    paid_on = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = ('payroll_run', 'employee')

    def __str__(self):
        return f'{self.employee} - {self.payroll_run}'

    @property
    def gross_salary(self):
        """Fixed monthly salary (Basic+HRA+Special Allowance) excluding ad-hoc extras."""
        return self.total_earnings - self.extra_earning


class LoanAdvance(TimeStampedModel):
    """A running loan/salary-advance whose monthly_installment is auto-deducted
    by the payroll engine each run, until remaining_balance hits zero."""
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='loans')
    description = models.CharField(max_length=255, blank=True)
    principal_amount = models.DecimalField(max_digits=12, decimal_places=2)
    monthly_installment = models.DecimalField(max_digits=12, decimal_places=2)
    remaining_balance = models.DecimalField(max_digits=12, decimal_places=2)
    start_date = models.DateField()
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.employee} — {self.description or "Loan"} (₹{self.remaining_balance} left)'


class PayrollExtra(TimeStampedModel):
    """An ad-hoc one-off earning (incentive/bonus) queued for the employee's
    NEXT payroll run. Consumed (linked to a PayrollRun) once processed."""
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='payroll_extras')
    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='consumed_extras')
    label = models.CharField(max_length=150)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_consumed = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.employee} — {self.label} (₹{self.amount})'


# ---------------------------------------------------------------------------
# 7. POLICIES & COMPANY NOTICES
# ---------------------------------------------------------------------------
class Policy(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='policies')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    document = models.FileField(upload_to='policies/', blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = 'Policies'

    def __str__(self):
        return self.title


class PolicyAcknowledgement(TimeStampedModel):
    policy = models.ForeignKey(Policy, on_delete=models.CASCADE, related_name='acknowledgements')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='policy_acknowledgements')
    acknowledged_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('policy', 'employee')

    def __str__(self):
        return f'{self.employee} acknowledged {self.policy}'


class CompanyNotice(TimeStampedModel):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='notices')
    title = models.CharField(max_length=200)
    message = models.TextField()
    notice_date = models.DateField()
    expiry_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.title


class NoticeRead(TimeStampedModel):
    notice = models.ForeignKey(CompanyNotice, on_delete=models.CASCADE, related_name='reads')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='notice_reads')
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('notice', 'employee')

    def __str__(self):
        return f'{self.employee} read {self.notice}'


# ---------------------------------------------------------------------------
# 8. ASSET MANAGEMENT (bonus)
# ---------------------------------------------------------------------------
class AssetCategory(TimeStampedModel):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Asset Categories"


class Asset(TimeStampedModel):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        ASSIGNED = 'assigned', 'Assigned'
        UNDER_REPAIR = 'under_repair', 'Under Repair'
        RETIRED = 'retired', 'Retired'

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='assets')
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='assets')
    name = models.CharField(max_length=150)
    asset_type = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    # NEW FIELDS
    device_password = models.CharField(max_length=255, blank=True, help_text="Login password or PIN for the device")
    additional_details = models.TextField(blank=True, help_text="OS version, RAM, etc.")

    purchase_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    assigned_on = models.DateField(null=True, blank=True)
    remarks = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # Detect if employee is changing to create history
        if self.pk:
            old_instance = Asset.objects.get(pk=self.pk)
            # If a new employee is assigned
            if old_instance.employee != self.employee and self.employee is not None:
                self.status = Asset.Status.ASSIGNED
                self.assigned_on = models.functions.Now()
                # Create history entry
                AssetAssignmentHistory.objects.create(
                    asset=self,
                    employee=self.employee,
                    assigned_date=models.functions.Now()
                )
        super().save(*args, **kwargs)

    def save(self, *args, **kwargs):
        if self.pk:
            # 1. Get the current state from the database
            old_asset = Asset.objects.get(pk=self.pk)

            # 2. Check if the employee has changed
            if old_asset.employee != self.employee:

                # If there was an old employee, close their history (Return them)
                if old_asset.employee:
                    AssetAssignmentHistory.objects.filter(
                        asset=self,
                        employee=old_asset.employee,
                        is_still_using=True
                    ).update(
                        returned_date=models.functions.Now(),
                        is_still_using=False
                    )

                # If there is a NEW employee assigned, start new history
                if self.employee:
                    AssetAssignmentHistory.objects.create(
                        asset=self,
                        employee=self.employee,
                        # No need to pass assigned_date if you added auto_now_add=True
                        is_still_using=True
                    )

        # Finally, save the actual Asset
        super().save(*args, **kwargs)

        # Handle the very first time an asset is created with an employee
        if not self.pk and self.employee:
            AssetAssignmentHistory.objects.create(
                asset=self, employee=self.employee, is_still_using=True
            )


class AssetAssignmentHistory(TimeStampedModel):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name='history')
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE)
    assigned_date = models.DateField(auto_now_add=True)
    returned_date = models.DateField(null=True, blank=True)
    is_still_using = models.BooleanField(default=True)


    class Meta:
        ordering = ['-assigned_date']


# ---------------------------------------------------------------------------
# 9. PERFORMANCE (bonus)
# ---------------------------------------------------------------------------
class PerformanceReview(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SUBMITTED = 'submitted', 'Submitted'
        ACKNOWLEDGED = 'acknowledged', 'Acknowledged'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='performance_reviews')
    review_period_from = models.DateField()
    review_period_to = models.DateField()
    review_date = models.DateField()
    reviewer = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='reviews_given')
    rating = models.PositiveSmallIntegerField()  # 1-5
    comments = models.TextField(blank=True)
    goals = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    def __str__(self):
        return f'{self.employee} review ({self.review_period_from} - {self.review_period_to})'
