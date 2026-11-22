from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import PaymentView

SessionLocal = sessionmaker(bind=create_engine("postgresql://payments-db/payments"))


def recent_references(account_id):
    with SessionLocal() as session:
        return [p.reference for p in session.query(PaymentView).filter(PaymentView.account_id == account_id).all()]
