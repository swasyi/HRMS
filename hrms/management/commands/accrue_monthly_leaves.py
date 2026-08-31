"""
Management command: accrue_monthly_leaves

Runs monthly (1st of each month or triggered manually).
For each confirmed active employee, credits days_per_year/12 for leave types
that have allocation_mode='monthly_accrued' (typically CL and EL).

Usage:
    python manage.py accrue_monthly_leaves
    python manage.py accrue_monthly_leaves --month 7 --year 2026
    python manage.py accrue_monthly_leaves --dry-run
"""
from datetime import date

from django.core.management.base import BaseCommand
from django.utils import timezone

from hrms import models as m


class Command(BaseCommand):
    help = 'Credit monthly leave accruals for CL/EL (allocation_mode=monthly_accrued)'

    def add_arguments(self, parser):
        parser.add_argument('--month', type=int, help='Month to accrue for (1-12). Default: current month.')
        parser.add_argument('--year', type=int, help='Year to accrue for. Default: current year.')
        parser.add_argument('--dry-run', action='store_true', help='Preview without making changes.')

    def handle(self, *args, **options):
        today = date.today()
        month = options.get('month') or today.month
        year = options.get('year') or today.year
        dry_run = options.get('dry_run', False)

        self.stdout.write(f"\n{'[DRY RUN] ' if dry_run else ''}Processing monthly leave accrual for {month}/{year}...\n")

        # Get all monthly-accrued leave types
        accrual_leave_types = m.LeaveType.objects.filter(
            allocation_mode='monthly_accrued'
        )

        if not accrual_leave_types.exists():
            self.stdout.write(self.style.WARNING('No leave types with allocation_mode=monthly_accrued found.'))
            return

        # Get all active, confirmed employees
        active_employees = m.Employee.objects.filter(
            status=m.Employee.Status.ACTIVE,
            date_of_confirmation__isnull=False,
        ).exclude(employment_type='intern')

        credited_count = 0
        skipped_count = 0

        for employee in active_employees:
            for lt in accrual_leave_types.filter(company=employee.company):
                # Check gender applicability
                if lt.applicable_gender != 'all' and employee.gender != lt.applicable_gender:
                    continue

                # Check if already accrued this month
                accrual, created = m.MonthlyLeaveAccrual.objects.get_or_create(
                    employee=employee,
                    leave_type=lt,
                    month=month,
                    year=year,
                    defaults={
                        'accrued_amount': round(float(lt.days_per_year) / 12, 2),
                        'is_credited': False,
                    }
                )

                if accrual.is_credited:
                    skipped_count += 1
                    continue

                credit_amount = round(float(lt.days_per_year) / 12, 2)

                if dry_run:
                    self.stdout.write(
                        f"  [DRY RUN] Would credit {credit_amount} {lt.code} to "
                        f"{employee.full_name} ({employee.employee_code})"
                    )
                    credited_count += 1
                    continue

                # Credit to live balance
                field_map = {
                    'CL': 'casual_leave', 'EL': 'earned_leave', 'SL': 'sick_leave',
                    'ML': 'menstrual_leave', 'MTL': 'menstrual_leave',
                    'BL': 'bereavement_leave', 'CO': 'comp_off',
                }
                field = field_map.get(lt.code.upper())
                if field:
                    live, _ = m.EmployeeLeaveBalanceLive.objects.get_or_create(e_name=employee)
                    current = getattr(live, field, 0)
                    setattr(live, field, float(current) + credit_amount)
                    live.save()

                    # Mark accrual as credited
                    accrual.is_credited = True
                    accrual.accrued_amount = credit_amount
                    accrual.credited_on = timezone.now()
                    accrual.save()

                    credited_count += 1
                    self.stdout.write(
                        f"  ✓ Credited {credit_amount} {lt.code} to "
                        f"{employee.full_name} ({employee.employee_code})"
                    )

        self.stdout.write(self.style.SUCCESS(
            f"\n{'[DRY RUN] ' if dry_run else ''}Done! "
            f"Credited: {credited_count}, Skipped (already done): {skipped_count}\n"
        ))
