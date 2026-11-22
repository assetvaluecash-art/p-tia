"""Contract checks for the synthetic documents used by live HITL journeys."""
from __future__ import annotations

import json
from pathlib import Path

from docx import Document


ASSET_DIR = Path(__file__).parent / "assets"


def test_each_golden_document_contains_a_real_same_type_ambiguity_challenge() -> None:
    manifest = json.loads((ASSET_DIR / "synthetic_asset_manifest.json").read_text())
    assert len(manifest["assets"]) == 7

    for asset in manifest["assets"]:
        challenge = asset["review_challenge"]
        assert challenge["candidate_type"] == "System"
        assert len(challenge["candidates"]) == 2
        assert challenge["reference"]
        assert challenge["relationship"] == "INTEGRATES_WITH"
        assert challenge["source"]
        assert "Neither" in challenge["conflict"] and "supersedes the other." in challenge["conflict"]

        doc = Document(ASSET_DIR / asset["filename"])
        text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
        assert text.count("Page ") >= 10
        page_breaks = sum(1 for node in doc.element.body.iter() if node.tag.endswith("}br"))
        assert page_breaks == 9
        assert text.count("System inventory entry. Name:") >= 2
        assert text.count(f"{challenge['source']} integrates with the {challenge['reference']} for the primary system-to-system data exchange.") == 1
        assert text.count(f"API interface record: {challenge['source']} integrates with the {challenge['reference']} through a REST API") == 1
        assert text.count("Both are same-type candidates; no authoritative mapping specifies which System owns this interface.") == 1
        assert text.count("Target type: System") >= 1
        assert text.count("INTEGRATES_WITH") >= 1
        assert text.count("The workflow requires the system owner to resolve the target") == 1
        assert "Expected extraction:" not in text
        assert "Distinguish no-op, rejected, failed" not in text
        for candidate in challenge["candidates"]:
            assert text.count(candidate) >= 2
        assert text.count(challenge["reference"]) >= 6


def test_corporate_lending_fixture_matches_the_configured_product_line() -> None:
    manifest = json.loads((ASSET_DIR / "synthetic_asset_manifest.json").read_text())
    asset = next(item for item in manifest["assets"] if item["domain_id"] == "corporate_commercial")
    assert asset["product_line"] == "Lending"
