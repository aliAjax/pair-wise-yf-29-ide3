"""法律证据保管与流转后台：接口装配层。

保管记录逻辑在 records.py，跨案引用规则在 references.py，页面展示在 web/index.html，
三者分开维护；本模块只负责 HTTP 路由与装配。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from records import BASE_DIR, DEFAULT_DB, BusinessError, CustodyRecords, now
from references import ReferenceRules

# 向后兼容：历史调用方（含测试）仍从 app 导入这些名字
CustodyStore = CustodyRecords


class CustodyService:
    """组合保管记录与跨案引用规则，供 HTTP 层调用。"""

    def __init__(self, db_path=DEFAULT_DB):
        self.records = CustodyRecords(db_path)
        self.references = ReferenceRules(self.records)

    def init_schema(self):
        self.records.init_schema()
        self.references.init_schema()

    def seed(self):
        self.init_schema()
        self.records.seed()

    def report(self, user_id, case_id):
        payload = self.records.report(user_id, case_id)
        payload["references"] = self.references.report_section(user_id, case_id)
        return payload

    def __getattr__(self, name):
        # 其余方法直接委托给保管记录模块
        return getattr(self.records, name)


class Handler(BaseHTTPRequestHandler):
    server_version = "EvidenceCustody/1.1"
    def _store(self): return self.server.store  # type: ignore[attr-defined]
    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try: data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError): raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict): raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data
    def _send(self, status, payload):
        body=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def _dispatch(self, method):
        path=urlparse(self.path).path.rstrip("/") or "/"; parts=[p for p in path.split("/") if p]
        query=parse_qs(urlparse(self.path).query)
        user=self.headers.get("X-User-Id",""); store=self._store()
        if method=="GET" and path=="/":
            body=(BASE_DIR/"web"/"index.html").read_bytes(); self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if method=="GET" and path=="/health": return self._send(200,{"ok":True})
        if parts==["api","cases"] and method=="POST":
            d=self._body(); return self._send(201,store.create_case(user,d.get("case_number",""),d.get("title","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="members" and method=="POST":
            d=self._body(); return self._send(201,store.add_member(user,int(parts[2]),d.get("user_id",""),d.get("role","")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="evidence" and method=="POST":
            d=self._body(); return self._send(201,store.ingest_evidence(user,int(parts[2]),d.get("label",""),d.get("filename",""),d.get("content_b64",""),d.get("retention_until",""),d.get("custodian")))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="report" and method=="GET":
            return self._send(200,store.report(user,int(parts[2])))
        if len(parts)==4 and parts[:2]==["api","cases"] and parts[3]=="references" and method=="GET":
            direction = query.get("direction", [None])[0]
            return self._send(200,store.references.list_for_case(user,int(parts[2]),direction))
        if len(parts)>=3 and parts[:2]==["api","references"]:
            ref_id=int(parts[2])
            if len(parts)==3 and method=="GET": return self._send(200,store.references.get_reference(user,ref_id))
            if len(parts)==4 and parts[3]=="evidence" and method=="GET":
                return self._send(200,store.references.referenced_evidence(user,ref_id))
            if len(parts)==4 and method=="POST":
                d=self._body()
                if parts[3]=="review": return self._send(200,store.references.review_reference(user,ref_id,d.get("decision",""),d.get("reason","")))
                if parts[3]=="resubmit": return self._send(201,store.references.resubmit(user,ref_id,d.get("purpose","")))
        if parts==["api","references"] and method=="POST":
            d=self._body()
            return self._send(201,store.references.create_reference(user,int(d.get("evidence_id","0")),int(d.get("target_case_id","0")),d.get("purpose","")))
        if len(parts)>=3 and parts[:2]==["api","evidence"]:
            evidence_id=int(parts[2])
            if len(parts)==3 and method=="GET": return self._send(200,store.get_evidence(user,evidence_id,bool(query)))
            if len(parts)==4 and method=="POST":
                d=self._body()
                if parts[3]=="transfer": return self._send(200,store.transfer(user,evidence_id,d.get("to_person",""),d.get("location",""),d.get("note","")))
                if parts[3]=="open": return self._send(200,store.open_evidence(user,evidence_id,d.get("location",""),d.get("note","")))
                if parts[3]=="derive": return self._send(201,store.derive(user,evidence_id,d.get("method",""),d.get("label",""),d.get("filename",""),d.get("content_b64","")))
                if parts[3]=="release": return self._send(200,store.release(user,evidence_id,d.get("recipient",""),d.get("note","")))
                if parts[3]=="hold": return self._send(200,store.set_hold(user,evidence_id,bool(d.get("hold")),d.get("reason","")))
        raise BusinessError("接口不存在",404,"not_found")
    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status,{"error":{"code":exc.code,"message":exc.message}})
        except (ValueError,TypeError): self._send(400,{"error":{"code":"invalid_path","message":"路径参数格式错误"}})
        except Exception as exc: self._send(500,{"error":{"code":"internal_error","message":str(exc)}})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._send(405,{"error":{"code":"immutable_audit","message":"证据和保管记录不提供删除接口"}})
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class CustodyServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,store): self.store=store; super().__init__(address,Handler)


def main():
    parser=argparse.ArgumentParser(description="法律证据保管与流转后台")
    parser.add_argument("--db",default=str(DEFAULT_DB)); parser.add_argument("--port",type=int,default=8105)
    parser.add_argument("--init",action="store_true"); parser.add_argument("--seed",action="store_true")
    args=parser.parse_args(); store=CustodyService(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server=CustodyServer(("127.0.0.1",args.port),store); print(f"证据保管系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__=="__main__": main()
