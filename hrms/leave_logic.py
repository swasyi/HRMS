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
# 1. DYNAMIC MODEL HELPERS (ZERO HARDCODED VALUES)
# ---------------------------------------------------------------------------
def get_leave_type_annual_quota(leave_type):
    """Dynamically reads the yearly quota from whatever field exists on LeaveType."""
    for attr in ('days_per_year', 'annual_quota', 'days_allowed', 'quota', 'max_days', 'days'):
        if hasattr(leave_type, attr):
            val = getattr(leave_type, attr)
            if val is not None:
                return Decimal(str(val))
    return Decimal('0.0')


def calculate_monthly_quota(leave_type):
    """Calculates monthly quota dynamically (Annual / 12) from model database record."""
    annual = get_leave_type_annual_quota(leave_type)
    code = (leave_type.code or '').upper().strip()
    norm_name = leave_type.name.lower()

    # Emergency / Block leaves are not divided by 12
    if code in ('SL', 'BL', 'MATERNITY', 'PATERNITY', 'MTL', 'PL') or any(
        k in norm_name for k in ('sick', 'bereave', 'breave', 'matern', 'patern')
    ):
        return annual

    if annual > Decimal('0.0'):
        raw_monthly = annual / Decimal('12.0')
        # Rounded to 1 decimal place, minimum 1.0 day if quota exists
        return round(raw_monthly, 1) if raw_monthly > Decimal('1.0') else Decimal('1.0')
    return Decimal('0.0')

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





def _sync_bank_balance(employee, leave_type, days, action='deduct'):
    """
    Directly updates the Master Leave Bank (EmployeeLeaveBalance) so
    the cards and tables update in real-time.
    """
    if not employee or not leave_type or days <= Decimal('0.0'):
        return

    bank = m.EmployeeLeaveBalance.objects.filter(e_name=employee).first()
    if not bank:
        return

    code = (leave_type.code or '').upper().strip()
    norm_name = leave_type.name.lower()

    field_map = {
        'CL': 'casual_leave',
        'EL': 'earned_leave',
        'SL': 'sick_leave',
        'BL': 'bereavement_leave',
        'ML': 'menstrual_leave',
        'MTL': 'menstrual_leave',
        'CO': 'comp_off',
    }
    target_field = field_map.get(code)
    if not target_field:
        for candidate in (
            'maternity_leave', 'paternity_leave',
            norm_name.replace(' ', '_') + '_leave',
            norm_name.replace(' ', '_')
        ):
            if hasattr(bank, candidate):
                target_field = candidate
                break

    if target_field and hasattr(bank, target_field):
        current_val = Decimal(str(getattr(bank, target_field) or 0.0))
        if action == 'deduct':
            new_val = max(Decimal('0.0'), current_val - days)
        elif action == 'refund':
            new_val = current_val + days
        else:
            return

        setattr(bank, target_field, new_val)
        bank.save(update_fields=[target_field])


def calculate_total_days(start_date, end_date, day_type='full'):
    if end_date < start_date:
        raise LeaveError('End date cannot be before start date.')
    if day_type == 'half':
        if start_date != end_date:
            raise LeaveError('Half-day leave must have the same start and end date.')
        return Decimal('0.5')
    return Decimal(str((end_date - start_date).days + 1))


def _log_action(application, action, performed_by=None, remarks=''):
    if hasattr(m, 'LeaveApprovalLog'):
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


from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from hrms import models as m



def _adjust_employee_leave_bank(employee, leave_type, days, action='deduct'):
    """Synchronizes balance deduction/refund directly on EmployeeLeaveBalance."""
    bank = m.EmployeeLeaveBalance.objects.filter(e_name=employee).first()
    if not bank or not leave_type:
        return

    days = Decimal(str(days or 0.0))
    if days <= Decimal('0.0'):
        return

    code = (leave_type.code or '').upper().strip()
    norm_name = leave_type.name.lower()

    field_map = {
        'CL': 'casual_leave',
        'EL': 'earned_leave',
        'SL': 'sick_leave',
        'BL': 'bereavement_leave',
        'ML': 'menstrual_leave',
        'MTL': 'menstrual_leave',
        'CO': 'comp_off',
    }
    target_field = field_map.get(code)
    if not target_field:
        for candidate in (
            'maternity_leave',
            'paternity_leave',
            norm_name.replace(' ', '_') + '_leave',
            norm_name.replace(' ', '_'),
        ):
            if hasattr(bank, candidate):
                target_field = candidate
                break

    if target_field and hasattr(bank, target_field):
        current_val = Decimal(str(getattr(bank, target_field) or 0.0))
        if action == 'deduct':
            new_val = max(Decimal('0.0'), current_val - days)
        elif action == 'refund':
            new_val = current_val + days
        else:
            return

        setattr(bank, target_field, new_val)
        bank.save(update_fields=[target_field])


