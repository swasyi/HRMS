"""
Dual email notifications for the hiring pipeline: candidate + interviewer/manager.

Uses Django's send_mail() with whatever EMAIL_BACKEND is configured (console
in dev, SES in your settings.py). Each send is isolated in a try/except so
one failed email (e.g. missing SMTP creds) never breaks the request — results
are returned so the view can surface them as messages instead of a 500.
"""
from django.conf import settings
from django.core.mail import send_mail


def _send(subject, body, to_email):
    if not to_email:
        return False, 'No recipient email on file.'
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=False)
        return True, None
    except Exception as e:  # SMTP/network errors shouldn't break the pipeline action
        return False, str(e)


def send_interview_emails(interview):
    """To the candidate (invite with date/time/mode) and to the interviewer
    (assignment notice). Returns a list of (role, email, success, error)."""
    application = interview.application
    candidate = application.candidate
    company = application.job_posting.company
    results = []

    when = interview.scheduled_on.strftime('%d %b %Y, %I:%M %p')
    subject_c = f'Interview Invitation — {application.job_posting.title} at {company.name}'
    body_c = (
        f'Dear {candidate.first_name},\n\n'
        f'You have been scheduled for an interview for the {application.job_posting.title} position '
        f'at {company.name}.\n\n'
        f'  Round:     {interview.interview_round}\n'
        f'  Date/Time: {when}\n'
        f'  Mode:      {interview.get_mode_display()}\n\n'
        f'Please join a few minutes early. If you need to reschedule, reply to this email.\n\n'
        f'Best regards,\n{company.name} Hiring Team'
    )
    ok, err = _send(subject_c, body_c, candidate.email)
    results.append(('candidate', candidate.email, ok, err))

    interviewer = interview.interviewer
    if interviewer and interviewer.email:
        subject_i = f'Interview Assignment — {candidate} ({application.job_posting.title})'
        body_i = (
            f'Hi {interviewer.first_name},\n\n'
            f'You have been assigned to interview {candidate} for the {application.job_posting.title} role.\n\n'
            f'  Round:     {interview.interview_round}\n'
            f'  Date/Time: {when}\n'
            f'  Mode:      {interview.get_mode_display()}\n\n'
            f'Candidate contact: {candidate.email} / {candidate.phone or "—"}\n'
            f'Experience: {candidate.experience_years} yrs — currently at {candidate.current_company or "—"}\n\n'
            f'Thanks,\nHR Team'
        )
        ok, err = _send(subject_i, body_i, interviewer.email)
        results.append(('interviewer', interviewer.email, ok, err))

    return results


def send_offer_emails(offer):
    """To the candidate (the offer) and to the hiring manager (last interviewer
    on the application, if any) as a heads-up that the offer went out."""
    application = offer.application
    candidate = application.candidate
    company = application.job_posting.company
    results = []

    subject_c = f'Offer Letter — {application.job_posting.title} at {company.name}'
    body_c = (
        f'Dear {candidate.first_name},\n\n'
        f'Congratulations! We are pleased to offer you the position of '
        f'{application.job_posting.title} at {company.name}.\n\n'
        f'  CTC Offered:    {offer.ctc_offered}\n'
        f'  Proposed Joining Date: {offer.joining_date or "To be confirmed"}\n'
        + (f'  Offer Valid Until: {offer.expiry_date}\n' if offer.expiry_date else '')
        + f'\nPlease reply to this email to confirm your acceptance.\n\n'
        f'Best regards,\n{company.name} Hiring Team'
    )
    ok, err = _send(subject_c, body_c, candidate.email)
    results.append(('candidate', candidate.email, ok, err))

    last_interview = application.interviews.order_by('-scheduled_on').first()
    manager = last_interview.interviewer if last_interview else None
    if manager and manager.email:
        subject_m = f'Offer Sent — {candidate} ({application.job_posting.title})'
        body_m = (
            f'Hi {manager.first_name},\n\n'
            f'An offer letter has been sent to {candidate} for the {application.job_posting.title} role '
            f'(CTC offered: {offer.ctc_offered}).\n\nThanks,\nHR Team'
        )
        ok, err = _send(subject_m, body_m, manager.email)
        results.append(('manager', manager.email, ok, err))

    return results
