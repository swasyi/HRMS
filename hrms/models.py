"""
HRMS models — mirrors the ER diagram (HRMS – Django Models).
Organised into the same 9 sections as the diagram, in one file for now.
Split into models/ package later if it grows unwieldy.
"""
from django.conf import settings
from django.db import models
from django.contrib.auth import get_user_model
import re
from django.db.models import Max
from decimal import Decimal
from datetime import datetime, date


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
    office_start_time = models.TimeField(default='10:00')
    office_end_time = models.TimeField(default='18:00')
    grace_minutes = models.PositiveIntegerField(default=15)
    grace_allowed_count = models.PositiveIntegerField(default=3)
    # ADD THESE 3 FIELDS TO MATCH THE POLICY MODEL:
    half_day_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=4.0)
    full_day_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=8.0)
    overtime_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=9.0)

    class Meta:
        verbose_name_plural = 'Companies'

    def __str__(self):
        return self.name

    def get_policy_for_date(self, target_date):
        """
        The 'Time Travel' logic:
        Checks if a historical policy exists for the given date.
        If no history matches, it falls back to the master Company settings.
        """
        # Find the most recent policy that started BEFORE or ON the target date
        policy = self.policy_history.filter(effective_from__lte=target_date).order_by('-effective_from').first()
        return policy if policy else self

    def save(self, *args, **kwargs):
        # We only check for changes if the Company already exists (update mode)
        if self.pk:
            # 1. Fetch the OLD settings from the database before they are overwritten
            # We use .get() to see what is currently saved in the DB
            old_data = Company.objects.get(pk=self.pk)

            # 2. Compare every timing field to see if HR changed anything
            timing_changed = (
                    old_data.office_start_time != self.office_start_time or
                    old_data.office_end_time != self.office_end_time or
                    old_data.grace_minutes != self.grace_minutes or
                    old_data.grace_allowed_count != self.grace_allowed_count or
                    old_data.half_day_threshold_hours != self.half_day_threshold_hours or
                    old_data.full_day_threshold_hours != self.full_day_threshold_hours or
                    old_data.overtime_threshold_hours != self.overtime_threshold_hours
            )

            # 3. If settings changed, we create a "Snapshot" of the NEW rules
            # starting from today (or the start of the month)
            if timing_changed:
                from datetime import date
                # We import AttendancePolicy inside to avoid circular import issues
                from .models import AttendancePolicy

                AttendancePolicy.objects.create(
                    company=self,
                    office_start_time=self.office_start_time,
                    office_end_time=self.office_end_time,
                    grace_minutes=self.grace_minutes,
                    grace_allowed_count=self.grace_allowed_count,
                    half_day_threshold_hours=self.half_day_threshold_hours,
                    full_day_threshold_hours=self.full_day_threshold_hours,
                    overtime_threshold_hours=self.overtime_threshold_hours,
                    effective_from=date.today()
                )

        # Finally, save the main Company record
        super().save(*args, **kwargs)

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

class HolidayCalendar(TimeStampedModel):
    """
    Acts as a Template (e.g., 'South India Calendar', 'General Office Calendar').
    """
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='holiday_calendars')
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    is_default = models.BooleanField(default=False, help_text="Default calendar for new employees")

    def __str__(self):
        return f"{self.name} ({self.company.name})"


