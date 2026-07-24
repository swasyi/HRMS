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


def apply_leave(employee, leave_type, start_date, end_date, reason=''):
    total_days = calculate_total_days(start_date, end_date)

    if leave_type.max_consecutive_days and total_days > leave_type.max_consecutive_days:
        raise LeaveError(
            f'{leave_type.name} cannot be applied for more than '
            f'{leave_type.max_consecutive_days} consecutive day(s).'
        )

    if (leave_type.applicable_gender != m.LeaveType.Gender.ALL
            and employee.gender != leave_type.applicable_gender):
        raise LeaveError(f'{leave_type.name} is not applicable to your profile.')

    balance = get_or_create_balance(employee, leave_type, start_date.year)
    if total_days > balance.available:
        raise LeaveError(
            f'Insufficient leave balance: requested {total_days} day(s), '
            f'only {balance.available} day(s) available for {leave_type.name}.'
        )

    application = m.LeaveApplication.objects.create(
        employee=employee, leave_type=leave_type, start_date=start_date, end_date=end_date,
        total_days=total_days, reason=reason, status=m.LeaveApplication.Status.PENDING,
    )
    balance.pending += total_days
    balance.save(update_fields=['pending', 'updated_at'])
    return application


def approve_leave(application, approver_employee=None):
    if application.status != m.LeaveApplication.Status.PENDING:
        raise LeaveError('Only pending applications can be approved.')

    balance = get_or_create_balance(application.employee, application.leave_type, application.start_date.year)
    balance.pending -= application.total_days
    balance.used += application.total_days
    balance.save(update_fields=['pending', 'used', 'updated_at'])

    application.status = m.LeaveApplication.Status.APPROVED
    application.approved_by = approver_employee
    application.approved_on = timezone.now()
    application.save()
    return application


def reject_leave(application, approver_employee=None, reason=''):
    if application.status != m.LeaveApplication.Status.PENDING:
        raise LeaveError('Only pending applications can be rejected.')

    balance = get_or_create_balance(application.employee, application.leave_type, application.start_date.year)
    balance.pending -= application.total_days
    balance.save(update_fields=['pending', 'updated_at'])

    application.status = m.LeaveApplication.Status.REJECTED
    application.approved_by = approver_employee
    application.approved_on = timezone.now()
    application.rejection_reason = reason
    application.save()
    return application


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
