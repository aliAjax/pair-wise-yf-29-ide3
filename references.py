"""跨案引用规则。

与保管记录（records.py）分开维护：本模块只定义“目标案件能否引用源案件证据”
的申请、核对、退回、重提与受理规则，不修改源证据，也不持有证据内容。

受理核对规则：源证据在申请后若发生移交（保管人变化）、开箱（状态离开 custody）
或法律保留变化，受理即退回，由申请人在目标案件重新提交。受理后目标案件只读
元数据与保管链，不能开箱、移交或派生证据。
"""
from __future__ import annotations

import json
import sqlite3

from records import BusinessError, now

REFERENCE_STATES = {"pending", "accepted", "rejected"}
# 与 custody_events 的事件类型对应，用于报告中的人类可读变化
STATE_LABELS = {
    "pending": "待受理",
    "accepted": "已受理",
    "rejected": "已退回",
}


class ReferenceRules:
    def __init__(self, records):
        self.records = records

    def init_schema(self):
        with self.records.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cross_case_references(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_evidence_id INTEGER NOT NULL REFERENCES evidence(id),
                    source_case_id INTEGER NOT NULL REFERENCES cases(id),
                    target_case_id INTEGER NOT NULL REFERENCES cases(id),
                    applicant_id TEXT NOT NULL REFERENCES users(id),
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
                    source_snapshot TEXT NOT NULL,
                    reviewer_id TEXT REFERENCES users(id),
                    rejection_reason TEXT NOT NULL DEFAULT '',
                    resubmitted_of INTEGER REFERENCES cross_case_references(id),
                    attempt INTEGER NOT NULL DEFAULT 1,
                    submitted_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    CHECK(target_case_id <> source_case_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_reference
                    ON cross_case_references(source_evidence_id,target_case_id)
                    WHERE status IN ('pending','accepted');
                """
            )

    # ---- 内部工具 ----
    @staticmethod
    def _snapshot(row):
        return {"status": row["status"], "current_custodian": row["current_custodian"], "legal_hold": bool(row["legal_hold"])}

    @staticmethod
    def _changes(snapshot, row):
        """返回申请快照与源证据当前状态之间的变化描述；无变化返回 []。"""
        changes = []
        if row["current_custodian"] != snapshot["current_custodian"]:
            changes.append("transferred")
        if row["status"] != snapshot["status"]:
            if row["status"] == "released":
                changes.append("released")
            else:
                changes.append("opened")
        if bool(row["legal_hold"]) != bool(snapshot["legal_hold"]):
            changes.append("hold_changed")
        return changes

    _CHANGE_TEXT = {
        "transferred": "源证据已移交（保管人变化）",
        "opened": "源证据已开箱（封存状态变化）",
        "released": "源证据已释放",
        "hold_changed": "源证据法律保留有变化",
    }

    def _row(self, conn, ref_id):
        row = conn.execute("SELECT * FROM cross_case_references WHERE id=?", (ref_id,)).fetchone()
        if not row:
            raise BusinessError("跨案引用不存在", 404, "not_found")
        return row

    def _view(self, conn, row):
        data = {k: row[k] for k in row.keys()}
        data["source_snapshot"] = json.loads(row["source_snapshot"])
        source = self.records.evidence_row(conn, row["source_evidence_id"])
        source_case = conn.execute("SELECT id,case_number,title FROM cases WHERE id=?", (row["source_case_id"],)).fetchone()
        target_case = conn.execute("SELECT id,case_number,title FROM cases WHERE id=?", (row["target_case_id"],)).fetchone()
        data["status_label"] = STATE_LABELS[row["status"]]
        data["source_case"] = dict(source_case)
        data["target_case"] = dict(target_case)
        data["source_evidence"] = {"id": source["id"], "label": source["label"], "filename": source["filename"],
                                   "case_id": source["case_id"], "sha256": source["sha256"], "size": source["size"]}
        current = self._snapshot(source)
        if row["status"] == "pending":
            data["source_state_current"] = current
            data["verification_changes"] = self._changes(data["source_snapshot"], source)
        elif row["status"] == "accepted":
            changes = self._changes(data["source_snapshot"], source)
            data["source_state_current"] = current
            data["source_changed"] = bool(changes)
            data["source_changes"] = changes
        return data

    # ---- 申请 ----
    def create_reference(self, user_id, evidence_id, target_case_id, purpose, resubmitted_of=None):
        purpose = purpose.strip()
        if len(purpose) < 5:
            raise BusinessError("引用用途至少 5 个字", 422, "purpose_required")
        with self.records.connect() as conn:
            self.records.user(conn, user_id)
            source = self.records.evidence_row(conn, evidence_id)
            target = self.records.case(conn, target_case_id)
            if target["id"] == source["case_id"]:
                raise BusinessError("目标案件与源案件相同，无需跨案引用", 422, "same_case")
            # 申请人必须是目标案件成员；无需是源案件成员
            self.records.member(conn, target_case_id, user_id)
            attempt, previous_id = 1, None
            if resubmitted_of is not None:
                prior = self._row(conn, resubmitted_of)
                if prior["source_evidence_id"] != evidence_id or prior["target_case_id"] != target_case_id:
                    raise BusinessError("重提必须沿用原证据和目标案件", 422, "resubmit_mismatch")
                if prior["status"] != "rejected":
                    raise BusinessError("只有已退回的引用可以重提", 409, "not_rejected")
                attempt, previous_id = prior["attempt"] + 1, prior["id"]
            snapshot = json.dumps(self._snapshot(source), ensure_ascii=False, sort_keys=True)
            try:
                cur = conn.execute(
                    """INSERT INTO cross_case_references(
                           source_evidence_id,source_case_id,target_case_id,applicant_id,purpose,
                           status,source_snapshot,resubmitted_of,attempt,submitted_at)
                       VALUES(?,?,?,?,?, 'pending', ?,?,?,?)""",
                    (evidence_id, source["case_id"], target_case_id, user_id, purpose,
                     snapshot, previous_id, attempt, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("该证据对此目标案件已有待受理或已受理的引用", 409, "reference_active")
            ref_id = cur.lastrowid
            self.records.audit(conn, target_case_id, user_id, "reference.apply",
                               {"reference_id": ref_id, "source_evidence_id": evidence_id, "attempt": attempt})
            self.records.audit(conn, source["case_id"], user_id, "reference.received",
                               {"reference_id": ref_id, "target_case_id": target_case_id, "attempt": attempt})
            return self._view(conn, self._row(conn, ref_id))

    def resubmit(self, user_id, ref_id, purpose):
        with self.records.connect() as conn:
            prior = self._row(conn, ref_id)
            evidence_id, target_case_id = prior["source_evidence_id"], prior["target_case_id"]
        return self.create_reference(user_id, evidence_id, target_case_id, purpose, resubmitted_of=ref_id)

    # ---- 受理人核对 ----
    def review_reference(self, user_id, ref_id, decision, reason=""):
        decision = decision.strip().lower()
        if decision not in {"accept", "return"}:
            raise BusinessError("核对结论必须是 accept 或 return", 422, "invalid_decision")
        reason = reason.strip()
        with self.records.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                ref = self._row(conn, ref_id)
                if ref["status"] != "pending":
                    raise BusinessError("该引用已核对，不能重复受理", 409, "already_reviewed")
                # 受理人必须是源案件保管员
                self.records.member(conn, ref["source_case_id"], user_id, {"custodian"})
                source = self.records.evidence_row(conn, ref["source_evidence_id"])
                snapshot = json.loads(ref["source_snapshot"])
                changes = self._changes(snapshot, source)
                if decision == "accept" and changes:
                    # 核对未通过：系统直接退回并记录具体原因，等待申请人重提
                    status, rejection = "rejected", "；".join(self._CHANGE_TEXT[c] for c in changes)
                elif decision == "return":
                    if len(reason) < 3:
                        raise BusinessError("退回原因至少 3 个字", 422, "reason_required")
                    status, rejection = "rejected", reason
                else:
                    status, rejection = "accepted", ""
                conn.execute(
                    "UPDATE cross_case_references SET status=?, reviewer_id=?, rejection_reason=?, reviewed_at=? WHERE id=?",
                    (status, user_id, rejection, now(), ref_id),
                )
                self.records.audit(conn, ref["source_case_id"], user_id, f"reference.{status}",
                                   {"reference_id": ref_id, "reason": rejection})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return self._view(conn, self._row(conn, ref_id))

    # ---- 查询 ----
    def _accessible(self, conn, row, user_id):
        """源案件或目标案件成员均可查看。"""
        self.records.user(conn, user_id)
        ok = conn.execute(
            """SELECT 1 FROM case_members
               WHERE active=1 AND user_id=? AND case_id IN (?,?) LIMIT 1""",
            (user_id, row["source_case_id"], row["target_case_id"]),
        ).fetchone()
        if not ok:
            raise BusinessError("不是源案件或目标案件成员，无权查看该引用", 403, "forbidden")

    def get_reference(self, user_id, ref_id):
        with self.records.connect() as conn:
            row = self._row(conn, ref_id)
            self._accessible(conn, row, user_id)
            return self._view(conn, row)

    def list_for_case(self, user_id, case_id, direction=None):
        if direction not in (None, "incoming", "outgoing"):
            raise BusinessError("direction 只能是 incoming 或 outgoing", 422, "invalid_direction")
        with self.records.connect() as conn:
            self.records.member(conn, case_id, user_id)
            sql, params = "SELECT * FROM cross_case_references WHERE ", []
            clauses = []
            if direction in (None, "incoming"):
                clauses.append("source_case_id=?")
                params.append(case_id)
            if direction in (None, "outgoing"):
                clauses.append("target_case_id=?")
                params.append(case_id)
            rows = conn.execute(sql + " OR ".join(clauses) + " ORDER BY id", params).fetchall()
            return [self._view(conn, r) for r in rows]

    def referenced_evidence(self, user_id, ref_id):
        """受理后目标案件只读查看源证据元数据和保管链；永远不返回证据内容。"""
        with self.records.connect() as conn:
            row = self._row(conn, ref_id)
            self._accessible(conn, row, user_id)
            if row["status"] != "accepted":
                raise BusinessError("引用受理后才能查看源证据", 409, "not_accepted")
            source = self.records.evidence_row(conn, row["source_evidence_id"])
            view = self.records.evidence_view(conn, source, include_content=False)
            view["content_b64"] = None  # 显式声明：跨案引用不提供原件
            view["reference"] = {"id": row["id"], "target_case_id": row["target_case_id"], "purpose": row["purpose"]}
            view["read_only"] = True
            return view

    def report_section(self, user_id, case_id):
        """案件报告中的引用章节：列出引用状态与退回原因。"""
        with self.records.connect() as conn:
            self.records.member(conn, case_id, user_id)

            def item(row, direction):
                other_id = row["target_case_id"] if direction == "incoming" else row["source_case_id"]
                other = conn.execute("SELECT id,case_number,title FROM cases WHERE id=?", (other_id,)).fetchone()
                source = self.records.evidence_row(conn, row["source_evidence_id"])
                snapshot = json.loads(row["source_snapshot"])
                entry = {
                    "id": row["id"], "direction": direction,
                    "status": row["status"], "status_label": STATE_LABELS[row["status"]],
                    "other_case": dict(other),
                    "source_evidence_id": row["source_evidence_id"], "source_evidence_label": source["label"],
                    "purpose": row["purpose"], "applicant_id": row["applicant_id"],
                    "reviewer_id": row["reviewer_id"], "rejection_reason": row["rejection_reason"],
                    "attempt": row["attempt"], "submitted_at": row["submitted_at"], "reviewed_at": row["reviewed_at"],
                }
                if row["status"] == "accepted":
                    changes = self._changes(snapshot, source)
                    entry["source_changed_after_accept"] = bool(changes)
                    entry["source_changes"] = changes
                return entry

            incoming = [item(r, "incoming") for r in conn.execute(
                "SELECT * FROM cross_case_references WHERE source_case_id=? ORDER BY id", (case_id,)).fetchall()]
            outgoing = [item(r, "outgoing") for r in conn.execute(
                "SELECT * FROM cross_case_references WHERE target_case_id=? ORDER BY id", (case_id,)).fetchall()]
            return {
                "incoming_count": len(incoming), "outgoing_count": len(outgoing),
                "incoming": incoming, "outgoing": outgoing,
            }
