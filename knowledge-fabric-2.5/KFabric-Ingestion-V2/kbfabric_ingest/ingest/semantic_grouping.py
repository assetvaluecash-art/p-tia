"""Structure-aware semantic grouping for ingestion."""

from __future__ import annotations

import re
import uuid
from typing import Iterable

from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.ingest.parser import BLOCK_ID_NAMESPACE
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)

_QE_PREFIX_RE = re.compile(
    r"^(TestCase|UserStory|AcceptanceCriteria|Defect|Incident)\b",
    re.IGNORECASE,
)


def generate_semantic_unit_id(
    source_file: str,
    domain: str,
    source_block_ids: list[str],
    unit_type: str
) -> str:
    """Deterministic UUID for a semantic unit."""
    stable_string = f"semantic:{domain}:{source_file}:{unit_type}:{'|'.join(source_block_ids)}"
    return str(uuid.uuid5(BLOCK_ID_NAMESPACE, stable_string))


def _split_text_by_sentences(text: str) -> list[str]:
    """Split text into sentences by punctuation."""
    sentence_endings = re.compile(r"([.!?])\s+")
    sentences = []
    start = 0
    for match in sentence_endings.finditer(text):
        end = match.end()
        sentence = text[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = end
    if start < len(text):
        last = text[start:].strip()
        if last:
            sentences.append(last)
    return sentences or [text]


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Chunk text into parts that respect max_chars."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current_chunk = ""
    for sentence in _split_text_by_sentences(text):
        if len(current_chunk) + len(sentence) <= max_chars:
            current_chunk += sentence + " "
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            if len(sentence) > max_chars:
                remaining = sentence
                while len(remaining) > max_chars:
                    chunks.append(remaining[:max_chars].strip())
                    remaining = remaining[max_chars:]
                current_chunk = remaining + " "
            else:
                current_chunk = sentence + " "
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    return chunks


_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-•*]\s+|\d+[\.\)]\s+|[a-zA-Z][\.\)]\s+)")
_TEST_CASE_LINE_RE = re.compile(r"^\s*test case\b", re.IGNORECASE)
_TC_ID_LINE_RE = re.compile(r"^\s*TC-[A-Z]+-\d+\b", re.IGNORECASE)


def _looks_like_list_text(text: str) -> bool:
    if not text:
        return False
    first_line = text.strip().splitlines()[0].strip()
    return bool(_LIST_PREFIX_RE.match(first_line))


def _is_list_block(block: CanonicalBlock) -> bool:
    try:
        if (block.metadata or {}).get("list"):
            return True
        return _looks_like_list_text(block.text or "")
    except Exception:
        return False


