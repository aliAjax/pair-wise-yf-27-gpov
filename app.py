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
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
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
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public')),
                    reviewer_side TEXT CHECK(reviewer_side IN ('A','B'))
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
                CREATE TABLE IF NOT EXISTS recon_batches(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','failed','completed')),
                    total INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recon_rows(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    side TEXT NOT NULL CHECK(side IN ('A','B')),
                    stable_no TEXT NOT NULL,
                    holder TEXT NOT NULL,
                    circulated_at TEXT,
                    clues TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    batch_id INTEGER REFERENCES recon_batches(id),
                    raw_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(side, stable_no)
                );
                CREATE TABLE IF NOT EXISTS recon_matches(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stable_no TEXT NOT NULL UNIQUE,
                    row_a_id INTEGER REFERENCES recon_rows(id),
                    row_b_id INTEGER REFERENCES recon_rows(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','paired','discrepancy','confirmed','rejected')),
                    discrepancy_note TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    confirmed_by_a TEXT REFERENCES users(id),
                    confirmed_by_b TEXT REFERENCES users(id),
                    a_confirmed_at TEXT,
                    b_confirmed_at TEXT,
                    last_synced_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # 兼容旧库:补充审查员归属方(A/B 双方审查员)
            try:
                conn.execute("ALTER TABLE users ADD COLUMN reviewer_side TEXT CHECK(reviewer_side IN ('A','B'))")
            except sqlite3.OperationalError:
                pass

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("reviewer_a", "A馆审查员", "reviewer"),
                    ("reviewer_b", "B馆审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )
            conn.execute("UPDATE users SET reviewer_side='A' WHERE id='reviewer_a'")
            conn.execute("UPDATE users SET reviewer_side='B' WHERE id='reviewer_b'")

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

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

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
                # 只有双方审查员都确认,权利主张才能推进返还
                if new_status == "resolved_return":
                    obj = self._object(conn, claim["object_id"])
                    match = conn.execute("SELECT * FROM recon_matches WHERE stable_no=?", (obj["inventory_no"],)).fetchone()
                    if match and match["status"] != "confirmed":
                        raise BusinessError("该藏品的对账结果未经双方审查员确认,不能推进返还", 409, "recon_not_confirmed")
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
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
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

    # ---------------- 对账台账 ----------------

    def _recon_match_to_dict(self, m):
        return {
            "id": m["id"], "stable_no": m["stable_no"],
            "row_a_id": m["row_a_id"], "row_b_id": m["row_b_id"],
            "status": m["status"], "discrepancy_note": m["discrepancy_note"],
            "version": m["version"],
            "confirmed_by_a": m["confirmed_by_a"], "confirmed_by_b": m["confirmed_by_b"],
            "a_confirmed_at": m["a_confirmed_at"], "b_confirmed_at": m["b_confirmed_at"],
            "last_synced_at": m["last_synced_at"],
            "created_at": m["created_at"], "updated_at": m["updated_at"],
        }

    def create_recon_batch(self, user_id, label):
        label = (label or "").strip()
        if not label:
            raise BusinessError("批次名称不能为空", 422, "invalid_batch")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            cur = conn.execute(
                "INSERT INTO recon_batches(label,status,created_by,created_at) VALUES(?,?,?,?)",
                (label, "open", user_id, now()),
            )
            return {"id": cur.lastrowid, "label": label, "status": "open"}

    def sync_recon_list(self, user_id, batch_id, side, rows):
        """同步某一方的清单。已核完的行不碰(重试只补未核完行);记录变化作废未确认结果。"""
        if side not in ("A", "B"):
            raise BusinessError("side 必须是 A 或 B", 422, "invalid_side")
        if not isinstance(rows, list) or not rows:
            raise BusinessError("rows 必须是非空数组", 422, "invalid_rows")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            batch = conn.execute("SELECT * FROM recon_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch:
                raise BusinessError("对账批次不存在", 404, "not_found")
            if batch["status"] == "completed":
                raise BusinessError("批次已完成,不能继续同步", 409, "batch_completed")
            processed, skipped = 0, 0
            affected, invalidated = set(), set()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for r in rows:
                stable_no = str(r.get("stable_no", "")).strip()
                holder = str(r.get("holder", "")).strip()
                circulated_at = str(r.get("circulated_at", "")).strip() or None
                clues = str(r.get("clues", "")).strip()
                raw_ref = str(r.get("raw_ref", "")).strip() or None
                if not stable_no:
                    raise BusinessError("stable_no 不能为空", 422, "invalid_stable_no")
                content_hash = hashlib.sha256(
                    json.dumps({"holder": holder, "circulated_at": circulated_at, "clues": clues},
                               sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                existing = conn.execute(
                    "SELECT * FROM recon_rows WHERE side=? AND stable_no=?", (side, stable_no)
                ).fetchone()
                if existing and existing["confirmed"]:
                    skipped += 1
                    continue  # 已核完的行不碰,重试只补未核完行
                if existing:
                    if existing["content_hash"] != content_hash:
                        invalidated.add(stable_no)  # 记录变化 → 作废未确认结果
                    conn.execute(
                        """UPDATE recon_rows SET holder=?,circulated_at=?,clues=?,content_hash=?,
                           batch_id=?,raw_ref=?,updated_at=? WHERE side=? AND stable_no=?""",
                        (holder, circulated_at, clues, content_hash, batch_id, raw_ref, now(), side, stable_no),
                    )
                else:
                    conn.execute(
                        """INSERT INTO recon_rows(side,stable_no,holder,circulated_at,clues,content_hash,confirmed,batch_id,raw_ref,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,0,?,?,?,?)""",
                        (side, stable_no, holder, circulated_at, clues, content_hash, batch_id, raw_ref, now(), now()),
                    )
                affected.add(stable_no)
                processed += 1
            # 重新配对 + 作废未确认结果(已确认的结论不动)
            for stable_no in affected:
                row_a = conn.execute("SELECT * FROM recon_rows WHERE side='A' AND stable_no=?", (stable_no,)).fetchone()
                row_b = conn.execute("SELECT * FROM recon_rows WHERE side='B' AND stable_no=?", (stable_no,)).fetchone()
                match = conn.execute("SELECT * FROM recon_matches WHERE stable_no=?", (stable_no,)).fetchone()
                if match and match["status"] == "confirmed":
                    continue
                paired = bool(row_a and row_b)
                row_a_id = row_a["id"] if row_a else None
                row_b_id = row_b["id"] if row_b else None
                if match:
                    if stable_no in invalidated:
                        conn.execute(
                            """UPDATE recon_matches SET status='pending',row_a_id=?,row_b_id=?,
                               confirmed_by_a=NULL,confirmed_by_b=NULL,a_confirmed_at=NULL,b_confirmed_at=NULL,
                               discrepancy_note=NULL,version=version+1,last_synced_at=?,updated_at=?
                               WHERE id=? AND status!='confirmed'""",
                            (row_a_id, row_b_id, now(), now(), match["id"]),
                        )
                    else:
                        conn.execute(
                            """UPDATE recon_matches SET status=?,row_a_id=?,row_b_id=?,last_synced_at=?,updated_at=?
                               WHERE id=? AND status!='confirmed'""",
                            ("paired" if paired else "pending", row_a_id, row_b_id, now(), now(), match["id"]),
                        )
                else:
                    conn.execute(
                        """INSERT INTO recon_matches(stable_no,row_a_id,row_b_id,status,version,last_synced_at,created_at,updated_at)
                           VALUES(?,?,?,?,1,?,?,?)""",
                        (stable_no, row_a_id, row_b_id, "paired" if paired else "pending", now(), now(), now()),
                    )
            total = conn.execute("SELECT COUNT(*) c FROM recon_matches").fetchone()["c"]
            done = conn.execute("SELECT COUNT(*) c FROM recon_matches WHERE status IN ('confirmed','rejected')").fetchone()["c"]
            conn.execute("UPDATE recon_batches SET total=?, status=? WHERE id=?",
                         (total, "completed" if total and total == done else "open", batch_id))
            conn.commit()
        except Exception:
            conn.rollback()
            conn.execute("UPDATE recon_batches SET status='failed' WHERE id=?", (batch_id,))
            conn.commit()
            raise
        return {"batch_id": batch_id, "side": side, "processed": processed,
                "skipped": skipped, "invalidated": sorted(invalidated)}

    def list_recon_matches(self, user_id, status=None):
        with self.connect() as conn:
            self._user(conn, user_id)
            if status:
                rows = conn.execute("SELECT * FROM recon_matches WHERE status=? ORDER BY id", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM recon_matches ORDER BY id").fetchall()
            return [self._recon_match_to_dict(r) for r in rows]

    def get_recon_match(self, user_id, match_id):
        with self.connect() as conn:
            self._user(conn, user_id)
            match = conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone()
            if not match:
                raise BusinessError("对账结果不存在", 404, "not_found")
            result = self._recon_match_to_dict(match)
            result["row_a"] = dict(conn.execute("SELECT * FROM recon_rows WHERE id=?", (match["row_a_id"],)).fetchone()) if match["row_a_id"] else None
            result["row_b"] = dict(conn.execute("SELECT * FROM recon_rows WHERE id=?", (match["row_b_id"],)).fetchone()) if match["row_b_id"] else None
            return result

    def confirm_recon_match(self, user_id, match_id, version):
        """双方审查员确认。乐观锁:同时提交只算先确认的,另一个拿到冲突。"""
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            side = reviewer["reviewer_side"]
            if side not in ("A", "B"):
                raise BusinessError("审查员未归属 A/B 方,不能确认", 403, "reviewer_side_required")
            try:
                conn.execute("BEGIN IMMEDIATE")
                match = conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone()
                if not match:
                    raise BusinessError("对账结果不存在", 404, "not_found")
                if match["status"] == "rejected":
                    raise BusinessError("该结果已被拒绝,不能确认", 409, "match_rejected")
                if match["status"] == "confirmed":
                    return self._recon_match_to_dict(match) | {"already_confirmed": True}
                if not match["row_a_id"] or not match["row_b_id"]:
                    raise BusinessError("双方清单未配齐,不能确认", 409, "match_incomplete")
                col = "confirmed_by_a" if side == "A" else "confirmed_by_b"
                at_col = "a_confirmed_at" if side == "A" else "b_confirmed_at"
                already = match[col] == user_id
                if already:
                    return self._recon_match_to_dict(match) | {"side_already_confirmed": True}
                cur = conn.execute(
                    f"UPDATE recon_matches SET {col}=?,{at_col}=?,version=version+1,updated_at=? "
                    f"WHERE id=? AND version=?",
                    (user_id, now(), now(), match_id, version),
                )
                if cur.rowcount == 0:
                    raise BusinessError("结果已被他人修改或确认,请刷新后重试", 409, "conflict")
                updated = conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone()
                if updated["confirmed_by_a"] and updated["confirmed_by_b"]:
                    conn.execute("UPDATE recon_matches SET status='confirmed',version=version+1,updated_at=? WHERE id=?",
                                 (now(), match_id))
                    conn.execute("UPDATE recon_rows SET confirmed=1 WHERE id IN (?,?)",
                                 (updated["row_a_id"], updated["row_b_id"]))
                conn.commit()
                result = conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone()
                return self._recon_match_to_dict(result)
            except Exception:
                conn.rollback()
                raise

    def mark_recon_discrepancy(self, user_id, match_id, note):
        note = (note or "").strip()
        if len(note) < 5:
            raise BusinessError("差异说明至少 5 字", 422, "note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            match = conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone()
            if not match:
                raise BusinessError("对账结果不存在", 404, "not_found")
            if match["status"] == "confirmed":
                raise BusinessError("已确认的结果不能标记差异", 409, "already_confirmed")
            conn.execute(
                "UPDATE recon_matches SET status='discrepancy',discrepancy_note=?,version=version+1,updated_at=? WHERE id=?",
                (note, now(), match_id),
            )
            conn.commit()
            return self._recon_match_to_dict(conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone())

    def reject_recon_match(self, user_id, match_id, note):
        note = (note or "").strip()
        if len(note) < 5:
            raise BusinessError("拒绝说明至少 5 字", 422, "note_required")
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            match = conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone()
            if not match:
                raise BusinessError("对账结果不存在", 404, "not_found")
            if match["status"] == "confirmed":
                raise BusinessError("已确认的结果不能拒绝", 409, "already_confirmed")
            conn.execute(
                "UPDATE recon_matches SET status='rejected',discrepancy_note=?,version=version+1,updated_at=? WHERE id=?",
                (note, now(), match_id),
            )
            conn.commit()
            return self._recon_match_to_dict(conn.execute("SELECT * FROM recon_matches WHERE id=?", (match_id,)).fetchone())

    def recon_handoff(self, user_id):
        """交接:只输出双方都确认的结论,未确认/差异/待核的不带入交接。"""
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            matches = conn.execute("SELECT * FROM recon_matches ORDER BY id").fetchall()
            conclusions, pending, discrepancies, rejected = [], [], [], []
            for m in matches:
                d = self._recon_match_to_dict(m)
                if m["status"] == "confirmed":
                    conclusions.append(d)
                elif m["status"] == "rejected":
                    rejected.append(d)
                elif m["status"] == "discrepancy":
                    discrepancies.append(d)
                else:
                    pending.append(d)
            return {"conclusions": conclusions, "pending": pending,
                    "discrepancies": discrepancies, "rejected": rejected,
                    "ready": not pending and not discrepancies}


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
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        if parts == ["api", "recon", "batches"] and method == "POST":
            d = self._body(); return self._send(201, store.create_recon_batch(user, d.get("label", "")))
        if len(parts) == 5 and parts[:3] == ["api", "recon", "batches"] and parts[4] == "sync" and method == "POST":
            d = self._body(); return self._send(200, store.sync_recon_list(user, int(parts[3]), d.get("side", ""), d.get("rows", [])))
        if parts == ["api", "recon", "matches"] and method == "GET":
            qs = parse_qs(parsed.query); status = qs.get("status", [None])[0]
            return self._send(200, {"items": store.list_recon_matches(user, status)})
        if len(parts) == 4 and parts[:3] == ["api", "recon", "matches"] and method == "GET":
            return self._send(200, store.get_recon_match(user, int(parts[3])))
        if len(parts) == 5 and parts[:3] == ["api", "recon", "matches"] and parts[4] == "confirm" and method == "POST":
            d = self._body(); return self._send(200, store.confirm_recon_match(user, int(parts[3]), d.get("version", 0)))
        if len(parts) == 5 and parts[:3] == ["api", "recon", "matches"] and parts[4] == "discrepancy" and method == "POST":
            d = self._body(); return self._send(200, store.mark_recon_discrepancy(user, int(parts[3]), d.get("note", "")))
        if len(parts) == 5 and parts[:3] == ["api", "recon", "matches"] and parts[4] == "reject" and method == "POST":
            d = self._body(); return self._send(200, store.reject_recon_match(user, int(parts[3]), d.get("note", "")))
        if parts == ["api", "recon", "handoff"] and method == "GET":
            return self._send(200, store.recon_handoff(user))
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
