"""Links between TIA's code graph and Knowledge Fabric's document-derived nodes (docs/NEO4J.md, docs/MEETING-2026-11-17.md).

Knowledge Fabric ingests documents that live outside the code: an API registry (often a spreadsheet), test cases, requirements. Each
becomes a node in Neo4j. This module decides, without a server, which TIA node each of them is, on shared identifiers only:

  API registry row   (service, HTTP method, path, key)  ->  the TIA endpoint with that method and normalised path
  KF test case       (its test case id)                 ->  the TIA test case with the same id   (written by neo4jgraph.link_test_cases)

Every registry row gets exactly one outcome, with the reason:
  confirmed   the row's service is an analysed service (by name or a declared alias), and exactly one of its endpoints has the row's
              method and normalised path
  inferred    exactly one endpoint matches, but on weaker ground: the service is missing or unknown and the path is unique across the
              landscape, the row gives no method, or the code's path is the end of the row's path (a gateway prefix such as /api/v1)
  ambiguous   more than one endpoint matches: nothing is linked, the candidates are listed
  no_match    no endpoint matches: the reason says what was looked for and what exists
  invalid     the row has no key or no path
Matching on wording or similarity is never done here: an `inferred` link is a lead that says why it is weaker, and a person can promote it by
adding a service alias. Endpoints in the code that no registry row names are listed too (`unregistered`).
"""
from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree

from .models import normalize_path

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ANY_METHOD = ("", "*", "ANY", "ALL")
OUTCOMES = ("confirmed", "inferred", "ambiguous", "no_match", "invalid")
# header names recognised for each field, compared with case, spaces and punctuation removed
COLUMNS = {
    "key": ("id", "key", "apiid", "registryid", "extractid", "systemextractid", "endpointid", "operationid", "kfid", "nodeid"),
    "service": ("service", "servicename", "system", "systemname", "application", "applicationname", "app", "microservice", "component"),
    "method": ("method", "httpmethod", "verb", "httpverb"),
    "path": ("path", "endpoint", "endpointpath", "route", "url", "uri", "urlpath", "resource", "resourcepath", "apipath"),
}


class RegistryError(Exception):
    """The registry file cannot be read, or its columns cannot be told."""