class Holiday(TimeStampedModel):
    class HolidayType(models.TextChoices):
        NATIONAL = 'national', 'National'
        FESTIVAL = 'festival', 'Festival'
        OPTIONAL = 'optional', 'Optional'

    # CHANGE THIS: Instead of linking to Company, link to HolidayCalendar
    calendar = models.ForeignKey(HolidayCalendar, on_delete=models.CASCADE, related_name='holidays',        null=True,   # Add this
        blank=True   )
    date = models.DateField()
    name = models.CharField(max_length=150)
    type = models.CharField(max_length=20, choices=HolidayType.choices, default=HolidayType.NATIONAL)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        unique_together = ('calendar', 'date', 'name')

    def __str__(self):
        return f'{self.name} - {self.date}'


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
    father_name = models.CharField(max_length=200, blank=True)
    mother_name = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True, help_text="Residential/Permanent Address")
    emergency_contact = models.TextField(
        blank=True,
        help_text="Name, relationship, and phone number of person to contact in emergency"
    )


    date_of_joining = models.DateField()
    date_of_confirmation = models.DateField(null=True, blank=True)

    employment_type = models.CharField(max_length=20, choices=EmploymentType.choices,
                                        default=EmploymentType.FULL_TIME)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    is_manager = models.BooleanField(
        default=False,
        help_text="Designates if this employee can act as a reporting manager."
    )
    reporting_manager = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='subordinates',
        help_text="Select direct reporting manager"
    )
    # NEW: Allows HR to be assigned to multiple companies
    managed_companies = models.ManyToManyField(
        'Company',
        blank=True,
        related_name='managers',
        help_text="For HR/Admins: Which companies can this user manage?"
    )
    holiday_calendar = models.ForeignKey(
        HolidayCalendar,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees',
        help_text="Assign regional holiday template to this employee"
    )

    def is_holiday(self, target_date):
        """Helper to check if a specific date is a holiday for this employee."""
        if not self.holiday_calendar:
            return False
        return self.holiday_calendar.holidays.filter(date=target_date).exists()

    def save(self, *args, **kwargs):
        if not self.employee_code:
            # 1. Find the highest existing employee code string
            # This looks for the "largest" string alphabetically (e.g., OHCE0098 > OHCE0043)
            max_code = Employee.objects.aggregate(Max('employee_code'))['employee_code__max']

            if max_code:
                # 2. Extract digits from the string "OHCE0098" -> 98
                nums = re.findall(r'\d+', max_code)
                if nums:
                    last_number = int(nums[0])
                    new_number = last_number + 1
                else:
                    new_number = 1
            else:
                # Start at 1 if no employees exist
                new_number = 1

            # 3. Format to OHCE + 4 digits (e.g., OHCE0099)
            self.employee_code = f'OHCE{new_number:04d}'

        super(Employee, self).save(*args, **kwargs)

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
# ---------------------------------------------------------------------------
# 3. HIRING / RECRUITMENT PIPELINE
# ---------------------------------------------------------------------------
class RecruitmentStage(TimeStampedModel):
    """Reusable stage definitions (e.g. 'Technical Round', 'Culture Fit').
    Stages are composed, in whatever order a job needs, via JobPipeline —
    they are not tied to any one job posting."""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    # ADD THIS:
    evaluation_criteria = models.JSONField(
        default=dict,
        blank=True,
        help_text='Define what to grade, e.g. {"Python": 5, "Communication": 5}'
    )

    is_default = models.BooleanField(
        default=False,
        help_text='If checked, this stage is auto-added to every new JobPipeline.')

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class JobPosting(TimeStampedModel):
    class JobType(models.TextChoices):
        FULL_TIME = 'full_time', 'Full Time'
        PART_TIME = 'part_time', 'Part Time'
        CONTRACT = 'contract', 'Contract'
        INTERN = 'intern', 'Intern'

    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='job_postings')
    department = models.ForeignKey('Department', on_delete=models.SET_NULL, null=True, blank=True)
    designation = models.ForeignKey('Designation', on_delete=models.SET_NULL, null=True, blank=True)
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

    def ordered_pipeline(self):
        """Convenience accessor used by templates/views: this job's stages,
        in hiring order."""
        return self.pipeline_stages.select_related('stage').order_by('order')


