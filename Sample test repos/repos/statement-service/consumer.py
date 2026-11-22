from kafka import KafkaConsumer


def poll():
    consumer = KafkaConsumer("payments.created", group_id="statements")
    for msg in consumer:
        remember(msg.value)


def remember(event):
    return event
