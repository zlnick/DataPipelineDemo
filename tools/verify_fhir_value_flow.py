#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FHIR **值传导**验证（固化判据；2026-09-21 P1#9）。

用途：验证"改源库的值 → 管道投递 → 目标 FHIR 仓库跟着变"这一链路（比"看行数/看消息状态"更硬）。

判据（**刻意避开 2026-09-21 实测踩过的三个坑**）：
  ① 基线用 `?_summary=count`（**不用 `?family=<中文>`** —— 该服务器上是慢查询，会假"未命中"）；
  ② 用**资源 id 差集**识别"本次新增"（**不按固定 id 读** —— 本环境写入可能是 create 语义，固定 id 永远是旧值）；
  ③ **不靠"最新 lastUpdated"排序**（同一秒多条时顺序不定，取 max 会误判）。

用法（宿主运行；脚本内部用 docker exec 做 SQL 探针与重扫）：
  python3 tools/verify_fhir_value_flow.py --ns CLINIC --table Patient --id P001 \\
      --col FamilyName --repo fhirserver --type Patient --field family --rescan

参数：
  --ns/--table/--id/--col   源库定位（命名空间 + 表 + 主键 + 待改列）
  --repo                    fhirserver（默认，目标）| demofhir（源仓库）
  --type                    资源类型（默认从 --table 推断：Patient→Patient …）
  --field                   目标侧字段（默认按类型：Patient→family）
  --value                   写入的校验值（默认自动生成 验证<时间戳>）
  --rescan                  改值后自动触发 SQL 源全量重扫（推荐）
  --timeout                 等待新增资源的秒数（默认 240）
  --no-restore              不还原源值（默认还原并再验一次）
  --json                    以 JSON 输出结论（便于脚本消费）
