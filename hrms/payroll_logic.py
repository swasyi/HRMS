"""
Payroll processing engine — kept out of views.py, same pattern as
attendance_logic.py / leave_logic.py.

BUSINESS LOGIC (as specified)
------------------------------
1. Daily Wage = (EmployeeSalary.ctc_annual / 12) / total_days_in_month

2. Attendance integration, for the given month:
     Paid Days = Full Days + (Half Days * 0.5) + Approved Paid Leave days + Comp-Off days
     Comp-Off: an AttendanceRecord with a check_in on a Sunday or a company Holiday
               is NOT counted as a normal full/half day — it's credited as a
               Comp-Off day instead (1 day), on top of that day being a day off.
     Net Absent = Total Days in Month - Paid Days

   ENGINEERING NOTE on a gap in the literal spec: if Sundays/Holidays the
   employee did NOT work were counted as absent, every employee would show
   ~8 "absent" days a month just for having weekends off, which would make
   Net Absent (and the resulting penalty deduction) meaningless. So this
   implementation also treats unworked Sundays/Holidays as paid non-working
   "Off Days" (tracked separately as `off_days`, folded into Paid Days) —
   standard payroll practice. Any day that is NOT a Sunday/Holiday, has no
   attendance record, and isn't covered by an approved paid leave is what
   counts as a genuine Net Absent day.

3. Earnings = Basic + HRA + Special Allowance + Extra (ad-hoc incentives via PayrollExtra)
   Deductions = PF (employee) + ESIC (employee) + Professional Tax + TDS + Loan/Advance repayment
   Penalty Deduction = Net Absent * Daily Wage

4. Net Pay = Total Earnings - Total Deductions - Penalty Deduction
"""
import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q

from . import models as m