def approve_leave(application, approver_user):
    """Approves leave: deducts days from master Leave Bank, updates attendance and status."""
    valid_pending = [
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
    ]
    if application.status not in valid_pending:
        raise LeaveError('Only pending applications can be approved.')

    with transaction.atomic():
        emp = application.employee
        days = Decimal(str(application.total_days or 0.0))
        code = (application.leave_type.code or '').upper().strip()

        # 1. Deduct from Master Leave Bank
        _adjust_employee_leave_bank(emp, application.leave_type, days, action='deduct')

        # 2. Deduct from Live Balance
        if 'adjust_live_balance' in globals():
            try:
                adjust_live_balance(emp, days, action='deduct', leave_code=code)
            except Exception:
                pass

        # 3. Create Daily Attendance Record
        is_half = getattr(application, 'day_type', 'full') == 'half'
        att_status = (
            getattr(m.AttendanceRecord.Status, 'HALF_DAY', m.AttendanceRecord.Status.ON_LEAVE)
            if is_half
            else m.AttendanceRecord.Status.ON_LEAVE
        )
        curr = application.start_date
        while curr <= application.end_date:
            m.AttendanceRecord.objects.update_or_create(
                employee=emp,
                attendance_date=curr,
                defaults={
                    'status': att_status,
                    'remarks': f"{code} Approved ({'Half Day' if is_half else 'Full Day'})",
                },
            )
            curr += timedelta(days=1)

        # 4. Finalize Status
        application.status = getattr(m.LeaveApplication.Status, 'APPROVED', 'approved')
        application.approved_by = approver_user
        application.approved_on = timezone.now()
        application.save(update_fields=['status', 'approved_by', 'approved_on'])

        if '_log_action' in globals():
            _log_action(application, 'hr_approved', performed_by=approver_user, remarks='Approved by HR/Admin')

    return application


def reject_leave(application, approver_user, reason=''):
    """Rejects leave: refunds master Leave Bank if previously approved, updates status."""
    with transaction.atomic():
        emp = application.employee
        days = Decimal(str(application.total_days or 0.0))
        code = (application.leave_type.code or '').upper().strip()

        # If it was previously approved, refund the days
        if application.status == getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'):
            _adjust_employee_leave_bank(emp, application.leave_type, days, action='refund')
            if 'adjust_live_balance' in globals():
                try:
                    adjust_live_balance(emp, days, action='refund', leave_code=code)
                except Exception:
                    pass

            m.AttendanceRecord.objects.filter(
                employee=emp,
                attendance_date__range=[application.start_date, application.end_date],
                status=m.AttendanceRecord.Status.ON_LEAVE,
            ).update(status=m.AttendanceRecord.Status.ABSENT, remarks='Leave Rejected/Cancelled')

        action_type = (
            'manager_rejected'
            if application.status == getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager')
            else 'hr_rejected'
        )

        application.status = getattr(m.LeaveApplication.Status, 'REJECTED', 'rejected')
        application.rejection_reason = reason
        application.approved_by = approver_user
        application.save()

        if '_log_action' in globals():
            _log_action(application, action_type, performed_by=approver_user, remarks=reason)

    return application
