-- A schema-19 store written by add-on 0.23.0's own code (git 13b7417, opsd/store.py and the modules
-- it used: approvals, items, maintenance, quiet) through its public API, then dumped with iterdump.
-- Example ids only. Frozen: the 19 -> 20 upgrade test loads it (tests/test_store_upgrade.py).
BEGIN TRANSACTION;
CREATE TABLE approvals (
        kind TEXT NOT NULL CHECK (kind IN ('approval', 'request', 'change', 'fact')),
        id TEXT NOT NULL,
        subject TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '{}',
        plan_hash TEXT NOT NULL DEFAULT '',
        requested_by TEXT NOT NULL DEFAULT '',
        run_when TEXT NOT NULL DEFAULT 'window',
        used_by INTEGER,
        detail TEXT NOT NULL DEFAULT '',
        run_id TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        decided_at TEXT,
        decided_by TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, id),
        CHECK (kind != 'approval' OR state IN ('pending', 'approved', 'rejected', 'expired', 'used', 'void')),
        CHECK (kind != 'request' OR state IN ('open', 'done', 'void', 'expired')),
        CHECK (kind != 'change' OR state IN ('refused', 'open', 'applying', 'applied', 'reverting', 'reverted',
            'failed', 'stale', 'rejected', 'expired')),
        CHECK (kind != 'fact' OR state IN ('pending', 'applied', 'rejected'))
    );
