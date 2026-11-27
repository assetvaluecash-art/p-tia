-- Review of the links between code and Knowledge Fabric (docs/KF-BRIDGE.md section 12): every bridge run turns what it is not sure of
-- (a link found by name, an ambiguous name, a suggestion, a rule pair, a product place, an issue key without a story, a decision whose node
-- disappeared) into review items. The item is the question; the answer is evidence (kind kf_link_decision, append-only), which the next
-- bridge run applies. An item's id is a hash of (kind, TIA item, candidates), so every run finds the same item again.
--   status    open | awaiting_second (two-person review) | deferred | decided | superseded (the question no longer arises)
--   item      the full question: candidates, evidence of both sides, allowed actions
--   decision  what was decided and the pairs it touched ({action, by, approvals, targets}), so a reset undoes exactly that
--   kf_*      the copy in Knowledge Fabric's review screen (review_inbox = tia_and_kf): its review id and the sync state
create table kf_review_items (
    tenant_id        uuid not null references tenants(id),
    item_id          text not null,
    kind             text not null check (kind in ('guessed_link', 'ambiguous', 'suggested_link', 'rule_pair', 'product_suggestion',
                                                   'product_conflict', 'story_not_found', 'stale_decision')),
    status           text not null default 'open' check (status in ('open', 'awaiting_second', 'deferred', 'decided', 'superseded')),
    priority         text not null default 'normal' check (priority in ('high', 'normal')),
    tia_id           text not null,
    service          text,
    repo             text,
    domain           text,
    product_line_id  text,
    item             jsonb not null,
    decision         jsonb,
    first_seen_run   bigint,
    last_seen_run    bigint,
    first_seen_at    timestamptz not null default now(),
    last_seen_at     timestamptz not null default now(),
    deferred_at      timestamptz,
    decided_at       timestamptz,
    kf_review_id     text,
    kf_sync          text not null default 'none' check (kf_sync in ('none', 'pending', 'sent', 'closing', 'closed', 'error')),
    kf_sync_error    text,
    kf_sync_at       timestamptz,
    updated_at       timestamptz not null default now(),
    primary key (tenant_id, item_id)
);
create index kf_review_by_status on kf_review_items (tenant_id, status, priority, kind);
create index kf_review_by_kf on kf_review_items (tenant_id, kf_sync) where kf_sync in ('pending', 'closing', 'error');
create unique index kf_review_by_kf_id on kf_review_items (tenant_id, kf_review_id) where kf_review_id is not null;

alter table kf_review_items enable row level security;
alter table kf_review_items force row level security;
create policy tenant_isolation on kf_review_items
    using (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid)
    with check (tenant_id = nullif(current_setting('tia.tenant_id', true), '')::uuid);

grant select, insert, update, delete on kf_review_items to tia_app;
