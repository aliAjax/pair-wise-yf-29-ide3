"""跨案引用规则：申请、受理核对、退回重提与只读查阅。

引用生命周期（pending → accepted / returned，returned 可由原申请人重提为新申请）
只在本模块维护；保管记录仍由 app.CustodyStore 维护，页面展示由 web/index.html 维护。
本服务通过组合 CustodyStore 复用其数据库连接与案件成员校验，不改动任何保管记录。
"""
from __future__ import annotations

import hashlib

from common import BusinessError, now

PENDING, ACCEPTED, RETURNED = "pending", "accepted", "returned"


class CrossReferenceService:
    def __init__(self, store):
        self.store = store

    def init_schema(self):
        with self.store.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS evidence_references(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    source_case_id INTEGER NOT NULL REFERENCES cases(id),
                    target_case_id INTEGER NOT NULL REFERENCES cases(id),
                    purpose TEXT NOT NULL,
                    applicant_id TEXT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','accepted','returned')),
                    -- 申请时刻源证据状态快照，受理时据此核对是否漂移
                    snapshot_status TEXT NOT NULL,
                    snapshot_custodian TEXT NOT NULL,
                    snapshot_legal_hold INTEGER NOT NULL CHECK(snapshot_legal_hold IN (0,1)),
                    snapshot_event_head TEXT NOT NULL,
                    reviewer_id TEXT REFERENCES users(id),
                    review_note TEXT NOT NULL DEFAULT '',
                    return_reason TEXT NOT NULL DEFAULT '',
                    resubmit_of INTEGER REFERENCES evidence_references(id),
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT
                );
                """
            )

    # ---------- 内部工具 ----------

    @staticmethod
    def _row(conn, reference_id):
        row = conn.execute("SELECT * FROM evidence_references WHERE id=?", (reference_id,)).fetchone()
        if not row:
            raise BusinessError("引用申请不存在", 404, "not_found")
        return row

    @staticmethod
    def _snapshot(conn, evidence_id):
        ev = conn.execute(
            "SELECT status,current_custodian,legal_hold FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        head = conn.execute(
            "SELECT event_hash FROM custody_events WHERE evidence_id=? ORDER BY sequence DESC LIMIT 1",
            (evidence_id,),
        ).fetchone()
        return {
            "status": ev["status"],
            "custodian": ev["current_custodian"],
            "legal_hold": int(ev["legal_hold"]),
            "event_head": head["event_hash"] if head else "GENESIS",
        }

    @staticmethod
    def _drift_reasons(ref, current):
        """比对申请时快照与源证据当前状态，列出需要退回的漂移原因。"""
        reasons = []
        if current["status"] != ref["snapshot_status"]:
            if ref["snapshot_status"] == "custody" and current["status"] == "opened":
                reasons.append("源证据已开箱")
            elif current["status"] == "released":
                reasons.append("源证据已释放")
            else:
                reasons.append(f"源证据状态由 {ref['snapshot_status']} 变为 {current['status']}")
        if current["custodian"] != ref["snapshot_custodian"]:
            reasons.append(f"源证据已移交（保管人 {ref['snapshot_custodian']} → {current['custodian']}）")
        if current["legal_hold"] != ref["snapshot_legal_hold"]:
            reasons.append("法律保留已解除" if ref["snapshot_legal_hold"] else "新增法律保留")
        if not reasons and current["event_head"] != ref["snapshot_event_head"]:
            reasons.append("源证据保管链出现新事件")
        return reasons

    def _public(self, conn, row):
        label = conn.execute("SELECT label FROM evidence WHERE id=?", (row["evidence_id"],)).fetchone()
        data = {
            "id": row["id"],
            "evidence_id": row["evidence_id"],
            "evidence_label": label["label"] if label else None,
            "source_case_id": row["source_case_id"],
            "target_case_id": row["target_case_id"],
            "purpose": row["purpose"],
            "applicant_id": row["applicant_id"],
            "status": row["status"],
            "return_reason": row["return_reason"],
            "reviewer_id": row["reviewer_id"],
            "review_note": row["review_note"],
            "resubmit_of": row["resubmit_of"],
            "created_at": row["created_at"],
            "reviewed_at": row["reviewed_at"],
        }
        if row["status"] == PENDING:
            data["drift"] = self._drift_reasons(row, self._snapshot(conn, row["evidence_id"]))
        return data

    @staticmethod
    def _ensure_no_active(conn, evidence_id, target_case_id):
        dup = conn.execute(
            """SELECT id FROM evidence_references
               WHERE evidence_id=? AND target_case_id=? AND status IN ('pending','accepted')""",
            (evidence_id, target_case_id),
        ).fetchone()
        if dup:
            raise BusinessError("该证据在此案件已存在待受理或已受理的引用", 409, "reference_exists")

    def _insert(self, conn, evidence_id, source_case_id, target_case_id, purpose, applicant_id, resubmit_of):
        snap = self._snapshot(conn, evidence_id)
        cur = conn.execute(
            """INSERT INTO evidence_references(
                   evidence_id,source_case_id,target_case_id,purpose,applicant_id,status,
                   snapshot_status,snapshot_custodian,snapshot_legal_hold,snapshot_event_head,
                   resubmit_of,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (evidence_id, source_case_id, target_case_id, purpose, applicant_id, PENDING,
             snap["status"], snap["custodian"], snap["legal_hold"], snap["event_head"],
             resubmit_of, now()),
        )
        return cur.lastrowid

    @staticmethod
    def _audit_both(conn, store, ref, user_id, action, detail):
        store._audit(conn, ref["source_case_id"], user_id, action, detail)
        store._audit(conn, ref["target_case_id"], user_id, action, detail)

    # ---------- 申请 ----------

    def create_request(self, user_id, evidence_id, target_case_id, purpose):
        """目标案件成员填写目标案件与用途，申请引用他案证据；系统快照源证据状态。"""
        purpose = (purpose or "").strip()
        if len(purpose) < 5:
            raise BusinessError("引用用途至少 5 字", 422, "invalid_purpose")
        try:
            target_case_id = int(target_case_id)
        except (TypeError, ValueError):
            raise BusinessError("目标案件编号格式错误", 422, "invalid_case")
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                ev = self.store._evidence(conn, evidence_id)
                self.store._case(conn, target_case_id)
                if ev["case_id"] == target_case_id:
                    raise BusinessError("不能引用本案件内的证据", 422, "same_case")
                self.store._member(conn, target_case_id, user_id)
                self._ensure_no_active(conn, evidence_id, target_case_id)
                ref_id = self._insert(conn, evidence_id, ev["case_id"], target_case_id, purpose, user_id, None)
                detail = {"reference_id": ref_id, "evidence_id": evidence_id, "target_case_id": target_case_id}
                self.store._audit(conn, ev["case_id"], user_id, "reference.request", detail)
                self.store._audit(conn, target_case_id, user_id, "reference.request", detail)
                return self._public(conn, self._row(conn, ref_id))
            except Exception:
                conn.rollback()
                raise

    # ---------- 受理核对 ----------

    def review(self, user_id, reference_id, decision, note=""):
        """源案件保管员受理。核对发现源证据已移交、开箱、释放或法律保留变化时，
        即使提交 accept 也强制退回并记录原因，由申请人重提。"""
        decision = (decision or "").strip().lower()
        if decision not in ("accept", "return"):
            raise BusinessError("受理结论必须是 accept 或 return", 422, "invalid_decision")
        note = (note or "").strip()
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                ref = self._row(conn, reference_id)
                self.store._member(conn, ref["source_case_id"], user_id, {"custodian"})
                if ref["status"] != PENDING:
                    raise BusinessError("该引用申请已受理或已退回", 409, "already_reviewed")
                drift = self._drift_reasons(ref, self._snapshot(conn, ref["evidence_id"]))
                if drift:
                    status, return_reason = RETURNED, "；".join(drift)
                elif decision == "return":
                    if not note:
                        raise BusinessError("退回时必须填写退回原因", 422, "reason_required")
                    status, return_reason = RETURNED, note
                else:
                    status, return_reason = ACCEPTED, ""
                conn.execute(
                    """UPDATE evidence_references
                       SET status=?,reviewer_id=?,review_note=?,return_reason=?,reviewed_at=? WHERE id=?""",
                    (status, user_id, note, return_reason, now(), reference_id),
                )
                updated = self._row(conn, reference_id)
                action = "reference.accept" if status == ACCEPTED else "reference.return"
                self._audit_both(conn, self.store, updated, user_id, action,
                                 {"reference_id": reference_id, "return_reason": return_reason, "drift": drift})
                return self._public(conn, updated)
            except Exception:
                conn.rollback()
                raise

    # ---------- 退回重提 ----------

    def resubmit(self, user_id, reference_id, purpose=None):
        """原申请人针对被退回的申请重提：生成关联原申请的新 pending 申请并刷新快照。"""
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                ref = self._row(conn, reference_id)
                if ref["status"] != RETURNED:
                    raise BusinessError("只有被退回的引用申请可以重提", 409, "not_returned")
                if ref["applicant_id"] != user_id:
                    raise BusinessError("只有原申请人可以重提该申请", 403, "forbidden")
                self.store._member(conn, ref["target_case_id"], user_id)
                new_purpose = (purpose or "").strip() or ref["purpose"]
                if len(new_purpose) < 5:
                    raise BusinessError("引用用途至少 5 字", 422, "invalid_purpose")
                self._ensure_no_active(conn, ref["evidence_id"], ref["target_case_id"])
                ref_id = self._insert(conn, ref["evidence_id"], ref["source_case_id"], ref["target_case_id"],
                                      new_purpose, user_id, reference_id)
                new_ref = self._row(conn, ref_id)
                detail = {"reference_id": ref_id, "resubmit_of": reference_id, "evidence_id": ref["evidence_id"]}
                self._audit_both(conn, self.store, new_ref, user_id, "reference.resubmit", detail)
                return self._public(conn, new_ref)
            except Exception:
                conn.rollback()
                raise

    # ---------- 查询与只读查阅 ----------

    def list_for_case(self, user_id, case_id):
        """案件双向引用台账：incoming=他案引用本案证据，outgoing=本案申请引用他案证据。"""
        with self.store.connect() as conn:
            self.store._member(conn, case_id, user_id)
            incoming = conn.execute(
                "SELECT * FROM evidence_references WHERE source_case_id=? ORDER BY id DESC", (case_id,)
            ).fetchall()
            outgoing = conn.execute(
                "SELECT * FROM evidence_references WHERE target_case_id=? ORDER BY id DESC", (case_id,)
            ).fetchall()
            return {
                "case_id": case_id,
                "incoming": [self._public(conn, r) for r in incoming],
                "outgoing": [self._public(conn, r) for r in outgoing],
            }

    def get_via_reference(self, user_id, evidence_id):
        """受理后目标案件成员只读查看元数据与保管链；不返回内容，不能开箱、移交或派生。"""
        with self.store.connect() as conn:
            ev = self.store._evidence(conn, evidence_id)
            ref = conn.execute(
                """SELECT r.* FROM evidence_references r
                   JOIN case_members m ON m.case_id=r.target_case_id AND m.user_id=? AND m.active=1
                   WHERE r.evidence_id=? AND r.status='accepted'
                   ORDER BY r.id DESC LIMIT 1""",
                (user_id, evidence_id),
            ).fetchone()
            if not ref:
                raise BusinessError("不是案件有效成员", 403, "forbidden")
            result = {k: ev[k] for k in ev.keys() if k != "content"}
            result["legal_hold"] = bool(ev["legal_hold"])
            result["integrity_valid"] = hashlib.sha256(ev["content"]).hexdigest() == ev["sha256"]
            result["events"] = [
                dict(x) for x in conn.execute(
                    "SELECT * FROM custody_events WHERE evidence_id=? ORDER BY sequence", (evidence_id,)
                ).fetchall()
            ]
            result["derived_children"] = [
                dict(x) for x in conn.execute(
                    "SELECT * FROM derivatives WHERE parent_evidence_id=? ORDER BY id", (evidence_id,)
                ).fetchall()
            ]
            result["access"] = "reference"
            result["read_only"] = True
            result["reference_id"] = ref["id"]
            return result
