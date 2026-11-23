"""tia command line: analyze"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .impact import AnalysisRequest, analyze
from .report import redact, render_comment, to_json
from .db import Database
from .store import Store


def _repos(items: list[str]) -> dict:
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit(f"--repo expects NAME=PATH, got '{it}'")
        n, p = it.split("=", 1)
        out[n] = p
    return out


def _registry_args(p):
    p.add_argument("--sheet", help="worksheet of an XLSX registry (default: the first)")
    p.add_argument("--column", action="append", default=[], metavar="FIELD=HEADER",
                   help="which column holds key, service, method or path when the header is not recognised (repeatable)")
    p.add_argument("--aliases", metavar="FILE", help="CSV or XLSX with columns registry and tia: registry service name -> TIA service")
    p.add_argument("--strip-prefix", action="append", default=[], metavar="PREFIX", help="gateway prefix to remove from registry paths")


def _match_registry(args, ex: dict, rows: list) -> dict:
    from . import kflinks
    cols = {}
    for c in args.column:
        if "=" not in c:
            raise SystemExit(f"--column expects FIELD=HEADER, got '{c}'")
        f, h = c.split("=", 1)
        cols[f.strip()] = h.strip()
    try:
        entries = kflinks.parse_registry(rows, cols, tuple(args.strip_prefix))
        return kflinks.match_registry(entries, ex, kflinks.read_aliases(args.aliases) if args.aliases else None)
    except kflinks.RegistryError as e:
        raise SystemExit(f"registry: {e}")


def _emit(report: dict, fmt: str, out_path: str | None, readable):
    rep = redact(report, readable)
    text = to_json(rep) if fmt == "json" else render_comment(rep)
    if fmt == "both":
        text = render_comment(rep) + "\n\n---\n\n```json\n" + to_json(rep) + "\n```"
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tia")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="analyse a proposed change (base..head) in one repository")
    a.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    a.add_argument("--target", required=True)
    a.add_argument("--base", required=True)
    a.add_argument("--head", required=True)
    a.add_argument("--pr", type=int)
    a.add_argument("--change-record")
    a.add_argument("--db-url", default=os.environ.get("TIA_DATABASE_URL"), help="PostgreSQL DSN (runtime role); optional: without it nothing is persisted")
    a.add_argument("--tenant-id", default=os.environ.get("TIA_TENANT_ID"))
    a.add_argument("--format", choices=["json", "comment", "both"], default="comment")
    a.add_argument("--out")
    a.add_argument("--readable", help="comma-separated repos the audience may see (default: all)")
    a.add_argument("--max-depth", type=int, default=8)
    a.add_argument("--max-results", type=int, default=200)
    a.add_argument("--github-repo", help="OWNER/REPO to post the PR comment to (needs --pr and $GITHUB_TOKEN)")

    s = sub.add_parser("shared-components", help="endpoints and functions many entry points depend on, and rule gaps between the "
                                                 "consumers of a shared endpoint (no change needed)")
    s.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    s.add_argument("--min-entry-points", type=int, default=2)
    s.add_argument("--limit", type=int, default=50)

    au = sub.add_parser("automation", help="automation inventory: every endpoint's inputs, rules, valid/invalid values and test matrix; "
                                           "with --ui every page's elements, locators, mandatory flags and dropdown values")
    au.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable")
    au.add_argument("--ui", action="store_true", help="pages and UI elements instead of APIs")

    cv = sub.add_parser("coverage", help="technical coverage: the aspects each entry point and page implies and the tests that address them")
    cv.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable")
    cv.add_argument("--test-case", metavar="FILE", help="assess one test case (JSON or YAML with title/steps/expected/entry_point) instead")

    n4 = sub.add_parser("neo4j-sync", help="mirror the graph of these repositories (and their written test cases) into Neo4j; connection from "
                                           "NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD (docs/NEO4J.md)")
    n4.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    n4.add_argument("--tenant-id", default=os.environ.get("TIA_TENANT_ID") or "local", help="tenant the mirror belongs to (default: local)")
    n4.add_argument("--kf-links", metavar="CSV", help="optional ontology links: a CSV with columns tia (METHOD /route or TIA node id) and kf")
    n4.add_argument("--kf-label", help="label of the KF ontology nodes the links point to (e.g. the system-extract label), required with --kf-links")
    n4.add_argument("--kf-key", help="property that identifies those KF nodes, required with --kf-links")
    n4.add_argument("--kf-rel", default="IMPLEMENTS", help="relationship type of the links (default IMPLEMENTS)")
    n4.add_argument("--registry", metavar="FILE", help="API registry (CSV or XLSX) to link to Knowledge Fabric's registry nodes; matched on "
                                                       "service, method and path (see tia kf-registry)")
    n4.add_argument("--registry-label", help="label of Knowledge Fabric's API registry nodes (required for registry links)")
    n4.add_argument("--registry-key", help="property of those nodes that holds the registry key (required for registry links)")
    n4.add_argument("--registry-path-prop", help="without --registry: read the registry from those Neo4j nodes, path in this property")
    n4.add_argument("--registry-method-prop", help="with --registry-path-prop: the property holding the HTTP method")
    n4.add_argument("--registry-service-prop", help="with --registry-path-prop: the property holding the service name")
    n4.add_argument("--confirmed-only", action="store_true", help="write only confirmed registry links, not inferred ones")
    n4.add_argument("--testcase-label", help="label of Knowledge Fabric's test-case nodes, to join them to TIA's test cases on the id")
    n4.add_argument("--testcase-key", help="property of those nodes that holds the test case id")
    _registry_args(n4)

    kc = sub.add_parser("kf-customers", help="Knowledge Fabric's customers (its CustomerRoot nodes) with tenant id, industry and domains; "
                                              "connection from NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD")
    kc.add_argument("--format", choices=["text", "json"], default="text")

    kl = sub.add_parser("kf-link", help="link these repositories' graph to a Knowledge Fabric customer (service -> System, test case -> TestCase), "
                                        "print the links and gaps (docs/KF-BRIDGE.md)")
    kl.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    kl.add_argument("--tenant-id", default=os.environ.get("TIA_TENANT_ID") or "local", help="TIA tenant the mirror belongs to (default: local)")
    kl.add_argument("--customer", required=True, help="Knowledge Fabric customer_id (see tia kf-customers)")
    kl.add_argument("--kf-tenant", help="Knowledge Fabric tenant id (default: read from the customer's CustomerRoot node)")
    kl.add_argument("--domain", action="append", default=[], help="restrict to these Knowledge Fabric domains (repeatable; default: all of the customer's)")
    kl.add_argument("--alias", action="append", default=[], metavar="SERVICE=SYSTEM", help="declare the Knowledge Fabric System a service realises (repeatable)")
    kl.add_argument("--no-name-matches", action="store_true", help="only declared and id links, no inferred exact-name links")
    kl.add_argument("--format", choices=["text", "json"], default="text")

    kr = sub.add_parser("kf-registry", help="match an API registry (CSV or XLSX) to the endpoints in the code, without writing anything: "
                                            "confirmed / inferred / ambiguous / no match per row, and the endpoints no row names")
    kr.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    kr.add_argument("--registry", required=True, metavar="FILE")
    kr.add_argument("--format", choices=["text", "json"], default="text")
    _registry_args(kr)

    fl = sub.add_parser("flows", help="execution flow of entry points (handler, code, services called, data, rules) and each service's design")
    fl.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include all repos in scope")
    fl.add_argument("--endpoint", help="one entry point, written METHOD /route")
    fl.add_argument("--format", choices=["text", "json"], default="text")

    sc = sub.add_parser("script", help="a Playwright test for a user flow from one screen to another")
    sc.add_argument("--repo", action="append", default=[], metavar="NAME=PATH", help="repeatable; include the front end and the APIs it calls")
    sc.add_argument("--app", required=True, help="the repository name of the front end")
    sc.add_argument("--from", dest="start", required=True, help="route of the first screen, e.g. /login")
    sc.add_argument("--to", dest="end", required=True, help="route of the last screen")
    sc.add_argument("--base-url", default="http://localhost:3000")
    sc.add_argument("--out", help="write the script to this file (default: print it)")

    args = ap.parse_args(argv)
    if args.cmd in ("flows", "script"):
        from .graph import build_graph
        from .impact import _extract
        ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
        g = build_graph(ex)
        if args.cmd == "flows":
            from .flows import design, flows, render
            rows = flows(g, args.endpoint)
            if args.endpoint and not rows:
                raise SystemExit(f"no analysed entry point '{args.endpoint}' (write it as METHOD /route)")
            text = "\n\n".join(render(f) for f in rows) if args.format == "text" else to_json({"flows": rows, "design": design(g)})
            sys.stdout.buffer.write((text + "\n").encode("utf-8"))
            return 0
        from .scripts import generate
        if args.app not in ex:
            raise SystemExit(f"--app {args.app} is not one of the --repo names")
        out = generate(g, ex, args.app, args.start, args.end, args.base_url)
        if out.get("error"):
            raise SystemExit(f"{out['error']}. Screens known: {', '.join(out['screens_known'])}. {out['note']}")
        if args.out:
            Path(args.out).write_text(out["script"], encoding="utf-8")
            print(f"wrote {args.out}; {len(out['unknowns'])} point(s) to review", file=sys.stderr)
        else:
            sys.stdout.buffer.write((out["script"] + "\n").encode("utf-8"))
        for u in out["unknowns"]:
            print(f"TODO {u['screen']}: {u['what']} - {u['detail']}", file=sys.stderr)
        return 0
    if args.cmd in ("kf-customers", "kf-link"):
        from . import neo4jgraph
        try:
            cfg4j = neo4jgraph.config_from_env()
            if cfg4j is None:
                raise neo4jgraph.Neo4jUnavailable("set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD (Knowledge Fabric's Neo4j)")
            drv = neo4jgraph.connect(cfg4j)
        except neo4jgraph.Neo4jUnavailable as e:
            raise SystemExit(f"Neo4j: {e}")
        try:
            if args.cmd == "kf-customers":
                rows = neo4jgraph.kf_customers(drv, cfg4j["database"])
                text = to_json({"customers": rows}) if args.format == "json" else "\n".join(
                    f"{r['customer_id']}  tenant {r['tenant_id']}  industry {r['industry_id']}  domains {', '.join(r['domains']) or '-'}" for r in rows) \
                    or "Knowledge Fabric has no customers (no CustomerRoot node)."
                sys.stdout.buffer.write((text + "\n").encode("utf-8"))
                return 0
            from .analysis_service import KfBridgeError, kf_link
            from .graph import build_graph
            from .graphstore import graph_id, graph_inputs
            from .impact import _extract
            from .kfsettings import validate as kf_validate
            aliases = {}
            for a in args.alias:
                if "=" not in a:
                    raise SystemExit(f"--alias expects SERVICE=SYSTEM, got '{a}'")
                k, v = a.split("=", 1)
                aliases.setdefault(k.strip(), []).append(v.strip())
            problems = []
            setting = kf_validate({"enabled": True, "customer_id": args.customer, **({"kf_tenant_id": args.kf_tenant} if args.kf_tenant else {}),
                                   "domains": args.domain or "auto", "system_aliases": aliases, "link_name_matches": not args.no_name_matches},
                                  problems)
            if problems:
                raise SystemExit("; ".join(problems))
            ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
            g, gid = build_graph(ex), graph_id(graph_inputs(ex))
            try:
                out = kf_link(drv, cfg4j["database"], args.tenant_id, ex, g, gid, setting)
            except KfBridgeError as e:
                raise SystemExit(f"Knowledge Fabric bridge: {e}")
        finally:
            drv.close()
        if args.format == "json":
            sys.stdout.buffer.write((to_json({k: v for k, v in out.items() if k != "planned"} | {"links": out["planned"]["links"],
                                                                                                "gaps": out["planned"]["gaps"],
                                                                                                "rule_proposals": out["planned"]["proposals"]})
                                     + "\n").encode("utf-8"))
            return 0
        sm = out["summary"]
        lines = [f"Knowledge Fabric customer {out['customer_id']} (tenant {out['kf_tenant_id']}), domains {', '.join(out['domains']) or '-'} "
                 f"({out['domain_mode']}); graph {gid}.", f"Links: {sm['realizes']} service -> System, {sm['same_as']} test case -> TestCase, "
                 f"{sm['screens']} screen -> FormCapture, {sm['data']} entity/table -> DataEntity, {sm['rules']} rule -> Rule "
                 f"({out['written']['added']} added, {out['written']['removed']} removed).", ""]
        for ln in out["planned"]["links"]:
            src = ln.get("tia_name") or ln.get("service") or ln.get("test_case")
            lines.append(f"  {ln['rel']:<9} {src} -> {ln['to_label']} '{ln['to_name']}' ({ln['domain']})  {ln['status']}"
                         + (f" [{ln['rule']}]" if ln.get("rule") else "") + f"  via {ln['source']}, id {ln['id_check']}")
        gaps = {k: v for k, v in out["planned"]["gaps"].items() if v}
        if gaps:
            lines += ["", "Gaps:"]
            for k, v in gaps.items():
                lines.append(f"  {k} ({len(v)}): " + "; ".join(str(x.get("service") or x.get("system") or x.get("id") or x.get("name") or x.get("domain"))
                                                              for x in v[:12]) + (" ..." if len(v) > 12 else ""))
        props = out["planned"]["proposals"]
        if props:
            lines += ["", f"Rule pairs to review ({len(props)}; confirm or reject them in TIA: GET /api/v1/kf/rule-proposals):"]
            for p in props[:30]:
                lines.append(f"  {p['expr']} ({p['service']}) ~ Rule '{p['kf_rule']}' ({p['domain']})  score {p['score']}"
                             + (f", shared fields {', '.join(p['shared_fields'])}" if p["shared_fields"] else ""))
        for w in out["warnings"]:
            lines.append(f"WARNING {w}")
        sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
        return 0
    if args.cmd == "kf-registry":
        from .impact import _extract
        from .kflinks import RegistryError, read_table, render
        ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
        try:
            rows = read_table(args.registry, args.sheet)
        except (OSError, RegistryError) as e:
            raise SystemExit(f"registry: {e}")
        out = _match_registry(args, ex, rows)
        sys.stdout.buffer.write(((render(out) if args.format == "text" else to_json(out)) + "\n").encode("utf-8"))
        return 0
    if args.cmd == "neo4j-sync":
        import csv
        from . import neo4jgraph
        from .graph import build_graph
        from .graphstore import graph_id, graph_inputs
        from .impact import _extract
        if args.kf_links and not (args.kf_label and args.kf_key):
            raise SystemExit("--kf-links needs --kf-label and --kf-key: TIA does not guess the Knowledge Fabric schema")
        if (args.registry or args.registry_path_prop) and not (args.registry_label and args.registry_key):
            raise SystemExit("registry links need --registry-label and --registry-key: TIA does not guess the Knowledge Fabric schema")
        if bool(args.testcase_label) != bool(args.testcase_key):
            raise SystemExit("--testcase-label and --testcase-key go together")
        try:
            cfg = neo4jgraph.config_from_env()
            if cfg is None:
                raise neo4jgraph.Neo4jUnavailable("set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD")
            drv = neo4jgraph.connect(cfg)
        except neo4jgraph.Neo4jUnavailable as e:
            raise SystemExit(f"Neo4j: {e}")
        ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
        g, gid = build_graph(ex), graph_id(graph_inputs(ex))
        try:
            out = neo4jgraph.sync(drv, neo4jgraph.project(g, args.tenant_id, gid, ex), args.tenant_id, gid, cfg["database"])
            if args.kf_links:
                with open(args.kf_links, encoding="utf-8-sig", newline="") as f:
                    rows = [{"tia": r.get("tia"), "kf": r.get("kf")} for r in csv.DictReader(f)]
                out["ontology_links"] = neo4jgraph.link_ontology(drv, args.tenant_id, rows, args.kf_label, args.kf_key, args.kf_rel, cfg["database"])
            if args.registry or args.registry_path_prop:
                if args.registry:
                    from .kflinks import RegistryError, read_table
                    try:
                        reg_rows = read_table(args.registry, args.sheet)
                    except (OSError, RegistryError) as e:
                        raise SystemExit(f"registry: {e}")
                else:
                    reg_rows = neo4jgraph.registry_from_neo4j(drv, args.registry_label, args.registry_key, args.registry_path_prop,
                                                              args.registry_method_prop, args.registry_service_prop, cfg["database"])
                matched = _match_registry(args, ex, reg_rows)
                out["registry"] = {"summary": matched["summary"], "rows": matched["rows"], "unregistered": matched["unregistered"],
                                   "links": neo4jgraph.link_registry(drv, args.tenant_id, matched, args.registry_label, args.registry_key,
                                                                     args.kf_rel, not args.confirmed_only, cfg["database"])}
            if args.testcase_label:
                out["test_case_links"] = neo4jgraph.link_test_cases(drv, args.tenant_id, args.testcase_label, args.testcase_key,
                                                                    database=cfg["database"])
        finally:
            drv.close()
        sys.stdout.buffer.write((to_json(out) + "\n").encode("utf-8"))
        return 0
    if args.cmd in ("automation", "coverage"):
        from .automation import api_inventory, summarise_api, ui_inventory
        from .coverage import assess_test_case, coverage_report
        from .graph import build_graph
        from .impact import _extract
        from .scrub import scrub_json
        from .testscope import labels_of
        ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
        if args.cmd == "automation":
            items = api_inventory(ex, labels_of(ex))
            out = {"pages": ui_inventory(ex)} if args.ui else {"endpoints": items, "summary": summarise_api(items)}
        elif args.test_case:
            import yaml
            tc = yaml.safe_load(Path(args.test_case).read_text(encoding="utf-8"))
            if not isinstance(tc, dict):
                raise SystemExit("--test-case expects one test case (an object with title/steps/expected/entry_point)")
            out = assess_test_case(ex, build_graph(ex), tc)
        else:
            out = coverage_report(ex, build_graph(ex))
        sys.stdout.buffer.write((to_json(scrub_json(out)[0]) + "\n").encode("utf-8"))
        return 0
    if args.cmd == "shared-components":
        from .graph import build_graph
        from .impact import _extract
        from .testscope import labels_of, rule_gaps, shared_components
        ex = {n: _extract(n, p, "HEAD", None, ()) for n, p in sorted(_repos(args.repo).items())}
        g = build_graph(ex)
        out = {"shared_components": shared_components(g, ex, max(args.min_entry_points, 2), args.limit),
               "rule_gaps": rule_gaps(g, ex, labels=labels_of(ex))}
        sys.stdout.buffer.write((to_json(out) + "\n").encode("utf-8"))
        return 0
    if args.cmd == "analyze":
        req = AnalysisRequest(repos=_repos(args.repo), target=args.target, base=args.base, head=args.head,
                              pull_request=args.pr, change_record=args.change_record,
                              max_depth=args.max_depth, max_results=args.max_results)
        store = Store(Database(args.db_url), args.tenant_id) if args.db_url and args.tenant_id else None
        rep = analyze(req, store)
        readable = args.readable.split(",") if args.readable else None
        _emit(rep, args.format, args.out, readable)
        if args.github_repo:
            from .github import upsert_comment
            tok = os.environ.get("GITHUB_TOKEN")
            if not (tok and args.pr):
                raise SystemExit("--github-repo needs --pr and $GITHUB_TOKEN")
            print(upsert_comment(args.github_repo, args.pr, render_comment(redact(rep, readable)), tok), file=sys.stderr)
        return 0 if rep["status"] != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
