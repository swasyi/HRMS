import csv
from datetime import datetime, date
from django.core.management.base import BaseCommand
from hrms.models import Company, Department, Designation, Employee, AttendanceRecord

class Command(BaseCommand):
    help = 'Imports employees and attendance from the muster report CSV'

    def handle(self, *args, **kwargs):
        # 1. Setup Company (Matches your brand name)
        company, _ = Company.objects.get_or_create(
            name="ObluHealthcare",
            defaults={'email': 'admin@obluhealthcare.com'}
        )
        # IMPORTANT: Ensure this points to a .csv file, not .xlsx
        file_path = r'C:\Users\Lenovo\Downloads\muster_report.csv'

        try:
            with open(file_path, mode='r', encoding='latin-1') as f:
                # DictReader reads the first row as headers
                reader = csv.DictReader(f)

                # CLEAN THE HEADERS: Remove newlines (\n) so "01\nJune\nSunday" becomes "01 June Sunday"
                original_headers = reader.fieldnames
                self.stdout.write(f"Headers found in file: {original_headers}")

                cleaned_headers = [h.replace('\n', ' ').strip() for h in original_headers]
                reader.fieldnames = cleaned_headers

                # Identify columns that contain attendance data for June
                date_columns = [col for col in cleaned_headers if 'July' in col]
                self.stdout.write(f"Date columns identified: {len(date_columns)}")

                if not date_columns:
                    self.stdout.write(self.style.ERROR("No 'July' columns found. Is the file saved as CSV?"))
                    return

                for row in reader:
                    emp_id = row.get('Employee ID')
                    emp_name = row.get('Employee Name')

                    if not emp_id or not emp_name:
                        continue

                    # 2. Get or Create Department
                    dept_name = row.get('Department', 'General')
                    department_obj, _ = Department.objects.get_or_create(
                        company=company,
                        name=dept_name
                    )

                    # 3. Get or Create Designation
                    desig_title = row.get('Designation', 'Staff')
                    designation_obj, _ = Designation.objects.get_or_create(
                        company=company,
                        department=department_obj,
                        title=desig_title
                    )

                    # 4. Create/Update Employee
                    name_parts = emp_name.split(' ', 1)
                    first_name = name_parts[0]
                    last_name = name_parts[1] if len(name_parts) > 1 else ""

                    employee_obj, _ = Employee.objects.update_or_create(
                        employee_code=emp_id,
                        defaults={
                            'company': company,
                            'department': department_obj,
                            'designation': designation_obj,
                            'first_name': first_name,
                            'last_name': last_name,
                            'email': f"{emp_id.lower()}@obluhealthcare.com",
                            'date_of_joining': '2024-01-01',
                            'status': Employee.Status.ACTIVE
                        }
                    )

                    # 5. Process daily attendance columns
                    for col in date_columns:
                        raw_status = row.get(col)
                        if not raw_status or raw_status.strip() in ['-', '']:
                            continue

                        status_code = raw_status.strip().upper()

                        # Parse day from header (e.g., "01 June Sunday" -> 1)
                        try:
                            day_val = int(col.split(' ')[0])
                            punch_date = datetime(2026, 6, day_val).date()
                        except (ValueError, IndexError):
                            continue

                        # Professional mapping logic
                        if 'WO' in status_code:
                            final_status = AttendanceRecord.Status.WEEK_OFF
                        elif 'HD' in status_code:
                            final_status = AttendanceRecord.Status.HALF_DAY
                        elif 'A' in status_code or 'LWP' in status_code:
                            final_status = AttendanceRecord.Status.ABSENT
                        elif 'L' in status_code: # Matches L1, L2, L4
                            final_status = AttendanceRecord.Status.ON_LEAVE
                        else:
                            final_status = AttendanceRecord.Status.PRESENT

                        # Update or Create the Attendance Record
                        AttendanceRecord.objects.update_or_create(
                            employee=employee_obj,
                            attendance_date=punch_date,
                            defaults={
                                'status': final_status,
                                'remarks': f"Imported: {status_code}"
                            }
                        )

                    self.stdout.write(self.style.SUCCESS(f"Successfully processed: {employee_obj.full_name}"))

        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f"File not found at: {file_path}"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"An error occurred: {str(e)}"))

    def handle(self, *args, **kwargs):
        # 1. Setup Company
        company, _ = Company.objects.get_or_create(name="ObluHealthcare", defaults={'email': 'admin@company.com'})

        file_path = r'C:\Users\Lenovo\Downloads\attendance-report.xls'

        self.stdout.write(f"Looking for file at: {file_path}")

        try:
            # Change to 'utf-8-sig' to remove the ï»¿ characters automatically
            with open(file_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)

                # Clean header names (remove newlines and extra spaces)
                original_headers = reader.fieldnames
                cleaned_headers = [h.replace('\n', ' ').strip() for h in original_headers]
                reader.fieldnames = cleaned_headers

                # Identify date columns
                date_columns = [col for col in cleaned_headers if 'June' in col]
                self.stdout.write(f"Date columns identified: {len(date_columns)}")

                if not date_columns:
                    self.stdout.write(self.style.ERROR("No 'June' columns found. stopping."))
                    return

                processed_count = 0
                for row in reader:
                    # Use the cleaned key 'Employee ID'
                    emp_id = row.get('Employee ID')
                    emp_name = row.get('Employee Name')

                    # If the script still can't find 'Employee ID', try the first column in the row
                    if not emp_id:
                        emp_id = list(row.values())[0]

                    if not emp_id or emp_id == '':
                        continue

                    # --- START DATABASE LOGIC ---

                    # Create Dept & Desig
                    dept, _ = Department.objects.get_or_create(company=company, name=row.get('Department', 'General'))
                    desig, _ = Designation.objects.get_or_create(company=company, department=dept,
                                                                 title=row.get('Designation', 'Staff'))

                    # Create/Update Employee
                    name_parts = str(emp_name).split(' ', 1)
                    f_name = name_parts[0]
                    l_name = name_parts[1] if len(name_parts) > 1 else ""

                    employee_obj, _ = Employee.objects.update_or_create(
                        employee_code=emp_id,
                        defaults={
                            'company': company,
                            'department': dept,
                            'designation': desig,
                            'first_name': f_name,
                            'last_name': l_name,
                            'email': f"{emp_id.lower()}@obluhealthcare.com",
                            'date_of_joining': '2024-01-01',
                            'status': Employee.Status.ACTIVE
                        }
                    )

                    # Process Dates
                    for col in date_columns:
                        raw_status = row.get(col)
                        if not raw_status or raw_status.strip() in ['-', '']:
                            continue

                        status_code = raw_status.strip().upper()
                        day_val = int(col.split(' ')[0])
                        punch_date = date(2026, 6, day_val)

                        # Logic mapping
                        if 'WO' in status_code:
                            final_status = AttendanceRecord.Status.WEEK_OFF
                        elif 'HD' in status_code:
                            final_status = AttendanceRecord.Status.HALF_DAY
                        elif 'A' in status_code or 'LWP' in status_code:
                            final_status = AttendanceRecord.Status.ABSENT
                        elif 'L' in status_code:
                            final_status = AttendanceRecord.Status.ON_LEAVE
                        else:
                            final_status = AttendanceRecord.Status.PRESENT

                        AttendanceRecord.objects.update_or_create(
                            employee=employee_obj,
                            attendance_date=punch_date,
                            defaults={'status': final_status, 'remarks': f"Imported: {status_code}"}
                        )

                    processed_count += 1
                    self.stdout.write(self.style.SUCCESS(f"Successfully processed: {emp_id} - {emp_name}"))

                self.stdout.write(
                    self.style.SUCCESS(f"DONE! Total employees imported/synced: {processed_count}"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"CRITICAL ERROR: {str(e)}"))