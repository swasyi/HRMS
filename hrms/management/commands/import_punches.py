import pandas as pd
import re
from datetime import datetime
from django.core.management.base import BaseCommand
from hrms.models import Employee, AttendanceRecord


class Command(BaseCommand):
    help = 'Imports attendance punches (Check-In/Out) from Excel/CSV reports'

    def handle(self, *args, **options):
        # 1. Hardcoded File Path (Update this as needed)
        file_path = r'C:\Users\Lenovo\Downloads\daily_punch_report aug.xlsx'

        self.stdout.write(f"Reading file: {file_path}")

        try:
            # 2. Load File (Supports .xlsx, .xls, .csv)
            if file_path.endswith('.csv'):
                df = pd.read_csv(file_path, encoding='utf-8')
            else:
                df = pd.read_excel(file_path)

            # 3. Clean headers
            # Excel sometimes puts newlines in headers; we clean them to find dates easily
            df.columns = [str(col).replace('\n', ' ').strip() for col in df.columns]

            # 4. Identify Date Columns
            # Looks for any header that contains a date pattern like DD-MM-YYYY or DD/MM/YYYY
            date_map = {}
            date_pattern = r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})'

            for col in df.columns:
                match = re.search(date_pattern, col)
                if match:
                    # Convert the string date found in header to a python date object
                    header_date_str = match.group(1).replace('/', '-')
                    try:
                        parsed_date = datetime.strptime(header_date_str, '%d-%m-%Y').date()
                        date_map[col] = parsed_date
                    except ValueError:
                        continue

            if not date_map:
                self.stdout.write(self.style.ERROR("No date columns (DD-MM-YYYY) found in the header!"))
                return

            self.stdout.write(f"Detected {len(date_map)} date columns.")

            count = 0
            # 5. Iterate through rows
            for _, row in df.iterrows():
                # Assuming first column or 'Employee ID' contains the code
                emp_code = str(row.iloc[0]).strip() if 'Employee ID' not in df.columns else str(
                    row['Employee ID']).strip()

                # Skip header-like rows or empty rows
                if not emp_code or emp_code == 'nan' or emp_code == 'None':
                    continue

                try:
                    employee = Employee.objects.get(employee_code=emp_code)
                except Employee.DoesNotExist:
                    # Optional: self.stdout.write(f"Skipping: Employee {emp_code} not found.")
                    continue

                for col_name, att_date in date_map.items():
                    cell_value = str(row[col_name]).strip()

                    # Skip empty cells
                    if not cell_value or cell_value.lower() in ['nan', '', 'absent', '-']:
                        continue

                    # 6. Parse Punches
                    # The cell usually looks like: "09:41 AM\n06:02 PM" or "09:41 AM 06:02 PM"
                    # We split by any whitespace or newline
                    punches = cell_value.split()
                    # punches[0] -> "09:41", punches[1] -> "AM", punches[2] -> "06:02", punches[3] -> "PM"

                    check_in_dt = None
                    check_out_dt = None

                    try:
                        # Reconstruct Time (Handles "09:41 AM" format)
                        if len(punches) >= 2:
                            in_time_str = f"{punches[0]} {punches[1]}"
                            check_in_dt = datetime.strptime(f"{att_date} {in_time_str}", '%Y-%m-%d %I:%M %p')

                        if len(punches) >= 4:
                            out_time_str = f"{punches[2]} {punches[3]}"
                            check_out_dt = datetime.strptime(f"{att_date} {out_time_str}", '%Y-%m-%d %I:%M %p')
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(f"Error parsing time for {emp_code} on {att_date}: {e}"))
                        continue

                    # 7. Update or Create the Attendance Record
                    # We set status to 'PRESENT' because there is punch data
                    AttendanceRecord.objects.update_or_create(
                        employee=employee,
                        attendance_date=att_date,
                        defaults={
                            'check_in': check_in_dt,
                            'check_out': check_out_dt,
                            'status': AttendanceRecord.Status.PRESENT,
                            'remarks': f"Imported Punch: {cell_value.replace('\n', ' ')}"
                        }
                    )
                    count += 1

                self.stdout.write(self.style.SUCCESS(f"Processed: {employee.full_name}"))

            self.stdout.write(self.style.SUCCESS(f"Successfully imported {count} punch records."))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"CRITICAL ERROR: {str(e)}"))
            import traceback
            traceback.print_exc()