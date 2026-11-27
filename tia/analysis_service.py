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
                                  pr_title=c.get("pr_title"), pr_branch=c.get("pr_branch"),
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
        pr_title=orig["change"].get("pull_request_title"), pr_branch=orig["change"].get("branch"),
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
            concrete.update({k: p[f] for k, f in (("pr_title", "title"), ("pr_branch", "head_ref")) if p.get(f)})
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
        """Link the tenant's software graph to its Knowledge Fabric customer and capture the business context (docs/KF-BRIDGE.md)."""
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
        inbox = kf_inbox_run(store)
        return {"links": out["links_total"], "added": out["written"]["added"], "removed": out["written"]["removed"], "gaps": out["summary"]["gaps"],
                "review": out["review_sync"], **({"inbox": inbox} if inbox else {})}

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
        inbox = kf_inbox_run(store)
        return {"ran": bool(why), "why": why, **({"links": out.get("links_total"), "error": out.get("error")} if why else {}),
                **({"inbox": inbox} if inbox else {})}

    return {"impact_analysis": impact_analysis, "github_pull_request": github_pull_request, "ado_pull_request": ado_pull_request,
            "kf_bridge": kf_bridge, "kf_poll": kf_poll,
            "scm_pull_request": scm_pull_request}


# ---- the Knowledge Fabric bridge (docs/KF-BRIDGE.md) -----------------------------------------------------------------
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


def kf_link(driver, database: Optional[str], tenant: str, ex: dict, g, gid: str, s: dict, decisions: Optional[dict] = None,
            recent_keys: Optional[dict] = None, hot: Optional[set] = None) -> dict:
    """The bridge without TIA's database (also used by the command line): make sure the tenant's mirror is current (re-sync it when it is
    missing, e.g. after Knowledge Fabric's reset script), resolve the customer, domains and product places, plan and write the links, capture
    the business context, and turn what is uncertain into review items. s: the canonical knowledge_fabric setting; decisions: the review
    decisions (kf_decisions); recent_keys: issue keys recent analyses named ({key: [{analysis_id, repo, service}]}); hot: services a recent
    change touched."""
    from . import kfbridge, kfreview, neo4jgraph
    from .kfsettings import place as normal_place
    if not s["customer_id"]:
        raise KfBridgeError("knowledge_fabric.customer_id is not set (pick it from GET /api/v1/kf/customers)")
    decisions = decisions or {}
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
    catalog = neo4jgraph.kf_catalog(driver, s["customer_id"], kf_tenant, database)

    def declared(e):
        b = (e.service_map or {}).get("business_systems")
        return [str(x) for x in b if isinstance(x, (str, int))] if isinstance(b, list) else [b] if isinstance(b, str) else []

    def declared_products(e):
        b = (e.service_map or {}).get("business_products")
        b = [b] if isinstance(b, dict) else b
        return [p for p in (normal_place(x) for x in b) if p] if isinstance(b, list) else []
    services = [{"key": f"{tenant}|svc:{e.service}", "service": e.service, "repo": repo, "declared": declared(e),
                 "declared_products": declared_products(e)} for repo, e in sorted(ex.items())]
    products, pgaps = kfbridge.resolve_products(services, s, catalog, decisions)
    mapped = sorted({p["domain"] for ps in products.values() for p in ps} & set(available))
    index = neo4jgraph.kf_index(driver, s["customer_id"], kf_tenant, sorted(set(domains) | set(mapped)),
                                labels=("System", "TestCase", "FormCapture", "DataEntity", "Rule"), database=database)
    cases = [{"key": f"{tenant}|testcase:{repo}:{tc['id']}", "id": tc["id"], "repo": repo}
             for repo, e in sorted(ex.items()) for tc in e.manual_tests if tc.get("id")]
    items, rules = kf_items(tenant, ex, g)
    planned = kfbridge.plan(services, cases, index, s, kf_tenant, domains, items=items, rules=rules, decisions=decisions, products=products)
    planned["gaps"]["unknown_domains"] = [{"domain": d} for d in sorted(set(unknown) | set(pgaps["unknown_domains"]))]
    planned["gaps"]["product_not_found"] = pgaps["product_not_found"]
    in_use = bool(products or pgaps["product_not_found"] or s["repository_products"] or s["service_products"]
                  or any(x["declared_products"] for x in services))
    planned["gaps"]["unmapped_repositories"] = pgaps["unmapped_repositories"] if in_use else []
    fp = neo4jgraph.kf_fingerprint(driver, s["customer_id"], kf_tenant, database)
    written = neo4jgraph.write_bridge(driver, tenant, s["customer_id"], kf_tenant, planned["links"], {"tia_graph_version": gid, "kf_fingerprint": fp},
                                      database)
    ctx = neo4jgraph.read_context(driver, tenant, s["customer_id"], s["max_context_items"], database)
    item_ctx = neo4jgraph.read_item_context(driver, tenant, s["customer_id"], {repo: e.service for repo, e in ex.items()}, s["max_context_items"],
                                            database)
    stories, story_gaps = {}, []
    if s["link_issue_keys"]:
        stories = neo4jgraph.read_stories(driver, tenant, s["customer_id"], kf_tenant, sorted(set(domains) | set(mapped)), s["max_context_items"],
                                          database=database)
        chosen = {t[len("issue:"):]: e for (t, e), d in decisions.items() if d.get("kind") == "story" and d.get("decision") == "choose"}
        ignored = {t[len("issue:"):] for (t, e), d in decisions.items() if d.get("kind") == "story" and d.get("decision") == "reject"}
        for key, eid in chosen.items():
            if len(stories.get(key) or []) > 1:
                stories[key] = [x for x in stories[key] if x["entity_id"] == eid] or stories[key]
        for key, refs in sorted((recent_keys or {}).items()):
            found = stories.get(key) or []
            if key in ignored or len(found) == 1:
                continue
            story_gaps.append({"key": key, "analyses": refs, "service": refs[0].get("service") if refs else None,
                               "repo": refs[0].get("repo") if refs else None,
                               "options": [{"entity_id": x["entity_id"], "name": x["name"], "domain": x["domain"]} for x in found]})
    # the places analyses roll up to: the declared ones, and for a service without one the places of its Systems (inferred)
    context_products = {svc: [{k: p.get(k) for k in ("domain", "product_line", "product_line_id", "function", "sub_function", "level", "source",
                                                      "status") if p.get(k) is not None} for p in ps] for svc, ps in products.items()}
    for sg in planned["product_suggestions"]:
        r, _ = kfbridge.resolve_place(catalog, sg["place"])
        if r is not None and sg["service"] not in products:
            context_products.setdefault(sg["service"], []).append(
                {**{k: r[k] for k in ("domain", "product_line", "product_line_id", "level") if k in r},
                 **({"function": r["function"]} if r.get("function") else {}), "source": "inherited", "status": kfbridge.NAME_MATCH,
                 "via": [v["system"] for v in sg["via"]]})

    def node_info(tid: str) -> dict:
        a = g.nodes.get(tid) or {}
        return {k: a.get(k) for k in ("type", "name", "file", "line", "route", "component", "expr")}
    review_items = kfreview.build_items({**planned, "repo_of": {e.service: r for r, e in ex.items()}}, products, story_gaps, hot or set(), node_info)
    return {"customer_id": s["customer_id"], "kf_tenant_id": kf_tenant, "domains": domains, "available_domains": available,
            "domain_mode": "auto" if s["domains"] == "auto" else "restricted", "graph_version": gid, "kf_fingerprint": fp,
            "mirror_restored": restored, "mirror_synced": mirrored != gid, "planned": planned, "summary": kfbridge.summary(planned),
            "written": written, "context": ctx, "items": item_ctx, "stories": stories, "warnings": warnings, "catalog": catalog,
            "products": context_products, "story_gaps": story_gaps, "review_items": review_items}


