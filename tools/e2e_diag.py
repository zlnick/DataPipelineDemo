# -*- coding: utf-8 -*-
"""诊断：读取管道状态与最近消息（结果写文件，避免 shell 引号问题）。"""
import json
import urllib.request


def get(path):
    with urllib.request.urlopen("http://localhost:5001/api" + path, timeout=120) as resp:
        return json.loads(resp.read().decode())


lines = []
st = (get("/pipelines/status") or {}).get("data") or {}
lines.append("status: " + json.dumps(st, ensure_ascii=False)[:500])
items = ((get("/pipelines/logs?count=8") or {}).get("data") or {}).get("items") or []
for it in items:
    lines.append("%s | %s -> %s | %s" % (it.get("time"), it.get("source"),
                                         it.get("target"), it.get("status")))
open("/tmp/diag2.log", "w").write("\n".join(lines))
print("\n".join(lines))
