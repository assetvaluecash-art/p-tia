-- Issue keys (PAY-4521) named by a change (commit messages, pull request title, branch name) are recorded in every report as
-- change.issue_keys, whether or not the Knowledge Fabric bridge is on. This index lets POST /query/tia find the analyses of a story
-- (selector issue_key) and lets the bridge list keys that name no story (docs/KF-BRIDGE.md sections 12 and 13).
create index analyses_issue_keys on analyses using gin ((data->'change'->'issue_keys'));
