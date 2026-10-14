from django.contrib.auth import views as auth_views
from django.urls import path

from budget import views


urlpatterns = [
    path("", views.orders_view, name="home"),

    path("orders/", views.orders_view, name="orders"),
    path("grants/", views.grants_view, name="grants"),
    path("resolve/", views.resolve_view, name="resolve"),

    path("orders/<int:pk>/file/", views.order_file, name="order_file"),

    path("resolve/approve/", views.resolve_approve, name="resolve_approve"),
    path("resolve/recycle/<int:pk>/", views.allocation_recycle, name="allocation_recycle"),
    path("resolve/lab-updated/<int:pk>/", views.allocation_lab_updated, name="allocation_lab_updated"),

    path("import/<int:pk>/", views.import_preview, name="import_preview"),
    path("import/<int:pk>/confirm/", views.import_confirm, name="import_confirm"),
    path("import/<int:pk>/cancel/", views.import_cancel, name="import_cancel"),
    path("import/<int:pk>/mode/", views.import_mode, name="import_mode"),

  
    path("settings/", views.settings_view, name="settings"),
    path("settings/password/", views.set_password, name="set_password"),

   
    path("api/<str:key>/new/", views.api_create, name="api_create"),
    path("api/<str:key>/import/", views.api_import, name="api_import"),
    path("api/<str:key>/autofetch/", views.api_autofetch, name="api_autofetch"),
    path("autofetch/<int:pk>/", views.api_autofetch_status, name="autofetch_status"),
    path("autofetch/<int:pk>/screenshot.png", views.autofetch_shot, name="autofetch_shot"),
    path("autofetch/<int:pk>/file/", views.autofetch_file, name="autofetch_file"),
    path("api/<str:key>/<str:pk>/", views.api_update, name="api_update"),
    path("api/<str:key>/<str:pk>/delete/", views.api_delete, name="api_delete"),

    # Authentication
    path("login/", auth_views.LoginView.as_view(
        template_name="budget/login.html"
    ), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]
