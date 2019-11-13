"""This module defines the URL patterns for the `/withdraw` endpoint."""
from django.urls import path
from .views import withdraw, interactive_withdraw

urlpatterns = [
    path("transactions/withdraw/interactive", withdraw),
    path("withdraw/interactive_withdraw", interactive_withdraw, name="interactive_withdraw"),
]
