# -*- coding: utf-8 -*-
"""按「目标分组 + 数据源覆盖度」组装多管道请求（与前端修正后的逻辑一致）并触发生成。"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"


def call(method, path, body=None):
    """调用后端 API（HTTP 错误时打印响应体，便于定位）。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"[HTTP {exc.code}] {method} {path}\n{detail[:1500]}")


ds_list = (call("GET", "/datasources") or {}).get("data", {}).get("items", [])
tgs = (call("GET", "/targets") or {}).get("data", {}).get("items", [])
maps = (call("GET", "/mappings") or {}).get("data", {}).get("items", [])

# 资产名 → 候选数据源
asset_cands = {}
for d in ds_list:
    for a in (call("GET", f"/datasources/{d['id']}/assets") or {}).get("data", {}).get("items", []):
        asset_cands.setdefault(a.get("name"), []).append({"dsId": d["id"], "dsType": d.get("type")})

# 目标表 → 目标记录
tgt_by_table = {}
for t in tgs:
    tgt_by_table.setdefault(t.get("table"), t)

by_target = {}
for m in maps:
    tg = tgt_by_table.get(m.get("target_table")) or {}
    by_target.setdefault(str(tg.get("target_id") or tg.get("id") or m.get("target_table")), []).append(m)

groups = []
for ms in by_target.values():
    score = {}
    for m in ms:
        for c in asset_cands.get(m.get("source"), []):
            score[c["dsId"]] = score.get(c["dsId"], 0) + 1
    best = sorted(score.items(), key=lambda kv: -kv[1])[0] if score else ("", 0)
    ds_id = best[0]
    ds_type = next((c["dsType"] for c in asset_cands.get(ms[0].get("source"), [])
                    if c["dsId"] == ds_id), "")
    tg = tgt_by_table.get(ms[0].get("target_table")) or {}
    groups.append({
        "source_type": ds_type or "SQL", "source_id": ds_id or None,
        "target_type": ms[0].get("target_type") or ("SOAP" if tg.get("type") == "SOAP" else "DB"),
        "target_id": tg.get("target_id") or tg.get("id"),
        "mappings": ms,
    })

print("组装出的管道组:")
for g in groups:
    print("  源", g["source_id"], g["source_type"], "→ 目标", g["target_id"], g["target_type"],
          "| 映射", [m.get("id") for m in g["mappings"]])

resp = call("POST", "/pipelines/generate", {"pipelines": groups})
print("code:", resp.get("code"), "message:", resp.get("message"))
data = resp.get("data") or {}
v = data.get("validation") or {}
print("result:", data.get("result"), "| validation ok:", v.get("ok"),
      "| checks:", list((v.get("results") or {}).keys()))
print("ai:", json.dumps(data.get("ai"), ensure_ascii=False)[:300])