def kf_bridge_run(store: Store, svc: RepoService, driver, database: Optional[str] = None, reason: str = "manual",
                  ex: Optional[dict] = None) -> dict:
    """One bridge run for a tenant (kf_link), recorded as evidence: the run (links, gaps, fingerprints, the business tree) and the business
    context that analyses pin (docs/KF-BRIDGE.md); then the review queue is brought up to date."""
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
    out = kf_link(driver, database, store.tenant_id, ex, g, gid, s, kf_decisions(store),
                  recent_keys=store.recent_issue_keys() if s["link_issue_keys"] else {}, hot=store.recent_services())
    if out["mirror_synced"]:
        store.audit("kf_mirror_restored" if out["mirror_restored"] else "neo4j_synced", graph=gid, reason="kf_bridge")
    now = datetime.now(timezone.utc)
    ctx_rec = es.put("kf_context", KF_SOURCE, {"customer_id": out["customer_id"], "tenant_id": out["kf_tenant_id"], "fingerprint": out["kf_fingerprint"],
                                               "pulled_at": now.isoformat(timespec="seconds"), "bridge_version": BRIDGE_VERSION,
                                               "graph_version": gid, "domains": out["domains"], "services": out["context"],
                                               "items": out["items"], "stories": out["stories"], "products": out["products"]}, observed_to=now)
    planned, written = out["planned"], out["written"]
    items = out["review_items"]
    run = {"reason": reason, "at": now.isoformat(timespec="seconds"), "customer_id": out["customer_id"], "kf_tenant_id": out["kf_tenant_id"],
           "domains": out["domains"], "domain_mode": out["domain_mode"], "available_domains": out["available_domains"],
           "graph_version": gid, "kf_fingerprint": out["kf_fingerprint"], "bridge_version": BRIDGE_VERSION, "mirror_restored": out["mirror_restored"],
           "summary": out["summary"], "written": {k: v for k, v in written.items() if k != "not_written"}, "not_written": written["not_written"][:200],
           "links": planned["links"][:5000], "links_total": len(planned["links"]), "gaps": {k: v[:500] for k, v in planned["gaps"].items()},
           "rule_proposals": planned["proposals"][:2000], "products": out["products"], "catalog": out["catalog"],
           "service_repos": {e.service: r for r, e in sorted(ex.items())},
           "product_suggestions": planned["product_suggestions"][:2000], "link_suggestions": planned["link_suggestions"][:2000],
           "review": {"items": len(items), "by_kind": {k: sum(1 for i in items if i["kind"] == k) for k in sorted({i["kind"] for i in items})}},
           "context_evidence_id": ctx_rec["id"], "warnings": out["warnings"], "duration_s": round(time.monotonic() - t0, 3)}
    rec = es.put("kf_bridge_run", KF_SOURCE, run, observed_to=now)
    synced = kf_review_sync(store, items, rec["id"], inbox=s["review_inbox"] == "tia_and_kf")
    store.audit("kf_bridge_completed", reason=reason, links=len(planned["links"]), added=written["added"], removed=written["removed"],
                customer=out["customer_id"], graph=gid, fingerprint=out["kf_fingerprint"], review_open=synced["open"])
    return {**run, "run_evidence_id": rec["id"], "review_sync": synced}


