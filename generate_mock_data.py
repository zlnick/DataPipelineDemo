"""生成演示模拟数据：FHIR Patient + SQL 实体（PatientEntity 表），供双管道看效果。

用法（backend 容器内执行，容器内已挂载本脚本）：
    docker compose exec dataflow-backend python /app/generate_mock_data.py
    docker compose exec dataflow-backend python /app/generate_mock_data.py --fhir 5 --sql 3

说明：
- FHIR：向 IRIS FHIR Server 事务写入几条 Patient（触发 FHIR 增量抓取链路）。
- SQL：向 SQLUser.PatientEntity（SQL 源演示表，三字段实体）插入几条行
  （触发 SQL 源轮询 → SOAP 投递链路）。假设 SQL 源向导选择了 PatientEntity 表。
- 主键带时间戳后缀，重复执行不互相覆盖。
"""

import argparse
import base64
import json
import random
import time
import urllib.request
from datetime import datetime, timezone

from backend.config import FHIRConfig
from backend.services import iris_connector

SURNAMES = ["张", "李", "王", "赵", "钱", "孙", "吴", "周", "陈", "刘", "杨", "黄", "唐", "何"]
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛", "明", "超", "梅", "瑞"]
GENDERS = ["male", "female"]

# 常用检验观察项（LOINC 编码 + 中文显示 + 单位）
OBS_DEFS = [
    ("8867-4", "心率", "次/分"),
    ("8310-5", "体温", "℃"),
    ("8480-6", "收缩压", "mmHg"),
    ("8462-4", "舒张压", "mmHg"),
    ("29463-7", "体重", "kg"),
    ("2339-0", "血糖", "mmol/L"),
]


def _rand_name() -> tuple[str, str, str]:
    """随机中文名（姓 / 名 / 性别）。"""
    surname = random.choice(SURNAMES)
    given = "".join(random.sample(GIVEN, 2)) if random.random() < 0.5 else random.choice(GIVEN)
    return surname, given, random.choice(GENDERS)


def generate_fhir_patients(count: int) -> list[str]:
    """向 FHIR Server 事务写入 count 条 Patient，返回响应状态列表。"""
    base = FHIRConfig.BASE_URL.rstrip("/") + "/"
    token = base64.b64encode(
        f"{FHIRConfig.USERNAME}:{FHIRConfig.PASSWORD}".encode()).decode("ascii")
    stamp = int(time.time() * 1000) % 100000
    entries = []
    for i in range(count):
        surname, given, gender = _rand_name()
        pid = f"G{stamp}{i}"
        entries.append({
            "resource": {
                "resourceType": "Patient",
                "id": pid,
                "name": [{"family": surname, "given": [given]}],
                "gender": gender,
                "birthDate": f"19{random.randint(70, 99)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            },
            "request": {"method": "POST", "url": "Patient"},
        })
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
    req = urllib.request.Request(base, data=json.dumps(bundle, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/fhir+json")
    req.add_header("Accept", "application/fhir+json")
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [e.get("response", {}).get("status", "?") for e in body.get("entry", [])]


def fetch_latest_patient_ids(count: int = 5) -> list[str]:
    """从 FHIR Server 拉取最近写入的 count 个 Patient 资源 ID（按 _lastUpdated 倒序）。"""
    base = FHIRConfig.BASE_URL.rstrip("/") + "/"
    token = base64.b64encode(
        f"{FHIRConfig.USERNAME}:{FHIRConfig.PASSWORD}".encode()).decode("ascii")
    req = urllib.request.Request(
        f"{base}Patient?_count={count}&_sort=-_lastUpdated", method="GET")
    req.add_header("Accept", "application/fhir+json")
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    ids = []
    for e in body.get("entry", []):
        res = e.get("resource") or {}
        if res.get("resourceType") == "Patient" and res.get("id"):
            ids.append(res["id"])
    return ids


def generate_fhir_observations(count: int) -> list[str]:
    """向 FHIR Server 写入 count 条 Observation（subject 关联最近 Patient）。

    字段形态与 Observation 目标表映射对齐：
    code.coding[0].code/display → Code/CodeDisplay；valueQuantity → Value/Unit；
    effectiveDateTime → EffectiveDate；subject.reference → PatientID（regexp 去前缀）。
    """
    base = FHIRConfig.BASE_URL.rstrip("/") + "/"
    token = base64.b64encode(
        f"{FHIRConfig.USERNAME}:{FHIRConfig.PASSWORD}".encode()).decode("ascii")
    patient_ids = fetch_latest_patient_ids(max(count, 5))
    if not patient_ids:
        print("警告：FHIR Server 暂无 Patient，Observation.subject 将引用空 ID")
    stamp = int(time.time() * 1000) % 100000
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    entries = []
    for i in range(count):
        pid = patient_ids[i % len(patient_ids)] if patient_ids else f"G{stamp}{i}"
        code, display, unit = random.choice(OBS_DEFS)
        value = round(random.uniform(36.0, 140.0), 1)
        oid = f"O{stamp}{i}"
        entries.append({
            "resource": {
                "resourceType": "Observation",
                "id": oid,
                "status": "final",
                "code": {
                    "coding": [{"system": "http://loinc.org",
                                "code": code, "display": display}],
                    "text": display,
                },
                "subject": {"reference": f"Patient/{pid}"},
                "effectiveDateTime": now,
                "valueQuantity": {"value": value, "unit": unit},
            },
            "request": {"method": "POST", "url": "Observation"},
        })
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
    req = urllib.request.Request(base, data=json.dumps(bundle, ensure_ascii=False).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/fhir+json")
    req.add_header("Accept", "application/fhir+json")
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [e.get("response", {}).get("status", "?") for e in body.get("entry", [])]


def generate_sql_entities(count: int) -> list[str]:
    """向 SQLUser.PatientEntity 插入 count 行实体（SQL 源演示数据）。"""
    stamp = int(time.time() * 1000) % 100000
    ids = []
    for i in range(count):
        surname, given, gender = _rand_name()
        patient_no = f"S{stamp}{i}"
        full_name = f"{surname} {given}"
        iris_connector.execute(
            "INSERT INTO PatientEntity (PatientNo, FullName, Gender) VALUES (?, ?, ?)",
            [patient_no, full_name, gender])
        ids.append(patient_no)
    return ids


def main() -> None:
    """入口：生成 FHIR 与 SQL 模拟数据。"""
    parser = argparse.ArgumentParser(description="生成演示模拟数据（FHIR Patient/Observation + SQL PatientEntity）")
    parser.add_argument("--fhir", type=int, default=3, help="FHIR Patient 条数（默认 3）")
    parser.add_argument("--obs", type=int, default=0,
                        help="FHIR Observation 条数（默认 0；subject 关联最近写入的 Patient）")
    parser.add_argument("--sql", type=int, default=3, help="SQL PatientEntity 行数（默认 3）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（可选，便于复现）")
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    fhir_status = generate_fhir_patients(args.fhir)
    print(f"FHIR Patient 写入 {args.fhir} 条，状态：{fhir_status}")

    if args.obs:
        obs_status = generate_fhir_observations(args.obs)
        print(f"FHIR Observation 写入 {args.obs} 条，状态：{obs_status}")

    sql_ids = generate_sql_entities(args.sql)
    print(f"SQL PatientEntity 插入 {args.sql} 行：{sql_ids}")
    print("完成：管道会自动增量处理，去 Pipelines 页查看消息与落库。")


if __name__ == "__main__":
    main()
