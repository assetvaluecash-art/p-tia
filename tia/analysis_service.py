"""Resolve -> execute an impact analysis. Shared by the synchronous API and the queue worker.

resolve():  turns user-facing refs (branch, PR number, defaults) into a concrete, reproducible request
            (exact commit shas for the target and every other repository in scope).
execute():  runs the analysis for such a concrete request.
job_key():  idempotency key: the same concrete request is the same job.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional

from .evidence import EvidenceStore
from . import CONFIG_VERSION, EXTRACTOR_VERSION, EXTRACTOR_VERSIONS, gitutil
from .extraction import extract_commit
from .graph import Graph
from .graphstore import LANDSCAPE, get_or_build, graph_id, graph_inputs
from .relationships import UNVERIFIABLE, reconcile
from .impact import AnalysisRequest, _extract, analyze
from .repos import IngestError, RepoService
from .access import AccessStore
from .githubapp import AppError, TokenSource, from_scm
from .publish import PublishError, audit_publication, comment_scope, publish_to_github
from .report import differences
from .store import Store
from .tenantconfig import DEFAULT, TenantConfig
from .secretstore import SecretStore
from .webhooks import ScmConfig, with_stored_token, with_tenant_config
from .worker import PermanentError


def resolve(svc: RepoService, p: dict, fetch_pr: bool = True, cfg: Optional[TenantConfig] = None) -> dict:
    """p keys: target, head|pr, base, token, repos, revisions, change_record, max_depth, max_results.
    cfg: the tenant's configuration: its limits are the defaults AND the ceilings (a caller may ask for less, never more), and its prefixes,
    test-naming choice and policy become part of the concrete request, so the analysis is identified by them."""
    cfg = cfg or DEFAULT
    target = p["target"]
    svc.get(target)
    if p.get("pr") is not None:
        if not fetch_pr:
            raise IngestError("pull-request refs are fetched by the connector before queueing; pass 'head' (commit or branch)")
        head_sha = svc.fetch_pull_request(target, p["pr"], p.get("token"))
    elif p.get("head"):
        head_sha = svc.resolve(target, p["head"])
    else:
        raise IngestError("provide 'head' (branch/sha) or 'pr' (number)")
    base_sha = svc.merge_base(target, svc.resolve(target, p.get("base")), head_sha)
    registry = {r.name: r for r in svc.list()}                   # one query for every repository record, not one per repository
    scope = list(p.get("repos") or list(registry))
    if target not in scope:
        scope.append(target)
    scope = sorted(set(scope))
    others = [n for n in scope if n != target]
    if len(others) > 4:                                          # one git process per repository: resolve them side by side
        import contextvars
        from concurrent.futures import ThreadPoolExecutor
        # each task runs in a copy of THIS request's context, so the per-request tenant (which `svc` is bound to) follows it into the thread
        with ThreadPoolExecutor(max_workers=min(16, len(others))) as pool:
            futs = [pool.submit(contextvars.copy_context().run, svc.resolve, n, (p.get("revisions") or {}).get(n), registry.get(n)) for n in others]
            revs = {n: f.result() for n, f in zip(others, futs)}
    else:
        revs = {n: svc.resolve(n, (p.get("revisions") or {}).get(n), registry.get(n)) for n in others}
    return {"target": target, "base": base_sha, "head": head_sha, "scope": scope, "revisions": revs,
            "pr": p.get("pr"), "change_record": p.get("change_record"),
            "max_depth": min(int(p.get("max_depth") or cfg.max_depth), cfg.max_depth),
            "max_results": min(int(p.get("max_results") or cfg.max_results), cfg.max_results),
            "head_ref": p.get("head"), "strip_prefixes": list(cfg.strip_prefixes),
            "exclude_patterns": list(cfg.exclude_patterns), "test_naming": cfg.test_naming,
            "time_budget_s": cfg.time_budget_seconds, "config": cfg.to_dict(), "config_version": cfg.version, "config_digest": cfg.digest}


def job_key(c: dict) -> str:
    ident = {k: c[k] for k in ("target", "base", "head", "scope", "revisions", "max_depth", "max_results")}
    return hashlib.sha256(json.dumps([ident, TenantConfig.from_stored(c["config"], c["config_version"], c["config_digest"]).ident(CONFIG_VERSION)
                                      if c.get("config") else CONFIG_VERSION], sort_keys=True).encode()).hexdigest()


def group_key(c: dict) -> str:
    """Submissions for the same pull request (or branch) supersede each other: newest wins."""
    return f"pr:{c['target']}:{c['pr']}" if c.get("pr") is not None else f"head:{c['target']}:{c.get('head_ref') or c['head']}"


def _iso(epoch: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds") if epoch else None


def freshness_of(svc: RepoService, scope: list, repos: Optional[dict] = None) -> dict:
    """Scan state of each repository in scope (FR-005): what the report must say about how current its evidence is."""
    out = {}
    for n in scope:
        r = repos[n] if repos is not None and n in repos else svc.get(n)
        out[n] = {"scan_status": r.scan_status, "scan_error": r.scan_error, "last_successful_scan": _iso(r.last_successful_scan),
                  "last_analysed_revision": r.analysed_commit}
    return out


def notify_owners(store: Store, rep: dict, secrets=None, link: Optional[str] = None, actor: Optional[str] = None) -> dict:
    """FR-045: notify each owning team of a materially affected component once per analysis (a team whose last notification failed is
    tried again) and record every attempt as evidence. -> coordination status."""
    from .coordination import channel_for, message, send, teams_to_notify
    from .itsm import ItsmError, client_for
    snap = store.get_snapshot(rep.get("evidence_snapshot_id", "")) or {}
    cfg = _snapshot_config(snap) if snap else store.effective_config()
    live = store.effective_config()                      # channels are delivery settings: the current ones apply
    owners = {**(rep.get("owners") or {}), **cfg.ownership}
    es = EvidenceStore(store.db, store.tenant_id)
    ch_ = rep["change"]
    previous = [r["data"] for r in es.list("notification", repo=ch_["repository"], limit=5000) if r["data"].get("analysis_id") == rep["analysis_id"]]
    itsm, itsm_error = None, None
    for team, fs in teams_to_notify(rep, owners).items():
        if any(n.get("team") == team and n.get("status") == "sent" for n in previous):
            continue                                       # once delivered, never sent again; only failed deliveries are retried
        ch = channel_for(live.coordination, team)
        subject, text = message(rep, team, fs, link)
        if ch is None:
            outcome = {"channel": None, "status": "failed", "detail": "no notification channel is configured for this team (coordination.channels)"}
        elif ch["type"] == "servicenow_task" and itsm_error is not None:
            outcome = {"channel": ch["type"], "status": "failed", "detail": itsm_error}
        else:
            if ch["type"] == "servicenow_task" and itsm is None:
                try:
                    itsm = client_for(live.itsm, secrets)
                except ItsmError as e:
                    itsm_error = str(e)
            outcome = ({"channel": ch["type"], "status": "failed", "detail": itsm_error} if ch["type"] == "servicenow_task" and itsm_error
                       else send(ch, team, subject, text, rep, fs, secrets, itsm))
        data = {"analysis_id": rep["analysis_id"], "team": team, "components": sorted({f["component"] for f in fs}), **outcome,
                "by": actor or "tia", "at": datetime.now(timezone.utc).isoformat(timespec="microseconds")}
        es.put("notification", outcome["channel"] or "none", data, repo=ch_["repository"], revision=ch_.get("proposed_revision"), actor=actor or "tia")
        store.audit("team_notified", analysis=rep["analysis_id"], team=team, channel=outcome["channel"], status=outcome["status"])
    return coordination_status(store, rep)


def coordination_status(store: Store, rep: dict) -> dict:
    from .coordination import status
    snap = store.get_snapshot(rep.get("evidence_snapshot_id", "")) or {}
    cfg = _snapshot_config(snap) if snap else store.effective_config()
    owners = {**(rep.get("owners") or {}), **cfg.ownership}
    es = EvidenceStore(store.db, store.tenant_id)
    repo = rep["change"]["repository"]
    pick = lambda kind: [{**r["data"], "evidence_id": r["id"]} for r in es.list(kind, repo=repo, limit=5000)        # noqa: E731
                         if r["data"].get("analysis_id") == rep["analysis_id"]]
    return status(rep, owners, pick("notification"), pick("acknowledgement"))


def execute(svc: RepoService, store: Store, c: dict, secrets=None) -> dict:
    repos = {r.name: r for r in svc.list()}                     # one query, not three per repository
    paths = {n: (repos[n] if n in repos else svc.get(n)).path for n in c["scope"]}
    scope = set(c["scope"])
    stale = [r for r in store.list_relationships("stale") if r["stale_reason"] in UNVERIFIABLE
             and r["consumer_repo"] in scope and r["provider_repo"] in scope]
    cfg = TenantConfig.from_stored(c["config"], c.get("config_version", 0), c.get("config_digest", "default")) if c.get("config") else DEFAULT
    rep = analyze(AnalysisRequest(repos=paths, target=c["target"], base=c["base"], head=c["head"],
                                  revisions=c["revisions"], pull_request=c.get("pr"), change_record=c.get("change_record"),
                                  max_depth=c["max_depth"], max_results=c["max_results"], time_budget_s=c.get("time_budget_s", 60.0),
                                  strip_prefixes=tuple(c.get("strip_prefixes", ())),
                                  exclude_patterns=tuple(c.get("exclude_patterns", cfg.exclude_patterns or ())),
                                  test_naming=c.get("test_naming", True),
                                  freshness=freshness_of(svc, c["scope"], repos), stale_relationships=stale, config=cfg,
                                  evidence=EvidenceStore(store.db, store.tenant_id).for_analysis(paths, c["target"], c["head"])), store)
    if rep["status"] != "failed":
        pin_analysis(svc, rep, repos)
        if cfg.coordination.get("auto_notify") and rep.get("findings"):
            try:
                notify_owners(store, rep, secrets)
            except Exception as e:                       # noqa: BLE001 - a notification problem never fails the analysis
                store.audit("team_notification_failed", analysis=rep["analysis_id"], error=str(e)[:200])
    return rep


def pin_analysis(svc: RepoService, rep: dict, repos: Optional[dict] = None):
    """Keep every analysed commit (and the proposed head) reachable in TIA's clones."""
    commits = {r["repo"]: [r["commit"]] for r in rep["coverage"]["repositories_analysed"]}
    target = rep["change"]["repository"]
    commits.setdefault(target, []).extend([rep["change"]["base_revision"], rep["change"]["proposed_revision"]])
    for repo, shas in commits.items():
        try:
            svc.pin_commits(repo, shas, (repos or {}).get(repo))
        except IngestError:
            continue