def kf_decisions(store: Store) -> dict:
    """The review decisions, newest per pair (links, rules, products, stories); with knowledge_fabric.review_policy two_person a positive
    decision counts only once two different people made it."""
    from .kfbridge import latest_decisions
    s = kf_settings(store)
    return latest_decisions(EvidenceStore(store.db, store.tenant_id).list("kf_link_decision", limit=20000),
                            two_person=s["review_policy"] == "two_person")


def kf_decide(store: Store, proposal: dict, decision: str, actor: Optional[str], comment: Optional[str] = None) -> dict:
    """Record a person's decision on a proposed rule pair (POST /api/v1/kf/rule-proposals/{id}): the review item of the pair when the queue
    has it, else directly as evidence. A confirmed pair becomes an ENFORCES link on the next bridge run, a rejected one is not proposed again."""
    if decision not in ("confirm", "reject", "reset"):
        raise ValueError("decision must be confirm, reject or reset")
    from . import kfreview
    iid = kfreview.item_id("rule_pair", proposal["tia_rule"], [proposal["kf_entity_id"]])
    if kf_review_get(store, iid) is not None:
        out = kf_review_decide(store, iid, decision, actor, comment)
        return {**out, "proposal": proposal["id"], "tia_rule": proposal["tia_rule"], "kf_entity_id": proposal["kf_entity_id"],   # the shape this
                "kf_rule": proposal.get("kf_rule"), "decision": decision, "actor": actor,                                         # route always had
                "comment": (comment or "")[:1000] or None, "evidence_id": (out["evidence_ids"] or [None])[0]}
    now = datetime.now(timezone.utc)
    data = {"kind": "rule", "proposal": proposal["id"], "tia_id": proposal["tia_rule"], "tia_rule": proposal["tia_rule"], "expr": proposal.get("expr"),
            "service": proposal.get("service"), "kf_entity_id": proposal["kf_entity_id"], "kf_rule": proposal.get("kf_rule"),
            "kf_name": proposal.get("kf_rule"), "label": "Rule", "rel": "ENFORCES", "domain": proposal.get("domain"),
            "decision": decision, "actor": actor, "comment": (comment or "")[:1000] or None, "at": now.isoformat(timespec="seconds")}
    rec = EvidenceStore(store.db, store.tenant_id).put("kf_link_decision", KF_SOURCE, data, repo=proposal.get("repo"), observed_to=now)
    store.audit("kf_rule_link_decided", proposal=proposal["id"], decision=decision, tia_rule=proposal["tia_rule"], kf_rule=proposal.get("kf_rule"),
                actor=actor)
    return {**data, "evidence_id": rec["id"]}


# ---- the review queue (docs/KF-BRIDGE.md section 12) --------------------------------------------------------------------------------
INBOX_KINDS = ("guessed_link", "ambiguous", "suggested_link", "rule_pair", "stale_decision")   # candidates are Knowledge Fabric nodes
_ITEM_COLS = ("item_id", "kind", "status", "priority", "tia_id", "service", "repo", "domain", "product_line_id", "item", "decision",
              "first_seen_run", "last_seen_run", "first_seen_at", "last_seen_at", "deferred_at", "decided_at", "kf_review_id", "kf_sync",
              "kf_sync_error", "kf_sync_at", "updated_at")


def _row(r) -> dict:
    d = dict(zip(_ITEM_COLS, r))
    for k in ("first_seen_at", "last_seen_at", "deferred_at", "decided_at", "kf_sync_at", "updated_at"):
        d[k] = d[k].isoformat(timespec="seconds") if d[k] else None
    return d


def kf_review_sync(store: Store, items: list, run_id, inbox: bool = False) -> dict:
    """Bring the queue up to date with one bridge run: new items are opened, items asked again are refreshed (a deferred or superseded one
    opens again), open items no longer asked are superseded, decided items keep their decision. With the Knowledge Fabric inbox on, a new
    item of a mapped service whose candidates are Knowledge Fabric nodes is queued for Knowledge Fabric's review screen; a superseded one
    that is there is queued to be closed."""
    from psycopg.types.json import Jsonb
    now = datetime.now(timezone.utc)
    counts = {"new": 0, "refreshed": 0, "reopened": 0, "superseded": 0}
    with store.db.tx(store.tenant_id) as c:
        have = {r[0]: {"status": r[1], "kf_review_id": r[2]} for r in c.execute("select item_id, status, kf_review_id from kf_review_items")}
        seen = set()
        for it in items:
            iid = it["item_id"]
            seen.add(iid)
            cols = (it["kind"], it["priority"], it["tia_id"], it.get("service"), it.get("repo"), it.get("domain"), it.get("product_line_id"), Jsonb(it))
            if iid in have:
                st = have[iid]["status"]
                new = "open" if st in ("deferred", "superseded") else st
                counts["reopened" if new != st else "refreshed"] += 1
                c.execute("update kf_review_items set kind = %s, priority = %s, tia_id = %s, service = %s, repo = %s, domain = %s, "
                          "product_line_id = %s, item = %s, status = %s, last_seen_run = %s, last_seen_at = %s, updated_at = %s, "
                          "deferred_at = case when %s = 'open' then null else deferred_at end where item_id = %s",
                          (*cols, new, run_id, now, now, new, iid))
            else:
                counts["new"] += 1
                pending = inbox and it["kind"] in INBOX_KINDS and it.get("product_line_id")
                c.execute("insert into kf_review_items (tenant_id, item_id, kind, priority, tia_id, service, repo, domain, product_line_id, item, "
                          "status, first_seen_run, last_seen_run, first_seen_at, last_seen_at, updated_at, kf_sync) "
                          "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s, %s, %s)",
                          (store.tenant_id, iid, *cols, run_id, run_id, now, now, now, "pending" if pending else "none"))
        gone = [i for i, h in have.items() if i not in seen and h["status"] in ("open", "deferred", "awaiting_second")]
        for iid in gone:
            counts["superseded"] += 1
            c.execute("update kf_review_items set status = 'superseded', updated_at = %s, "
                      "kf_sync = case when kf_review_id is not null and kf_sync in ('sent', 'error') then 'closing' "
                      "when kf_sync = 'pending' then 'none' else kf_sync end where item_id = %s", (now, iid))
        n_open = c.execute("select count(*) from kf_review_items where status in ('open', 'awaiting_second')").fetchone()[0]
    return {**counts, "open": n_open}


