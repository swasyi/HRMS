"""
HRMS Service Layer — encapsulates business processes such as attendance penalties,
leave deductions, and automated payroll adjustments.
"""
from decimal import Decimal
import logging
from django.db import transaction
from django.utils import timezone
from . import models as m

logger = logging.getLogger(__name__)


def process_late_arrival_penalty(record, reason=None):
    """
    Automated Late Arrival Penalty and Leave Deduction system.
    
    Triggered when an employee exceeds their monthly grace limit or arrives
    beyond the grace window and is marked as 'Half Day' (HD).
    
    Business Rules:
    1. Duplicate Prevention: Checks if a penalty already exists for (employee, penalty_date).
    2. Fallback Deduction Hierarchy (for Full-Time):
       - First Priority: Deduct 0.5 from CL (Casual Leave) if CL >= 0.5.
       - Second Priority: Deduct 0.5 from EL (Earned Leave) if EL >= 0.5.
       - Third Priority: If both CL and EL < 0.5, mark Deduction Source as 'LWP / Salary' (0.5 day wage deduction).
    3. Intern Handling:
       - Direct half-day stipend/salary calculation without deducting from standard paid leaves.
    4. Records entry in AttendancePenalty with status, source, and audit timestamps.
    """
    if not record or not record.employee:
        return None

    employee = record.employee
    penalty_date = record.attendance_date or timezone.localdate()

    # 1. Prevent duplicate penalty logs for the same employee and date
    existing_penalty = m.AttendancePenalty.objects.filter(
        employee=employee,
        penalty_date=penalty_date
    ).first()
    if existing_penalty:
        return existing_penalty

    late_mins = int(record.late_minutes or 0)
    deduction_amount = Decimal('0.5')
    employment_type = getattr(employee, 'employment_type', 'full_time') or 'full_time'

    penalty_reason = reason or f"Grace limit exceeded - Late Arrival ({late_mins} mins late)"

    with transaction.atomic():
        # Case A: INTERN EMPLOYEES
        if employment_type == 'intern':
            salary_deduction = Decimal('0.00')
            try:
                emp_salary = m.EmployeeSalary.objects.filter(
                    employee=employee, is_active=True
                ).first()
                if emp_salary and emp_salary.ctc_annual:
                    daily_wage = (emp_salary.ctc_annual / Decimal('12')) / Decimal('30')
                    salary_deduction = (daily_wage / Decimal('2')).quantize(Decimal('0.01'))
            except Exception as e:
                logger.warning(f"Failed calculating intern salary deduction for {employee}: {e}")

            penalty = m.AttendancePenalty.objects.create(
                employee=employee,
                attendance_record=record,
                penalty_date=penalty_date,
                reason=penalty_reason,
                late_minutes=late_mins,
                deduction_days=deduction_amount,
                deduction_source="LWP / Salary",
                status=m.AttendancePenalty.DeductionStatus.INTERN_SALARY,
                is_intern_penalty=True,
                salary_deduction_amount=salary_deduction,
                employment_type_snapshot=employment_type,
            )
            return penalty

        # Case B: FULL-TIME EMPLOYEES (CL -> EL -> LWP Hierarchy)
        live_balance, _ = m.EmployeeLeaveBalanceLive.objects.select_for_update().get_or_create(e_name=employee)
        bank_balance = m.EmployeeLeaveBalance.objects.select_for_update().filter(e_name=employee).first()

        cl_available = Decimal(str(live_balance.casual_leave or 0.0))
        el_available = Decimal(str(live_balance.earned_leave or 0.0))

        if cl_available >= deduction_amount:
            # First priority: Deduct 0.5 from CL
            new_cl = float(cl_available - deduction_amount)
            live_balance.casual_leave = new_cl
            live_balance.save(update_fields=['casual_leave'])

            if bank_balance:
                bank_balance.casual_leave = max(0.0, float(Decimal(str(bank_balance.casual_leave or 0.0)) - deduction_amount))
                bank_balance.save(update_fields=['casual_leave'])

            deduction_source = "CL"
            penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED

        elif el_available >= deduction_amount:
            # Second priority: Deduct 0.5 from EL
            new_el = float(el_available - deduction_amount)
            live_balance.earned_leave = new_el
            live_balance.save(update_fields=['earned_leave'])

            if bank_balance:
                bank_balance.earned_leave = max(0.0, float(Decimal(str(bank_balance.earned_leave or 0.0)) - deduction_amount))
                bank_balance.save(update_fields=['earned_leave'])

            deduction_source = "EL"
            penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED

        else:
            # Third priority: Both exhausted -> Mark as LWP / Salary deduction
            deduction_source = "LWP / Salary"
            penalty_status = m.AttendancePenalty.DeductionStatus.LWP

        penalty = m.AttendancePenalty.objects.create(
            employee=employee,
            attendance_record=record,
            penalty_date=penalty_date,
            reason=penalty_reason,
            late_minutes=late_mins,
            deduction_days=deduction_amount,
            deduction_source=deduction_source,
            status=penalty_status,
            is_intern_penalty=False,
            employment_type_snapshot=employment_type,
        )
        return penalty