def reproduce(svc: RepoService, store: Store, analysis_id: str) -> dict:
    """Re-run an analysis on its materialised evidence snapshot and say whether the result is identical (AC-015)."""
    orig = store.get_analysis(analysis_id)
    if not orig or orig.get("status") == "failed":
        raise IngestError("unknown analysis, or an analysis that failed has no evidence to reproduce")
    snap = store.get_snapshot(orig.get("evidence_snapshot_id", ""))
    if not snap:
        raise IngestError("this analysis has no materialised evidence snapshot")
    target = snap["target"]
    needed = {repo: [commit] for repo, commit in snap["revisions"].items()}
    needed[target] = sorted({snap["target_base"], snap["target_head"]})          # the proposed head is not in 'revisions'
    paths = {}
    for repo, commits in needed.items():
        for commit in commits:
            try:
                r = svc.get(repo)
                gitutil.resolve_rev(r.path, commit)
            except (IngestError, gitutil.GitError):
                raise IngestError(f"cannot reproduce: revision {commit[:7]} of repository '{repo}' is no longer available")
        paths[repo] = r.path
    new = analyze(AnalysisRequest(
        repos=paths, target=target, base=snap["target_base"], head=snap["target_head"],
        revisions={r: c for r, c in snap["revisions"].items() if r != target}, pull_request=orig["change"].get("pull_request"),
        change_record=orig["change"].get("change_record"), max_depth=snap["max_depth"], max_results=snap["max_results"],
        test_naming=snap["test_naming"], strip_prefixes=tuple(snap["strip_prefixes"]),
        exclude_patterns=tuple(snap.get("exclude_patterns") or ()), freshness=snap["freshness"],
        stale_relationships=snap["stale_relationships"], time_budget_s=snap.get("time_budget_s", 60.0),
        config=_snapshot_config(snap), evidence=EvidenceStore(store.db, store.tenant_id).pinned(snap.get("evidence_ids") or {})),
        store, persist=False)
    diff = differences(orig, new)
    versions_match = (snap["config_version"] == CONFIG_VERSION and snap["extractor_versions"] == EXTRACTOR_VERSIONS)
    store.audit("analysis_reproduced", analysis=analysis_id, snapshot=snap["id"], identical=not diff, versions_match=versions_match)
    return {"analysis_id": analysis_id, "evidence_snapshot_id": snap["id"], "identical": not diff, "differences": diff,
            "versions_match": versions_match, "report": new}