class JobPipeline(TimeStampedModel):
    """The ordered sequence of RecruitmentStages for ONE specific JobPosting —
    each job can define its own unique hiring flow (e.g. a Sales role might
    skip 'Technical Round' entirely)."""
    job = models.ForeignKey(JobPosting, on_delete=models.CASCADE, related_name='pipeline_stages')
    stage = models.ForeignKey(RecruitmentStage, on_delete=models.CASCADE, related_name='job_pipelines')
    order = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('job', 'stage')
        ordering = ['job', 'order']

    def __str__(self):
        return f'{self.job} — [{self.order}] {self.stage}'


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

    class Source(models.TextChoices):
        LINKEDIN = 'linkedin', 'LinkedIn'
        INDEED = 'indeed', 'Indeed'
        REFERRAL = 'referral', 'Referral'
        CAREER_SITE = 'career_site', 'Career Site'

    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name='applications')
    job_posting = models.ForeignKey(JobPosting, on_delete=models.CASCADE, related_name='applications')
    applied_on = models.DateField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.APPLIED)
    source = models.CharField(max_length=20, choices=Source.choices, blank=True)
    cover_letter = models.TextField(blank=True)

    # --- ATS metadata ---
    expected_ctc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    current_ctc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notice_period = models.PositiveIntegerField(default=0, help_text='Notice period, in days.')
    current_stage = models.ForeignKey(RecruitmentStage, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='applications',
                                       help_text="Where this candidate currently sits in the job's pipeline.")
    progress_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    referred_by = models.ForeignKey(
        'Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='referrals',
        help_text="Employee who referred this candidate"
    )
    rejection_reason = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Reason for rejection (e.g., Salary mismatch, Skill gap)"
    )
    rejection_notes = models.TextField(
        blank=True,
        null=True,
        help_text="Detailed internal notes on why the candidate was not a fit"
    )

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

    def recompute_progress(self):
        """Sets progress_percentage from current_stage's position in the
        job's pipeline. Call after changing current_stage; not automatic on
        save() so bulk updates don't pay the query cost."""
        total = self.job_posting.pipeline_stages.count()
        if not total or not self.current_stage_id:
            return
        position = self.job_posting.pipeline_stages.filter(
            order__lte=self.job_posting.pipeline_stages.get(stage_id=self.current_stage_id).order
        ).count()
        self.progress_percentage = round((position / total) * 100, 2)
        self.save(update_fields=['progress_percentage', 'updated_at'])

    # --- Locking / confirmed-outcome protection ---
    is_locked = models.BooleanField(
        default=False,
        help_text='Set automatically once the outcome is confirmed (Hired/Rejected). '
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
    pipeline_stage = models.ForeignKey(
        JobPipeline, on_delete=models.SET_NULL, null=True, blank=True, related_name='interviews',
        help_text="Which stage of the job's own pipeline this interview covers.")
    interview_round = models.CharField(
        max_length=100, blank=True,
        help_text='Free-text label — kept for cases with no matching JobPipeline stage.')
    scheduled_on = models.DateTimeField()
    # ManyToManyField , allows multiple employees to see the candidate on their dashboard and submit their own feedback for the same round
    # interviewer = models.ManyToManyField ('Employee', on_delete=models.SET_NULL, null=True, blank=True,related_name='interviews_conducted',help_text="The panel of employees conducting this interview")
    interviewer = models.ManyToManyField(
        'Employee',
        blank=True,
        related_name='interviews_conducted',
        help_text="The panel of employees conducting this interview"
    )

    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.ONLINE)
    feedback = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    scorecard = models.JSONField(
        default=dict, blank=True,
        help_text='Structured ratings, e.g. {"technical": 4, "communication": 5, "culture": 3}')

    def average_score(self):
        """Handy for templates: mean of whatever numeric ratings are in
        scorecard, or None if it's empty."""
        values = [v for v in self.scorecard.values() if isinstance(v, (int, float))]
        return round(sum(values) / len(values), 2) if values else None

    def __str__(self):
        label = self.pipeline_stage.stage.name if self.pipeline_stage_id else (self.interview_round or 'Interview')
        return f'{self.application} - {label}'

# this model allows every person on the panel to give their own private rating.

