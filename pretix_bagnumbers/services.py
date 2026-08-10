import csv
import io
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from .models import BagNumber, ItemNumberConfig, NumberRange


def assign_number(position):
    """
    Assigns the smallest free number >= range.start for an OrderPosition.
    Returns None if the product has no number range or the position already
    has a number. SELECT FOR UPDATE protects against race conditions.
    """
    try:
        config = position.item.bagnumber_config
    except ItemNumberConfig.DoesNotExist:
        return None

    if BagNumber.objects.filter(position=position).exists():
        return position.bagnumber

    event = position.order.event

    with transaction.atomic():
        rng = NumberRange.objects.select_for_update().get(
            pk=config.number_range_id
        )
        used = set(
            BagNumber.objects
            .filter(event=event, number__gte=rng.start)
            .values_list("number", flat=True)
        )
        n = rng.start
        while n in used:
            n += 1
        if rng.end is not None and n > rng.end:
            raise ValidationError(
                f"Number range '{rng.name}' is full "
                f"({rng.start}–{rng.end})."
            )
        tn = BagNumber.objects.create(
            event=event, position=position, number_range=rng, number=n,
        )
        position.order.log_action(
            "pretix_bagnumbers.number.assigned",
            data={"number": n, "positionid": position.positionid},
        )
        return tn


def release_number(position, log=True):
    """Releases a bag number (e.g. on cancellation)."""
    tn = BagNumber.objects.filter(position=position).first()
    if tn is None:
        return
    number = tn.number
    tn.delete()
    if log:
        position.order.log_action(
            "pretix_bagnumbers.number.released",
            data={"number": number, "positionid": position.positionid},
        )


def sync_number(position):
    """
    Ensures the position has the correct bag number for its current product.

    - Product has no config → release any existing number.
    - Product has a config, position has no number → assign one.
    - Product has a config, position has a number from the WRONG range
      (e.g. after a product change) → release old, assign new.
    - Product has a config, position has a number from the correct range → no-op.
    """
    try:
        expected_range = position.item.bagnumber_config.number_range
    except ItemNumberConfig.DoesNotExist:
        release_number(position)
        return

    existing = BagNumber.objects.filter(position=position).first()
    if existing is None:
        assign_number(position)
    elif existing.number_range_id != expected_range.pk:
        release_number(position)
        assign_number(position)
    # else: number is already in the correct range — no-op


# --- CSV import -------------------------------------------------------

# Header aliases are matched case-insensitively against the stripped cell
# value. Extra/unknown columns in the file are ignored.
_COLUMN_ALIASES = {
    "order_code": {"order code", "order", "bestellcode", "bestellung", "bestellnummer"},
    "position": {"position", "pos", "positionid", "position id", "positions-id"},
    "number": {"number", "bag number", "nummer", "taschennummer"},
}


@dataclass
class ImportRow:
    line_no: int
    order_code: str
    positionid: int
    number: int
    position: object
    number_range: object
    old_number: Optional[int] = None


@dataclass
class ImportRowError:
    line_no: int
    raw: dict
    message: str


@dataclass
class ImportResult:
    clean: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    # Set when the file itself is unusable (missing columns, empty file).
    # In that case `clean`/`errors` are always empty.
    global_error: Optional[str] = None


def _detect_delimiter(header_line):
    semicolons = header_line.count(";")
    commas = header_line.count(",")
    return ";" if semicolons >= commas and semicolons > 0 else ","


def _map_columns(header_row):
    mapping = {}
    for idx, cell in enumerate(header_row):
        key = cell.strip().lower()
        for canonical, aliases in _COLUMN_ALIASES.items():
            if key in aliases:
                mapping[canonical] = idx
    missing = [c for c in _COLUMN_ALIASES if c not in mapping]
    return mapping, missing


