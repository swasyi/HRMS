"""
Leave management business logic — kept out of views.py, same pattern as
attendance_logic.py.

Rules implemented:

APPLY
  - total_days = inclusive day-count between start_date and end_date.
  - Rejected outright (LeaveError) if:
      * end_date < start_date
      * leave_type.max_consecutive_days is set and exceeded
      * leave_type.applicable_gender doesn't match the employee's gender
      * the employee's LeaveBalance.available for that leave_type/year is
        less than total_days requested — THIS is the balance-sufficiency
        check the prompt asked for.
  - On success: creates a PENDING LeaveApplication and reserves the days by
    incrementing LeaveBalance.pending (so a second application can't
    over-book the same balance while this one awaits approval).

APPROVE / REJECT (Admin/HR only — enforced in views.py via HRRequiredMixin,
  not here; this module only assumes the caller already checked that)
  - Approve: pending -> used on the balance, status -> APPROVED.
  - Reject: pending is released (nothing consumed), status -> REJECTED.

CANCEL (the employee themself, or HR)
  - Pending: releases the pending reservation.
  - Approved: gives back the previously-consumed `used` days (e.g. plans changed).
"""
from decimal import Decimal

from django.utils import timezone

from . import models as m


class LeaveError(Exception):
    """Raised for any invalid leave action — insufficient balance, bad dates, etc."""


def get_or_create_balance(employee, leave_type, year):
    balance, _ = m.LeaveBalance.objects.get_or_create(
        employee=employee, leave_type=leave_type, year=year,
        defaults={'allocated': leave_type.days_per_year},
    )
    return balance


def calculate_total_days(start_date, end_date):
    if end_date < start_date:
        raise LeaveError('End date cannot be before start date.')
    return Decimal((end_date - start_date).days + 1)


def calculate_total_days(start_date, end_date, day_type='full'):
    if end_date < start_date:
        raise LeaveError('End date cannot be before start date.')

    if day_type == 'half':
        if start_date != end_date:
            raise LeaveError(
                'Half-day leave can only be applied for a single day (Start and End date must be the same).')
        return Decimal('0.5')

    return Decimal((end_date - start_date).days + 1)


def apply_leave(employee, leave_type, start_date, end_date, day_type='full', reason=''):
    # 1. Force dates for Half Day
    if day_type == 'half':
        end_date = start_date
        total_days = Decimal('0.5')
    else:
        total_days = calculate_total_days(start_date, end_date, day_type)

    # 2. INTERN/TRAINEE LOGIC: "Money Deduct" (Switch to LWP)
    if employee.employment_type in ['intern', 'trainee']:
        if leave_type.is_paid:
            lwp_type = m.LeaveType.objects.filter(code='LWP', company=employee.company).first()
            if lwp_type:
                leave_type = lwp_type

    if leave_type.max_consecutive_days and total_days > leave_type.max_consecutive_days:
        raise LeaveError(
            f'{leave_type.name} cannot be applied for more than '
            f'{leave_type.max_consecutive_days} consecutive day(s).'
        )

    if (leave_type.applicable_gender != m.LeaveType.Gender.ALL
            and employee.gender != leave_type.applicable_gender):
        raise LeaveError(f'{leave_type.name} is not applicable to your profile.')

    if leave_type.is_paid:
        balance = get_or_create_balance(employee, leave_type, start_date.year)
        if total_days > balance.available:
            raise LeaveError(f'Insufficient balance: {balance.available} days left for {leave_type.name}.')

    # Routing logic:
    # If employee has reporting_manager, send to manager first (PENDING_MANAGER).
    # Otherwise send directly to HR (PENDING_HR).
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
    )

    # Automated email notification to HR & CC
    try:
        from . import email_logic
        email_logic.send_leave_application_email(application)
    except Exception:
        pass

    return application


def manager_approve_leave(application, approver_user):
    """
    Manager approves leave application from their subordinate:
    Advances status from PENDING_MANAGER to PENDING_HR.
    """
    if application.status != m.LeaveApplication.Status.PENDING_MANAGER:
        raise LeaveError('Only applications pending manager approval can be approved by a manager.')

    application.status = m.LeaveApplication.Status.PENDING_HR
    application.approved_by = approver_user
    application.approved_on = timezone.now()
    application.save(update_fields=['status', 'approved_by', 'approved_on', 'updated_at'])
    return application