@transaction.atomic
def cancel_leave(application, requested_by_employee, is_hr=False):
    """Cancels leave: releases pending reservation or refunds approved master bank."""
    cancellable = (
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'),
    )
    if application.status not in cancellable:
        raise LeaveError('This application can no longer be cancelled.')

    if not is_hr and application.employee != requested_by_employee:
        raise LeaveError('You can only cancel your own leave applications.')

    emp = application.employee
    days = Decimal(str(application.total_days or 0.0))
    code = (application.leave_type.code or '').upper().strip()

    # Refund if previously approved
    if application.status == getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'):
        _adjust_employee_leave_bank(emp, application.leave_type, days, action='refund')
        if 'adjust_live_balance' in globals():
            try:
                adjust_live_balance(emp, days, action='refund', leave_code=code)
            except Exception:
                pass

        m.AttendanceRecord.objects.filter(
            employee=emp,
            attendance_date__range=[application.start_date, application.end_date],
            status=m.AttendanceRecord.Status.ON_LEAVE,
        ).update(status=m.AttendanceRecord.Status.ABSENT, remarks='Leave Cancelled')

    application.status = getattr(m.LeaveApplication.Status, 'CANCELLED', 'cancelled')
    application.save()

    performer = getattr(requested_by_employee, 'user', None)
    if '_log_action' in globals():
        _log_action(application, 'cancelled', performed_by=performer, remarks='Cancelled by employee' if not is_hr else 'Cancelled by HR')

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
def apply_late_penalty_deduction(record, reason=None):
    """
    Handles late arrival penalties and leave deduction by delegating to services.
    - Full-Time: Deducts 0.5 days from CL → EL → LWP / Salary.
    - Intern: Direct half-day salary/stipend deduction.
    Logs the penalty in AttendancePenalty with full atomicity.
    """
    from .services import process_late_arrival_penalty
    return process_late_arrival_penalty(record, reason=reason)


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


# ---------------------------------------------------------------------------
# 1. APPLY LEAVE (Instantly Deducts from Bank)
# ---------------------------------------------------------------------------
@transaction.atomic
def apply_leave(employee, leave_type, start_date, end_date, day_type='full',
                reason='', supporting_document=None, relationship='',
                leave_stage='', parent_application=None):
    if day_type == 'half':
        end_date = start_date
        total_days = Decimal('0.5')
    else:
        total_days = calculate_total_days(start_date, end_date, day_type)

    # 1. Validate Balance
    bank = m.EmployeeLeaveBalance.objects.filter(e_name=employee).first()
    code = (leave_type.code or '').upper().strip()
    field_map = {
        'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
        'BL': 'bereavement_leave', 'ML': 'menstrual_leave', 'CO': 'comp_off',
    }
    target_field = field_map.get(code)
    if bank and target_field and hasattr(bank, target_field):
        current_bal = Decimal(str(getattr(bank, target_field) or 0.0))
        if total_days > current_bal and leave_type.is_paid:
            raise LeaveError(f'Insufficient balance: {current_bal} days remaining for {leave_type.name}.')

    # 2. Prevent Overlapping Leaves
    overlap = m.LeaveApplication.objects.filter(
        employee=employee,
        status__in=[
            getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
            getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
            getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
            getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'),
        ],
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists()
    if overlap:
        raise LeaveError('You already have a leave applied/approved for these dates.')

    # 3. Routing hierarchy
    if employee.reporting_manager:
        initial_status = m.LeaveApplication.Status.PENDING_MANAGER
    else:
        initial_status = m.LeaveApplication.Status.PENDING_HR

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
    )

    # 4. INSTANT DEDUCTION FROM LEAVE BANK
    if leave_type.is_paid:
        _sync_bank_balance(employee, leave_type, total_days, action='deduct')

    _log_action(application, 'applied', performed_by=getattr(employee, 'user', None), remarks=reason)
    return application


