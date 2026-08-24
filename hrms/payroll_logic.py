"""
Payroll processing engine — kept out of views.py, same pattern as
attendance_logic.py / leave_logic.py.

BUSINESS LOGIC:
1. Daily Wage = (EmployeeSalary.ctc_annual / 12) / total_days_in_month
2. Attendance breakdown for given month:
   - Full Days, Half Days (0.5), Comp-Off (1.0 for worked Sundays/Holidays)
   - Off Days (paid non-working Sundays/Holidays)
   - Paid Leave Days (approved paid leaves)
   - Absent Days = total_days_in_month - paid_days
3. Full-Time:
   - Earnings = Basic + HRA + Special Allowance + Extra Earnings
   - Deductions = PF + ESIC + Professional Tax + TDS + Loan Installment
   - Penalty Deduction = Absent Days * Daily Wage
   - Net Pay = Total Earnings - Total Deductions - Penalty Deduction
4. Interns:
   - Earnings = Stipend (CTC / 12) + Extra Earnings
   - Deductions = TDS + Loan Installment (no PF/ESIC/PT)
   - Penalty Deduction = Absent Days * Daily Wage
   - Net Pay = Total Earnings - Total Deductions - Penalty Deduction
"""
import calendar
from datetime import date, datetime, timedelta, time
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Q
from django.utils import timezone

from . import models as m


class PayrollError(Exception):
    """Raised for run-level problems (e.g. no company/period)."""


TWO_DP = Decimal('0.01')


def _q(value, places=TWO_DP):
    """Rounds Decimal value to two decimal places."""
    return Decimal(str(value)).quantize(places, rounding=ROUND_HALF_UP)


def get_active_salary(employee, as_of):
    """Returns the EmployeeSalary row active on the given date."""
    return (
        m.EmployeeSalary.objects
        .filter(employee=employee, is_active=True, effective_from__lte=as_of)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=as_of))
        .order_by('-effective_from')
        .first()
    )


def get_employee_holidays(employee, year, month):
    """Fetches holidays specific to the employee's assigned regional calendar or company."""
    if employee.holiday_calendar:
        return set(
            m.Holiday.objects.filter(
                calendar=employee.holiday_calendar,
                date__year=year,
                date__month=month
            ).values_list('date', flat=True)
        )
    elif employee.company:
        return set(
            m.Holiday.objects.filter(
                calendar__company=employee.company,
                date__year=year,
                date__month=month
            ).values_list('date', flat=True)
        )
    return set()


