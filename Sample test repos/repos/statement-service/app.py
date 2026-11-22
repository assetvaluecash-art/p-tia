from fastapi import FastAPI

from client import fetch_payments

app = FastAPI()


def statement_rows(account_id):
    """Disputed payments are left out of the statement."""
    return [p for p in fetch_payments(account_id) if not p["disputed"]]


def render_pdf(rows):
    return b"%PDF " + str(len(rows)).encode()


@app.get("/statements/{account_id}/pdf")
def statement_pdf(account_id: str):
    return render_pdf(statement_rows(account_id))
