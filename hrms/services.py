"""
HRMS Service Layer — encapsulates business processes such as attendance penalties,
leave deductions, and automated payroll adjustments.
"""
from decimal import Decimal
import logging
from django.db import transaction
from django.utils import timezone
from . import models as m

logger = logging.getLogger(__name__)


def process_late_arrival_penalty(record, reason=None):
    """
    Automated Late Arrival Penalty and Leave Deduction system.
    
    Triggered when an employee exceeds their monthly grace limit or arrives
    beyond the grace window and is marked as 'Half Day' (HD).
    
    Business Rules:
    1. Duplicate Prevention: Checks if a penalty already exists for (employee, penalty_date).
    2. Fallback Deduction Hierarchy (for Full-Time):
       - First Priority: Deduct 0.5 from CL (Casual Leave) if CL >= 0.5.
       - Second Priority: Deduct 0.5 from EL (Earned Leave) if EL >= 0.5.
       - Third Priority: If both CL and EL < 0.5, mark Deduction Source as 'LWP / Salary' (0.5 day wage deduction).
    3. Intern Handling:
       - Direct half-day stipend/salary calculation without deducting from standard paid leaves.
    4. Records entry in AttendancePenalty with status, source, and audit timestamps.
    """
    if not record or not record.employee:
        return None

    employee = record.employee
    penalty_date = record.attendance_date or timezone.localdate()

    # 1. Prevent duplicate penalty logs for the same employee and date
    existing_penalty = m.AttendancePenalty.objects.filter(
        employee=employee,
        penalty_date=penalty_date
    ).first()
    if existing_penalty:
        return existing_penalty

    late_mins = int(record.late_minutes or 0)
    deduction_amount = Decimal('0.5')
    employment_type = getattr(employee, 'employment_type', 'full_time') or 'full_time'

    penalty_reason = reason or f"Grace limit exceeded - Late Arrival ({late_mins} mins late)"

    with transaction.atomic():
        # Case A: INTERN EMPLOYEES
        if employment_type == 'intern':
            salary_deduction = Decimal('0.00')
            try:
                emp_salary = m.EmployeeSalary.objects.filter(
                    employee=employee, is_active=True
                ).first()
                if emp_salary and emp_salary.ctc_annual:
                    daily_wage = (emp_salary.ctc_annual / Decimal('12')) / Decimal('30')
                    salary_deduction = (daily_wage / Decimal('2')).quantize(Decimal('0.01'))
            except Exception as e:
                logger.warning(f"Failed calculating intern salary deduction for {employee}: {e}")

            penalty = m.AttendancePenalty.objects.create(
                employee=employee,
                attendance_record=record,
                penalty_date=penalty_date,
                reason=penalty_reason,
                late_minutes=late_mins,
                deduction_days=deduction_amount,
                deduction_source="LWP / Salary",
                status=m.AttendancePenalty.DeductionStatus.INTERN_SALARY,
                is_intern_penalty=True,
                salary_deduction_amount=salary_deduction,
                employment_type_snapshot=employment_type,
            )
            return penalty

        # Case B: FULL-TIME EMPLOYEES (CL -> EL -> LWP Hierarchy)
        live_balance, _ = m.EmployeeLeaveBalanceLive.objects.select_for_update().get_or_create(e_name=employee)
        bank_balance = m.EmployeeLeaveBalance.objects.select_for_update().filter(e_name=employee).first()

        cl_available = Decimal(str(live_balance.casual_leave or 0.0))
        el_available = Decimal(str(live_balance.earned_leave or 0.0))

        if cl_available >= deduction_amount:
            # First priority: Deduct 0.5 from CL
            new_cl = float(cl_available - deduction_amount)
            live_balance.casual_leave = new_cl
            live_balance.save(update_fields=['casual_leave'])

            if bank_balance:
                bank_balance.casual_leave = max(0.0, float(Decimal(str(bank_balance.casual_leave or 0.0)) - deduction_amount))
                bank_balance.save(update_fields=['casual_leave'])

            deduction_source = "CL"
            penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED

        elif el_available >= deduction_amount:
            # Second priority: Deduct 0.5 from EL
            new_el = float(el_available - deduction_amount)
            live_balance.earned_leave = new_el
            live_balance.save(update_fields=['earned_leave'])

            if bank_balance:
                bank_balance.earned_leave = max(0.0, float(Decimal(str(bank_balance.earned_leave or 0.0)) - deduction_amount))
                bank_balance.save(update_fields=['earned_leave'])

            deduction_source = "EL"
            penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED

        else:
            # Third priority: Both exhausted -> Mark as LWP / Salary deduction
            deduction_source = "LWP / Salary"
            penalty_status = m.AttendancePenalty.DeductionStatus.LWP

        penalty = m.AttendancePenalty.objects.create(
            employee=employee,
            attendance_record=record,
            penalty_date=penalty_date,
            reason=penalty_reason,
            late_minutes=late_mins,
            deduction_days=deduction_amount,
            deduction_source=deduction_source,
            status=penalty_status,
            is_intern_penalty=False,
            employment_type_snapshot=employment_type,
        )
        return penalty
