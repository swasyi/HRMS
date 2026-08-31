import csv
import io
from datetime import datetime
from django.core.management.base import BaseCommand
from hrms.models import Employee


class Command(BaseCommand):
    help = 'Updates Employee DOJ, Phone, and Employment Type from CSV'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='Path to the CSV file')

    def parse_date(self, date_str):
        """Helper to handle both 14.08.2000 and 21-03-2005 formats"""
        if not date_str or date_str.strip() == '':
            return None
        date_str = date_str.strip()
        for fmt in ("%d.%m.%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None

    def handle(self, *args, **options):
        file_path = options['csv_file']

        try:
            # 1. Open and skip the leading empty comma lines
            with open(file_path, mode='r', encoding='utf-8-sig') as f:
                lines = f.readlines()

            # Find where the actual data starts
            start_index = 0
            for i, line in enumerate(lines):
                if "Employee Id" in line:
                    start_index = i
                    break

            # Reconstruct CSV starting from the correct header row
            valid_content = "".join(lines[start_index:])
            reader = csv.DictReader(io.StringIO(valid_content))

            self.stdout.write(f"Processing headers: {reader.fieldnames}")

            updated_count = 0
            skipped_count = 0

            for row in reader:
                # Cleanup row keys (Excel adds spaces sometimes)
                data = {k.strip(): v for k, v in row.items() if k}

                emp_id = data.get('Employee Id', '').strip()
                if not emp_id: continue

                try:
                    employee = Employee.objects.get(employee_code=emp_id)

                    # Update Phone (Mapping "Personal Phone NO.")
                    phone = data.get('Personal Phone NO.', '').strip()
                    if phone:
                        employee.phone = phone

                    # Update Dates using our multi-format parser
                    doj = self.parse_date(data.get('DOJ', ''))
                    dob = self.parse_date(data.get('DOB', ''))

                    if doj: employee.date_of_joining = doj
                    if dob: employee.date_of_birth = dob

                    # Status & Employment Type Logic
                    status_text = data.get('Employement Status', '').strip().lower()

                    if status_text == 'confirmed':
                        employee.employment_type = Employee.EmploymentType.FULL_TIME
                        employee.status = Employee.Status.ACTIVE
                    elif 'probation' in status_text or 'trainee' in status_text:
                        employee.employment_type = Employee.EmploymentType.INTERN
                        employee.status = Employee.Status.ACTIVE
                    elif 'left' in status_text or 'terminated' in status_text:
                        employee.status = Employee.Status.RELIEVED

                    # Save ONLY the fields we updated
                    employee.save(update_fields=[
                        'phone', 'date_of_joining', 'date_of_birth',
                        'employment_type', 'status', 'updated_at'
                    ])

                    updated_count += 1
                    self.stdout.write(self.style.SUCCESS(f"✅ Updated: {emp_id}"))

                except Employee.DoesNotExist:
                    skipped_count += 1
                    self.stdout.write(self.style.WARNING(f"❓ Not in DB: {emp_id}"))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f"❌ Error on {emp_id}: {str(e)}"))

            self.stdout.write(self.style.SUCCESS(f"\nFinished! Updated: {updated_count}, Skipped: {skipped_count}"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Critical Error: {str(e)}"))