def kf_reviews(store: Store, status: Optional[str] = "open", kind: Optional[str] = None, domain: Optional[str] = None,
               product_line: Optional[str] = None, repo: Optional[str] = None, readable: Optional[set] = None, limit: int = 100,
               offset: int = 0) -> dict:
    """The queue, filtered (status `all` for every status; product_line matches the id or the name). Items of a repository the caller
    cannot read are left out; items without a repository (an issue key, a decision whose node is gone) are shown."""
    where, args = [], []
    if status and status != "all":
        where.append("status = %s")
        args.append(status)
    for col, v in (("kind", kind), ("domain", domain), ("repo", repo)):
        if v:
            where.append(f"{col} = %s")
            args.append(v)
    if product_line:
        where.append("(product_line_id = %s or product_line_id like %s)")
        args += [product_line, f"%|PRODUCT_LINE:{product_line}"]
    sql = "select " + ", ".join(_ITEM_COLS) + " from kf_review_items" + (" where " + " and ".join(where) if where else "")
    with store.db.tx(store.tenant_id) as c:
        rows = [_row(r) for r in c.execute(sql + " order by (priority = 'high') desc, (status = 'awaiting_second') desc, kind, item_id", args)]
        counts = dict(c.execute("select status, count(*) from kf_review_items group by status").fetchall())
    if readable is not None:
        rows = [r for r in rows if not r["repo"] or r["repo"] in readable]
    return {"total": len(rows), "items": rows[offset: offset + limit], "limit": limit, "offset": offset, "by_status": counts}


def kf_review_get(store: Store, iid: str) -> Optional[dict]:
    with store.db.tx(store.tenant_id) as c:
        r = c.execute("select " + ", ".join(_ITEM_COLS) + " from kf_review_items where item_id = %s", (iid,)).fetchone()
    return _row(r) if r else None


def kf_review_history(store: Store, iid: str) -> list:
    """Every decision recorded on the item (evidence kf_link_decision), newest first."""
    return [{"evidence_id": r["id"], **r["data"]} for r in EvidenceStore(store.db, store.tenant_id).list("kf_link_decision", limit=20000)
            if (r.get("data") or {}).get("item_id") == iid]


class ReviewConflict(Exception):
    """The item cannot take this decision now: already decided (the first decision wins), or a second approval by the same person."""


