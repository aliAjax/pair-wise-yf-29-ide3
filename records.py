"""保管记录：证据、保管事件链与完整性报告。

本模块只维护“保管记录”本身，不知道跨案引用规则的存在；
跨案引用规则见 references.py，页面展示见 web/index.html。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "custody.db"
MEMBER_ROLES = {"custodian", "analyst", "auditor"}


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CustodyRecords:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS cases(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_number TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS case_members(
                    case_id INTEGER NOT NULL REFERENCES cases(id), user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL CHECK(role IN ('custodian','analyst','auditor')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    granted_by TEXT NOT NULL REFERENCES users(id), granted_at TEXT NOT NULL,
                    PRIMARY KEY(case_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), label TEXT NOT NULL,
                    filename TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                    content BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'custody'
                        CHECK(status IN ('custody','opened','released','derivative')),
                    current_custodian TEXT NOT NULL, legal_hold INTEGER NOT NULL DEFAULT 0 CHECK(legal_hold IN (0,1)),
                    retention_until TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(case_id,label)
                );
                CREATE TABLE IF NOT EXISTS custody_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id), sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('INGEST','TRANSFER','OPEN','ANALYZE','RELEASE','HOLD_SET','HOLD_CLEARED')),
                    actor_id TEXT NOT NULL REFERENCES users(id), from_person TEXT,
                    to_person TEXT, location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(evidence_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS derivatives(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    child_evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id),
                    method TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, UNIQUE(parent_evidence_id,child_evidence_id)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases(id), actor_id TEXT NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name) VALUES(?,?)",
                [
                    ("custodian1", "证据保管员甲"), ("custodian2", "证据保管员乙"),
                    ("analyst1", "电子数据分析员"), ("auditor1", "案件审计员"), ("outsider", "外部人员"),
                ],
            )

    # ---- 基础校验 ----
    def user(self, conn, user_id):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在或已停用", 401, "unknown_user")
        return user

    def case(self, conn, case_id):
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise BusinessError("案件不存在", 404, "not_found")
        return row

    def member(self, conn, case_id, user_id, roles=None):
        user = self.user(conn, user_id)
        self.case(conn, case_id)
        row = conn.execute(
            "SELECT * FROM case_members WHERE case_id=? AND user_id=? AND active=1", (case_id, user_id)
        ).fetchone()
        if not row:
            raise BusinessError("不是案件有效成员", 403, "forbidden")
        if roles and row["role"] not in roles:
            raise BusinessError("当前案件角色无权执行此操作", 403, "forbidden")
        return user, row

    def audit(self, conn, case_id, actor_id, action, detail):
        conn.execute(
            "INSERT INTO audit_log(case_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (case_id, actor_id, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    # ---- 案件与成员 ----
    def create_case(self, user_id, case_number, title):
        if not case_number.strip() or len(title.strip()) < 2:
            raise BusinessError("案件编号和标题不能为空", 422, "invalid_case")
        with self.connect() as conn:
            self.user(conn, user_id)
            try:
                cur = conn.execute(
                    "INSERT INTO cases(case_number,title,created_by,created_at) VALUES(?,?,?,?)",
                    (case_number.strip(), title.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("案件编号已存在", 409, "case_exists")
            case_id = cur.lastrowid
            conn.execute(
                "INSERT INTO case_members(case_id,user_id,role,granted_by,granted_at) VALUES(?,?,?,?,?)",
                (case_id, user_id, "custodian", user_id, now()),
            )
            self.audit(conn, case_id, user_id, "case.create", {"case_number": case_number.strip()})
            return {"id": case_id, "case_number": case_number.strip(), "title": title.strip()}

    def add_member(self, user_id, case_id, member_id, role):
        if role not in MEMBER_ROLES:
            raise BusinessError("案件角色必须是 custodian、analyst 或 auditor", 422, "invalid_role")
        with self.connect() as conn:
            case = self.case(conn, case_id)
            if case["created_by"] != user_id:
                raise BusinessError("只有案件创建人可以授权成员", 403, "forbidden")
            self.user(conn, member_id)
            conn.execute(
                """INSERT INTO case_members(case_id,user_id,role,active,granted_by,granted_at) VALUES(?,?,?,1,?,?)
                   ON CONFLICT(case_id,user_id) DO UPDATE SET role=excluded.role,active=1,granted_by=excluded.granted_by,granted_at=excluded.granted_at""",
                (case_id, member_id, role, user_id, now()),
            )
            self.audit(conn, case_id, user_id, "member.grant", {"member_id": member_id, "role": role})
            return {"case_id": case_id, "member_id": member_id, "role": role}

    # ---- 保管事件链 ----
    @staticmethod
    def event_hash(event):
        canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()

    def append_event(self, conn, evidence_id, event_type, actor, from_person="", to_person="", location="", note=""):
        previous = conn.execute(
            "SELECT event_hash,sequence FROM custody_events WHERE evidence_id=? ORDER BY sequence DESC LIMIT 1", (evidence_id,)
        ).fetchone()
        sequence = (previous["sequence"] + 1) if previous else 1
        previous_hash = previous["event_hash"] if previous else "GENESIS"
        payload = {
            "evidence_id": evidence_id, "sequence": sequence, "event_type": event_type,
            "actor_id": actor, "from_person": from_person or None, "to_person": to_person or None,
            "location": location, "note": note, "previous_hash": previous_hash, "created_at": now(),
        }
        digest = self.event_hash(payload)
        cur = conn.execute(
            """INSERT INTO custody_events(evidence_id,sequence,event_type,actor_id,from_person,to_person,location,note,previous_hash,event_hash,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (evidence_id, sequence, event_type, actor, from_person or None, to_person or None, location, note, previous_hash, digest, payload["created_at"]),
        )
        return cur.lastrowid, digest

    # ---- 证据流转 ----
    def ingest_evidence(self, user_id, case_id, label, filename, content_b64, retention_until, custodian=None):
        label, filename = label.strip(), filename.strip()
        if not label or not filename:
            raise BusinessError("证据标签和文件名不能为空", 422, "invalid_evidence")
        try:
            content = base64.b64decode(content_b64, validate=True)
            deadline = date.fromisoformat(retention_until)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("证据内容 Base64 或保留期限格式错误", 422, "invalid_evidence")
        if deadline < date.today():
            raise BusinessError("保留期限不能早于今天", 422, "invalid_retention")
        digest = hashlib.sha256(content).hexdigest()
        custodian = (custodian or user_id).strip()
        with self.connect() as conn:
            self.member(conn, case_id, user_id, {"custodian"})
            if not custodian:
                raise BusinessError("保管人不能为空", 422, "invalid_custodian")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,current_custodian,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (case_id, label, filename, digest, len(content), content, custodian, retention_until, user_id, now()),
                )
                evidence_id = cur.lastrowid
                self.append_event(conn, evidence_id, "INGEST", user_id, to_person=custodian, note=f"入册 SHA-256 {digest}")
                self.audit(conn, case_id, user_id, "evidence.ingest", {"evidence_id": evidence_id, "sha256": digest, "label": label})
                return {"id": evidence_id, "label": label, "sha256": digest, "size": len(content), "status": "custody", "current_custodian": custodian}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("该案件中的证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def evidence_row(self, conn, evidence_id):
        row = conn.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
        if not row:
            raise BusinessError("证据不存在", 404, "not_found")
        return row

    def evidence_view(self, conn, row, include_content=False):
        result = {k: row[k] for k in row.keys() if k != "content"}
        result["legal_hold"] = bool(row["legal_hold"])
        result["integrity_valid"] = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
        result["events"] = [dict(x) for x in conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (row["id"],)).fetchall()]
        result["derived_children"] = [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (row["id"],)).fetchall()]
        if include_content:
            result["content_b64"] = base64.b64encode(row["content"]).decode()
        return result

    def get_evidence(self, user_id, evidence_id, include_content=False):
        with self.connect() as conn:
            row = self.evidence_row(conn, evidence_id)
            self.member(conn, row["case_id"], user_id)
            return self.evidence_view(conn, row, include_content)

    def transfer(self, user_id, evidence_id, to_person, location, note=""):
        if not to_person.strip() or not location.strip():
            raise BusinessError("接收人和保管位置不能为空", 422, "invalid_transfer")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self.evidence_row(conn, evidence_id)
                self.member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] == "released":
                    raise BusinessError("已释放证据不能再移交", 409, "evidence_released")
                self.append_event(conn, evidence_id, "TRANSFER", user_id, from_person=row["current_custodian"], to_person=to_person.strip(), location=location.strip(), note=note.strip())
                conn.execute("UPDATE evidence SET current_custodian=? WHERE id=?", (to_person.strip(), evidence_id))
                self.audit(conn, row["case_id"], user_id, "custody.transfer", {"evidence_id": evidence_id, "to": to_person.strip(), "location": location.strip()})
                return {"id": evidence_id, "current_custodian": to_person.strip(), "location": location.strip()}
            except Exception:
                conn.rollback()
                raise

    def open_evidence(self, user_id, evidence_id, location, note=""):
        if not location.strip():
            raise BusinessError("开箱地点不能为空", 422, "location_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self.evidence_row(conn, evidence_id)
                self.member(conn, row["case_id"], user_id, {"custodian"})
                if row["status"] != "custody":
                    raise BusinessError("只有处于封存保管状态的证据可以开箱", 409, "invalid_status")
                self.append_event(conn, evidence_id, "OPEN", user_id, from_person=row["current_custodian"], location=location.strip(), note=note.strip())
                conn.execute("UPDATE evidence SET status='opened' WHERE id=?", (evidence_id,))
                self.audit(conn, row["case_id"], user_id, "evidence.open", {"evidence_id": evidence_id, "location": location.strip()})
                return {"id": evidence_id, "status": "opened", "location": location.strip()}
            except Exception:
                conn.rollback()
                raise

    def derive(self, user_id, evidence_id, method, label, filename, content_b64):
        if len(method.strip()) < 3 or not label.strip() or not filename.strip():
            raise BusinessError("分析方法、子证据标签和文件名不能为空", 422, "invalid_derivative")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                parent = self.evidence_row(conn, evidence_id)
                self.member(conn, parent["case_id"], user_id, {"analyst"})
                if parent["status"] != "opened":
                    raise BusinessError("原始证据必须先开箱才能分析", 409, "evidence_not_opened")
                cur = conn.execute(
                    """INSERT INTO evidence(case_id,label,filename,sha256,size,content,status,current_custodian,legal_hold,retention_until,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (parent["case_id"], label.strip(), filename.strip(), digest, len(content), content, "derivative", user_id, 0, parent["retention_until"], user_id, now()),
                )
                child_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO derivatives(parent_evidence_id,child_evidence_id,method,actor_id,created_at) VALUES(?,?,?,?,?)",
                    (evidence_id, child_id, method.strip(), user_id, now()),
                )
                self.append_event(conn, evidence_id, "ANALYZE", user_id, from_person=parent["current_custodian"], note=f"生成衍生证据 #{child_id}: {method.strip()}")
                self.append_event(conn, child_id, "INGEST", user_id, from_person=parent["current_custodian"], to_person=user_id, note=f"由证据 #{evidence_id} 派生，SHA-256 {digest}")
                self.audit(conn, parent["case_id"], user_id, "evidence.derive", {"parent_id": evidence_id, "child_id": child_id, "method": method.strip(), "sha256": digest})
                return {"id": child_id, "parent_id": evidence_id, "label": label.strip(), "sha256": digest, "status": "derivative"}
            except sqlite3.IntegrityError:
                conn.rollback()
                raise BusinessError("衍生证据标签已存在", 409, "label_exists")
            except Exception:
                conn.rollback()
                raise

    def set_hold(self, user_id, evidence_id, hold, reason):
        if len(reason.strip()) < 5:
            raise BusinessError("法律保留原因至少 5 字", 422, "reason_required")
        with self.connect() as conn:
            row = self.evidence_row(conn, evidence_id)
            case = self.case(conn, row["case_id"])
            if user_id != case["created_by"]:
                self.member(conn, row["case_id"], user_id, {"auditor"})
            conn.execute("UPDATE evidence SET legal_hold=? WHERE id=?", (int(bool(hold)), evidence_id))
            event = "HOLD_SET" if hold else "HOLD_CLEARED"
            self.append_event(conn, evidence_id, event, user_id, note=reason.strip())
            self.audit(conn, row["case_id"], user_id, "evidence.hold", {"evidence_id": evidence_id, "hold": bool(hold), "reason": reason.strip()})
            return {"id": evidence_id, "legal_hold": bool(hold)}

    def release(self, user_id, evidence_id, recipient, note=""):
        if not recipient.strip():
            raise BusinessError("接收方不能为空", 422, "recipient_required")
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self.evidence_row(conn, evidence_id)
                self.member(conn, row["case_id"], user_id, {"custodian"})
                if row["legal_hold"]:
                    raise BusinessError("存在法律保留，禁止释放证据", 409, "legal_hold_active")
                if row["status"] == "released":
                    raise BusinessError("证据已经释放", 409, "already_released")
                self.append_event(conn, evidence_id, "RELEASE", user_id, from_person=row["current_custodian"], to_person=recipient.strip(), note=note.strip())
                conn.execute("UPDATE evidence SET status='released' WHERE id=?", (evidence_id,))
                self.audit(conn, row["case_id"], user_id, "evidence.release", {"evidence_id": evidence_id, "recipient": recipient.strip()})
                return {"id": evidence_id, "status": "released", "recipient": recipient.strip()}
            except Exception:
                conn.rollback()
                raise

    def report(self, user_id, case_id):
        with self.connect() as conn:
            self.member(conn, case_id, user_id)
            case = self.case(conn, case_id)
            items, all_valid = [], True
            for row in conn.execute("SELECT * FROM evidence WHERE case_id=? ORDER BY id", (case_id,)).fetchall():
                hash_valid = hashlib.sha256(row["content"]).hexdigest() == row["sha256"]
                events = conn.execute("SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (row["id"],)).fetchall()
                expected_prev, chain_valid = "GENESIS", True
                for e in events:
                    payload = {
                        "evidence_id": e["evidence_id"], "sequence": e["sequence"], "event_type": e["event_type"],
                        "actor_id": e["actor_id"], "from_person": e["from_person"], "to_person": e["to_person"],
                        "location": e["location"], "note": e["note"], "previous_hash": e["previous_hash"], "created_at": e["created_at"],
                    }
                    if e["previous_hash"] != expected_prev or self.event_hash(payload) != e["event_hash"]:
                        chain_valid = False
                    expected_prev = e["event_hash"]
                all_valid = all_valid and hash_valid and chain_valid
                items.append({
                    "id": row["id"], "label": row["label"], "filename": row["filename"], "sha256": row["sha256"],
                    "size": row["size"], "status": row["status"], "current_custodian": row["current_custodian"],
                    "legal_hold": bool(row["legal_hold"]), "retention_until": row["retention_until"],
                    "hash_valid": hash_valid, "chain_valid": chain_valid,
                    "events": [dict(e) for e in events],
                    "derivatives": [dict(x) for x in conn.execute("SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (row["id"],)).fetchall()],
                })
            audit = conn.execute("SELECT * FROM audit_log WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
            return {
                "case": dict(case), "generated_at": now(), "overall_integrity_valid": all_valid,
                "evidence_count": len(items), "evidence": items,
                "audit": [dict(a) | {"detail": json.loads(a["detail"])} for a in audit],
            }
