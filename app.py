"""博物馆藏品来源与返还审查系统。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
RECON_SIDES = ("A", "B")
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recon_entries(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    side TEXT NOT NULL CHECK(side IN ('A','B')),
                    client_row_id TEXT NOT NULL,
                    stable_no TEXT NOT NULL,
                    local_ref TEXT, holder TEXT, transfer_date TEXT,
                    payload TEXT NOT NULL,
                    content_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    submitted_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(side,client_row_id,revision)
                );
                CREATE INDEX IF NOT EXISTS idx_recon_latest ON recon_entries(side,stable_no,revision);
                CREATE TABLE IF NOT EXISTS recon_sync_runs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    side TEXT NOT NULL CHECK(side IN ('A','B')),
                    client_batch_id TEXT, received INTEGER NOT NULL, inserted INTEGER NOT NULL,
                    skipped INTEGER NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recon_pair_revisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stable_no TEXT NOT NULL, pair_seq INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('matched','discrepancy','one_sided')),
                    discrepancies TEXT NOT NULL,
                    entry_a INTEGER REFERENCES recon_entries(id),
                    entry_b INTEGER REFERENCES recon_entries(id),
                    state TEXT NOT NULL DEFAULT 'current' CHECK(state IN ('current','superseded_voided','frozen')),
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(stable_no,pair_seq)
                );
                CREATE TABLE IF NOT EXISTS recon_confirmations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_revision_id INTEGER NOT NULL REFERENCES recon_pair_revisions(id),
                    side TEXT NOT NULL CHECK(side IN ('A','B')),
                    reviewer_id TEXT NOT NULL REFERENCES users(id), note TEXT NOT NULL,
                    voided INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    UNIQUE(pair_revision_id,side)
                );
                """
            )
            self._migrate(conn)

    def _migrate(self, conn):
        """为既有库补齐新列（全新建库时同样幂等）。"""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "side" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN side TEXT")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(claims)").fetchall()}
        if "stable_no" not in cols:
            conn.execute("ALTER TABLE claims ADD COLUMN stable_no TEXT")
        conn.execute("UPDATE users SET side='A' WHERE id='reviewer1' AND side IS NULL")
        conn.execute("UPDATE users SET side='B' WHERE id='reviewer2' AND side IS NULL")

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,side) VALUES(?,?,?,?)",
                [
                    ("staff", "藏品研究员", "staff", None),
                    ("reviewer1", "返还审查员(甲馆)", "reviewer", "A"),
                    ("reviewer2", "返还审查员(乙馆)", "reviewer", "B"),
                    ("claimant1", "权利主张人", "claimant", None),
                    ("public", "公众访客", "public", None),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome, stable_no=None):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        stable_no = self._blank(stable_no)
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            if stable_no and not conn.execute(
                "SELECT 1 FROM recon_pair_revisions WHERE stable_no=? AND state='current'", (stable_no,)
            ).fetchone():
                raise BusinessError("关联的对账台账稳定编号不存在", 404, "recon_not_found")
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,stable_no,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), stable_no, now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create",
                        {"claim_id": cur.lastrowid, "stable_no": stable_no})
            return {"id": cur.lastrowid, "object_id": object_id, "stable_no": stable_no, "status": "submitted"}

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                allowed = CLAIM_TRANSITIONS.get(claim["status"], set())
                if new_status not in allowed:
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                if new_status == "resolved_return" and claim["stable_no"]:
                    self.assert_return_cleared(conn, claim["stable_no"])
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note.strip(), now()),
                )
                new_version = claim["object_id"]
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition",
                            {"claim_id": claim_id, "from": claim["status"], "to": new_status,
                             "stable_no": claim["stable_no"]})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}

    # ------------------------------------------------------------------
    # 两馆对账台账
    # ------------------------------------------------------------------

    _RECON_KNOWN = {"stable_no", "client_row_id", "local_ref", "holder", "transfer_date"}

    @staticmethod
    def _blank(value):
        text = str(value).strip() if value is not None else ""
        return text or None

    def _clean_recon_row(self, row):
        """归一化一条清单行：稳定编号必填，流转日期必须是 YYYY-MM-DD。"""
        if not isinstance(row, dict):
            raise BusinessError("清单行必须是对象", 422, "invalid_recon_row")
        stable_no = self._blank(row.get("stable_no"))
        if not stable_no:
            raise BusinessError("稳定编号 stable_no 不能为空", 422, "invalid_recon_row")
        client_row_id = self._blank(row.get("client_row_id")) or stable_no
        local_ref, holder = self._blank(row.get("local_ref")), self._blank(row.get("holder"))
        transfer_date = self._blank(row.get("transfer_date"))
        if transfer_date:
            try:
                date.fromisoformat(transfer_date)
            except ValueError:
                raise BusinessError(f"{stable_no} 的流转日期必须是 YYYY-MM-DD", 422, "invalid_recon_date")
        extras = {k: v for k, v in row.items() if k not in self._RECON_KNOWN}
        normalized = {
            "stable_no": stable_no, "client_row_id": client_row_id,
            "local_ref": local_ref, "holder": holder, "transfer_date": transfer_date,
            "extras": extras,
        }
        canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        normalized["content_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        return normalized

    @staticmethod
    def _latest_entry(conn, side, stable_no):
        return conn.execute(
            "SELECT * FROM recon_entries WHERE side=? AND stable_no=? ORDER BY revision DESC,id DESC LIMIT 1",
            (side, stable_no),
        ).fetchone()

    @staticmethod
    def _compare_entries(entry_a, entry_b):
        """比较两侧字段，产出差异清单；只有一侧记录时状态为 one_sided。"""
        if entry_a is None or entry_b is None:
            missing = "A" if entry_a is None else "B"
            return "one_sided", [{"field": "side_presence", "missing_side": missing,
                                  "message": f"缺少 {'甲' if missing == 'A' else '乙'}馆清单记录"}]
        diffs = []
        for field, label in (("local_ref", "本方编号"), ("holder", "持有人"), ("transfer_date", "流转日期")):
            a, b = entry_a[field], entry_b[field]
            if (a or None) != (b or None):
                diffs.append({"field": field, "label": label, "a": a, "b": b,
                              "message": f"{label}不一致：甲馆={a or '空'}，乙馆={b or '空'}"})
        return ("discrepancy" if diffs else "matched"), diffs

    def _rebuild_pairs(self, conn, stable_nos):
        """受影响稳定编号重建当前配对版本。

        已双方确认的版本冻结存档、另开新版本；未双方确认的版本整版作废，
        其上的确认留痕但标记 voided，交接不得再引用。
        """
        for stable_no in stable_nos:
            entry_a = self._latest_entry(conn, "A", stable_no)
            entry_b = self._latest_entry(conn, "B", stable_no)
            status, diffs = self._compare_entries(entry_a, entry_b)
            diffs_json = json.dumps(diffs, ensure_ascii=False, sort_keys=True)
            current = conn.execute(
                "SELECT * FROM recon_pair_revisions WHERE stable_no=? AND state='current' ORDER BY pair_seq DESC LIMIT 1",
                (stable_no,),
            ).fetchone()
            if current and current["entry_a"] == (entry_a["id"] if entry_a else None) \
                    and current["entry_b"] == (entry_b["id"] if entry_b else None) \
                    and current["discrepancies"] == diffs_json:
                continue  # 幂等重试：两侧最新行与差异均未变化，不重建
            reason = ""
            if current:
                confirmed_sides = {r["side"] for r in conn.execute(
                    "SELECT side FROM recon_confirmations WHERE pair_revision_id=? AND voided=0", (current["id"],))}
                if {"A", "B"} <= confirmed_sides:
                    conn.execute("UPDATE recon_pair_revisions SET state='frozen',reason=? WHERE id=?",
                                 ("一侧清单记录产生新修订，双方确认版冻结存档", current["id"]))
                    reason = "沿用已双方确认的冻结版本之后重开，需重新双确认"
                else:
                    conn.execute("UPDATE recon_confirmations SET voided=1 WHERE pair_revision_id=?", (current["id"],))
                    conn.execute("UPDATE recon_pair_revisions SET state='superseded_voided',reason=? WHERE id=?",
                                 ("一侧清单记录变化，未双方确认结果作废", current["id"]))
                    reason = "一侧清单记录变化，未确认结果作废"
            seq = conn.execute("SELECT COALESCE(MAX(pair_seq),0)+1 FROM recon_pair_revisions WHERE stable_no=?",
                               (stable_no,)).fetchone()[0]
            conn.execute(
                """INSERT INTO recon_pair_revisions(stable_no,pair_seq,status,discrepancies,entry_a,entry_b,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (stable_no, seq, status, diffs_json,
                 entry_a["id"] if entry_a else None, entry_b["id"] if entry_b else None, reason, now()),
            )

    def sync_recon_entries(self, user_id, side, rows, client_batch_id=None):
        """接收一侧清单：按(侧,行键,内容哈希)幂等去重，重试只补未同步行。"""
        if side not in RECON_SIDES:
            raise BusinessError("side 必须是 A 或 B", 422, "invalid_side")
        if not isinstance(rows, list) or not rows:
            raise BusinessError("rows 必须是非空数组", 422, "invalid_recon_rows")
        cleaned = [self._clean_recon_row(r) for r in rows]
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            inserted, skipped, affected = 0, 0, set()
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in cleaned:
                    latest = conn.execute(
                        "SELECT * FROM recon_entries WHERE side=? AND client_row_id=? ORDER BY revision DESC,id DESC LIMIT 1",
                        (side, item["client_row_id"]),
                    ).fetchone()
                    if latest:
                        if latest["content_hash"] == item["content_hash"]:
                            skipped += 1
                            continue
                        if latest["stable_no"] != item["stable_no"]:
                            affected.add(latest["stable_no"])
                        revision = latest["revision"] + 1
                    else:
                        revision = 1
                    conn.execute(
                        """INSERT INTO recon_entries(side,client_row_id,stable_no,local_ref,holder,transfer_date,
                              payload,content_hash,revision,submitted_by,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (side, item["client_row_id"], item["stable_no"], item["local_ref"], item["holder"],
                         item["transfer_date"], json.dumps(item, ensure_ascii=False, sort_keys=True),
                         item["content_hash"], revision, user_id, now()),
                    )
                    inserted += 1
                    affected.add(item["stable_no"])
                self._rebuild_pairs(conn, affected)
                cur = conn.execute(
                    """INSERT INTO recon_sync_runs(side,client_batch_id,received,inserted,skipped,actor_id,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (side, self._blank(client_batch_id), len(cleaned), inserted, skipped, user_id, now()),
                )
                self._audit(conn, None, user_id, "recon.sync",
                            {"side": side, "batch": client_batch_id, "received": len(cleaned),
                             "inserted": inserted, "skipped": skipped, "affected": sorted(affected)})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return {"sync_run_id": cur.lastrowid, "side": side, "received": len(cleaned),
                    "inserted": inserted, "skipped": skipped, "rebuilt": sorted(affected)}

    @staticmethod
    def _pair_payload(conn, pair):
        confirms = conn.execute(
            "SELECT side,reviewer_id,note,voided,created_at FROM recon_confirmations WHERE pair_revision_id=? ORDER BY id",
            (pair["id"],)).fetchall()
        live = {c["side"]: dict(c) for c in confirms if not c["voided"]}

        def entry_view(entry_id):
            if not entry_id:
                return None
            e = conn.execute("SELECT * FROM recon_entries WHERE id=?", (entry_id,)).fetchone()
            return {k: e[k] for k in ("id", "side", "client_row_id", "stable_no", "local_ref",
                                      "holder", "transfer_date", "revision", "submitted_by", "created_at")}

        return {
            "stable_no": pair["stable_no"], "pair_seq": pair["pair_seq"], "status": pair["status"],
            "state": pair["state"], "reason": pair["reason"],
            "discrepancies": json.loads(pair["discrepancies"]),
            "entry_a": entry_view(pair["entry_a"]), "entry_b": entry_view(pair["entry_b"]),
            "confirmations": [dict(c) for c in confirms],
            "confirmed_sides": sorted(live),
            "confirmed_both": {"A", "B"} <= set(live),
            "created_at": pair["created_at"],
        }

    def list_pairs(self, user_id):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            pairs = conn.execute(
                "SELECT * FROM recon_pair_revisions WHERE state='current' ORDER BY stable_no,pair_seq"
            ).fetchall()
            return [self._pair_payload(conn, p) for p in pairs]

    def pair_detail(self, user_id, stable_no):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            pairs = conn.execute(
                "SELECT * FROM recon_pair_revisions WHERE stable_no=? ORDER BY pair_seq", (stable_no,)
            ).fetchall()
            if not pairs:
                raise BusinessError("台账中没有该稳定编号", 404, "not_found")
            current = next((p for p in pairs if p["state"] == "current"), None)
            return {"stable_no": stable_no,
                    "current": self._pair_payload(conn, current) if current else None,
                    "history": [self._pair_payload(conn, p) for p in pairs]}

    def _current_pair(self, conn, stable_no):
        pair = conn.execute(
            "SELECT * FROM recon_pair_revisions WHERE stable_no=? AND state='current' ORDER BY pair_seq DESC LIMIT 1",
            (stable_no,),
        ).fetchone()
        if not pair:
            raise BusinessError("台账中没有该稳定编号", 404, "not_found")
        return pair

    def confirm_pair(self, user_id, stable_no, note):
        """一侧审查员确认当前版本；同一版本同侧唯一，先到先得，后到拿冲突。"""
        if len(note.strip()) < 5:
            raise BusinessError("确认说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            side = reviewer["side"]
            if side not in RECON_SIDES:
                raise BusinessError("该审查员未归属甲馆(A)或乙馆(B)，不能确认台账", 403, "forbidden")
            conn.execute("BEGIN IMMEDIATE")
            try:
                pair = self._current_pair(conn, stable_no)
                if pair["status"] == "one_sided":
                    raise BusinessError("仅有一侧清单记录，无法确认，待对侧补录", 409, "one_sided_pair")
                existing = conn.execute(
                    "SELECT * FROM recon_confirmations WHERE pair_revision_id=? AND side=? AND voided=0",
                    (pair["id"], side),
                ).fetchone()
                if existing:
                    raise BusinessError(
                        f"版本 #{pair['pair_seq']} 的{side}侧确认已由 {existing['reviewer_id']} "
                        f"于 {existing['created_at']} 先提交，本次提交构成冲突",
                        409, "confirmation_conflict")
                try:
                    conn.execute(
                        "INSERT INTO recon_confirmations(pair_revision_id,side,reviewer_id,note,created_at) VALUES(?,?,?,?,?)",
                        (pair["id"], side, user_id, note.strip(), now()),
                    )
                except sqlite3.IntegrityError:
                    # 并发下两侧/两人同时提交：唯一约束保证只有先确认者落库。
                    raise BusinessError(
                        f"版本 #{pair['pair_seq']} 的{side}侧确认已被先到的提交占用，本次提交冲突",
                        409, "confirmation_conflict")
                self._audit(conn, None, user_id, "recon.confirm",
                            {"stable_no": stable_no, "pair_seq": pair["pair_seq"], "side": side})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return self.pair_detail(user_id, stable_no)["current"]

    def handoff(self, user_id, stable_no):
        """交接结论：只返回当前版本；未双方确认一律拒绝，不复用冻结的旧结论。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            pair = self._current_pair(conn, stable_no)
            payload = self._pair_payload(conn, pair)
            if not payload["confirmed_both"]:
                raise BusinessError(
                    f"当前台账版本 #{payload['pair_seq']}（{payload['status']}）尚未经双方审查员确认，"
                    f"已确认侧：{payload['confirmed_sides'] or '无'}，交接/返还不得使用",
                    409, "recon_not_ready")
            payload["handoff_at"] = now()
            return payload

    def assert_return_cleared(self, conn, stable_no):
        pair = self._current_pair(conn, stable_no)
        payload = self._pair_payload(conn, pair)
        if not payload["confirmed_both"]:
            raise BusinessError(
                f"该主张关联的台账版本 #{payload['pair_seq']} 尚未经双方审查员确认，权利主张不能推进返还",
                409, "recon_not_ready")


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if parts == ["api", "recon", "sync"] and method == "POST":
            d = self._body(); return self._send(201, store.sync_recon_entries(user, d.get("side", ""), d.get("rows", []), d.get("client_batch_id")))
        if parts == ["api", "recon", "pairs"] and method == "GET":
            return self._send(200, {"items": store.list_pairs(user)})
        if len(parts) == 4 and parts[:3] == ["api", "recon", "pairs"] and method == "GET":
            return self._send(200, store.pair_detail(user, unquote(parts[3])))
        if len(parts) == 5 and parts[:3] == ["api", "recon", "pairs"] and parts[4] == "confirm" and method == "POST":
            d = self._body(); return self._send(200, store.confirm_pair(user, unquote(parts[3]), d.get("note", "")))
        if len(parts) == 5 and parts[:3] == ["api", "recon", "pairs"] and parts[4] == "handoff" and method == "GET":
            return self._send(200, store.handoff(user, unquote(parts[3])))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", ""), d.get("stable_no")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
