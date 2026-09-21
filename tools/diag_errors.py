# -*- coding: utf-8 -*-
"""Ens 运行期日志 **分类摘要**（默认聚焦"真错误"）。

为什么分类（2026-09-21）：`Ens_Util.Log` 的 Type=3(Error) 里常混入 Ens **自身的排队提示**，例如
`Queued messages are being saved in the suspended production in the queue global (^Ens.Queue)`
—— 它只在**停/启 Production 期间**出现（重扫 SQL 源、重置环境都会触发），属**提示非故障**；
直接按 Type=3 计数会误报"有错误"（本次全面重测就为此多花了几轮）。

用法（容器内；datakit：`bash tools/datakit/run.sh diag_errors.py [--limit N] [--all]`）：
  --limit N  扫描最近 N 条（默认 200）
  --all      连排队提示的明细也打印（默认只汇总条数）
"""
import argparse

import iris.dbapi as D

TYPE_NAME = {1: "Assert", 2: "Warning", 3: "Error", 4: "Info", 5: "Alert", 6: "System"}
# 已知的"非故障"提示（停/启 Production 期间必然出现）
BENIGN = ("Queued messages are being saved in the suspended production",)

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=200)
ap.add_argument("--all", action="store_true", help="连排队提示明细也打印")
a = ap.parse_args()

conn = D.connect(hostname="iris", port=1972, namespace="USER",
                 username="superuser", password="SYS")
cur = conn.cursor()
try:
    cur.execute("SELECT TOP %d ID, Type, ConfigName, Text, TimeLogged FROM Ens_Util.Log "
                "ORDER BY ID DESC" % a.limit)
    rows = cur.fetchall()
except Exception as exc:  # noqa: BLE001
    print("log query err:", exc)
    rows = []
conn.close()

by_type: dict = {}
real_err, benign = [], []
for _id, typ, cfg, txt, t in rows:
    by_type[typ] = by_type.get(typ, 0) + 1
    s = str(txt or "")
    if typ == 3 and any(b in s for b in BENIGN):
        benign.append((t, cfg, s[:120]))
    elif typ == 3:
        real_err.append((t, cfg, s[:150]))

print("最近 %d 条日志分类: %s"
      % (len(rows), {TYPE_NAME.get(k, str(k)): v for k, v in sorted(by_type.items())}))
print("Type=3 细分: **真错误 %d 条**；排队提示（非故障）%d 条" % (len(real_err), len(benign)))
if real_err:
    print("--- 真错误（最多 10 条）---")
    for t, cfg, s in real_err[:10]:
        print("  ", t, cfg, s)
if a.all and benign:
    print("--- 排队提示（停/启 Production 期间必然出现）---")
    for t, cfg, s in benign[:5]:
        print("  ", t, s[:110])
if not rows:
    print("  （日志为空）")
print("DIAG-ERRORS-%s" % ("OK" if not real_err else "HAS-ERROR"))