def compute_attendance_breakdown(employee, year, month):
    """
    The 'Smart Engine': Calculates attendance breakdown using punch times
    and versioned office policy, matching attendance rules exactly.
    """
    total_days_in_month = calendar.monthrange(year, month)[1]
    comp = employee.company

    # Regional holidays for this employee
    holidays = get_employee_holidays(employee, year, month)

    records = {
        r.attendance_date: r
        for r in m.AttendanceRecord.objects.filter(
            employee=employee, attendance_date__year=year, attendance_date__month=month
        )
    }

    full_days = Decimal('0')
    half_days = Decimal('0')
    comp_off_days = Decimal('0')
    off_days = Decimal('0')
    paid_leave_days = Decimal('0')
    unaccounted_working_days = Decimal('0')
    grace_used_count = 0

    for day_num in range(1, total_days_in_month + 1):
        d = date(year, month, day_num)

        # Versioned policy for this date
        policy = comp.get_policy_for_date(d) if hasattr(comp, 'get_policy_for_date') else comp
        off_start = getattr(policy, 'office_start_time', None) or time(10, 0)
        off_end = getattr(policy, 'office_end_time', None) or time(18, 0)
        grace_mins = getattr(policy, 'grace_minutes', 15)
        grace_limit = getattr(policy, 'grace_allowed_count', 3)
        full_threshold = float(getattr(policy, 'full_day_threshold_hours', 8.0) or 8.0)
        half_threshold = float(getattr(policy, 'half_day_threshold_hours', 4.0) or 4.0)

        is_holiday_or_sun = (d.weekday() == 6) or (d in holidays)
        record = records.get(d)

        # 1. Sundays and Holidays
        if is_holiday_or_sun:
            if record and record.check_in:
                comp_off_days += Decimal('1.0')
            else:
                off_days += Decimal('1.0')
            continue

        # 2. Working Days with Attendance Punches
        if record and record.check_in:
            # Check for Status 'on_leave'
            if record.status == m.AttendanceRecord.Status.ON_LEAVE:
                if record.remarks != "LWP" and employee.employment_type != 'intern':
                    paid_leave_days += Decimal('1.0')
                else:
                    unaccounted_working_days += Decimal('1.0')
                continue

            local_in = timezone.localtime(record.check_in) if timezone.is_aware(record.check_in) else record.check_in
            local_out = timezone.localtime(record.check_out) if record.check_out and timezone.is_aware(record.check_out) else record.check_out
            p_in = local_in.time()
            p_out = local_out.time() if local_out else off_start

            # Effective working hours calculation
            eff_s = max(p_in, off_start)
            eff_e = min(p_out, off_end) if local_out else off_start
            eff_hours = max(0.0, (datetime.combine(d, eff_e) - datetime.combine(d, eff_s)).total_seconds() / 3600)
            grace_deadline = (datetime.combine(d, off_start) + timedelta(minutes=grace_mins)).time()

            if p_in <= off_start:
                if eff_hours >= full_threshold:
                    full_days += Decimal('1.0')
                elif eff_hours >= half_threshold:
                    half_days += Decimal('1.0')
                    if employee.employment_type == 'full_time':
                        half_leave = m.LeaveApplication.objects.filter(
                            employee=employee, status=m.LeaveApplication.Status.APPROVED,
                            start_date=d, day_type='half', leave_type__is_paid=True
                        ).first()
                        if half_leave:
                            paid_leave_days += Decimal('0.5')
                else:
                    unaccounted_working_days += Decimal('1.0')

            elif p_in <= grace_deadline:
                if local_out and p_out >= off_end:
                    if grace_used_count < grace_limit:
                        grace_used_count += 1
                        full_days += Decimal('1.0')  # Grace forgiven
                    else:
                        half_days += Decimal('1.0')  # Grace exhausted
                        if employee.employment_type == 'full_time':
                            half_leave = m.LeaveApplication.objects.filter(
                                employee=employee, status=m.LeaveApplication.Status.APPROVED,
                                start_date=d, day_type='half', leave_type__is_paid=True
                            ).first()
                            if half_leave:
                                paid_leave_days += Decimal('0.5')
                else:
                    half_days += Decimal('1.0')  # Came late, left early
                    if employee.employment_type == 'full_time':
                        half_leave = m.LeaveApplication.objects.filter(
                            employee=employee, status=m.LeaveApplication.Status.APPROVED,
                            start_date=d, day_type='half', leave_type__is_paid=True
                        ).first()
                        if half_leave:
                            paid_leave_days += Decimal('0.5')

            else:
                half_days += Decimal('1.0')  # Late arrival past grace window
                if employee.employment_type == 'full_time':
                    half_leave = m.LeaveApplication.objects.filter(
                        employee=employee, status=m.LeaveApplication.Status.APPROVED,
                        start_date=d, day_type='half', leave_type__is_paid=True
                    ).first()
                    if half_leave:
                        paid_leave_days += Decimal('0.5')

        else:
            # 3. No Punch -> Check Leave Applications
            leave = m.LeaveApplication.objects.filter(
                employee=employee, status=m.LeaveApplication.Status.APPROVED,
                start_date__lte=d, end_date__gte=d
            ).first()
            if leave and leave.leave_type.is_paid and employee.employment_type != 'intern':
                l_days = Decimal(str(leave.total_days)) if leave.day_type == 'full' else Decimal('0.5')
                paid_leave_days += l_days
                if l_days < Decimal('1.0'):
                    unaccounted_working_days += (Decimal('1.0') - l_days)
            else:
                unaccounted_working_days += Decimal('1.0')

    max_days = Decimal(str(total_days_in_month))
    total_worked = full_days + (half_days * Decimal('0.5')) + comp_off_days + off_days
    paid_days = min(max_days, total_worked + paid_leave_days)
    absent_days = max(Decimal('0'), max_days - paid_days)

    return {
        'full_days': full_days,
        'half_days': half_days,
        'comp_off_days': comp_off_days,
        'off_days': off_days,
        'paid_leave_days': paid_leave_days,
        'absent_days': absent_days,
        'paid_days': paid_days,
        'total_days_in_month': total_days_in_month,
    }


def get_loan_deduction(employee):
    """Sums this month's installment across all active loans, returns (total_deduction, [(loan, installment)])."""
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
    """Returns unconsumed extras for the employee: (total_extra, [extra_objects])."""
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

    is_intern = (employee.employment_type == 'intern')

    if is_intern:
        # Intern Stipend Model: no PF, ESIC, or PT
        total_earnings = _q(monthly_ctc + extra_earning)
        total_deductions = _q(salary.tds + loan_deduction)
    else:
        # Full-time Employee Model
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

    # Consume loans and extras
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
