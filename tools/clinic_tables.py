# -*- coding: utf-8 -*-
"""CLINIC 命名空间：源库四表初始化（Patient/Encounter/Diagnosis/MedicationOrder）。

表结构已收敛到单一事实源 ``backend/services/clinic_schema.py``
（clinic_seed / init_data / 本工具共用同一份 DDL）。

用法（backend 容器内；宿主机用 tools/clinic_init.sh 包装调用）：
    python3 tools/clinic_tables.py            # 默认：幂等 ensure —— 缺表才建，**已有表与数据不动**
    python3 tools/clinic_tables.py --reset    # 重置：DROP 四表后重建（**会清空数据**，仅重置场景）
    python3 tools/clinic_tables.py --host iris
"""
import argparse
import logging
import os
import time

import iris.dbapi

from backend.services.clinic_schema import CLINIC_TABLES, ensure_tables

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def default_host() -> str:
    """容器内（/app 存在）默认 iris；宿主机默认 127.0.0.1。"""
    return os.getenv("CLINIC_IRIS_HOST") or ("iris" if os.path.isdir("/app") else "127.0.0.1")


def connect(host: str, port: int, namespace: str, wait: int = 0):
    """连接 IRIS；wait>0 时按秒重试（**首次安装**时 IRIS 初始化需数分钟）。"""
    t0 = time.time()
    while True:
        try:
            return iris.dbapi.connect(hostname=host, port=port, namespace=namespace,
                                      username="superuser", password="SYS")
        except Exception:  # noqa: BLE001 - 未就绪时连接失败属正常
            if time.time() - t0 >= wait:
                raise
            logger.info("等待 IRIS(%s) 就绪…（已等 %.0fs）", host, time.time() - t0)
            time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=default_host(), help="IRIS 主机（默认按运行环境推断）")
    ap.add_argument("--port", type=int, default=1972)
    ap.add_argument("--namespace", default="CLINIC")
    ap.add_argument("--reset", action="store_true", help="DROP 四表后重建（会清空数据）")
    ap.add_argument("--wait", type=int, default=0, help="等 IRIS 就绪的秒数（首次安装建议 180）")
    a = ap.parse_args()

    conn = connect(a.host, a.port, a.namespace, wait=a.wait)
    try:
        cur = conn.cursor()
        rows, created = ensure_tables(cur, reset=a.reset)
        conn.commit()
        logger.info("CLINIC 四表就绪（本次新建：%s）", ",".join(created) or "无")
        for name, cnt in rows:
            logger.info("  %s 行数=%s", name, cnt)
        logger.info("表清单：%s", ", ".join(CLINIC_TABLES))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
