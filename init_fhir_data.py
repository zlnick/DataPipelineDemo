"""FHIR 示例数据加载脚本。

向 IRIS 自带 FHIR Server 提交示例资源（Patient / Observation），
以 Bundle（type=transaction）方式写入 FHIR endpoint。

注意（方案 A，见 docs/PROJECT_PLAN.md）：
- IRIS FHIR Server 对资源读写默认要求 Basic Auth（superuser/SYS），
  本脚本访问 endpoint 时携带认证头。
- 使用 PUT（幂等），脚本可重复执行。

本脚本在 backend 容器启动时执行（init_data.py 之后）。
"""

import base64
import json
import logging
import random
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request

from backend.config import FHIRConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 确定性随机：每次生成相同示例数据，保证演示稳定
random.seed(2026)

# 预置示例数据的统一 lastUpdated（历史数据时间戳，早于增量游标）
SEED_LAST_UPDATED = "2026-08-29T00:00:00.000Z"

FAMILY_NAMES = ["张", "李", "王", "刘", "陈", "杨", "赵", "黄", "周", "吴"]
GIVEN_NAMES = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛", "明", "超", "霞", "平", "刚"]
CITIES = ["北京", "上海", "广州", "深圳", "成都", "杭州"]

# 常用检验观察项（LOINC 编码 + 单位）
OBSERVATION_DEFS = [
    ("8867-4", "心率", "次/分"),
    ("8310-5", "体温", "℃"),
    ("8480-6", "收缩压", "mmHg"),
    ("8462-4", "舒张压", "mmHg"),
    ("29463-7", "体重", "kg"),
    ("2339-0", "血糖", "mmol/L"),
]


def wait_for_fhir(max_retries: int = 60, delay: float = 2.0) -> None:
    """等待 FHIR endpoint 就绪（探测 /metadata，匿名可访问）。

    参数:
        max_retries: 最大重试次数。
        delay: 每次重试间隔（秒）。
    """
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(FHIRConfig.BASE_URL + "metadata")
            # 注意：必须显式设置 Accept 头，否则 IRIS FHIR Server 返回 406
            req.add_header("Accept", "application/fhir+json")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("FHIR endpoint 已就绪（第 %d 次尝试成功）", attempt)
                    return
        except Exception as exc:  # noqa: BLE001 - 等待期间任何异常都应继续重试
            logger.info("等待 FHIR endpoint 就绪中...（第 %d/%d 次）: %s", attempt, max_retries, exc)
            time.sleep(delay)
    raise RuntimeError(f"FHIR endpoint 在 {max_retries * delay:.0f} 秒内未能就绪，初始化失败。")


def make_patient(pid: str, family: str, given: str, gender: str,
                 birth_date: str, phone: str, line: str, city: str) -> dict:
    """构造一个 FHIR Patient 资源。"""
    return {
        "resourceType": "Patient",
        "id": pid,
        "meta": {
            "profile": ["http://hl7.org/fhir/StructureDefinition/Patient"],
            "lastUpdated": SEED_LAST_UPDATED,
        },
        "name": [{"family": family, "given": [given]}],
        "gender": gender,
        "birthDate": birth_date,
        "telecom": [{"system": "phone", "value": phone}],
        "address": [{"line": [line], "city": city}],
    }


def make_observation(oid: str, pid: str, code: str, display: str,
                     unit: str, value: float, date_str: str) -> dict:
    """构造一个 FHIR Observation 资源（关联患者）。"""
    return {
        "resourceType": "Observation",
        "id": oid,
        "meta": {"lastUpdated": SEED_LAST_UPDATED},
        "status": "final",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": code, "display": display}],
            "text": display,
        },
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": date_str,
        "valueQuantity": {"value": value, "unit": unit},
    }


