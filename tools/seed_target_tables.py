"""向 USER 库目标表 Patient / Observation 直接写入测试数据（不经过任何数据管道）。

用途：给「转换目标表」（FHIR→DB 管道的落库目标）快速铺演示数据，
      便于在 Pipelines 页「目标数据」查看器 / 前端表格里看到效果。造的是源侧测试数据时不要用本脚本。

用法（backend 容器内执行；datakit 入口可自动进容器）：
    cd tools/datakit && ./run.sh seed_target_tables.py --count 3
    ./run.sh seed_target_tables.py --count 2 --family 赵 --given 敏 --obs 1
    ./run.sh seed_target_tables.py --clear          # 先清空 Patient/Observation 再写

说明：
- Patient 列：ID / FamilyName / GivenName / Gender / BirthDate / Phone / Address / City
- Observation 列：ID / PatientID / Code / CodeDisplay / Value / Unit / EffectiveDate / Status
- 默认每个患者配 2 条 Observation（PatientID 关联本次写入的患者）；
- 主键带时间戳后缀，重复执行只追加不覆盖（--clear 才清表）。
"""

import argparse
import random
import time

import iris.dbapi

# 与 backend/config.IRISConfig 对齐（容器内直连 IRIS 超级服务器）
IRIS = {
    "hostname": "iris",
    "port": 1972,
    "namespace": "USER",
    "username": "superuser",
    "password": "SYS",
}

SURNAMES = ["张", "李", "王", "赵", "钱", "孙", "吴", "周", "陈", "刘"]
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛"]
CITY = ["杭州", "上海", "北京", "广州", "成都", "武汉"]

# 常用检验观察项（LOINC 编码 / 中文显示 / 单位 / 参考取值区间）
OBS_DEFS = [
    ("8867-4", "心率", "次/分", (55.0, 100.0)),
    ("8310-5", "体温", "℃", (36.0, 37.5)),
    ("8480-6", "收缩压", "mmHg", (90.0, 140.0)),
    ("8462-4", "舒张压", "mmHg", (60.0, 90.0)),
    ("29463-7", "体重", "kg", (45.0, 90.0)),
    ("2339-0", "血糖", "mmol/L", (3.9, 7.5)),
]


def build_patient(idx: int, stamp: int, family: str, given: str) -> list:
    """构造一行 Patient 记录（列顺序与建表语句一致）。"""
    pid = f"T{stamp}{idx}"
    fam = family or random.choice(SURNAMES)
    giv = given or random.choice(GIVEN)
    gender = "male" if idx % 3 else "female"
    birth = f"19{random.randint(60, 99)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
    city = CITY[idx % len(CITY)]
    return [pid, fam, giv, gender, birth, f"139{stamp}{idx:02d}",
            f"{city}测试路{idx + 1}号", city]


def build_observation(row: list, idx: int, jdx: int, stamp: int, effective: str) -> list:
    """构造一行 Observation 记录（PatientID 关联给定患者行）。"""
    code, display, unit, (low, high) = random.choice(OBS_DEFS)
    return [f"O{stamp}{idx}{jdx}", row[0], code, display,
            f"{round(random.uniform(low, high), 1)}", unit, effective, "final"]


def main() -> None:
    """入口：解析参数 → 连接 IRIS → 写目标表 → 打印计数与样例。"""
    ap = argparse.ArgumentParser(description="向 USER 库 Patient/Observation 目标表写测试数据")
    ap.add_argument("--count", type=int, default=3, help="Patient 行数（默认 3）")
    ap.add_argument("--obs", type=int, default=2,
                    help="每个患者的 Observation 行数（默认 2；0 = 不造）")
    ap.add_argument("--family", default="", help="指定姓，默认随机")
    ap.add_argument("--given", default="", help="指定名，默认随机")
    ap.add_argument("--clear", action="store_true", help="写入前先清空 Patient/Observation 两表")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（便于复现）")
    args = ap.parse_args()
    if args.seed is not None:
        random.seed(args.seed)

    conn = iris.dbapi.connect(**IRIS)
    cur = conn.cursor()
    try:
        if args.clear:
            cur.execute("DELETE FROM SQLUser.Observation")
            cur.execute("DELETE FROM SQLUser.Patient")
            conn.commit()
            print("[清空] SQLUser.Patient / SQLUser.Observation 已清空")

        stamp = int(time.time() * 1000) % 100000
        effective = time.strftime("%Y-%m-%dT%H:%M:%S")
        p_rows, o_rows = [], []
        for i in range(args.count):
            row = build_patient(i, stamp, args.family, args.given)
            p_rows.append(row)
            for j in range(args.obs):
                o_rows.append(build_observation(row, i, j, stamp, effective))

        cur.executemany(
            "INSERT INTO SQLUser.Patient (ID,FamilyName,GivenName,Gender,BirthDate,"
            "Phone,Address,City) VALUES (?,?,?,?,?,?,?,?)", p_rows)
        if o_rows:
            cur.executemany(
                "INSERT INTO SQLUser.Observation (ID,PatientID,Code,CodeDisplay,Value,"
                "Unit,EffectiveDate,Status) VALUES (?,?,?,?,?,?,?,?)", o_rows)
        conn.commit()

        print(f"[写入] Patient {len(p_rows)} 行 / Observation {len(o_rows)} 行")
        cur.execute("SELECT COUNT(*) FROM SQLUser.Patient")
        print("Patient 总行数:", cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM SQLUser.Observation")
        print("Observation 总行数:", cur.fetchone()[0])
        print("Patient 样例:", p_rows[0] if p_rows else "-")
        print("Observation 样例:", o_rows[0] if o_rows else "-")
        print("提示：Pipelines 页「目标数据」选 Patient/Observation 即可看到。")
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
