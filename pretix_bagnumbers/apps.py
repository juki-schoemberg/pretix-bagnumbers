from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class PluginApp(AppConfig):
    name = "pretix_bagnumbers"
    verbose_name = _("Bag Numbers")

    class PretixPluginMeta:
        name = _("Bag Numbers")
        author = "JuKi Schömberg e.V."
        description = _(
            "Consecutive bag numbers with configurable number ranges per product. "
            "Numbers are unique per event, are released on cancellation, and are "
            "available in ticket printing and exports."
        )
        visible = True
        version = "1.1.0"
        category = "FEATURE"
        compatibility = "pretix>=2026.9.0"

    def ready(self):
        from . import signals  # noqa
        from . import logentrytypes  # noqa
