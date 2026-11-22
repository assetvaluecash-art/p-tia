#!/usr/bin/env python3
"""Build a per-test static map from pytest cases to imported product modules.

This is a review aid for repository cleanup, not a runtime call graph. Dynamic
imports, fixtures, dependency injection, and calls through aliases can make an
import-only map incomplete; each row labels its evidence as static imports.
"""
from __future__ import annotations

import ast
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "tests"


def module_for_path(path: Path) -> str:
    return path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")


def main() -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "reports" / "test_touchpoints"
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"test-touchpoints-{run_id}.jsonl"
    summary_path = out_dir / f"test-touchpoints-{run_id}.json"
    rows: list[dict[str, object]] = []
    parse_errors: list[dict[str, str]] = []

    for path in sorted(TESTS.rglob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            parse_errors.append({"path": str(path.relative_to(ROOT)), "error": f"{type(exc).__name__}: {exc}"})
            continue

        file_imports: set[str] = set()
        functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    names = [node.module or ""]
                file_imports.update(name for name in names if name.startswith(("kbfabric_ingest", "custom_ml")))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                functions.append(node)

        if not functions:
            continue
        for fn in sorted(functions, key=lambda item: item.lineno):
            local_imports = set(file_imports)
            for node in ast.walk(fn):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    else:
                        names = [node.module or ""]
                    local_imports.update(name for name in names if name.startswith(("kbfabric_ingest", "custom_ml")))
            rows.append({
                "case_id": f"MAP-{path.relative_to(TESTS).as_posix()}::{fn.name}",
                "test_file": path.relative_to(ROOT).as_posix(),
                "test_name": fn.name,
                "line": fn.lineno,
                "touchpoints": sorted(local_imports),
                "evidence_basis": "static product-module imports at file/function scope",
                "coverage_warning": "Does not prove runtime calls; inspect fixtures, dependency injection, and indirect calls.",
            })

    with jsonl_path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    module_counts: Counter[str] = Counter()
    for row in rows:
        module_counts.update(row["touchpoints"])
    summary = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "AST static import inventory",
        "test_case_count": len(rows),
        "parse_error_count": len(parse_errors),
        "test_cases_without_product_imports": sum(not row["touchpoints"] for row in rows),
        "most_imported_modules": module_counts.most_common(100),
        "jsonl_path": str(jsonl_path),
        "parse_errors": parse_errors,
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 1 if parse_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
