from django.urls import path

from . import views

urlpatterns = [
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/",
        views.OverviewView.as_view(),
        name="overview",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/range/add/",
        views.RangeCreateUpdateView.as_view(),
        name="range.add",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/range/<int:pk>/",
        views.RangeCreateUpdateView.as_view(),
        name="range.edit",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/range/<int:pk>/delete/",
        views.RangeDeleteView.as_view(),
        name="range.delete",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/range/<int:pk>/delete/confirm/",
        views.RangeDeleteConfirmView.as_view(),
        name="range.delete.confirm",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/number/<int:pk>/",
        views.NumberChangeView.as_view(),
        name="number.edit",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/product/<int:item_pk>/assign/",
        views.BulkAssignView.as_view(),
        name="product.assign_bulk",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/import/",
        views.ImportView.as_view(),
        name="import",
    ),
    path(
        "control/event/<str:organizer>/<str:event>/bagnumbers/export/",
        views.ExportView.as_view(),
        name="export",
    ),
]
