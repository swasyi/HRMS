# Wiring `hrms` into your `merger` project

The app is now **fully built end-to-end**: Organisation & Employee, Attendance
& Grace Policy, Leave Management, Payroll & Salary Structure, Hiring/Recruitment,
Assets, and Performance. Nothing is left as a "coming soon" placeholder.

## 1. Copy the app in
Drop the `hrms/` folder next to your other apps (`inventory`, `docs`, etc.), at the same
level as `manage.py`.

## 2. settings.py changes

```python
INSTALLED_APPS = [
    'hrms',                 # add this
    'docs',
    'request_logs',
    'meta',
    'incentive_calculator',
    'tally_voucher',
    'proforma_invoice',
    'quotations',
    'inventory',
    'customer_dashboard',
    'crispy_forms',
    'crispy_bootstrap5',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'hrms.context_processors.hrms_role',   # add this
            ],
        },
    },
]

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'   # required: resumes, documents, policy files use FileField/ImageField
```

`AUTH_USER_MODEL = 'inventory.User'` needs no change — `Employee.user` is a
nullable `OneToOneField(settings.AUTH_USER_MODEL)`.

## 3. Root urls.py

```python
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from hrms.views import HRMSLoginView
from django.contrib.auth.views import LogoutView

urlpatterns = [
    path('admin/', admin.site.urls),                 # superadmin-only, already Django-native
    path('login', HRMSLoginView.as_view(), name='login'),
    path('logout', LogoutView.as_view(next_page='login'), name='logout'),
    path('hrms/', include('hrms.urls')),
    # ...your existing app urls...
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
```

Consider setting `LOGIN_REDIRECT_URL = "/hrms/"` so people land on the
dashboard after signing in (currently `"/"` in your settings.py).

## 4. Migrate

```bash
python manage.py makemigrations hrms
python manage.py migrate
```

Extra dependencies used by this app:
```bash
pip install reportlab --break-system-packages   # payslip PDF export
```

## 5. Roles

| Who | Django field | What they get |
|---|---|---|
| You (superadmin) | `is_superuser=True` | Full `/admin/` + can unlock confirmed hiring records |
| HR / Manager | `is_accountant=True` | Full HRMS dashboard: employees, payroll, leave approvals, hiring, etc. |
| Employee | `is_viewer=True` (default) | Self-service: own profile, attendance, leave, payslips, assets, reviews |

```python
from inventory.models import User
hr = User.objects.create_user(username='hr_priya', password='...', is_accountant=True, is_viewer=False)
```

Enforcement lives in `hrms/permissions.py`: `HRRequiredMixin`, `SuperAdminRequiredMixin`,
`EmployeeSelfOrHRMixin`. Every template gets `hrms_role` / `hrms_is_hr` via
`hrms/context_processors.py`.

## 6. Model extensions beyond your original models.py

Your ER diagram's own note says "fields listed are key fields only, not
exhaustive" — these fill genuine gaps where the business logic you asked for
couldn't be computed from the original schema:

- **`PaySlip`**: added `full_days`, `half_days`, `comp_off_days`, `off_days`,
  `paid_leave_days`, `absent_days`, `paid_days`, `total_days_in_month`,
  `daily_wage`, `penalty_deduction`, `loan_deduction`, `extra_earning` — so
  the payroll summary table and payslip PDF can show the full computation.
- **`LoanAdvance`** (new): an employee's running loan/advance —
  `monthly_installment` auto-deducted each payroll run until `remaining_balance`
  hits zero.
- **`PayrollExtra`** (new): a queued one-off incentive/bonus, applied to the
  employee's *next* payroll run then marked consumed.
- **`Application`**: added `is_locked`, `locked_by`, `locked_at`. Your
  `Application.Status` has no separate "Confirmed" state, so this treats
  reaching either terminal outcome — **Hired** or **Rejected** — as the
  "confirmed" point that triggers locking.
- **`RecruitmentAuditLog`** (new): `application`, `from_status`, `to_status`,
  `action`, `performed_by` (the `User` who did it), `note`, `created_at`.

Run `makemigrations hrms` to pick all of these up.

## 7. Business logic modules (framework-agnostic, kept out of views.py)

- `attendance_logic.py` — the "3-strike" grace-window rule: on time / within
  grace (forgiven up to N times/month) / beyond grace (automatic Half-Day).
  `check_in()`, `check_out()`, `evaluate_arrival()`.
- `leave_logic.py` — `apply_leave()` enforces the balance-sufficiency check
  (rejects if `total_days > LeaveBalance.available`) before ever creating a
  `LeaveApplication`, and reserves the days (`pending`) until approved/rejected.
  `approve_leave()` / `reject_leave()` are only reachable through HR-gated views.