def kf_review_decide(store: Store, iid: str, action: str, actor: Optional[str], comment: Optional[str] = None,
                     kf_entity_id: Optional[str] = None, place: Optional[dict] = None, source: str = "tia",
                     can_change_config: bool = True) -> dict:
    """A person's decision on one review item: recorded as evidence (kf_link_decision, append-only) and on the item; applied by the next
    bridge run (relinked when auto_relink is on). With review_policy two_person a positive decision waits for a second, different person;
    a decision that changes the tenant setting (confirm_with_alias, a new product place for a conflict) is saved as a new configuration
    version when it takes effect. source: `tia` (this API) or `kf` (Knowledge Fabric's review screen). Raises LookupError (no such item),
    ReviewConflict, kfreview.ReviewError (an action that does not fit), PermissionError (a configuration change without config_write)."""
    from psycopg.types.json import Jsonb
    from . import kfreview
    s = kf_settings(store)
    now = datetime.now(timezone.utc)
    at = now.isoformat(timespec="seconds")
    with store.db.tx(store.tenant_id) as c:
        r = c.execute("select " + ", ".join(_ITEM_COLS) + " from kf_review_items where item_id = %s for update", (iid,)).fetchone()
        if r is None:
            raise LookupError(iid)
        row = _row(r)
        if action == "reset":
            if row["status"] != "decided":
                raise ReviewConflict(f"only a decided item can be reset (this one is {row['status']})")
        elif row["status"] not in ("open", "deferred", "awaiting_second"):
            prior = row["decision"] or {}
            raise ReviewConflict(f"the item is {row['status']}" + (f": {prior.get('action')} by {prior.get('by')} at {prior.get('at')} "
                                                                    f"(in {prior.get('source', 'tia')}); the first decision wins" if prior else ""))
        item = {**row["item"], "decision": row["decision"]}
        out = kfreview.decide(item, action, kf_entity_id, place)
        if out["config"] and not can_change_config:
            raise PermissionError("this decision changes the tenant setting (an alias or a product place): it needs the config_write permission")
        positive = action in kfreview.POSITIVE and row["item"]["kind"] != "product_conflict"
        status, approvals = "decided", [actor]
        if action == "defer":
            status = "deferred"
        elif action == "reset":
            status = "open"
        elif s["review_policy"] == "two_person" and positive:
            prev = row["decision"] or {}
            same = (row["status"] == "awaiting_second" and prev.get("action") == action and prev.get("kf_entity_id") == kf_entity_id
                    and prev.get("place") == place)
            if same and actor in (prev.get("approvals") or []):
                raise ReviewConflict("the second approval must come from another person (review_policy two_person)")
            approvals = (prev.get("approvals") or []) + [actor] if same else [actor]
            status = "decided" if len(set(approvals)) >= 2 else "awaiting_second"
        decision = None if action == "reset" else {"action": action, "kf_entity_id": kf_entity_id, "place": place, "by": actor, "at": at,
                                                   "comment": (comment or "")[:1000] or None, "approvals": approvals, "source": source,
                                                   "targets": kfreview.targets_of(out["records"]) or (row["decision"] or {}).get("targets") or []}
        kf_sync = row["kf_sync"]
        if row["kf_review_id"] and status == "decided":
            kf_sync = "closed" if source == "kf" else "closing"
        c.execute("update kf_review_items set status = %s, decision = %s, decided_at = %s, deferred_at = %s, kf_sync = %s, updated_at = %s "
                  "where item_id = %s", (status, Jsonb(decision) if decision is not None else None, now if status == "decided" else None,
                                         now if status == "deferred" else None, kf_sync, now, iid))
    es = EvidenceStore(store.db, store.tenant_id)
    ids = []
    try:
        for rec in out["records"]:
            data = {**rec, "actor": actor, "comment": (comment or "")[:1000] or None, "at": at, "source": source}
            ids.append(es.put("kf_link_decision", KF_SOURCE, data, repo=row["repo"], observed_to=now)["id"])
        config_version = None
        if out["config"] and status == "decided":
            config_version = _kf_config_merge(store, out["config"], actor, f"review {iid}: {action}" + (f" ({comment})" if comment else ""))
    except Exception:
        with store.db.tx(store.tenant_id) as c:           # the decision did not take: the item goes back to where it was
            c.execute("update kf_review_items set status = %s, decision = %s, decided_at = null, kf_sync = %s, updated_at = %s where item_id = %s",
                      (row["status"], Jsonb(row["decision"]) if row["decision"] is not None else None, row["kf_sync"], now, iid))
        raise
    store.audit("kf_review_decided", item=iid, kind=row["kind"], action=action, status=status, actor=actor, source=source,
                evidence=ids, config_version=config_version)
    return {"item_id": iid, "kind": row["kind"], "action": action, "status": status, "approvals": approvals if status != "open" else [],
            "evidence_ids": ids, "config_version": config_version, "applies": "on the next bridge run" if status == "decided" else None}


def kf_inbox_push(store: Store, client, limit: int = 200) -> dict:
    """Knowledge Fabric's review screen (knowledge_fabric.review_inbox tia_and_kf; tia.kfinbox): send the new items of mapped services, and
    close the ones decided or no longer asked in TIA. A failure is kept on the item (kf_sync error) and retried on the next poll."""
    from . import kfreview
    from .kfinbox import KfInboxError
    s = kf_settings(store)
    last = kf_last_run(store) or {}
    now = datetime.now(timezone.utc)
    counts = {"created": 0, "closed": 0, "errors": 0}
    with store.db.tx(store.tenant_id) as c:
        rows = [_row(r) for r in c.execute("select " + ", ".join(_ITEM_COLS) + " from kf_review_items where kf_sync in ('pending', 'closing', 'error') "
                                           "order by updated_at limit %s", (limit,))]

    def mark(iid, sync, rid=None, err=None):
        with store.db.tx(store.tenant_id) as c:
            c.execute("update kf_review_items set kf_sync = %s, kf_review_id = coalesce(%s, kf_review_id), kf_sync_error = %s, kf_sync_at = %s "
                      "where item_id = %s", (sync, rid, err, now, iid))
    for row in rows:
        it, iid = row["item"], row["item_id"]
        scope = {"ingestion_batch_id": f"tia-links:{store.tenant_id}:{row['product_line_id']}", "domain_id": it.get("scope_domain"),
                 "product_line_id": row["product_line_id"]}
        try:
            if row["kf_review_id"] is None:
                if row["status"] not in ("open", "deferred", "awaiting_second"):
                    mark(iid, "none")
                    continue
                rid = client.create(kfreview.inbox_payload(it, store.tenant_id, s["customer_id"], last.get("kf_tenant_id"), s["review_inbox_detail"]))
                mark(iid, "sent", rid)
                counts["created"] += 1
            elif row["status"] in ("decided", "superseded"):
                d = row["decision"] or {}
                action = "WITHDRAWN" if row["status"] == "superseded" else kfreview.KF_ACTION.get(d.get("action"), "WITHDRAWN")
                client.close(row["kf_review_id"], scope, {"action": action, "selected_entity_id": d.get("kf_entity_id"),
                                                          "reviewer_id": f"tia:{d.get('by')}", "notes": d.get("comment"), "decided_at": d.get("at")})
                mark(iid, "closed")
                counts["closed"] += 1
            else:
                mark(iid, "sent")
        except KfInboxError as e:
            mark(iid, "error", err=str(e)[:300])
            counts["errors"] += 1
    if any(counts.values()):
        store.audit("kf_inbox_synced", **counts)
    return counts


