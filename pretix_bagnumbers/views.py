import codecs
import csv

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.db.models import Count
from django.views import View
from django.views.generic import FormView, TemplateView

from pretix.base.models import Item, Order, OrderPosition
from pretix.control.permissions import EventPermissionRequiredMixin

from .forms import ImportUploadForm, NumberRangeForm, BagNumberChangeForm
from .models import ItemNumberConfig, NumberRange, BagNumber
from .services import assign_number, apply_import, parse_import_csv, release_number


class OverviewView(EventPermissionRequiredMixin, TemplateView):
    template_name = "pretix_bagnumbers/overview.html"
    permission = "can_change_event_settings"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = self.request.event

        # Fetch all assigned numbers in one query, grouped by range
        numbers_by_range = {}
        for row in BagNumber.objects.filter(event=event).values("number_range_id", "number"):
            numbers_by_range.setdefault(row["number_range_id"], []).append(row["number"])

        ranges = list(
            event.bagnumber_ranges.annotate(numbers_count=Count("numbers"))
        )
        for rng in ranges:
            assigned = sorted(numbers_by_range.get(rng.pk, []))
            if assigned:
                max_n = assigned[-1]
                assigned_set = set(assigned)
                rng.gaps = [n for n in range(rng.start, max_n) if n not in assigned_set]
            else:
                rng.gaps = []
        ctx["ranges"] = ranges

        ctx["configs"] = ItemNumberConfig.objects.filter(
            item__event=event
        ).select_related("item", "number_range")

        numbers_qs = BagNumber.objects.filter(
            event=event
        ).select_related(
            "position__order", "position__item", "number_range"
        )
        range_param = self.request.GET.get("range", "all")
        selected_range = next((r for r in ranges if str(r.pk) == range_param), None)
        if selected_range:
            numbers_qs = numbers_qs.filter(number_range=selected_range)
            range_param = str(selected_range.pk)
        else:
            range_param = "all"
        ctx["numbers"] = numbers_qs
        ctx["selected_range"] = range_param

        ctx["unassigned_count"] = OrderPosition.objects.filter(
            order__event=event,
            order__status__in=[Order.STATUS_PAID, Order.STATUS_PENDING],
            canceled=False,
            item__bagnumber_config__isnull=False,
            bagnumber__isnull=True,
        ).count()

        return ctx


class ExportView(EventPermissionRequiredMixin, View):
    """
    Direct CSV download of assigned bag numbers -- Order Code, Position,
    Number, Participant Name -- in the same shape `parse_import_csv()`
    expects, so "export, reassign, re-import" works as a round trip.
    Respects the `range` query parameter from the overview's filter
    (a NumberRange pk, or "all"/absent for everything).
    """
    permission = "can_view_orders"

    def get(self, request, *args, **kwargs):
        event = request.event
        qs = BagNumber.objects.filter(event=event).select_related(
            "position__order", "position__item", "number_range"
        )

        range_param = request.GET.get("range")
        filename_suffix = "alle"
        if range_param and range_param != "all":
            rng = get_object_or_404(NumberRange, pk=range_param, event=event)
            qs = qs.filter(number_range=rng)
            filename_suffix = slugify(rng.name) or "bereich"

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="bagnumbers_{event.slug}_{filename_suffix}.csv"'
        )
        response.write(codecs.BOM_UTF8.decode("utf-8"))  # so Excel opens umlauts correctly
        writer = csv.writer(response)
        writer.writerow(["Order Code", "Position", "Number", "Participant Name"])
        for bn in qs:
            writer.writerow([
                bn.position.order.code,
                bn.position.positionid,
                bn.number,
                bn.position.attendee_name or "",
            ])
        return response


class RangeCreateUpdateView(EventPermissionRequiredMixin, FormView):
    template_name = "pretix_bagnumbers/range_form.html"
    permission = "can_change_event_settings"
    form_class = NumberRangeForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        pk = self.kwargs.get("pk")
        if pk:
            kwargs["instance"] = get_object_or_404(
                NumberRange, pk=pk, event=self.request.event
            )
        kwargs["event"] = self.request.event
        return kwargs

    def form_valid(self, form):
        form.instance.event = self.request.event
        form.save()
        messages.success(self.request, _("Number range saved."))
        return redirect(self._overview_url())

    def _overview_url(self):
        return reverse(
            "plugins:pretix_bagnumbers:overview",
            kwargs={
                "organizer": self.request.organizer.slug,
                "event": self.request.event.slug,
            },
        )


class RangeDeleteView(EventPermissionRequiredMixin, View):
    permission = "can_change_event_settings"

    def post(self, request, *args, **kwargs):
        rng = get_object_or_404(NumberRange, pk=kwargs["pk"], event=request.event)
        overview_url = reverse(
            "plugins:pretix_bagnumbers:overview",
            kwargs={"organizer": request.organizer.slug, "event": request.event.slug},
        )

        if rng.numbers.exists() and request.POST.get("force") != "1":
            return redirect(reverse(
                "plugins:pretix_bagnumbers:range.delete.confirm",
                kwargs={"organizer": request.organizer.slug, "event": request.event.slug, "pk": rng.pk},
            ))

        # Release all bag numbers with proper logging before deleting the range
        for bn in BagNumber.objects.filter(number_range=rng).select_related("position"):
            release_number(bn.position)

        rng.delete()  # cascades to ItemNumberConfig
        messages.success(request, _("Number range deleted."))
        return redirect(overview_url)


