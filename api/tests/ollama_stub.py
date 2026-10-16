"""Deterministic local Ollama HTTP stub for browser acceptance tests only."""

import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        prompt = request.get("prompt", "")
        if "requirements" in request["format"].get("properties", {}):
            match = re.search(r"文档 ID: ([a-f0-9]+)", prompt)
            value = {"requirements": [{"id": "REQ-1", "title": "登录", "summary": "有效账号登录",
                                        "source_ids": [match.group(1)], "acceptance": ["进入首页"], "methods": ["EP"]}],
                     "ambiguities": [{"id": "AQ-1", "description": "失败次数限制", "critical": True, "decision": ""}],
                     "risks": ["重复失败"]}
        elif "categories" in request["format"].get("properties", {}):
            value = {"categories": {key: None for key in ("business_module", "business_scenario", "test_dimension", "endpoint_type")},
                     "level": "1", "smoke": None, "rationale": "模型候选需人工核对", "questions": ["确认分类与冒烟值"]}
        else:
            value = {"cases": [{"module": ["登录"], "title": "有效账号登录", "priority": "P1",
                                "precondition": "账号存在", "steps": [{"action": "输入正确账号", "expected": "进入首页"}],
                                "requirement_ids": ["REQ-1"], "method": "EP", "label": "Web", "remark": "", "smoke": ""}]}
        body = json.dumps({"response": json.dumps(value, ensure_ascii=False)}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 11435), Handler).serve_forever()
