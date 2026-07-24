import pandas as pd
from django.core.management.base import BaseCommand
from django.utils import timezone
from hrms import models as m  # Adjust 'hrms' to your actual app name
from datetime import date


class Command(BaseCommand):
    help = 'Import Bank Details and Salary (Gross Wages) from Excel'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str, help='Path to the excel/csv file')

    def handle(self, *args, **options):
        file_path = options['file_path']
        june_first = date(2026, 6, 1) # <--- ADD THIS



        # Read the file (handles both csv and xlsx)
        if file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)

        count_bank = 0
        count_salary = 0

        for index, row in df.iterrows():
            emp_id = str(row.get('Employee ID')).strip()

            try:
                employee = m.Employee.objects.get(employee_code=emp_id)
            except m.Employee.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"Employee {emp_id} not found. Skipping..."))
                continue

            # 1. Update/Create Bank Details
            bank_name = row.get('Bank Name')
            acc_no = row.get('Bank Account No')

            # Only update if bank info is present in the sheet
            if pd.notna(bank_name) and pd.notna(acc_no):
                m.EmployeeBankDetail.objects.update_or_create(
                    employee=employee,
                    defaults={
                        'account_holder': employee.full_name,
                        'account_number': str(acc_no),
                        'ifsc_code': str(row.get('IFSC Code', '')),
                        'bank_name': str(bank_name),
                        'branch_name': str(row.get('Bank Branch Name', '')),
                        'account_type': str(row.get('Account Type', 'Saving')),
                    }
                )
                count_bank += 1

            # 2. Update/Create Salary Structure
            gross_wage = row.get('Gross Wages', 0)
            if pd.notna(gross_wage) and gross_wage > 0:
                # Assuming Gross Wages is monthly, calculating Annual CTC
                annual_ctc = float(gross_wage) * 12

                # We update the active salary or create a new one
                m.EmployeeSalary.objects.update_or_create(
                    employee=employee,
                    is_active=True,
                    defaults={
                        'ctc_annual': annual_ctc,
                        'basic': float(gross_wage) * 0.50,  # Example: setting basic as 50% of gross
                        'effective_from': june_first,
                    }
                )
                count_salary += 1

        self.stdout.write(self.style.SUCCESS(
            f"Successfully updated {count_bank} Bank Details and {count_salary} Salary records."
        ))