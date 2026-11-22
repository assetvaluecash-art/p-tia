from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs/openapi/ingestion-studio-job-hitl.openapi.yaml"
GUIDE_PATH = ROOT / "docs/INGESTION_STUDIO_JOB_HITL_UI_GUIDELINES.md"
OUT_PATH = ROOT / "docs/INGESTION_STUDIO_API_AND_UI_HANDBOOK.docx"
SPEC = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))

NAVY = "12345A"
BLUE = "2468B4"
PALE = "EAF1F8"
MID = "52657A"
INK = "24364B"


def shade(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_cell_text(cell, value: str, bold: bool = False, color: str = INK, size: int = 9) -> None:
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    r = p.add_run(value)
    r.bold = bold
    r.font.name = "Aptos"
    r.font.size = Pt(size)
    r.font.color.rgb = RGBColor.from_string(color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def table(doc: Document, headers: list[str], rows: list[list[str]], widths: list[float] | None = None) -> None:
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(headers):
        set_cell_text(t.rows[0].cells[i], h, True, "FFFFFF", 9)
        shade(t.rows[0].cells[i], NAVY)
    for row in rows:
        cells = t.add_row().cells
        for i, value in enumerate(row):
            set_cell_text(cells[i], value)
            if len(t.rows) % 2 == 0:
                shade(cells[i], "F5F8FB")
    if widths:
        for row in t.rows:
            for i, width in enumerate(widths):
                row.cells[i].width = Inches(width)
    doc.add_paragraph().paragraph_format.space_after = Pt(1)


def resolve_ref(ref: str) -> dict[str, Any]:
    node: Any = SPEC
    for part in ref.removeprefix("#/ ").replace("#/", "").split("/"):
        if part:
            node = node[part]
    return node


def deref(schema: dict[str, Any] | None, seen: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    seen = set(seen or ())
    ref = schema.get("$ref")
    if not ref:
        return schema
    if ref in seen:
        return {"type": "object", "description": "(recursive reference)"}
    seen.add(ref)
    base = dict(resolve_ref(ref))
    base.update({k: v for k, v in schema.items() if k != "$ref"})
    return deref(base, seen)


def type_label(schema: dict[str, Any]) -> str:
    typ = schema.get("type", "object")
    if typ == "array":
        return f"array of {type_label(deref(schema.get('items', {})))}"
    if typ == "object" and schema.get("additionalProperties"):
        return "object/map"
    return str(typ) + (f" ({schema['format']})" if schema.get("format") else "")


def schema_rows(schema: dict[str, Any], prefix: str = "", required: bool = False, seen: set[str] | None = None) -> list[list[str]]:
    schema = deref(schema, seen)
    if not schema:
        return [[prefix or "—", "—", ""]]
    rows: list[list[str]] = []
    if schema.get("enum"):
        rows.append([prefix or "value", "enum", ", ".join(map(str, schema["enum"]))])
        return rows
    props = schema.get("properties")
    if props:
        reqs = set(schema.get("required", []))
        for name, child in props.items():
            child = deref(child, seen)
            label = f"{prefix}.{name}" if prefix else name
            is_required = name in reqs
            desc = str(child.get("description", ""))
            rows.append([label + (" *" if is_required else ""), type_label(child), desc])
            if child.get("properties"):
                rows.extend(schema_rows(child, label, is_required, seen))
            elif child.get("type") == "array" and isinstance(child.get("items"), dict):
                item = deref(child["items"], seen)
                if item.get("properties"):
                    rows.extend(schema_rows(item, label + "[]", False, seen))
        return rows
    return [[prefix or "value", type_label(schema), str(schema.get("description", ""))]]


def add_schema_table(doc: Document, schema: dict[str, Any], title: str) -> None:
    doc.add_heading(title, level=4)
    rows = schema_rows(schema)
    table(doc, ["Field ( * required )", "Type", "Meaning / constraints"], rows, [2.0, 1.25, 3.55])


def add_operation(doc: Document, path: str, method: str, op: dict[str, Any]) -> None:
    doc.add_heading(f"{method.upper()}  {path}", level=2)
    p = doc.add_paragraph()
    r = p.add_run(f"{op.get('summary', '')}  ·  {op.get('x-implementation-status', 'Status not classified')}")
    r.bold = True
    r.font.color.rgb = RGBColor.from_string(BLUE)
    p = doc.add_paragraph(op.get("description", "No operation description supplied."))
    p.paragraph_format.space_after = Pt(5)
    params = op.get("parameters", [])
    if params:
        rows = []
        for item in params:
            item = deref(item)
            sch = deref(item.get("schema", {}))
            rows.append([f"{item.get('name')} ({item.get('in')})", "Required" if item.get("required") else "Optional", type_label(sch), str(item.get("description", sch.get("description", "")))])
        table(doc, ["Parameter", "Use", "Type", "Meaning"], rows, [1.45, .8, 1.1, 3.45])
    body = op.get("requestBody")
    if body:
        doc.add_heading("Request body", level=3)
        for media, content in body.get("content", {}).items():
            doc.add_paragraph(f"Content type: {media}. {'Required.' if body.get('required') else 'Optional.'}")
            if "schema" in content:
                add_schema_table(doc, content["schema"], "Request fields")
            if "example" in content:
                doc.add_paragraph("Example:")
                doc.add_paragraph(yaml.safe_dump(content["example"], sort_keys=False, allow_unicode=True).strip(), style="No Spacing")
    responses = op.get("responses", {})
    rows = []
    for code, response in responses.items():
        response = deref(response)
        description = response.get("description", "")
        schemas = []
        for media, content in response.get("content", {}).items():
            sch = content.get("schema")
            if sch:
                expanded = deref(sch)
                schemas.append(f"{media}: {type_label(expanded)}")
        rows.append([str(code), description, "\n".join(schemas) or "No response body documented"])
    doc.add_heading("Responses", level=3)
    table(doc, ["HTTP", "Meaning", "Response body"], rows, [.65, 3.2, 2.95])
    # Display key successful response structure separately to make integration straightforward.
    for code, response in responses.items():
        if str(code).startswith("2"):
            response = deref(response)
            for content in response.get("content", {}).values():
                schema = content.get("schema")
                if schema:
                    add_schema_table(doc, schema, f"HTTP {code} response fields")
                    break
            break


def markdown_to_doc(doc: Document, text: str) -> None:
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("``` ") or line.strip() == "```":
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(.18)
            r = p.add_run("\n".join(code))
            r.font.name = "Aptos Mono"
            r.font.size = Pt(8)
            r.font.color.rgb = RGBColor.from_string("334155")
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"\|?\s*:?-{3,}", lines[i + 1]):
            headers = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            i += 2
            data = []
            while i < len(lines) and lines[i].startswith("|"):
                data.append([c.strip().strip("`") for c in lines[i].strip().strip("|").split("|")])
                i += 1
            table(doc, headers, data)
            continue
        if line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif re.match(r"^\s*[-*] ", line):
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(re.sub(r"^\s*[-*] ", "", line))
        elif re.match(r"^\s*\d+\. ", line):
            p = doc.add_paragraph(style="List Number")
            p.add_run(re.sub(r"^\s*\d+\. ", "", line))
        else:
            doc.add_paragraph(line)
        i += 1


def main() -> None:
    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = Inches(.65)
    sec.bottom_margin = Inches(.65)
    sec.left_margin = Inches(.72)
    sec.right_margin = Inches(.72)
    styles = doc.styles
    styles["Normal"].font.name = "Aptos"
    styles["Normal"].font.size = Pt(9.5)
    styles["Normal"].font.color.rgb = RGBColor.from_string(INK)
    for name, size, color in [("Title", 28, NAVY), ("Heading 1", 20, NAVY), ("Heading 2", 13, BLUE), ("Heading 3", 11, NAVY), ("Heading 4", 10, MID)]:
        st = styles[name]
        st.font.name = "Aptos Display"
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = RGBColor.from_string(color)
    header = sec.header.paragraphs[0]
    header.text = "PULSE AI  /  INGESTION STUDIO API HANDBOOK"
    header.runs[0].font.size = Pt(8)
    header.runs[0].font.bold = True
    header.runs[0].font.color.rgb = RGBColor.from_string(MID)
    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("Engineering and UI integration guide  ·  Working API contract")
    footer.runs[0].font.size = Pt(8)
    footer.runs[0].font.color.rgb = RGBColor.from_string(MID)

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(70)
    r = p.add_run("INGESTION STUDIO")
    r.bold = True
    r.font.size = Pt(11)
    r.font.color.rgb = RGBColor.from_string(BLUE)
    doc.add_heading("API & UI Integration Handbook", 0)
    p = doc.add_paragraph("Customer setup → ingestion submission → run monitoring → human review (HITL)")
    p.runs[0].font.size = Pt(15)
    p.runs[0].font.color.rgb = RGBColor.from_string(MID)
    doc.add_paragraph("Prepared from the repository OpenAPI contract and existing UI guidelines · 15 October 2026")
    doc.add_paragraph("Companion machine-readable contract: docs/openapi/ingestion-studio-job-hitl.openapi.yaml")
    doc.add_paragraph("Audience: UI engineering, API integration, QA, and product engineering")
    doc.add_page_break()

    doc.add_heading("How to use this handbook", level=1)
    doc.add_paragraph("This guide documents the end-to-end UI-facing contract, with field tables and response outcomes for each endpoint. OpenAPI is the structured source for route schemas. The existing UI guideline document is included in full in the final section.")
    table(doc, ["Label", "Meaning for integration"], [
        ["Existing", "Route is present in the current FastAPI service."],
        ["Partial", "A route or workflow exists, but one or more behaviors, fields, or security integrations remain incomplete."],
        ["Planned", "Target contract only. Do not call it as if it were live."],
        ["New contract field", "Documented addition to support the UI; verify deployed API version before sending it."],
    ], [1.5, 5.3])
    doc.add_heading("Important integration decisions", level=2)
    for bullet in [
        "Call APIs with the authenticated bearer token. Treat customer selection as context; customer entitlement filtering is still incomplete.",
        "Select a customer, then a configured domain, then one of that domain’s product lines. Send the canonical product_line_id explicitly; never infer it from the documents.",
        "Industry is customer metadata. The knowledge hierarchy is customer root → domain root → product line → function → subfunction; industry must not become a graph root.",
        "Store returned job_id and ingestion_batch_id. Use job_id for job details, progress, event stream, cancel/resume, and report; use batch plus domain and product line for every HITL request.",
        "Use the endpoint status labels in this guide. Planned operations/fields must not be assumed available in the deployed service.",
        "Legacy DELETE /jobs/{job_id} directly stops Ray. Present cooperative cancellation only through POST /jobs/{job_id}/cancel when confirmed available.",
    ]:
        doc.add_paragraph(bullet, style="List Bullet")

    doc.add_heading("User journey and endpoint index", level=1)
    index_rows = []
    for path, methods in SPEC["paths"].items():
        for method, op in methods.items():
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                index_rows.append([method.upper(), path, op.get("summary", ""), op.get("x-implementation-status", "Not classified")])
    table(doc, ["Method", "Path", "Purpose", "Availability"], index_rows, [.65, 2.2, 2.95, 1.0])

    doc.add_heading("Endpoint contracts", level=1)
    doc.add_paragraph("Asterisk indicates a required request field. Status codes and shapes are taken from the companion OpenAPI contract; some older routes still return legacy FastAPI detail errors.")
    groups = [
        ("1. Customer setup and ingestion configuration", ["Customer onboarding", "Ingestion Configuration"]),
        ("2. Submit and track ingestion", ["Ingestion Jobs"]),
        ("3. Review and approve knowledge", ["HITL Review"]),
        ("4. Reports", ["Reports"]),
    ]
    for group_title, tags in groups:
        doc.add_heading(group_title, level=1)
        for path, methods in SPEC["paths"].items():
            for method, op in methods.items():
                if method.lower() in {"get", "post", "put", "patch", "delete"} and any(t in (op.get("tags") or []) for t in tags):
                    add_operation(doc, path, method, op)

    doc.add_page_break()
    doc.add_heading("Shared data contract reference", level=1)
    doc.add_paragraph("The endpoint sections show the request and response models where they are used. This section provides a compact catalog for the principal UI-facing payloads.")
    important = ["CustomerOption", "IngestionContext", "SubmitJobRequest", "CurrentSubmitJobResponse", "SubmitJobResponse", "JobList", "JobDetails", "ProgressSnapshot", "HITLReviewItem", "HITLReviewList", "HITLDecisionRequest", "HITLDecisionResponse", "HITLExecutionResponse", "HITLReviewEvent", "QueueSnapshot", "ApiError", "CustomerOnboardingRequest", "CustomerOnboardingResponse"]
    for name in important:
        schema = SPEC["components"]["schemas"].get(name)
        if schema:
            add_schema_table(doc, schema, name)

    doc.add_page_break()
    doc.add_heading("Prepared UI notes", level=1)
    doc.add_paragraph(f"Included verbatim in substance from {GUIDE_PATH.relative_to(ROOT)}. The source file remains the editable detailed UI note.")
    markdown_to_doc(doc, GUIDE_PATH.read_text(encoding="utf-8"))
    doc.save(OUT_PATH)
    print(OUT_PATH)


if __name__ == "__main__":
    main()