def _snapshot_config(snap: dict) -> TenantConfig:
    """The configuration an analysis ran under, as recorded in its snapshot: reproduction uses THAT, never the tenant's current one."""
    t = snap.get("tenant_config")
    return TenantConfig.from_stored(t["settings"], t["version"], t["digest"]) if t else DEFAULT


def landscape_extracts(svc: RepoService, store: Store, names: Optional[list] = None) -> dict:
    """Extracts of every registered repository at its default branch (incremental: unchanged files are not re-parsed), normalised with
    the tenant's gateway prefixes so the landscape graph and an analysis see the same paths."""
    strip = tuple(store.effective_config().strip_prefixes)
    out = {}
    for r in svc.list():
        if names and r.name not in names:
            continue
        try:
            ex = _extract(r.name, r.path, svc.resolve(r.name, None), store, strip)
        except Exception as e:           # a broken repository must not blank the whole graph
            store.audit("graph_extract_failed", repo=r.name, error=str(e)[:200])
            svc.record_scan(r.name, error=str(e)[:300])
            continue
        out[r.name] = ex
        svc.record_scan(r.name, commit=ex.commit, files_failed=len(ex.files_failed))
    return out


def refresh_landscape(svc: RepoService, store: Store) -> dict:
    """Bring the persisted landscape graph and the relationship history up to date with the repositories' current
    default branches and scan outcomes."""
    cfg = store.effective_config()
    ex = landscape_extracts(svc, store)
    repos = svc.list()
    if ex:
        g, gid, built = get_or_build(store, ex, tuple(cfg.strip_prefixes), cfg.test_naming)
        moved = store.set_head(LANDSCAPE, gid)
        if moved:
            store.audit("graph_version_advanced", graph=gid, built=built, repos={r: e.commit for r, e in sorted(ex.items())})
    else:
        g, gid, built, moved = Graph(), None, False, False
    rel = reconcile(store, g, ex, repos, gid)
    return {"graph_id": gid, "built": built, "moved": moved, "relationships": rel}


def _norm(d) -> str:
    """Canonical JSON text: database round trips turn tuples into lists, which must not count as drift."""
    return json.dumps(d, sort_keys=True, default=list)


