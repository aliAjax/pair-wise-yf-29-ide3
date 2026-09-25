import base64
import http.client
import json
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import BusinessError, CustodyServer, CustodyStore, Handler
from crossref import CrossReferenceService


class CrossReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CustodyStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.xref = CrossReferenceService(self.store)
        self.xref.init_schema()
        # 源案件：custodian1 为保管员，auditor1 为审计员
        self.source = self.store.create_case("custodian1", "CASE-2026-101", "源头案件")
        self.store.add_member("custodian1", self.source["id"], "auditor1", "auditor")
        # 目标案件：custodian2 创建，analyst1 为分析员（申请人）
        self.target = self.store.create_case("custodian2", "CASE-2026-202", "引用案件")
        self.store.add_member("custodian2", self.target["id"], "analyst1", "analyst")
        self.retention = (date.today() + timedelta(days=3650)).isoformat()
        self.evidence = self.store.ingest_evidence(
            "custodian1", self.source["id"], "E-101", "ledger.xlsx",
            base64.b64encode(b"ledger bytes").decode(), self.retention, "custodian1",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, user="analyst1", purpose="并案侦查需要比对流水"):
        return self.xref.create_request(user, self.evidence["id"], self.target["id"], purpose)

    def test_accept_then_read_only_view_blocks_mutations(self):
        ref = self.request()
        self.assertEqual(ref["status"], "pending")
        self.assertEqual(ref["drift"], [])
        # 受理前目标案件成员不可见
        with self.assertRaises(BusinessError) as ctx:
            self.xref.get_via_reference("analyst1", self.evidence["id"])
        self.assertEqual(ctx.exception.status, 403)
        accepted = self.xref.review("custodian1", ref["id"], "accept")
        self.assertEqual(accepted["status"], "accepted")
        view = self.xref.get_via_reference("analyst1", self.evidence["id"])
        self.assertTrue(view["read_only"])
        self.assertEqual(view["access"], "reference")
        self.assertEqual(view["reference_id"], ref["id"])
        self.assertEqual(view["sha256"], self.evidence["sha256"])
        self.assertEqual(len(view["events"]), 1)
        self.assertNotIn("content", view)
        self.assertNotIn("content_b64", view)
        # 目标案件成员不能开箱、移交或派生
        for call in (
            lambda: self.store.open_evidence("analyst1", self.evidence["id"], "B 区证物室"),
            lambda: self.store.transfer("analyst1", self.evidence["id"], "analyst1", "某处"),
            lambda: self.store.derive("analyst1", self.evidence["id"], "分析提取方法", "D-1", "d.bin",
                                      base64.b64encode(b"x").decode()),
        ):
            with self.assertRaises(BusinessError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 403)
        # 保管库本身仍拒绝非成员（只读访问只在引用服务中放行）
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_evidence("analyst1", self.evidence["id"])
        self.assertEqual(ctx.exception.status, 403)

    def test_transfer_before_review_forces_return_and_resubmit(self):
        ref = self.request()
        self.store.transfer("custodian1", self.evidence["id"], "custodian2", "法院证物库", "案件调取")
        # 即使受理人点 accept，系统核对到移交也强制退回
        reviewed = self.xref.review("custodian1", ref["id"], "accept")
        self.assertEqual(reviewed["status"], "returned")
        self.assertIn("移交", reviewed["return_reason"])
        # 只能由原申请人重提，生成新申请并关联原申请
        with self.assertRaises(BusinessError) as ctx:
            self.xref.resubmit("custodian2", ref["id"])
        self.assertEqual(ctx.exception.status, 403)
        again = self.xref.resubmit("analyst1", ref["id"])
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["resubmit_of"], ref["id"])
        self.assertEqual(again["drift"], [])
        accepted = self.xref.review("custodian1", again["id"], "accept")
        self.assertEqual(accepted["status"], "accepted")

    def test_open_before_review_forces_return(self):
        ref = self.request()
        self.store.open_evidence("custodian1", self.evidence["id"], "A 区证物室")
        reviewed = self.xref.review("custodian1", ref["id"], "accept")
        self.assertEqual(reviewed["status"], "returned")
        self.assertIn("开箱", reviewed["return_reason"])

    def test_legal_hold_change_forces_return(self):
        ref = self.request()
        self.store.set_hold("auditor1", self.evidence["id"], True, "诉讼保全要求")
        reviewed = self.xref.review("custodian1", ref["id"], "accept")
        self.assertEqual(reviewed["status"], "returned")
        self.assertIn("法律保留", reviewed["return_reason"])

    def test_manual_return_requires_reason(self):
        ref = self.request()
        with self.assertRaises(BusinessError) as ctx:
            self.xref.review("custodian1", ref["id"], "return")
        self.assertEqual(ctx.exception.code, "reason_required")
        returned = self.xref.review("custodian1", ref["id"], "return", "用途说明不充分")
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(returned["return_reason"], "用途说明不充分")
        # 重提时可更新用途
        again = self.xref.resubmit("analyst1", ref["id"], "补充用途：并案侦查需要比对流水")
        self.assertEqual(again["purpose"], "补充用途：并案侦查需要比对流水")
        with self.assertRaises(BusinessError) as ctx:
            self.xref.resubmit("analyst1", again["id"])
        self.assertEqual(ctx.exception.code, "not_returned")

    def test_request_validation_reviewer_and_duplicates(self):
        with self.assertRaises(BusinessError) as ctx:
            self.xref.create_request("outsider", self.evidence["id"], self.target["id"], "并案侦查需要")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.xref.create_request("analyst1", self.evidence["id"], self.source["id"], "并案侦查需要")
        self.assertEqual(ctx.exception.code, "same_case")
        with self.assertRaises(BusinessError):
            self.xref.create_request("analyst1", self.evidence["id"], self.target["id"], "短")
        ref = self.request()
        with self.assertRaises(BusinessError) as ctx:
            self.request()
        self.assertEqual(ctx.exception.code, "reference_exists")
        # 只有源案件保管员可以受理
        with self.assertRaises(BusinessError) as ctx:
            self.xref.review("analyst1", ref["id"], "accept")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.xref.review("custodian1", ref["id"], "maybe")
        self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(BusinessError) as ctx:
            self.xref.review("custodian1", ref["id"] + 999, "accept")
        self.assertEqual(ctx.exception.status, 404)

    def test_report_lists_reference_status_and_return_reason(self):
        ref = self.request()
        self.store.open_evidence("custodian1", self.evidence["id"], "A 区证物室")
        self.xref.review("custodian1", ref["id"], "accept")
        outgoing = self.xref.list_for_case("analyst1", self.target["id"])["outgoing"]
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(outgoing[0]["status"], "returned")
        self.assertIn("开箱", outgoing[0]["return_reason"])
        incoming = self.xref.list_for_case("custodian1", self.source["id"])["incoming"]
        self.assertEqual(incoming[0]["id"], ref["id"])
        # 非案件成员看不到台账
        with self.assertRaises(BusinessError) as ctx:
            self.xref.list_for_case("outsider", self.target["id"])
        self.assertEqual(ctx.exception.status, 403)


