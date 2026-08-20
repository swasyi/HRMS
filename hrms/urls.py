from django.urls import path

from . import views

app_name = 'hrms'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),

    path('set-company/', views.set_active_company, name='set_active_company'),
    # --- Organisation (HR/Admin) ---
    path('org/company/', views.CompanyListView.as_view(), name='company_list'),
    path('org/company/add/', views.CompanyCreateView.as_view(), name='company_add'),
    path('org/company/<int:pk>/edit/', views.CompanyUpdateView.as_view(), name='company_edit'),

    path('org/departments/', views.DepartmentListView.as_view(), name='department_list'),
    path('org/departments/add/', views.DepartmentCreateView.as_view(), name='department_add'),
    path('org/departments/<int:pk>/edit/', views.DepartmentUpdateView.as_view(), name='department_edit'),
    path('org/departments/<int:pk>/delete/', views.DepartmentDeleteView.as_view(), name='department_delete'),

    path('org/designations/', views.DesignationListView.as_view(), name='designation_list'),
    path('org/designations/add/', views.DesignationCreateView.as_view(), name='designation_add'),
    path('org/designations/<int:pk>/edit/', views.DesignationUpdateView.as_view(), name='designation_edit'),
    path('org/designations/<int:pk>/delete/', views.DesignationDeleteView.as_view(), name='designation_delete'),

    # --- Employee ---
    path('employees/', views.EmployeeListView.as_view(), name='employee_list'),
    path('employees/add/', views.EmployeeCreateView.as_view(), name='employee_add'),
    path('employees/<int:pk>/', views.EmployeeDetailView.as_view(), name='employee_detail'),
    path('employees/<int:pk>/edit/', views.EmployeeUpdateView.as_view(), name='employee_edit'),
    path('employees/<int:pk>/delete/', views.EmployeeDeleteView.as_view(), name='employee_delete'),
    path('employees/<int:pk>/bank-detail/', views.EmployeeBankDetailUpdateView.as_view(), name='employee_bankdetail'),
    path('employees/<int:pk>/documents/add/', views.EmployeeDocumentCreateView.as_view(), name='employee_document_add'),
    path('employees/documents/<int:pk>/delete/', views.EmployeeDocumentDeleteView.as_view(), name='employee_document_delete'),
    path('employees/<int:pk>/notices/add/', views.EmployeeNoticeCreateView.as_view(), name='employee_notice_add'),
    path('employees/notices/<int:pk>/delete/', views.EmployeeNoticeDeleteView.as_view(), name='employee_notice_delete'),

    path('me/profile/', views.MyProfileView.as_view(), name='my_profile'),
    path('me/documents/', views.MyDocumentsView.as_view(), name='my_documents'),

    # --- Hiring (HR/Admin) ---
    path('hiring/jobs/', views.JobPostingListView.as_view(), name='jobposting_list'),
    path('hiring/jobs/add/', views.JobPostingCreateView.as_view(), name='jobposting_add'),
    path('hiring/jobs/<int:pk>/edit/', views.JobPostingUpdateView.as_view(), name='jobposting_edit'),
    path('hiring/jobs/<int:pk>/delete/', views.JobPostingDeleteView.as_view(), name='jobposting_delete'),

    path('hiring/candidates/', views.CandidateListView.as_view(), name='candidate_list'),
    path('hiring/candidates/add/', views.CandidateCreateView.as_view(), name='candidate_add'),
    path('hiring/candidates/<int:pk>/', views.CandidateDetailView.as_view(), name='candidate_detail'),

    path('hiring/applications/', views.ApplicationListView.as_view(), name='application_list'),
    path('hiring/applications/add/', views.ApplicationCreateView.as_view(), name='application_add'),
    path('hiring/applications/<int:pk>/', views.ApplicationDetailView.as_view(), name='application_detail'),
    path('hiring/applications/<int:pk>/status/', views.ApplicationStatusView.as_view(), name='application_status'),
    path('hiring/applications/<int:pk>/unlock/', views.UnlockApplicationView.as_view(), name='application_unlock'),
    path('hiring/applications/<int:pk>/offer/', views.OfferLetterCreateView.as_view(), name='offer_create'),

    path('hiring/interviews/', views.InterviewListView.as_view(), name='interview_list'),
    path('hiring/interviews/mine/', views.MyInterviewsView.as_view(), name='my_interviews'),
    path('hiring/applications/<int:pk>/interviews/add/', views.InterviewCreateView.as_view(), name='interview_add'),
    path('hiring/interviews/<int:pk>/edit/', views.InterviewUpdateView.as_view(), name='interview_edit'),
    path('hiring/interviews/<int:pk>/feedback/', views.InterviewFeedbackView.as_view(), name='interview_feedback'),

    path('hiring/offers/<int:pk>/<str:action>/', views.OfferLetterActionView.as_view(), name='offer_action'),
    path('hiring/offers/<int:pk>/convert/', views.ConvertToEmployeeView.as_view(), name='offer_convert'),

    # Recruitment stages (reusable stage library)
    path('hiring/stages/', views.RecruitmentStageListView.as_view(), name='recruitmentstage_list'),
    path('hiring/stages/add/', views.RecruitmentStageCreateView.as_view(), name='recruitmentstage_add'),
    path('hiring/stages/<int:pk>/edit/', views.RecruitmentStageUpdateView.as_view(), name='recruitmentstage_edit'),
    path('hiring/stages/<int:pk>/delete/', views.RecruitmentStageDeleteView.as_view(), name='recruitmentstage_delete'),

    # Per-job pipeline management
    path('hiring/jobs/<int:pk>/pipeline/', views.JobPipelineManageView.as_view(), name='jobpipeline_manage'),

    # Offer templates
    path('hiring/offer-templates/', views.OfferTemplateListView.as_view(), name='offertemplate_list'),
    path('hiring/offer-templates/add/', views.OfferTemplateCreateView.as_view(), name='offertemplate_add'),
    path('hiring/offer-templates/<int:pk>/edit/', views.OfferTemplateUpdateView.as_view(), name='offertemplate_edit'),
    path('hiring/offer-templates/<int:pk>/delete/', views.OfferTemplateDeleteView.as_view(), name='offertemplate_delete'),
    path('hiring/offer-templates/<int:template_pk>/preview/<int:application_pk>/',
         views.OfferTemplatePreviewView.as_view(), name='offertemplate_preview'),

    # --- Attendance ---

    path('attendance/records/', views.AttendanceRecordListView.as_view(), name='attendance_records'),
    path('attendance/records/add/', views.AttendanceRecordCreateView.as_view(), name='attendance_record_add'),
    path('attendance/records/<int:pk>/edit/', views.AttendanceRecordUpdateView.as_view(), name='attendance_record_edit'),
    path('attendance/policy/', views.AttendancePolicyListView.as_view(), name='attendance_policy'),
    path('attendance/policy/<int:pk>/configure/', views.AttendancePolicyFormView.as_view(), name='attendance_policy_configure'),
    path('attendance/policy/add/', views.AttendancePolicyCreateView.as_view(), name='attendance_policy_create'),
    path('attendance/policy-version/<int:pk>/edit/', views.AttendancePolicyUpdateView.as_view(), name='attendance_policy_edit'),


    path('attendance/me/', views.MyAttendanceView.as_view(), name='my_attendance'),
    path('attendance/check-in/', views.CheckInView.as_view(), name='attendance_check_in'),
    path('attendance/check-out/', views.CheckOutView.as_view(), name='attendance_check_out'),

    path('attendance/holidays/', views.HolidayListView.as_view(), name='holiday_list'),
    path('attendance/holidays/add/', views.HolidayCreateView.as_view(), name='holiday_add'),
    path('attendance/holidays/<int:pk>/edit/', views.HolidayUpdateView.as_view(), name='holiday_edit'),
    path('attendance/holidays/<int:pk>/delete/', views.HolidayDeleteView.as_view(), name='holiday_delete'),

    path('attendance/report/<int:emp_id>/<int:month>/<int:year>/', views.EmployeePunchReportView.as_view(), name='employee_punch_report'),
    path('attendance/calendars/manage/', views.HolidayCalendarManageView.as_view(), name='holiday_calendar_manage'),
    # path('attendance/calendars/<int:calendar_id>/holidays/', views.CalendarHolidayListView.as_view(),
    #      name='calendar_holiday_list'),
    #
    # # This one is for adding a new date to that calendar
    # path('attendance/calendars/<int:calendar_id>/holidays/add/', views.CalendarHolidayCreateView.as_view(),
    #      name='calendar_holiday_add'),

    # --- Leave ---
    path('leave/', views.LeaveApplicationListView.as_view(), name='my_leave'),
    path('leave/apply/', views.LeaveApplicationCreateView.as_view(), name='leave_apply'),
    path('leave/<int:pk>/approve/', views.LeaveApproveView.as_view(), name='leave_approve'),
    path('leave/<int:pk>/reject/', views.LeaveRejectView.as_view(), name='leave_reject'),
    path('leave/<int:pk>/cancel/', views.LeaveCancelView.as_view(), name='leave_cancel'),

    path('leave/balance/', views.LeaveBalanceListView.as_view(), name='leave_balance'),
    path('leave/history/<int:pk>/', views.EmployeeLeaveHistoryView.as_view(), name='employee_leave_history'),

    path('leave/balance/add/', views.LeaveBalanceCreateView.as_view(), name='leave_balance_add'),
    path('leave/balance/<int:pk>/edit/', views.LeaveBalanceUpdateView.as_view(), name='leave_balance_edit'),

    path('leave/types/', views.LeaveTypeListView.as_view(), name='leavetype_list'),
    path('leave/types/add/', views.LeaveTypeCreateView.as_view(), name='leavetype_add'),
    path('leave/types/<int:pk>/edit/', views.LeaveTypeUpdateView.as_view(), name='leavetype_edit'),
    path('leave/types/<int:pk>/delete/', views.LeaveTypeDeleteView.as_view(), name='leavetype_delete'),
    path('leave-bank/', views.LeaveBankListView.as_view(), name='leave-bank'),

    # --- Payroll ---
    path('payroll/structures/', views.SalaryStructureListView.as_view(), name='salarystructure_list'),
    path('payroll/structures/add/', views.SalaryStructureCreateView.as_view(), name='salarystructure_add'),
    path('payroll/structures/<int:pk>/', views.SalaryStructureDetailView.as_view(), name='salarystructure_detail'),
    path('payroll/structures/<int:pk>/edit/', views.SalaryStructureUpdateView.as_view(), name='salarystructure_edit'),
    path('payroll/structures/<int:pk>/delete/', views.SalaryStructureDeleteView.as_view(), name='salarystructure_delete'),
    path('payroll/structures/<int:pk>/components/add/', views.SalaryComponentCreateView.as_view(), name='salarycomponent_add'),
    path('payroll/components/<int:pk>/delete/', views.SalaryComponentDeleteView.as_view(), name='salarycomponent_delete'),

    path('payroll/employee-salaries/', views.EmployeeSalaryListView.as_view(), name='employee_salary_list'),
    path('payroll/employee-salaries/add/', views.EmployeeSalaryCreateView.as_view(), name='employee_salary_add'),
    path('payroll/employee-salaries/<int:pk>/edit/', views.EmployeeSalaryUpdateView.as_view(), name='employee_salary_edit'),

    path('payroll/loans/', views.LoanAdvanceListView.as_view(), name='loan_list'),
    path('payroll/loans/add/', views.LoanAdvanceCreateView.as_view(), name='loan_add'),
    path('payroll/loans/<int:pk>/edit/', views.LoanAdvanceUpdateView.as_view(), name='loan_edit'),

    path('payroll/extras/', views.PayrollExtraListView.as_view(), name='extra_list'),
    path('payroll/extras/add/', views.PayrollExtraCreateView.as_view(), name='extra_add'),

    path('payroll/runs/', views.PayrollRunListView.as_view(), name='payrollrun_list'),
    path('payroll/runs/process/', views.PayrollProcessView.as_view(), name='payroll_process'),
    path('payroll/runs/<int:pk>/', views.PayrollRunDetailView.as_view(), name='payrollrun_detail'),

    path('payroll/payslips/', views.PaySlipListView.as_view(), name='my_payslips'),
    path('payroll/payslips/<int:pk>/', views.PaySlipDetailView.as_view(), name='payslip_detail'),
    path('payroll/payslips/<int:pk>/pdf/', views.PaySlipPDFView.as_view(), name='payslip_pdf'),

    # --- Policies & Notices ---
    path('policies/', views.coming_soon, {'active_group': 'policy', 'active_item': 'policy',
                                           'title': 'Policies'}, name='policy_list'),
    path('notices/', views.coming_soon, {'active_group': 'policy', 'active_item': 'notice',
                                          'title': 'Company Notices'}, name='notice_list'),

    # --- Assets ---
    path('assets/', views.AssetListView.as_view(), name='asset_list'),
    path('assets/add/', views.AssetCreateView.as_view(), name='asset_add'),
    path('ajax/create-category/', views.create_category_ajax, name='create_category_ajax'),
    path('assets/<int:pk>/edit/', views.AssetUpdateView.as_view(), name='asset_edit'),
    path('assets/<int:pk>/return/', views.AssetReturnView.as_view(), name='asset_return'),
    path('assets/<int:pk>/', views.AssetDetailView.as_view(), name='asset_detail'),
    # --- Performance ---
    path('performance/', views.PerformanceReviewListView.as_view(), name='performance_list'),
    path('performance/add/', views.PerformanceReviewCreateView.as_view(), name='performance_add'),
    path('performance/<int:pk>/', views.PerformanceReviewDetailView.as_view(), name='performance_detail'),
    path('performance/<int:pk>/edit/', views.PerformanceReviewUpdateView.as_view(), name='performance_edit'),
    path('performance/<int:pk>/acknowledge/', views.PerformanceAcknowledgeView.as_view(), name='performance_acknowledge'),
]