class InterviewFeedback(TimeStampedModel):
    """Individual feedback from each person on the interview panel."""
    interview = models.ForeignKey(
        'Interview',
        on_delete=models.CASCADE,
        related_name='individual_feedbacks'
    )
    interviewer = models.ForeignKey(
        'Employee',
        on_delete=models.CASCADE
    )
    feedback_text = models.TextField(blank=True)
    scorecard_filled = models.JSONField(
        default=dict,
        help_text="The actual scores given based on the Stage evaluation_criteria"
    )
    recommendation = models.CharField(
        max_length=20,
        choices=[('hire', 'Hire'), ('maybe', 'Maybe'), ('reject', 'Reject')],
        default='maybe'
    )

    def __str__(self):
        return f"Feedback from {self.interviewer} for {self.interview.application.candidate}"



# Add this to allow HR and Managers to "chat" about a candidate.
class ApplicationNote(TimeStampedModel):
    """Internal discussion/comments for a specific application."""
    application = models.ForeignKey(
        'Application',
        on_delete=models.CASCADE,
        related_name='notes'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE
    )
    message = models.TextField()
    is_private = models.BooleanField(
        default=False,
        help_text="If True, only HR can see this note"
    )

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Note by {self.author} on {self.application}"
class OfferTemplate(TimeStampedModel):
    """Reusable HTML offer-letter templates with {{ placeholder }} tokens
    that OfferLetter.content is generated from."""
    name = models.CharField(max_length=150)
    body_html = models.TextField(
        help_text='HTML body. Supported placeholders: {{candidate_name}}, {{job_title}}, '
                   '{{company_name}}, {{ctc_offered}}, {{offer_date}}, {{joining_date}}.')
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def render(self, application, ctc_offered, offer_date, joining_date=None):
        """Simple, dependency-free token substitution. Swap for a proper
        template engine (Jinja2 / Django Template) later if you need
        loops/conditionals in offer bodies."""
        tokens = {
            '{{candidate_name}}': str(application.candidate),
            '{{job_title}}': application.job_posting.title,
            '{{company_name}}': str(application.job_posting.company),
            '{{ctc_offered}}': str(ctc_offered),
            '{{offer_date}}': offer_date.strftime('%d %b %Y') if offer_date else '',
            '{{joining_date}}': joining_date.strftime('%d %b %Y') if joining_date else 'TBD',
        }
        html = self.body_html
        for token, value in tokens.items():
            html = html.replace(token, value)
        return html