class RangeDeleteConfirmView(EventPermissionRequiredMixin, TemplateView):
    template_name = "pretix_bagnumbers/range_delete_confirm.html"
    permission = "can_change_event_settings"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        rng = get_object_or_404(NumberRange, pk=self.kwargs["pk"], event=self.request.event)
        ctx["range"] = rng
        ctx["number_count"] = rng.numbers.count()
        ctx["affected_configs"] = ItemNumberConfig.objects.filter(
            number_range=rng
        ).select_related("item")
        return ctx


class BulkAssignView(EventPermissionRequiredMixin, View):
    permission = "can_change_orders"

    def post(self, request, *args, **kwargs):
        item = get_object_or_404(Item, pk=kwargs["item_pk"], event=request.event)
        overview_url = reverse(
            "plugins:pretix_bagnumbers:overview",
            kwargs={"organizer": request.organizer.slug, "event": request.event.slug},
        )

        try:
            item.bagnumber_config
        except ItemNumberConfig.DoesNotExist:
            messages.error(request, _("No number range configured for this product."))
            return redirect(overview_url)

        positions = OrderPosition.objects.filter(
            order__event=request.event,
            order__status__in=[Order.STATUS_PAID, Order.STATUS_PENDING],
            item=item,
            canceled=False,
            bagnumber__isnull=True,
        )

        assigned = 0
        range_full = False
        for pos in positions:
            try:
                if assign_number(pos):
                    assigned += 1
            except ValidationError:
                range_full = True
                break

        if assigned:
            messages.success(request, _("%(count)s numbers assigned.") % {"count": assigned})
        if range_full:
            messages.warning(request, _(
                "The number range is full. Not all positions could be assigned a number."
            ))
        if not assigned and not range_full:
            messages.info(request, _("No orders found that still need a number."))

        return redirect(overview_url)


class NumberChangeView(EventPermissionRequiredMixin, FormView):
    template_name = "pretix_bagnumbers/number_form.html"
    permission = "can_change_orders"
    form_class = BagNumberChangeForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = get_object_or_404(
            BagNumber, pk=self.kwargs["pk"], event=self.request.event
        )
        return kwargs

    def form_valid(self, form):
        old_number = form.initial["number"]
        form.save()
        form.instance.position.order.log_action(
            "pretix_bagnumbers.number.changed",
            user=self.request.user,
            data={
                "old_number": old_number,
                "number": form.instance.number,
                "positionid": form.instance.position.positionid,
            },
        )
        messages.success(self.request, _("Number changed."))
        return redirect(reverse(
            "plugins:pretix_bagnumbers:overview",
            kwargs={
                "organizer": self.request.organizer.slug,
                "event": self.request.event.slug,
            },
        ))


class ImportView(EventPermissionRequiredMixin, FormView):
    """
    Two-step CSV import: uploading the file shows a preview (sample of
    clean rows + full list of errors) without writing anything; a second,
    explicit POST commits it. The decoded CSV text is kept in the session
    between the two steps and re-validated on confirm, so the write always
    reflects the current database state.
    """
    permission = "can_change_orders"
    template_name = "pretix_bagnumbers/import_form.html"
    form_class = ImportUploadForm

    SESSION_KEY = "pretix_bagnumbers_import"
    PREVIEW_LIMIT = 50
    ERROR_DISPLAY_LIMIT = 200

    def _overview_url(self):
        return reverse(
            "plugins:pretix_bagnumbers:overview",
            kwargs={"organizer": self.request.organizer.slug, "event": self.request.event.slug},
        )

    def _import_url(self):
        return reverse(
            "plugins:pretix_bagnumbers:import",
            kwargs={"organizer": self.request.organizer.slug, "event": self.request.event.slug},
        )

    def post(self, request, *args, **kwargs):
        if request.POST.get("confirm") == "1":
            return self._confirm(request)
        if request.POST.get("cancel") == "1":
            request.session.pop(self.SESSION_KEY, None)
            return redirect(self._import_url())
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        text = form.cleaned_data["file"]
        filename = self.request.FILES["file"].name
        result = parse_import_csv(text, self.request.event)

        if result.global_error:
            messages.error(self.request, result.global_error)
            return self.render_to_response(self.get_context_data(form=form))

        if not result.clean and not result.errors:
            messages.info(self.request, _("The file does not contain any data rows."))
            return self.render_to_response(self.get_context_data(form=form))

        self.request.session[self.SESSION_KEY] = {"text": text, "filename": filename}
        ctx = self.get_context_data(
            form=form,
            preview=True,
            filename=filename,
            preview_rows=result.clean[:self.PREVIEW_LIMIT],
            preview_total=len(result.clean),
            error_rows=result.errors[:self.ERROR_DISPLAY_LIMIT],
            error_total=len(result.errors),
        )
        return self.render_to_response(ctx)

    def _confirm(self, request):
        data = request.session.get(self.SESSION_KEY)
        if not data:
            messages.error(request, _("Your import session has expired. Please upload the file again."))
            return redirect(self._import_url())

        result = parse_import_csv(data["text"], request.event)
        try:
            applied = apply_import(
                result.clean, request.event, user=request.user, filename=data["filename"],
            )
        except IntegrityError:
            messages.error(request, _(
                "The import could not be completed because a conflicting change happened at the "
                "same time. Please review the preview and try again."
            ))
            return redirect(self._import_url())

        request.session.pop(self.SESSION_KEY, None)

        if applied:
            messages.success(request, _("%(count)s bag numbers were imported.") % {"count": applied})
        if result.errors:
            messages.warning(request, _(
                "%(count)s row(s) were skipped due to errors."
            ) % {"count": len(result.errors)})
        if not applied and not result.errors:
            messages.info(request, _("Nothing to import."))

        return redirect(self._overview_url())
