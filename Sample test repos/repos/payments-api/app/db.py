from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SessionLocal = sessionmaker(bind=create_engine("postgresql://payments-db/payments"))
