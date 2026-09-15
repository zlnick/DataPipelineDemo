# -*- coding: utf-8 -*-
"""按当前已注册的 SQL 源 / FHIR 目标 / 已保存映射重新生成单管道（模拟界面 Pipelines 页「生成」）。

用于修复生成链路后重跑（不重复登记数据源/目标/AI 匹配）。
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"[FAIL] {method} {path} -> HTTP {exc.code}\n{exc.read().decode()[:800]}")
    if payload.get("code") != 0:
        raise SystemExit(f"[FAIL] {method} {path} -> {payload.get('message')}")
    return payload.get("data")


ds = [d for d in call("GET", "/datasources").get("items", []) if d.get("type") == "SQL"][0]
tg = [t for t in call("GET", "/targets").get("items", [])
      if (t.get("type") or "").upper() == "FHIR"][0]
mappings = call("GET", "/mappings").get("items", [])
print("source:", ds["id"], ds.get("name"), "| target:", tg.get("target_id"),
      tg.get("target_name"), "| mappings:", len(mappings))
gen = call("POST", "/pipelines/generate", {
    "mappings": mappings, "source_id": ds["id"], "target_id": tg.get("target_id"),
    "source_type": "SQL", "target_type": "FHIR"})
v = gen.get("validation") or {}
print("result:", gen.get("result"), "| validation ok:", v.get("ok"),
      "| errors:", v.get("error_count"), "| checks:", list((v.get("results") or {}).keys()))
print("ai:", json.dumps(gen.get("ai"), ensure_ascii=False))
topo = ((v.get("results") or {}).get("pipeline") or {}).get("components")
print("topology components:", topo)
