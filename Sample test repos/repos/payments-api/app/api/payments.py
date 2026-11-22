from typing import List

from fastapi import FastAPI, HTTPException

from app.db import SessionLocal
from app.schemas import Balance, Payment, PaymentRequest, PaymentRow
from app.services.payments import (PaymentRefused, account_balance, create_payment, exchange_rates, list_payments,
                                   refund_payment)
from app.repositories.payments import load_payment

app = FastAPI()


@app.post("/payments", response_model=Payment, status_code=201)
def api_create_payment(req: PaymentRequest):
    with SessionLocal() as session:
        try:
            record = create_payment(session, req)
        except LookupError:
            raise HTTPException(status_code=404, detail="unknown account")
        except PaymentRefused:
            raise HTTPException(status_code=409, detail="insufficient balance")
        return {"payment_id": record.id, "account_id": record.account_id, "amount": float(record.amount),
                "currency": record.currency, "status": record.status}


@app.get("/payments/{payment_id}", response_model=Payment)
def api_get_payment(payment_id: str):
    with SessionLocal() as session:
        record = load_payment(session, payment_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown payment")
        return record


@app.get("/accounts/{account_id}/payments", response_model=List[PaymentRow])
def api_list_payments(account_id: str):
    with SessionLocal() as session:
        return list_payments(session, account_id)


@app.get("/accounts/{account_id}/balance", response_model=Balance)
def api_balance(account_id: str):
    with SessionLocal() as session:
        try:
            return account_balance(session, account_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="unknown account")


@app.post("/payments/{payment_id}/refund")
def api_refund(payment_id: str):
    with SessionLocal() as session:
        try:
            return refund_payment(session, payment_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="unknown payment")


@app.get("/fx/rates")
def api_rates():
    return exchange_rates()
