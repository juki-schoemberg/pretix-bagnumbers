import pytest

from pretix_bagnumbers.models import BagNumber, ItemNumberConfig, NumberRange
from pretix_bagnumbers.services import apply_import, parse_import_csv

from .conftest import make_order


@pytest.fixture
def number_range(event):
    return NumberRange.objects.create(event=event, name="Kinder", start=100, end=199)


@pytest.fixture
def item(event):
    from pretix.base.models import Item
    return Item.objects.create(event=event, name="Kinderticket", default_price=0)


@pytest.fixture
def item_with_range(item, number_range):
    ItemNumberConfig.objects.create(item=item, number_range=number_range)
    return item


@pytest.fixture
def order(event, item_with_range):
    return make_order(event, item_with_range, "TEST1")


@pytest.mark.django_db
def test_parse_erfolg(event, order):
    csv_text = "Order Code,Position,Number\nTEST1,1,150\n"
    result = parse_import_csv(csv_text, event)
    assert result.global_error is None
    assert not result.errors
    assert len(result.clean) == 1
    row = result.clean[0]
    assert row.order_code == "TEST1"
    assert row.positionid == 1
    assert row.number == 150
    assert row.old_number is None


@pytest.mark.django_db
def test_parse_ueberzaehlige_spalten_werden_ignoriert(event, order):
    csv_text = "Order Code,Position,Number,Participant Name\nTEST1,1,150,Anna Bauer\n"
    result = parse_import_csv(csv_text, event)
    assert result.global_error is None
    assert not result.errors
    assert len(result.clean) == 1
    assert result.clean[0].number == 150


@pytest.mark.django_db
def test_parse_semikolon_wird_erkannt(event, order):
    csv_text = "Order Code;Position;Number\nTEST1;1;150\n"
    result = parse_import_csv(csv_text, event)
    assert result.global_error is None
    assert len(result.clean) == 1


@pytest.mark.django_db
def test_parse_fehlende_spalte(event):
    csv_text = "Order Code,Position\nTEST1,1\n"
    result = parse_import_csv(csv_text, event)
    assert result.global_error is not None
    assert "number" in str(result.global_error)


@pytest.mark.django_db
def test_parse_leere_datei(event):
    result = parse_import_csv("", event)
    assert result.global_error is not None


@pytest.mark.django_db
def test_parse_bestellung_nicht_gefunden(event):
    csv_text = "Order Code,Position,Number\nUNKNOWN,1,150\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 1
    assert "not found" in result.errors[0].message


@pytest.mark.django_db
def test_parse_position_nicht_gefunden(event, order):
    csv_text = "Order Code,Position,Number\nTEST1,99,150\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 1
    assert "not found" in result.errors[0].message


@pytest.mark.django_db
def test_parse_falscher_bestellstatus(event, order):
    from pretix.base.models import Order
    order.status = Order.STATUS_CANCELED
    order.save()

    csv_text = "Order Code,Position,Number\nTEST1,1,150\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 1
    assert "status" in result.errors[0].message


@pytest.mark.django_db
def test_parse_kein_nummernkreis_konfiguriert(event, item):
    order = make_order(event, item, "NOCFG")
    csv_text = "Order Code,Position,Number\nNOCFG,1,150\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 1
    assert "number range" in result.errors[0].message


@pytest.mark.django_db
def test_parse_nummer_ausserhalb_bereich(event, order):
    csv_text = "Order Code,Position,Number\nTEST1,1,50\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 1
    assert "outside of number range" in result.errors[0].message


@pytest.mark.django_db
def test_parse_duplikat_position_in_datei(event, order):
    csv_text = "Order Code,Position,Number\nTEST1,1,150\nTEST1,1,160\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 2
    assert all("multiple times" in e.message for e in result.errors)


@pytest.mark.django_db
def test_parse_duplikat_nummer_in_datei(event, order, item_with_range):
    order2 = make_order(event, item_with_range, "TEST2")
    csv_text = "Order Code,Position,Number\nTEST1,1,150\nTEST2,1,150\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 2


@pytest.mark.django_db
def test_parse_konflikt_mit_nicht_importierter_position(event, order, item_with_range, number_range):
    order2 = make_order(event, item_with_range, "TEST2")
    pos2 = order2.positions.first()
    BagNumber.objects.create(event=event, position=pos2, number_range=number_range, number=105)

    csv_text = "Order Code,Position,Number\nTEST1,1,105\n"
    result = parse_import_csv(csv_text, event)
    assert not result.clean
    assert len(result.errors) == 1
    assert "already assigned" in result.errors[0].message