class CrossReferenceHttpTests(unittest.TestCase):
    def setUp(self):
        self._orig_log = Handler.log_message
        Handler.log_message = lambda *a: None
        self.tmp = tempfile.TemporaryDirectory()
        store = CustodyStore(Path(self.tmp.name) / "http.db")
        store.seed()
        xref = CrossReferenceService(store)
        xref.init_schema()
        self.server = CustodyServer(("127.0.0.1", 0), store, xref)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.store = store

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        Handler.log_message = self._orig_log
        self.tmp.cleanup()

    def api(self, method, path, user, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, json.dumps(body or {}),
                     {"X-User-Id": user, "Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, payload

    def test_reference_flow_and_report_over_http(self):
        source = self.store.create_case("custodian1", "CASE-HTTP-1", "源案件")
        target = self.store.create_case("custodian2", "CASE-HTTP-2", "目标案件")
        self.store.add_member("custodian2", target["id"], "analyst1", "analyst")
        ev = self.store.ingest_evidence(
            "custodian1", source["id"], "E-1", "a.bin", base64.b64encode(b"data").decode(),
            (date.today() + timedelta(days=365)).isoformat(),
        )
        # 未受理：目标案件成员 403
        status, _ = self.api("GET", f"/api/evidence/{ev['id']}", "analyst1")
        self.assertEqual(status, 403)
        # 申请
        status, ref = self.api("POST", f"/api/evidence/{ev['id']}/references", "analyst1",
                               {"target_case_id": target["id"], "purpose": "并案侦查需要比对"})
        self.assertEqual(status, 201)
        self.assertEqual(ref["status"], "pending")
        # 非源案件保管员受理被拒
        status, body = self.api("POST", f"/api/references/{ref['id']}/review", "analyst1", {"decision": "accept"})
        self.assertEqual(status, 403)
        # 源案件保管员受理
        status, reviewed = self.api("POST", f"/api/references/{ref['id']}/review", "custodian1", {"decision": "accept"})
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["status"], "accepted")
        # 受理后只读查看；带内容参数也不返回内容
        status, view = self.api("GET", f"/api/evidence/{ev['id']}?content=1", "analyst1")
        self.assertEqual(status, 200)
        self.assertTrue(view["read_only"])
        self.assertEqual(view["access"], "reference")
        self.assertNotIn("content_b64", view)
        self.assertEqual(len(view["events"]), 1)
        # 跨案引用不能开箱
        status, body = self.api("POST", f"/api/evidence/{ev['id']}/open", "analyst1", {"location": "B 区"})
        self.assertEqual(status, 403)
        # 双向报告均列出引用状态
        status, report = self.api("GET", f"/api/cases/{target['id']}/report", "analyst1")
        self.assertEqual(status, 200)
        self.assertEqual(report["references"]["outgoing"][0]["status"], "accepted")
        status, report = self.api("GET", f"/api/cases/{source['id']}/report", "custodian1")
        self.assertEqual(status, 200)
        self.assertEqual(report["references"]["incoming"][0]["status"], "accepted")
        # 引用台账接口
        status, listing = self.api("GET", f"/api/cases/{target['id']}/references", "custodian2")
        self.assertEqual(status, 200)
        self.assertEqual(listing["outgoing"][0]["id"], ref["id"])


if __name__ == "__main__":
    unittest.main()
