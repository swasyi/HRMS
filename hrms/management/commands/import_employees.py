import csv
import datetime
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from hrms.models import (
    Company, Department, Designation, Employee,
    EmployeeSalary, SalaryStructure, LeaveType, LeaveBalance
)

User = get_user_model()


class Command(BaseCommand):
    help = "Import employees from CSV with smart mapping and specific leave rules"

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str)

    def clean_key(self, key):
        """Standardizes headers: 'Employee Code' -> 'employeecode'"""
        if not key: return ""
        return str(key).strip().replace(" ", "").replace("_", "").lower()

    def parse_date_flexible(self, date_val):
        """Handles various date formats found in CSVs"""
        if not date_val: return None
        date_str = str(date_val).strip()
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%Y/%m/%d'):
            try:
                return datetime.datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None

    def handle(self, *args, **options):
        file_path = options['file_path']

        # 1. Setup Company & Salary Structure
        comp, _ = Company.objects.get_or_create(name="TechFlow Solutions")
        struct, _ = SalaryStructure.objects.get_or_create(company=comp, name="Standard Structure")

        # 2. Setup Leave Types
        leave_configs = [
            ('SL', 'Sick Leave', 10), ('EL', 'Earned Leave', 20),
            ('CL', 'Casual Leave', 12), ('BL', 'Bereavement Leave', 5),
            ('ML', 'Menstrual Leave', 24)  # 2 per month
        ]
        lt_objs = {}
        for code, name, days in leave_configs:
            lt, _ = LeaveType.objects.get_or_create(
                company=comp,
                code=code,
                defaults={'name': name, 'days_per_year': days}
            )
            lt_objs[code] = lt

        current_year = datetime.datetime.now().year
        count = 0

        # 3. Read CSV
        try:
            # utf-8-sig handles the 'BOM' hidden character Excel adds to CSVs
            with open(file_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)

                # Normalize the keys of the dictionary for this row
                for row in reader:
                    # Create a new dict with clean keys
                    clean_row = {self.clean_key(k): v for k, v in row.items()}

                    emp_code = str(clean_row.get('employeecode', '')).strip()
                    if not emp_code or emp_code == 'None':
                        continue

                    # --- DATA PROCESSING ---
                    fname = clean_row.get('firstname', 'Employee')
                    lname = clean_row.get('lastname', '')
                    email = clean_row.get('email', f"{emp_code}@tech.com")
                    uname = clean_row.get('username', fname.lower())

                    # Create User
                    user, _ = User.objects.get_or_create(username=uname, defaults={'email': email})
                    user.set_password(str(clean_row.get('password', 'Pass123')))
                    user.save()

                    # Dept/Desig
                    dept, _ = Department.objects.get_or_create(company=comp,
                                                               name=clean_row.get('department', 'General'))
                    desig, _ = Designation.objects.get_or_create(company=comp, department=dept,
                                                                 title=clean_row.get('designation', 'Staff'))

                    # Date
                    join_date = self.parse_date_flexible(clean_row.get('joindate')) or datetime.date.today()

                    # Employee
                    emp, _ = Employee.objects.update_or_create(
                        employee_code=emp_code,
                        defaults={
                            'company': comp,
                            'user': user,
                            'first_name': fname,
                            'last_name': lname,
                            'email': email,
                            'gender': str(clean_row.get('gender', 'M')).upper()[:1],
                            'employment_type': str(clean_row.get('employmenttype', 'full_time')).lower(),
                            'date_of_joining': join_date,
                            'department': dept,
                            'designation': desig,
                            'status': 'active'
                        }
                    )

                    # Salary
                    ctc = float(clean_row.get('ctc') or 0)
                    EmployeeSalary.objects.update_or_create(
                        employee=emp,
                        defaults={
                            'structure': struct,
                            'ctc_annual': ctc,
                            'basic': ctc * 0.5,
                            'hra': ctc * 0.2,
                            'effective_from': join_date,
                            'is_active': True
                        }
                    )

                    # --- LEAVE ASSIGNMENT LOGIC ---
                    etype = emp.employment_type
                    gender = emp.gender

                    if etype == 'full_time':
                        # Permanent gets SL, EL, CL, BL
                        for code in ['SL', 'EL', 'CL', 'BL']:
                            LeaveBalance.objects.get_or_create(
                                employee=emp,
                                leave_type=lt_objs[code],
                                year=current_year,
                                defaults={'allocated': lt_objs[code].days_per_year}
                            )
                    else:
                        # Probation/Contract/Intern
                        if gender == 'F':
                            # Women on probation get 2 Menstrual Leaves per month
                            LeaveBalance.objects.get_or_create(
                                employee=emp,
                                leave_type=lt_objs['ML'],
                                year=current_year,
                                defaults={'allocated': 24}
                            )
                        # Note: Males on probation get 0 balances automatically

                    self.stdout.write(self.style.SUCCESS(f"Imported: {fname} {lname} ({emp_code})"))
                    count += 1

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Critical Error: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Successfully imported {count} employees from CSV."))