def generate_patients(count: int = 10) -> list[dict]:
    """生成 count 条 Patient 示例资源。"""
    patients = []
    for i in range(1, count + 1):
        family = random.choice(FAMILY_NAMES)
        given = random.choice(GIVEN_NAMES)
        gender = "male" if i % 2 == 1 else "female"
        year = random.randint(1965, 2000)
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        pid = f"P{i:03d}"
        patients.append(make_patient(
            pid=pid,
            family=family,
            given=given,
            gender=gender,
            birth_date=f"{year:04d}-{month:02d}-{day:02d}",
            phone=f"138{i:08d}",
            line=f"{CITIES[i % len(CITIES)]}示例街道{i * 17}号",
            city=CITIES[i % len(CITIES)],
        ))
    return patients


def generate_observations(patients: list[dict], per_patient: int = 3) -> list[dict]:
    """为每位患者生成 per_patient 条 Observation 示例资源。"""
    observations = []
    idx = 1
    for patient in patients:
        pid = patient["id"]
        for _k in range(per_patient):
            code, display, unit = random.choice(OBSERVATION_DEFS)
            value = round(random.uniform(50, 120), 1)
            oid = f"OBS{idx:04d}"
            date_str = f"2026-08-{random.randint(1, 28):02d}T0{random.randint(6, 9)}:30:00+08:00"
            observations.append(make_observation(
                oid=oid, pid=pid, code=code, display=display,
                unit=unit, value=value, date_str=date_str,
            ))
            idx += 1
    return observations


def submit_bundle(entries: list[dict]) -> dict:
    """以 Bundle(transaction) 方式提交资源到 FHIR endpoint。

    参数:
        entries: FHIR 资源对象列表。

    返回:
        FHIR 服务器返回的 Bundle 响应（dict）。
    """
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"resource": e, "request": {"method": "PUT", "url": f"{e['resourceType']}/{e['id']}"}}
            for e in entries
        ],
    }
    payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    token = base64.b64encode(f"{FHIRConfig.USERNAME}:{FHIRConfig.PASSWORD}".encode("utf-8")).decode("ascii")

    req = urllib.request.Request(FHIRConfig.BASE_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/fhir+json")
    req.add_header("Accept", "application/fhir+json")
    req.add_header("Authorization", f"Basic {token}")

    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    """初始化主流程。"""
    # 1. 等待 FHIR endpoint 就绪
    wait_for_fhir()

    # 2. 生成示例资源并提交
    patients = generate_patients(10)
    observations = generate_observations(patients, per_patient=3)
    logger.info("生成示例资源：Patient %d 条 / Observation %d 条", len(patients), len(observations))

    try:
        result = submit_bundle(patients + observations)
    except urllib.error.HTTPError as exc:
        logger.error("FHIR Bundle 提交失败 HTTP %s: %s", exc.code, exc.read().decode("utf-8", "replace"))
        raise

    # 3. 统计提交结果
    entries = result.get("entry", [])
    ok = sum(1 for e in entries if e.get("response", {}).get("status", "").startswith("2"))
    logger.info("FHIR Bundle 提交完成：成功 %d/%d", ok, len(entries))
    if ok != len(entries):
        logger.warning("存在失败条目，请检查 FHIR Server 日志。")

    # 4. 初始化增量同步游标（预置数据视为历史数据，游标设为当前时间之后，
    #    这样增量同步不会重复抓取预置数据；演示新增时用更新的 lastUpdated 触发增量）
    try:
        import iris

        from backend.config import IRISConfig
        conn = iris.connect(**IRISConfig.as_dict())
        native = iris.createIRIS(conn)
        cursor = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        native.set(cursor, "^demo.Config", "sync", "cursor")
        conn.close()
        logger.info("增量同步游标已初始化: %s", cursor)
    except Exception as exc:  # noqa: BLE001 - 游标初始化失败不影响主体功能
        logger.warning("初始化增量同步游标失败（可稍后手动设置）: %s", exc)


if __name__ == "__main__":
    main()
