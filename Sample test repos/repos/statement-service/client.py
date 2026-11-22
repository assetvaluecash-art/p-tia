import os

import requests

PAYMENTS_BASE_URL = os.getenv("PAYMENTS_BASE_URL", "http://payments-api")


def fetch_payments(account_id: str):
    return requests.get(f"{PAYMENTS_BASE_URL}/accounts/{account_id}/payments", timeout=5).json()