def kf_inbox_run(store: Store) -> Optional[dict]:
    """The inbox step of a bridge or poll job: nothing unless review_inbox is tia_and_kf and TIA_KF_REVIEW_URL is set. Never raises."""
    from .kfinbox import KfInboxError, KfReviewClient, config_from_env
    s = kf_settings(store)
    cfg = config_from_env()
    if s["review_inbox"] != "tia_and_kf" or cfg is None:
        return None
    try:
        return kf_inbox_push(store, KfReviewClient(cfg["url"], cfg["token"], cfg["private_ok"]))
    except KfInboxError as e:
        store.audit("kf_inbox_failed", reason=str(e)[:300])
        return {"error": str(e)}


def kf_inbox_receive(store: Store, ev: dict) -> dict:
    """A decision made in Knowledge Fabric's review screen (a signed callback, tia.kfinbox.parse_callback): the item's decision, recorded
    with the actor `kf:<reviewer>`. An alias approved there is recorded as a confirmation: the alias itself changes TIA's configuration, so
    only TIA adds it. Raises LookupError (no such item for this tenant and customer), ReviewConflict, kfreview.ReviewError."""
    from . import kfreview
    s = kf_settings(store)
    if s["review_inbox"] != "tia_and_kf":
        raise ReviewConflict("this tenant does not use Knowledge Fabric's review screen (knowledge_fabric.review_inbox is tia)")
    if ev.get("customer_id") and ev["customer_id"] != s["customer_id"]:
        raise LookupError("the decision names another Knowledge Fabric customer")
    row = kf_review_get(store, ev["plan_id"])
    if row is None or (row["kf_review_id"] and row["kf_review_id"] != ev["review_id"]):
        raise LookupError(ev["plan_id"])
    action, eid = kfreview.kf_to_tia(row["item"], ev["action"], ev.get("selected_entity_id"))
    if action == "confirm_with_alias":
        action, eid = ("confirm", None) if "confirm" in row["item"]["actions"] else ("choose", eid)
    if not row["kf_review_id"]:
        with store.db.tx(store.tenant_id) as c:
            c.execute("update kf_review_items set kf_review_id = %s, kf_sync = 'sent' where item_id = %s and kf_review_id is null",
                      (ev["review_id"], row["item_id"]))
    out = kf_review_decide(store, row["item_id"], action, f"kf:{ev['reviewer_id']}", ev.get("notes"), kf_entity_id=eid, source="kf",
                           can_change_config=False)
    return {**out, "kf_review_id": ev["review_id"], "event_id": ev.get("event_id")}


def _kf_config_merge(store: Store, change: dict, actor: Optional[str], comment: str) -> Optional[int]:
    """A review decision's change to the knowledge_fabric setting (aliases added, or a service's product places replaced), saved as a new
    configuration version. -> the version, or None when nothing changed."""
    cfg = store.effective_config().to_dict()
    kf = cfg.setdefault("knowledge_fabric", {})
    for setting, entries in change.items():
        cur = dict(kf.get(setting) or {})
        for name, vals in entries.items():
            if setting.endswith("_aliases"):
                cur[name] = sorted(set(cur.get(name) or []) | set(vals))
            else:
                cur[name] = vals
        kf[setting] = cur
    r = store.put_config(cfg, comment[:500], actor)
    if r["created"]:
        store.audit("tenant_config_changed", version=r["version"], id=r["id"], actor=actor, comment=comment[:500],
                    changes=[x["path"] for x in r["changes"]])
        return r["version"]
    return None


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


# ---- the Knowledge Fabric section of the web UI (docs/plans/KF-UI-PLAN.md section 7) ------------------------------------------------
KF_JOB_KINDS = ("kf_bridge", "kf_poll")
OVERLAY_RELS = ("REALIZES", "STORES", "ENFORCES", "SAME_AS")


class VersionConflict(Exception):
    """Someone saved the configuration after the version the caller edited."""

    def __init__(self, version: int):
        super().__init__("the configuration changed since you read it: reload and apply your change again")
        self.version = version


def _when(iso: Optional[str]) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


def kf_link_repo(run: dict, ln: dict) -> Optional[str]:
    """The repository a bridge link's code side belongs to: the service's repository, or the one a test case key names."""
    tid = ln["from"].split("|", 1)[-1]
    if ln.get("service"):
        return (run.get("service_repos") or {}).get(ln["service"])
    if tid.startswith("testcase:"):
        return tid.split(":", 2)[1]
    return None


