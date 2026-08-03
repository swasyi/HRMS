"""
views.py additions for the Section 3 ATS upgrade.
Append these classes alongside your existing hiring views. Two existing
views need a one-line touch-up, noted below with UPDATE.
"""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from . import forms as f
from . import models as m
# assumes HRRequiredMixin / SidebarContextMixin already exist in this module,
# same as the rest of views.py


# --- NEW: RecruitmentStage CRUD --------------------------------------------
class RecruitmentStageListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.RecruitmentStage
    template_name = 'hrms/hiring/recruitmentstage_list.html'
    context_object_name = 'stages'
    active_group, active_item = 'hiring', 'recruitmentstage'


class RecruitmentStageCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.RecruitmentStage
    form_class = f.RecruitmentStageForm
    template_name = 'hrms/hiring/recruitmentstage_form.html'
    success_url = reverse_lazy('hrms:recruitmentstage_list')
    active_group, active_item = 'hiring', 'recruitmentstage'

    def form_valid(self, form):
        messages.success(self.request, 'Recruitment stage created.')
        return super().form_valid(form)


class RecruitmentStageUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.RecruitmentStage
    form_class = f.RecruitmentStageForm
    template_name = 'hrms/hiring/recruitmentstage_form.html'
    success_url = reverse_lazy('hrms:recruitmentstage_list')
    active_group, active_item = 'hiring', 'recruitmentstage'

    def form_valid(self, form):
        messages.success(self.request, 'Recruitment stage updated.')
        return super().form_valid(form)


class RecruitmentStageDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.RecruitmentStage
    template_name = 'hrms/hiring/recruitmentstage_confirm_delete.html'
    success_url = reverse_lazy('hrms:recruitmentstage_list')
    active_group, active_item = 'hiring', 'recruitmentstage'


# --- NEW: JobPipeline — manage a job's stage sequence on one screen -------
class JobPipelineManageView(HRRequiredMixin, SidebarContextMixin, View):
    """One page per JobPosting: an inline formset to add/reorder/remove the
    RecruitmentStages that make up that job's own hiring flow."""
    template_name = 'hrms/hiring/jobpipeline_manage.html'
    active_group, active_item = 'hiring', 'jobposting'

    def dispatch(self, request, *args, **kwargs):
        self.job = get_object_or_404(m.JobPosting, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        formset = f.JobPipelineFormSet(instance=self.job)
        return render(request, self.template_name, {
            'job': self.job, 'formset': formset,
            'active_group': self.active_group, 'active_item': self.active_item,
        })

    def post(self, request, pk):
        formset = f.JobPipelineFormSet(request.POST, instance=self.job)
        if formset.is_valid():
            formset.save()
            messages.success(request, 'Pipeline updated.')
            return redirect('hrms:jobpipeline_manage', pk=self.job.pk)
        return render(request, self.template_name, {
            'job': self.job, 'formset': formset,
            'active_group': self.active_group, 'active_item': self.active_item,
        })


# --- NEW: OfferTemplate CRUD ------------------------------------------------
class OfferTemplateListView(HRRequiredMixin, SidebarContextMixin, ListView):
    model = m.OfferTemplate
    template_name = 'hrms/hiring/offertemplate_list.html'
    context_object_name = 'templates'
    active_group, active_item = 'hiring', 'offertemplate'


class OfferTemplateCreateView(HRRequiredMixin, SidebarContextMixin, CreateView):
    model = m.OfferTemplate
    form_class = f.OfferTemplateForm
    template_name = 'hrms/hiring/offertemplate_form.html'
    success_url = reverse_lazy('hrms:offertemplate_list')
    active_group, active_item = 'hiring', 'offertemplate'

    def form_valid(self, form):
        messages.success(self.request, 'Offer template created.')
        return super().form_valid(form)


class OfferTemplateUpdateView(HRRequiredMixin, SidebarContextMixin, UpdateView):
    model = m.OfferTemplate
    form_class = f.OfferTemplateForm
    template_name = 'hrms/hiring/offertemplate_form.html'
    success_url = reverse_lazy('hrms:offertemplate_list')
    active_group, active_item = 'hiring', 'offertemplate'

    def form_valid(self, form):
        messages.success(self.request, 'Offer template updated.')
        return super().form_valid(form)


class OfferTemplateDeleteView(HRRequiredMixin, SidebarContextMixin, DeleteView):
    model = m.OfferTemplate
    template_name = 'hrms/hiring/offertemplate_confirm_delete.html'
    success_url = reverse_lazy('hrms:offertemplate_list')
    active_group, active_item = 'hiring', 'offertemplate'


class OfferTemplatePreviewView(HRRequiredMixin, View):
    """AJAX-ish endpoint: render the chosen template against an application
    so the OfferLetter form can pre-fill `content` before the user tweaks it."""
    def get(self, request, template_pk, application_pk):
        template = get_object_or_404(m.OfferTemplate, pk=template_pk)
        application = get_object_or_404(m.Application, pk=application_pk)
        ctc = request.GET.get('ctc_offered') or 0
        rendered = template.render(application, ctc_offered=ctc, offer_date=None)
        return render(request, 'hrms/hiring/_offer_preview_fragment.html', {'rendered': rendered})


# --- UPDATE: InterviewCreateView.post() / InterviewUpdateView -------------
# Pass application= into the form so pipeline_stage choices are scoped to
# that job's own pipeline:
#
#   form = f.InterviewForm(request.POST, application=self.application)
#
# and in InterviewUpdateView.get_form_kwargs():
#
#   def get_form_kwargs(self):
#       kwargs = super().get_form_kwargs()
#       kwargs['application'] = self.object.application if self.object else None
#       return kwargs

# --- UPDATE: OfferLetterCreateView.post() ----------------------------------
# hire.move_to_offer(...) should now also accept template/content, e.g.:
#
#   hire.move_to_offer(
#       self.application, request.user,
#       offer_date=form.cleaned_data['offer_date'],
#       ctc_offered=form.cleaned_data['ctc_offered'],
#       joining_date=form.cleaned_data.get('joining_date'),
#       expiry_date=form.cleaned_data.get('expiry_date'),
#       template=form.cleaned_data.get('template'),
#       content=form.cleaned_data.get('content'),
#   )