"""
HRMS Service Layer — encapsulates business processes such as attendance penalties,
leave deductions, automated payroll adjustments, and applicant resume parsing.
"""
import io
import os
import re
import uuid
from decimal import Decimal
import logging

from django.core.files.base import ContentFile
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    from PyPDF2 import PdfReader

from . import models as m

logger = logging.getLogger(__name__)


def _normalize_text(value):
    if value is None:
        return ""
    return " ".join(str(value).replace("\r", "\n").split())


def _extract_pdf_text(file_path_or_buffer):
    """Read raw text from a PDF file path or file-like object."""
    if isinstance(file_path_or_buffer, (str, os.PathLike)):
        with open(file_path_or_buffer, "rb") as file_handle:
            return _extract_pdf_text(file_handle)

    file_obj = file_path_or_buffer
    if hasattr(file_obj, "read"):
        data = file_obj.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        file_obj = io.BytesIO(data)
    else:
        raise ValueError("Unsupported resume input. Expected a file path or file-like object.")

    try:
        reader = PdfReader(file_obj)
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"Unable to read PDF content: {exc}") from exc

    pages = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text:
            pages.append(page_text)

    extracted = "\n".join(pages).strip()
    if not extracted:
        raise ValueError("The PDF file did not contain readable text.")
    return extracted


def _extract_email(text):
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text, flags=re.IGNORECASE)
    return match.group(0).strip() if match else ""


def _extract_phone_number(text):
    phone_candidates = []
    for match in re.finditer(
            r"(?:(?:\+?\d{1,3})[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3}[\s.-]?\d{4}",
            text,
    ):
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if 10 <= len(digits) <= 12:
            phone_candidates.append(raw.strip())

    if not phone_candidates:
        return ""
    for candidate in phone_candidates:
        if "+" in candidate:
            return candidate
    return phone_candidates[0]


def _extract_name(text):
    patterns = [
        r"(?:^|[\n\r])\s*(?:name\s*[:\-]?)\s*([A-Z][A-Za-z'.\- ]{2,60})",
        r"(?:^|[\n\r])\s*([A-Z][A-Za-z'.\-]{2,}\s+[A-Z][A-Za-z'.\-]{2,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if len(candidate.split()) >= 2:
                return candidate

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:20]:
        if re.search(r"resume|curriculum|profile|summary|experience|education|contact", line, re.IGNORECASE):
            continue
        cleaned = re.sub(r"[^A-Za-z\s'.-]", "", line).strip()
        words = cleaned.split()
        if 2 <= len(words) <= 4 and all(word and word[0].isupper() for word in words if word.isalpha()):
            return cleaned
    return "Candidate"


def _extract_experience_years(text):
    candidates = []
    for match in re.finditer(r"(?:(\d+)\s*(?:\+)?\s*(?:years?|yrs?|yr))", text, flags=re.IGNORECASE):
        value = match.group(1)
        if value:
            candidates.append(float(value))
    for match in re.finditer(r"(?:(\d+)\s*(?:\+)?\s*(?:months?|mos?))", text, flags=re.IGNORECASE):
        value = match.group(1)
        if value:
            candidates.append(float(value) / 12.0)
    if not candidates:
        return 0.0
    return round(max(candidates), 2)