def rescan_landscape(svc: RepoService, store: Store) -> dict:
    """Full-rescan path: re-derive every repository's facts from source, ignoring all caches, compare with what the
    incremental path had produced, repair any drift, and rebuild the graph. Drift means a cache or extractor fault."""
    repos, rescanned = [], {}
    cfg = store.effective_config()
    strip = tuple(cfg.strip_prefixes)
    for r in svc.list():
        try:
            commit = svc.resolve(r.name, None)
            cached = store.get_extract(r.name, commit, EXTRACTOR_VERSION, strip)
            incremental = cached or extract_commit(r.name, r.path, commit, store, strip)[0]
            full, stats = extract_commit(r.name, r.path, commit, store, strip, force=True)
        except Exception as e:
            repos.append({"repo": r.name, "error": str(e)[:200]})
            continue
        drift = _norm(incremental.to_dict()) != _norm(full.to_dict())
        store.put_extract(full)
        rescanned[r.name] = full
        repos.append({"repo": r.name, "commit": commit, "files": stats["files"], "parsed": stats["parsed"], "drift": drift})
    if not rescanned:
        return {"repositories": repos, "graph_id": None, "graph_drift": False}
    old_id = graph_id(graph_inputs(rescanned, strip, cfg.test_naming))
    old = store.get_graph(old_id)
    g, gid, _ = get_or_build(store, rescanned, strip, cfg.test_naming, rebuild=True)
    graph_drift = old is not None and _norm(old) != _norm(g.to_dict())
    moved = store.set_head(LANDSCAPE, gid)
    store.audit("graph_rescanned", graph=gid, drifted=[x["repo"] for x in repos if x.get("drift")], graph_drift=graph_drift,
                head_moved=moved)
    return {"repositories": repos, "graph_id": gid, "graph_drift": graph_drift, "head_moved": moved}


def pr_event_job_key(p: dict, cfg: Optional[TenantConfig] = None) -> str:
    ident = [p["provider"], p["repo"], p["pr"], p["head_sha"], p["base_ref"]]
    return hashlib.sha256(json.dumps([ident, (cfg or DEFAULT).ident(CONFIG_VERSION)]).encode()).hexdigest()


def pr_event_group_key(p: dict) -> str:
    return f"pr:{p['repo']}:{p['pr']}"          # same group as direct analyses of this PR: newest wins


def _transient(msg: str) -> bool:
    return "Cannot reach" in msg or "timed out" in msg


