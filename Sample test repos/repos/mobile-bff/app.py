from fastapi import FastAPI

from client import fetch_payments
from repository import recent_references

app = FastAPI()


def activity(account_id):
    """Every payment of the account, newest first."""
    return sorted(fetch_payments(account_id), key=lambda p: p["payment_id"], reverse=True)


@app.get("/mobile/accounts/{account_id}/activity")
def mobile_activity(account_id: str):
    return {"items": activity(account_id), "references": recent_references(account_id)}