def parse_resume_pdf(file_path_or_buffer):
    """Extract structured metadata from a PDF resume."""
    raw_text = _extract_pdf_text(file_path_or_buffer)
    return {
        "name": _normalize_text(_extract_name(raw_text) or "Candidate"),
        "email": _extract_email(raw_text) or "",
        "phone": _extract_phone_number(raw_text) or "",
        "experience_years": _extract_experience_years(raw_text),
        "raw_text": raw_text,
    }


def _split_name(full_name):
    normalized = " ".join(str(full_name).split())
    parts = normalized.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def create_candidate_from_resume(file_obj, job_posting_id):
    """Parse a resume PDF, save a Candidate, and create an Application for the target job."""
    job_posting = get_object_or_404(m.JobPosting, pk=job_posting_id)
    if file_obj is None:
        raise ValueError("No resume file was provided.")

    if hasattr(file_obj, "read"):
        original_position = None
        if hasattr(file_obj, "tell"):
            try:
                original_position = file_obj.tell()
            except (AttributeError, OSError):
                original_position = None
        file_bytes = file_obj.read()
        if original_position is not None and hasattr(file_obj, "seek"):
            file_obj.seek(original_position)
        if isinstance(file_bytes, str):
            file_bytes = file_bytes.encode("utf-8")
    else:
        raise ValueError("Expected a file-like object with a .read() method.")

    if not file_bytes:
        raise ValueError("The uploaded resume is empty.")

    parsed = parse_resume_pdf(io.BytesIO(file_bytes))
    name = parsed.get("name") or "Candidate"
    email = parsed.get("email") or ""
    phone = parsed.get("phone") or ""
    experience_years = float(parsed.get("experience_years", 0) or 0)

    if not email:
        email = f"{slugify(name) or 'candidate'}-{uuid.uuid4().hex[:8]}@example.invalid"

    first_name, last_name = _split_name(name)
    default_stage = job_posting.pipeline_stages.select_related('stage').order_by('order').first()

    with transaction.atomic():
        candidate = m.Candidate.objects.create(
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            experience_years=Decimal(str(experience_years)),
        )

        safe_name = f"{slugify(name) or 'candidate'}-{uuid.uuid4().hex[:8]}.pdf"
        candidate.resume.save(safe_name, ContentFile(file_bytes), save=False)
        candidate.save(update_fields=['resume', 'updated_at'])

        application = m.Application.objects.create(
            candidate=candidate,
            job_posting=job_posting,
            status=m.Application.Status.APPLIED,
            current_stage=(default_stage.stage if default_stage else None),
            progress_percentage=0,
            source=m.Application.Source.CAREER_SITE,
        )

    return {
        "candidate_id": candidate.pk,
        "application_id": application.pk,
        "candidate_name": candidate.first_name + (" " + candidate.last_name if candidate.last_name else ""),
        "email": candidate.email,
        "phone": candidate.phone,
        "experience_years": experience_years,
    }


