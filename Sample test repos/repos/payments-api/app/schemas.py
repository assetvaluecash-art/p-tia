from enum import Enum

from pydantic import BaseModel, Field


class Currency(str, Enum):
    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"


class PaymentRequest(BaseModel):
    account_id: str = Field(min_length=6, max_length=12)
    amount: float = Field(gt=0, le=10000)
    currency: Currency
    reference: str = Field(max_length=18)


class Payment(BaseModel):
    payment_id: str
    account_id: str
    amount: float
    currency: str
    status: str


class PaymentRow(BaseModel):
    payment_id: str
    amount: float
    status: str
    disputed: bool


class Balance(BaseModel):
    account_id: str
    balance: float
    currency: str
