"""This module defines the URL patterns for the `/deposit` endpoint."""
from django.urls import path
from .views import deposit, interactive_deposit, confirm_transaction
from django.views.decorators.csrf import csrf_exempt

urlpatterns = [
    path("transactions/deposit/interactive", csrf_exempt(deposit)),
    path("deposit/interactive_deposit", interactive_deposit, name="interactive_deposit"),
    path("deposit/confirm_transaction", confirm_transaction, name="confirm_transaction"),
]
