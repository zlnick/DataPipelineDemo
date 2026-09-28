# -*- coding: utf-8 -*-
"""术语服务器向量能力：**状态检查 + 缺表补建 + 使用指引**（幂等；可选步骤，不进默认初始化）。

事实（2026-09-27 实测）：
- 术语服务器的向量能力**开箱可用** —— `Terminology.Vector.TermEmbedding` 表
  （列 ID/Code/Embedding/Lang/Model/ReleaseId/SystemUri/Text）、`/terminology/vector/search`、
  `/terminology/vector/crosswalk` 路由都在；`Vector/Utils.cls` 经 `^Config("Vector","EmbeddingHost")`
  （默认 `embedding:8000`）调用本地 embedding 服务。
- 缺的只是**向量数据**（默认 0 行），由读者按需生成：`tools/dx_vectorize.py`（中文 ICD-10 全量）、
  `tools/term_embed.py`（任意术语集）、`tools/rxnorm_import.py --save-vec` / `run_rxnorm_vec.sh`（RxNorm）。
- 演示核心**不使用向量**（术语转换只用成品映射 `data/seeds/term_map_seed.json`）。
- fork 里的 `/tmp/embedding-configuration.sql` 是**另一套列定义**（CodeSystem/DescriptionEmbedding），
  与本项目工具使用的表结构不一致，故本脚本**不导入它**；表结构以 `Terminology.Vector.TermEmbedding`
  类为准（缺表时编译该类即可建表）。

用法（宿主机）:
    bash tools/termsrv_vector_init.sh            # 检查状态（缺表则补建）
    bash tools/termsrv_vector_init.sh --check    # 只检查
"""
import argparse
import os
import sys

import iris
import iris.dbapi

NS = "TERMINOLOGY"
TABLE = "Terminology_Vector.TermEmbedding"
CLASS = "Terminology.Vector.TermEmbedding"


def table_state(cur):
    """返回 (是否存在, 总行数, [(SystemUri, 条数)])。"""
    cur.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = 'Terminology_Vector' AND TABLE_NAME = 'TermEmbedding'")
    exists = int(cur.fetchone()[0]) > 0
    if not exists:
        return False, 0, []
    cur.execute("SELECT COUNT(*) FROM %s" % TABLE)
    total = int(cur.fetchone()[0])
    cur.execute("SELECT SystemUri, COUNT(*) FROM %s GROUP BY SystemUri" % TABLE)
    return True, total, [(r[0], int(r[1])) for r in cur.fetchall()]


def compile_class(host: str, port: int) -> bool:
    """缺表时编译持久类以建表（IRIS 持久类的表随类编译创建）。"""
    try:
        conn = iris.connect(host, port, NS, "superuser", "SYS")
        try:
            iris.createIRIS(conn).classMethodVoid("%SYSTEM.OBJ", "Compile", CLASS, "ck")
            return True
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        print("  [!!] 编译 %s 失败：%s" % (CLASS, str(exc)[:160]))
        return False


def connect(host: str, port: int):
    return iris.dbapi.connect(hostname=host, port=port, namespace=NS,
                              username="superuser", password="SYS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.getenv("TERM_IRIS_HOST", "iris-terminology"))
    ap.add_argument("--port", type=int, default=1972)
    ap.add_argument("--check", action="store_true", help="只检查，不做补建")
    a = ap.parse_args()

    conn = connect(a.host, a.port)
    cur = conn.cursor()
    exists, total, per = table_state(cur)
    if not exists and not a.check:
        print("向量表缺失，尝试编译 %s 建表…" % CLASS)
        conn.close()
        compile_class(a.host, a.port)
        conn = connect(a.host, a.port)
        cur = conn.cursor()
        exists, total, per = table_state(cur)

    print("== 术语服务器向量能力状态 ==")
    print("  向量表 %s : %s" % (TABLE, "存在" if exists else "**缺失**"))
    print("  向量条数 : %d" % total)
    for uri, n in per:
        print("    - %s : %d" % (uri, n))
    conn.close()

    if not exists:
        print("  [!!] 表仍缺失：请检查 iris-terminology 镜像/日志（应随 Terminology.Vector.* 类自动建表）")
        return 1

    print()
    print("演示核心**不需要**向量（术语转换只用成品映射 data/seeds/term_map_seed.json）；")
    print("想试验术语向量化 / 语义检索 / AI 补录映射的读者：")
    print("  ① docker compose up -d embedding          # 本地向量服务（Qwen3-Embedding-0.6B，首次下载 ~1.1GB）")
    print("  ② python3 tools/dx_vectorize.py --zh      # 例：中文 ICD-10 全量向量化（~2 万条；也可用 term_embed.py）")
    print("     bash run_rxnorm_vec.sh                  # 例：RxNorm 全量向量化（需自备原始数据）")
    print("  ③ curl -u superuser:SYS 'http://localhost:52774/terminology/vector/search?q=阿司匹林'")
    print("  ④ python3 tools/term_map_build.py         # 完整链路：向量召回 → LLM 判码 → 写回映射")
    return 0


if __name__ == "__main__":
    sys.exit(main())