# ---------------------------------------------------------------------------
# 2. APPROVE LEAVE
# ---------------------------------------------------------------------------
@transaction.atomic
def approve_leave(application, approver_user):
    valid_pending = [
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
    ]
    if application.status not in valid_pending:
        raise LeaveError('Only pending applications can be approved.')

    emp = application.employee
    code = (application.leave_type.code or '').upper().strip()

    # Create Daily Attendance Records
    is_half = getattr(application, 'day_type', 'full') == 'half'
    att_status = (
        getattr(m.AttendanceRecord.Status, 'HALF_DAY', m.AttendanceRecord.Status.ON_LEAVE)
        if is_half else m.AttendanceRecord.Status.ON_LEAVE
    )
    curr = application.start_date
    while curr <= application.end_date:
        m.AttendanceRecord.objects.update_or_create(
            employee=emp,
            attendance_date=curr,
            defaults={
                'status': att_status,
                'remarks': f"{code} Approved ({'Half Day' if is_half else 'Full Day'})",
            }
        )
        curr += timedelta(days=1)

    application.status = getattr(m.LeaveApplication.Status, 'APPROVED', 'approved')
    application.approved_by = approver_user
    application.approved_on = timezone.now()
    application.save(update_fields=['status', 'approved_by', 'approved_on'])

    _log_action(application, 'hr_approved', performed_by=approver_user, remarks='Approved by HR/Admin')
    return application


# ---------------------------------------------------------------------------
# 3. REJECT LEAVE (Refunds Balance Back)
# ---------------------------------------------------------------------------
@transaction.atomic
def reject_leave(application, approver_user, reason=''):
    emp = application.employee
    days = Decimal(str(application.total_days or 0.0))

    # REFUND THE DEDUCTED DAYS BACK TO MASTER BANK
    if application.leave_type.is_paid and application.status != getattr(m.LeaveApplication.Status, 'REJECTED', 'rejected'):
        _sync_bank_balance(emp, application.leave_type, days, action='refund')

    # Reset attendance records if any existed
    m.AttendanceRecord.objects.filter(
        employee=emp,
        attendance_date__range=[application.start_date, application.end_date],
        status__in=[m.AttendanceRecord.Status.ON_LEAVE, getattr(m.AttendanceRecord.Status, 'HALF_DAY', 'HD')]
    ).delete()

    action_type = (
        'manager_rejected'
        if application.status == getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager')
        else 'hr_rejected'
    )
    application.status = getattr(m.LeaveApplication.Status, 'REJECTED', 'rejected')
    application.rejection_reason = reason
    application.approved_by = approver_user
    application.save()

    _log_action(application, action_type, performed_by=approver_user, remarks=reason)
    return application


# ---------------------------------------------------------------------------
# 4. CANCEL / WITHDRAW LEAVE (Refunds Balance Back)
# ---------------------------------------------------------------------------
@transaction.atomic
def cancel_leave(application, requested_by_employee, is_hr=False):
    cancellable = (
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'),
    )
    if application.status not in cancellable:
        raise LeaveError('This application can no longer be cancelled.')

    if not is_hr and application.employee != requested_by_employee:
        raise LeaveError('You can only cancel your own leave applications.')

    emp = application.employee
    days = Decimal(str(application.total_days or 0.0))

    # REFUND THE DEDUCTED DAYS BACK TO MASTER BANK
    if application.leave_type.is_paid:
        _sync_bank_balance(emp, application.leave_type, days, action='refund')

    m.AttendanceRecord.objects.filter(
        employee=emp,
        attendance_date__range=[application.start_date, application.end_date],
        status__in=[m.AttendanceRecord.Status.ON_LEAVE, getattr(m.AttendanceRecord.Status, 'HALF_DAY', 'HD')]
    ).delete()

    application.status = getattr(m.LeaveApplication.Status, 'CANCELLED', 'cancelled')
    application.save()

    performer = getattr(requested_by_employee, 'user', None)
    _log_action(application, 'cancelled', performed_by=performer, remarks='Withdrawn / Cancelled')
    return application


from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from . import models as m


class LeaveError(Exception):
    """Raised for any invalid leave action."""


# ---------------------------------------------------------------------------
# 1. DYNAMIC MODEL HELPERS (ZERO HARDCODED VALUES)
# ---------------------------------------------------------------------------
def get_leave_type_annual_quota(leave_type):
    """Dynamically reads the yearly quota from whatever field exists on LeaveType."""
    for attr in ('days_per_year', 'annual_quota', 'days_allowed', 'quota', 'max_days', 'days'):
        if hasattr(leave_type, attr):
            val = getattr(leave_type, attr)
            if val is not None:
                return Decimal(str(val))
    return Decimal('0.0')