def kf_status(store: Store, queue, all_readable: bool, neo4j_configured: bool) -> dict:
    """GET /api/v1/kf/status: the state of the bridge for the status strip. state: off | never_run | running | failed | connected.
    Counts for every reader; names, warnings and the failure text only for a principal who may read every repository."""
    s = kf_settings(store)
    cfg = store.effective_config()
    last = kf_last_run(store)
    bridge = queue.latest(store.tenant_id, ("kf_bridge",))
    poll = queue.latest(store.tenant_id, ("kf_poll",))
    job = bridge["job"]
    if poll["job"] and poll["job"]["status"] == "running":
        job = poll["job"]
    failure = store.last_audit(("kf_bridge_failed",))
    last_at = _when((last or {}).get("at"))
    failed_after = failure is not None and (last_at is None or (_when(failure["at"]) or last_at) > last_at)
    if job and job["status"] == "failed" and (last_at is None or (_when(job["updated_at"]) or last_at) > last_at):
        failed_after, failure = True, failure or {"at": job["updated_at"], "reason": job["error"]}
    if not s["enabled"]:
        state = "off"
    elif job and job["status"] in ("queued", "running") and (job["kind"] == "kf_bridge" or job["status"] == "running"):
        state = "running"
    elif failed_after:
        state = "failed"
    elif last is None:
        state = "never_run"
    else:
        state = "connected"
    with store.db.tx(store.tenant_id) as c:
        review = dict(c.execute("select kind, count(*) from kf_review_items where status in ('open', 'awaiting_second') group by kind").fetchall())
    gaps = {k: len(v or []) for k, v in ((last or {}).get("gaps") or {}).items()}
    settings = s if all_readable else {k: v for k, v in s.items() if not k.endswith("_aliases") and not k.endswith("_products")}
    out = {"state": state, "enabled": s["enabled"], "neo4j_configured": neo4j_configured, "settings": settings,
           "config_version": cfg.version or 0, "to_review": {"total": sum(review.values()), "by_kind": review}, "gaps": gaps,
           "job": job and {k: job[k] for k in ("id", "kind", "status", "created_at", "updated_at")} | {"error": job["error"] if all_readable else None},
           "next_poll_at": poll["next_run_at"] if s["enabled"] and s["auto_relink"] else None, "last_run": None, "last_failure": None,
           "details_withheld": not all_readable}
    if last:
        out["last_run"] = {k: last.get(k) for k in ("at", "reason", "duration_s", "customer_id", "kf_tenant_id", "domains", "domain_mode",
                                                     "available_domains", "graph_version", "kf_fingerprint", "bridge_version", "mirror_restored",
                                                     "summary", "written", "links_total", "run_evidence_id", "review")}
        out["last_run"]["warnings"] = (last.get("warnings") or []) if all_readable else len(last.get("warnings") or [])
    if failure and failed_after:
        out["last_failure"] = {"at": failure["at"], "reason": failure.get("reason") if all_readable else None}
    return out


def kf_overlay(last: dict, readable: Optional[set]) -> dict:
    """GET /api/v1/kf/overlay: the latest run's links as the graph view draws them (tia_id = the graph's node id), only those whose code is
    in a repository the caller may read."""
    rows = []
    for ln in last.get("links") or []:
        if ln["rel"] not in OVERLAY_RELS:
            continue
        repo = kf_link_repo(last, ln)
        if readable is not None and repo not in readable:
            continue
        rows.append({"tia_id": ln["from"].split("|", 1)[-1], "rel": ln["rel"], "kf_label": ln["to_label"], "kf_name": ln.get("to_name"),
                     "entity_id": ln["to"], "domain": ln.get("domain"), "status": ln["status"], "source": ln.get("source"),
                     "strategy": ln.get("strategy"), "repo": repo})
    return {"run_evidence_id": last.get("run_evidence_id"), "at": last.get("at"), "links": rows, "links_total": len(rows)}


def kf_node(store: Store, last: dict, node_id: str, readable: Optional[set]) -> Optional[dict]:
    """GET /api/v1/kf/node: one graph node's links and the business context the run captured for it (the System entry for a service, the
    item entry for a screen, entity, table or rule). None when it has none or its repository is not readable."""
    links = [x for x in kf_overlay(last, readable)["links"] if x["tia_id"] == node_id]
    if not links:
        return None
    ctx = EvidenceStore(store.db, store.tenant_id).get_many([last["context_evidence_id"]]) if last.get("context_evidence_id") else []
    data = (ctx[0]["data"] if ctx else {}) or {}
    eids = {x["entity_id"] for x in links}
    context = []
    if node_id.startswith("svc:"):
        context = [{"kind": "service", **e} for e in (data.get("services") or {}).get(node_id[len("svc:"):]) or [] if e.get("entity_id") in eids]
    else:
        context = [e for items in (data.get("items") or {}).values() for e in items if e.get("tia_id") == node_id and e.get("entity_id") in eids]
    return {"node": node_id, "repo": links[0]["repo"], "at": last.get("at"), "links": links, "context": context,
            "context_at": data.get("pulled_at")}


def kf_settings_patch(store: Store, set_: dict, add_aliases: list, remove_aliases: list, actor: Optional[str], comment: Optional[str],
                      expected_version: Optional[int]) -> dict:
    """PATCH /api/v1/kf/settings: merge a partial change into knowledge_fabric and save it as a new configuration version.
    Raises VersionConflict (the configuration changed since expected_version), ConfigError (invalid)."""
    from .kfsettings import merge
    from .tenantconfig import ConfigError
    cfg = store.effective_config()
    if expected_version is not None and expected_version != (cfg.version or 0):
        raise VersionConflict(cfg.version or 0)
    raw = cfg.to_dict()
    new, problems = merge(raw.get("knowledge_fabric") or {}, set_, add_aliases, remove_aliases)
    if problems:
        raise ConfigError(problems)
    raw["knowledge_fabric"] = new
    r = store.put_config(raw, (comment or "Knowledge Fabric settings")[:500], actor)
    if r["created"]:
        store.audit("tenant_config_changed", version=r["version"], id=r["id"], actor=actor, comment=comment, changes=[c["path"] for c in r["changes"]])
        store.audit("kf_settings_changed", version=r["version"], actor=actor, set=sorted(set_ or {}),
                    aliases_added=len(add_aliases or []), aliases_removed=len(remove_aliases or []))
    return {"version": r["version"], "created": r["created"], "changes": r["changes"], "settings": kf_settings(store)}


