import base64
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyService


class CrossCaseReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = CustodyService(Path(self.tmp.name) / "test.db")
        self.svc.seed()
        # 源案件 A：custodian1 创建，custodian2 保管员、analyst1 分析员、auditor1 审计员
        self.case_a = self.svc.create_case("custodian1", "CASE-A", "源案件跨境资金调查")
        self.svc.add_member("custodian1", self.case_a["id"], "custodian2", "custodian")
        self.svc.add_member("custodian1", self.case_a["id"], "analyst1", "analyst")
        self.svc.add_member("custodian1", self.case_a["id"], "auditor1", "auditor")
        # 目标案件 B：analyst1 创建（在 B 中是保管员），outsider 仅为 B 成员
        self.case_b = self.svc.create_case("analyst1", "CASE-B", "目标案件并案审查")
        self.svc.add_member("analyst1", self.case_b["id"], "outsider", "auditor")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()
        self.ev = self.svc.ingest_evidence(
            "custodian1", self.case_a["id"], "E-A1", "statement.csv",
            base64.b64encode(b"bank statement original bytes").decode(),
            self.retention, "custodian1",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _apply(self, user="analyst1", purpose="并案资金流向比对调查使用"):
        return self.svc.references.create_reference(user, self.ev["id"], self.case_b["id"], purpose)

    def test_apply_validation_and_permissions(self):
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.create_reference("analyst1", self.ev["id"], self.case_a["id"], "同案件引用用途说明")
        self.assertEqual(ctx.exception.code, "same_case")
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.create_reference("custodian1", self.ev["id"], self.case_b["id"], "太短")
        self.assertEqual(ctx.exception.code, "purpose_required")
        # 非目标案件成员不能发起引用
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.create_reference("custodian1", self.ev["id"], 99999, "不存在的目标案件用途")
        self.assertEqual(ctx.exception.status, 404)
        ref = self._apply()
        self.assertEqual(ref["status"], "pending")
        self.assertEqual(ref["attempt"], 1)
        self.assertFalse(ref["verification_changes"])
        # 待受理/已受理的引用不能重复发起
        with self.assertRaises(BusinessError) as ctx:
            self._apply()
        self.assertEqual(ctx.exception.code, "reference_active")

    def test_accept_then_target_case_reads_metadata_and_chain_only(self):
        ref = self._apply()
        accepted = self.svc.references.review_reference("custodian1", ref["id"], "accept")
        self.assertEqual(accepted["status"], "accepted")
        # 目标案件成员（含仅在目标案件的 outsider）可读元数据与保管链
        for viewer in ("analyst1", "outsider"):
            view = self.svc.references.referenced_evidence(viewer, ref["id"])
            self.assertTrue(view["read_only"])
            self.assertIsNone(view["content_b64"])
            self.assertEqual(view["sha256"], self.ev["sha256"])
            self.assertTrue(any(e["event_type"] == "INGEST" for e in view["events"]))
        # 目标案件不能直接开箱、移交、派生，也不能走源案件证据详情接口
        for action in ("open", "transfer"):
            with self.assertRaises(BusinessError) as ctx:
                if action == "open":
                    self.svc.open_evidence("outsider", self.ev["id"], "B 案件证物室")
                else:
                    self.svc.transfer("outsider", self.ev["id"], "outsider", "B 案件位置")
            self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.svc.get_evidence("outsider", self.ev["id"])
        self.assertEqual(ctx.exception.status, 403)
        # 源/目标案件之外的用户看不到引用
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.get_reference("nobody", ref["id"])
        self.assertEqual(ctx.exception.status, 401)
        # 源案件成员可以看到引用记录
        self.assertEqual(self.svc.references.get_reference("auditor1", ref["id"])["status"], "accepted")

    def test_transfer_during_review_forces_return_and_resubmit(self):
        ref = self._apply()
        self.svc.transfer("custodian1", self.ev["id"], "custodian2", "法院证物库", "封存后移交")
        result = self.svc.references.review_reference("custodian2", ref["id"], "accept")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("移交", result["rejection_reason"])
        # 退回后可重提：新单据 attempt=2 并关联原单据
        again = self.svc.references.resubmit("analyst1", ref["id"], "移交完成后重新引用用于并案比对")
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["attempt"], 2)
        self.assertEqual(again["resubmitted_of"], ref["id"])
        accepted = self.svc.references.review_reference("custodian2", again["id"], "accept")
        self.assertEqual(accepted["status"], "accepted")

    def test_open_during_review_forces_return(self):
        ref = self._apply()
        self.svc.open_evidence("custodian1", self.ev["id"], "A 区证物室", "两名人员在场")
        result = self.svc.references.review_reference("custodian1", ref["id"], "accept")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("开箱", result["rejection_reason"])

    def test_hold_change_during_review_forces_return(self):
        ref = self._apply()
        self.svc.set_hold("auditor1", self.ev["id"], True, "诉讼保全法律保留要求")
        result = self.svc.references.review_reference("custodian1", ref["id"], "accept")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("法律保留", result["rejection_reason"])

    def test_manual_return_requires_reason_and_reviewer_rules(self):
        ref = self._apply()
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.review_reference("custodian1", ref["id"], "return", "no")
        self.assertEqual(ctx.exception.code, "reason_required")
        # 非源案件保管员不能受理核对
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.review_reference("analyst1", ref["id"], "accept")
        self.assertEqual(ctx.exception.status, 403)
        returned = self.svc.references.review_reference("custodian1", ref["id"], "return", "用途与办案无关")
        self.assertEqual(returned["status"], "rejected")
        self.assertEqual(returned["rejection_reason"], "用途与办案无关")
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.review_reference("custodian1", ref["id"], "accept")
        self.assertEqual(ctx.exception.code, "already_reviewed")
        with self.assertRaises(BusinessError) as ctx:
            self.svc.references.referenced_evidence("analyst1", ref["id"])
        self.assertEqual(ctx.exception.code, "not_accepted")

    def test_report_lists_reference_status_reasons_and_drift(self):
        ref = self._apply()
        self.svc.transfer("custodian1", self.ev["id"], "custodian2", "法院证物库")
        self.svc.references.review_reference("custodian2", ref["id"], "accept")
        again = self.svc.references.resubmit("analyst1", ref["id"], "重新提交用于并案资金比对")
        self.svc.references.review_reference("custodian2", again["id"], "accept")
        # 受理后源证据再开箱：报告标注漂移
        self.svc.open_evidence("custodian2", self.ev["id"], "法庭准备室", "庭审需要")

        source_report = self.svc.report("custodian1", self.case_a["id"])
        incoming = source_report["references"]["incoming"]
        self.assertEqual(len(incoming), 2)
        self.assertEqual({x["status"] for x in incoming}, {"rejected", "accepted"})
        rejected_item = next(x for x in incoming if x["status"] == "rejected")
        self.assertIn("移交", rejected_item["rejection_reason"])
        accepted_item = next(x for x in incoming if x["status"] == "accepted")
        self.assertTrue(accepted_item["source_changed_after_accept"])
        self.assertIn("opened", accepted_item["source_changes"])

        target_report = self.svc.report("analyst1", self.case_b["id"])
        outgoing = target_report["references"]["outgoing"]
        self.assertEqual(target_report["references"]["outgoing_count"], 2)
        self.assertEqual([x["attempt"] for x in outgoing], [1, 2])
        # 受理后的引用仍可读，且视图标注源已变化
        view = self.svc.references.get_reference("outsider", again["id"])
        self.assertTrue(view["source_changed"])
        self.assertIn("opened", view["source_changes"])

    def test_case_reference_listing_directions(self):
        self._apply()
        incoming = self.svc.references.list_for_case("custodian1", self.case_a["id"], "incoming")
        outgoing = self.svc.references.list_for_case("analyst1", self.case_b["id"], "outgoing")
        self.assertEqual(len(incoming), 1)
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(incoming[0]["source_case"]["case_number"], "CASE-A")
        self.assertEqual(outgoing[0]["target_case"]["case_number"], "CASE-B")


if __name__ == "__main__":
    unittest.main()