def parse_import_csv(text, event):
    """
    Parses and fully validates a bag-number import CSV against the current
    database state. Pure/read-only — does not write anything.

    Returns an ImportResult. If `global_error` is set, the file could not be
    read at all (wrong/missing columns, empty file). Otherwise `clean`
    contains rows ready to be applied via `apply_import()` and `errors`
    contains everything that was rejected, with a reason per row.
    """
    stripped = text.strip("﻿ \r\n")
    if not stripped:
        return ImportResult(global_error=_("The file is empty."))

    delimiter = _detect_delimiter(stripped.splitlines()[0])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)

    try:
        header = next(reader)
    except StopIteration:
        return ImportResult(global_error=_("The file is empty."))

    mapping, missing = _map_columns(header)
    if missing:
        return ImportResult(global_error=_(
            "The file is missing the following column(s): %(cols)s"
        ) % {"cols": ", ".join(missing)})

    def cell(row, name):
        idx = mapping[name]
        return row[idx].strip() if idx < len(row) else ""

    raw_rows = []
    parse_errors = []
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not c.strip() for c in row):
            continue

        order_code_raw = cell(row, "order_code")
        positionid_raw = cell(row, "position")
        number_raw = cell(row, "number")
        raw = {"order_code": order_code_raw, "position": positionid_raw, "number": number_raw}

        if not order_code_raw or not positionid_raw or not number_raw:
            parse_errors.append(ImportRowError(line_no, raw, _("Missing value(s).")))
            continue
        try:
            positionid = int(positionid_raw)
        except ValueError:
            parse_errors.append(ImportRowError(line_no, raw, _("'%(v)s' is not a whole number.") % {"v": positionid_raw}))
            continue
        try:
            number = int(number_raw)
        except ValueError:
            parse_errors.append(ImportRowError(line_no, raw, _("'%(v)s' is not a whole number.") % {"v": number_raw}))
            continue

        raw_rows.append({
            "line_no": line_no, "order_code": order_code_raw.upper(),
            "positionid": positionid, "number": number, "raw": raw,
        })

    pos_key_counts = Counter((r["order_code"], r["positionid"]) for r in raw_rows)
    number_counts = Counter(r["number"] for r in raw_rows)

    candidates = []
    dup_errors = []
    for r in raw_rows:
        pos_key = (r["order_code"], r["positionid"])
        if pos_key_counts[pos_key] > 1:
            dup_errors.append(ImportRowError(r["line_no"], r["raw"], _(
                "Position %(code)s-%(pos)s appears multiple times in the file."
            ) % {"code": r["order_code"], "pos": r["positionid"]}))
        elif number_counts[r["number"]] > 1:
            dup_errors.append(ImportRowError(r["line_no"], r["raw"], _(
                "Number %(n)s appears multiple times in the file."
            ) % {"n": r["number"]}))
        else:
            candidates.append(r)

    from pretix.base.models import Order

    codes = {r["order_code"] for r in candidates}
    orders_by_code = {
        o.code: o for o in Order.objects.filter(event=event, code__in=codes)
    }

    business_errors = []
    valid_rows = []
    target_position_ids = set()

    for r in candidates:
        order = orders_by_code.get(r["order_code"])
        if order is None:
            business_errors.append(ImportRowError(r["line_no"], r["raw"], _(
                "Order %(code)s not found."
            ) % {"code": r["order_code"]}))
            continue
        if order.status not in (Order.STATUS_PAID, Order.STATUS_PENDING):
            business_errors.append(ImportRowError(r["line_no"], r["raw"], _(
                "Order %(code)s has a status that cannot receive bag numbers."
            ) % {"code": order.code}))
            continue

        position = order.positions.filter(positionid=r["positionid"], canceled=False).first()
        if position is None:
            business_errors.append(ImportRowError(r["line_no"], r["raw"], _(
                "Position %(code)s-%(pos)s not found."
            ) % {"code": order.code, "pos": r["positionid"]}))
            continue

        try:
            config = position.item.bagnumber_config
        except ItemNumberConfig.DoesNotExist:
            business_errors.append(ImportRowError(r["line_no"], r["raw"], _(
                "Product '%(item)s' has no number range configured."
            ) % {"item": position.item.name}))
            continue

        rng = config.number_range
        if r["number"] < rng.start or (rng.end is not None and r["number"] > rng.end):
            business_errors.append(ImportRowError(r["line_no"], r["raw"], _(
                "Number %(n)s is outside of number range '%(name)s' (%(start)s–%(end)s)."
            ) % {
                "n": r["number"], "name": rng.name, "start": rng.start,
                "end": rng.end if rng.end is not None else "∞",
            }))
            continue

        valid_rows.append(ImportRow(
            line_no=r["line_no"], order_code=order.code, positionid=r["positionid"],
            number=r["number"], position=position, number_range=rng,
        ))
        target_position_ids.add(position.pk)

    numbers_wanted = {row.number for row in valid_rows}
    conflicts = {
        c.number: c
        for c in BagNumber.objects.filter(event=event, number__in=numbers_wanted)
        .exclude(position_id__in=target_position_ids)
        .select_related("position__order")
    }

    final_rows = []
    conflict_errors = []
    for row in valid_rows:
        conflict = conflicts.get(row.number)
        if conflict is not None:
            conflict_errors.append(ImportRowError(row.line_no, {
                "order_code": row.order_code, "position": row.positionid, "number": row.number,
            }, _(
                "Number %(n)s is already assigned to order %(code)s (not part of this import)."
            ) % {"n": row.number, "code": conflict.position.order.code}))
            continue
        try:
            row.old_number = row.position.bagnumber.number
        except BagNumber.DoesNotExist:
            row.old_number = None
        final_rows.append(row)

    all_errors = parse_errors + dup_errors + business_errors + conflict_errors
    all_errors.sort(key=lambda e: e.line_no)

    return ImportResult(clean=final_rows, errors=all_errors)


