from django import forms
from django.utils.translation import gettext_lazy as _

from pretix.base.models import Order, OrderPosition

from .models import ItemNumberConfig, NumberRange, BagNumber
from .services import sync_number


class ItemNumberConfigForm(forms.ModelForm):
    title = _("Bag Number")

    class Meta:
        model = ItemNumberConfig
        fields = ["number_range"]

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._event = event
        self._original_range_id = self.instance.number_range_id if self.instance.pk else None

        self.fields["number_range"].queryset = event.bagnumber_ranges.all()
        self.fields["number_range"].required = False
        self.fields["number_range"].label = _("Number range")
        self.fields["number_range"].help_text = _(
            "Leave empty if this product should not receive a bag number. "
            "Manage number ranges under Settings → Bag Numbers."
        )

        # Only offer reassignment when an existing range is already configured.
        if self.instance.pk and self._original_range_id:
            self.fields["reassign_existing"] = forms.BooleanField(
                label=_("Reassign bag numbers to new range"),
                required=False,
                help_text=_(
                    "Only relevant when you change the number range above. "
                    "Checked: all paid and pending orders for this product will have their "
                    "current bag numbers released and new numbers assigned from the newly "
                    "selected range. "
                    "Unchecked: existing orders keep their current numbers; only new orders "
                    "will receive numbers from the new range."
                ),
            )

    def save(self, commit=True):
        if self.cleaned_data.get("number_range") is None:
            if self.instance.pk:
                self.instance.delete()
            return None

        new_range = self.cleaned_data.get("number_range")
        range_changed = (
            self._original_range_id is not None
            and new_range is not None
            and new_range.pk != self._original_range_id
        )

        result = super().save(commit=commit)

        if commit and self.cleaned_data.get("reassign_existing") and range_changed:
            positions = OrderPosition.objects.filter(
                order__event=self._event,
                order__status__in=[Order.STATUS_PAID, Order.STATUS_PENDING],
                item=self.instance.item,
                canceled=False,
            )
            for pos in positions:
                sync_number(pos)

        return result


class NumberRangeForm(forms.ModelForm):
    class Meta:
        model = NumberRange
        fields = ["name", "start", "end"]

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._event = event

    def clean(self):
        data = super().clean()
        start = data.get("start")
        end = data.get("end")

        if end is not None and start is not None and end < start:
            raise forms.ValidationError(
                _("End must not be less than start.")
            )

        if start is not None and self._event is not None:
            existing = NumberRange.objects.filter(event=self._event)
            if self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            for rng in existing:
                overlaps = (rng.end is None or start <= rng.end) and \
                           (end is None or rng.start <= end)
                if overlaps:
                    raise forms.ValidationError(
                        _("This number range overlaps with '%(name)s' (%(start)s–%(end)s).")
                        % {
                            "name": rng.name,
                            "start": rng.start,
                            "end": rng.end if rng.end is not None else "∞",
                        }
                    )
        return data


class ImportUploadForm(forms.Form):
    file = forms.FileField(
        label=_("CSV file"),
        help_text=_(
            "Columns: Order Code, Position, Number. Comma or semicolon separated, "
            "UTF-8 or Windows-1252 encoded, with a header row."
        ),
    )

    def clean_file(self):
        f = self.cleaned_data["file"]
        raw = f.read()
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise forms.ValidationError(
            _("Could not read the file. Please save it as UTF-8 or Windows-1252 encoded CSV.")
        )


class BagNumberChangeForm(forms.ModelForm):
    class Meta:
        model = BagNumber
        fields = ["number"]

    def clean_number(self):
        n = self.cleaned_data["number"]
        clash = BagNumber.objects.filter(
            event=self.instance.event, number=n,
        ).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(
                _("Number %(n)s is already assigned (order %(o)s).")
                % {"n": n, "o": clash.first().position.order.code}
            )
        return n
