from kafka import KafkaProducer

TOPIC = "payments.created"
producer = KafkaProducer(bootstrap_servers="kafka:9092")


def publish_created(record):
    producer.send(TOPIC, {"payment_id": record.id, "account_id": record.account_id, "amount": float(record.amount)})