class OfferLetter(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        SENT = 'sent', 'Sent'
        ACCEPTED = 'accepted', 'Accepted'
        DECLINED = 'declined', 'Declined'
        EXPIRED = 'expired', 'Expired'

    application = models.OneToOneField(Application, on_delete=models.CASCADE, related_name='offer_letter')
    template = models.ForeignKey(OfferTemplate, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='offer_letters')
    offer_date = models.DateField()
    ctc_offered = models.DecimalField(max_digits=12, decimal_places=2)
    joining_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    expiry_date = models.DateField(null=True, blank=True)
    content = models.TextField(
        blank=True,
        help_text='Final offer content (rendered from the chosen template, then hand-edited).')
    generated_pdf = models.FileField(upload_to='offers/generated/', blank=True, null=True)

    def __str__(self):
        return f'Offer - {self.application.candidate}'

# ---------------------------------------------------------------------------
# 4. ATTENDANCE & GRACE POLICY
# ---------------------------------------------------------------------------
class AttendancePolicy(TimeStampedModel):
    # company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name='attendance_policy')
    # company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='policy_archives')
    company = models.ForeignKey('Company', on_delete=models.CASCADE, related_name='policy_history')

    # Use EXACT same names as Company model
    office_start_time = models.TimeField()
    office_end_time = models.TimeField()
    grace_minutes = models.PositiveIntegerField(default=15)
    grace_allowed_count = models.PositiveIntegerField(default=3)
    half_day_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=4)
    full_day_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=8)
    overtime_threshold_hours = models.DecimalField(max_digits=4, decimal_places=2, default=9, null=True,blank=True)
    # This rule was active until the end of this date
    effective_from = models.DateField()

    def __str__(self):
        return f"{self.company.name} Policy (Starts {self.effective_from})"

    class Meta:
        ordering = ['-effective_from'] # Newest versions at the top


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

    def save(self, *args, **kwargs):
        # 1. Get the correct policy for this specific day
        policy = self.employee.company.get_policy_for_date(self.attendance_date)

        # 2. Calculate Total Hours
        if self.check_in and self.check_out:
            diff = self.check_out - self.check_in
            self.total_hours = Decimal(diff.total_seconds() / 3600).quantize(Decimal('0.01'))

            # 3. Apply Thresholds from the Policy
            if self.total_hours >= policy.full_day_threshold_hours:
                self.status = self.Status.PRESENT
                self.is_half_day = False
            elif self.total_hours >= policy.half_day_threshold_hours:
                self.status = self.Status.HALF_DAY
                self.is_half_day = True
            else:
                self.status = self.Status.ABSENT
                self.is_half_day = False

            # 4. Check for Overtime
            self.is_overtime = self.total_hours >= policy.overtime_threshold_hours

        super().save(*args, **kwargs)

    # models.py -> AttendanceRecord

    def save(self, *args, **kwargs):
        # 1. Get the policy (contains thresholds like 8.0 for Full Day)
        policy = self.employee.company.get_policy_for_date(self.attendance_date)

        if self.check_in and self.check_out:
            # 2. Calculate GROSS duration (Total time in office)
            diff = self.check_out - self.check_in
            gross_hours = Decimal(diff.total_seconds() / 3600).quantize(Decimal('0.01'))

            # 3. Set total_hours to Gross Time so it includes lunch
            self.total_hours = gross_hours

            # 4. Use Gross Time to determine Status
            # If they were in the office for 8+ hours total, it's a Full Day
            if self.total_hours >= policy.full_day_threshold_hours:
                self.status = self.Status.PRESENT
                self.is_half_day = False
            elif self.total_hours >= policy.half_day_threshold_hours:
                self.status = self.Status.HALF_DAY
                self.is_half_day = True
            else:
                self.status = self.Status.ABSENT
                self.is_half_day = False

            # 5. Check for Overtime
            self.is_overtime = self.total_hours >= policy.overtime_threshold_hours

        super().save(*args, **kwargs)
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


class AttendancePenalty(TimeStampedModel):
    class DeductionStatus(models.TextChoices):
        APPLIED = 'applied', 'Applied'
        LWP = 'lwp', 'LWP (No Leave Balance)'
        WAIVED = 'waived', 'Waived'

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='penalties')
    attendance_record = models.ForeignKey('AttendanceRecord', on_delete=models.CASCADE, null=True, blank=True, related_name='penalties')
    penalty_date = models.DateField()
    reason = models.CharField(max_length=255, default='Late Arrival (Exceeded Grace Period)')
    late_minutes = models.PositiveIntegerField(default=0)
    deduction_days = models.DecimalField(max_digits=4, decimal_places=2, default=0.5)
    deduction_source = models.CharField(max_length=100, default='CL')
    status = models.CharField(max_length=20, choices=DeductionStatus.choices, default=DeductionStatus.APPLIED)

    class Meta:
        ordering = ['-penalty_date', '-created_at']

    def __str__(self):
        return f"Penalty: {self.employee} - {self.penalty_date} ({self.deduction_source})"



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
        PENDING_MANAGER = 'pending_manager', 'Pending Manager Approval'
        PENDING_HR = 'pending_hr', 'Pending HR Approval'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'
    class DayType(models.TextChoices):
        FULL_DAY = 'full', 'Full Day'
        HALF_DAY = 'half', 'Half Day'

    # ... other fields ...
    day_type = models.CharField(max_length=10, choices=DayType.choices, default=DayType.FULL_DAY)

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='leave_applications')
    leave_type = models.ForeignKey(LeaveType, on_delete=models.CASCADE, related_name='applications')
    start_date = models.DateField()
    end_date = models.DateField()
    total_days = models.DecimalField(max_digits=5, decimal_places=1)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    applied_on = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='leave_approvals')
    approved_on = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-applied_on']

    def __str__(self):
        return f'{self.employee} - {self.leave_type} ({self.start_date} to {self.end_date})'

