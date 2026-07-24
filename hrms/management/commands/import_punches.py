import csv
import re
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone
from hrms.models import Employee, AttendanceRecord


class Command(BaseCommand):
    help = 'Imports attendance punches from a daily_punch_report CSV file'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str, help='Path to the csv file')

    def handle(self, *args, **options):
        file_path = options['file_path']

        try:
            with open(file_path, mode='r', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader)

                # Identify date columns (e.g., "01-06-2026 \n Monday")
                # We skip the first 4 columns (ID, Name, Dept, Desig)
                date_map = {}
                for i in range(4, len(header)):
                    date_match = re.search(r'(\d{2}-\d{2}-\d{4})', header[i])
                    if date_match:
                        date_map[i] = datetime.strptime(date_match.group(1), '%d-%m-%Y').date()

                count = 0
                for row in reader:
                    emp_code = row[0].strip()
                    try:
                        employee = Employee.objects.get(employee_code=emp_code)
                    except Employee.DoesNotExist:
                        self.stdout.write(self.style.WARNING(f"Employee {emp_code} not found. Skipping."))
                        continue

                    for idx, att_date in date_map.items():
                        cell = row[idx].strip()
                        if not cell:
                            continue

                        # Split "09:41 AM\n06:02 PM" into two parts
                        punches = cell.split('\n')
                        check_in_str = punches[0].strip()
                        check_out_str = punches[1].strip() if len(punches) > 1 else None

                        # Helper to convert "09:41 AM" and date into a full datetime
                        def parse_time(time_str):
                            if not time_str: return None
                            dt_str = f"{att_date.strftime('%Y-%m-%d')} {time_str}"
                            return datetime.strptime(dt_str, '%Y-%m-%d %I:%M %p')

                        check_in_dt = parse_time(check_in_str)
                        check_out_dt = parse_time(check_out_str)

                        # Create or Update the Attendance Record
                        obj, created = AttendanceRecord.objects.update_or_create(
                            employee=employee,
                            attendance_date=att_date,
                            defaults={
                                'check_in': check_in_dt,
                                'check_out': check_out_dt,
                                'status': 'present'
                            }
                        )
                        count += 1

                self.stdout.write(self.style.SUCCESS(f"Successfully imported {count} punch records."))

        except FileNotFoundError:
            self.stdout.write(self.style.ERROR("File not found!"))