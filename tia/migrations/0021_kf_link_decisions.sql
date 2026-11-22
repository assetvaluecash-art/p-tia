-- Knowledge Fabric bridge, phase 7 (docs/KF-BRIDGE.md section 10): a person's decision on a proposed pair (rule found in code, Knowledge
-- Fabric Rule) is recorded evidence; confirmed pairs become ENFORCES links, rejected ones are not proposed again.
alter table evidence_records drop constraint evidence_records_kind_check;
alter table evidence_records add constraint evidence_records_kind_check check (kind in (
    'test_run', 'coverage', 'runtime_edge', 'deployment', 'incident', 'verdict',
    'approval', 'acknowledgement', 'notification', 'itsm_attachment', 'release', 'incident_link', 'build',
    'kf_context', 'kf_bridge_run', 'kf_link_decision'));