def compute_attendance_breakdown(employee, year, month):
    """
    The 'Smart Engine': Calculates full_days and half_days using punch times
    and office hours, matching the Attendance Report logic exactly.
    """
    from django.utils import timezone
    from datetime import datetime, timedelta, time

    total_days_in_month = calendar.monthrange(year, month)[1]
    period_start = date(year, month, 1)
    period_end = date(year, month, total_days_in_month)
    tz = timezone.get_current_timezone()

    comp = employee.company
    off_start, off_end = comp.office_start_time, comp.office_end_time
    grace_mins, grace_limit = comp.grace_minutes, comp.grace_allowed_count

    holidays = get_company_holidays(comp, year, month)
    records = {
        r.attendance_date: r
        for r in m.AttendanceRecord.objects.filter(
            employee=employee, attendance_date__year=year, attendance_date__month=month)
    }

    # Initialize Counters
    full_days = Decimal('0')
    half_days = Decimal('0')
    comp_off_days = Decimal('0')
    off_days = Decimal('0')
    paid_leave_days = Decimal('0')
    unaccounted_working_days = Decimal('0')
    grace_used_count = 0

    for day_num in range(1, total_days_in_month + 1):
        d = date(year, month, day_num)
        is_holiday_or_sun = (d.weekday() == 6) or (d in holidays)
        record = records.get(d)

        # 1. Handle Sundays and Holidays
        if is_holiday_or_sun:
            if record and record.check_in:
                comp_off_days += 1
            else:
                off_days += 1
            continue

        # 2. Process Working Days
        if record and record.check_in:
            # A. Check for Leave First (via Status 'on_leave')
            if record.status == m.AttendanceRecord.Status.ON_LEAVE:
                if record.remarks != "LWP" and employee.employment_type != 'intern':
                    paid_leave_days += 1
                else:
                    unaccounted_working_days += 1
                continue

            # B. Smart Logic for Punches
            local_in = timezone.localtime(record.check_in)
            local_out = timezone.localtime(record.check_out) if record.check_out else None
            p_in = local_in.time()
            p_out = local_out.time() if local_out else off_start

            # Effective hours math (10-6 rule)
            eff_s = max(p_in, off_start)
            eff_e = min(p_out, off_end)
            eff_hours = (datetime.combine(d, eff_e) - datetime.combine(d, eff_s)).total_seconds() / 3600
            grace_deadline = (datetime.combine(d, off_start) + timedelta(minutes=grace_mins)).time()

            if p_in <= off_start:
                if eff_hours >= 8:
                    full_days += 1
                elif eff_hours >= 4:
                    half_days += 1
                    # --- NEW: TOP-UP LOGIC FOR PERMANENT EMPLOYEES ---
                    if employee.employment_type == 'full_time':
                        # Check if a half-day leave exists to cover the other half
                        half_leave = m.LeaveApplication.objects.filter(
                            employee=employee, status='approved',
                            start_date=d, day_type='half', leave_type__is_paid=True
                        ).first()
                        if half_leave:
                            paid_leave_days += Decimal('0.5')
                else:
                    unaccounted_working_days += 1

            elif p_in <= grace_deadline:
                if p_out >= off_end:
                    if grace_used_count < grace_limit:
                        grace_used_count += 1
                        full_days += 1  # Grace 'saves' the full day
                    else:
                        half_days += 1  # Grace exhausted
                        # --- NEW: TOP-UP LOGIC FOR LATE ARRIVAL ---
                        if employee.employment_type == 'full_time':
                            half_leave = m.LeaveApplication.objects.filter(
                                employee=employee, status='approved',
                                start_date=d, day_type='half', leave_type__is_paid=True
                            ).first()
                            if half_leave:
                                paid_leave_days += Decimal('0.5')

                else:
                    half_days += 1  # Came late, left early
                    # Same Top-up for Late > Grace
                    if employee.employment_type == 'full_time':
                        half_leave = m.LeaveApplication.objects.filter(
                            employee=employee, status='approved',
                            start_date=d, day_type='half', leave_type__is_paid=True
                        ).first()
                        if half_leave:
                            paid_leave_days += Decimal('0.5')

            else:
                half_days += 1  # Late Arrival (> 10:15)\
                # Same Top-up for Late > Grace
                if employee.employment_type == 'full_time':
                    half_leave = m.LeaveApplication.objects.filter(
                        employee=employee, status='approved',
                        start_date=d, day_type='half', leave_type__is_paid=True
                    ).first()
                    if half_leave:
                        paid_leave_days += Decimal('0.5')


        else:
            # 3. No Punch -> Check LeaveApplication Table directly
            leave = m.LeaveApplication.objects.filter(
                employee=employee, status='approved',
                start_date__lte=d, end_date__gte=d
            ).first()
            if leave:
                if leave.leave_type.is_paid and employee.employment_type != 'intern':
                    paid_leave_days += 1
                else:
                    unaccounted_working_days += 1
            else:
                unaccounted_working_days += 1

    absent_days = unaccounted_working_days
    paid_days = full_days + (half_days * Decimal('0.5')) + comp_off_days + off_days + paid_leave_days

    return {
        'full_days': full_days, 'half_days': half_days, 'comp_off_days': comp_off_days,
        'off_days': off_days, 'paid_leave_days': paid_leave_days, 'absent_days': absent_days,
        'paid_days': paid_days, 'total_days_in_month': total_days_in_month,
    }


# Update this function
def get_employee_holidays(employee, year, month):
    """Fetches holidays specific to the employee's assigned regional calendar."""
    if not employee.holiday_calendar:
        return set()

    return set(
        m.Holiday.objects.filter(
            calendar=employee.holiday_calendar,
            date__year=year,
            date__month=month
        ).values_list('date', flat=True)
    )

def get_policy_for_date(company, target_date):
    """
    Checks if there is a historical policy record that covers the target_date.
    If not, returns the current Company settings.
    """
    from .models import AttendancePolicy

    # Look for an archive where 'effective_until' is greater than or equal to the target date.
    # We order by effective_until to get the closest matching history.
    history = AttendancePolicy.objects.filter(
        company=company,
        effective_until__gte=target_date
    ).order_by('effective_until').first()

    if history:
        return history  # This object has office_start_time, grace_minutes, etc.

    return company  # Default to the current Company model fields