INSERT INTO "approvals" VALUES('approval','5b7fd9127a401b925dbcba826b2764be','update.example_core@2026.10.3','pending','{"entity_id": "update.example_core", "from": "2026.10.1", "kind": "core", "plan": 2, "reason": "a fragile custom integration", "tier": "approve", "title": "Home Assistant Core", "to": "2026.10.3"}','8d5fddd4674df0b14db06a98000c4111deb59db27d799303dccf43d35c8ee334','0123456789abcdef0123456789abcdef','window',NULL,'',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00',NULL,NULL,'2026-11-05T10:00:00+00:00');
INSERT INTO "approvals" VALUES('approval','ad573743edf1055fca9a0ff4420c6e34','update.example_os@16.2','approved','{"entity_id": "update.example_os", "from": "16.1", "kind": "os", "plan": 2, "reason": "a fragile custom integration", "tier": "approve", "title": "Example OS", "to": "16.2"}','7a5f22c9a41cd9ebe7695223e5e83970e4ca4a5099bfcbeee9ad5cdd3f9bedbe','0123456789abcdef0123456789abcdef','now',NULL,'',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00','2026-11-05T10:01:00+00:00','0123456789abcdef0123456789abcdef','2026-11-05T10:01:00+00:00');
INSERT INTO "approvals" VALUES('approval','94f2156e3f9d11fe566e61f42d189319','update.example_rejected@2026.10.3','rejected','{"entity_id": "update.example_rejected", "from": "2026.10.1", "kind": "addon", "plan": 2, "reason": "a fragile custom integration", "tier": "approve", "title": "Rejected", "to": "2026.10.3"}','2ac8852a9c16fc4d097ea7c0fead36b2c561d33ca096443a0a9993342bbcd3ea','0123456789abcdef0123456789abcdef','window',NULL,'rejected',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00','2026-11-05T10:00:00+00:00','0123456789abcdef0123456789abcdef','2026-11-05T10:00:00+00:00');
INSERT INTO "approvals" VALUES('approval','b62d71c089d32dd00c6f52b58f6d9fc8','update.example_addon@1.0.1','used','{"entity_id": "update.example_addon", "from": "1.0.0", "kind": "addon", "plan": 2, "reason": "a fragile custom integration", "tier": "approve", "title": "Example add-on", "to": "1.0.1"}','1d87591793c78ed4582783b5421057298922f30687e8314cf7b70948dbb47a04','0123456789abcdef0123456789abcdef','window',1,'',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00','2026-11-05T10:02:00+00:00','0123456789abcdef0123456789abcdef','2026-11-05T10:23:00+00:00');
INSERT INTO "approvals" VALUES('approval','5a976d5798245c55dd07fabf99dbd18d','update.example_expired@2026.10.3','expired','{"entity_id": "update.example_expired", "from": "2026.10.1", "kind": "hacs", "plan": 2, "reason": "a fragile custom integration", "tier": "approve", "title": "Expired", "to": "2026.10.3"}','55a20a1540434b7e3fb402a2afbe8417729cfa21c6fe14a30fe82ac238057d52','0123456789abcdef0123456789abcdef','window',NULL,'',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00','2026-11-05T10:03:00+00:00',NULL,'2026-11-05T10:03:00+00:00');
INSERT INTO "approvals" VALUES('approval','d6ff05684e79ae5105996d7ec6fb4527','update.example_void@2026.10.3','void','{"entity_id": "update.example_void", "from": "2026.10.1", "kind": "hacs", "plan": 2, "reason": "a fragile custom integration", "tier": "approve", "title": "Void", "to": "2026.10.3"}','d4a00b0a1d9b79d5e686442f3818d061653c93c199e6989d67b65f4f04c6ea4c','0123456789abcdef0123456789abcdef','window',NULL,'superseded',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00','2026-11-05T10:04:00+00:00',NULL,'2026-11-05T10:04:00+00:00');
INSERT INTO "approvals" VALUES('change','1','Example automation','open','{"variant": "change", "why": "the example light flickers", "effect": "automations/example.yaml", "files": [{"path": "automations/x.yaml", "sha256": "abababababababababababababababababababababababababababababababab"}], "summary": [{"path": "automations/x.yaml", "added": 1, "removed": 0}], "diff": "--- a\n+++ b\n", "base_sha": "1111111111111111111111111111111111111111", "commit_sha": "2222222222222222222222222222222222222222", "ref": "refs/ops/proposals/1", "fact_id": null, "applied_sha": null, "reverted_sha": null, "thread_id": null}','','agent:triage','window',NULL,'','run-triage-1','2026-11-05T10:10:00+00:00','2026-11-12T12:00:00+00:00',NULL,NULL,'2026-11-05T10:11:00+00:00');
INSERT INTO "approvals" VALUES('approval','6ccedab1043064024d272014bb1627e5','change:1:apply','approved','{"action": "apply", "binding": {"action": "apply", "base": "1111111111111111111111111111111111111111", "commit": "2222222222222222222222222222222222222222", "proposal": 1}, "kind": "change", "title": "Apply example"}','245f047037730e9db9be956ead9611988c43cca7a5525195fcdebb0193cce941','0123456789abcdef0123456789abcdef','now',NULL,'',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00','2026-11-05T10:12:00+00:00','0123456789abcdef0123456789abcdef','2026-11-05T10:12:00+00:00');
INSERT INTO "approvals" VALUES('change','2','House facts','applied','{"variant": "facts", "why": "noted", "effect": "docs/house.md", "files": [], "summary": [], "diff": "", "base_sha": null, "commit_sha": null, "ref": null, "fact_id": null, "applied_sha": null, "reverted_sha": null, "thread_id": null}','','admin','window',NULL,'',NULL,'2026-11-02T00:40:00+00:00','2026-11-02T17:20:00+00:00',NULL,NULL,'2026-11-02T00:40:00+00:00');
INSERT INTO "approvals" VALUES('fact','1','','pending','{"text": "The example freezer runs cold on purpose.", "thread_id": null, "turn_id": null, "change_id": null}','','','window',NULL,'',NULL,'2026-11-05T10:15:00+00:00',NULL,NULL,NULL,'2026-11-05T10:15:00+00:00');
INSERT INTO "approvals" VALUES('fact','2','','applied','{"text":"An applied fact.","thread_id":null,"turn_id":null,"change_id":2}','','','window',NULL,'',NULL,'2026-11-02T00:40:00+00:00',NULL,'2026-11-02T00:50:00+00:00',NULL,'2026-11-02T00:50:00+00:00');
INSERT INTO "approvals" VALUES('approval','d026d0b75f73502790a2c5a2b8948efc','dashboard:0123456789abcdef','approved','{"binding": {"dashboard": "example", "finding": "0123456789abcdef", "hash": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}, "kind": "dashboard", "title": "Migrate the example dashboard"}','be4b0e71a947fd5543a2d6f80b42d29f8b9d029136c41ba40289921ad5842126','0123456789abcdef0123456789abcdef','now',NULL,'',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00','2026-11-05T10:16:00+00:00','0123456789abcdef0123456789abcdef','2026-11-05T10:16:00+00:00');
INSERT INTO "approvals" VALUES('approval','03ab24a5372221311e0fc74e6cf3447a','dashboard:1111222233334444','pending','{"binding": {"dashboard": "example", "finding": "1111222233334444", "hash": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}, "kind": "dashboard", "title": "Other"}','2119eb9f37c751f95bb2a26692d761b53cee46fc2b3515ca3fd82816b6bfd95e','0123456789abcdef0123456789abcdef','now',NULL,'',NULL,'2026-11-05T10:00:00+00:00','2026-11-08T10:00:00+00:00',NULL,NULL,'2026-11-05T10:00:00+00:00');
INSERT INTO "approvals" VALUES('request','1','update.example_core@2026.10.3','done','{"entity_id": "update.example_core", "from_version": "2026.10.1", "to_version": "2026.10.3", "plan_run": 2, "title": "Core 2026.10.3", "thread_id": null}','','agent:chat','window',NULL,'approved in the panel','run-chat-1','2026-11-05T10:18:00+00:00','2026-11-08T12:00:00+00:00','2026-11-05T10:20:00+00:00',NULL,'2026-11-05T10:20:00+00:00');
INSERT INTO "approvals" VALUES('request','2','update.example_addon@1.0.1','open','{"entity_id": "update.example_addon", "from_version": "1.0.0", "to_version": "1.0.1", "plan_run": 2, "title": "Add-on 1.0.1", "thread_id": null}','','agent:chat','window',NULL,'','run-chat-1','2026-11-05T10:19:00+00:00','2026-11-08T12:00:00+00:00',NULL,NULL,'2026-11-05T10:19:00+00:00');
CREATE TABLE audit (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        run_id TEXT,
        principal TEXT NOT NULL,
        channel TEXT NOT NULL,
        capability TEXT NOT NULL,
        args TEXT NOT NULL DEFAULT '{}',
        tier TEXT,
        decision TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        notify_admin INTEGER NOT NULL DEFAULT 0
    );
INSERT INTO "audit" VALUES(1,'2026-11-05T08:40:00+00:00','run-chat-1','agent','opsctl','integration.reload','{"domain": "example"}','auto','execute','ok','done',0);
INSERT INTO "audit" VALUES(2,'2026-11-05T08:40:00+00:00','run-chat-1','agent','opsctl','baseline.ack','{"domain": "example"}','auto','execute','ok','done',0);
INSERT INTO "audit" VALUES(3,'2026-11-05T08:40:00+00:00','run-chat-1','agent','opsctl','notify.admin','{"domain": "example"}','auto','execute','ok','done',0);
INSERT INTO "audit" VALUES(4,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "5b7fd9127a401b925dbcba826b2764be", "subject": "update.example_core@2026.10.3"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(5,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "ad573743edf1055fca9a0ff4420c6e34", "subject": "update.example_os@16.2"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(6,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "94f2156e3f9d11fe566e61f42d189319", "subject": "update.example_rejected@2026.10.3"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(7,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.rejected','{"id": "94f2156e3f9d11fe566e61f42d189319", "subject": "update.example_rejected@2026.10.3"}','approve','rejected','rejected','',0);
INSERT INTO "audit" VALUES(8,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "b62d71c089d32dd00c6f52b58f6d9fc8", "subject": "update.example_addon@1.0.1"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(9,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "5a976d5798245c55dd07fabf99dbd18d", "subject": "update.example_expired@2026.10.3"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(10,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "d6ff05684e79ae5105996d7ec6fb4527", "subject": "update.example_void@2026.10.3"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(11,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "6ccedab1043064024d272014bb1627e5", "subject": "change:1:apply"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(12,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "d026d0b75f73502790a2c5a2b8948efc", "subject": "dashboard:0123456789abcdef"}','approve','requested','requested','',0);
INSERT INTO "audit" VALUES(13,'2026-11-05T10:00:00+00:00',NULL,'panel:0123456789abcdef0123456789abcdef','approvals','approval.requested','{"id": "03ab24a5372221311e0fc74e6cf3447a", "subject": "dashboard:1111222233334444"}','approve','requested','requested','',0);
CREATE TABLE item_events (
        id INTEGER PRIMARY KEY,
        item_id TEXT NOT NULL,
        at TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('you', 'daemon', 'agent')),
        text TEXT NOT NULL,
        revision INTEGER NOT NULL
    );
INSERT INTO "item_events" VALUES(1,'u-a4cf8519d7e0','2026-11-05T11:30:00+00:00','daemon','Home Assistant Core 2026.10.3: created',1);
INSERT INTO "item_events" VALUES(2,'w-44b578da21fd','2026-11-05T11:30:00+00:00','daemon','Update window: created',1);
INSERT INTO "item_events" VALUES(3,'p-4ce9791002d3','2026-11-05T11:30:00+00:00','daemon','Example automation: created',1);
INSERT INTO "item_events" VALUES(4,'p-03dec71c7b20','2026-11-05T11:30:00+00:00','daemon','House fact: created',1);
INSERT INTO "item_events" VALUES(5,'r-12e7ab6f51d8','2026-11-05T11:30:00+00:00','daemon','Check the example filter: created',1);
INSERT INTO "item_events" VALUES(6,'c-59923230bec5','2026-11-05T11:30:00+00:00','daemon','Example repair: created',1);
INSERT INTO "item_events" VALUES(7,'m-11e58eede06d','2026-11-05T11:30:00+00:00','daemon','Example deprecation: created',1);
INSERT INTO "item_events" VALUES(8,'q-42024584b652','2026-11-05T11:30:00+00:00','daemon','Why is the example light offline?: created',1);
INSERT INTO "item_events" VALUES(9,'a-41','2026-11-05T11:30:00+00:00','daemon','Backup created: created',1);
INSERT INTO "item_events" VALUES(10,'q-fff5a7d0491d','2026-11-05T11:30:00+00:00','daemon','Earlier conversation: created',1);
CREATE TABLE items (
        id TEXT PRIMARY KEY,
        key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('update', 'reminder', 'investigation', 'proposal', 'check', 'backup',
            'reload', 'window', 'maintenance')),
        kicker TEXT NOT NULL DEFAULT '',
        ref TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('proposed', 'waiting', 'scheduled', 'running', 'done', 'failed',
            'cancelled', 'expired')),
        gist TEXT NOT NULL DEFAULT '',
        need TEXT,
        about TEXT,
        actions TEXT NOT NULL DEFAULT '[]',
        phase TEXT,
        follows TEXT,
        facts TEXT NOT NULL DEFAULT '[]',
        agent TEXT,
        change TEXT,
        footnote TEXT,
        quiet INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 1,
        as_of TEXT NOT NULL,
        at TEXT NOT NULL,
        needs_since TEXT,
        resolved_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '{}',
        push TEXT NOT NULL DEFAULT '{}',
        needs INTEGER GENERATED ALWAYS AS (state = 'waiting' OR (state = 'failed' AND need IS NOT NULL)) VIRTUAL,
        CHECK ((state = 'waiting') = (need IS NOT NULL) OR (state = 'failed'))
    );
INSERT INTO "items" VALUES('u-a4cf8519d7e0','updates:update.example_core@2026.10.3','update','','','Home Assistant Core 2026.10.3','waiting','','{"expires_at": null, "kind": "approval", "primary": {"id": "approve", "label": "Approve"}, "secondary": [], "text": "t"}',NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00',NULL,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{"approval": "5b7fd9127a401b925dbcba826b2764be"}','{"at": "2026-11-05T11:31:00+00:00", "key": "waiting:approval:approve", "outcome": "held"}');
INSERT INTO "items" VALUES('w-44b578da21fd','window:2','window','','','Update window','running','',NULL,NULL,'[]','{"step": "installing"}',NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00',NULL,NULL,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{}','{}');
INSERT INTO "items" VALUES('p-4ce9791002d3','change:1','proposal','','','Example automation','waiting','','{"expires_at": null, "kind": "approval", "primary": {"id": "apply", "label": "Apply"}, "secondary": [], "text": "t"}',NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:40:00+00:00',NULL,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{"change": 1}','{}');
INSERT INTO "items" VALUES('p-03dec71c7b20','fact:1','proposal','','','House fact','waiting','','{"expires_at": null, "kind": "fact", "primary": {"id": "apply", "label": "Apply"}, "secondary": [], "text": "t"}',NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:40:00+00:00',NULL,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{"fact": 1}','{}');
INSERT INTO "items" VALUES('r-12e7ab6f51d8','task:1','reminder','','','Check the example filter','scheduled','',NULL,NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00',NULL,NULL,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{"task": 1}','{}');
INSERT INTO "items" VALUES('c-59923230bec5','health:repair:example','check','','','Example repair','waiting','','{"expires_at": null, "kind": "ack", "primary": {"id": "ack", "label": "Ack"}, "secondary": [], "text": "t"}',NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:50:00+00:00',NULL,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{"urgent": true}','{}');
INSERT INTO "items" VALUES('m-11e58eede06d','maint:0123456789abcdef','maintenance','','','Example deprecation','done','',NULL,NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00',NULL,'2026-11-05T11:55:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{}','{}');
INSERT INTO "items" VALUES('q-42024584b652','ask:q1example','investigation','Question','','Why is the example light offline?','done','',NULL,NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00',NULL,'2026-11-05T11:59:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{}','{}');
INSERT INTO "items" VALUES('a-41','activity:41','backup','','','Backup created','done','',NULL,NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,1,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00',NULL,'2026-11-05T11:20:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{}','{}');
INSERT INTO "items" VALUES('q-fff5a7d0491d','chat:earlier','investigation','','','Earlier conversation','done','',NULL,NULL,'[]',NULL,NULL,'[]',NULL,NULL,NULL,0,1,'2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00',NULL,'2026-10-30T06:00:00+00:00','2026-11-05T11:30:00+00:00','2026-11-05T11:30:00+00:00','{}','{}');
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
INSERT INTO "kv" VALUES('count:2026-11-05:ai_cost_musd','42100','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:cap:integration.reload','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:change_proposals','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:chat_retries','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:fact_proposals','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:notice:x','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:chat_turns','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:chat_cap_notice','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:followup_runs','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:cap:plan_fix','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:cap:approval.request','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:cap:dashboard.save','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('count:2026-11-05:cap:baseline.ack','1','2026-11-05T00:00:00+00:00');
INSERT INTO "kv" VALUES('activity.seq','41','2026-11-05T11:20:00+00:00');
INSERT INTO "kv" VALUES('chat.main_thread','eeeeeeeeeeeeeeee','2026-10-30T06:00:00+00:00');
INSERT INTO "kv" VALUES('chat.created_at','2026-10-30T06:00:00+00:00','2026-10-30T06:00:00+00:00');
INSERT INTO "kv" VALUES('chat.read','2','2026-11-05T20:37:34+00:00');
INSERT INTO "kv" VALUES('chat.read:ask:q-42024584b652','4','2026-11-05T20:37:34+00:00');
INSERT INTO "kv" VALUES('push.held:000000000001','{"held_at": "2026-11-05T11:31:00+00:00", "message": "{\"title\": \"Needs you: Core 2026.10.3\", \"body\": \"Approve?\", \"urgent\": false, \"tag\": \"u-a4cf8519d7e0\", \"thread_id\": null, \"actions\": [], \"budget_class\": \"agent\", \"url\": null, \"action_ttl\": null, \"quiet_ok\": false}", "outcome": "", "settled_at": null, "state": "held", "title": "Needs you: Core 2026.10.3"}','2026-11-05T11:31:00+00:00');
INSERT INTO "kv" VALUES('push.held:000000000002','{"held_at": "2026-11-05T11:32:00+00:00", "message": "{\"title\": \"Ops Agent: backup made\", \"body\": \"ok\", \"urgent\": false, \"tag\": \"ha-ops\", \"thread_id\": null, \"actions\": [], \"budget_class\": \"agent\", \"url\": null, \"action_ttl\": null, \"quiet_ok\": false}", "outcome": "", "settled_at": null, "state": "held", "title": "Ops Agent: backup made"}','2026-11-05T11:32:00+00:00');
INSERT INTO "kv" VALUES('push.held:000000000003','{"held_at": "2026-11-05T02:00:00+00:00", "message": "{\"title\": \"Ops Agent: backup made\", \"body\": \"ok\", \"urgent\": false, \"tag\": \"ha-ops\", \"thread_id\": null, \"actions\": [], \"budget_class\": \"agent\", \"url\": null, \"action_ttl\": null, \"quiet_ok\": false}", "outcome": "limited", "settled_at": "2026-11-05T03:40:00+00:00", "state": "undelivered", "title": "Earlier"}','2026-11-05T03:40:00+00:00');
INSERT INTO "kv" VALUES('push.action:act-example-1','{"expires_at": "2026-11-06T12:00:00+00:00", "issued_at": "2026-11-05T11:31:00+00:00", "kind": "item", "tag": "u-a4cf8519d7e0", "thread_id": null, "used_at": null}','2026-11-05T11:31:00+00:00');
INSERT INTO "kv" VALUES('push.action:act-example-2','{"expires_at": "2026-11-06T12:00:00+00:00", "issued_at": "2026-11-05T03:40:00+00:00", "kind": "reply", "tag": "ha-ops", "thread_id": null, "used_at": "2026-11-05T05:20:00+00:00"}','2026-11-05T05:20:00+00:00');
INSERT INTO "kv" VALUES('maintenance:findings','[{"key": "0123456789abcdef", "variant": "deprecation", "signal": "repair", "what": "example YAML is deprecated", "domain": "example", "removal": "2027.2", "affected": ["configuration.yaml:12"], "steps": [], "sources": [], "quote": "Example YAML is deprecated", "fixable": ["yaml"], "detail": {}, "seen_at": "2026-11-05T11:00:00+00:00", "named_removal": false}, {"key": "fedcba9876543210", "variant": "deprecation", "signal": "log", "what": "example option", "domain": "example", "removal": null, "affected": ["automations/a.yaml"], "steps": [], "sources": [], "quote": "option x is deprecated", "fixable": [], "detail": {}, "seen_at": "2026-11-05T11:00:00+00:00", "named_removal": false}, {"key": "00112233445566aa", "variant": "replacement", "signal": "replacement", "what": "example_old has a replacement", "domain": "example_old", "removal": null, "affected": [], "steps": [], "sources": [], "quote": "", "fixable": [], "detail": {"candidate": "example_new", "missing": []}, "seen_at": "2026-11-05T11:00:00+00:00", "named_removal": false}, {"key": "aabbccddeeff0011", "variant": "suggestion", "signal": "marketplace", "what": "an example marketplace card", "domain": null, "removal": null, "affected": [], "steps": [], "sources": [], "quote": "", "fixable": [], "detail": {}, "seen_at": "2026-11-05T11:00:00+00:00", "named_removal": false}]','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:meta','{"at": "2026-11-05T11:00:00+00:00", "full_at": "2026-11-05T11:00:00+00:00", "installed": "2026.10.1", "newest": "2026.10.3", "trigger": "daily", "problems": ["x"], "inventory": {"yaml": 3}, "configured": ["example"]}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:hidden:0123456789abcdef','029701644e0d7f72','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:plan:0123456789abcdef','{"evidence": "029701644e0d7f72", "plan": "move it to the UI", "at": "2026-11-05T11:00:00+00:00"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:plan:fedcba9876543210','{"evidence": "0547a5cd1c9dbb30", "plan": "rename the option", "at": "2026-11-05T11:00:00+00:00"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:hidden:00112233445566aa','3e23eb345796a0ed','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:hidden:aabbccddeeff0011','0000000000000000','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:plan:aabbccddeeff0011','{"evidence": "0000000000000000", "plan": "x"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:gone:1122334455667788','not configured here','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:fork:example','{"core": "2026.10.1"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:dashboard:0123456789abcdef','{"plan": "x"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:result:0123456789abcdef','{"ok": true}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('maintenance:migrated:0123456789abcdef','1','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('verdict:maint:0123456789abcdef','{"verdict": "affected", "fingerprint": "x"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('items.digest','{"at": "2026-11-05T11:00:00+00:00"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('item.ack:health:repair:other','{"until": "2026-11-05T22:00:00+00:00"}','2026-11-05T11:00:00+00:00');
INSERT INTO "kv" VALUES('reload.last','{"example": "2026-11-05T11:55:00+00:00"}','2026-11-05T11:55:00+00:00');
INSERT INTO "kv" VALUES('backup.last','{"slug": "abc12345"}','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('usage.unit','{"unit": "percent"}','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('updates.paused','','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('update_review:update.example_core:2026.10.3','{"ok": true}','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('update_seen:update.example_core:2026.10.3','2026-11-01T08:00:00+00:00','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('update_hold:update.example_hacs@2.1.0','{"reason": "rolled back"}','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('guard.anomaly','{"at": "2026-11-05T10:21:00+00:00"}','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('fork:proposal:fp1','{"id": "fp1", "domain": "example"}','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('evidence:cccccccccccccccccccccccccccccccc','{"at": "2026-11-05T10:21:00+00:00"}','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('coresource:github_backoff_until','2026-11-05T12:30:00+00:00','2026-11-05T10:21:00+00:00');
INSERT INTO "kv" VALUES('investigation.cancel:7','1','2026-11-05T10:21:00+00:00');
CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        profile TEXT NOT NULL,
        class TEXT NOT NULL,
        day TEXT NOT NULL,
        week TEXT NOT NULL,
        month TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        reserved_turns INTEGER NOT NULL,
        reserved_tokens INTEGER NOT NULL,
        reserved_seconds REAL NOT NULL DEFAULT 0,
        reserved_share REAL NOT NULL DEFAULT 0,
        turns INTEGER,
        tokens INTEGER,
        tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0,
        tokens_cache_read INTEGER NOT NULL DEFAULT 0,
        duration_s REAL NOT NULL DEFAULT 0,
        model TEXT NOT NULL DEFAULT '',
        seconds REAL,
        share_5h REAL,
        share_7d REAL,
        acct_5h REAL,
        acct_7d REAL,
        share_estimated INTEGER NOT NULL DEFAULT 0
    );
INSERT INTO "runs" VALUES('run-triage-1','triage','agent','2026-11-05','2026-11-02','2026-11','2026-11-05T07:00:00+00:00','2026-11-05T07:10:00+00:00','ok',20,0,600.0,0.0,7,1500,1200,300,9000,42.5,'claude-example',42.5,NULL,NULL,0.12,0.05,0);
INSERT INTO "runs" VALUES('run-chat-1','chat','user','2026-11-05','2026-11-02','2026-11','2026-11-05T07:00:00+00:00','2026-11-05T07:05:00+00:00','aborted',20,0,180.0,0.0,5,0,0,0,0,3.0,'',45.0,NULL,NULL,NULL,NULL,0);
INSERT INTO "runs" VALUES('run-inv-1','investigate','agent','2026-11-05','2026-11-02','2026-11','2026-11-05T07:00:00+00:00',NULL,'running',20,0,420.0,0.0,NULL,NULL,0,0,0,0.0,'',NULL,NULL,NULL,NULL,NULL,0);
CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('reminder', 'followup', 'recurring', 'retry')),
        title TEXT NOT NULL,
        owner TEXT NOT NULL,
        created_by TEXT NOT NULL,
        system_key TEXT UNIQUE,
        schedule TEXT NOT NULL DEFAULT '{}',
        payload TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'done', 'cancelled', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        generation INTEGER NOT NULL DEFAULT 0,
        next_fire_at TEXT,
        last_fired_at TEXT,
        thread_id TEXT,
        idempotency_key TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
INSERT INTO "tasks" VALUES(1,'reminder','Check the example filter','admin','panel:admin',NULL,'{}','{"note": "filter"}','pending',0,0,'2026-11-05T13:00:00+00:00',NULL,NULL,'rem-1','2026-11-05T10:30:00+00:00','2026-11-05T10:30:00+00:00');
INSERT INTO "tasks" VALUES(2,'recurring','Health check','system','daemon','health.check','{"every": 900}','{}','pending',0,0,'2026-11-05T12:15:00+00:00',NULL,NULL,NULL,'2026-11-05T10:30:00+00:00','2026-11-05T10:30:00+00:00');
INSERT INTO "tasks" VALUES(3,'recurring','Update window','system','daemon','updates.window','{"every": 900}','{}','pending',0,0,'2026-11-05T12:15:00+00:00',NULL,NULL,NULL,'2026-11-05T10:30:00+00:00','2026-11-05T10:30:00+00:00');
INSERT INTO "tasks" VALUES(4,'recurring','Status refresh','system','daemon','status.refresh','{"every": 900}','{}','pending',0,0,'2026-11-05T12:15:00+00:00',NULL,NULL,NULL,'2026-11-05T10:30:00+00:00','2026-11-05T10:30:00+00:00');
INSERT INTO "tasks" VALUES(5,'retry','Retry message #1','admin','daemon','retry:1','{}','{"turn_id": 1, "attempt": 1}','pending',0,0,'2026-11-05T12:30:00+00:00',NULL,'eeeeeeeeeeeeeeee','retry:1:1','2026-11-05T11:59:00+00:00','2026-11-05T11:59:00+00:00');
CREATE TABLE turns (
        id INTEGER PRIMARY KEY,
        role TEXT NOT NULL CHECK (role IN ('admin', 'agent', 'system')),
        text TEXT NOT NULL,
        at TEXT NOT NULL,
        run_id TEXT,
        status TEXT NOT NULL DEFAULT 'ok',
        meta TEXT NOT NULL DEFAULT '{}',
        client_id TEXT,
        reply_to INTEGER
    );
INSERT INTO "turns" VALUES(1,'admin','hello from before','2026-10-30T06:00:00+00:00',NULL,'ok','{}',NULL,NULL);
INSERT INTO "turns" VALUES(2,'agent','an earlier answer','2026-10-30T06:01:00+00:00',NULL,'ok','{}',NULL,1);
INSERT INTO "turns" VALUES(3,'admin','Why is the example light offline?','2026-11-05T11:57:00+00:00',NULL,'ok','{"thread": "ask:q-42024584b652"}','client-example-1',NULL);
INSERT INTO "turns" VALUES(4,'agent','Its bridge restarted.','2026-11-05T11:58:00+00:00','run-chat-1','ok','{"thread": "ask:q-42024584b652"}',NULL,3);
INSERT INTO "turns" VALUES(5,'admin','Is this urgent?','2026-11-05T11:59:00+00:00',NULL,'queued','{"thread": "item:c-59923230bec5"}','client-example-2',NULL);
INSERT INTO "turns" VALUES(6,'system','Proposed a change','2026-11-05T11:59:00+00:00',NULL,'proposal','{"change": 1, "thread": "item:c-59923230bec5"}',NULL,5);
CREATE TABLE update_jobs (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES update_runs(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        entity_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        from_version TEXT NOT NULL,
        to_version TEXT NOT NULL,
        target TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'installing', 'rebooting', 'verifying',
            'succeeded', 'rolling_back', 'rolled_back', 'rollback_failed', 'failed', 'skipped', 'reboot_pending')),
        install_attempts INTEGER NOT NULL DEFAULT 0,
        rollback_attempts INTEGER NOT NULL DEFAULT 0,
        detail TEXT NOT NULL DEFAULT '',
        regressions TEXT NOT NULL DEFAULT '[]',
        idempotency_key TEXT NOT NULL UNIQUE,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL
    );
INSERT INTO "update_jobs" VALUES(1,2,0,'update.example_addon','addon','Example add-on','1.0.0','1.0.1','{"slug": "example"}','installing',1,0,'','[]','2:update.example_addon:1.0.0:1.0.1','2026-11-05T10:23:00+00:00',NULL,'2026-11-05T10:23:00+00:00');
INSERT INTO "update_jobs" VALUES(2,2,1,'update.example_hacs','hacs','Example HACS','2.0.0','2.1.0','{"repo": "example/example"}','pending',0,0,'','[]','2:update.example_hacs:2.0.0:2.1.0',NULL,NULL,'2026-11-05T10:21:00+00:00');
CREATE TABLE update_runs (
        id INTEGER PRIMARY KEY,
        window_day TEXT NOT NULL,
        trigger TEXT NOT NULL,
        mode TEXT NOT NULL CHECK (mode IN ('report', 'act')),
        state TEXT NOT NULL CHECK (state IN ('planned', 'backing_up', 'installing', 'done', 'aborted', 'failed')),
        plan TEXT NOT NULL DEFAULT '[]',
        review TEXT NOT NULL DEFAULT '{}',
        backup TEXT NOT NULL DEFAULT '{}',
        before TEXT,
        before_first TEXT,
        summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT
    );
INSERT INTO "update_runs" VALUES(1,'2026-11-01','window','report','done','[]','{}','{}',NULL,NULL,'Nothing to install','2026-11-01T08:00:00+00:00','2026-11-01T08:10:00+00:00','2026-11-01T08:10:00+00:00');
INSERT INTO "update_runs" VALUES(2,'2026-11-05','window','act','installing','[{"entity_id": "update.example_core", "kind": "core", "title": "Home Assistant Core", "installed": "2026.10.1", "latest": "2026.10.3", "tier": "approve", "bump": "patch", "reason": "a fragile custom integration", "review": {}, "held": false}, {"entity_id": "update.example_os", "kind": "os", "title": "Example OS", "installed": "16.1", "latest": "16.2", "tier": "approve", "bump": "patch", "reason": "a fragile custom integration", "review": {}, "held": false}, {"entity_id": "update.example_addon", "kind": "addon", "title": "Example add-on", "installed": "1.0.0", "latest": "1.0.1", "tier": "approve", "bump": "patch", "reason": "a fragile custom integration", "review": {}, "held": false}, {"entity_id": "update.example_rejected", "kind": "addon", "title": "Rejected", "installed": "2026.10.1", "latest": "2026.10.3", "tier": "approve", "bump": "patch", "reason": "a fragile custom integration", "review": {}, "held": false}]','{}','{"slug": "abc12345"}','{"unavailable": []}',NULL,'Installing 1 of 2','2026-11-05T10:20:00+00:00','2026-11-05T10:05:00+00:00',NULL);
CREATE INDEX audit_at ON audit(at);
CREATE INDEX audit_run ON audit(run_id);
CREATE INDEX tasks_due ON tasks(state, next_fire_at);
CREATE UNIQUE INDEX tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX approvals_state ON approvals(kind, state, created_at);
CREATE INDEX approvals_subject ON approvals(kind, subject, state);
CREATE UNIQUE INDEX turns_client ON turns(client_id) WHERE client_id IS NOT NULL;
CREATE INDEX turns_reply ON turns(reply_to) WHERE reply_to IS NOT NULL;
CREATE INDEX runs_day ON runs(day);
CREATE INDEX runs_started ON runs(started_at);
CREATE INDEX update_runs_day ON update_runs(window_day, trigger);
CREATE INDEX update_runs_state ON update_runs(state);
CREATE INDEX update_jobs_run ON update_jobs(run_id, seq);
CREATE INDEX update_jobs_state ON update_jobs(state);
CREATE INDEX items_needs ON items(needs, needs_since);
CREATE INDEX items_at ON items(at);
CREATE INDEX items_state ON items(state, resolved_at);
CREATE INDEX item_events_item ON item_events(item_id, id)
;
COMMIT;
PRAGMA user_version = 19;
