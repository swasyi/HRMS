import re
from datetime import datetime, timedelta
import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from hrms import leave_logic as lv
from hrms import models as m


class Command(BaseCommand):
    help = 'Imports all punches and manages auto-leaves for the full month.'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str, help='Path to the excel file')

    def handle(self, *args, **options):
        file_path = options['file_path']
        self.stdout.write(f"Processing file: {file_path}")

        try:
            df = pd.read_excel(file_path)
            df.columns = [str(col).replace('\n', ' ').strip() for col in df.columns]

            # 1. Map Date Columns (DD-MM-YYYY or D-M-YYYY)
            date_map = {}
            date_pattern = r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})'
            for col in df.columns:
                match = re.search(date_pattern, col)
                if match:
                    header_date_str = match.group(1).replace('/', '-')
                    try:
                        parsed_date = datetime.strptime(header_date_str, '%d-%m-%Y').date()
                        date_map[col] = parsed_date
                    except ValueError:
                        continue

            if not date_map:
                self.stdout.write(self.style.ERROR("No valid date columns (DD-MM-YYYY) found in headers!"))
                return

            LEAVE_CODES = ['CL', 'EL', 'SL', 'BL', 'MTL', 'CO', 'LWP', 'UL']
            success_punches = 0
            success_leaves = 0
            skipped_employees = []

            with transaction.atomic():
                for _, row in df.iterrows():
                    # Identify Employee ID column or fallback to first column
                    raw_id = row.get('Employee ID', row.iloc[0])
                    emp_code = str(raw_id).strip()
                    emp_name = str(row.get('Employee Name', row.iloc[1] if len(row) > 1 else '')).strip()

                    if not emp_code or emp_code.lower() in ['nan', 'none', 'employee', 'employee id', '']:
                        continue

                    # Resilient Employee Matching (matches code, stripped zeros, or full name)
                    employee = None
                    clean_code = emp_code.lstrip('0')

                    employee = m.Employee.objects.filter(
                        Q(employee_code__iexact=emp_code) |
                        Q(employee_code__iexact=clean_code) |
                        Q(employee_code__endswith=emp_code)
                    ).first()

                    if not employee and emp_name and emp_name.lower() not in ['nan', 'none']:
                        employee = m.Employee.objects.filter(
                            Q(first_name__icontains=emp_name) |
                            Q(user__first_name__icontains=emp_name)
                        ).first()

                    if not employee:
                        skipped_employees.append(f"{emp_code} ({emp_name})")
                        continue

                    grace_used_this_month = 0

                    for col_name, att_date in date_map.items():
                        cell_raw = str(row[col_name]).strip()
                        cell_value = cell_raw.replace('\n', ' ').replace('\r', ' ').upper().strip()

                        if not cell_value or cell_value in ['NAN', '', '-', 'WO', 'NA']:
                            continue

                        # Case A: Explicit Manual Leave Code
                        if any(code in cell_value for code in LEAVE_CODES):
                            code_to_use = 'LWP' if 'UL' in cell_value else cell_value
                            if employee.employment_type in ['intern', 'trainee'] and code_to_use != 'LWP':
                                continue

                            l_type = m.LeaveType.objects.filter(code=code_to_use, company=employee.company).first()
                            if l_type:
                                is_combined = ':' in cell_value
                                m.LeaveApplication.objects.update_or_create(
                                    employee=employee, start_date=att_date, end_date=att_date,
                                    defaults={
                                        'leave_type': l_type,
                                        'day_type': 'half' if is_combined else 'full',
                                        'total_days': 0.5 if is_combined else 1.0,
                                        'status': m.LeaveApplication.Status.APPROVED,
                                        'reason': 'Manual entry from Excel Grid'
                                    }
                                )
                                success_leaves += 1
                                if not is_combined:
                                    continue

                        # Case B: Explicit Absent
                        if cell_value in ['A', 'ABS', 'ABSENT']:
                            if employee.employment_type == 'full_time':
                                if not m.LeaveApplication.objects.filter(employee=employee,
                                                                         start_date=att_date).exists():
                                    cl_type = m.LeaveType.objects.filter(code='CL', company=employee.company).first()
                                    if cl_type:
                                        lv.apply_leave(employee, cl_type, att_date, att_date, day_type='full',
                                                       reason="Auto-convert Absent to Leave")
                                        success_leaves += 1
                            else:
                                m.AttendanceRecord.objects.update_or_create(
                                    employee=employee, attendance_date=att_date,
                                    defaults={'status': m.AttendanceRecord.Status.ABSENT, 'remarks': 'Marked Absent'}
                                )
                            continue

                        # Case C: Check-In & Check-Out Time Extraction
                        time_matches = re.findall(r'(\d{1,2}:\d{2}\s*(?:AM|PM))', cell_value, re.IGNORECASE)
                        if time_matches:
                            cin_dt = None
                            cout_dt = None

                            try:
                                cin_dt = datetime.strptime(f"{att_date} {time_matches[0].strip()}", '%Y-%m-%d %I:%M %p')
                                if len(time_matches) >= 2:
                                    cout_dt = datetime.strptime(f"{att_date} {time_matches[1].strip()}",
                                                                '%Y-%m-%d %I:%M %p')

                                m.AttendanceRecord.objects.update_or_create(
                                    employee=employee, attendance_date=att_date,
                                    defaults={
                                        'check_in': cin_dt,
                                        'check_out': cout_dt,
                                        'status': m.AttendanceRecord.Status.PRESENT if (
                                                    cin_dt and cout_dt) else m.AttendanceRecord.Status.HALF_DAY
                                    }
                                )
                                success_punches += 1

                                # Grace & Auto-Deduction for Full-Time
                                if employee.employment_type == 'full_time' and cin_dt:
                                    policy = employee.company.get_policy_for_date(att_date) if hasattr(employee.company,
                                                                                                       'get_policy_for_date') else None
                                    if policy:
                                        off_start = policy.office_start_time
                                        grace_limit = policy.grace_allowed_count
                                        grace_window = (datetime.combine(att_date, off_start) +
                                                        timedelta(minutes=policy.grace_minutes)).time()

                                        work_hrs = (cout_dt - cin_dt).total_seconds() / 3600 if (
                                                    cin_dt and cout_dt) else 0
                                        is_late = cin_dt.time() > grace_window
                                        is_short = (0 < work_hrs < 4) or (cout_dt is None)

                                        if (is_late or is_short) and not m.LeaveApplication.objects.filter(
                                                employee=employee, start_date=att_date).exists():
                                            cl_type = m.LeaveType.objects.filter(code='CL',
                                                                                 company=employee.company).first()
                                            if cl_type:
                                                if is_short:
                                                    lv.apply_leave(employee, cl_type, att_date, att_date,
                                                                   day_type='half',
                                                                   reason="Auto-Deduct: Short Work Duration / Missing Punch")
                                                    success_leaves += 1
                                                elif cin_dt.time() > grace_window:
                                                    lv.apply_leave(employee, cl_type, att_date, att_date,
                                                                   day_type='half',
                                                                   reason="Auto-Deduct: Late Arrival")
                                                    success_leaves += 1
                                                elif off_start < cin_dt.time() <= grace_window:
                                                    if grace_used_this_month < grace_limit:
                                                        grace_used_this_month += 1
                                                    else:
                                                        lv.apply_leave(employee, cl_type, att_date, att_date,
                                                                       day_type='half',
                                                                       reason="Auto-Deduct: Grace Exhausted")
                                                        success_leaves += 1
                            except Exception:
                                continue

            if skipped_employees:
                self.stdout.write(self.style.WARNING(
                    f"\nSkipped {len(skipped_employees)} unmatched employee rows:\n{', '.join(skipped_employees[:10])}..."))

            self.stdout.write(self.style.SUCCESS(
                f"\n--- SYNC COMPLETE ---\n- Total Punches Processed: {success_punches}\n- Total Auto-Leaves: {success_leaves}\n- Status: READY FOR PAYROLL"
            ))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"FATAL ERROR: {str(e)}"))