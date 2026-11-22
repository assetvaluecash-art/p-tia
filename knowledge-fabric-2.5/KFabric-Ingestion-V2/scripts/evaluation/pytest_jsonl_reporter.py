"""Pytest plugin: persist one JSONL row per pytest test item with static imports."""
from __future__ import annotations

import ast
import json
import time
from datetime import datetime, timezone
from pathlib import Path

_CONFIG = None

def pytest_addoption(parser):
    group = parser.getgroup("evidence")
    group.addoption("--test-evidence-file", action="store", default=None,
                    help="Append one evidence row per pytest test case to this new JSONL file.")


def pytest_configure(config):
    global _CONFIG
    _CONFIG = config
    path = config.getoption("--test-evidence-file")
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    config._test_evidence_path = out
    config._test_evidence_stream = out.open("x", encoding="utf-8")
    config._test_evidence = {}
    config._test_evidence_imports = {}
    config._test_evidence_started = datetime.now(timezone.utc).isoformat()


def pytest_runtest_logstart(nodeid, location):
    # This hook is available globally; the report stream lives on the config.
    pass


def pytest_runtest_logreport(report):
    config = _CONFIG
    if config is None:
        return
    if not getattr(config, "_test_evidence_path", None):
        return
    state = config._test_evidence.setdefault(report.nodeid, {"phases": {}, "started": time.perf_counter()})
    outcome = report.outcome.upper()
    state["phases"][report.when] = {
        "result": outcome,
        "duration_seconds": round(report.duration, 4),
        "longrepr": str(report.longrepr)[:1200] if report.failed else None,
    }
    if report.when != "teardown":
        return
    item_path = report.nodeid.split("::", 1)[0]
    touchpoints = config._test_evidence_imports.get(item_path)
    if touchpoints is None:
        touchpoints = []
        source = Path(config.rootpath) / item_path
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    touchpoints.extend(alias.name for alias in node.names if alias.name.startswith(("kbfabric_ingest", "custom_ml")))
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(("kbfabric_ingest", "custom_ml")):
                    touchpoints.append(node.module)
        except (OSError, SyntaxError) as exc:
            touchpoints = [f"static-import-map-error:{type(exc).__name__}"]
        config._test_evidence_imports[item_path] = sorted(set(touchpoints))
    phase_results = [phase["result"] for phase in state["phases"].values()]
    final = "FAIL" if "FAILED" in phase_results else ("SKIP" if "SKIPPED" in phase_results else "PASS")
    row = {
        "case_id": report.nodeid,
        "test_file": item_path,
        "method": "pytest",
        "path": report.nodeid,
        "result": final,
        "phase_results": state["phases"],
        "duration_seconds": round(time.perf_counter() - state["started"], 4),
        "touchpoints": touchpoints,
        "evidence_basis": "pytest phase outcome plus static module-level product imports",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    config._test_evidence_stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    config._test_evidence_stream.flush()
    del config._test_evidence[report.nodeid]


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    stream = getattr(config, "_test_evidence_stream", None)
    if stream is None:
        return
    for nodeid, state in list(config._test_evidence.items()):
        row = {
            "case_id": nodeid,
            "method": "pytest",
            "path": nodeid,
            "result": "INCOMPLETE",
            "phase_results": state["phases"],
            "touchpoints": [],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    stream.flush()
    stream.close()
    rows = [json.loads(line) for line in config._test_evidence_path.read_text(encoding="utf-8").splitlines() if line]
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": config._test_evidence_started,
        "exit_status": exitstatus,
        "case_count": len(rows),
        "passed": sum(row["result"] == "PASS" for row in rows),
        "failed": sum(row["result"] == "FAIL" for row in rows),
        "skipped": sum(row["result"] == "SKIP" for row in rows),
        "incomplete": sum(row["result"] == "INCOMPLETE" for row in rows),
        "jsonl_path": str(config._test_evidence_path),
    }
    config._test_evidence_path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
