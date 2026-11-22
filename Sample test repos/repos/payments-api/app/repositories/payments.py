from app.models import AccountRecord, PaymentRecord


def load_account(session, account_id):
    return session.query(AccountRecord).filter(AccountRecord.id == account_id).one_or_none()


def load_payments(session, account_id):
    return session.query(PaymentRecord).filter(PaymentRecord.account_id == account_id).all()


def load_payment(session, payment_id):
    return session.query(PaymentRecord).filter(PaymentRecord.id == payment_id).one_or_none()


def save_payment(session, record):
    session.add(record)
    session.commit()
