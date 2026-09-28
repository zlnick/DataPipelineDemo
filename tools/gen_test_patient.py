# -*- coding: utf-8 -*-
"""FHIR 测试数据生成器：一键「造源数据 → 触发同步 → 校验 FHIR 落地」。

在**宿主机**直接运行即可（脚本会自动送进 backend 容器执行，因为需要容器网络访问 iris/CLINIC）：

    python3 tools/gen_test_patient.py                        # 生成 1 位（姓名按序号顺延）
    python3 tools/gen_test_patient.py --family 赵 --given 敏   # 指定姓名
    python3 tools/gen_test_patient.py --count 2 --diagnosis 糖尿病 --drug 阿司匹林
    python3 tools/gen_test_patient.py --force                # 停→清 SQL 源扫描凭证→启（全量重扫）
    python3 tools/gen_test_patient.py --no-verify            # 只造数，不等待校验

SQL→SOAP / SQL→DB（`--source user`）：造 USER 库的 SQL **源表**，再等管道把结果写进**目标表**：

    python3 tools/gen_test_patient.py --source user --count 3                          # 源表 SQLUser.Patient（默认）
    python3 tools/gen_test_patient.py --source user --table PatientSource --count 3    # 源表换成 PatientSource
    python3 tools/gen_test_patient.py --source user --verify-table Patient --count 3   # 校验 SQL→DB 写回的 Patient

需求表（USER 命名空间）与脚本的对应关系：

| 表 | 角色 | 谁写它 |
|---|---|---|
| `Patient` | SQL 源（结构 = ID/FamilyName/GivenName/Gender/BirthDate/Phone/Address/City） | 本脚本 `--table Patient`（默认） |
| `PatientSource` | SQL 源（README「步骤 B」的业务库演示表，结构与 `Patient` 相同） | 本脚本 `--table PatientSource` |
| `PatientEntity` | **SOAP 落库表（目标）**，由 mock `AddPatient` UPSERT | 管道运行结果（`--verify-table` 默认查它） |

行为：①CLINIC 追加 N 位患者（含就诊/诊断/药嘱，引用完整）②等待源适配器增量投递（--force 则全量重扫）
③按 identifier 查回 FHIR（打印中文姓名）、统计子资源数量，并给出计数前后对比。
"""
import os
import subprocess
import sys

CONTAINER = "dataflow-backend"


def _run_in_container():
    """宿主机路径：把自身送入 backend 容器执行并透传参数。"""
    here = os.path.abspath(__file__)
    subprocess.run(["docker", "cp", here, f"{CONTAINER}:/tmp/gen_test_patient.py"], check=True)
    cmd = ["docker", "exec", "-w", "/app", CONTAINER, "python", "/tmp/gen_test_patient.py",
           *sys.argv[1:]]
    raise SystemExit(subprocess.run(cmd).returncode)