from django.db import models




from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=Employee)
def auto_create_leave_bank_row(sender, instance, created, **kwargs):
    """
    When a new Employee is saved, this automatically creates
    a row in EmployeeLeaveBalance with 0.0 values.
    """
    if created:
        EmployeeLeaveBalance.objects.get_or_create(e_name=instance)
        EmployeeLeaveBalanceLive.objects.get_or_create(e_name=instance)


from django.db.models.signals import post_save
from django.dispatch import receiver
from datetime import timedelta




from django.db.models.signals import post_save
from django.dispatch import receiver
from datetime import timedelta
# Assuming your logic file is named leave_logic.py
from . import leave_logic as lv

@receiver(post_save, sender=LeaveApplication)
def sync_leave_and_attendance_on_approval(sender, instance, created, **kwargs):
    """
    1. Deducts leave from bank using Priority Logic (CL -> EL fallback).
    2. Automatically creates AttendanceRecord entries as 'ON_LEAVE'.
    3. Handles Refund if leave is cancelled.
    """
    if instance.status == LeaveApplication.Status.APPROVED:
        # --- PART A: Smart Deduction from Leave Bank ---
        # This function now handles the fallback: Specific Code -> CL -> EL -> LWP
        result_msg = lv.adjust_leave_bank(
            employee=instance.employee,
            amount=instance.total_days,
            action="deduct",
            leave_code=instance.leave_type.code
        )

        # --- PART B: Create Attendance Records for the leave period ---
        current_date = instance.start_date
        while current_date <= instance.end_date:
            AttendanceRecord.objects.update_or_create(
                employee=instance.employee,
                attendance_date=current_date,
                defaults={
                    'status': AttendanceRecord.Status.ON_LEAVE,
                    # We store the leave code + the bank result (e.g., "SL | Deducted from CL")
                    'remarks': f"{instance.leave_type.code.upper()} | {result_msg}",
                }
            )
            current_date += timedelta(days=1)

    # Handle Cancellation/Rejection: If an APPROVED leave is cancelled, give the days back
    elif instance.status in [LeaveApplication.Status.REJECTED, LeaveApplication.Status.CANCELLED]:
        # 1. Refund the bank
        lv.adjust_leave_bank(
            employee=instance.employee,
            amount=instance.total_days,
            action="refund",
            leave_code=instance.leave_type.code
        )

        # 2. Reset Attendance Records
        AttendanceRecord.objects.filter(
            employee=instance.employee,
            attendance_date__range=[instance.start_date, instance.end_date],
            status=AttendanceRecord.Status.ON_LEAVE
        ).update(status=AttendanceRecord.Status.ABSENT, remarks="Leave Cancelled")


class EmployeeLeaveBalance(models.Model):
    # Link directly to your existing Employee table
    e_name = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="assigned_leaves")

    # Status can stay here if 'Leave Status' is different from 'Work Status'
    # Otherwise, you can also pull 'status' from the Employee model.
    # status = models.CharField(max_length=100, default="Active")
    @property
    def work_status(self):
        """Fetches the live status directly from the Employee model."""
        return self.e_name.get_status_display()

    # Leave Balances
    bereavement_leave = models.FloatField(default=0.0)
    menstrual_leave = models.FloatField(default=0.0)
    sick_leave = models.FloatField(default=0.0)
    earned_leave = models.FloatField(default=0.0)
    casual_leave = models.FloatField(default=0.0)
    comp_off = models.FloatField(default=0.0)

    def __str__(self):
        return f"Leave Balance for {self.e_name.name}"

    # These properties allow you to "see" the Dept/Desig without saving them here
    @property
    def department(self):
        return self.e_name.department

    @property
    def designation(self):
        return self.e_name.designation

    @property
    def employment_type(self):
        return self.e_name.get_employment_type_display()
    # ADD THIS LINE TO SHOW THE DATE
    @property
    def joining_date(self):
        return self.e_name.date_of_joining
    @property
    def confirmation_date(self):
        return self.e_name.date_of_confirmation