def make_handlers(db, allow_local: bool = False, allowed_hosts: Optional[list] = None, audit_key: Optional[bytes] = None,
                  workspace: str = ".tia-data/repos", scm: Optional[ScmConfig] = None, keywrapper=None, ado_cfg=None):
    """Job handlers for the worker. Each job runs bound to its own tenant."""
    from .jobs import JobQueue
    queue = JobQueue(db)
    base_scm = scm or ScmConfig()
    app = from_scm(base_scm)                                      # one App object: its installation tokens are cached across jobs
    if ado_cfg is None:
        from .ado import AdoConfig
        ado_cfg = AdoConfig.from_env()

    def cancelled(job) -> bool:
        state = queue.get(job.tenant_id, job.id)
        return bool(state and state["cancel_requested"])

    def github_pull_request(job):
        """A GitHub pull_request webhook: sync, fetch the PR, analyse against its real base, upsert the PR comment."""
        p = job.payload
        if cancelled(job):
            return {"skipped": "superseded by a newer push"}
        scm = with_stored_token(base_scm, SecretStore(db, job.tenant_id, keywrapper) if keywrapper else None)
        tokens = TokenSource(scm, db, app)
        if tokens.uses_app:
            try:
                scm = dataclasses.replace(scm, github_token=tokens.token_for(job.tenant_id, full_name=p["full_name"], hint=p.get("installation_id")))
            except AppError as e:
                if e.code in ("not_installed", "installation_gone", "no_access", "not_configured", "bad_key"):
                    raise PermanentError(str(e))
                raise                                              # unreachable: transient, retried
        if not scm.github_token:
            raise PermanentError("no GitHub token is configured on the worker (TIA_GITHUB_TOKEN or the tenant's stored github/token)")
        store = Store(db, job.tenant_id, audit_key)
        svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
        try:
            svc.sync(p["repo"], scm.github_token)
            refresh_landscape(svc, store)
            cfg = store.effective_config()
            concrete = resolve(svc, {"target": p["repo"], "pr": p["pr"], "base": p["base_ref"], "token": scm.github_token}, cfg=cfg)
            rep = execute(svc, store, concrete, SecretStore(db, job.tenant_id, keywrapper) if keywrapper else None)
        except IngestError as e:
            refresh_landscape(svc, store)          # a failed fetch makes this repository's relationships stale
            if _transient(str(e)):
                raise
            raise PermanentError(str(e))
        if cancelled(job):
            return {"analysis_id": rep["analysis_id"], "skipped_publish": "superseded by a newer push"}
        try:
            scm_t = with_tenant_config(scm, cfg)
            res = publish_to_github(rep, target_repo=p["repo"], full_name=p["full_name"], pr=p["pr"], head_sha=p["head_sha"], scm=scm_t,
                                    comment_repos=comment_scope(AccessStore(db, job.tenant_id), svc, scm_t, p["repo"],
                                                                os.environ.get("TIA_AUTH_REQUIRED", "").lower() in ("1", "true", "yes")))
        except PublishError as e:
            if e.permanent:
                raise PermanentError(str(e))
            raise
        audit_publication(store, provider="github", full_name=p["full_name"], pr=p["pr"], analysis_id=rep["analysis_id"], result=res)
        return {"analysis_id": rep["analysis_id"], "status": rep["status"], "comment": res["comment"], "check_run": res["check_run"]}

    def impact_analysis(job):
        store = Store(db, job.tenant_id, audit_key)
        state = queue.get(job.tenant_id, job.id)
        if state and state["cancel_requested"]:
            return {"skipped": "superseded by a newer submission"}      # newest wins: do not do stale work
        svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
        try:
            rep = execute(svc, store, job.payload, SecretStore(db, job.tenant_id, keywrapper) if keywrapper else None)
        except IngestError as e:
            raise PermanentError(str(e))
        return {"analysis_id": rep["analysis_id"], "status": rep["status"]}

    def ado_pull_request(job):
        """An Azure DevOps pull-request service hook: sync, fetch the PR, analyse against its target branch, upsert the thread and status."""
        from . import ado
        from .publish import audience_view
        p = job.payload
        if cancelled(job):
            return {"skipped": "superseded by a newer push"}
        token = ado_cfg.token or (SecretStore(db, job.tenant_id, keywrapper).get_text("ado/token") if keywrapper else None)
        if not token:
            raise PermanentError("no Azure DevOps token is configured on the worker (TIA_ADO_TOKEN or the tenant's stored ado/token)")
        if not ado.allowed_host(ado_cfg, p["ado"]["base_url"]):
            raise PermanentError("the organisation is not on an Azure DevOps host this server will call")
        store = Store(db, job.tenant_id, audit_key)
        svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
        try:
            svc.sync(p["repo"], token)
            merged = svc.fetch_pull_request(p["repo"], p["pr"], token)
            head = p.get("head_sha") or merged
            try:
                gitutil.resolve_rev(svc.get(p["repo"]).path, head)            # the source commit itself, when it was fetched with the PR
            except gitutil.GitError:
                head = merged
            refresh_landscape(svc, store)
            cfg = store.effective_config()
            concrete = resolve(svc, {"target": p["repo"], "head": head, "base": p["base_ref"]}, cfg=cfg)
            concrete["pr"] = p["pr"]
            rep = execute(svc, store, concrete, SecretStore(db, job.tenant_id, keywrapper) if keywrapper else None)
        except IngestError as e:
            refresh_landscape(svc, store)
            if _transient(str(e)):
                raise
            raise PermanentError(str(e))
        if cancelled(job):
            return {"analysis_id": rep["analysis_id"], "skipped_publish": "superseded by a newer push"}
        scm_t = with_tenant_config(base_scm, store.effective_config())
        view = audience_view(rep, p["repo"], scm_t, [p["repo"]] if os.environ.get("TIA_AUTH_REQUIRED", "").lower() in ("1", "true", "yes") else None)
        try:
            res = ado.publish(ado_cfg, token, rep, view, {**p["ado"], "pr": p["pr"]}, scm_t.public_url)
        except ado.AdoError as e:
            if e.permanent:
                raise PermanentError(str(e))
            raise
        audit_publication(store, provider="ado", full_name=f"{p['ado']['project']}/{p['ado']['repository']}", pr=p["pr"], analysis_id=rep["analysis_id"],
                          result={"comment": res["comment"], "check_run": res["status"]})
        return {"analysis_id": rep["analysis_id"], "status": rep["status"], "comment": res["comment"], "pr_status": res["status"]}

    def scm_pull_request(job):
        """A GitLab merge request or Bitbucket pull request event: sync, fetch it, analyse against its target branch, upsert comment and status."""
        from . import scmhosts
        from .publish import audience_view
        p = job.payload
        if cancelled(job):
            return {"skipped": "superseded by a newer push"}
        hcfg = scmhosts.HostConfig.from_env(p["provider"])
        secrets = SecretStore(db, job.tenant_id, keywrapper) if keywrapper else None
        token = hcfg.token or (secrets.get_text(f"{p['provider']}/token") if secrets else None)
        if not token:
            raise PermanentError(f"no {p['provider']} token is configured on the worker (TIA_{p['provider'].upper()}_TOKEN or the tenant's stored {p['provider']}/token)")
        store = Store(db, job.tenant_id, audit_key)
        svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
        try:
            svc.sync(p["repo"], token)
            head = p.get("head_sha")
            try:
                merged = svc.fetch_pull_request(p["repo"], p["pr"], token)
            except IngestError:
                merged = None                                      # Bitbucket Cloud has no pull-request refs: the source branch was synced
            try:
                gitutil.resolve_rev(svc.get(p["repo"]).path, head or "")
            except gitutil.GitError:
                head = merged or svc.resolve(p["repo"], p["host"].get("source_ref"))
            refresh_landscape(svc, store)
            cfg = store.effective_config()
            concrete = resolve(svc, {"target": p["repo"], "head": head, "base": p["base_ref"]}, cfg=cfg)
            concrete["pr"] = p["pr"]
            rep = execute(svc, store, concrete, secrets)
        except IngestError as e:
            refresh_landscape(svc, store)
            if _transient(str(e)):
                raise
            raise PermanentError(str(e))
        if cancelled(job):
            return {"analysis_id": rep["analysis_id"], "skipped_publish": "superseded by a newer push"}
        scm_t = with_tenant_config(base_scm, store.effective_config())
        view = audience_view(rep, p["repo"], scm_t, [p["repo"]] if os.environ.get("TIA_AUTH_REQUIRED", "").lower() in ("1", "true", "yes") else None)
        try:
            res = scmhosts.publish(hcfg, token, rep, view, p["host"]["full_name"], p["pr"], head, scm_t.public_url, p["host"].get("project_id"))
        except scmhosts.ScmHostError as e:
            if e.permanent:
                raise PermanentError(str(e))
            raise
        audit_publication(store, provider=p["provider"], full_name=p["host"]["full_name"], pr=p["pr"], analysis_id=rep["analysis_id"], result=res)
        return {"analysis_id": rep["analysis_id"], "status": rep["status"], "comment": res["comment"], "commit_status": res["status"]}

    def _kf_driver():
        from . import neo4jgraph
        cfg4j = neo4jgraph.config_from_env()
        if cfg4j is None:
            raise PermanentError("Neo4j is not configured on the worker (NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD): the Knowledge Fabric bridge cannot run")
        try:
            return neo4jgraph.connect(cfg4j), cfg4j["database"]
        except neo4jgraph.Neo4jUnavailable as e:
            raise RuntimeError(str(e))                            # unreachable: retried with backoff

    def kf_bridge(job):
        """Link the tenant's software graph to its Knowledge Fabric customer and capture the business context (docs/KF-BRIDGE-PLAN.md)."""
        if cancelled(job):
            return {"skipped": "superseded by a newer run"}
        store = Store(db, job.tenant_id, audit_key)
        svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
        drv, database = _kf_driver()
        try:
            out = kf_bridge_run(store, svc, drv, database, reason=(job.payload or {}).get("reason", "job"))
        except KfBridgeError as e:
            store.audit("kf_bridge_failed", reason=str(e)[:300])
            raise PermanentError(str(e))
        finally:
            drv.close()
        kf_next_poll(queue, store)
        return {"links": out["links_total"], "added": out["written"]["added"], "removed": out["written"]["removed"], "gaps": out["summary"]["gaps"]}

    def kf_poll(job):
        """Run the bridge when either graph moved: Knowledge Fabric's customer graph (fingerprint), TIA's landscape (graph head), or a
        mirror wiped by Knowledge Fabric's reset script. Then schedule the next poll (the chain ends when the setting is turned off)."""
        from . import neo4jgraph
        store = Store(db, job.tenant_id, audit_key)
        s = kf_settings(store)
        if not s["enabled"]:
            return {"skipped": "the Knowledge Fabric bridge is disabled"}
        last = kf_last_run(store) or {}
        drv, database = _kf_driver()
        try:
            head = (store.get_head(LANDSCAPE) or {}).get("graph_id")
            mirrored = neo4jgraph.mirror_version(drv, store.tenant_id, database)
            cust = neo4jgraph.kf_customer(drv, s["customer_id"], database) if s["customer_id"] else None
            fp = neo4jgraph.kf_fingerprint(drv, s["customer_id"], s["kf_tenant_id"] or (cust or {}).get("tenant_id"), database) if cust else None
            why = ("mirror missing" if mirrored is None else "TIA graph changed" if head and head != last.get("graph_version")
                   else "Knowledge Fabric changed" if fp and fp != last.get("kf_fingerprint") else None)
            if why:
                svc = RepoService(store, workspace, allow_local=allow_local, allowed_hosts=allowed_hosts)
                try:
                    out = kf_bridge_run(store, svc, drv, database, reason=f"poll: {why}")
                except KfBridgeError as e:
                    store.audit("kf_bridge_failed", reason=str(e)[:300])
                    out = {"error": str(e)}
        finally:
            drv.close()
        kf_next_poll(queue, store)
        return {"ran": bool(why), "why": why, **({"links": out.get("links_total"), "error": out.get("error")} if why else {})}

    return {"impact_analysis": impact_analysis, "github_pull_request": github_pull_request, "ado_pull_request": ado_pull_request,
            "kf_bridge": kf_bridge, "kf_poll": kf_poll,
            "scm_pull_request": scm_pull_request}


