"""This module defines the URL patterns for the `/withdraw` endpoint."""
from django.urls import path
from .views import withdraw, interactive_withdraw
from django.views.decorators.csrf import csrf_exempt

urlpatterns = [
    path("transactions/withdraw/interactive", csrf_exempt(withdraw)),
    path("withdraw/interactive_withdraw", interactive_withdraw, name="interactive_withdraw"),
]
