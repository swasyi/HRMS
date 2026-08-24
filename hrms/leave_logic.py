"""
Leave management business logic — kept out of views.py, same pattern as
attendance_logic.py.

Rules implemented:

APPLY
  - total_days = inclusive day-count between start_date and end_date.
  - Rejected outright (LeaveError) if:
      * end_date < start_date
      * leave_type.max_consecutive_days is set and exceeded
      * leave_type.min_consecutive_days is set and not met (e.g., SL requires 2+ days)
      * leave_type.applicable_gender doesn't match the employee's gender
      * leave_type.requires_document and no supporting_document provided
      * leave_type.requires_relationship and no relationship provided
      * leave_type.requires_stage and no leave_stage provided
      * the employee's balance is insufficient
  - On success: creates a PENDING LeaveApplication, routes based on hierarchy.

MANAGER APPROVE
  - Moves PENDING_MANAGER → PENDING_HR. Records manager_approved_by/on.

HR APPROVE / REJECT
  - Approve: deducts from EmployeeLeaveBalanceLive, creates attendance records.
  - Reject: mandatory rejection reason, refunds if previously approved.

CANCEL
  - Atomic rollback of leave balance. Works for PENDING and APPROVED.

PENALTY
  - Full-Time: CL → EL → LWP cascade (0.5 days).
  - Intern: Direct salary deduction (half-day wage).

REAPPLY
  - Creates new application linked to rejected parent via parent_application.

AUDIT
  - Every action creates a LeaveApprovalLog entry.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import models as m


class LeaveError(Exception):
    """Raised for any invalid leave action — insufficient balance, bad dates, etc."""


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def get_or_create_balance(employee, leave_type, year):
    balance, _ = m.LeaveBalance.objects.get_or_create(
        employee=employee, leave_type=leave_type, year=year,
        defaults={'allocated': leave_type.days_per_year},
    )
    return balance


def calculate_total_days(start_date, end_date, day_type='full'):
    if end_date < start_date:
        raise LeaveError('End date cannot be before start date.')
    if day_type == 'half':
        if start_date != end_date:
            raise LeaveError(
                'Half-day leave can only be applied for a single day (Start and End date must be the same).')
        return Decimal('0.5')
    return Decimal((end_date - start_date).days + 1)


def get_balance_formatted(employee, leave_type):
    """Returns formatted balance string like 'CL (1.5/2.0)' for display."""
    try:
        bank = m.EmployeeLeaveBalance.objects.get(e_name=employee)
        live = m.EmployeeLeaveBalanceLive.objects.get(e_name=employee)
    except (m.EmployeeLeaveBalance.DoesNotExist, m.EmployeeLeaveBalanceLive.DoesNotExist):
        return f"{leave_type.code} (0/0)"

    field_map = {
        'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
        'ML': 'menstrual_leave', 'MTL': 'menstrual_leave',
        'BL': 'bereavement_leave', 'CO': 'comp_off',
    }
    field = field_map.get(leave_type.code.upper())
    if field:
        total = getattr(bank, field, 0)
        remaining = getattr(live, field, 0)
        return f"{leave_type.code} ({remaining}/{total})"
    return f"{leave_type.code}"


def _log_action(application, action, performed_by=None, remarks=''):
    """Create an audit log entry for a leave action."""
    m.LeaveApprovalLog.objects.create(
        application=application,
        action=action,
        performed_by=performed_by,
        remarks=remarks,
    )


# ---------------------------------------------------------------------------
# APPLY LEAVE
# ---------------------------------------------------------------------------
def apply_leave(employee, leave_type, start_date, end_date, day_type='full',
                reason='', supporting_document=None, relationship='',
                leave_stage='', parent_application=None):
    """
    Apply for leave with full validation and routing logic.
    """
    # 1. Force dates for Half Day
    if day_type == 'half':
        end_date = start_date
        total_days = Decimal('0.5')
    else:
        total_days = calculate_total_days(start_date, end_date, day_type)

    # 2. INTERN/TRAINEE LOGIC: Switch to LWP
    if employee.employment_type in ['intern', 'trainee']:
        if leave_type.is_paid:
            lwp_type = m.LeaveType.objects.filter(code='LWP', company=employee.company).first()
            if lwp_type:
                leave_type = lwp_type

    # 3. Consecutive day limits
    if leave_type.max_consecutive_days and total_days > leave_type.max_consecutive_days:
        raise LeaveError(
            f'{leave_type.name} cannot be applied for more than '
            f'{leave_type.max_consecutive_days} consecutive day(s).'
        )

    # 4. Minimum consecutive days (e.g., SL requires 2+ days)
    if leave_type.min_consecutive_days and total_days < leave_type.min_consecutive_days:
        raise LeaveError(
            f'{leave_type.name} requires a minimum of '
            f'{leave_type.min_consecutive_days} consecutive day(s).'
        )

    # 5. Gender applicability
    if (leave_type.applicable_gender != m.LeaveType.Gender.ALL
            and employee.gender != leave_type.applicable_gender):
        raise LeaveError(f'{leave_type.name} is not applicable to your profile.')

    # 6. Document requirement
    if leave_type.requires_document and not supporting_document:
        raise LeaveError(f'{leave_type.name} requires a supporting document (medical certificate, etc.).')

    # 7. Relationship requirement (Bereavement Leave)
    if leave_type.requires_relationship and not relationship:
        raise LeaveError(f'{leave_type.name} requires you to specify the relationship (e.g., Mother, Father).')

    # 8. Stage requirement (Maternity/Paternity)
    if leave_type.requires_stage and not leave_stage:
        raise LeaveError(f'{leave_type.name} requires you to specify the leave stage (Pre-Natal or Post-Natal).')

    # 9. Balance check — auto-shift to LWP if CL/EL exhausted
    if leave_type.is_paid:
        live_report, _ = m.EmployeeLeaveBalanceLive.objects.get_or_create(e_name=employee)
        field_map = {
            'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
            'ML': 'menstrual_leave', 'MTL': 'menstrual_leave',
            'BL': 'bereavement_leave', 'CO': 'comp_off',
        }
        field = field_map.get(leave_type.code.upper())
        if field:
            available = getattr(live_report, field, 0)
            if float(total_days) > float(available):
                # Auto-shift to LWP if CL or EL is exhausted
                if leave_type.code.upper() in ('CL', 'EL'):
                    lwp_type = m.LeaveType.objects.filter(code='LWP', company=employee.company).first()
                    if lwp_type:
                        leave_type = lwp_type
                    else:
                        raise LeaveError(f'Insufficient balance: {available} days left for {leave_type.name}.')
                else:
                    raise LeaveError(f'Insufficient balance: {available} days left for {leave_type.name}.')

    # 10. Routing logic
    if employee.reporting_manager:
        # If the employee IS a manager themselves, bypass Level 1 → go direct to HR
        if employee.is_manager and not employee.reporting_manager:
            initial_status = m.LeaveApplication.Status.PENDING_HR
        else:
            initial_status = m.LeaveApplication.Status.PENDING_MANAGER
    else:
        initial_status = m.LeaveApplication.Status.PENDING_HR

    # 11. SLA deadline (48h from now)
    sla_deadline = timezone.now() + timezone.timedelta(hours=48)

    application = m.LeaveApplication.objects.create(
        employee=employee,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        day_type=day_type,
        total_days=total_days,
        reason=reason,
        status=initial_status,
        supporting_document=supporting_document,
        relationship=relationship,
        leave_stage=leave_stage,
        parent_application=parent_application,
        sla_deadline=sla_deadline,
    )

    # 12. Audit log
    _log_action(application, 'applied', performed_by=employee.user, remarks=reason)

    # 13. Email notification
    try:
        from . import email_logic
        email_logic.send_leave_application_email(application)
    except Exception:
        pass

    return application


# ---------------------------------------------------------------------------
# MANAGER APPROVE
# ---------------------------------------------------------------------------
def manager_approve_leave(application, approver_user):
    """
    Manager approves leave from their subordinate:
    Advances status from PENDING_MANAGER to PENDING_HR.
    Records separate manager_approved_by/on fields.
    """
    if application.status != m.LeaveApplication.Status.PENDING_MANAGER:
        raise LeaveError('Only applications pending manager approval can be approved by a manager.')

    application.status = m.LeaveApplication.Status.PENDING_HR
    application.manager_approved_by = approver_user
    application.manager_approved_on = timezone.now()
    # Reset SLA deadline for HR review (48h from now)
    application.sla_deadline = timezone.now() + timezone.timedelta(hours=48)
    application.save(update_fields=[
        'status', 'manager_approved_by', 'manager_approved_on', 'sla_deadline', 'updated_at'
    ])

    _log_action(application, 'manager_approved', performed_by=approver_user,
                remarks='Manager approved — escalated to HR for final review.')
    return application


# ---------------------------------------------------------------------------
# HR APPROVE
# ---------------------------------------------------------------------------
def approve_leave(application, approver_user):
    """
    HR / SuperAdmin approves leave:
    1. Updates Status to Approved.
    2. Deducts leaves from EmployeeLeaveBalanceLive.
    3. Automatically creates Attendance Records as 'ON_LEAVE'.
    """
    if application.status not in [
        m.LeaveApplication.Status.PENDING,
        m.LeaveApplication.Status.PENDING_HR,
        m.LeaveApplication.Status.PENDING_MANAGER
    ]:
        raise LeaveError('Only pending applications can be approved.')

    # STEP 1: Deduct from Leave Balance (Live Report)
    msg = adjust_live_balance(
        employee=application.employee,
        amount=application.total_days,
        action="deduct",
        leave_code=application.leave_type.code
    )

    # STEP 2: Create Attendance Records
    from datetime import timedelta
    current_date = application.start_date
    while current_date <= application.end_date:
        m.AttendanceRecord.objects.update_or_create(
            employee=application.employee,
            attendance_date=current_date,
            defaults={
                'status': m.AttendanceRecord.Status.ON_LEAVE,
                'remarks': f"{application.leave_type.code.upper()} | {msg}",
            }
        )
        current_date += timedelta(days=1)

    # STEP 3: Update Application Status
    application.status = m.LeaveApplication.Status.APPROVED
    application.approved_by = approver_user
    application.approved_on = timezone.now()
    application.save()

    _log_action(application, 'hr_approved', performed_by=approver_user,
                remarks=f'HR final approval. Balance: {msg}')
    return f"Success: {msg}"


# ---------------------------------------------------------------------------
# REJECT LEAVE
# ---------------------------------------------------------------------------
def reject_leave(application, approver_user, reason=''):
    """
    1. Updates Status to Rejected and records rejection_reason.
    2. If it was already approved, it refunds leaves to 'Leave Balance'.
    3. Resets Attendance records to ABSENT if previously ON_LEAVE.
    """
    from datetime import timedelta

    if application.status == m.LeaveApplication.Status.APPROVED:
        adjust_live_balance(
            employee=application.employee,
            amount=application.total_days,
            action="refund",
            leave_code=application.leave_type.code
        )
        m.AttendanceRecord.objects.filter(
            employee=application.employee,
            attendance_date__range=[application.start_date, application.end_date],
            status=m.AttendanceRecord.Status.ON_LEAVE
        ).update(status=m.AttendanceRecord.Status.ABSENT, remarks="Leave Rejected/Cancelled")

    # Determine action type for audit log
    if application.status == m.LeaveApplication.Status.PENDING_MANAGER:
        action_type = 'manager_rejected'
    else:
        action_type = 'hr_rejected'

    application.status = m.LeaveApplication.Status.REJECTED
    application.rejection_reason = reason
    application.approved_by = approver_user
    application.save()

    _log_action(application, action_type, performed_by=approver_user, remarks=reason)
    return application


# ---------------------------------------------------------------------------
# CANCEL LEAVE (Atomic)
# ---------------------------------------------------------------------------
@transaction.atomic
def cancel_leave(application, requested_by_employee, is_hr=False):
    """
    Cancel a leave application with atomic balance rollback.
    - Pending: releases the pending reservation.
    - Approved: refunds the consumed days.
    """
    cancellable_statuses = (
        m.LeaveApplication.Status.PENDING,
        m.LeaveApplication.Status.PENDING_MANAGER,
        m.LeaveApplication.Status.PENDING_HR,
        m.LeaveApplication.Status.APPROVED,
    )
    if application.status not in cancellable_statuses:
        raise LeaveError('This application can no longer be cancelled.')
    if not is_hr and application.employee != requested_by_employee:
        raise LeaveError('You can only cancel your own leave applications.')

    # Refund if previously approved
    if application.status == m.LeaveApplication.Status.APPROVED:
        adjust_live_balance(
            employee=application.employee,
            amount=application.total_days,
            action="refund",
            leave_code=application.leave_type.code
        )
        # Reset attendance records
        m.AttendanceRecord.objects.filter(
            employee=application.employee,
            attendance_date__range=[application.start_date, application.end_date],
            status=m.AttendanceRecord.Status.ON_LEAVE
        ).update(status=m.AttendanceRecord.Status.ABSENT, remarks="Leave Cancelled")

    application.status = m.LeaveApplication.Status.CANCELLED
    application.save()

    performer = requested_by_employee.user if requested_by_employee and hasattr(requested_by_employee, 'user') else None
    _log_action(application, 'cancelled', performed_by=performer,
                remarks='Cancelled by employee' if not is_hr else 'Cancelled by HR')
    return application


# ---------------------------------------------------------------------------
# REAPPLY LEAVE (after rejection)
# ---------------------------------------------------------------------------
def reapply_leave(original_application, employee, reason='', **kwargs):
    """
    Creates a new leave application linked to the rejected parent.
    Maintains full thread continuity.
    """
    if original_application.status != m.LeaveApplication.Status.REJECTED:
        raise LeaveError('You can only re-apply for rejected leave applications.')
    if original_application.employee != employee:
        raise LeaveError('You can only re-apply for your own leave applications.')

    new_app = apply_leave(
        employee=employee,
        leave_type=original_application.leave_type,
        start_date=kwargs.get('start_date', original_application.start_date),
        end_date=kwargs.get('end_date', original_application.end_date),
        day_type=kwargs.get('day_type', original_application.day_type),
        reason=reason or original_application.reason,
        supporting_document=kwargs.get('supporting_document', original_application.supporting_document),
        relationship=kwargs.get('relationship', original_application.relationship),
        leave_stage=kwargs.get('leave_stage', original_application.leave_stage),
        parent_application=original_application,
    )

    _log_action(new_app, 'reapplied', performed_by=employee.user,
                remarks=f'Re-applied after rejection of #{original_application.pk}. New reason: {reason}')
    return new_app


# ---------------------------------------------------------------------------
# PENALTY DEDUCTION (Full-Time vs Intern)
# ---------------------------------------------------------------------------
def apply_late_penalty_deduction(record):
    """
    Handles late arrival penalties:
    - Full-Time: Deducts 0.5 days from CL → EL → LWP.
    - Intern: Direct half-day salary deduction.
    Logs the penalty in AttendancePenalty.
    """
    if not record or not record.employee:
        return None

    # Check if penalty already logged for this record
    existing = m.AttendancePenalty.objects.filter(
        employee=record.employee,
        penalty_date=record.attendance_date
    ).first()
    if existing:
        return existing

    employee = record.employee
    employment_type = employee.employment_type
    amount = Decimal('0.5')

    if employment_type == 'intern':
        # INTERN: Calculate half-day salary deduction
        salary_deduction = Decimal('0')
        try:
            emp_salary = m.EmployeeSalary.objects.filter(
                employee=employee, is_active=True
            ).first()
            if emp_salary:
                daily_wage = emp_salary.ctc_annual / Decimal('365')
                salary_deduction = (daily_wage / 2).quantize(Decimal('0.01'))
        except Exception:
            pass

        penalty = m.AttendancePenalty.objects.create(
            employee=employee,
            attendance_record=record,
            penalty_date=record.attendance_date,
            reason=f"Late Arrival ({record.late_minutes} mins late - Exceeded Grace)",
            late_minutes=record.late_minutes,
            deduction_days=amount,
            deduction_source="Intern Salary Deduction",
            status=m.AttendancePenalty.DeductionStatus.INTERN_SALARY,
            is_intern_penalty=True,
            salary_deduction_amount=salary_deduction,
            employment_type_snapshot=employment_type,
        )
        return penalty

    # FULL-TIME: CL → EL → LWP cascade
    live_report, _ = m.EmployeeLeaveBalanceLive.objects.get_or_create(e_name=employee)
    deduction_source = "LWP"
    penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED

    if live_report.casual_leave >= float(amount):
        live_report.casual_leave -= float(amount)
        live_report.save(update_fields=['casual_leave'])
        deduction_source = "Deducted 0.5 from CL"
    elif live_report.earned_leave >= float(amount):
        live_report.earned_leave -= float(amount)
        live_report.save(update_fields=['earned_leave'])
        deduction_source = "Deducted 0.5 from EL"
    else:
        deduction_source = "LWP (Insufficient Balance)"
        penalty_status = m.AttendancePenalty.DeductionStatus.LWP

    penalty = m.AttendancePenalty.objects.create(
        employee=employee,
        attendance_record=record,
        penalty_date=record.attendance_date,
        reason=f"Late Arrival ({record.late_minutes} mins late - Exceeded Grace)",
        late_minutes=record.late_minutes,
        deduction_days=amount,
        deduction_source=deduction_source,
        status=penalty_status,
        is_intern_penalty=False,
        employment_type_snapshot=employment_type,
    )
    return penalty


# ---------------------------------------------------------------------------
# LIVE BALANCE ADJUSTMENT (Consolidated — single source of truth)
# ---------------------------------------------------------------------------
def adjust_live_balance(employee, amount, action="deduct", leave_code=None):
    """
    Central function to deduct/refund leaves from EmployeeLeaveBalanceLive.
    Does NOT touch EmployeeLeaveBalance (Bank).
    """
    live_report, _ = m.EmployeeLeaveBalanceLive.objects.get_or_create(e_name=employee)
    amount = float(amount)

    field_map = {
        'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
        'ML': 'menstrual_leave', 'MTL': 'menstrual_leave',
        'BL': 'bereavement_leave', 'CO': 'comp_off',
    }

    if action == "refund" and leave_code:
        field = field_map.get(leave_code.upper())
        if field:
            setattr(live_report, field, getattr(live_report, field) + amount)
            live_report.save()
            return "Refunded to Live Balance"

    elif action == "deduct":
        field = field_map.get(leave_code.upper()) if leave_code else None

        # 1. Try Specific Field
        if field and getattr(live_report, field) >= amount:
            setattr(live_report, field, getattr(live_report, field) - amount)
            live_report.save()
            return f"Deducted from {leave_code}"

        # 2. Priority Fallback (CL → EL)
        if live_report.casual_leave >= amount:
            live_report.casual_leave -= amount
            live_report.save()
            return "Deducted from CL (Fallback)"
        elif live_report.earned_leave >= amount:
            live_report.earned_leave -= amount
            live_report.save()
            return "Deducted from EL (Fallback)"

        return "LWP"

    return "No action"


# ---------------------------------------------------------------------------
# LEAVE BANK ADJUSTMENT (Consolidated — single source of truth)
# ---------------------------------------------------------------------------
def adjust_leave_bank(employee, amount, action="deduct", leave_code=None):
    """
    Central logic to adjust EmployeeLeaveBalanceLive (the live report).
    Priority for deduction: Specific Code → CL → EL → LWP
    """
    return adjust_live_balance(employee, amount, action, leave_code)


# ---------------------------------------------------------------------------
# REFUND ON PUNCH
# ---------------------------------------------------------------------------
def refund_leave_on_punch(employee, attendance_date, attendance_status):
    """
    Checks if there's an approved leave for this date.
    If yes, refunds the appropriate amount.
    """
    leave = m.LeaveApplication.objects.filter(
        employee=employee,
        status='approved',
        start_date__lte=attendance_date,
        end_date__gte=attendance_date
    ).first()

    if not leave:
        return False

    refund_amount = 0
    if attendance_status == 'FD':
        refund_amount = 0.5 if leave.day_type == 'half' else 1.0
    elif attendance_status == 'HD':
        refund_amount = 0.5

    if refund_amount > 0:
        adjust_live_balance(employee, refund_amount, action="refund", leave_code=leave.leave_type.code)
        return True
    return False


# ---------------------------------------------------------------------------
# LEAVE BANK SYNC (Consolidated)
# ---------------------------------------------------------------------------
def get_financial_year(target_date=None):
    """
    Returns (fy_start_date, fy_end_date, fy_year_int) for the given date (default today).
    Indian Financial Year: April 1 to March 31.
    """
    from datetime import date as dt_date
    d = target_date or dt_date.today()
    if d.month >= 4:
        fy_start = dt_date(d.year, 4, 1)
        fy_end = dt_date(d.year + 1, 3, 31)
        fy_year = d.year
    else:
        fy_start = dt_date(d.year - 1, 4, 1)
        fy_end = dt_date(d.year, 3, 31)
        fy_year = d.year - 1
    return fy_start, fy_end, fy_year


def sync_employee_leave_bank(employee, target_date=None):
    """
    Calculates annual leave entitlement based on Office Rules:
    - Intern Male: 0
    - Intern Female: 2 Menstrual, 0 others
    - Full Time: Fixed SL/BL, Pro-rated CL/EL based on Confirmation Date within the active FY.
    """
    bank, _ = m.EmployeeLeaveBalance.objects.get_or_create(e_name=employee)

    def get_days(code):
        return float(m.LeaveType.objects.filter(
            code=code, company=employee.company
        ).values_list('days_per_year', flat=True).first() or 0)

    base_sl = get_days('SL')
    base_bl = get_days('BL')
    base_cl = get_days('CL')
    base_el = get_days('EL')
    base_mtl = get_days('MTL')

    # INTERNS
    if employee.employment_type == 'intern':
        bank.sick_leave = 0.0
        bank.bereavement_leave = 0.0
        bank.casual_leave = 0.0
        bank.earned_leave = 0.0
        bank.comp_off = 0.0
        bank.menstrual_leave = 2.0 if employee.gender == 'F' else 0.0
        bank.save()
        return

    # FULL TIME
    conf_date = employee.date_of_confirmation
    if not conf_date:
        return

    # Dynamic Financial Year (April 1 to March 31)
    fy_start, fy_end, _ = get_financial_year(target_date)

    bank.sick_leave = base_sl
    bank.bereavement_leave = base_bl
    bank.menstrual_leave = base_mtl if employee.gender == 'F' else 0.0

    if conf_date <= fy_start:
        months_left = 12
    elif conf_date > fy_end:
        months_left = 0
    else:
        months_left = (fy_end.year - conf_date.year) * 12 + (fy_end.month - conf_date.month) + 1
        months_left = min(max(months_left, 0), 12)

    bank.casual_leave = round((base_cl / 12.0) * months_left, 1)
    bank.earned_leave = round((base_el / 12.0) * months_left, 1)
    bank.save()


# ---------------------------------------------------------------------------
# HOLIDAY HELPERS
# ---------------------------------------------------------------------------
def get_employee_holidays(employee, start_date, end_date):
    """Returns a list of holiday dates for a specific employee."""
    if not employee.holiday_calendar:
        return []
    return m.Holiday.objects.filter(
        calendar=employee.holiday_calendar,
        date__range=[start_date, end_date]
    ).values_list('date', flat=True)


def is_holiday(employee, target_date):
    """Checks if a specific date is a holiday for this employee."""
    if not employee.holiday_calendar:
        return False
    return m.Holiday.objects.filter(
        calendar=employee.holiday_calendar,
        date=target_date
    ).exists()