退出码：0 = 传导与还原均 PASS；1 = 有 CHECK/FAIL
"""
import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request as u

REPOS = {
    "fhirserver": "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4",
    "demofhir": "http://localhost:52773/csp/healthshare/demofhir/fhir/r4",
}
AUTH = {"Authorization": "Basic " + base64.b64encode(b"superuser:SYS").decode(),
        "Accept": "application/fhir+json"}
CONTAINER = "dataflow-backend"

# 容器内 SQL 探针（`get` 读整行 / `set` 改一列并读回确认）
PROBE = '''# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, "/app")
import iris, iris.dbapi
NS = {"hostname": "iris", "port": 1972, "username": "superuser", "password": "SYS"}
cmd = sys.argv[1]
conn = iris.dbapi.connect(namespace=sys.argv[2], **NS)
try:
    cur = conn.cursor()
    if cmd == "get":
        cur.execute('SELECT * FROM SQLUser."%s" WHERE ID=?' % sys.argv[3], (sys.argv[4],))
        cols = [d[0] for d in cur.description]
        row = cur.fetchone() or []
        print("ROW " + json.dumps(dict(zip(cols, [str(v) for v in row])), ensure_ascii=False))
    else:
        cur.execute('UPDATE SQLUser."%s" SET "%s"=? WHERE ID=?' % (sys.argv[3], sys.argv[5]),
                    (sys.argv[6], sys.argv[4]))
        conn.commit()
        cur.execute('SELECT "%s" FROM SQLUser."%s" WHERE ID=?' % (sys.argv[5], sys.argv[3]),
                    (sys.argv[4],))
        print("SET " + str(cur.fetchone()[0]))
finally:
    conn.close()
'''.replace("import sys\n", "import json\nimport sys\n", 1)



def probe(*args) -> str:
    """执行容器内 SQL 探针（get/set），返回最后一行输出。"""
    r = subprocess.run(["docker", "exec", "-i", CONTAINER, "python", "-"] + list(args),
                       input=PROBE, capture_output=True, text=True, timeout=180)
    lines = [ln for ln in (r.stdout or "").strip().split("\n") if ln.strip()]
    return lines[-1] if lines else ("ERR " + (r.stderr or "")[-200:])


def rescan() -> str:
    """触发 SQL 源全量重扫（停 Production → 清扫描凭证 → 启 → 核验真的重扫）。"""
    src = open("tools/rescan_sql_source.py", encoding="utf-8").read()
    r = subprocess.run(["docker", "exec", "-i", CONTAINER, "python", "-"],
                       input=src, capture_output=True, text=True, timeout=900)
    out = r.stdout or ""
    if not out.strip():                      # ⚠ 大文件经 stdin 传容器可能变 0 字节（已记录的坑）
        return "ERR 重扫脚本未在容器内执行（stdin 传输为空？）：%s" % (r.stderr or "")[-120:]
    ls = [x for x in out.split("\n") if x.strip()]
    return ls[-1][:120]


def fhir_ids(repo: str, rtype: str, limit: int = 100) -> dict:
    """{id: resource}（用于**差集**识别新增；演示规模一次取完）。"""
    url = "%s/%s?_count=%d" % (REPOS[repo], rtype, limit)
    with u.urlopen(u.Request(url, headers=AUTH), timeout=120) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return {str((e.get("resource") or {}).get("id")): (e.get("resource") or {})
            for e in (d.get("entry") or [])}


def field_of(res: dict, path: str):
    """按点路径取值（支持 `name[0].family` 形式；仅 dict/list 索引）。"""
    cur = res
    for seg in (path or "").split("."):
        key, idx = seg, None
        if "[" in seg:
            key, rest = seg.split("[", 1)
            idx = int(rest.rstrip("]"))
        cur = cur.get(key) if isinstance(cur, dict) else None
        if idx is not None:
            cur = cur[idx] if isinstance(cur, list) and len(cur) > idx else None
    return cur


def poll_new(repo: str, rtype: str, before: set, field: str, want: str, timeout: int):
    """轮询直到出现"id 不在 before 中且目标字段 == want"的资源（**差集判据**）。"""
    t0, last = time.time(), {}
    while time.time() - t0 < timeout:
        cur = fhir_ids(repo, rtype)
        last = cur
        hits = [i for i, res in cur.items()
                if i not in before and str(field_of(res, field)) == str(want)]
        if hits:
            return True, round(time.time() - t0, 1), hits, len(cur)
        time.sleep(8)
    return False, round(time.time() - t0, 1), [], len(last)


def main() -> int:
    ap = argparse.ArgumentParser(description="FHIR 值传导验证（判据已固化）")
    ap.add_argument("--ns", required=True, help="源库命名空间（CLINIC / USER …）")
    ap.add_argument("--table", required=True, help="源表名（SQLUser.<table>）")
    ap.add_argument("--id", required=True, help="源行主键（ID 列）值")
    ap.add_argument("--col", required=True, help="待改的源列（如 FamilyName）")
    ap.add_argument("--repo", default="fhirserver", choices=sorted(REPOS))
    ap.add_argument("--type", default="Patient", help="FHIR 资源类型（默认 Patient）")
    ap.add_argument("--field", default="name[0].family", help="目标字段路径（默认 name[0].family）")
    ap.add_argument("--value", default="", help="写入值（默认自动生成）")
    ap.add_argument("--rescan", action="store_true", help="改值后自动重扫 SQL 源")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--no-restore", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    val = a.value or ("验证V" + time.strftime("%H%M%S"))
    out = {"source": "%s.%s(%s).%s" % (a.ns, a.table, a.id, a.col),
           "target": "%s/%s.%s" % (a.repo, a.type, a.field), "value": val}

    row = probe("get", a.ns, a.table, a.id)
    try:
        old = json.loads(row[4:]).get(a.col) if row.startswith("ROW ") else None
    except Exception:  # noqa: BLE001
        old = None
    if old is None:
        print("❌ 读源行失败: %s" % row[:200])
        return 1
    before = set(fhir_ids(a.repo, a.type))
    print("① 基线: 源 %s.%s = %r；目标 %s/%s 现有 %d 条"
          % (a.table, a.col, old, a.repo, a.type, len(before)))

    print("② 改源值:", probe("set", a.ns, a.table, a.id, a.col, val))
    if a.rescan:
        print("③ 重扫:", rescan())
    ok1, dt1, hits1, n1 = poll_new(a.repo, a.type, before, a.field, val, a.timeout)
    print("④ 传导（%.1fs）: 新增命中 %s（现有 %d 条）→ %s"
          % (dt1, hits1[:3], n1, "PASS" if ok1 else "FAIL"))
    out.update({"conduct": ok1, "conduct_sec": dt1, "conduct_hits": hits1[:5]})

    ok2 = None
    if not a.no_restore:
        print("⑤ 还原:", probe("set", a.ns, a.table, a.id, a.col, old))
        if a.rescan:
            print("⑥ 重扫:", rescan())
        after1 = set(fhir_ids(a.repo, a.type))
        ok2, dt2, hits2, _ = poll_new(a.repo, a.type, after1, a.field, old, a.timeout)
        print("⑦ 还原（%.1fs）: 新增命中 %s → %s" % (dt2, hits2[:3], "PASS" if ok2 else "FAIL"))
        out.update({"restore": ok2, "restore_sec": dt2, "restore_hits": hits2[:5]})
    out["total_now"] = len(fhir_ids(a.repo, a.type))

    ok = bool(ok1) and (ok2 is None or bool(ok2))
    print("\n==== 结论: 传导=%s%s ===="
          % ("PASS" if ok1 else "FAIL",
             "" if ok2 is None else "；还原=%s" % ("PASS" if ok2 else "FAIL")))
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    print("VALUE-FLOW-%s" % ("OK" if ok else "CHECK"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