def approve_leave(application, approver_user):
    """
    HR / SuperAdmin approves leave:
    1. Updates Status to Approved.
    2. Deducts leaves from 'Leave Balance' (EmployeeLeaveBalanceLive).
    3. Automatically creates Attendance Records as 'ON_LEAVE'.
    """
    if application.status not in [
        m.LeaveApplication.Status.PENDING,
        m.LeaveApplication.Status.PENDING_HR,
        m.LeaveApplication.Status.PENDING_MANAGER
    ]:
        raise LeaveError('Only pending applications can be approved.')

    # --- STEP 1: Deduct from Leave Balance (Live Report) ---
    msg = adjust_live_balance(
        employee=application.employee,
        amount=application.total_days,
        action="deduct",
        leave_code=application.leave_type.code
    )

    # --- STEP 2: Create Attendance Records ---
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

    # --- STEP 3: Update Application Status ---
    application.status = m.LeaveApplication.Status.APPROVED
    application.approved_by = approver_user
    application.approved_on = timezone.now()
    application.save()

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

    application.status = m.LeaveApplication.Status.REJECTED
    application.rejection_reason = reason
    application.approved_by = approver_user
    application.save()
    return application


def apply_late_penalty_deduction(record):
    """
    Deducts 0.5 leaves from EmployeeLeaveBalanceLive (Priority: CL first, then EL)
    when an AttendanceRecord is marked as a Half Day due to late arrival exceeding grace.
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

    live_report, _ = m.EmployeeLeaveBalanceLive.objects.get_or_create(e_name=record.employee)
    amount = 0.5
    deduction_source = "LWP"
    penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED

    # Priority 1: Casual Leave (CL)
    if live_report.casual_leave >= amount:
        live_report.casual_leave -= amount
        live_report.save(update_fields=['casual_leave'])
        deduction_source = "Deducted 0.5 from CL"
    # Priority 2: Earned Leave (EL)
    elif live_report.earned_leave >= amount:
        live_report.earned_leave -= amount
        live_report.save(update_fields=['earned_leave'])
        deduction_source = "Deducted 0.5 from EL"
    else:
        deduction_source = "LWP (Insufficient Balance)"
        penalty_status = m.AttendancePenalty.DeductionStatus.LWP

    penalty = m.AttendancePenalty.objects.create(
        employee=record.employee,
        attendance_record=record,
        penalty_date=record.attendance_date,
        reason=f"Late Arrival ({record.late_minutes} mins late - Exceeded Grace)",
        late_minutes=record.late_minutes,
        deduction_days=Decimal('0.5'),
        deduction_source=deduction_source,
        status=penalty_status,
    )
    return penalty


# leave_logic.py

# leave_logic.py

def adjust_live_balance(employee, amount, action="deduct", leave_code=None):
    """
    THIS IS THE FIX: We target EmployeeLeaveBalanceLive (Page 2).
    We do NOT touch EmployeeLeaveBalance (Page 1 Bank).
    """
    # TARGET THE LIVE MODEL ONLY
    live_report, _ = m.EmployeeLeaveBalanceLive.objects.get_or_create(e_name=employee)
    amount = float(amount)

    field_map = {
        'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
        'ML': 'menstrual_leave', 'BL': 'bereavement_leave', 'CO': 'comp_off'
    }

    if action == "refund" and leave_code:
        field = field_map.get(leave_code.upper())
        if field:
            setattr(live_report, field, getattr(live_report, field) + amount)
            live_report.save()
            return "Refunded to Live Balance"

    elif action == "deduct":
        field = field_map.get(leave_code.upper())

        # 1. Try Specific Field in LIVE REPORT
        if field and getattr(live_report, field) >= amount:
            setattr(live_report, field, getattr(live_report, field) - amount)
            live_report.save()
            return f"Deducted from {leave_code}"

        # 2. Priority Fallback in LIVE REPORT (CL -> EL)
        if live_report.casual_leave >= amount:
            live_report.casual_leave -= amount
            live_report.save()
            return "Deducted from CL (Live)"
        elif live_report.earned_leave >= amount:
            live_report.earned_leave -= amount
            live_report.save()
            return "Deducted from EL (Live)"

        return "LWP"  # No leaves left in Live Balance

    return "No action"
def cancel_leave(application, requested_by_employee, is_hr=False):
    if application.status not in (m.LeaveApplication.Status.PENDING, m.LeaveApplication.Status.APPROVED):
        raise LeaveError('This application can no longer be cancelled.')
    if not is_hr and application.employee != requested_by_employee:
        raise LeaveError('You can only cancel your own leave applications.')

    balance = get_or_create_balance(application.employee, application.leave_type, application.start_date.year)
    if application.status == m.LeaveApplication.Status.PENDING:
        balance.pending -= application.total_days
    else:  # was APPROVED — give back the consumed days
        balance.used -= application.total_days
    balance.save(update_fields=['pending', 'used', 'updated_at'])

    application.status = m.LeaveApplication.Status.CANCELLED
    application.save()
    return application


def refund_leave_on_punch(employee, attendance_date, attendance_status):
    """
    Checks if there's an approved leave for this date.
    If yes, refunds the appropriate amount to the Leave Bank.
    """
    from .models import LeaveApplication, EmployeeLeaveBalance, LeaveType

    # 1. Find an approved leave covering this date
    leave = LeaveApplication.objects.filter(
        employee=employee,
        status='approved',
        start_date__lte=attendance_date,
        end_date__gte=attendance_date
    ).first()

    if not leave:
        return

    # 2. Determine refund amount
    refund_amount = 0
    if attendance_status == 'FD':
        # Refund whatever was taken for this day (max 1.0)
        refund_amount = 0.5 if leave.day_type == 'half' else 1.0
    elif attendance_status == 'HD':
        # They worked half, so we always refund 0.5
        refund_amount = 0.5

    if refund_amount > 0:
        # 3. Update the Leave Bank
        mapping = {
            'EL': 'earned_leave', 'SL': 'sick_leave', 'CL': 'casual_leave',
            'MTL': 'menstrual_leave', 'BL': 'bereavement_leave', 'CO': 'comp_off',
        }
        field_name = mapping.get(leave.leave_type.code.upper())

        if field_name:
            bank, _ = EmployeeLeaveBalance.objects.get_or_create(e_name=employee)
            current_val = getattr(bank, field_name)
            setattr(bank, field_name, float(current_val) + refund_amount)
            bank.save()

            # Optional: Add a note to the attendance record that balance was refunded
            return True
    return False
def refund_leave_on_punch(employee, attendance_date, attendance_status):
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
        # Use the engine to put the days back in the bank
        adjust_leave_bank(employee, refund_amount, action="refund", leave_code=leave.leave_type.code)
        return True
    return False


def adjust_leave_bank(employee, amount, action="deduct", leave_code=None):
    """apply_leave
    Central logic to subtract or add leaves to the EmployeeLeaveBalance bank.
    Priority for deduction: Specific Code -> CL -> EL -> LWP
    """
    bank, _ = m.EmployeeLeaveBalance.objects.get_or_create(e_name=employee)
    amount = float(amount)

    # Mapping model fields to codes
    field_map = {
        'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
        'MTL': 'menstrual_leave', 'BL': 'bereavement_leave', 'CO': 'comp_off'
    }

    if action == "refund" and leave_code:
        field = field_map.get(leave_code.upper())
        if field:
            setattr(bank, field, float(getattr(bank, field)) + amount)
            bank.save()
            return f"Refunded {amount} to {leave_code}"

    elif action == "deduct":
        # 1. Try Specific Leave Code first (if provided)
        if leave_code:
            field = field_map.get(leave_code.upper())
            if field and getattr(bank, field) >= amount:
                setattr(bank, field, float(getattr(bank, field)) - amount)
                bank.save()
                return f"Deducted {amount} from {leave_code}"

        # 2. PRIORITY FALLBACK: Casual Leave (CL)
        if bank.casual_leave >= amount:
            bank.casual_leave = float(bank.casual_leave) - amount
            bank.save()
            return f"Deducted {amount} from CL"

        # 3. SECOND FALLBACK: Earned Leave (EL)
        elif bank.earned_leave >= amount:
            bank.earned_leave = float(bank.earned_leave) - amount
            bank.save()
            return f"Deducted {amount} from EL"

        # 4. FINAL FALLBACK: No leave available -> Mark for salary cut
        else:
            return "LWP"  # Signal to Payroll/Attendance for monetary cut

    return "No action"


# leave_logic.py updates

# leave_logic.py

def adjust_leave_bank(employee, amount, action="deduct", leave_code=None):
    """
    FIX: Change the target to EmployeeLeaveBalanceLive.
    This ensures Page 1 (Bank) is never touched during approval.
    """
    # CHANGE THIS LINE: Target the 'Live' model
    live_report, _ = m.EmployeeLeaveBalanceLive.objects.get_or_create(e_name=employee)
    amount = float(amount)

    field_map = {
        'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
        'ML': 'menstrual_leave', 'BL': 'bereavement_leave', 'CO': 'comp_off'
    }

    if action == "refund" and leave_code:
        field = field_map.get(leave_code.upper())
        if field:
            # Update Live Report only
            current = getattr(live_report, field)
            setattr(live_report, field, current + amount)
            live_report.save()
            return f"Refunded to Balance Report"

    elif action == "deduct":
        field = field_map.get(leave_code.upper())
        # Deduct from Live Report only
        if field and getattr(live_report, field) >= amount:
            setattr(live_report, field, getattr(live_report, field) - amount)
            live_report.save()
            return f"Deducted from {leave_code}"

        # Fallback logic also targets the Live Report
        elif live_report.casual_leave >= amount:
            live_report.casual_leave -= amount
            live_report.save()
            return "Deducted from CL (Live Fallback)"

    return "No Change"


from datetime import date
from . import models as m


def sync_employee_leave_bank(employee):
    """
    Calculates annual leave entitlement based on Office Rules:
    - Intern Male: 0
    - Intern Female: 2 Menstrual, 0 others
    - Full Time: Fixed SL/BL, Pro-rated CL/EL based on Confirmation Date.
    """
    bank, _ = m.EmployeeLeaveBalance.objects.get_or_create(e_name=employee)

    # 1. Pull base values from LeaveType table (avoid hardcoding)
    # We use .filter().first() to avoid errors if a code is missing
    get_days = lambda code: float(
        m.LeaveType.objects.filter(code=code, company=employee.company).values_list('days_per_year',
                                                                                    flat=True).first() or 0)

    base_sl = get_days('SL')
    base_bl = get_days('BL')
    base_cl = get_days('CL')
    base_el = get_days('EL')
    base_mtl = get_days('MTL')

    # --- CATEGORY A: INTERNS ---
    if employee.employment_type == 'intern':
        bank.sick_leave = 0.0
        bank.bereavement_leave = 0.0
        bank.casual_leave = 0.0
        bank.earned_leave = 0.0
        bank.comp_off = 0.0
        bank.menstrual_leave = 2.0 if employee.gender == 'F' else 0.0
        bank.save()
        return

    # --- CATEGORY B: FULL TIME (Pro-rated) ---
    conf_date = employee.date_of_confirmation
    if not conf_date:
        # If not confirmed, we don't assign the annual bank yet
        return

    # Financial Year Logic (Apr - Mar)
    today = date.today()
    fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    fy_end = date(fy_start.year + 1, 3, 31)

    # 1. SL & BL: Always same for all confirmed employees
    bank.sick_leave = base_sl
    bank.bereavement_leave = base_bl
    bank.menstrual_leave = base_mtl if employee.gender == 'F' else 0.0

    # 2. Calculate Months Remaining for CL & EL
    # Start counting from the LATER of (Confirmation Date) or (Current FY Start)
    calc_start = max(conf_date, fy_start)

    # Months calculation: Total months from start until end of March
    if calc_start > fy_end:
        months_left = 0
    else:
        months_left = (fy_end.year - calc_start.year) * 12 + (fy_end.month - calc_start.month) + 1

    months_left = min(max(months_left, 0), 12)

    # Formula: (Annual Days / 12) * months left
    bank.casual_leave = round((base_cl / 12.0) * months_left, 1)
    bank.earned_leave = round((base_el / 12.0) * months_left, 1)

    bank.save()
    from datetime import date
    from . import models as m

def sync_employee_leave_bank(employee):
    bank, _ = m.EmployeeLeaveBalance.objects.get_or_create(e_name=employee)

    # 1. Pull base values from LeaveType table
    def get_days(code):
        return float(m.LeaveType.objects.filter(code=code, company=employee.company).values_list('days_per_year',
                                                                                                 flat=True).first() or 0)

    base_sl = get_days('SL')  # Expecting 10.0
    base_bl = get_days('BL')  # Expecting 5.0
    base_cl = get_days('CL')  # Expecting 10.0
    base_el = get_days('EL')  # Expecting 20.0
    base_mtl = get_days('MTL')

    # --- CATEGORY A: INTERNS ---
    if employee.employment_type == 'intern':
        bank.sick_leave = 0.0
        bank.bereavement_leave = 0.0
        bank.casual_leave = 0.0
        bank.earned_leave = 0.0
        bank.comp_off = 0.0
        bank.menstrual_leave = 2.0 if employee.gender == 'F' else 0.0
        bank.save()
        return

    # --- CATEGORY B: FULL TIME ---
    conf_date = employee.date_of_confirmation
    if not conf_date:
        return

    # FIXED Financial Year for the 2026-2027 Period
    FY_START = date(2026, 4, 1)
    FY_END = date(2027, 3, 31)

    # 1. SL & BL: Always full once confirmed
    bank.sick_leave = base_sl
    bank.bereavement_leave = base_bl
    bank.menstrual_leave = base_mtl if employee.gender == 'F' else 0.0

    # 2. Pro-rating Logic
    # If confirmed BEFORE April 2026, they get the FULL 12 months.
    if conf_date <= FY_START:
        months_left = 12
    else:
        # If confirmed DURING the year (e.g. May 2026), calculate remaining months until March 2027
        months_left = (FY_END.year - conf_date.year) * 12 + (FY_END.month - conf_date.month) + 1
        months_left = min(max(months_left, 0), 12)

    # Final Calculation
    bank.casual_leave = round((base_cl / 12.0) * months_left, 1)
    bank.earned_leave = round((base_el / 12.0) * months_left, 1)

    bank.save()


def get_employee_holidays(employee, start_date, end_date):
    """
    Returns a list of holiday dates for a specific employee
    based on their assigned calendar.
    """
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