def _is_list_header(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if t.endswith(":"):
        return True
    return bool(re.match(r"^(Impacted Components|Scenario id|Process id|Test case id)\b", t, re.IGNORECASE))


def _is_list_continuation(prev: CanonicalBlock, curr: CanonicalBlock) -> bool:
    if not prev or not curr:
        return False
    prev_text = (prev.text or "").strip()
    curr_text = (curr.text or "").strip()
    if not prev_text or not curr_text:
        return False
    if _is_list_block(curr):
        return True
    # Short continuation line after a list item (no bullet prefix)
    if len(curr_text) <= 160 and curr_text[0].islower():
        return True
    return False


def _split_block_id(base_id: str, suffix: str) -> str:
    return str(uuid.uuid5(BLOCK_ID_NAMESPACE, f"{base_id}:{suffix}"))


def _split_test_case_paragraphs(blocks: list[CanonicalBlock]) -> list[CanonicalBlock]:
    """Split paragraph blocks so intro text stays separate from Test Case lines."""
    if not blocks:
        return []
    split_blocks: list[CanonicalBlock] = []
    for block in blocks:
        if block.block_type != "paragraph":
            split_blocks.append(block)
            continue
        lines = (block.text or "").splitlines()
        if not lines:
            split_blocks.append(block)
            continue
        split_idx = None
        for idx, line in enumerate(lines):
            candidate = line.strip()
            if not candidate:
                continue
            if _TEST_CASE_LINE_RE.match(candidate) or _TC_ID_LINE_RE.match(candidate):
                split_idx = idx
                break
        if split_idx is None:
            split_blocks.append(block)
            continue
        intro_text = "\n".join(lines[:split_idx]).strip()
        test_case_text = "\n".join(lines[split_idx:]).strip()
        if not intro_text or not test_case_text:
            split_blocks.append(block)
            continue
        intro_block = block.copy(update={"text": intro_text})
        testcase_block = block.copy(
            update={
                "block_id": _split_block_id(block.block_id, f"tc_split_{split_idx}"),
                "text": test_case_text,
            }
        )
        split_blocks.extend([intro_block, testcase_block])
    return split_blocks


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r\n", "\n")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    merged: list[str] = []
    for line in lines:
        if _looks_like_list_text(line) or line.endswith(":"):
            merged.append(line)
        else:
            if merged:
                merged[-1] = f"{merged[-1]} {line}"
            else:
                merged.append(line)
    return "\n".join(merged).strip()


def _format_block_text(block: CanonicalBlock) -> str:
    text = _normalize_text(block.text or "")
    if not text:
        return ""
    if _is_list_block(block) and not text.lstrip().startswith(("-", "•")):
        return f"- {text}"
    return text


def _merge_metadata(blocks: Iterable[CanonicalBlock]) -> dict:
    merged: dict = {}
    for block in blocks:
        if block.metadata:
            merged.update(block.metadata)
    return merged


def _build_text_from_blocks(blocks: list[CanonicalBlock], header_text_raw: str = "") -> str:
    parts = []
    if header_text_raw:
        header_text_raw = _normalize_text(header_text_raw)
        if header_text_raw:
            parts.append(header_text_raw)
    for block in blocks:
        if block.block_type == "image":
            continue
        formatted = _format_block_text(block)
        if formatted:
            parts.append(formatted)
    return "\n".join(parts).strip()


def _same_page(a: CanonicalBlock, b: CanonicalBlock) -> bool:
    if a.page_number is None or b.page_number is None:
        return True
    return a.page_number == b.page_number


def _find_prev_heading(blocks: list[CanonicalBlock], idx: int) -> str:
    for j in range(idx - 1, -1, -1):
        if blocks[j].block_type == "heading" and (blocks[j].text or "").strip():
            return blocks[j].text.strip()[:200]
    return ""


_TC_HEADER_RE = re.compile(
    r"test[\s_-]?case[\s_-]?id|tc[\s_-]?id|testcase[\s_-]?id",
    re.IGNORECASE,
)


def _extract_tc_id_from_kv(text: str) -> str:
    """Extract the Test Case ID value from a Key=Value row.

    Scans all Key=Value pairs for one whose key matches the TC header
    pattern and returns its value, or "" if not found.
    """
    for segment in re.split(r",\s*(?=[A-Za-z][A-Za-z0-9 _-]*=)", text):
        if "=" not in segment:
            continue
        key, _, val = segment.partition("=")
        if _TC_HEADER_RE.search(key.strip()):
            return val.strip()
    return ""


def _consolidate_test_case_rows(
    rows: list[CanonicalBlock],
    header_text_raw: str
) -> list[list[CanonicalBlock]]:
    """Consolidate spreadsheet rows belonging to the same test case.

    Groups consecutive rows sharing the same Test Case ID (or having an
    empty ID, indicating a continuation/step row).
    Supports two text formats:
      - Key=Value: ``Test case ID=CLOS_DEL_SCN_004_TC_001, Scenario ID=...``
      - Column-letter: ``A=CLOS_DEL_SCN_004_TC_001, B=...``
    Returns a list of groups, each a list of CanonicalBlocks.
    """
    if not rows:
        return []

    # Strategy 1: Key=Value format (column names as keys)
    first_text = rows[0].text or ""
    if _extract_tc_id_from_kv(first_text):
        groups: list[list[CanonicalBlock]] = []
        current_group: list[CanonicalBlock] = []
        current_id: str | None = None
        for row in rows:
            row_id = _extract_tc_id_from_kv(row.text or "")
            if not row_id or row_id == current_id:
                current_group.append(row)
            else:
                if current_group:
                    groups.append(current_group)
                current_group = [row]
                current_id = row_id
        if current_group:
            groups.append(current_group)
        return groups

    # Strategy 2: Column-letter format (A=value, B=value)
    tc_col = None
    header_lower = (header_text_raw or "").lower()
    tc_patterns = ["test case id", "testcase_id", "test_case_id", "tc_id", "tcid", "test case name"]
    for pat in tc_patterns:
        if pat in header_lower:
            match = re.search(
                r'([A-Z]{1,2})=' + re.escape(pat),
                header_lower.replace(" ", "").replace("_", ""),
            )
            if match:
                tc_col = match.group(1)
                break
    if tc_col is None:
        for col_letter in ["A", "B", "C"]:
            col_pat = re.compile(r'\b' + col_letter + r'=(\S+)', re.IGNORECASE)
            for row in rows[:3]:
                m = col_pat.search(row.text or "")
                if m and re.match(r'^(TC|CLOS|DEL|SYN|SCN)[_\-]', m.group(1), re.IGNORECASE):
                    tc_col = col_letter
                    break
            if tc_col:
                break

    if tc_col is None:
        return [[r] for r in rows]

    groups = []
    current_group: list[CanonicalBlock] = []
    current_id = None
    col_pat = re.compile(r'\b' + tc_col + r'=([^,]+)', re.IGNORECASE)
    for row in rows:
        m = col_pat.search(row.text or "")
        row_id = m.group(1).strip() if m else ""
        if not row_id or row_id == current_id:
            current_group.append(row)
        else:
            if current_group:
                groups.append(current_group)
            current_group = [row]
            current_id = row_id
    if current_group:
        groups.append(current_group)

    return groups


def _create_semantic_blocks(
    unit_blocks: list[CanonicalBlock],
    unit_type: str,
    max_chars: int,
    image_block: CanonicalBlock | None = None,
    heading_context_raw: str = "",
    caption_text_raw: str = "",
    header_text_raw: str = ""
) -> list[CanonicalBlock]:
    source_block_ids = [b.block_id for b in unit_blocks]
    source_file = unit_blocks[0].source_file
    domain = unit_blocks[0].domain
    semantic_unit_id = generate_semantic_unit_id(source_file, domain, source_block_ids, unit_type)

    base_metadata = _merge_metadata(unit_blocks)
    if heading_context_raw:
        base_metadata["heading_context_raw"] = heading_context_raw
    if caption_text_raw:
        base_metadata["caption_text_raw"] = caption_text_raw

    text = _build_text_from_blocks(unit_blocks, header_text_raw=header_text_raw)

    if image_block:
        # Image units should not be split. Truncate deterministically if too long.
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()
            base_metadata["unit_truncated"] = True

        return [
            CanonicalBlock(
                block_id=semantic_unit_id,
                source_file=source_file,
                file_type=image_block.file_type,
                domain=domain,
                page_number=image_block.page_number,
                block_index=min(b.block_index for b in unit_blocks),
                block_type="image",
                text=text,
                image_bytes=image_block.image_bytes,
                metadata=base_metadata,
                semantic_unit_id=semantic_unit_id,
                source_block_ids=source_block_ids,
                unit_type=unit_type,
                unit_part_index=1,
                unit_part_total=1
            )
        ]

    # One row = one block; do not split table/test_case/defect/incident rows by character count.
    _row_unit_types = {"table_row", "test_case_row", "defect_row", "incident_row"}
    if unit_type in _row_unit_types and len(unit_blocks) == 1:
        parts = [text] if text else [""]
    else:
        parts = _chunk_text(text, max_chars) if text else [""]
    unit_blocks_out: list[CanonicalBlock] = []
    for idx, part in enumerate(parts, start=1):
        part_id = semantic_unit_id if idx == 1 and len(parts) == 1 else str(
            uuid.uuid5(BLOCK_ID_NAMESPACE, f"{semantic_unit_id}|part:{idx}")
        )
        block_type = unit_type if unit_type in _row_unit_types else "paragraph"
        unit_blocks_out.append(
            CanonicalBlock(
                block_id=part_id,
                source_file=source_file,
                file_type=unit_blocks[0].file_type,
                domain=domain,
                page_number=unit_blocks[0].page_number,
                block_index=min(b.block_index for b in unit_blocks),
                block_type=block_type,
                text=part,
                image_bytes=None,
                metadata=base_metadata,
                semantic_unit_id=semantic_unit_id,
                source_block_ids=source_block_ids,
                unit_type=unit_type,
                unit_part_index=idx,
                unit_part_total=len(parts)
            )
        )
    return unit_blocks_out


def group_blocks(
    blocks: list[CanonicalBlock],
    max_chars_per_block: int,
    group_file_types: set[str] | None = None,
    list_grouping_enabled: bool = True,
    paragraph_streak_enabled: bool = False,
    paragraph_streak_max_blocks: int = 5,
    paragraph_streak_same_page: bool = True
) -> list[CanonicalBlock]:
    """Group physical blocks into structure-aware semantic units."""
    if not blocks:
        return []
    if group_file_types is None:
        group_file_types = {"docx", "pdf", "pptx", "xlsx", "csv"}

    # Normalize paragraph blocks before grouping
    blocks = _split_test_case_paragraphs(blocks)

    # Group by source_file
    file_to_blocks: dict[str, list[CanonicalBlock]] = {}
    for block in blocks:
        file_to_blocks.setdefault(block.source_file, []).append(block)

    grouped: list[CanonicalBlock] = []
    for source_file, file_blocks in file_to_blocks.items():
        file_blocks = sorted(file_blocks, key=lambda b: b.block_index)
        if not file_blocks or file_blocks[0].file_type not in group_file_types:
            grouped.extend(file_blocks)
            continue

        consumed: set[int] = set()
        last_heading_text = ""

        for i, block in enumerate(file_blocks):
            if i in consumed:
                continue

            if block.block_type == "heading":
                last_heading_text = (block.text or "").strip()
                unit_blocks = [block]
                consumed.add(i)
                j = i + 1
                while j < len(file_blocks):
                    nxt = file_blocks[j]
                    if nxt.block_type in ("heading", "image", "table_row"):
                        break
                    if block.page_number is not None and nxt.page_number is not None:
                        if block.page_number != nxt.page_number:
                            break
                    unit_blocks.append(nxt)
                    consumed.add(j)
                    j += 1
                grouped.extend(_create_semantic_blocks(
                    unit_blocks, "heading_paragraphs", max_chars_per_block
                ))
                continue

            _row_types = {"table_row", "test_case_row", "defect_row", "incident_row"}
            if block.block_type in _row_types:
                rows: list[CanonicalBlock] = []
                caption_block = None
                if i > 0 and (i - 1) not in consumed:
                    prev = file_blocks[i - 1]
                    if prev.block_type in ("heading", "paragraph") and not _is_list_block(prev):
                        if _same_page(prev, block) or prev.page_number is None:
                            caption_block = prev
                            consumed.add(i - 1)

                j = i
                while j < len(file_blocks) and file_blocks[j].block_type in _row_types:
                    rows.append(file_blocks[j])
                    consumed.add(j)
                    j += 1

                header_text_raw = ""
                if rows:
                    header_candidate = (rows[0].metadata or {}).get("table_header_candidate") or (rows[0].text or "").strip()
                    if header_candidate:
                        header_text_raw = header_candidate

                if caption_block:
                    header_text_raw = "\n".join([t for t in [caption_block.text, header_text_raw] if t])

                row_type = block.block_type

                # Key=Value rows already embed column names — skip the full
                # header to save embedding tokens.  The header remains in
                # block metadata (table_header_candidate) for LLM IE.
                _has_kv = any("=" in (r.text or "") for r in rows)
                effective_header = "" if _has_kv else header_text_raw

                if row_type == "test_case_row":
                    tc_groups = _consolidate_test_case_rows(rows, header_text_raw)
                    for tc_rows in tc_groups:
                        grouped.extend(_create_semantic_blocks(
                            tc_rows,
                            "test_case_row",
                            max_chars_per_block,
                            header_text_raw=effective_header
                        ))
                else:
                    for row in rows:
                        grouped.extend(_create_semantic_blocks(
                            [row],
                            row_type,
                            max_chars_per_block,
                            header_text_raw=effective_header
                        ))
                continue

            if block.block_type == "image":
                unit_blocks = [block]
                consumed.add(i)

                heading_context_raw = last_heading_text
                caption_text_raw = ((block.metadata or {}).get("caption_text_raw") or "").strip()

                # Candidate caption/description blocks around the image
                for offset in (-1, 1):
                    idx = i + offset
                    if 0 <= idx < len(file_blocks) and idx not in consumed:
                        candidate = file_blocks[idx]
                        if candidate.block_type == "paragraph" and not _is_list_block(candidate):
                            if _same_page(candidate, block) or block.page_number is None:
                                unit_blocks.insert(0, candidate) if offset < 0 else unit_blocks.append(candidate)
                                consumed.add(idx)
                                if not caption_text_raw and offset < 0:
                                    caption_text_raw = candidate.text.strip()[:200]

                grouped.extend(_create_semantic_blocks(
                    unit_blocks,
                    "caption_image_description",
                    max_chars_per_block,
                    image_block=block,
                    heading_context_raw=heading_context_raw,
                    caption_text_raw=caption_text_raw
                ))
                continue

            # List header + list items
            if list_grouping_enabled and block.block_type == "paragraph" and _is_list_header(block.text or ""):
                unit_blocks = [block]
                consumed.add(i)
                j = i + 1
                while j < len(file_blocks):
                    nxt = file_blocks[j]
                    if nxt.block_type != "paragraph":
                        break
                    if _is_list_block(nxt) or _is_list_continuation(block, nxt):
                        unit_blocks.append(nxt)
                        consumed.add(j)
                        j += 1
                        continue
                    break
                grouped.extend(_create_semantic_blocks(
                    unit_blocks, "list_group", max_chars_per_block
                ))
                continue

            if list_grouping_enabled and block.block_type == "paragraph" and _is_list_block(block):
                unit_blocks = [block]
                consumed.add(i)
                j = i + 1
                while j < len(file_blocks):
                    nxt = file_blocks[j]
                    if nxt.block_type != "paragraph":
                        break
                    if paragraph_streak_same_page and not _same_page(block, nxt):
                        break
                    if _is_list_block(nxt) or _is_list_continuation(block, nxt):
                        unit_blocks.append(nxt)
                        consumed.add(j)
                        j += 1
                        continue
                    break
                grouped.extend(_create_semantic_blocks(
                    unit_blocks, "list_group", max_chars_per_block
                ))
                continue

            if paragraph_streak_enabled and block.block_type == "paragraph" and not _is_list_block(block):
                block_text_stripped = (block.text or "").lstrip("- ").strip()
                is_qe_start = bool(_QE_PREFIX_RE.match(block_text_stripped))
                unit_blocks = [block]
                j = i + 1
                while not is_qe_start and j < len(file_blocks) and len(unit_blocks) < paragraph_streak_max_blocks:
                    nxt = file_blocks[j]
                    if nxt.block_type != "paragraph" or _is_list_block(nxt):
                        break
                    if paragraph_streak_same_page and not _same_page(block, nxt):
                        break
                    nxt_text = (nxt.text or "").lstrip("- ").strip()
                    if _QE_PREFIX_RE.match(nxt_text):
                        break
                    unit_blocks.append(nxt)
                    consumed.add(j)
                    j += 1
                if len(unit_blocks) > 1:
                    consumed.add(i)
                    grouped.extend(_create_semantic_blocks(
                        unit_blocks, "paragraph_streak", max_chars_per_block
                    ))
                    continue

            # Default: standalone paragraph/list
            grouped.extend(_create_semantic_blocks(
                [block],
                "standalone_paragraph",
                max_chars_per_block
            ))
            consumed.add(i)

    logger.info(
        "Semantic grouping complete",
        input_blocks=len(blocks),
        output_blocks=len(grouped)
    )
    return grouped