def main_in_container():
    """容器内：造数 → 触发同步 → 校验 FHIR。"""
    import argparse
    import base64
    import json
    import random
    import time
    import urllib.request

    import iris.dbapi

    from backend.services import iris_connector as ic
    from backend.services.clinic_seed import (CITY, DRUG_URI, FAM, GIV, ICD_URI,
                                              IRIS_PORT, SCENARIOS, _load_terms, _pick)

    ap = argparse.ArgumentParser(description="生成 FHIR 测试患者数据")
    ap.add_argument("--source", choices=("clinic", "user"), default="clinic",
                    help="clinic=造 CLINIC 源并校验 FHIR 落地（默认）；"
                         "user=造 USER 库 SQL 源表并校验 SQL→SOAP / SQL→DB 目标落库")
    ap.add_argument("--table", choices=("Patient", "PatientSource"), default="Patient",
                    help="--source user 时的源表 SQLUser.<表>：Patient=SQL→SOAP 演示默认；"
                         "PatientSource=README「步骤 B」的业务库演示表（同结构，避开与目标表同名造成回环）")
    ap.add_argument("--verify-table", default="",
                    help="--source user 时的落库校验表（USER 命名空间）；默认 PatientEntity"
                         "（SOAP mock 落库表），SQL→DB 写回 Patient 时传 --verify-table Patient")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--family", default="")
    ap.add_argument("--given", default="")
    ap.add_argument("--mrn", default="")
    ap.add_argument("--diagnosis", default="")
    ap.add_argument("--drug", default="")
    ap.add_argument("--force", action="store_true", help="停→清扫描凭证→启（全量重扫）")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--timeout", type=int, default=90)
    args = ap.parse_args()

    # 校验的是 FHIR **目标**仓库（演示默认 = FHIRSERVER，转换结果落这里）；
    # FHIR **源**仓库默认是 DemoFHIR（见 backend/config.py 的 FHIRConfig）。
    FHIR = "http://iris:52773/csp/healthshare/fhirserver/fhir/r4"
    AUTH = base64.b64encode(b"superuser:SYS").decode()

    if args.source == "user":
        return _run_user_source(args)

    def fhir_get(path):
        """GET FHIR（容器内地址）。"""
        req = urllib.request.Request(FHIR + path, headers={
            "Authorization": "Basic " + AUTH, "Accept": "application/fhir+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def counts():
        """各资源总数。"""
        return {rt: int(fhir_get(f"/{rt}?_summary=count").get("total") or 0)
                for rt in ("Patient", "Encounter", "Condition", "MedicationRequest")}

    before = counts()
    try:
        diags, drugs = _load_terms()
    except Exception as exc:  # noqa: BLE001 - 术语服务不可用时回退内置示例码
        print("[warn] 术语服务不可用，使用内置示例码:", exc)
        diags = [("E11.900", "2型糖尿病"), ("E78.500", "高脂血症")]
        drugs = [("B01AC06", "阿司匹林肠溶片", "nrdl")]

    conn = iris.dbapi.connect(hostname="iris", port=IRIS_PORT, namespace="CLINIC",
                              username="superuser", password="SYS")
    cur = conn.cursor()

    def next_id(table, prefix):
        """按现有 ID 数字后缀取最大 +1。"""
        cur.execute(f"SELECT ID FROM {table}")
        nums = [int(str(r[0])[len(prefix):]) for r in cur.fetchall()
                if str(r[0] or "").startswith(prefix) and str(r[0])[len(prefix):].isdigit()]
        return f"{prefix}{(max(nums) if nums else 0) + 1:04d}"

    created = []
    for _ in range(max(1, args.count)):
        cur.execute("SELECT COUNT(*) FROM Patient")
        idx = int(cur.fetchone()[0])          # 用现有行数轮转，保证姓名/场景多样
        pid = next_id("Patient", "P")
        fam = args.family or FAM[idx % len(FAM)]
        giv = args.given or GIV[idx % len(GIV)]
        mrn = args.mrn or f"MRN-{1000 + idx}"
        gender = "male" if idx % 3 else "female"
        birth = (f"{random.randint(1950, 2005):04d}-{random.randint(1, 12):02d}-"
                 f"{random.randint(1, 28):02d}")
        city = CITY[idx % len(CITY)]
        cur.execute("INSERT INTO Patient (ID,MRN,FamilyName,GivenName,Gender,BirthDate,Phone,"
                    "Address,City) VALUES (?,?,?,?,?,?,?,?,?)",
                    [pid, mrn, fam, giv, gender, birth, f"138{idx:06d}{idx:02d}",
                     f"{city}测试路{idx + 1}号", city])

        sc = SCENARIOS[idx % len(SCENARIOS)]
        kw = args.diagnosis or sc[0]
        kw2 = None if args.diagnosis else sc[1]
        drug_kw = args.drug or (sc[2][0] if sc[2] else None)
        d1 = _pick(diags, kw) or diags[0]
        d2 = _pick(diags, kw2) if kw2 else None

        eid = next_id("Encounter", "E")
        cur.execute("INSERT INTO Encounter (ID,PatientID,ClassCode,ClassDisplay,Status,PeriodStart,"
                    "PeriodEnd,ReasonCode,ReasonText) VALUES (?,?,?,?,?,?,?,?,?)",
                    [eid, pid, "AMB", "门诊", "finished", "2026-09-20T09:00:00", "", d1[0], d1[1]])
        dids = []
        for rank, d in enumerate([d1] + ([d2] if d2 else []), 1):
            did = next_id("Diagnosis", "D")
            dids.append(did)
            cur.execute("INSERT INTO Diagnosis (ID,EncounterID,PatientID,Code,Name,CodeSystem,Rank,"
                        "OnsetDate,ClinicalStatus) VALUES (?,?,?,?,?,?,?,?,?)",
                        [did, eid, pid, d[0], d[1], ICD_URI, rank, "2026-09-05", "active"])
        med = (_pick(drugs, drug_kw) if drug_kw else None) or \
            (drugs[0] if drugs else ("B01AC06", "阿司匹林肠溶片", "nrdl"))
        cs = med[2] if len(med) > 2 else "nrdl"
        mid = next_id("MedicationOrder", "M")
        cur.execute("INSERT INTO MedicationOrder (ID,EncounterID,PatientID,MedicationCode,"
                    "MedicationName,CodeSystem,DosageValue,DosageUnit,Route,Frequency,StartDate,"
                    "Status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [mid, eid, pid, med[0], med[1], DRUG_URI.get(cs, cs),
                     "500", "mg", "口服", "每日2次", "2026-09-20", "active"])
        created.append({"pid": pid, "mrn": mrn, "name": f"{fam}{giv}", "eid": eid,
                        "dids": dids, "mids": [mid]})
    conn.commit()

    for c in created:
        print(f"[造数] {c['pid']} {c['name']} ({c['mrn']}) | 就诊 {c['eid']} | "
              f"诊断 {c['dids']} | 药嘱 {c['mids']}")
    print("[造数] CLINIC:", end=" ")
    for t in ("Patient", "Encounter", "Diagnosis", "MedicationOrder"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"{t}={cur.fetchone()[0]}", end=" ")
    print()
    cur.close()
    conn.close()

    if args.force:
        print("[同步] --force：停 Production → 清 SQL 源扫描凭证 → 重启（全量重扫）")
        ic.class_method_value("Ens.Director", "StopProduction", 60)
        items = _sql_service_items()
        try:
            import iris as _iris
            conn = _iris.connect("iris", 1972, "USER", "superuser", "SYS")
            try:
                native = _iris.createIRIS(conn)
                for item in items:
                    native.kill("^Ens.AppData", item, "adapter.sqlrow")
                    native.kill("^Ens.AppData", item, "adapter.sqlparam")
                    native.kill("^IRIS.Temp.Adapter.sqlrow", item)
            finally:
                conn.close()  # 许可按连接计数，用后即关
            print("[同步] 凭证已清:", items)
        except Exception as exc:  # noqa: BLE001 - 清凭证失败不阻断（可手工处理）
            print("[warn] 清凭证失败（可手工处理）:", exc)
        res = ic.class_method_value("Ens.Director", "StartProduction", "demo.DataflowProduction")
        if res != 1:   # 启动失败必须显式报错：否则"清了凭证但没重启"会被当成同步成功
            print(f"[warn] StartProduction -> {res}，尝试 RecoverProduction 后重试")
            try:
                ic.class_method_value("Ens.Director", "RecoverProduction")
            except Exception as rec_exc:  # noqa: BLE001
                print("[warn] RecoverProduction 失败:", rec_exc)
            res = ic.class_method_value("Ens.Director", "StartProduction", "demo.DataflowProduction")
        print(f"[同步] Production 重启 -> {res}（1=成功；非 1 时源 BS 不会重扫）")
        if res != 1:
            print("[error] Production 启动失败 —— 本次全量重扫不会生效（检查许可单元/组件后重试）")
            return
    if args.no_verify:
        print("[校验] 已跳过（--no-verify）")
        return

    print(f"[校验] 等待 FHIR 落地（最长 {args.timeout}s）…")
    deadline = time.time() + args.timeout
    got = {}
    while time.time() < deadline:
        for c in created:
            if c["mrn"] in got:
                continue
            try:
                b = fhir_get("/Patient?identifier=" + c["mrn"])
            except Exception:  # noqa: BLE001
                continue
            if int(b.get("total") or 0) > 0:
                got[c["mrn"]] = b["entry"][0]["resource"]
        if len(got) == len(created):
            break
        time.sleep(5)

    after = counts()
    print("[校验] 计数:", " ".join(f"{k} {before[k]}→{after[k]}" for k in before))
    for c in created:
        res = got.get(c["mrn"])
        if not res:
            print(f"[校验] ✗ {c['mrn']} {c['name']} 未在 FHIR 出现"
                  f"（查消息/错误日志与源扫描凭证，或加 --force 全量重扫）")
            continue
        pu = res.get("id")
        nm = json.dumps(res.get("name"), ensure_ascii=False)
        enc = fhir_get(f"/Encounter?subject=Patient/{pu}&_summary=count").get("total")
        cond = fhir_get(f"/Condition?subject=Patient/{pu}&_summary=count").get("total")
        med = fhir_get(f"/MedicationRequest?subject=Patient/{pu}&_summary=count").get("total")
        print(f"[校验] ✓ Patient/{pu} name={nm} | Encounter={enc} "
              f"Condition={cond} MedicationRequest={med}")


# `--source user` 的落库校验口径：表 → (校验查询 SQL, 打印列名)；默认 PatientEntity（SOAP mock 落库表）。
_VERIFY_QUERIES = {
    "PatientEntity": ("SELECT PatientNo, FullName, Gender FROM SQLUser.PatientEntity WHERE PatientNo=?",
                      ["PatientNo", "FullName", "Gender"]),
    "PatientSource": ("SELECT ID, FamilyName, GivenName FROM SQLUser.PatientSource WHERE ID=?",
                      ["ID", "FamilyName", "GivenName"]),
    "Patient": ("SELECT ID, FamilyName, GivenName FROM SQLUser.Patient WHERE ID=?",
                ["ID", "FamilyName", "GivenName"]),
}


def _run_user_source(args):
    """向 USER 库的 SQL **源表**（默认 `SQLUser.Patient`）插 N 行，供 SQL→SOAP / SQL→DB 管道测试，并校验目标落库。

    源表用 `--table` 选（`Patient` / `PatientSource`，两者结构相同：ID/FamilyName/GivenName/Gender/
    BirthDate/Phone/Address/City），主键沿用 `P0001…`（按现有行数顺延，重复执行不互相覆盖）。
    SOAP 目标（demo 内置 AddPatient mock）会把结果 UPSERT 到 `SQLUser.PatientEntity`
    （PatientNo/FullName/Gender），即默认的校验口径；SQL→DB 写回 `Patient` 时用 `--verify-table Patient`。
    """
    import time

    import iris.dbapi

    from backend.services import iris_connector as ic

    table = getattr(args, "table", "Patient")
    verify_table = getattr(args, "verify_table", "") or "PatientEntity"
    verify_sql, verify_cols = _VERIFY_QUERIES.get(verify_table, _VERIFY_QUERIES["PatientEntity"])

    conn = iris.dbapi.connect(hostname="iris", port=1972, namespace="USER",
                              username="superuser", password="SYS")
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM SQLUser.{table}")
    base = int(cur.fetchone()[0])
    created = []
    for n in range(1, max(1, args.count) + 1):
        idx = base + n
        fam = args.family or "测试"
        giv = args.given or f"患者{idx}"
        created.append({"pid": f"P{idx:04d}", "fam": fam, "giv": giv, "name": f"{fam}{giv}",
                        "gender": "male" if idx % 3 else "female"})

    # 基线：校验表里**已存在**的同主键行 —— 这类行无法判别是否本次投递（否则会报假 ✓）。
    # 必须在 INSERT 之前取（插入才会触发管道投递）。
    stale = {c["pid"] for c in created if ic.query(verify_sql, [c["pid"]])}
    if stale:
        print(f"[造数] 提示：{verify_table} 中已存在同主键 " + ", ".join(sorted(stale))
              + " —— 这些行不参与落库判别")

    for c in created:
        cur.execute(f"INSERT INTO SQLUser.{table} (ID,FamilyName,GivenName,Gender,BirthDate,"
                    "Phone,Address,City) VALUES (?,?,?,?,?,?,?,?)",
                    [c["pid"], c["fam"], c["giv"], c["gender"], "1990-01-01",
                     f"139{int(c['pid'][1:]):08d}", "测试路1号", "测试市"])
    conn.commit()
    cur.execute(f"SELECT COUNT(*) FROM SQLUser.{table}")
    print(f"[造数] USER SQLUser.{table} 行数:", cur.fetchone()[0])
    cur.close()
    conn.close()
    for c in created:
        print(f"[造数] {c['pid']} {c['name']}（{c['gender']}）")

    if args.no_verify:
        print("[校验] 已跳过（--no-verify）")
        return

    pending = [c for c in created if c["pid"] not in stale]
    if not pending:
        print(f"[校验] 本次 {len(created)} 位在 {verify_table} 里已有同主键，无法判别是否新投递"
              f"（换一张主键不重叠的源表，或先清理 {verify_table}）")
        return
    print(f"[校验] 等待目标落库（最长 {args.timeout}s，看 SQLUser.{verify_table}）…")
    deadline = time.time() + args.timeout
    found = {}
    while time.time() < deadline:
        for c in pending:
            if c["pid"] in found:
                continue
            rows = ic.query(verify_sql, [c["pid"]])
            if rows:
                found[c["pid"]] = rows[0]
        if len(found) == len(pending):
            break
        time.sleep(5)
    for c in pending:
        r = found.get(c["pid"])
        if r:
            detail = " ".join(f"{k}={v}" for k, v in zip(verify_cols, r))
            print(f"[校验] ✓ {verify_table}: {detail}")
        else:
            print(f"[校验] ✗ {c['pid']} 未落到 {verify_table}"
                  f"（查消息/错误日志与源扫描凭证，或加 --force 全量重扫）")


def _sql_service_items():
    """取所有 SQL 源 BS 名（--force 清凭证用）。

    优先枚举 `^Ens.AppData` 一级下标：SQL 源 BS 名会随数据管道类别/去重变化
    （`SQLService_Patient__sql2fhir_patient_tx`、`SQLService_Patient_2` …），只按
    ClassName 查配置表会漏掉"已改名但容器里仍有扫描凭证"的 Item，导致重扫无效。

    返回:
        Item 名列表（排序去重）；都取不到时退回旧版默认名。
    """
    from backend.services import iris_connector as ic
    names: set = set()
    try:
        import iris as _iris
        conn = _iris.connect("iris", 1972, "USER", "superuser", "SYS")
        try:
            native = _iris.createIRIS(conn)
            sub = native.nextSubscript(False, "^Ens.AppData", "")
            while sub:
                if str(sub).startswith("SQLService"):
                    names.add(str(sub))
                sub = native.nextSubscript(False, "^Ens.AppData", sub)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 枚举失败退回配置表
        pass
    try:
        rows = ic.query("SELECT Name FROM Ens_Config.Item "
                        "WHERE ClassName='EnsLib.SQL.Service.GenericService'")
        names |= {str(r[0]) for r in rows if r[0]}
    except Exception:  # noqa: BLE001 - 配置表为空/不存在
        pass
    return sorted(names) or ["SQLService_Patient"]


if __name__ == "__main__":
    if os.path.exists("/app/backend"):
        main_in_container()   # 已在 backend 容器内
    else:
        _run_in_container()   # 宿主机：自举进容器