@pytest.mark.django_db
def test_parse_tausch_zwischen_zwei_positionen_erlaubt(event, order, item_with_range, number_range):
    order2 = make_order(event, item_with_range, "TEST2")
    pos1 = order.positions.first()
    pos2 = order2.positions.first()
    BagNumber.objects.create(event=event, position=pos1, number_range=number_range, number=100)
    BagNumber.objects.create(event=event, position=pos2, number_range=number_range, number=101)

    csv_text = "Order Code,Position,Number\nTEST1,1,101\nTEST2,1,100\n"
    result = parse_import_csv(csv_text, event)
    assert not result.errors
    assert len(result.clean) == 2
    by_order = {row.order_code: row for row in result.clean}
    assert by_order["TEST1"].number == 101
    assert by_order["TEST1"].old_number == 100
    assert by_order["TEST2"].number == 100
    assert by_order["TEST2"].old_number == 101


@pytest.mark.django_db
def test_apply_import_schreibt_und_loggt(event, order):
    from pretix.base.models import LogEntry

    csv_text = "Order Code,Position,Number\nTEST1,1,150\n"
    result = parse_import_csv(csv_text, event)
    applied = apply_import(result.clean, event, user=None, filename="test.csv")

    assert applied == 1
    pos = order.positions.first()
    assert pos.bagnumber.number == 150
    assert LogEntry.objects.filter(action_type="pretix_bagnumbers.number.assigned").count() == 1
    assert LogEntry.objects.filter(action_type="pretix_bagnumbers.import.completed").count() == 1


@pytest.mark.django_db
def test_apply_import_ersetzt_bestehende_nummer_und_loggt_changed(event, order, number_range):
    pos = order.positions.first()
    BagNumber.objects.create(event=event, position=pos, number_range=number_range, number=100)

    csv_text = "Order Code,Position,Number\nTEST1,1,150\n"
    result = parse_import_csv(csv_text, event)
    apply_import(result.clean, event, user=None, filename="test.csv")

    pos.refresh_from_db()
    assert pos.bagnumber.number == 150
    from pretix.base.models import LogEntry
    assert LogEntry.objects.filter(action_type="pretix_bagnumbers.number.changed").count() == 1


@pytest.mark.django_db
def test_apply_import_leer_loggt_trotzdem_zusammenfassung(event):
    from pretix.base.models import LogEntry
    applied = apply_import([], event, user=None, filename="empty.csv")
    assert applied == 0
    assert LogEntry.objects.filter(action_type="pretix_bagnumbers.import.completed").count() == 1


@pytest.mark.django_db
def test_apply_import_unveraenderte_zeile_wird_uebersprungen(event, order, number_range):
    from pretix.base.models import LogEntry

    pos = order.positions.first()
    bn = BagNumber.objects.create(event=event, position=pos, number_range=number_range, number=150)

    csv_text = "Order Code,Position,Number\nTEST1,1,150\n"
    result = parse_import_csv(csv_text, event)
    assert result.clean[0].old_number == 150

    applied = apply_import(result.clean, event, user=None, filename="test.csv")

    assert applied == 0
    pos.refresh_from_db()
    assert pos.bagnumber.pk == bn.pk  # untouched, not deleted/recreated
    assert pos.bagnumber.number == 150
    assert not LogEntry.objects.filter(action_type="pretix_bagnumbers.number.changed").exists()
    assert not LogEntry.objects.filter(action_type="pretix_bagnumbers.number.assigned").exists()
    summary = LogEntry.objects.get(action_type="pretix_bagnumbers.import.completed")
    assert summary.parsed_data["applied"] == 0


@pytest.mark.django_db
def test_apply_import_mischung_aus_unveraendert_und_geaendert(event, order, item_with_range, number_range):
    order2 = make_order(event, item_with_range, "TEST2")
    pos1 = order.positions.first()
    pos2 = order2.positions.first()
    BagNumber.objects.create(event=event, position=pos1, number_range=number_range, number=150)
    BagNumber.objects.create(event=event, position=pos2, number_range=number_range, number=160)

    csv_text = "Order Code,Position,Number\nTEST1,1,150\nTEST2,1,170\n"
    result = parse_import_csv(csv_text, event)
    applied = apply_import(result.clean, event, user=None, filename="test.csv")

    assert applied == 1
    pos1.refresh_from_db()
    pos2.refresh_from_db()
    assert pos1.bagnumber.number == 150
    assert pos2.bagnumber.number == 170