# ---- the Knowledge Fabric bridge (docs/KF-BRIDGE-PLAN.md sections 8-10) -----------------------------------------------------------------
class KfBridgeError(Exception):
    """The bridge cannot run: disabled, misconfigured, or Knowledge Fabric has no such customer. Not retried."""


KF_SOURCE = "tia-kf-bridge"


def kf_settings(store: Store) -> dict:
    from .kfsettings import canonical
    return canonical(store.effective_config().knowledge_fabric)


def kf_items(tenant: str, ex: dict, g) -> tuple[dict, list]:
    """TIA's side of the phase 7 links, from the graph: screens, ORM entities and tables (with the business names their repositories declare
    in tia-services.yaml under business_screens / business_data), and the rules found in code."""
    service_of_repo = {repo: e.service for repo, e in ex.items()}

    def declared(repo, key):
        d = (ex[repo].service_map or {}).get(key) if repo in ex else None
        if not isinstance(d, dict):
            return {}
        return {str(k): [str(x) for x in (v if isinstance(v, list) else [v]) if isinstance(x, (str, int))] for k, v in d.items()}
    items: dict = {"screen": [], "entity": [], "table": []}
    rules = []
    for nid, a in g.nodes.items():
        t, repo = a.get("type"), a.get("repo")
        base = {"key": f"{tenant}|{nid}", "id": nid, "name": a.get("name"), "repo": repo, "service": a.get("service") or service_of_repo.get(repo)}
        if t == "screen":
            items["screen"].append({**base, "route": a.get("route"), "component": a.get("component"), "declared": declared(repo, "business_screens")})
        elif t in ("entity", "table"):
            items[t].append({**base, **({"table": a.get("table")} if t == "entity" else {}), "declared": declared(repo, "business_data")})
        elif t == "rule":
            rules.append({**base, "expr": a.get("expr") or a.get("name"), "fields": list(a.get("fields") or []), "function": a.get("function")})
    return items, rules


