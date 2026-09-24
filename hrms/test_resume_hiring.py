from io import BytesIO
from unittest.mock import patch


from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse


from inventory.models import User
from hrms.models import Application, Company, Department, Designation, JobPipeline, JobPosting, RecruitmentStage




class HiringResumeAndStageTests(TestCase):
   def setUp(self):
       self.company = Company.objects.create(name='Acme HR')
       self.department = Department.objects.create(company=self.company, name='Engineering')
       self.designation = Designation.objects.create(company=self.company, department=self.department, title='Senior Engineer')


       self.hr_user = User.objects.create_user(username='hr1', password='testpass', is_accountant=True)
       self.super_admin = User.objects.create_user(username='root', password='testpass', is_superuser=True)


       self.stage_one = RecruitmentStage.objects.create(name='Applied')
       self.stage_two = RecruitmentStage.objects.create(name='Screening')
       self.stage_three = RecruitmentStage.objects.create(name='Interview')


       self.job = JobPosting.objects.create(
           company=self.company,
           department=self.department,
           designation=self.designation,
           title='Python Developer',
           description='Backend role',
           is_active=True,
       )
       JobPipeline.objects.create(job=self.job, stage=self.stage_one, order=1)
       JobPipeline.objects.create(job=self.job, stage=self.stage_two, order=2)
       JobPipeline.objects.create(job=self.job, stage=self.stage_three, order=3)


       self.candidate = self._make_candidate()
       self.application = Application.objects.create(
           candidate=self.candidate,
           job_posting=self.job,
           status=Application.Status.APPLIED,
           current_stage=self.stage_one,
       )


   def _make_candidate(self):
       from hrms.models import Candidate
       return Candidate.objects.create(
           first_name='Jane',
           last_name='Doe',
           email='jane@example.com',
           phone='+91 9876543210',
           experience_years=3,
       )


   @patch('hrms.services.parse_resume_pdf')
   def test_create_candidate_from_resume_uses_parser_and_creates_application(self, mock_parse):
       from hrms.services import create_candidate_from_resume


       mock_parse.return_value = {
           'name': 'Alice Johnson',
           'email': 'alice@example.com',
           'phone': '+1 555-123-4567',
           'experience_years': 4.5,
           'raw_text': 'Alice Johnson experience 4 years',
       }


       pdf = SimpleUploadedFile('resume.pdf', b'%PDF-1.4', content_type='application/pdf')
       result = create_candidate_from_resume(pdf, self.job.pk)


       self.assertEqual(result['candidate_name'], 'Alice Johnson')
       self.assertTrue(Application.objects.filter(candidate__email='alice@example.com').exists())
       self.assertEqual(Application.objects.filter(candidate__email='alice@example.com').first().current_stage, self.stage_one)


   def test_stage_move_rejects_locked_application_for_non_admin(self):
       self.application.is_locked = True
       self.application.save(update_fields=['is_locked'])


       self.client.force_login(self.hr_user)
       response = self.client.post(
           reverse('hrms:application_move_stage', args=[self.application.pk]),
           {'target_stage_id': self.stage_two.pk},
           content_type='application/json',
       )
       self.assertEqual(response.status_code, 400)


   def test_stage_move_allows_superadmin(self):
       self.client.force_login(self.super_admin)
       response = self.client.post(
           reverse('hrms:application_move_stage', args=[self.application.pk]),
           {'target_stage_id': self.stage_two.pk},
           content_type='application/json',
       )
       self.assertEqual(response.status_code, 200)
       self.application.refresh_from_db()
       self.assertEqual(self.application.current_stage_id, self.stage_two.pk)
       self.assertEqual(self.application.status, Application.Status.SHORTLISTED)