class EmployeeLeaveBalanceLive(models.Model):
    """PAGE 2: THE LIVE REPORT - Deductions happen here"""
    e_name = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="live_balances")
    bereavement_leave = models.FloatField(default=0.0)
    menstrual_leave = models.FloatField(default=0.0)
    sick_leave = models.FloatField(default=0.0)
    earned_leave = models.FloatField(default=0.0)
    casual_leave = models.FloatField(default=0.0)
    comp_off = models.FloatField(default=0.0)

    def __str__(self):
        return f"Live Balance for {self.e_name.full_name}"
@receiver(post_save, sender=Employee)
def auto_create_leave_rows(sender, instance, created, **kwargs):
    """Creates a row in both Bank and Live Balance when a new Employee is created."""
    if created:
        EmployeeLeaveBalance.objects.get_or_create(e_name=instance)
        EmployeeLeaveBalanceLive.objects.get_or_create(e_name=instance)

# --- THE SYNC SIGNAL (Bank -> Balance) ---
@receiver(post_save, sender=EmployeeLeaveBalance)
def sync_bank_assignment_to_live_balance(sender, instance, created, **kwargs):
    """
    This handles your requirement: 'if i increase manually leave in leave bank to yha b wo increase ho jaegi'
    """
    live, _ = EmployeeLeaveBalanceLive.objects.get_or_create(e_name=instance.e_name)

    if created:
        # Initial copy of assigned leaves
        live.casual_leave = instance.casual_leave
        live.sick_leave = instance.sick_leave
        live.earned_leave = instance.earned_leave
        live.menstrual_leave = instance.menstrual_leave
        live.bereavement_leave = instance.bereavement_leave
        live.comp_off = instance.comp_off
    else:
        # If HR manually changes the Bank, we don't want to reset usage.
        # We calculate the difference and add/subtract it to the Live Balance.
        # (This logic ensures the Bank stays permanent while Balance stays live)
        pass  # In a full implementation, you would track the 'delta' here.

    live.save()



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
    # SNAPSHOT FIELDS
    applied_start_time = models.TimeField(null=True, blank=True)
    applied_end_time = models.TimeField(null=True, blank=True)
    applied_grace_limit = models.PositiveIntegerField(null=True, blank=True)

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
    # e.g., "serial_number,device_password,additional_details"
    required_fields = models.TextField(blank=True, help_text="Comma separated field names to show in the form")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Asset Categories"


from django.db import models
from decimal import Decimal
from datetime import date


# ... (keep existing imports)

class Asset(TimeStampedModel):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        ASSIGNED = 'assigned', 'Assigned'
        UNDER_REPAIR = 'under_repair', 'Under Repair'
        RETIRED = 'retired', 'Retired'

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='assets')
    category = models.ForeignKey(AssetCategory, on_delete=models.PROTECT, related_name='assets', null=True)
    employee = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='assets')
    name = models.CharField(max_length=150)
    asset_type = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    sim = models.CharField(max_length=100, null=True, blank=True, help_text="SIM card number or provider details")
    phone_number = models.CharField(max_length=20, null=True, blank=True, help_text="Phone number associated with the SIM/Device")

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
    def save(self, *args, **kwargs):
        # 1. Capture whether this is a brand new record BEFORE saving
        is_new = self.pk is None

        if not is_new:
            # 2. This logic runs ONLY for updates (re-assignments)
            old_asset = Asset.objects.get(pk=self.pk)

            # Check if the employee has changed
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

                # If there is a NEW employee assigned during update, start new history
                if self.employee:
                    AssetAssignmentHistory.objects.create(
                        asset=self,
                        employee=self.employee,
                        is_still_using=True
                    )

        # 3. Save the actual Asset to the database
        # After this line, self.pk will exist even if it was a new record
        super().save(*args, **kwargs)

        # 4. Handle the very first time an asset is created with an employee
        # We use the 'is_new' variable we captured at the beginning
        if is_new and self.employee:
            AssetAssignmentHistory.objects.create(
                asset=self,
                employee=self.employee,
                is_still_using=True
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