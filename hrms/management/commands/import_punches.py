import pandas as pd
import re
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from hrms import models as m
from hrms import leave_logic as lv  # Import centralized leave logic


class Command(BaseCommand):
    help = 'Imports punches and auto-manages leaves for permanent employees based on grace and work duration.'

    def add_arguments(self, parser):
        # Path to the monthly Excel file
        parser.add_argument('file_path', type=str, help='Path to the excel file')

    def handle(self, *args, **options):
        file_path = options['file_path']
        self.stdout.write(f"Processing file: {file_path}")

        try:
            # Load Excel and clean white spaces from headers
            df = pd.read_excel(file_path)
            df.columns = [str(col).replace('\n', ' ').strip() for col in df.columns]

            # 1. MAP DATE COLUMNS
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
                self.stdout.write(self.style.ERROR("No date columns (DD-MM-YYYY) found in headers!"))
                return

            # Constants for processing
            LEAVE_CODES = ['CL', 'EL', 'SL', 'BL', 'MTL', 'CO', 'LWP', 'UL']
            success_punches = 0
            success_leaves = 0

            with transaction.atomic():
                for _, row in df.iterrows():
                    emp_code = str(row.iloc[0]).strip()
                    if not emp_code or emp_code.lower() in ['nan', 'none', 'employee']:
                        continue

                    try:
                        employee = m.Employee.objects.get(employee_code=emp_code)
                    except m.Employee.DoesNotExist:
                        continue

                    # --- RESET GRACE TRACKER FOR EVERY EMPLOYEE PER MONTH ---
                    grace_used_this_month = 0

                    for col_name, att_date in date_map.items():
                        cell_value = str(row[col_name]).strip().upper()

                        # Skip Week-offs or empty cells
                        if not cell_value or cell_value in ['NAN', '', '-', 'WO', 'NA']:
                            continue

                        # --- CASE A: MANUAL LEAVE CODES IN EXCEL ---
                        if any(code in cell_value for code in LEAVE_CODES):
                            code_to_use = 'LWP' if 'UL' in cell_value else cell_value

                            # Filter out paid leaves for non-permanent staff
                            if employee.employment_type in ['intern', 'trainee'] and code_to_use != 'LWP':
                                continue

                            l_type = m.LeaveType.objects.filter(code=code_to_use, company=employee.company).first()
                            if l_type:
                                # Determine if cell also contains a punch (Half-Day scenario)
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
                                    continue  # Skip punch logic for full day leave

                        # --- CASE B: EXPLICIT ABSENT ---
                        if cell_value in ['A', 'ABSENT']:

                            if employee.employment_type == 'full_time':
                                # Don't mark Absent! Instead, try to apply 1.0 Full Day Leave
                                if not m.LeaveApplication.objects.filter(employee=employee,
                                                                         start_date=att_date).exists():
                                    cl_type = m.LeaveType.objects.filter(code='CL', company=employee.company).first()
                                    if cl_type:
                                        lv.apply_leave(employee, cl_type, att_date, att_date, day_type='full',
                                                       reason="Auto-convert Absent to Leave")
                                        success_leaves += 1
                            else:
                                # Interns/Trainees stay Absent (Money Deduct)
                                m.AttendanceRecord.objects.update_or_create(
                                    employee=employee, attendance_date=att_date,
                                    defaults={'status': m.AttendanceRecord.Status.ABSENT, 'remarks': 'Marked Absent'}
                                )
                            continue

                        # --- CASE C: PUNCH TIMES & SMART AUTO-LEAVE ---
                        punches = cell_value.split()
                        if len(punches) >= 2:
                            try:
                                in_t = f"{punches[0]} {punches[1]}"
                                cin_dt = datetime.strptime(f"{att_date} {in_t}", '%Y-%m-%d %I:%M %p')

                                cout_dt = None
                                if len(punches) >= 4:
                                    out_t = f"{punches[2]} {punches[3]}"
                                    cout_dt = datetime.strptime(f"{att_date} {out_t}", '%Y-%m-%d %I:%M %p')

                                # 1. Save the Attendance Record
                                m.AttendanceRecord.objects.update_or_create(
                                    employee=employee, attendance_date=att_date,
                                    defaults={'check_in': cin_dt, 'check_out': cout_dt, 'status': 'present'}
                                )
                                success_punches += 1

                                # 2. SMART AUTO-DEDUCTION (FOR FULL-TIME ONLY)
                                if employee.employment_type == 'full_time':
                                    policy = employee.company.get_policy_for_date(att_date)
                                    off_start = policy.office_start_time
                                    grace_limit = policy.grace_allowed_count
                                    grace_window = (datetime.combine(att_date, off_start) +
                                                    timedelta(minutes=policy.grace_minutes)).time()

                                    # Work duration check (Short Punch Logic)
                                    work_hrs = (cout_dt - cin_dt).total_seconds() / 3600 if (cin_dt and cout_dt) else 0
                                    is_late = cin_dt.time() > grace_window
                                    is_short = work_hrs > 0 and work_hrs < 4

                                    # Check for existing applications to prevent duplicate deductions
                                    if (is_late or is_short) and not m.LeaveApplication.objects.filter(
                                            employee=employee, start_date=att_date).exists():
                                        cl_type = m.LeaveType.objects.filter(code='CL',
                                                                             company=employee.company).first()

                                        if cl_type:
                                            # LOGIC 1: WORKED LESS THAN 4 HOURS -> DEDUCT 1.0 DAY
                                            if work_hrs > 0 and work_hrs < 4:
                                                lv.apply_leave(employee, cl_type, att_date, att_date,
                                                               day_type='full',
                                                               reason="Auto-Deduct: Short Work Duration (<4h)")
                                                success_leaves += 1

                                            # LOGIC 2: ARRIVED BEYOND GRACE WINDOW (>10:15) -> DEDUCT 0.5 DAY
                                            elif cin_dt.time() > grace_window:
                                                lv.apply_leave(employee, cl_type, att_date, att_date,
                                                               day_type='half', reason="Auto-Deduct: Late Arrival")
                                                success_leaves += 1

                                            # LOGIC 3: ARRIVED INSIDE GRACE WINDOW (10:01 - 10:15)
                                            elif cin_dt.time() > off_start and cin_dt.time() <= grace_window:
                                                if grace_used_this_month < grace_limit:
                                                    # Free Grace used, just increment counter
                                                    grace_used_this_month += 1
                                                else:
                                                    # Grace Exhausted -> Deduct 0.5 Day
                                                    lv.apply_leave(employee, cl_type, att_date, att_date,
                                                                   day_type='half',
                                                                   reason="Auto-Deduct: Grace Exhausted")
                                                    success_leaves += 1
                            except Exception:
                                continue

                self.stdout.write(self.style.SUCCESS(f"Finished Employee: {employee.full_name}"))

            self.stdout.write(self.style.SUCCESS(
                f"\n--- SYNC COMPLETE ---\n- Punches: {success_punches}\n- Auto-Leaves: {success_leaves}\n- Status: READY FOR PAYROLL"
            ))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"FATAL ERROR: {str(e)}"))