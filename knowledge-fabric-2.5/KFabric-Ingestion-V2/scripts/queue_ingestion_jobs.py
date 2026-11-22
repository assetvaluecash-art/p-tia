from __future__ import annotations

from kbfabric.worker.celery_app import run_ingestion_task


def main() -> None:
    jobs = [
        ("samples/trade_finance", "trade_finance"),
        ("samples/wealth_portfolio", "wealth_portfolio"),
        ("samples/consumer_payments", "consumer_payments"),
    ]

    for input_dir, domain in jobs:
        result = run_ingestion_task.delay(input_dir, domain)
        print(f"Queued ingestion job for domain={domain}, task_id={result.id}")


if __name__ == "__main__":
    main()