class PayrollError(Exception):
    """Raised for run-level problems (e.g. no company/period)."""


TWO_DP = Decimal('0.01')
ONE_DP = Decimal('0.1')


def _q(value, places=TWO_DP):
    return Decimal(value).quantize(places, rounding=ROUND_HALF_UP)


def get_active_salary(employee, as_of):
    """The EmployeeSalary row effective for the given date."""
    return (
        m.EmployeeSalary.objects
        .filter(employee=employee, is_active=True, effective_from__lte=as_of)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=as_of))
        .order_by('-effective_from')
        .first()
    )


def get_company_holidays(company, year, month):
    return set(
        m.Holiday.objects.filter(company=company, date__year=year, date__month=month)
        .values_list('date', flat=True)
    )


def get_paid_leave_days(employee, year, month, period_start, period_end):
    """Sums APPROVED, paid LeaveApplication days that overlap this month
    (clipped to the month's boundaries so a leave spanning two months only
    counts the days that actually fall in this period)."""
    total = Decimal('0')
    apps = m.LeaveApplication.objects.filter(
        employee=employee,
        status=m.LeaveApplication.Status.APPROVED,
        leave_type__is_paid=True,
        start_date__lte=period_end,
        end_date__gte=period_start,
    ).select_related('leave_type')
    for app in apps:
        overlap_start = max(app.start_date, period_start)
        overlap_end = min(app.end_date, period_end)
        days = (overlap_end - overlap_start).days + 1
        total += Decimal(days)
    return total