def calculate_monthly_quota(leave_type):
    """Calculates monthly quota dynamically (Annual / 12) from model database record."""
    annual = get_leave_type_annual_quota(leave_type)
    code = (leave_type.code or '').upper().strip()
    norm_name = leave_type.name.lower()

    # Emergency / Block leaves are not divided by 12
    if code in ('SL', 'BL', 'MATERNITY', 'PATERNITY', 'MTL', 'PL') or any(
        k in norm_name for k in ('sick', 'bereave', 'breave', 'matern', 'patern')
    ):
        return annual

    if annual > Decimal('0.0'):
        raw_monthly = annual / Decimal('12.0')
        # Rounded to 1 decimal place, minimum 1.0 day if quota exists
        return round(raw_monthly, 1) if raw_monthly > Decimal('1.0') else Decimal('1.0')
    return Decimal('0.0')


def get_monthly_available_days(employee, leave_type, target_date=None):
    """Calculates how many days of this leave type are available for the current calendar month."""
    d = target_date or timezone.localdate()
    monthly_quota = calculate_monthly_quota(leave_type)

    active_statuses = [
        getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'),
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
    ]

    month_used_result = m.LeaveApplication.objects.filter(
        employee=employee,
        leave_type=leave_type,
        status__in=active_statuses,
        start_date__year=d.year,
        start_date__month=d.month,
    ).aggregate(total=Sum('total_days'))['total']

    month_used = Decimal(str(month_used_result)) if month_used_result else Decimal('0.0')

    # Also check annual master bank balance
    bank = m.EmployeeLeaveBalance.objects.filter(e_name=employee).first()
    code = (leave_type.code or '').upper().strip()
    norm_name = leave_type.name.lower()

    annual_bal = Decimal('0.0')
    if bank:
        field_candidates = (
            code.lower() + '_leave',
            norm_name.replace(' ', '_') + '_leave',
            norm_name.replace(' ', '_'),
        )
        for attr in field_candidates:
            if hasattr(bank, attr):
                val = getattr(bank, attr)
                if val is not None:
                    annual_bal = Decimal(str(val))
                    break
        else:
            annual_bal = get_leave_type_annual_quota(leave_type)
    else:
        annual_bal = get_leave_type_annual_quota(leave_type)

    return max(Decimal('0.0'), min(annual_bal, monthly_quota - month_used))


def _sync_bank_balance(employee, leave_type, days, action='deduct'):
    """Directly updates the Master Leave Bank (EmployeeLeaveBalance)."""
    if not employee or not leave_type or days <= Decimal('0.0'):
        return

    bank = m.EmployeeLeaveBalance.objects.filter(e_name=employee).first()
    if not bank:
        return

    code = (leave_type.code or '').upper().strip()
    norm_name = leave_type.name.lower()

    field_map = {
        'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
        'BL': 'bereavement_leave', 'ML': 'menstrual_leave', 'MTL': 'menstrual_leave',
        'CO': 'comp_off',
    }
    target_field = field_map.get(code)
    if not target_field:
        for candidate in (
            'maternity_leave', 'paternity_leave',
            norm_name.replace(' ', '_') + '_leave',
            norm_name.replace(' ', '_')
        ):
            if hasattr(bank, candidate):
                target_field = candidate
                break

    if target_field and hasattr(bank, target_field):
        current_val = Decimal(str(getattr(bank, target_field) or 0.0))
        if action == 'deduct':
            new_val = max(Decimal('0.0'), current_val - days)
        elif action == 'refund':
            new_val = current_val + days
        else:
            return

        setattr(bank, target_field, new_val)
        bank.save(update_fields=[target_field])


def calculate_total_days(start_date, end_date, day_type='full'):
    if end_date < start_date:
        raise LeaveError('End date cannot be before start date.')
    if day_type == 'half':
        if start_date != end_date:
            raise LeaveError('Half-day leave must have the same start and end date.')
        return Decimal('0.5')
    return Decimal(str((end_date - start_date).days + 1))


def _log_action(application, action, performed_by=None, remarks=''):
    if hasattr(m, 'LeaveApprovalLog'):
        m.LeaveApprovalLog.objects.create(
            application=application,
            action=action,
            performed_by=performed_by,
            remarks=remarks,
        )