def kf_link(driver, database: Optional[str], tenant: str, ex: dict, g, gid: str, s: dict, decisions: Optional[dict] = None) -> dict:
    """The bridge without TIA's database (also used by the command line): make sure the tenant's mirror is current (re-sync it when it is
    missing, e.g. after Knowledge Fabric's reset script), resolve the customer and domains, plan and write the links, capture the business
    context. s: the canonical knowledge_fabric setting."""
    from . import kfbridge, neo4jgraph
    if not s["customer_id"]:
        raise KfBridgeError("knowledge_fabric.customer_id is not set (pick it from GET /api/v1/kf/customers)")
    mirrored = neo4jgraph.mirror_version(driver, tenant, database)
    restored = mirrored is None
    if mirrored != gid:
        neo4jgraph.sync(driver, neo4jgraph.project(g, tenant, gid, ex), tenant, gid, database)
    cust = neo4jgraph.kf_customer(driver, s["customer_id"], database)
    if cust is None:
        raise KfBridgeError(f"Knowledge Fabric has no customer {s['customer_id']!r} (no CustomerRoot node): choose one from GET /api/v1/kf/customers")
    if cust.get("ambiguous_tenant") and not s["kf_tenant_id"]:
        raise KfBridgeError(f"customer {s['customer_id']!r} has several tenant ids in Knowledge Fabric ({', '.join(cust['tenant_ids'])}): set "
                            "knowledge_fabric.kf_tenant_id")
    kf_tenant = s["kf_tenant_id"] or cust.get("tenant_id")
    warnings = []
    if s["kf_tenant_id"] and cust.get("tenant_id") and s["kf_tenant_id"] != cust["tenant_id"] and not cust.get("ambiguous_tenant"):
        warnings.append(f"knowledge_fabric.kf_tenant_id {s['kf_tenant_id']!r} differs from the customer's CustomerRoot tenant {cust['tenant_id']!r}")
    available = neo4jgraph.kf_domains(driver, s["customer_id"], kf_tenant, database)
    domains, unknown = kfbridge.resolve_domains(s["domains"], available)
    index = neo4jgraph.kf_index(driver, s["customer_id"], kf_tenant, domains, labels=("System", "TestCase", "FormCapture", "DataEntity", "Rule"),
                                database=database)

    def declared(e):
        b = (e.service_map or {}).get("business_systems")
        return [str(x) for x in b if isinstance(x, (str, int))] if isinstance(b, list) else [b] if isinstance(b, str) else []
    services = [{"key": f"{tenant}|svc:{e.service}", "service": e.service, "repo": repo, "declared": declared(e)} for repo, e in sorted(ex.items())]
    cases = [{"key": f"{tenant}|testcase:{repo}:{tc['id']}", "id": tc["id"], "repo": repo}
             for repo, e in sorted(ex.items()) for tc in e.manual_tests if tc.get("id")]
    items, rules = kf_items(tenant, ex, g)
    planned = kfbridge.plan(services, cases, index, s, kf_tenant, domains, items=items, rules=rules, decisions=decisions or {})
    planned["gaps"]["unknown_domains"] = [{"domain": d} for d in unknown]
    fp = neo4jgraph.kf_fingerprint(driver, s["customer_id"], kf_tenant, database)
    written = neo4jgraph.write_bridge(driver, tenant, s["customer_id"], kf_tenant, planned["links"], {"tia_graph_version": gid, "kf_fingerprint": fp},
                                      database)
    ctx = neo4jgraph.read_context(driver, tenant, s["customer_id"], s["max_context_items"], database)
    item_ctx = neo4jgraph.read_item_context(driver, tenant, s["customer_id"], {repo: e.service for repo, e in ex.items()}, s["max_context_items"],
                                            database)
    stories = {}
    if s["link_issue_keys"]:
        stories = neo4jgraph.read_stories(driver, tenant, s["customer_id"], kf_tenant, domains, s["max_context_items"], database=database)
    return {"customer_id": s["customer_id"], "kf_tenant_id": kf_tenant, "domains": domains, "available_domains": available,
            "domain_mode": "auto" if s["domains"] == "auto" else "restricted", "graph_version": gid, "kf_fingerprint": fp,
            "mirror_restored": restored, "mirror_synced": mirrored != gid, "planned": planned, "summary": kfbridge.summary(planned),
            "written": written, "context": ctx, "items": item_ctx, "stories": stories, "warnings": warnings}


