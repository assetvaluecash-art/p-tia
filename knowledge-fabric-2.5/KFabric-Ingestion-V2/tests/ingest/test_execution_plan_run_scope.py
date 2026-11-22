from types import SimpleNamespace

from kbfabric_ingest.ingest.pipeline import ingestion_pipeline_steps as steps


def test_execution_plan_ids_are_stable_within_batch_and_isolated_between_batches(monkeypatch):
    mapping = SimpleNamespace(incoming=SimpleNamespace(block_id="stable-block-id"))
    monkeypatch.setattr(steps, "evaluate_execution_gate", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        steps,
        "build_execution_plan",
        lambda **kwargs: SimpleNamespace(
            plan_id=kwargs["plan_id"],
            action=SimpleNamespace(value="QUEUE_FOR_REVIEW"),
        ),
    )

    first = steps.step_build_execution_plans([mapping], ingestion_batch_id="batch-a")
    retry = steps.step_build_execution_plans([mapping], ingestion_batch_id="batch-a")
    second_run = steps.step_build_execution_plans([mapping], ingestion_batch_id="batch-b")

    assert first[0].plan_id == retry[0].plan_id
    assert first[0].plan_id != second_run[0].plan_id
    assert "batch-a" in first[0].plan_id
    assert "batch-b" in second_run[0].plan_id