# ---------------------------------------------------------------------------
# 2. APPLY LEAVE (WITH AUTO LWP SPLIT AND REAL-TIME BANK DEDUCTION)
# ---------------------------------------------------------------------------
@transaction.atomic
def apply_leave(employee, leave_type, start_date, end_date, day_type='full',
                reason='', supporting_document=None, relationship='',
                leave_stage='', parent_application=None):

    if day_type == 'half':
        end_date = start_date
        total_days = Decimal('0.5')
    else:
        total_days = calculate_total_days(start_date, end_date, day_type)

    # Prevent Overlap
    active_statuses = [
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'),
    ]
    if m.LeaveApplication.objects.filter(
        employee=employee,
        status__in=active_statuses,
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists():
        raise LeaveError('You already have an active leave request applied/approved for these dates.')

    # Routing
    initial_status = (
        m.LeaveApplication.Status.PENDING_MANAGER
        if employee.reporting_manager
        else m.LeaveApplication.Status.PENDING_HR
    )

    user_notice = None

    # Handle Paid Allocation & LWP Conversion
    if leave_type.is_paid:
        available_days = get_monthly_available_days(employee, leave_type, target_date=start_date)

        if total_days > available_days:
            paid_days = available_days
            lwp_days = total_days - available_days

            lwp_type = m.LeaveType.objects.filter(
                code='LWP', company=employee.company
            ).first() or m.LeaveType.objects.filter(code='LWP').first()

            if not lwp_type:
                lwp_type = leave_type

            # Split: Partial Paid + Partial LWP
            if paid_days > Decimal('0.0'):
                # 1. Paid Application
                paid_end = start_date + timedelta(days=int(paid_days)) if day_type != 'half' else start_date
                app_paid = m.LeaveApplication.objects.create(
                    employee=employee,
                    leave_type=leave_type,
                    start_date=start_date,
                    end_date=paid_end,
                    day_type=day_type,
                    total_days=paid_days,
                    reason=f"{reason} (Paid portion: {paid_days} days)",
                    status=initial_status,
                    supporting_document=supporting_document,
                    relationship=relationship,
                    leave_stage=leave_stage,
                )
                _sync_bank_balance(employee, leave_type, paid_days, action='deduct')

                # 2. LWP Portion
                lwp_start = paid_end + timedelta(days=1) if day_type != 'half' else start_date
                m.LeaveApplication.objects.create(
                    employee=employee,
                    leave_type=lwp_type,
                    start_date=lwp_start,
                    end_date=end_date,
                    day_type=day_type,
                    total_days=lwp_days,
                    reason=f"{reason} (Excess over quota: {lwp_days} days marked as LWP)",
                    status=initial_status,
                    supporting_document=supporting_document,
                )

                user_notice = (
                    f"You have only {paid_days} day(s) of {leave_type.name} available this month. "
                    f"Applied: {paid_days} day(s) as {leave_type.name} and {lwp_days} day(s) as Leave Without Pay (LWP) which will be deducted from your salary."
                )
                _log_action(app_paid, 'applied', performed_by=getattr(employee, 'user', None), remarks=user_notice)
                return app_paid, user_notice

            # Zero Paid Available: Convert all to LWP
            else:
                app_lwp = m.LeaveApplication.objects.create(
                    employee=employee,
                    leave_type=lwp_type,
                    start_date=start_date,
                    end_date=end_date,
                    day_type=day_type,
                    total_days=total_days,
                    reason=f"{reason} (Exceeded {leave_type.name} limit - Converted to LWP)",
                    status=initial_status,
                    supporting_document=supporting_document,
                )
                user_notice = (
                    f"You have 0 days of {leave_type.name} available this month. "
                    f"Your entire application ({total_days} days) has been recorded as Leave Without Pay (LWP) and will be deducted from your salary."
                )
                _log_action(app_lwp, 'applied', performed_by=getattr(employee, 'user', None), remarks=user_notice)
                return app_lwp, user_notice

        # Full balance available within quota
        _sync_bank_balance(employee, leave_type, total_days, action='deduct')

    # Standard Application
    app = m.LeaveApplication.objects.create(
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
    )
    _log_action(app, 'applied', performed_by=getattr(employee, 'user', None), remarks=reason)
    return app, user_notice


# ---------------------------------------------------------------------------
# 3. MANAGER APPROVE
# ---------------------------------------------------------------------------
def manager_approve_leave(application, approver_user):
    if application.status != getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'):
        raise LeaveError('Only applications pending manager approval can be approved by a manager.')

    application.status = getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr')
    application.manager_approved_by = approver_user
    application.manager_approved_on = timezone.now()
    application.save(update_fields=['status', 'manager_approved_by', 'manager_approved_on'])

    _log_action(application, 'manager_approved', performed_by=approver_user, remarks='Manager approved — escalated to HR.')
    return application


# ---------------------------------------------------------------------------
# 4. HR APPROVE
# ---------------------------------------------------------------------------
@transaction.atomic
def approve_leave(application, approver_user):
    valid_pending = [
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
    ]
    if application.status not in valid_pending:
        raise LeaveError('Only pending applications can be approved.')

    emp = application.employee
    code = (application.leave_type.code or '').upper().strip()

    is_half = getattr(application, 'day_type', 'full') == 'half'
    att_status = (
        getattr(m.AttendanceRecord.Status, 'HALF_DAY', m.AttendanceRecord.Status.ON_LEAVE)
        if is_half else m.AttendanceRecord.Status.ON_LEAVE
    )

    curr = application.start_date
    while curr <= application.end_date:
        m.AttendanceRecord.objects.update_or_create(
            employee=emp,
            attendance_date=curr,
            defaults={
                'status': att_status,
                'remarks': f"{code} Approved ({'Half Day' if is_half else 'Full Day'})",
            }
        )
        curr += timedelta(days=1)

    application.status = getattr(m.LeaveApplication.Status, 'APPROVED', 'approved')
    application.approved_by = approver_user
    application.approved_on = timezone.now()
    application.save(update_fields=['status', 'approved_by', 'approved_on'])

    _log_action(application, 'hr_approved', performed_by=approver_user, remarks='Approved by HR')
    return application


# ---------------------------------------------------------------------------
# 5. REJECT LEAVE (Refunds Balance Back to Master Bank)
# ---------------------------------------------------------------------------
@transaction.atomic
def reject_leave(application, approver_user, reason=''):
    emp = application.employee
    days = Decimal(str(application.total_days or 0.0))

    # Refund the deducted days back to the bank
    if application.leave_type.is_paid:
        _sync_bank_balance(emp, application.leave_type, days, action='refund')

    m.AttendanceRecord.objects.filter(
        employee=emp,
        attendance_date__range=[application.start_date, application.end_date],
        status__in=[m.AttendanceRecord.Status.ON_LEAVE, getattr(m.AttendanceRecord.Status, 'HALF_DAY', 'HD')]
    ).delete()

    action_type = (
        'manager_rejected'
        if application.status == getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager')
        else 'hr_rejected'
    )
    application.status = getattr(m.LeaveApplication.Status, 'REJECTED', 'rejected')
    application.rejection_reason = reason
    application.approved_by = approver_user
    application.save()

    _log_action(application, action_type, performed_by=approver_user, remarks=reason)
    return application


# ---------------------------------------------------------------------------
# 6. CANCEL / WITHDRAW LEAVE (Refunds Balance Back to Master Bank)
# ---------------------------------------------------------------------------
@transaction.atomic
def cancel_leave(application, requested_by_employee, is_hr=False):
    cancellable = (
        getattr(m.LeaveApplication.Status, 'PENDING', 'pending'),
        getattr(m.LeaveApplication.Status, 'PENDING_MANAGER', 'pending_manager'),
        getattr(m.LeaveApplication.Status, 'PENDING_HR', 'pending_hr'),
        getattr(m.LeaveApplication.Status, 'APPROVED', 'approved'),
    )
    if application.status not in cancellable:
        raise LeaveError('This application can no longer be cancelled.')

    if not is_hr and application.employee != requested_by_employee:
        raise LeaveError('You can only cancel your own leave applications.')

    emp = application.employee
    days = Decimal(str(application.total_days or 0.0))

    # Refund the deducted days back to the bank
    if application.leave_type.is_paid:
        _sync_bank_balance(emp, application.leave_type, days, action='refund')

    m.AttendanceRecord.objects.filter(
        employee=emp,
        attendance_date__range=[application.start_date, application.end_date],
        status__in=[m.AttendanceRecord.Status.ON_LEAVE, getattr(m.AttendanceRecord.Status, 'HALF_DAY', 'HD')]
    ).delete()

    application.status = getattr(m.LeaveApplication.Status, 'CANCELLED', 'cancelled')
    application.save()

    performer = getattr(requested_by_employee, 'user', None)
    _log_action(application, 'cancelled', performed_by=performer, remarks='Withdrawn by Employee' if not is_hr else 'Cancelled by HR')
    return application

from datetime import date
from django.db import transaction
from . import models as m

def auto_convert_absent_to_leaves(employee_ids, year, month, user):
    """
    Finds 'absent' records for confirmed employees in the target month/year.
    Deducts: CL -> EL -> LWP fallback.
    Updates AttendanceRecord and creates an approved LeaveApplication.
    """
    summary = {
        'total_employees': 0,
        'converted_days': 0,
        'cl_count': 0,
        'el_count': 0,
        'lwp_count': 0,
        'skipped_unconfirmed': 0,
    }

    # Fetch Leave Types by code or name
    cl_type = m.LeaveType.objects.filter(code__iexact='CL').first() or m.LeaveType.objects.filter(name__icontains='Casual').first()
    el_type = m.LeaveType.objects.filter(code__iexact='EL').first() or m.LeaveType.objects.filter(name__icontains='Earned').first()
    lwp_type = m.LeaveType.objects.filter(code__iexact='LWP').first() or m.LeaveType.objects.filter(name__icontains='Without').first()

    # Confirmed employees filter
    employees = m.Employee.objects.filter(id__in=employee_ids)
    confirmed_employees = [e for e in employees if getattr(e, 'employment_status', '').lower() in ['confirmed', 'permanent']]
    summary['skipped_unconfirmed'] = len(employees) - len(confirmed_employees)
    summary['total_employees'] = len(confirmed_employees)

    with transaction.atomic():
        for emp in confirmed_employees:
            # Get leave balances for the year
            cl_bal = m.EmployeeLeaveBalance.objects.filter(employee=emp, leave_type=cl_type, year=year).first() if cl_type else None
            el_bal = m.EmployeeLeaveBalance.objects.filter(employee=emp, leave_type=el_type, year=year).first() if el_type else None

            # Fetch all absent records for this month
            absents = m.AttendanceRecord.objects.filter(
                employee=emp,
                date__year=year,
                date__month=month,
                status='absent'
            ).order_by('date')

            for rec in absents:
                target_type = None
                leave_code = 'LWP'

                # Hierarchy: 1. CL -> 2. EL -> 3. LWP
                if cl_bal and (cl_bal.closing_balance or 0) >= 1.0:
                    cl_bal.closing_balance -= 1.0
                    cl_bal.save()
                    target_type = cl_type
                    leave_code = 'CL'
                    summary['cl_count'] += 1
                elif el_bal and (el_bal.closing_balance or 0) >= 1.0:
                    el_bal.closing_balance -= 1.0
                    el_bal.save()
                    target_type = el_type
                    leave_code = 'EL'
                    summary['el_count'] += 1
                else:
                    target_type = lwp_type
                    leave_code = 'LWP'
                    summary['lwp_count'] += 1

                # Update attendance record status
                rec.status = 'on_leave'
                rec.notes = (rec.notes or '') + f" [Auto-approved as {leave_code} by {user.username}]"
                rec.save()

                # Create Approved Leave Application for payroll & audit sync
                if target_type:
                    m.LeaveApplication.objects.create(
                        employee=emp,
                        leave_type=target_type,
                        start_date=rec.date,
                        end_date=rec.date,
                        number_of_days=1.0,
                        status='approved',
                        approved_by=user,
                        reason=f"Auto-approved absence conversion to {leave_code}"
                    )

                summary['converted_days'] += 1

    return summary