def kf_bridge_run(store: Store, svc: RepoService, driver, database: Optional[str] = None, reason: str = "manual",
                  ex: Optional[dict] = None) -> dict:
    """One bridge run for a tenant (kf_link), recorded as evidence: the run (links, gaps, fingerprints) and the business context that
    analyses pin (docs/KF-BRIDGE-PLAN.md sections 8-10)."""
    import time
    from .kfontology import BRIDGE_VERSION
    t0 = time.monotonic()
    s = kf_settings(store)
    if not s["enabled"]:
        raise KfBridgeError("the Knowledge Fabric bridge is disabled for this tenant (knowledge_fabric.enabled)")
    cfg = store.effective_config()
    ex = ex if ex is not None else landscape_extracts(svc, store)
    if not ex:
        raise KfBridgeError("no repositories are registered: there is nothing to link")
    g, gid, _ = get_or_build(store, ex, tuple(cfg.strip_prefixes), cfg.test_naming)
    es = EvidenceStore(store.db, store.tenant_id)
    out = kf_link(driver, database, store.tenant_id, ex, g, gid, s, kf_decisions(store))
    if out["mirror_synced"]:
        store.audit("kf_mirror_restored" if out["mirror_restored"] else "neo4j_synced", graph=gid, reason="kf_bridge")
    now = datetime.now(timezone.utc)
    ctx_rec = es.put("kf_context", KF_SOURCE, {"customer_id": out["customer_id"], "tenant_id": out["kf_tenant_id"], "fingerprint": out["kf_fingerprint"],
                                               "pulled_at": now.isoformat(timespec="seconds"), "bridge_version": BRIDGE_VERSION,
                                               "graph_version": gid, "domains": out["domains"], "services": out["context"],
                                               "items": out["items"], "stories": out["stories"]}, observed_to=now)
    planned, written = out["planned"], out["written"]
    run = {"reason": reason, "at": now.isoformat(timespec="seconds"), "customer_id": out["customer_id"], "kf_tenant_id": out["kf_tenant_id"],
           "domains": out["domains"], "domain_mode": out["domain_mode"], "available_domains": out["available_domains"],
           "graph_version": gid, "kf_fingerprint": out["kf_fingerprint"], "bridge_version": BRIDGE_VERSION, "mirror_restored": out["mirror_restored"],
           "summary": out["summary"], "written": {k: v for k, v in written.items() if k != "not_written"}, "not_written": written["not_written"][:200],
           "links": planned["links"][:5000], "links_total": len(planned["links"]), "gaps": {k: v[:500] for k, v in planned["gaps"].items()},
           "rule_proposals": planned["proposals"][:2000],
           "context_evidence_id": ctx_rec["id"], "warnings": out["warnings"], "duration_s": round(time.monotonic() - t0, 3)}
    rec = es.put("kf_bridge_run", KF_SOURCE, run, observed_to=now)
    store.audit("kf_bridge_completed", reason=reason, links=len(planned["links"]), added=written["added"], removed=written["removed"],
                customer=out["customer_id"], graph=gid, fingerprint=out["kf_fingerprint"])
    return {**run, "run_evidence_id": rec["id"]}


def kf_decisions(store: Store) -> dict:
    """The review decisions on (code rule, Knowledge Fabric Rule) pairs: the newest per pair wins."""
    from .kfbridge import latest_decisions
    return latest_decisions(EvidenceStore(store.db, store.tenant_id).list("kf_link_decision", limit=20000))


def kf_decide(store: Store, proposal: dict, decision: str, actor: Optional[str], comment: Optional[str] = None) -> dict:
    """Record a person's decision on a proposed rule pair as evidence (kind kf_link_decision); a confirmed pair becomes an ENFORCES link on
    the next bridge run, a rejected one is not proposed again. proposal: an entry of the latest run's rule_proposals."""
    if decision not in ("confirm", "reject"):
        raise ValueError("decision must be confirm or reject")
    now = datetime.now(timezone.utc)
    data = {"proposal": proposal["id"], "tia_rule": proposal["tia_rule"], "expr": proposal.get("expr"), "service": proposal.get("service"),
            "kf_entity_id": proposal["kf_entity_id"], "kf_rule": proposal.get("kf_rule"), "domain": proposal.get("domain"),
            "decision": decision, "actor": actor, "comment": (comment or "")[:1000] or None, "at": now.isoformat(timespec="seconds")}
    rec = EvidenceStore(store.db, store.tenant_id).put("kf_link_decision", KF_SOURCE, data, repo=proposal.get("repo"), observed_to=now)
    store.audit("kf_rule_link_decided", proposal=proposal["id"], decision=decision, tia_rule=proposal["tia_rule"], kf_rule=proposal.get("kf_rule"),
                actor=actor)
    return {**data, "evidence_id": rec["id"]}


def kf_last_run(store: Store) -> Optional[dict]:
    rows = EvidenceStore(store.db, store.tenant_id).list("kf_bridge_run", limit=1)
    return {**rows[0]["data"], "run_evidence_id": rows[0]["id"]} if rows else None


def kf_enqueue(queue, tenant: str, kind: str, reason: str, delay_s: float = 0, bucket: Optional[str] = None) -> dict:
    """Queue a bridge job. kf_bridge: newest wins per tenant; kf_poll: one per time bucket, so several workers keep one chain."""
    key = f"{kind}:{tenant}:{bucket or datetime.now(timezone.utc).isoformat(timespec='microseconds')}"
    return queue.enqueue(tenant, kind, {"reason": reason}, hashlib.sha256(key.encode()).hexdigest(),
                         group_key=f"{kind}:{tenant}", delay_s=delay_s)


def kf_next_poll(queue, store: Store) -> Optional[dict]:
    s = kf_settings(store)
    if not (s["enabled"] and s["auto_relink"]):
        return None
    period = s["poll_minutes"] * 60
    bucket = str(int(datetime.now(timezone.utc).timestamp() // period) + 1)
    return kf_enqueue(queue, store.tenant_id, "kf_poll", "poll", delay_s=period, bucket=bucket)
