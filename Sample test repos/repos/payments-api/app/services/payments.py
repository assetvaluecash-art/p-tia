import os

from app.events import publish_created
from app.models import PaymentRecord
from app.repositories.payments import load_account, load_payment, load_payments, save_payment

MIN_BALANCE = float(os.getenv("MIN_BALANCE", "80"))
RATES = {"EUR": 1.0, "USD": 1.08, "GBP": 0.86}


class PaymentRefused(Exception):
    pass


def balance_of(account):
    """The balance a payment can use."""
    return float(account.balance)


def can_pay(account, amount):
    return balance_of(account) - amount >= MIN_BALANCE


def fee_for(amount):
    return round(amount * 0.01, 2)


def convert(amount, currency):
    return round(amount * RATES[currency], 2)


def create_payment(session, req):
    account = load_account(session, req.account_id)
    if account is None:
        raise LookupError("unknown account")
    if not can_pay(account, req.amount + fee_for(req.amount)):
        raise PaymentRefused("insufficient balance")
    record = PaymentRecord(id=f"P{req.reference}", account_id=req.account_id, amount=req.amount, currency=req.currency,
                           status="PENDING", reference=req.reference, disputed=False)
    save_payment(session, record)
    publish_created(record)
    return record


def list_payments(session, account_id):
    return [{"payment_id": p.id, "amount": float(p.amount), "status": p.status, "disputed": p.disputed}
            for p in load_payments(session, account_id)]


def account_balance(session, account_id):
    account = load_account(session, account_id)
    if account is None:
        raise LookupError("unknown account")
    return {"account_id": account_id, "balance": balance_of(account), "currency": account.currency}


def held_amount(account):
    return float(account.held) if os.getenv("HOLDS_ENABLED") == "true" else 0.0


def refund_payment(session, payment_id):
    record = load_payment(session, payment_id)
    if record is None:
        raise LookupError("unknown payment")
    record.status = "REFUNDED"
    save_payment(session, record)
    return {"refunded": float(record.amount) - fee_for(float(record.amount))}


def exchange_rates():
    return {c: convert(1.0, c) for c in RATES}