def apply_import(clean_rows, event, user=None, filename=""):
    """
    Writes previously validated import rows to the database in one
    transaction: releases the current numbers of all target positions first,
    then assigns the new ones. This makes swap scenarios (two positions
    trading numbers) work without transient unique-constraint conflicts.

    Rows whose number is already exactly what's in the database (i.e.
    `old_number == number`) are true no-ops and are skipped entirely --
    no delete/create, no log entry -- so a re-import of an unmodified
    export doesn't churn the database or the order history.

    `clean_rows` should come from a fresh `parse_import_csv()` call made
    right before this, so the validation reflects the current database
    state as closely as possible.
    """
    changed_rows = [row for row in clean_rows if row.old_number != row.number]

    applied = 0
    if changed_rows:
        range_ids = {row.number_range.pk for row in changed_rows}
        position_ids = [row.position.pk for row in changed_rows]

        with transaction.atomic():
            # Lock ranges to serialize with concurrent automatic assignment
            # (order_placed etc.), same as assign_number().
            list(NumberRange.objects.select_for_update().filter(pk__in=range_ids))

            BagNumber.objects.filter(position_id__in=position_ids).delete()

            for row in changed_rows:
                BagNumber.objects.create(
                    event=event, position=row.position,
                    number_range=row.number_range, number=row.number,
                )
                if row.old_number is not None:
                    action = "pretix_bagnumbers.number.changed"
                    data = {"old_number": row.old_number, "number": row.number, "positionid": row.positionid}
                else:
                    action = "pretix_bagnumbers.number.assigned"
                    data = {"number": row.number, "positionid": row.positionid}
                row.position.order.log_action(action, user=user, data=data)

            applied = len(changed_rows)
            event.log_action(
                "pretix_bagnumbers.import.completed",
                user=user,
                data={"filename": filename, "applied": applied},
            )
    else:
        event.log_action(
            "pretix_bagnumbers.import.completed",
            user=user,
            data={"filename": filename, "applied": 0},
        )

    return applied