def _norm_name(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


# ---- reading a registry file ---------------------------------------------------------------------------------------------------------
def read_table(path: str, sheet: Optional[str] = None) -> list[dict]:
    """Rows of a CSV (comma, semicolon or tab separated) or XLSX file as dictionaries keyed by the header row."""
    p = Path(path)
    if p.stat().st_size > MAX_BYTES:
        raise RegistryError(f"{p.name} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    data = p.read_bytes()
    if data[:2] == b"PK":
        return _xlsx_rows(data, sheet)
    text = data.decode("utf-8-sig", "replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text), dialect=dialect)):
        if i >= MAX_ROWS:
            raise RegistryError(f"more than {MAX_ROWS} rows")
        rows.append({str(k or "").strip(): (v or "").strip() if isinstance(v, str) else v for k, v in r.items() if k is not None})
    return rows


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
       "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx_rows(data: bytes, sheet: Optional[str]) -> list[dict]:
    """The first worksheet (or the named one) of an .xlsx workbook, read with the standard library only."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        if sum(i.file_size for i in z.infolist()) > MAX_BYTES * 5:
            raise RegistryError("the workbook expands to more than 100 MB")
        wb = ElementTree.fromstring(z.read("xl/workbook.xml"))
        sheets = [(s.get("name"), s.get(f"{{{_NS['r']}}}id")) for s in wb.find("m:sheets", _NS)]
        if not sheets:
            raise RegistryError("the workbook has no worksheet")
        chosen = next((s for s in sheets if s[0] == sheet), None) if sheet else sheets[0]
        if chosen is None:
            raise RegistryError(f"no worksheet named {sheet!r} (there are: {', '.join(s[0] for s in sheets)})")
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next(r.get("Target") for r in rels.findall("pr:Relationship", _NS) if r.get("Id") == chosen[1])
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ElementTree.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        grid = []
        for row in ElementTree.fromstring(z.read(target)).iter(f"{{{_NS['m']}}}row"):
            cells = {}
            for c in row.findall("m:c", _NS):
                t, v = c.get("t"), c.find("m:v", _NS)
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
                elif v is None:
                    continue
                elif t == "s":
                    val = shared[int(v.text)]
                elif t == "b":
                    val = "TRUE" if v.text == "1" else "FALSE"
                else:
                    val = v.text or ""
                    if re.fullmatch(r"-?\d+\.0", val):
                        val = val[:-2]
                cells[_col_index(c.get("r") or "A")] = val.strip()
            grid.append(cells)
            if len(grid) > MAX_ROWS:
                raise RegistryError(f"more than {MAX_ROWS} rows")
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError, StopIteration, ValueError) as e:
        raise RegistryError(f"not a readable .xlsx workbook: {type(e).__name__}: {str(e)[:120]}")
    grid = [g for g in grid if any(v for v in g.values())]
    if not grid:
        return []
    width = max(max(g) for g in grid if g) + 1
    header = [grid[0].get(i, "") for i in range(width)]
    return [{header[i]: g.get(i, "") for i in range(width) if header[i]} for g in grid[1:]]


# ---- understanding the rows -----------------------------------------------------------------------------------------------------------
def detect_columns(headers: list, override: Optional[dict] = None) -> dict:
    """field -> header for key, service, method and path. `override` (field -> header) wins; the rest is recognised from COLUMNS."""
    out, override = {}, {k: v for k, v in (override or {}).items() if v}
    by_norm = {_norm_name(h): h for h in headers if h}
    for field, want in override.items():
        if field not in COLUMNS:
            raise RegistryError(f"unknown field {field!r}: use key, service, method or path")
        if want not in headers:
            raise RegistryError(f"column {want!r} is not in the registry (columns: {', '.join(h for h in headers if h)})")
        out[field] = want
    for field, names in COLUMNS.items():
        if field not in out:
            hit = next((by_norm[n] for n in names if n in by_norm and by_norm[n] not in out.values()), None)
            if hit:
                out[field] = hit
    missing = [f for f in ("key", "path") if f not in out]
    if missing:
        raise RegistryError(f"cannot tell which column holds the {' and the '.join(missing)} (columns: {', '.join(h for h in headers if h)}); "
                            "name it with --column " + " ".join(f"{m}=<header>" for m in missing))
    return out


def _split_path(raw: str) -> tuple[str, str]:
    """A registry cell -> (method found in it, path): 'GET /a/{id}', 'https://host/a/{id}?x=1' and '/a/{id}' are all accepted."""
    s = str(raw or "").strip()
    m = re.match(r"^([A-Za-z]+)\s+(\S.*)$", s)
    method = ""
    if m and m.group(1).upper() in METHODS:
        method, s = m.group(1).upper(), m.group(2).strip()
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", s)               # scheme and host of a full URL
    s = s.split("#", 1)[0]
    if s and not s.startswith("/"):
        s = "/" + s
    return method, s


def parse_registry(rows: list[dict], columns: Optional[dict] = None, strip_prefixes: tuple = ()) -> list[dict]:
    """Registry rows -> entries {row, key, service, method, path, norm_path}; `row` is the spreadsheet row number (header = 1)."""
    if not rows:
        return []
    headers = list(dict.fromkeys(h for r in rows for h in r))          # every row's headers: JSON rows may leave empty cells out
    cols = detect_columns(headers, columns)
    out = []
    for i, r in enumerate(rows, start=2):
        method_in_path, path = _split_path(r.get(cols["path"]))
        method = str(r.get(cols["method"]) or "").strip().upper() if "method" in cols else ""
        method = method or method_in_path
        out.append({"row": i, "key": str(r.get(cols["key"]) or "").strip(),
                    "service": str(r.get(cols["service"]) or "").strip() if "service" in cols else "",
                    "method": "" if method in ANY_METHOD else method, "path": path,
                    "norm_path": normalize_path(path, tuple(strip_prefixes)) if path else ""})
    return out


# ---- matching ---------------------------------------------------------------------------------------------------------------------------
def _services(extracts: dict, aliases: Optional[dict]) -> dict:
    """normalised name -> TIA service: each service and repository name, plus the declared aliases (registry name -> TIA service)."""
    idx = {}
    for repo, ex in extracts.items():
        idx[_norm_name(ex.service)] = ex.service
        idx.setdefault(_norm_name(repo), ex.service)
    known = {ex.service for ex in extracts.values()}
    for name, svc in (aliases or {}).items():
        target = idx.get(_norm_name(svc))
        if target is None:
            raise RegistryError(f"alias {name!r} -> {svc!r}: {svc!r} is not an analysed service ({', '.join(sorted(known))})")
        idx[_norm_name(name)] = target
    return idx


def _ref(e) -> str:
    return f"{e.method} {e.path}"


def _suffix(code_path: str, reg_path: str) -> bool:
    """The code's normalised path is the end of the registry's path, at a segment boundary (a gateway prefix in front of it)."""
    return code_path != "/" and reg_path.endswith(code_path) and reg_path[: -len(code_path)].startswith("/") and len(reg_path) > len(code_path)


def match_registry(entries: list[dict], extracts: dict, aliases: Optional[dict] = None) -> dict:
    """Each registry entry -> one outcome (see the module docstring). -> {"rows", "unregistered", "summary"}."""
    idx = _services(extracts, aliases)
    eps = [e for ex in extracts.values() for e in ex.endpoints if e.source == "code"]
    rows, used = [], set()
    for en in entries:
        base = {"row": en["row"], "key": en["key"], "service": en["service"], "method": en["method"] or "*", "path": en["path"]}
        if not en["key"] or not en["path"]:
            rows.append({**base, "status": "invalid", "reason": "the row has no " + ("key" if not en["key"] else "path")})
            continue
        svc = idx.get(_norm_name(en["service"])) if en["service"] else None
        scope = [e for e in eps if e.service == svc] if svc else eps
        weak = []
        if en["service"] and not svc:
            weak.append(f"service {en['service']!r} is not an analysed service or alias")
        elif not en["service"]:
            weak.append("the row names no service")
        same_path = [e for e in scope if e.norm_path == en["norm_path"]]
        strategy = "method-path"
        if not same_path:
            same_path = [e for e in scope if _suffix(e.norm_path, en["norm_path"])]
            if same_path:
                strategy = "path-suffix"
                weak.append(f"the code's path is the end of the registry path (prefix {en['norm_path'][: -len(same_path[0].norm_path)]} not in the code)")
        cands = [e for e in same_path if not en["method"] or e.method == en["method"]]
        if not en["method"] and cands:
            weak.append("the row names no HTTP method")
        if len(cands) == 1:
            e = cands[0]
            if strategy == "method-path" and not en["method"]:
                strategy = "path-any-method"
            if svc and not weak:
                status, reason = "confirmed", f"{svc} has exactly one endpoint {e.method} {e.path}"
            else:
                status = "inferred"
                reason = f"one endpoint matches ({e.service}: {e.method} {e.path}), but " + "; ".join(weak)
                if not svc:
                    strategy += "+landscape"
            used.add(e.id)
            rows.append({**base, "status": status, "strategy": strategy, "endpoint": e.id, "entry_point": _ref(e), "tia_service": e.service,
                         "reason": reason})
        elif cands:
            rows.append({**base, "status": "ambiguous", "reason": f"{len(cands)} endpoints match: nothing is linked",
                         "candidates": [{"endpoint": e.id, "entry_point": _ref(e), "service": e.service} for e in cands]})
        else:
            where = svc or "the analysed services"
            if same_path:
                reason = f"{where} has {en['norm_path']} only as " + ", ".join(sorted({e.method for e in same_path})) + f", not {en['method']}"
            elif en["service"] and not svc:
                reason = f"service {en['service']!r} is not an analysed service or alias, and no analysed endpoint has this path"
            else:
                reason = f"no endpoint {en['method'] or '*'} {en['norm_path']} in {where}"
            rows.append({**base, "status": "no_match", "reason": reason})
    unregistered = sorted(({"endpoint": e.id, "entry_point": _ref(e), "service": e.service} for e in eps if e.id not in used),
                          key=lambda x: (x["service"], x["entry_point"]))
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in OUTCOMES}
    summary.update(rows=len(rows), linkable=summary["confirmed"] + summary["inferred"], unregistered_endpoints=len(unregistered))
    return {"rows": rows, "unregistered": unregistered, "summary": summary}


def read_aliases(path: str) -> dict:
    """A CSV or XLSX with two columns, the registry's service name and the TIA service (headers: registry/name and tia/service)."""
    rows = read_table(path)
    if not rows:
        return {}
    heads = list(rows[0].keys())
    reg = next((h for h in heads if _norm_name(h) in ("registry", "registryname", "registryservice", "name", "alias")), None)
    tia = next((h for h in heads if _norm_name(h) in ("tia", "tiaservice", "service", "repository", "repo")), None)
    if not reg or not tia or reg == tia:
        raise RegistryError(f"the alias file needs a 'registry' and a 'tia' column (it has: {', '.join(heads)})")
    return {str(r[reg]).strip(): str(r[tia]).strip() for r in rows if str(r.get(reg) or "").strip() and str(r.get(tia) or "").strip()}


def render(result: dict) -> str:
    """A plain-text report of match_registry's result."""
    s = result["summary"]
    lines = [f"API registry: {s['rows']} row(s): {s['confirmed']} confirmed, {s['inferred']} inferred, {s['ambiguous']} ambiguous, "
             f"{s['no_match']} not matched, {s['invalid']} invalid. {s['unregistered_endpoints']} endpoint(s) in the code are in no row.", ""]
    for r in result["rows"]:
        target = r.get("entry_point") or "-"
        lines.append(f"row {r['row']:>4}  {r['status']:<9}  {r['key']:<16}  {r['method']:<6} {r['path']}")
        lines.append(f"            -> {target}  ({r['reason']})")
        for c in r.get("candidates", []):
            lines.append(f"               candidate: {c['service']}: {c['entry_point']}")
    if result["unregistered"]:
        lines += ["", "Endpoints in the code that no registry row names:"]
        lines += [f"  {u['service']}: {u['entry_point']}" for u in result["unregistered"]]
    return "\n".join(lines)
