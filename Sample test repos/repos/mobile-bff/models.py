from sqlalchemy import Column, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class PaymentView(Base):
    __tablename__ = "payments"
    id = Column(String(20), primary_key=True)
    account_id = Column(String(12), nullable=False)
    reference = Column(String(18), nullable=True)
