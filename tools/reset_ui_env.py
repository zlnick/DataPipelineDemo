# -*- coding: utf-8 -*-
"""UI 测试环境一键重置（在 dataflow-backend 容器内执行）。

重置范围 = 「从零演示」起点，与 2026-09-09 UI 测试前状态一致：
  ① 停并删除 demo 动态 Production（demo.DataflowProduction + Ens.Config 记录）
  ② 清空 ^demo.* 登记（数据源/资产/目标/映射/配置/领域模型/验证经验/数据管道实体）
  ③ 清空 Ens 消息历史（MessageHeader/MessageBody，先停 Production 再清，安全）
  ④ 清空 demo 目标表数据（PatientEntity/PatientSource/FHIRQueue/Patient/Observation）
  ⑤ 清空 CLINIC 命名空间四表（MedicationOrder/Diagnosis/Encounter/Patient）
  ⑥ 清除 FHIR server 测试资源（按常用类型逐个搜索并 DELETE）
服务、命名空间、表结构、术语/向量库全部保留。

用法（本机仓库根）：
    docker cp tools/reset_ui_env.py dataflow-backend:/tmp/reset_ui_env.py
    docker exec dataflow-backend python /tmp/reset_ui_env.py
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("reset_ui_env")

# 连接参数（backend 容器内 Docker 网络服务名，同 clinic_seed 约定）
IRIS_HOST = "iris"
IRIS_PORT = 1972
CLINIC_NS = "CLINIC"
USER_NS = "USER"
FHIR_BASE = "http://iris:52773/csp/healthshare/fhirserver/fhir/r4"

DEMO_GLOBALS = [
    "^demo.DataSource", "^demo.DataAsset", "^demo.Target", "^demo.Mapping", "^demo.Config",
    "^demo.SourceAsset", "^demo.TargetInterface", "^demo.TransformationPlan",
    "^demo.ValidationIssue",
    "^demo.PipelineInstance",   # 数据管道实体（受管理持久对象）
]
PRODUCTION_NAME = "demo.DataflowProduction"
DEMO_TARGET_TABLES = ["PatientEntity", "PatientSource", "FHIRQueue", "Patient", "Observation"]
CLINIC_TABLES = ["MedicationOrder", "Diagnosis", "Encounter", "Patient"]
FHIR_CLEAN_TYPES = [
    "Patient", "Encounter", "Condition", "MedicationRequest", "Medication", "MedicationDispense",
    "Observation", "AllergyIntolerance", "Procedure", "DiagnosticReport", "Immunization",
    "Organization", "Practitioner", "PractitionerRole", "CarePlan", "Goal", "ServiceRequest",
    "DocumentReference", "Specimen", "Device", "Location", "QuestionnaireResponse",
]


def step(name):
    log.info("\n===== %s =====", name)


def clean_production():
    """停 Production、删 Ens.Config.Production 记录与动态类定义（防 Ens 自动恢复）。"""
    from backend.services import iris_connector
    try:
        iris_connector.class_method_value(
            "Ens.Director", "StopProduction", PRODUCTION_NAME, 1)
        log.info("Production 已停止")
    except Exception as exc:  # noqa: BLE001
        log.info("停 Production 跳过（未运行或不存在）: %s", str(exc)[:120])
    try:
        iris_connector.class_method_value("Ens.Config.Production", "%DeleteId", PRODUCTION_NAME)
        log.info("Ens.Config.Production 记录已删除")
    except Exception as exc:  # noqa: BLE001
        log.info("删除 Ens.Config.Production 跳过: %s", str(exc)[:120])
    try:
        iris_connector.class_method_value("%SYSTEM.OBJ", "Delete", PRODUCTION_NAME, "c")
        log.info("动态 Production 类定义已删除")
    except Exception as exc:  # noqa: BLE001
        log.info("删除动态类定义跳过: %s", str(exc)[:120])
    clean_config_tables()


def clean_config_tables():
    """SQL 级强制清理 Ens 配置表残留（对象层 %DeleteId 不一定级联删 Items 行）。

    为什么必须做：只删类定义/对象记录时，Ens_Config.Production 与 Ens_Config.Item
    表里可能残留旧 Production 行与组件行（实测残留 1 + 4 行）；下次生成同名 Production 时
    这些「幽灵组件」会被复用/复活，白占许可单元并可能消费数据。
    """
    import iris.dbapi
    conn = iris.dbapi.connect(hostname=IRIS_HOST, port=IRIS_PORT, namespace=USER_NS,
                              username="superuser", password="SYS")
    try:
        cur = conn.cursor()
        for q, name in (("DELETE FROM Ens_Config.Item WHERE Production = ?", "Ens_Config.Item"),
                        ("DELETE FROM Ens_Config.Production WHERE Name = ?", "Ens_Config.Production")):
            try:
                cur.execute(q, (PRODUCTION_NAME,))
                log.info("配置表残留清理 %s（%s 行）", name, getattr(cur, "rowcount", "?"))
            except Exception as exc:  # noqa: BLE001
                log.warning("配置表残留清理 %s 失败: %s", name, str(exc)[:120])
        conn.commit()
    finally:
        conn.close()


def _demo_globals_state(native):
    """返回 {global 名: 是否仍有数据}（逐项探测），供清理前后复核。

    为什么不做「枚举 ^demo 的一级下标」：**IRIS 的 global 名字允许含点**——
    `^demo.Config` 不是某个 `^demo` 的子节点，而是**名字就叫 "demo.Config" 的独立 global**
    （2026-09-14 实测：isDefined("^demo.Config")=10，而 isDefined("^demo","Config")=0，
    iterator("^demo") 返回空）。因此**没有共同的根可以枚举**：DEMO_GLOBALS 白名单就是唯一事实源，
    新增任何 `^demo.X` 必须同步登记到本文件顶部，否则重置不干净（历史踩坑：幽灵数据管道实体）。
    """
    state = {}
    for g in DEMO_GLOBALS:
        try:
            state[g] = bool(native.isDefined(g))
        except Exception as exc:  # noqa: BLE001
            state[g] = "err: " + str(exc)[:80]
    return state


def kill_demo_globals():
    from backend.services import iris_connector
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        before = _demo_globals_state(native)
        log.info("重置前仍存在的 ^demo.* global：%s",
                 ", ".join(g for g, v in before.items() if v is True) or "（无）")
        for g in DEMO_GLOBALS:
            try:
                native.kill(g)
                log.info("已清空 %s", g)
            except Exception as exc:  # noqa: BLE001
                log.warning("清空 %s 失败: %s", g, exc)
        # 可信体检：逐个白名单 global 复核确实已空（旧版扫 ^demo 一级下标永远为空 —— 见 _demo_globals_state）
        leftover = [g for g, v in _demo_globals_state(native).items() if v is True]
        if leftover:
            log.warning("⚠ 仍有未清空的 ^demo global：%s", ", ".join(leftover))
        else:
            log.info("体检通过：DEMO_GLOBALS 全部已清空")
    finally:
        conn.close()


def _appdata_items(native):
    """枚举 ^Ens.AppData 一级下标（= 所有曾登记的适配器 Item 名）。

    为什么要枚举而不是硬编码名字：SQL 源 BS 名会随数据管道类别/去重变化
    （`SQLService_Patient`、`SQLService_Patient__sql2fhir_patient_tx`、`SQLService_Patient_2` …），
    只清默认名会漏清 → 重新生成后旧凭证残留、同批数据被判为"已扫描"而跳过
    （前端误报"生成失败"）。
    """
    names = set()
    try:
        sub = native.nextSubscript(False, "^Ens.AppData", "")
    except Exception as exc:  # noqa: BLE001 - 枚举失败则退回配置表名单
        log.warning("枚举 ^Ens.AppData 下标失败（仅用配置表名单）: %s", str(exc)[:120])
        return names
    while sub:
        names.add(str(sub))
        try:
            sub = native.nextSubscript(False, "^Ens.AppData", sub)
        except Exception as exc:  # noqa: BLE001
            log.warning("继续枚举 ^Ens.AppData 失败: %s", str(exc)[:120])
            break
    return names


def clear_sql_source_appdata():
    """清 SQL 源 BS 的扫描凭证与错误行（Production 已停，安全）。

    为什么必须清：GenericService 以 KeyFieldName 记录"已扫描到哪"，并记住出错行；
    不清除会让**重新 seed 的同号数据被当作已处理而跳过**（曾致重新生成后零消息、
    前端误报"生成失败"）。
    注意：EnsLib.SQL.InboundAdapter.ClearAllAppData 在本版本 IRIS 报
    `<COMMAND> *Function must return a value`（库方法 bug，无效），故直接清 global。
    """
    import iris

    from backend.services import iris_connector
    # 覆盖三层来源：全局枚举（含历史/按类别改名）+ 配置表（Production 尚在时最准）
    #             + 旧版默认名（兼容极早期环境）
    items = {"SQLService_Patient"}
    nconn = iris.connect(IRIS_HOST, IRIS_PORT, USER_NS, "superuser", "SYS")
    try:
        native = iris.createIRIS(nconn)
        items |= _appdata_items(native)
        try:
            for r in iris_connector.query(
                    "SELECT Name FROM Ens_Config.Item "
                    "WHERE ClassName='EnsLib.SQL.Service.GenericService'"):
                if r[0]:
                    items.add(str(r[0]))
        except Exception:  # noqa: BLE001 - 配置已删时忽略
            pass
        done = 0
        for item in sorted(items):
            for g, subs in (("^Ens.AppData", (item, "adapter.sqlrow")),
                            ("^Ens.AppData", (item, "adapter.sqlparam")),
                            ("^IRIS.Temp.Adapter.sqlrow", (item,))):
                try:
                    native.kill(g, *subs)
                    done += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("清 %s(%s) 失败: %s", g, item, exc)
        log.info("SQL 源扫描凭证/错误行已清（覆盖 Item: %s，%d 项）",
                 ", ".join(sorted(items)), done)
    finally:
        nconn.close()


def clear_generated_classes():
    """删除 Agent 生成的 BP 类与 /dur/generated 源码（下次生成由 Agent 重新产出）。"""
    from backend.services import iris_connector
    try:
        iris_connector.class_method_value("%SYSTEM.OBJ", "Delete",
                                          "demo.SqlFhirPatientTxProcess", "c")
        log.info("BP 类 demo.SqlFhirPatientTxProcess 已删除")
    except Exception as exc:  # noqa: BLE001
        log.info("删除 BP 类跳过: %s", str(exc)[:120])
    for f in ("/dur/generated/SqlFhirPatientTxProcess.cls",
              "/dur/generated/DataflowProduction.cls"):
        try:
            iris_connector.class_method_value("%Library.File", "Delete", f)
        except Exception as exc:  # noqa: BLE001
            log.info("删除 %s 跳过: %s", f, str(exc)[:120])
    log.info("生成物清理完成（/dur/generated 源码 + BP 类）")


def clear_ens_messages():
    import iris.dbapi
    conn = iris.dbapi.connect(hostname=IRIS_HOST, port=IRIS_PORT, namespace=USER_NS,
                              username="superuser", password="SYS")
    try:
        cur = conn.cursor()
        for t in ("Ens.MessageBody", "Ens.MessageHeader"):
            try:
                cur.execute(f"DELETE FROM {t}")
                log.info("消息表 %s 已清空（%s 行）", t,
                         getattr(cur, "rowcount", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("清空 %s 失败: %s", t, str(exc)[:120])
        conn.commit()
    finally:
        conn.close()


def clear_user_tables():
    import iris.dbapi
    conn = iris.dbapi.connect(hostname=IRIS_HOST, port=IRIS_PORT, namespace=USER_NS,
                              username="superuser", password="SYS")
    try:
        cur = conn.cursor()
        for t in DEMO_TARGET_TABLES:
            try:
                cur.execute(f"DELETE FROM {t}")
                log.info("目标表 %s 数据已清空（%s 行）", t,
                         getattr(cur, "rowcount", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("目标表 %s 清空失败（可能不存在）: %s", t, str(exc)[:120])
        conn.commit()
    finally:
        conn.close()


def clear_clinic_tables():
    import iris.dbapi
    conn = iris.dbapi.connect(hostname=IRIS_HOST, port=IRIS_PORT, namespace=CLINIC_NS,
                              username="superuser", password="SYS")
    try:
        cur = conn.cursor()
        for t in CLINIC_TABLES:
            try:
                cur.execute(f"DELETE FROM {t}")
                log.info("CLINIC 表 %s 数据已清空（%s 行）", t,
                         getattr(cur, "rowcount", 0) or 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("CLINIC 表 %s 清空失败: %s", t, str(exc)[:120])
        conn.commit()
    finally:
        conn.close()



def purge_fhir_resources():
    """按类型搜索并逐个 DELETE FHIR server 上的测试资源（demo 专用服务）。

    注意：IRIS FHIR Server 对 DELETE 返回空 200 响应体（非 JSON），
    不能用要求 JSON 的 fhir_client._request 判定——这里用 urllib 直接删除，
    2xx 即视为成功并复核各类型剩余数。
    """
    import base64
    import urllib.error
    import urllib.request

    from backend.services import fhir_client

    def _search(rt):
        try:
            return fhir_client._request(f"{FHIR_BASE}/{rt}?_count=1000",
                                        "superuser", "SYS")
        except Exception as exc:  # noqa: BLE001
            log.warning("搜索 %s 失败: %s", rt, str(exc)[:120])
            return None

    token = base64.b64encode(b"superuser:SYS").decode("ascii")

    def _delete(url: str) -> bool:
        req = urllib.request.Request(url, method="DELETE")
        req.add_header("Accept", "application/fhir+json")
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()  # IRIS FHIR DELETE 返回空 body，读完即可
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            return 200 <= exc.code < 300
        except Exception:  # noqa: BLE001
            return False

    total = 0
    for rt in FHIR_CLEAN_TYPES:
        bundle = _search(rt)
        if not bundle:
            continue
        ids = []
        for e in bundle.get("entry", []) or []:
            r = e.get("resource") or {}
            if r.get("id"):
                ids.append(r["id"])
        ok = 0
        for rid in ids:
            if _delete(f"{FHIR_BASE}/{rt}/{rid}"):
                ok += 1
        if ok:
            total += ok
            log.info("FHIR %s 已删除 %s/%s 个资源", rt, ok, len(ids))
        # 复核剩余数（0 = 清理干净）
        after = _search(rt)
        remain = len((after or {}).get("entry", []) or [])
        log.info("FHIR %s 剩余: %s", rt, remain)
    log.info("FHIR 测试资源清理合计删除 %s 个", total)


def verify():
    """重置后核对：登记为空、各表计数为 0。"""
    import iris.dbapi
    from backend.services import repository

    ds = repository.list_datasources()
    tgs = repository.list_targets()
    log.info("\n===== 重置核对 =====")
    log.info("数据源登记数: %s；数据目标登记数: %s", len(ds), len(tgs))

    conn = iris.dbapi.connect(hostname=IRIS_HOST, port=IRIS_PORT, namespace=USER_NS,
                              username="superuser", password="SYS")
    try:
        cur = conn.cursor()
        for t in DEMO_TARGET_TABLES:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                log.info("目标表 %s 行数: %s", t, cur.fetchone()[0])
            except Exception as exc:  # noqa: BLE001
                log.warning("目标表 %s 查询失败（可能不存在）: %s", t, str(exc)[:100])
    finally:
        conn.close()

    conn2 = iris.dbapi.connect(hostname=IRIS_HOST, port=IRIS_PORT, namespace=CLINIC_NS,
                               username="superuser", password="SYS")
    try:
        cur = conn2.cursor()
        for t in CLINIC_TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            log.info("CLINIC 表 %s 行数: %s", t, cur.fetchone()[0])
    finally:
        conn2.close()


def main():
    step("1/8 停/删 demo Production（含 Ens 配置表残留清理）")
    clean_production()
    step("2/8 清 SQL 源扫描凭证与错误行（防重 seed 后零消息）")
    clear_sql_source_appdata()
    step("3/8 清空 ^demo.* 登记")
    kill_demo_globals()
    step("4/8 清空 Ens 消息历史")
    clear_ens_messages()
    step("5/8 清空 demo 目标表")
    clear_user_tables()
    step("6/8 清空 CLINIC 四表")
    clear_clinic_tables()
    step("7/8 清生成物（BP 类 + /dur/generated 源码）")
    clear_generated_classes()
    step("8/8 清除 FHIR server 测试资源")
    purge_fhir_resources()
    verify()
    log.info("\n重置完成：演示可从零开始（服务/命名空间/表结构保留）")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log.error("重置失败: %s", exc)
        sys.exit(1)