def compute_attendance_breakdown(employee, year, month):
    """
    The 'Smart Engine': Calculates full_days and half_days using punch times
    and office hours, matching the Attendance Report logic exactly.
    """
    from django.utils import timezone
    from datetime import datetime, timedelta, time
    import calendar
    from decimal import Decimal

    total_days_in_month = calendar.monthrange(year, month)[1]
    period_start = date(year, month, 1)
    period_end = date(year, month, total_days_in_month)
    tz = timezone.get_current_timezone()

    comp = employee.company
    off_start, off_end = comp.office_start_time, comp.office_end_time
    grace_mins, grace_limit = comp.grace_minutes, comp.grace_allowed_count

    holidays = get_employee_holidays(employee, year, month)
    records = {
        r.attendance_date: r
        for r in m.AttendanceRecord.objects.filter(
            employee=employee, attendance_date__year=year, attendance_date__month=month)
    }

    # Initialize Counters
    full_days = Decimal('0')
    half_days = Decimal('0')
    comp_off_days = Decimal('0')
    off_days = Decimal('0')
    paid_leave_days = Decimal('0')
    unaccounted_working_days = Decimal('0')
    grace_used_count = 0

    for day_num in range(1, total_days_in_month + 1):
        d = date(year, month, day_num)
        is_holiday_or_sun = (d.weekday() == 6) or (d in holidays)
        record = records.get(d)

        # 1. Handle Sundays and Holidays
        if is_holiday_or_sun:
            if record and record.check_in:
                comp_off_days += 1
            else:
                off_days += 1
            continue

        # 2. Process Working Days
        if record and record.check_in:
            # A. Check for Leave First (via Status 'on_leave')
            if record.status == m.AttendanceRecord.Status.ON_LEAVE:
                if record.remarks != "LWP" and employee.employment_type != 'intern':
                    paid_leave_days += Decimal('1.0')
                else:
                    unaccounted_working_days += Decimal('1.0')
                continue

            # B. Smart Logic for Punches
            local_in = timezone.localtime(record.check_in)
            local_out = timezone.localtime(record.check_out) if record.check_out else None
            p_in = local_in.time()
            p_out = local_out.time() if local_out else off_start

            # Effective hours math (10-6 rule)
            eff_s = max(p_in, off_start)
            eff_e = min(p_out, off_end)
            eff_hours = (datetime.combine(d, eff_e) - datetime.combine(d, eff_s)).total_seconds() / 3600
            grace_deadline = (datetime.combine(d, off_start) + timedelta(minutes=grace_mins)).time()

            # --- Punch Evaluation with Top-Up Logic ---
            if p_in <= off_start:
                if eff_hours >= 8:
                    full_days += 1
                elif eff_hours >= 4:
                    half_days += 1
                    # TOP-UP: Check for matching half-day leave
                    if employee.employment_type == 'full_time':
                        half_leave = m.LeaveApplication.objects.filter(
                            employee=employee, status='approved',
                            start_date=d, day_type='half', leave_type__is_paid=True
                        ).first()
                        if half_leave:
                            paid_leave_days += Decimal('0.5')
                else:
                    unaccounted_working_days += 1

            elif p_in <= grace_deadline:
                if p_out >= off_end:
                    if grace_used_count < grace_limit:
                        grace_used_count += 1
                        full_days += 1  # Grace 'saves' the full day
                    else:
                        half_days += 1  # Grace exhausted
                        if employee.employment_type == 'full_time':
                            half_leave = m.LeaveApplication.objects.filter(
                                employee=employee, status='approved',
                                start_date=d, day_type='half', leave_type__is_paid=True
                            ).first()
                            if half_leave:
                                paid_leave_days += Decimal('0.5')
                else:
                    half_days += 1  # Left early
                    if employee.employment_type == 'full_time':
                        half_leave = m.LeaveApplication.objects.filter(
                            employee=employee, status='approved',
                            start_date=d, day_type='half', leave_type__is_paid=True
                        ).first()
                        if half_leave:
                            paid_leave_days += Decimal('0.5')
            else:
                half_days += 1  # Late Arrival (> 10:15)
                if employee.employment_type == 'full_time':
                    half_leave = m.LeaveApplication.objects.filter(
                        employee=employee, status='approved',
                        start_date=d, day_type='half', leave_type__is_paid=True
                    ).first()
                    if half_leave:
                        paid_leave_days += Decimal('0.5')
        else:
            # 3. No Punch -> Check LeaveApplication Table directly
            leave = m.LeaveApplication.objects.filter(
                employee=employee, status='approved',
                start_date__lte=d, end_date__gte=d
            ).first()
            if leave:
                if leave.leave_type.is_paid and employee.employment_type != 'intern':
                    l_days = Decimal(str(leave.total_days))
                    paid_leave_days += l_days
                    # If it was only a half-day leave with no punch, other 0.5 is absent
                    unaccounted_working_days += (Decimal('1.0') - l_days)
                else:
                    unaccounted_working_days += 1
            else:
                unaccounted_working_days += 1

    absent_days = unaccounted_working_days
    # paid_days = full_days + (half_days * Decimal('0.5')) + comp_off_days + off_days + paid_leave_days
    # paid_days = min(paid_days, Decimal(str(total_days_in_month)))
    total_worked = full_days + (half_days * Decimal('0.5')) + comp_off_days + off_days
    max_days = Decimal(str(total_days_in_month))
    paid_days = min(max_days, total_worked + paid_leave_days)

    return {
        'full_days': full_days, 'half_days': half_days, 'comp_off_days': comp_off_days,
        'off_days': off_days, 'paid_leave_days': paid_leave_days,
        # 'absent_days': absent_days,
        'absent_days': max_days - paid_days,
        'paid_days': paid_days, 'total_days_in_month': total_days_in_month,
    }



def get_loan_deduction(employee):
    """Sums this month's installment across all active loans (capped at each
    loan's remaining balance) and returns (total_deduction, [loan_objs_touched])."""
    loans = list(m.LoanAdvance.objects.filter(employee=employee, is_active=True))
    total = Decimal('0')
    touched = []
    for loan in loans:
        installment = min(loan.monthly_installment, loan.remaining_balance)
        if installment > 0:
            total += installment
            touched.append((loan, installment))
    return total, touched


