# -*- coding: utf-8 -*-
"""硬证据：管道**复用/重建**后是否丢组件 —— 逐项比对「存储定义 vs Production 组件清单」。

判定口径：对每个管道实例，其**存储定义**（`render_components`，退 `components`；再并上
`ai_components` 里的基础设施）里的每个组件名，都必须在 Production 里存在（允许 `__{类别}` 后缀）。
缺失清单为空 = **零丢件**。
"""
import json
import sys
import urllib.error
import urllib.request

API = "http://localhost:5001/api"


def get(p):
    with urllib.request.urlopen(API + p, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def post(p, payload, timeout=2400):
    req = urllib.request.Request(API + p, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def items() -> list[str]:
    return sorted(i.get("name") for i in ((get("/pipelines/items").get("data") or {}).get("items") or []))


def insts() -> dict:
    return {i["id"]: i for i in ((get("/pipelines/instances").get("data") or {}).get("items") or [])}


def missing_of(rec: dict, prod: list[str]) -> list[str]:
    """该实例存储定义里、Production 中**找不到**的组件名（允许类别后缀）。"""
    out: list[str] = []
    seen: set[str] = set()
    for src in ("render_components", "components", "ai_components"):
        for c in (rec.get(src) or []):
            if not isinstance(c, dict):
                continue
            n = str(c.get("name") or "")
            if not n or n in seen:
                continue
            seen.add(n)
            if n in prod or any(x.startswith(n + "__") for x in prod):
                continue
            out.append(n)
    return out


def audit(tag: str) -> bool:
    prod, ins = items(), insts()
    print("  [%s] Production 组件 %d 个" % (tag, len(prod)))
    ok = True
    for pid, rec in ins.items():
        miss = missing_of(rec, prod)
        flag = "✓" if not miss else "✗ 缺失 %s" % miss
        if miss:
            ok = False
        print("      %-24s %-10s 定义件数=%d → %s" % (
            pid, rec.get("status"),
            len({str(c.get("name")) for s in ("render_components", "components", "ai_components")
                 for c in (rec.get(s) or []) if isinstance(c, dict)}), flag))
    print("  [%s] 结论：%s" % (tag, "零丢件 ✓" if ok else "有丢件 ✗"))
    return ok


maps = (get("/mappings").get("data") or {}).get("items", [])
tgt = {}
for t in (get("/targets").get("data") or {}).get("items", []):
    k = "%s:%s" % (str(t.get("type") or "").upper(), t.get("table"))
    if t.get("table"):
        tgt.setdefault(k, t.get("target_id") or t.get("id"))
ds_type = {str(d.get("id")): (d.get("type") or "")
           for d in (get("/datasources").get("data") or {}).get("items", [])}
groups: dict[str, dict] = {}
for m in maps:
    tt = str(m.get("target_type") or "").upper()
    tid = tgt.get("%s:%s" % (tt, m.get("target_table"))) or "%s:%s" % (tt, m.get("target_table"))
    g = groups.setdefault("%s|%s" % (m.get("source_id"), tid),
                          {"source_type": ds_type.get(str(m.get("source_id")), ""),
                           "source_id": m.get("source_id"), "target_type": tt,
                           "target_id": tid, "mappings": []})
    g["mappings"].append(m)
G = list(groups.values())
soap = [g for g in G if g.get("target_type") == "SOAP"]

print("==== 基线 ====")
base_ok = audit("基线")

print("\n==== 路径 A：复用提交（不带 force；未变更应复用冻结定义，不重跑 AI）====")
st, r = post("/pipelines/generate", {"pipelines": soap})
d = r.get("data") or {}
print("  HTTP %s result=%s unchanged=%s render_skipped=%s" % (
    st, d.get("result"), d.get("unchanged"), d.get("render_skipped")))
a_ok = audit("A 后")

print("\n==== 路径 B：**强制重建**（force=true → 重跑 Agent B 重建组件定义）====")
st2, r2 = post("/pipelines/generate", {"pipelines": soap, "force": True})
d2 = r2.get("data") or {}
print("  HTTP %s result=%s | %s" % (st2, d2.get("result"), str(r2.get("message"))[:60]))
print("  validation: ok=%s errors=%s" % ((d2.get("validation") or {}).get("ok"),
                                         (d2.get("validation") or {}).get("error_count")))
b_ok = audit("B 后")

print("\n==== 总结 ====")
print("  基线零丢件=%s ｜ 复用提交后零丢件=%s ｜ 强制重建后零丢件=%s" % (base_ok, a_ok, b_ok))
print("  %s" % ("ALL-OK（重建/复用均不丢组件）" if (base_ok and a_ok and b_ok) else "FAILED（见上方缺失清单）"))
sys.exit(0 if (base_ok and a_ok and b_ok) else 1)
