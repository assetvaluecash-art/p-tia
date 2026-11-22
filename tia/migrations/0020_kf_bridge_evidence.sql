-- The Knowledge Fabric bridge (docs/KF-BRIDGE-PLAN.md sections 8-10): each bridge run (links, gaps, fingerprints) and the business context it
-- captured are recorded evidence, so an analysis pins the context it used and a re-run reproduces it without calling Knowledge Fabric.
alter table evidence_records drop constraint evidence_records_kind_check;
alter table evidence_records add constraint evidence_records_kind_check check (kind in (
    'test_run', 'coverage', 'runtime_edge', 'deployment', 'incident', 'verdict',
    'approval', 'acknowledgement', 'notification', 'itsm_attachment', 'release', 'incident_link', 'build',
    'kf_context', 'kf_bridge_run'));