def kf_unlink(store: Store, driver, database: Optional[str] = None) -> dict:
    """DELETE /api/v1/kf/links: remove this tenant's bridge edges to its Knowledge Fabric customer (turning the bridge off). Knowledge Fabric's
    own nodes and TIA's mirror stay; only the edges TIA wrote go."""
    s = kf_settings(store)
    last = kf_last_run(store) or {}
    customer = s["customer_id"] or last.get("customer_id")
    if not customer:
        raise KfBridgeError("no Knowledge Fabric customer is set and the bridge never ran: there are no links to remove")
    from . import neo4jgraph
    out = neo4jgraph.write_bridge(driver, store.tenant_id, customer, s["kf_tenant_id"] or last.get("kf_tenant_id"), [],
                                  {"tia_graph_version": None, "kf_fingerprint": None}, database)      # no links: every edge of ours goes
    store.audit("kf_links_removed", customer=customer, removed=out["removed"])
    return {"customer_id": customer, "removed": out["removed"]}


def kf_review_measures(store: Store, readable: Optional[set] = None) -> dict:
    """The review measures (docs/plans/KF-PRODUCTS-RETRIEVAL-REVIEW-PLAN.md C.9): open items by kind and age, the time to a decision, the
    share of links confirmed versus guessed in the latest run, and how often people reject links found by name (when that is high, name
    matching is better switched off for the tenant)."""
    now = datetime.now(timezone.utc)
    with store.db.tx(store.tenant_id) as c:
        rows = c.execute("select kind, status, repo, first_seen_at, decided_at, decision from kf_review_items").fetchall()
    rows = [r for r in rows if readable is None or not r[2] or r[2] in readable]
    ages = ("under_1_day", "1_to_7_days", "over_7_days")
    open_by: dict = {}
    for kind, status, _, first, _, _ in rows:
        if status in ("open", "awaiting_second", "deferred"):
            days = (now - first).total_seconds() / 86400 if first else 0
            b = open_by.setdefault(kind, dict.fromkeys(ages, 0))
            b[ages[0] if days < 1 else ages[1] if days <= 7 else ages[2]] += 1
    waits = sorted((d - f).total_seconds() / 3600 for _, s, _, f, d, _ in rows if s == "decided" and f and d)

    def pct(p):
        return round(waits[min(len(waits) - 1, int(p * len(waits)))], 2) if waits else None
    named = [(dec or {}).get("action") for kind, s, _, _, _, dec in rows if kind == "guessed_link" and s == "decided"]
    rejected, confirmed = named.count("reject"), sum(1 for a in named if a in ("confirm", "confirm_with_alias", "choose"))
    rate = round(rejected / (rejected + confirmed), 3) if rejected + confirmed else None
    last = kf_last_run(store) or {}
    by_status: dict = {}
    for ln in last.get("links") or []:
        if readable is None or kf_link_repo(last, ln) in readable:
            by_status[ln["status"]] = by_status.get(ln["status"], 0) + 1
    total = sum(by_status.values())
    guessed = by_status.get("inferred", 0)
    return {"open_by_kind_and_age": open_by, "open_total": sum(sum(v.values()) for v in open_by.values()),
            "hours_to_decision": {"decided": len(waits), "median": pct(0.5), "p90": pct(0.9)},
            "links": {"total": total, "confirmed": total - guessed, "guessed": guessed, "by_status": by_status,
                      "confirmed_share": round((total - guessed) / total, 3) if total else None},
            "name_matches": {"decided": rejected + confirmed, "rejected": rejected, "confirmed": confirmed, "rejection_rate": rate,
                             "advice": ("Most links found by name are rejected: consider switching knowledge_fabric.link_name_matches off "
                                        "for this tenant.") if rate is not None and rate >= 0.5 and rejected + confirmed >= 5 else None},
            "at": now.isoformat(timespec="seconds"), "run_at": last.get("at")}


def kf_decision_list(store: Store, readable: Optional[set], kind: Optional[str] = None, limit: int = 200) -> list:
    """GET /api/v1/kf/decisions: every recorded decision (links, rules, products, stories), newest first, with who, when and why; only those
    whose code is in a readable repository (or that name none)."""
    out = []
    for r in EvidenceStore(store.db, store.tenant_id).list("kf_link_decision", limit=20000):
        d = r.get("data") or {}
        k = d.get("kind") or ("rule" if d.get("tia_rule") else "link")
        if kind and k != kind:
            continue
        if readable is not None and r.get("repo") and r["repo"] not in readable:
            continue
        out.append({"evidence_id": r["id"], "kind": k, "decision": d.get("decision"), "tia_id": d.get("tia_id") or d.get("tia_rule"),
                    "kf_entity_id": d.get("kf_entity_id"), "kf_name": d.get("kf_name") or d.get("kf_rule"), "label": d.get("label"),
                    "rel": d.get("rel"), "domain": d.get("domain"), "place": d.get("place"), "item_id": d.get("item_id"),
                    "actor": d.get("actor"), "at": d.get("at"), "comment": d.get("comment"), "source": d.get("source", "tia"), "repo": r.get("repo")})
        if len(out) >= limit:
            break
    return out