def process_late_arrival_penalty(record, reason=None):
    """
    Automated Late Arrival Penalty and Leave Deduction system.

    Triggered when an employee exceeds their monthly grace limit or arrives
    beyond the grace window and is marked as 'Half Day' (HD).

    Business Rules:
    1. Duplicate Prevention: Checks if a penalty already exists for (employee, penalty_date).
    2. Fallback Deduction Hierarchy (for Full-Time):
       - First Priority: Deduct 0.5 from CL (Casual Leave) if CL >= 0.5.
       - Second Priority: Deduct 0.5 from EL (Earned Leave) if EL >= 0.5.
       - Third Priority: If both CL and EL < 0.5, mark Deduction Source as 'LWP / Salary' (0.5 day wage deduction).
    3. Intern Handling:
       - Direct half-day stipend/salary calculation without deducting from standard paid leaves.
    4. Records entry in AttendancePenalty with status, source, and audit timestamps.
    """
    if not record or not record.employee:
        return None

    employee = record.employee
    penalty_date = record.attendance_date or timezone.localdate()

    # 1. Prevent duplicate penalty logs for the same employee and date
    existing_penalty = m.AttendancePenalty.objects.filter(
        employee=employee,
        penalty_date=penalty_date
    ).first()
    if existing_penalty:
        return existing_penalty

    late_mins = int(record.late_minutes or 0)
    deduction_amount = Decimal('0.5')
    employment_type = getattr(employee, 'employment_type', 'full_time') or 'full_time'

    penalty_reason = reason or f"Grace limit exceeded - Late Arrival ({late_mins} mins late)"

    with transaction.atomic():
        # Case A: INTERN EMPLOYEES
        if employment_type == 'intern':
            salary_deduction = Decimal('0.00')
            try:
                emp_salary = m.EmployeeSalary.objects.filter(
                    employee=employee, is_active=True
                ).first()
                if emp_salary and emp_salary.ctc_annual:
                    daily_wage = (emp_salary.ctc_annual / Decimal('12')) / Decimal('30')
                    salary_deduction = (daily_wage / Decimal('2')).quantize(Decimal('0.01'))
            except Exception as e:
                logger.warning(f"Failed calculating intern salary deduction for {employee}: {e}")

            penalty = m.AttendancePenalty.objects.create(
                employee=employee,
                attendance_record=record,
                penalty_date=penalty_date,
                reason=penalty_reason,
                late_minutes=late_mins,
                deduction_days=deduction_amount,
                deduction_source="LWP / Salary",
                status=m.AttendancePenalty.DeductionStatus.INTERN_SALARY,
                is_intern_penalty=True,
                salary_deduction_amount=salary_deduction,
                employment_type_snapshot=employment_type,
            )
            return penalty

        # Case B: FULL-TIME EMPLOYEES (CL -> EL -> LWP Hierarchy)
        live_balance, _ = m.EmployeeLeaveBalanceLive.objects.select_for_update().get_or_create(e_name=employee)
        bank_balance = m.EmployeeLeaveBalance.objects.select_for_update().filter(e_name=employee).first()

        cl_available = Decimal(str(live_balance.casual_leave or 0.0))
        el_available = Decimal(str(live_balance.earned_leave or 0.0))

        if cl_available >= deduction_amount:
            # First priority: Deduct 0.5 from CL
            new_cl = float(cl_available - deduction_amount)
            live_balance.casual_leave = new_cl
            live_balance.save(update_fields=['casual_leave'])

            if bank_balance:
                bank_balance.casual_leave = max(0.0, float(
                    Decimal(str(bank_balance.casual_leave or 0.0)) - deduction_amount))
                bank_balance.save(update_fields=['casual_leave'])

            deduction_source = "CL"
            penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED


        elif el_available >= deduction_amount:
            # Second priority: Deduct 0.5 from EL
            new_el = float(el_available - deduction_amount)
            live_balance.earned_leave = new_el
            live_balance.save(update_fields=['earned_leave'])

            if bank_balance:
                bank_balance.earned_leave = max(0.0, float(
                    Decimal(str(bank_balance.earned_leave or 0.0)) - deduction_amount))
                bank_balance.save(update_fields=['earned_leave'])

            deduction_source = "EL"
            penalty_status = m.AttendancePenalty.DeductionStatus.APPLIED


        else:
            # Third priority: Both exhausted -> Mark as LWP / Salary deduction
            deduction_source = "LWP / Salary"
            penalty_status = m.AttendancePenalty.DeductionStatus.LWP

        penalty = m.AttendancePenalty.objects.create(
            employee=employee,
            attendance_record=record,
            penalty_date=penalty_date,
            reason=penalty_reason,
            late_minutes=late_mins,
            deduction_days=deduction_amount,
            deduction_source=deduction_source,
            status=penalty_status,
            is_intern_penalty=False,
            employment_type_snapshot=employment_type,
        )
        return penalty
