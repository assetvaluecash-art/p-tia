from sqlalchemy import Boolean, Column, Numeric, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class AccountRecord(Base):
    __tablename__ = "accounts"
    id = Column(String(12), primary_key=True)
    balance = Column(Numeric(14, 2), nullable=False)
    held = Column(Numeric(14, 2), nullable=False)
    currency = Column(String(3), nullable=False)


class PaymentRecord(Base):
    __tablename__ = "payments"
    id = Column(String(20), primary_key=True)
    account_id = Column(String(12), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String(12), nullable=False)
    reference = Column(String(18), nullable=True)
    disputed = Column(Boolean, nullable=False)
