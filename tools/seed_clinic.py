# -*- coding: utf-8 -*-
"""为 CLINIC SQL 源生成演示数据（等价界面「生成演示数据」按钮）。

⚠ 前置条件：**需要先登记一个 SQL 数据源**（界面「数据源管理」→ JDBC 连接 → 选表；
演示默认 = `CLINIC` 命名空间，见 README「演示步骤 §B 第 1 步」）。
未登记数据源时本脚本会**明确报错并给出指引**（旧版直接抛 IndexError，让人摸不着头脑）。

用法:
    python3 tools/seed_clinic.py            # 默认 3 位患者
    python3 tools/seed_clinic.py 10         # 指定患者数
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:5001/api"

try:
    items = json.load(urllib.request.urlopen(BASE + "/datasources"))["data"]["items"]
except Exception as exc:  # noqa: BLE001 - 后端未起/端口不同
    print("[!!] 无法访问后端 %s（先确认服务已启动）：%s" % (BASE, exc), file=sys.stderr)
    raise SystemExit(2)

sqls = [d for d in items if d.get("type") == "SQL"]
if not sqls:
    print("[!!] 未登记任何 SQL 数据源 —— 请先在界面「数据源管理」登记（演示默认 = CLINIC 命名空间），\n"
          "     等价的 API 调用: POST /api/datasources/<id>/seed  {\"patients\": 3}\n"
          "     说明：本脚本等价界面「生成演示数据」按钮，**必须先有数据源**才能造数。", file=sys.stderr)
    raise SystemExit(2)


def _is_clinic(ds: dict) -> bool:
    """数据源是否与 CLINIC 源库相关（演示默认口径）。"""
    return "clinic" in json.dumps(ds, ensure_ascii=False).lower()


ds = next((d for d in sqls if _is_clinic(d)), sqls[0])
if not _is_clinic(ds):
    print("[warn] 未发现 CLINIC 相关数据源，改用第一个 SQL 数据源：%s" % ds.get("name"), file=sys.stderr)

patients = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 3
print("datasource:", ds["id"], ds.get("name"), "| patients:", patients)
req = urllib.request.Request(
    f"{BASE}/datasources/{ds['id']}/seed",
    data=json.dumps({"patients": patients}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    print(urllib.request.urlopen(req, timeout=900).read().decode()[:300])
except urllib.error.HTTPError as exc:  # noqa: BLE001
    print("[!!] 造数失败 HTTP %s: %s" % (exc.code, exc.read().decode()[:200]), file=sys.stderr)
    raise SystemExit(1)