def get_pending_extras(employee):
    extras = list(m.PayrollExtra.objects.filter(employee=employee, is_consumed=False))
    total = sum((e.amount for e in extras), Decimal('0'))
    return total, extras


def process_employee(employee, payroll_run, year, month):
    """Computes and saves (create-or-update) one employee's PaySlip for this run.
    Returns (payslip, warning_or_None)."""
    period_end = date(year, month, calendar.monthrange(year, month)[1])
    salary = get_active_salary(employee, period_end)
    if salary is None:
        return None, f'{employee.full_name} — no active salary structure configured, skipped.'

    breakdown = compute_attendance_breakdown(employee, year, month)
    total_days = breakdown['total_days_in_month']

    monthly_ctc = salary.ctc_annual / Decimal('12')
    daily_wage = _q(monthly_ctc / Decimal(total_days))
    penalty_deduction = _q(breakdown['absent_days'] * daily_wage)

    loan_deduction, touched_loans = get_loan_deduction(employee)
    extra_earning, touched_extras = get_pending_extras(employee)

    total_earnings = _q(salary.basic + salary.hra + salary.special_allowance + extra_earning)
    total_deductions = _q(
        salary.pf_employee + salary.esic_employee + salary.professional_tax
        + salary.tds + loan_deduction
    )
    net_pay = _q(total_earnings - total_deductions - penalty_deduction)

    payslip, _ = m.PaySlip.objects.update_or_create(
        payroll_run=payroll_run, employee=employee,
        defaults={
            'full_days': breakdown['full_days'],
            'half_days': breakdown['half_days'],
            'comp_off_days': breakdown['comp_off_days'],
            'off_days': breakdown['off_days'],
            'paid_leave_days': breakdown['paid_leave_days'],
            'absent_days': breakdown['absent_days'],
            'paid_days': breakdown['paid_days'],
            'total_days_in_month': total_days,
            'daily_wage': daily_wage,
            'penalty_deduction': penalty_deduction,
            'loan_deduction': _q(loan_deduction),
            'extra_earning': _q(extra_earning),
            'total_earnings': total_earnings,
            'total_deductions': total_deductions,
            'net_pay': net_pay,
        },
    )

    # Consume the loan installments / extras only now that the payslip is saved.
    for loan, installment in touched_loans:
        loan.remaining_balance = loan.remaining_balance - installment
        if loan.remaining_balance <= 0:
            loan.remaining_balance = Decimal('0')
            loan.is_active = False
        loan.save(update_fields=['remaining_balance', 'is_active', 'updated_at'])

    for extra in touched_extras:
        extra.is_consumed = True
        extra.payroll_run = payroll_run
        extra.save(update_fields=['is_consumed', 'payroll_run', 'updated_at'])

    return payslip, None


def process_payroll(company, year, month, user=None):
    """Loops through all ACTIVE employees of `company`, computes their PaySlip
    for (year, month), and creates/updates the PayrollRun + PaySlips.
    Returns (payroll_run, warnings list)."""
    payroll_run, _ = m.PayrollRun.objects.get_or_create(
        company=company, month=month, year=year,
        defaults={'status': m.PayrollRun.Status.PROCESSING, 'created_by': user},
    )
    payroll_run.status = m.PayrollRun.Status.PROCESSING
    payroll_run.created_by = user or payroll_run.created_by
    payroll_run.save(update_fields=['status', 'created_by', 'updated_at'])

    warnings = []
    employees = m.Employee.objects.filter(company=company, status=m.Employee.Status.ACTIVE)
    for employee in employees:
        _, warning = process_employee(employee, payroll_run, year, month)
        if warning:
            warnings.append(warning)

    payroll_run.status = m.PayrollRun.Status.COMPLETED
    payroll_run.payroll_date = date.today()
    payroll_run.save(update_fields=['status', 'payroll_date', 'updated_at'])

    return payroll_run, warnings