- `payroll_logic.py` — `process_payroll()` loops every active employee:
  Daily Wage = `(ctc_annual/12) / days_in_month`; attendance-based Paid Days
  (Full + Half×0.5 + Comp-Off + Paid Leave + unworked weekly-offs/holidays);
  Net Absent × Daily Wage as a penalty deduction; Net Pay = Earnings −
  Deductions − Penalty. **One documented deviation**: unworked Sundays/Holidays
  count as paid `off_days`, not absences — otherwise every employee would show
  ~8 "absent" days a month just for weekends, making the penalty meaningless.
- `hiring_logic.py` — the pipeline: `shortlist_application()`,
  `reject_application()`, `schedule_interview()`, **`move_to_offer()`** (the
  flow explicitly asked for — drafts an `OfferLetter`, flips the `Application`
  to `OFFERED`), `send_offer()`, `accept_offer()`, `decline_offer()`,
  `expire_offer()`, `convert_to_employee()`. Every mutating action:
  1. calls `ensure_unlocked()` first — raises `HiringError` unless the acting
     user `is_superuser`, once the record is locked;
  2. writes a `RecruitmentAuditLog` row recording who did what;
  3. auto-locks the `Application` the moment it reaches Hired or Rejected.
  `convert_to_employee()` is deliberately exempt from the lock check — locking
  protects the *recruitment decision*, not the onboarding step that follows it.
- `email_logic.py` — dual notifications, fired automatically by
  `schedule_interview()` and `send_offer()`:
  - Interview scheduled → candidate gets an invite (round/date/time/mode),
    the assigned interviewer gets an assignment notice with candidate contact info.
  - Offer sent → candidate gets the offer (CTC/joining date/expiry), the
    hiring manager (the application's most recent interviewer, standing in
    for "the manager") gets a heads-up.
  - Uses plain `send_mail()` against whatever `EMAIL_BACKEND` you have
    configured (console in dev, SES in your settings.py). Each send is
    isolated in a try/except — a bad SMTP config surfaces as a Django warning
    message, it doesn't 500 the request. Upgrading to HTML templates via
    `EmailMultiAlternatives` + `render_to_string` is a natural next step.

## 8. Module-by-module view/template summary

**Organisation + Employee**: full CRUD for Company (Super Admin only)/Department/
Designation/Employee, plus Bank Detail/Documents/Notices inline on the employee
detail page. Employees see only their own profile via `MyProfileView`.

**Attendance**: `AttendancePolicyListView`/`FormView` (one policy per company),
`AttendanceRecordListView` (HR, filterable) + manual entry that auto-applies
the grace rules, `MyAttendanceView` with Check In/Out buttons, `HolidayListView`.

**Leave**: `LeaveTypeListView` (HR), `LeaveBalanceListView` (HR sees all,
employee sees own) + HR allocation, `LeaveApplicationListView` (`my_leave`)
with inline Approve/Reject (HR-only) and Cancel (self or HR).

**Payroll**: `SalaryStructureListView`/`DetailView` (+ inline `SalaryComponent`
management), `EmployeeSalaryListView` (**must be configured per employee before
payroll can run** — unconfigured employees are skipped with a warning, not
given ₹0), `LoanAdvanceListView`, `PayrollExtraListView`, `PayrollRunListView`,
**`PayrollProcessView`** (role-gated to `is_accountant or is_superuser`
explicitly, not just the general HR mixin) rendering the Bootstrap 5 summary
table (Name/Designation/Salary/Full Days/Half Days/Absent/Comp-Off/Paid
Leaves/Loan Ded./Extra/Net Payable), `PaySlipListView` + `PaySlipPDFView`
(ReportLab — pure-Python, no system dependency).

**Hiring**: `JobPostingListView`, `CandidateListView`/`DetailView`,
`ApplicationListView`/`DetailView` (the pipeline hub — status buttons, audit
trail timeline, lock badge + Unlock for Super Admin), `InterviewCreateView`
(fires the dual email), `OfferLetterCreateView` (the "move to Offer" flow),
`OfferLetterActionView` (Send/Accept/Decline/Expire), `ConvertToEmployeeView`.
Extra addition: `MyInterviewsView`/`InterviewFeedbackView` — any employee
assigned as an interviewer (not just HR) can see their schedule and submit
feedback, since interviewers are often line managers without HR access.

**Assets**: `AssetListView` (HR sees all + status filter, employee sees only
their own), HR-only Create/Update, and a **Return** quick-action that
unassigns and flips status back to Available.

**Performance**: `PerformanceReviewListView` (HR sees all + employee filter,
employee sees own), HR-only Create/Update, self-service **Acknowledge** action
moving a Submitted review to Acknowledged.

## 9. Natural next steps (not built, worth knowing about)

- **HTML email templates** — current notifications are plain text.
- **Company scoping** — HR currently sees all companies' data in one view;
  fine for single-company use, worth restricting explicitly for multi-tenant.
- **DRF endpoints** — every `*_logic.py` module is framework-agnostic (no
  Django view/request coupling), so wrapping them in DRF serializers/viewsets
  for a mobile app or SPA later is a clean addition, not a rewrite.
