from django.utils.translation import gettext_lazy as _

from pretix.base.logentrytypes import EventLogEntryType, OrderLogEntryType, log_entry_types


@log_entry_types.new_from_dict({
    "pretix_bagnumbers.number.assigned": _(
        "Bag number {number} was assigned (position #{positionid})."
    ),
    "pretix_bagnumbers.number.released": _(
        "Bag number {number} was released (position #{positionid})."
    ),
    "pretix_bagnumbers.number.changed": _(
        "Bag number was manually changed from {old_number} to {number} "
        "(position #{positionid})."
    ),
})
class BagNumberLogEntryType(OrderLogEntryType):
    pass


@log_entry_types.new_from_dict({
    "pretix_bagnumbers.import.completed": _(
        "Bag numbers were imported from file \"{filename}\" ({applied} positions updated)."
    ),
})
class BagNumberImportLogEntryType(EventLogEntryType):
    pass
