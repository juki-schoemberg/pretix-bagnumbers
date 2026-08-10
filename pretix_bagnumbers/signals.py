from django.dispatch import receiver
from django.template.loader import get_template
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _

from pretix.api.signals import orderposition_api_details
from pretix.base.signals import (
    event_copy_data,
    layout_text_variables,
    order_canceled,
    order_changed,
    order_gracefully_delete,
    order_placed,
    order_reactivated,
    order_split,
)
from pretix.control.signals import item_forms, nav_event, order_info

from .forms import ItemNumberConfigForm
from .models import ItemNumberConfig, BagNumber
from .services import assign_number, release_number, sync_number

DUID = "pretix_bagnumbers"


# ---------------------------------------------------------------- Vergabe

@receiver(order_placed, dispatch_uid=f"{DUID}_order_placed")
def on_order_placed(sender, order, **kwargs):
    for pos in order.positions.all():
        assign_number(pos)


@receiver(order_canceled, dispatch_uid=f"{DUID}_order_canceled")
def on_order_canceled(sender, order, **kwargs):
    # Anforderung: Nummer wird bei Stornierung wieder freigegeben.
    for pos in order.all_positions.all():
        release_number(pos)


@receiver(order_reactivated, dispatch_uid=f"{DUID}_order_reactivated")
def on_order_reactivated(sender, order, **kwargs):
    # Anforderung: bei Reaktivierung wird eine NEUE Nummer vergeben.
    # Die alte wurde beim Storno freigegeben und kann inzwischen
    # anderweitig vergeben sein -- deshalb kein Restore, sondern
    # normale Neuvergabe (kleinste freie Nummer).
    for pos in order.positions.all():
        assign_number(pos)


@receiver(order_split, dispatch_uid=f"{DUID}_order_split")
def on_order_split(sender, original, split_order, **kwargs):
    # Beim Aufteilen einer Bestellung entstehen neue OrderPositions
    # ohne Nummer -- hier bekommen sie eine neue zugewiesen.
    for pos in split_order.positions.all():
        assign_number(pos)


@receiver(order_changed, dispatch_uid=f"{DUID}_order_changed")
def on_order_changed(sender, order, **kwargs):
    for pos in order.all_positions.all():
        if pos.canceled:
            release_number(pos)
        else:
            sync_number(pos)


@receiver(order_gracefully_delete, dispatch_uid=f"{DUID}_order_delete")
def on_order_gracefully_delete(sender, order, **kwargs):
    # Testmodus-Bestellungen: Nummern sofort freigeben, damit sie den
    # Nummernkreis nicht blockieren. Kein Log-Eintrag, die Bestellung
    # wird ohnehin geloescht.
    for pos in order.all_positions.all():
        release_number(pos, log=False)


# ------------------------------------------- Konfiguration am Produkt

@receiver(item_forms, dispatch_uid=f"{DUID}_item_forms")
def add_item_form(sender, request, item, **kwargs):
    """Rendert ein Zusatzformular direkt auf der Produkt-Detailseite."""
    try:
        instance = item.bagnumber_config
    except ItemNumberConfig.DoesNotExist:
        instance = ItemNumberConfig(item=item)
    return ItemNumberConfigForm(
        instance=instance,
        event=sender,
        data=(request.POST if request.method == "POST" else None),
        prefix="bagnumbers",
    )


# ------------------------------------------------------- Navigation

@receiver(nav_event, dispatch_uid=f"{DUID}_nav")
def add_nav_entry(sender, request, **kwargs):
    url = resolve(request.path_info)
    return [{
        "label": _("Bag Numbers"),
        "icon": "list-ol",
        "url": reverse(
            "plugins:pretix_bagnumbers:overview",
            kwargs={
                "event": request.event.slug,
                "organizer": request.organizer.slug,
            },
        ),
        "active": url.namespace == "plugins:pretix_bagnumbers",
    }]


# ---------------------------------------------------- Ticketdruck (PDF)

@receiver(layout_text_variables, dispatch_uid=f"{DUID}_layout_var")
def add_layout_variable(sender, **kwargs):
    def evaluate(op, order, event):
        try:
            return str(op.bagnumber.number)
        except BagNumber.DoesNotExist:
            return ""

    return {
        "bagnumber": {
            "label": _("Bag number"),
            "editor_sample": "123",
            "evaluate": evaluate,
        }
    }


# ------------------------------------------------------------ REST-API

@receiver(orderposition_api_details, dispatch_uid=f"{DUID}_api_details")
def api_details(sender, orderposition, **kwargs):
    """Taschennummer im plugin_details-Feld der OrderPosition-API."""
    try:
        return {"bag_number": orderposition.bagnumber.number}
    except BagNumber.DoesNotExist:
        return {"bag_number": None}


# ------------------------------------------------------- Order-Detailseite

@receiver(order_info, dispatch_uid=f"{DUID}_order_info")
def show_order_bag_numbers(sender, order, request, **kwargs):
    numbers = BagNumber.objects.filter(
        event=sender,
        position__order=order,
    ).select_related("position__item", "number_range")
    if not numbers.exists():
        return ""
    settings_url = reverse(
        "plugins:pretix_bagnumbers:overview",
        kwargs={
            "organizer": request.organizer.slug,
            "event": request.event.slug,
        },
    )
    template = get_template("pretix_bagnumbers/order_info.html")
    return template.render({
        "bag_numbers": numbers,
        "settings_url": settings_url,
        "request": request,
    }, request=request)


# ----------------------------------------------------- Event-Klonen

@receiver(event_copy_data, dispatch_uid=f"{DUID}_copy")
def copy_event_data(sender, other, item_map, **kwargs):
    """Nummernkreise + Produktzuordnungen mitkopieren, Nummern nicht."""
    range_map = {}
    for rng in other.bagnumber_ranges.all():
        old_pk = rng.pk
        rng.pk = None
        rng.event = sender
        rng.save()
        range_map[old_pk] = rng
    for cfg in ItemNumberConfig.objects.filter(
        item__event=other, number_range_id__in=range_map,
    ):
        new_item = item_map.get(cfg.item_id)
        if new_item:
            ItemNumberConfig.objects.create(
                item=new_item,
                number_range=range_map[cfg.number_range_id],
            )
