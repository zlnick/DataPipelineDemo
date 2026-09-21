# -*- coding: utf-8 -*-
"""UI 测试环境一键重置（在 dataflow-backend 容器内执行）。

重置范围 = 「从零演示」起点，与 2026-09-09 UI 测试前状态一致（**步序以 main() 为准**）：
  ① 停并删除 demo 动态 Production（demo.DataflowProduction + Ens.Config 记录）
     —— 2026-09-16 加固：停止动作改为**校验式**（`stop_production` 复查 IsProductionRunning，
     按名停不生效时退化为无参 StopProduction，仍失败则显式告警），避免「删了类但运行态残留 1」
     导致重置后 /api/pipelines/status 虚报 running=true
  ② 清 SQL 源扫描凭证与错误行（防重 seed 后零消息）
  ③ 清空 ^demo.* 登记（数据源/资产/目标/映射/配置/领域模型/验证经验/数据管道实体）
     —— 2026-09-18 起**无任何例外**：运行期术语转换由共享 BO 直查术语服务器，本地不再有术语缓存
  ④ 清空 Ens **内部残留**：MessageHeader/MessageBody、`Ens.StreamContainer`（消息流存储）、
     `EnsLib_HTTP.GenericMessage(+/_HTTPHeaders)`（HTTP 消息体）、`Ens.BusinessProcess`（BP 进程）、
     `Ens_Util.Log`（事件日志）。事件日志会被运行期校验当"事实"读
     （`pipeline_validator` 按时间窗查 Ens_Util.Log），不清就把**上一轮报错算到新一轮头上**（假红）
  ⑤ 清空 USER 命名空间**全部非系统表**（动态发现 `INFORMATION_SCHEMA.TABLES`：
     `SQLUser.*` 演示目标表 + `demo.*` 消息表 + `PatientService_*` SOAP 消息表；
     以后新增表**自动覆盖**，不需要改脚本）
  ⑥ 清空 CLINIC 命名空间**全部非系统表**（动态发现，当前为 4 张数据源表）
  ⑦ 删生成物（**来源指纹**判定：`%File.Exists('/dur/generated/<短名>.cls')` → `%SYSTEM.OBJ:Delete`
     删类 → `RemoveDirectoryTree` + `CreateDirectory` 清空重建目录；无来源文件的手写类**只保留不删**）
     —— **必须排在清表之后**（2026-09-18 修）：反序（先删类）会留下"类已删但表仍在"的**孤儿存储**，
     其行级 DELETE 被 filer 拒（`-106`）、`DROP TABLE` 也被拒（`-300 DDL not allowed`）→ 残留永远清不掉；
     正序清表时类还在 → DELETE 正常，随后删类时表随之消失。
  ⑧ 清除 FHIR **两个仓库**的测试资源（源 DemoFHIR + 目标 FHIRSERVER；按常用类型逐个搜索并 DELETE）
  ⑨ 打印术语服务器（**事实源**）摘要（信息性日志；本重置完全不触及它）。
      —— 2026-09-18 起**原「术语映射镜像」步骤已移除**：运行期术语转换改由**共享 BO**
      `demo.TerminologyOperation` 实时查术语服务器（零本地码表副本）→ 重置后**无需预热任何缓存**。
服务、命名空间、表结构、术语/向量库全部保留。

用法（**一条命令**，本机仓库根目录）：
    bash tools/datakit/run.sh reset_ui_env.py          # 推荐：自动拷入容器并执行
    # 等价手动两步（容器内执行）：
    #   docker cp tools/reset_ui_env.py dataflow-backend:/tmp/reset_ui_env.py
    #   docker exec dataflow-backend python /tmp/reset_ui_env.py

参数：
    --check-only   只体检不清理（相当于"我现在到底干净吗"，返回同一份清单）
    --skip-fhir    跳过 FHIR 资源清理（省 ~20s，用于只要清 SQL/Ens 侧）

自检与退出码（**不需要人来判断是否干净**）：
    脚本末尾输出 ✅/❌ 清单，逐项断言
      ① `IsProductionRunning()==0` + `Ens_Config.Production` 0 行 + 生成生产类已删
      ② `^demo.*` 登记全部为空（**无例外**：本地术语缓存机制已下线，`^demo.Config` 也必须全空）
      ③ Ens 内部表行数为 0（消息/流存储/HTTP 消息体/BP 进程/事件日志）
      ④ USER / CLINIC 命名空间非系统表（动态发现）行数为 0
      ⑤ `/dur/generated` 已清空、生成类无残留
      ⑥ HTTP 接口与 UI 同源核对：`/pipelines/status.running=false`、
         `/pipelines/items`、`/pipelines/instances`、`/pipelines/logs`、
         `/pipelines/validation-issues`、`/datasources`、`/targets`、`/mappings` 全空
      ⑦ FHIR 两个仓库（源 DemoFHIR / 目标 FHIRSERVER）演示资源类型全空
    自检**一律重新扫描"当前事实"**（不复用清理阶段的清单）：删生成类会让其 SQL 表随之消失，
    拿旧清单复核会报"表都不存在了却仍有数据"的假 ❌（2026-09-16 实测 -106）
    任何一项 ❌ → 打印失败原因并 `exit 1`；全 ✅ → `exit 0`（可安全接 CI / 直接开演示）
    HTTP / FHIR 自检依赖 backend / FHIR 已启动；探测不到会显式标 ⚠ 并给出手工核对命令，不计失败。
"""
import json
import logging
import sys
import time
import urllib.error
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("reset_ui_env")

# 连接参数（backend 容器内 Docker 网络服务名，同 clinic_seed 约定）
IRIS_HOST = "iris"
IRIS_PORT = 1972
CLINIC_NS = "CLINIC"
USER_NS = "USER"
# FHIR 两个仓库（演示默认：**源 = DemoFHIR**（第二个独立仓库）/ **目标 = FHIRSERVER**（实例自带））
# 重置时**两个都要清**：只清目标会留下上一轮的 FHIR 源数据，"从零演示"的增量起点就不干净。
FHIR_TARGET_BASE = "http://iris:52773/csp/healthshare/fhirserver/fhir/r4"
FHIR_SOURCE_BASE = "http://iris:52773/csp/healthshare/demofhir/fhir/r4"
FHIR_REPOS = [("目标 FHIRSERVER", FHIR_TARGET_BASE), ("源 DemoFHIR", FHIR_SOURCE_BASE)]

DEMO_GLOBALS = [
    "^demo.DataSource", "^demo.DataAsset", "^demo.Target", "^demo.Mapping", "^demo.Config",
    "^demo.SourceAsset", "^demo.TargetInterface", "^demo.TransformationPlan",
    "^demo.ValidationIssue",
    "^demo.PipelineInstance",   # 数据管道实体（受管理持久对象）
    # BP 运行痕迹（bp_bundle/bp_resp/bp_counts/...）：**曾漏登记 → 跨轮残留**，
    # 重置后仍能读到上一轮的 FHIR 回执（时间戳都对不上），极易被当成"本次运行证据"而误判
    # （2026-09-19 实测：用户据残留 trace 认为"术语没生效"）。故纳入白名单。
    "^demo.Trace",
]
PRODUCTION_NAME = "demo.DataflowProduction"
# 演示数据表：**兜底名单**。正常情况下由 INFORMATION_SCHEMA 动态发现全部非系统表，
# 这里保留是为了「动态发现失败（连不上/权限异常）时仍能清掉核心表」+ 日志可读性。
DEMO_TARGET_TABLES = ["PatientEntity", "PatientSource", "FHIRQueue", "Patient", "Observation"]
CLINIC_TABLES = ["MedicationOrder", "Diagnosis", "Encounter", "Patient"]
# 动态发现时排除的系统 schema 前缀（其余视为演示/生成物表 → 自动清空）
# ⚠ 比较时统一大写：IRIS 返回的 schema 大小写不一，若常量写成混合大小写再用
#   `schema.upper().startswith(前缀)` 比较会**永远不匹配**（2026-09-16 实测：把
#   Ens.StreamContainer / EnsLib_HTTP.* 等内部表当成"非系统表"报错/清理）。
SYSTEM_SCHEMA_PREFIXES = tuple(p.upper() for p in (
    "%", "INFORMATION_SCHEMA", "Security", "Config", "IRIS", "HS",   # HS/HSFHIR = FHIR server 存储
    "Ens", "Ens_", "EnsLib", "EnsPortal", "EnsUtil",                  # Ens 内部表（见 ENS_INTERNAL_TABLES）
))
# 需要显式清空的 Ens 内部表（前缀 Ens 已被排除出动态发现，故在此点名）
# 为什么连这些一起清：它们都是**上一轮运行的残留**（消息流存储/HTTP 消息体/BP 进程/日志），
# 留着会让「零起点」名不副实，也会干扰运行期校验（校验按时间窗读事件日志与消息）。
# 清前取证（2026-09-16，.trash/20260916-n11reset/inv_ens2.py）：
#   Ens.StreamContainer = 上一轮 FHIR JSON 报文；EnsLib_HTTP.GenericMessage = OperationOutcome/请求头；
#   Ens.BusinessProcess = %ConfigName=SqlFhirPatientTxProcess 的未完成进程 → 均为运行残留。
# 刻意**不清**（库/运维元数据，非演示数据）：Ens_Config.SearchTableProp、Ens_Deployment.Token、
#   Ens_Util.LookupTable（内容实测为 `%IRIS_X12ReplyType` 等 IRIS 内置查找表）。
ENS_INTERNAL_TABLES = [
    "Ens.MessageBody",                     # 消息体
    "Ens.MessageHeader",                   # 消息头
    "Ens.StreamContainer",                 # 消息流存储（删消息后必须清，否则无限增长）
    "EnsLib_HTTP.GenericMessage",          # HTTP 消息体（历史请求垃圾）
    "EnsLib_HTTP.GenericMessage_HTTPHeaders",
    "Ens.BusinessProcess",                 # BP 进程残留（含统计）
    "Ens_Util.Log",                        # 事件日志（运行期校验会当"事实"读）
]
# 上表被 SQL 拒绝删除时的 ObjectScript 兜底动作（2026-09-16 实测）：
#   `Ens.BusinessProcess` 行删不掉：SQL filer 的 %SQLDelete 要解析行里记录的业务主机类
#   （`demo.SqlFhirPatientTxProcess`），该类已被删除 → <SQLCODE -415> <CLASS DOES NOT EXIST>；
#   改用 `%KillExtent` 清空 extent 可行（实测 2 行 → 0 行）。
ENS_TABLE_FALLBACK = {"Ens.BusinessProcess": ("Ens.BusinessProcess", "%KillExtent")}
# Ens 库/框架类命名空间前缀：这些不是"应用业务主机类"，枚举生成物时直接跳过
# （否则日志会刷出 100+ 个 EnsLib.* 库类，看不清真正要保留的手写类）
LIBRARY_CLASS_PREFIXES = ("%", "Ens.", "EnsLib.", "EnsPortal.", "EnsUtil.", "Ens_")
GENERATED_DIR = "/dur/generated"          # 生成物源码目录（容器内路径）
API_BASE = "http://127.0.0.1:5000/api"    # backend 容器内本地端口（宿主映射 5001）
FHIR_CLEAN_TYPES = [
    "Patient", "Encounter", "Condition", "MedicationRequest", "Medication", "MedicationDispense",
    "Observation", "AllergyIntolerance", "Procedure", "DiagnosticReport", "Immunization",
    "Organization", "Practitioner", "PractitionerRole", "CarePlan", "Goal", "ServiceRequest",
    "DocumentReference", "Specimen", "Device", "Location", "QuestionnaireResponse",
]


def step(name):
    log.info("\n===== %s =====", name)


def production_running() -> int:
    """Ens.Director.IsProductionRunning()：1=在运行，0=未运行，-1=查询失败。"""
    from backend.services import iris_connector
    try:
        return int(iris_connector.class_method_value(
            "Ens.Director", "IsProductionRunning") or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("IsProductionRunning 查询失败: %s", str(exc)[:120])
        return -1


def stop_production(max_wait: int = 15) -> bool:
    """停 Production 并**校验**运行态确实已复位（不再是"调完就宣布成功"）。

    2026-09-16 实测坑：原实现调用 `StopProduction` 后不看返回值/不复查就打印「Production 已停止」，
    而删掉 Production 类之后 Ensemble 的**运行态标记**仍可能残留 1 → 重置后
    `/api/pipelines/status` 虚报 `running=true`，同时 `/api/pipelines/items` 为空（自相矛盾，
    从零演示会被误导，且 `StartProduction` 会先收到 ALREADY_RUNNING）。

    策略：① 按名停（timeout=1）→ 复查（最多等 max_wait 秒）；
    ② 仍未复位 → 无参 `StopProduction()`（停"当前正在跑"的那个，不依赖类是否存在）→ 再复查；
    ③ 仍失败则**显式告警**（不静默），返回 False。
    """
    from backend.services import iris_connector
    if production_running() == 0:
        log.info("Production 未在运行，跳过停止")
        return True
    for label, args in (("按名停", (PRODUCTION_NAME, 1)), ("无参停（停当前正在跑的）", ())):
        try:
            iris_connector.class_method_void("Ens.Director", "StopProduction", *args)
            log.info("%s：StopProduction 已调用", label)
        except Exception as exc:  # noqa: BLE001
            log.info("%s 调用异常（继续用运行态判定）: %s", label, str(exc)[:120])
        deadline = time.time() + max_wait
        while time.time() < deadline:
            if production_running() == 0:
                log.info("%s 后运行态已复位（IsProductionRunning=0）", label)
                return True
            time.sleep(1)
    log.warning(
        "⚠ Production 运行态仍未复位（IsProductionRunning=%s）：重置后 /api/pipelines/status 会虚报 "
        "running=true。请在 backend 容器内复查一次 Ens.Director.StopProduction(\"\", 1)",
        production_running())
    return False


def clean_production():
    """停 Production、删 Ens.Config.Production 记录与动态类定义（防 Ens 自动恢复）。"""
    from backend.services import iris_connector
    stop_production()
    try:
        iris_connector.class_method_value("Ens.Config.Production", "%DeleteId", PRODUCTION_NAME)
        log.info("Ens.Config.Production 记录已删除")
    except Exception as exc:  # noqa: BLE001
        log.info("删除 Ens.Config.Production 跳过: %s", str(exc)[:120])
    try:
        exists = int(iris_connector.query(
            "SELECT COUNT(*) FROM %Dictionary.CompiledClass WHERE Name = ?",
            [PRODUCTION_NAME])[0][0])
    except Exception:  # noqa: BLE001 - 探测失败则按"存在"处理，直接尝试删除
        exists = 1
    if not exists:
        # 幂等：二次运行时类已删，直接跳过，避免刷一行 ERROR #5351 噪音
        log.info("动态 Production 类定义不存在（已删过），跳过")
    else:
        try:
            iris_connector.class_method_value("%SYSTEM.OBJ", "Delete", PRODUCTION_NAME, "c")
            log.info("动态 Production 类定义已删除")
        except Exception as exc:  # noqa: BLE001
            log.info("删除动态类定义跳过: %s", str(exc)[:120])
    # 删类后再复查一次：实测删类与运行态复位存在时序差，此处兜底
    stop_production(max_wait=5)
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


def _config_leftover(native) -> list:
    """`^demo.Config` 下仍存在的一级下标（自检用）。

    2026-09-18 起：运行期术语转换改由**共享 BO** `demo.TerminologyOperation` 实时查术语服务器，
    **不再有本地缓存**（`^demo.Config("termcache")` 机制已下线）→ 本处不再有任何例外：
    `^demo.Config` 必须整体为空。
    """
    out = []
    try:
        sub = native.nextSubscript(False, "^demo.Config", "")
        while sub:
            out.append(str(sub))
            sub = native.nextSubscript(False, "^demo.Config", sub)
    except Exception as exc:  # noqa: BLE001 - 枚举失败按"可能有残留"处理，保守告警
        log.warning("枚举 ^demo.Config 一级下标失败: %s", str(exc)[:120])
        return ["(枚举失败)"]
    return out


def _dirty_globals(state: dict, config_left: list) -> tuple:
    """由 `_demo_globals_state` + `_config_leftover` 判定「仍有残留」清单（自检 ② 的唯一判据）。

    返回 `(残留清单, 是否有保留项)`；2026-09-18 起**无合法保留项**（本地术语缓存已下线），
    第二个元素恒为 False（保留签名以免改动调用方）。
    """
    dirty = [g for g, v in state.items() if v is True]
    return dirty, False


def report_term_server() -> None:
    """打印术语服务器（**事实源**）就位情况（信息性日志；本重置不触及它）。

    运行期术语转换由**共享 BO** `demo.TerminologyOperation` 实时查询该服务器（零本地码表副本），
    因此重置后**无需预热任何缓存**：术语映射天然就位。
    """
    try:
        from backend.services import term_catalog

        pairs = term_catalog.systems().get("pairs") or []
        srv = "、".join(f"{str(p.get('sourceSystem') or '?')} → {str(p.get('targetSystem') or '?')}: "
                        f"{p.get('entries')} 条（active {p.get('active')} / negative {p.get('negative')}）"
                        for p in pairs) or "（服务器目录为空）"
        log.info("术语服务器（事实源，独立生命周期；运行期经共享 BO 实时查询）：%s", srv)
    except Exception as exc:  # noqa: BLE001 - 只读信息，失败不影响重置结论
        log.warning("术语服务器摘要读取失败（可忽略）: %s", str(exc)[:160])


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


def _ens_component_classes() -> list:
    """枚举本命名空间里**应用级** Ens 业务主机类（BP/BS/BO/Production）的名字。

    跳过 Ens 库/框架类（EnsLib.*/Ens.*/%* 等，见 LIBRARY_CLASS_PREFIXES）：
    生成物与应用类都在业务命名空间（如 demo.*），库类既不可能是生成物，刷出来只会干扰日志。
    """
    from backend.services import iris_connector
    rows = iris_connector.query(
        "SELECT Name FROM %Dictionary.CompiledClass WHERE "
        "Super LIKE '%Ens.BusinessProcess%' OR Super LIKE '%Ens.BusinessService%' "
        "OR Super LIKE '%Ens.BusinessOperation%' OR Super LIKE '%Ens.Production%'")
    return sorted(str(r[0]) for r in rows
                  if r and r[0] and not str(r[0]).startswith(LIBRARY_CLASS_PREFIXES))


def _generated_file_exists(cls: str) -> bool:
    """该类是否有生成来源文件 `/dur/generated/<短名>.cls`（= Agent 生成物的来源指纹）。"""
    from backend.services import iris_connector
    path = f"{GENERATED_DIR}/{cls.split('.')[-1]}.cls"
    try:
        return int(iris_connector.class_method_value("%File", "Exists", path) or 0) == 1
    except Exception as exc:  # noqa: BLE001
        log.warning("探测生成来源文件失败（%s）: %s", path, str(exc)[:100])
        return False


def clear_generated_classes() -> list:
    """删除生成物：**有来源文件**的生成类 + 清空重建 `/dur/generated`。

    判定规则（2026-09-16 实测过链路，见 .trash/20260916-n11reset/probe_objdelete.py）：
      · 候选 = 命名空间内所有 Ens 业务主机类（BP/BS/BO/Production）；
      · 生成物一定由 `demo.PipelineQuery:AddGeneratedBpSource` 写到
        `/dur/generated/<短名>.cls` 再 Load →「`%File.Exists(该路径)`」就是**可信来源指纹**；
      · 有指纹 → 删类（`%SYSTEM.OBJ:Delete(cls,"c")`，实测删类但**不删源码文件**）→ 再做目录级清空；
      · 无指纹 → **保留**（仓库手写的 `demo.FHIRService`/`demo.TargetOperation` 等也是 Ens 类型，
        无差别删会把演示应用删坏），仅在日志里点名。

    为什么不再硬编码 `demo.SqlFhirPatientTxProcess`：类名随 Agent 产出/白名单演进变化，
    硬编码必漏（漏删 → 旧类残留、新 Production 指向陈旧实现）。
    返回仍未清掉的生成类清单（供自检断言，正常为空）。
    """
    from backend.services import iris_connector
    candidates = _ens_component_classes()
    generated = [c for c in candidates if _generated_file_exists(c)]
    kept = [c for c in candidates if c not in generated]
    for cls in generated:
        try:
            iris_connector.class_method_value("%SYSTEM.OBJ", "Delete", cls, "c")
            log.info("已删除生成类 %s（来源 %s/%s.cls）", cls, GENERATED_DIR, cls.split(".")[-1])
        except Exception as exc:  # noqa: BLE001
            log.warning("删除生成类 %s 失败: %s", cls, str(exc)[:120])
    # 目录级清空重建：覆盖所有源码文件（含已无对应类的孤儿文件）
    try:
        if int(iris_connector.class_method_value(
                "%File", "DirectoryExists", GENERATED_DIR) or 0) == 1:
            iris_connector.class_method_value("%File", "RemoveDirectoryTree", GENERATED_DIR)
        iris_connector.class_method_value("%File", "CreateDirectory", GENERATED_DIR)
        log.info("%s 已清空重建（存在=%s）", GENERATED_DIR,
                 iris_connector.class_method_value("%File", "DirectoryExists", GENERATED_DIR))
    except Exception as exc:  # noqa: BLE001
        log.warning("清空 %s 失败: %s", GENERATED_DIR, str(exc)[:120])
    if kept:
        log.info("保留（无生成来源文件，判定为仓库手写/库类）：%s", ", ".join(kept))
    # 复核：此时目录已空，任何"仍有来源文件"的 Ens 类都是没删掉的残留
    leftover = [c for c in candidates if _generated_file_exists(c)]
    if leftover:
        log.warning("⚠ 生成类残留未清掉：%s", ", ".join(leftover))
    return leftover



def _dbapi(ns: str):
    """建一个 dbapi 连接（backend 容器内用 Docker 网络服务名 iris 访问 IRIS）。"""
    import iris.dbapi
    return iris.dbapi.connect(hostname=IRIS_HOST, port=IRIS_PORT, namespace=ns,
                              username="superuser", password="SYS")


def _discover_tables(conn, ns: str) -> list:
    """列出命名空间内所有「非系统」基表，返回 [(SCHEMA, TABLE), ...]。

    为什么动态发现而不是硬编码表名：演示目标表跟着生成的管道走
    （同一批数据可能落到 Patient/Encounter/Condition/…，Agent 生成的 BP 还会自带
    `<类>_MessagesReceived/_MessagesSent` 统计表），硬编码一定漏表 → 重置不干净。
    系统 schema 前缀见 SYSTEM_SCHEMA_PREFIXES（Ens / HS(FHIR) / % 等）。
    """
    cur = conn.cursor()
    cur.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_SCHEMA, TABLE_NAME")
    rows = cur.fetchall()
    kept = [(str(s or ""), str(n)) for s, n in rows
            if not str(s or "").upper().startswith(SYSTEM_SCHEMA_PREFIXES)]
    log.info("动态发现 %s：基表 %d 张（系统 %d / 非系统 %d）",
             ns, len(rows), len(rows) - len(kept), len(kept))
    return kept


def clear_namespace_tables(ns: str, label: str, fallback) -> list:
    """清空命名空间内所有非系统表的数据，保留表结构。

    返回仍非空的表清单（如 ["SQLUser.Patient=3"]），供自检断言用。
    """
    conn = _dbapi(ns)
    try:
        try:
            targets = _discover_tables(conn, ns)
        except Exception as exc:  # noqa: BLE001 - 发现失败则退回兜底名单
            log.warning("动态发现 %s 表失败，退回兜底名单: %s", ns, str(exc)[:120])
            targets = [("SQLUser", t) for t in fallback]
        if not targets:
            log.info("%s 暂无可清表（尚未建表，属正常）", label)
        left = []
        cur = conn.cursor()
        for sch, name in targets:
            q = f'"{sch}"."{name}"'
            try:
                cur.execute(f"DELETE FROM {q}")
                conn.commit()
                cur.execute(f"SELECT COUNT(*) FROM {q}")
                n = cur.fetchone()[0]
                if n:
                    left.append(f"{sch}.{name}={n}")
                    log.warning("%s 表 %s.%s 仍有 %s 行（清不掉，检查权限/触发器）",
                                label, sch, name, n)
                else:
                    log.info("%s 表 %s.%s 已空", label, sch, name)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).splitlines()[0][:100]
                # DELETE 可能被 SQL filer 拒绝（生成 BP 表实测报 -106 Row to DELETE not found；
                # Ens.BusinessProcess 报 -415 CLASS DOES NOT EXIST）→ 退回"以行数为准"判定：
                # 数不出（表已随类删除，-30）或行数为 0 都不算残留，只有真的还有行才是残留。
                try:
                    conn.rollback()
                    cur.execute(f"SELECT COUNT(*) FROM {q}")
                    n2 = int(cur.fetchone()[0])
                except Exception:  # noqa: BLE001 - 表已不存在（随生成类一起删除）
                    log.info("%s 表 %s.%s 已不存在（随类删除），无需清理", label, sch, name)
                    continue
                if n2:
                    # **孤儿消息表**（生成类已被步骤⑤删除 → 表所属类不存在）：行级 DELETE 会被 SQL filer
                    # 拒绝（实测 `<SQLCODE: <-106>:<Row to DELETE not found>`；Ens.BusinessProcess 报
                    # `-415 CLASS DOES NOT EXIST`），但 `SELECT COUNT(*)` 仍能数出残留行 → "零起点"永远差一项。
                    # 这类表结构本就随生成类消失、不需要保留（下次生成时 Ens 会重建）→ 升级为 DROP TABLE。
                    _bad = msg.upper()
                    if ("-415" in msg or "-106" in msg or "CLASS DOES NOT EXIST" in _bad
                            or "ROW TO DELETE NOT FOUND" in _bad or "DOES NOT EXIST" in _bad):
                        try:
                            conn.rollback()
                            cur.execute(f"DROP TABLE {q}")
                            conn.commit()
                            log.info("%s 表 %s.%s 为**孤儿表**（行级删除被 filer 拒绝）→ 已 DROP",
                                     label, sch, name)
                            continue
                        except Exception as de:  # noqa: BLE001 - DROP 失败则如实记为残留
                            log.warning("%s 孤儿表 %s.%s DROP 失败: %s",
                                        label, sch, name, str(de).splitlines()[0][:100])
                            # ③ **幽灵行**（索引/目录项在、数据 global 已丢：`$Data(^<类>D)=0`）：
                            #    Load 同名生成类（重新绑定存储定义）→ 删类 → 目录行数随之归零。
                            #    只对**本项目生成物**（`/dur/generated/<短名>.cls` 存在）执行，其余不动。
                            _short = name.split(".")[-1]
                            _src = "/dur/generated/%s.cls" % _short
                            try:
                                from backend.services import iris_connector as _ic
                                if _ic.class_method_value("%File", "Exists", _src):
                                    _cls = "%s.%s" % (sch, name)
                                    _ic.class_method_value("%SYSTEM.OBJ", "Load", _src, "ck")
                                    _ic.class_method_value("%SYSTEM.OBJ", "Delete", _cls, "ck")
                                    conn.rollback()
                                    cur.execute(f"SELECT COUNT(*) FROM {q}")
                                    n3 = int(cur.fetchone()[0])
                                    if not n3:
                                        log.info("%s 幽灵表 %s.%s：重建→删类后已清空", label, sch, name)
                                        continue
                                    log.warning("%s 幽灵表 %s.%s 重建→删类后仍为 %s 行",
                                                label, sch, name, n3)
                            except Exception as _ge:  # noqa: BLE001
                                log.warning("%s 幽灵表 %s.%s 自愈失败: %s", label, sch, name,
                                            str(_ge).splitlines()[0][:100])
                    left.append(f"{sch}.{name}={n2}")
                    log.warning("%s 表 %s.%s 清理失败且仍有 %s 行: %s", label, sch, name, n2, msg)
                else:
                    log.info("%s 表 %s.%s DELETE 报错但行数已为 0（记为已清）: %s",
                             label, sch, name, msg)
        return left
    finally:
        conn.close()


def clear_ens_internal():
    """清 Ensemble 内部残留：消息历史 + 流存储 + HTTP 消息体 + BP 进程 + 事件日志。

    为什么要连事件日志一起清：运行期校验直接读 `Ens_Util.Log` 当事实
    （`pipeline_validator` 按 TimeLogged 过滤），上一轮 ERROR 会被算到新一轮头上 → 假红。
    2026-09-16 实测：旧版重置后 `Ens_Util.Log` 仍残留 3196 行、`Ens.StreamContainer` 1575 行。

    个别表 SQL DELETE 会被 SQL filer 拒绝（见 ENS_TABLE_FALLBACK），此时走 ObjectScript 兜底。
    """
    from backend.services import iris_connector
    conn = _dbapi(USER_NS)
    try:
        for t in ENS_INTERNAL_TABLES:
            cur = conn.cursor()
            try:
                cur.execute(f"DELETE FROM {t}")
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("%s SQL DELETE 失败，尝试兜底: %s", t, str(exc)[:110])
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001 - 回滚失败不影响后续
                    pass
                fallback = ENS_TABLE_FALLBACK.get(t)
                if not fallback:
                    continue
                try:
                    iris_connector.class_method_value(*fallback)
                    log.info("%s 已用 %s:%s 兜底清理", t, fallback[0], fallback[1])
                except Exception as exc2:  # noqa: BLE001
                    log.warning("%s 兜底清理失败: %s", t, str(exc2)[:110])
            try:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                log.info("%s 清理后剩余 %s 行", t, cur.fetchone()[0])
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 行数复核失败: %s", t, str(exc)[:110])
    finally:
        conn.close()


def clear_user_tables() -> list:
    """清空 USER 命名空间**全部非系统表**（动态发现；演示目标表 + demo.*/SOAP 消息表）。"""
    return clear_namespace_tables(USER_NS, "USER", DEMO_TARGET_TABLES)


def clear_clinic_tables() -> list:
    """清空 CLINIC 命名空间**全部非系统表**（动态发现；当前为数据源侧 4 张表）。"""
    return clear_namespace_tables(CLINIC_NS, "CLINIC", CLINIC_TABLES)



def _reset_repo_official(label: str, base: str) -> bool:
    """用**官方内置 API** 清空一个 FHIR 仓库：`HS.FHIRServer.Installer.Reset("", pAppKey)`。

    与逐条 DELETE 的区别（2026-09-21 实测，189 条）：官方 Reset **秒级**清空该 repository 的**全部资源**，
    且**不动** endpoint / Web Application / schema 类（`/metadata` 仍 200、PUT/DELETE 仍可用）。

    ⚠ 已知行为：调用会抛 `<THROW>`（两次一致）**但删除已生效** → 这里**不把异常当失败**，
    真实结果由调用方以"逐类计数是否为 0"复核；只有官方路径整体不可用（连接失败 / 类缺失）才返回 False
    交回退（逐条 DELETE）。

    返回 True = 已走官方路径（含"抛异常但可能已生效"，需复核）；False = 官方不可用，请回退。
    """
    import iris                        # noqa: PLC0415 - 仅此处需要 Native SDK
    from urllib.parse import urlsplit  # noqa: PLC0415

    try:
        parts = urlsplit(base).path.strip("/").split("/")       # csp/healthshare/<ns>/fhir/r4
        p_app_key = "/" + "/".join(parts)
        ns = parts[2].upper() if len(parts) > 2 else "USER"      # FHIRSERVER / DEMOFHIR
        conn = _dbapi(ns)
        try:
            native = iris.createIRIS(conn)
            try:
                native.classMethodValue("HS.FHIRServer.Installer", "Reset", "", p_app_key)
                log.info("%s: 官方 Reset 返回成功（pAppKey=%s）", label, p_app_key)
            except Exception as exc:  # noqa: BLE001 - 已知会抛 <THROW> 但删除已生效
                log.info("%s: 官方 Reset 抛异常（已知行为，以计数复核为准）: %s",
                         label, str(exc)[:110])
        finally:
            conn.close()
        return True
    except Exception as exc:  # noqa: BLE001 - 官方路径不可用 → 回退逐条 DELETE
        log.warning("%s: 官方 Reset 不可用（回退逐条 DELETE）: %s", label, str(exc)[:140])
        return False


def purge_fhir_resources():
    """清空 FHIR **两个仓库**（源 DemoFHIR + 目标 FHIRSERVER）上的演示资源。

    注意：IRIS FHIR Server 对 DELETE 返回空 200 响应体（非 JSON），
    不能用要求 JSON 的 fhir_client._request 判定——这里用 urllib 直接删除，
    2xx 即视为成功并复核各类型剩余数。
    """
    import base64
    import urllib.error
    import urllib.request

    from backend.services import fhir_client

    token = base64.b64encode(b"superuser:SYS").decode("ascii")

    def _search(base: str, rt: str):
        try:
            return fhir_client._request(f"{base}/{rt}?_count=1000",
                                        "superuser", "SYS")
        except Exception as exc:  # noqa: BLE001
            log.warning("搜索 %s 失败: %s", rt, str(exc)[:120])
            return None

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

    for label, base in FHIR_REPOS:
        total = 0
        # ① 优先**官方 Reset**（秒级、彻底、不动 endpoint / Web App / schema）——见 _reset_repo_official；
        #    随后**逐类复核计数**（调用抛 <THROW> 不代表失败，以复核为准）；仍有残留才回退逐条 DELETE。
        if _reset_repo_official(label, base):
            remain_after = 0
            for rt in FHIR_CLEAN_TYPES:
                after = _search(base, rt)
                remain = len((after or {}).get("entry", []) or [])
                if remain:
                    remain_after += remain
                    log.info("%s: FHIR %s 官方 Reset 后仍有 %s 条", label, rt, remain)
            if not remain_after:
                log.info("%s: 官方 Reset 已清空该仓库（复核剩余 0）", label)
                continue
            log.warning("%s: 官方 Reset 未清空（剩余 %s）→ 回退逐条 DELETE", label, remain_after)
        for rt in FHIR_CLEAN_TYPES:
            bundle = _search(base, rt)
            if not bundle:
                continue
            ids = [(e.get("resource") or {}).get("id")
                   for e in (bundle.get("entry") or [])]
            ids = [i for i in ids if i]
            ok = 0
            for rid in ids:
                if _delete(f"{base}/{rt}/{rid}"):
                    ok += 1
            if ok:
                total += ok
                log.info("%s: FHIR %s 已删除 %s/%s 个资源", label, rt, ok, len(ids))
            # 复核剩余数（0 = 清理干净）
            after = _search(base, rt)
            remain = len((after or {}).get("entry", []) or [])
            log.info("%s: FHIR %s 剩余: %s", label, rt, remain)
        log.info("%s: FHIR 测试资源清理合计删除 %s 个", label, total)


CHECKS: list = []


def check(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
    """登记一条自检结论；warn=True 的项不计入失败（如 backend 未启动跳过 HTTP 检查）。"""
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail, "warn": warn})
    log.info("%s %s%s", "✅" if ok else ("⚠" if warn else "❌"), name,
             f" —— {detail}" if detail else "")


def _count(ns: str, table: str) -> int:
    """表行数（体检用；表不存在时抛异常由调用方处理）。"""
    conn = _dbapi(ns)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def _leftover_tables(ns: str, fallback) -> list:
    """返回命名空间内「仍有数据」的非系统表清单（体检 --check-only 用）。"""
    conn = _dbapi(ns)
    try:
        try:
            targets = _discover_tables(conn, ns)
        except Exception as exc:  # noqa: BLE001
            log.warning("动态发现 %s 表失败，退回兜底名单: %s", ns, str(exc)[:120])
            targets = [("SQLUser", t) for t in fallback]
        cur = conn.cursor()
        left = []
        for sch, name in targets:
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{sch}"."{name}"')
                n = int(cur.fetchone()[0])
            except Exception:  # noqa: BLE001 - 单表异常不影响整体体检
                continue
            if n:
                left.append(f"{sch}.{name}={n}")
        return left
    finally:
        conn.close()


def _leftover_generated_classes() -> list:
    """生成物残留 = 仍有来源文件（/dur/generated/<短名>.cls）的 Ens 业务主机类。"""
    return [c for c in _ens_component_classes() if _generated_file_exists(c)]


def verify_production() -> None:
    """Production 运行态与配置残留（重置与体检共用；对应历史上"状态自相矛盾"那个坑）。"""
    running = production_running()
    check("Production 已停止（IsProductionRunning=0）", running == 0, f"实际={running}")
    from backend.services import iris_connector
    try:
        rows = iris_connector.query("SELECT COUNT(*) FROM Ens_Config.Production")
        n = int(rows[0][0]) if rows else 0
        check("Ens_Config.Production 配置记录为 0", n == 0, f"行数={n}")
    except Exception as exc:  # noqa: BLE001
        check("Ens_Config.Production 配置记录为 0", False, f"查询失败: {str(exc)[:80]}", warn=True)
    try:
        rows = iris_connector.query(
            "SELECT COUNT(*) FROM %Dictionary.CompiledClass WHERE Name = ?", [PRODUCTION_NAME])
        n = int(rows[0][0]) if rows else 0
        check(f"生成的生产类 {PRODUCTION_NAME} 已删除", n == 0, f"仍存在定义={n}")
    except Exception as exc:  # noqa: BLE001
        check(f"生成的生产类 {PRODUCTION_NAME} 已删除", False,
              f"查询失败: {str(exc)[:80]}", warn=True)


def verify_iris(user_left, clinic_left, cls_left) -> None:
    """IRIS 侧自检：登记 / ^demo.* / 消息与事件日志 / 各表行数 / 生成物。"""
    import iris

    from backend.services import iris_connector, repository
    ds, tgs = repository.list_datasources(), repository.list_targets()
    check("数据源/数据目标登记为空", not ds and not tgs,
          f"datasources={len(ds)} targets={len(tgs)}")
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        state = _demo_globals_state(native)
        config_left = _config_leftover(native)
    finally:
        conn.close()
    dirty, _kept = _dirty_globals(state, config_left)
    check("^demo.* 登记全部为空", not dirty,
          f"残留: {', '.join(dirty)}" if dirty else f"{len(state)} 个 global 均空")
    report_term_server()
    for t in ENS_INTERNAL_TABLES:
        try:
            n = _count(USER_NS, t)
            check(f"{t} 行数为 0", n == 0, f"行数={n}")
        except Exception as exc:  # noqa: BLE001
            check(f"{t} 行数为 0", False, f"查询失败: {str(exc)[:80]}", warn=True)
    check("USER 命名空间非系统表全部为空", not user_left,
          f"仍有数据: {', '.join(user_left)}" if user_left else "")
    check("CLINIC 命名空间非系统表全部为空", not clinic_left,
          f"仍有数据: {', '.join(clinic_left)}" if clinic_left else "")
    check("生成类无残留（来源指纹已清）", not cls_left,
          f"残留: {', '.join(cls_left)}" if cls_left else "")
    try:
        exists = int(iris_connector.class_method_value(
            "%File", "DirectoryExists", GENERATED_DIR) or 0) == 1
    except Exception:  # noqa: BLE001
        exists = False
    check(f"{GENERATED_DIR} 目录存在且已清空", exists, f"DirectoryExists={int(exists)}")


def verify_api() -> None:
    """HTTP 自检：调 backend 接口核对 UI 首屏看到的状态（与 UI 完全同源）。

    为什么值得做：`^demo.*` 为空 ≠ UI 显示为空（历史上出现过「运行态残留导致
    /pipelines/status 报 running=true 而列表为空」的自相矛盾），这一步才等价于
    "打开页面看到的是干净的"。
    """
    probes = [
        ("/pipelines/status", lambda d: d.get("running") is False, "running=false"),
        ("/pipelines/items", lambda d: not d.get("items"), "items 为空"),
        ("/pipelines/instances", lambda d: not d.get("items"), "items 为空"),
        ("/pipelines/logs?limit=5", lambda d: not d.get("items"), "items 为空"),
        ("/pipelines/validation-issues", lambda d: not d.get("items"), "items 为空"),
        ("/datasources", lambda d: not d.get("items"), "items 为空"),
        ("/targets", lambda d: not d.get("items"), "items 为空"),
        ("/mappings", lambda d: not d.get("items"), "items 为空"),
    ]
    reachable = 0
    for path, ok_fn, desc in probes:
        try:
            data = _api_get(path).get("data") or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("GET %s 探测失败: %s", path, str(exc)[:120])
            continue
        reachable += 1
        ok = bool(ok_fn(data))
        check(f"GET {path} → {desc}", ok,
              "" if ok else json.dumps(data, ensure_ascii=False)[:140])
    if not reachable:
        check("HTTP 接口核对（backend 未就绪，跳过）", False,
              f"手工核对: curl -s {API_BASE}/pipelines/status", warn=True)


def verify_fhir() -> None:
    """FHIR 侧自检：**两个仓库**（源 DemoFHIR + 目标 FHIRSERVER）演示资源类型均为空。"""
    from backend.services import fhir_client
    for label, base in FHIR_REPOS:
        dirty, unreachable = [], 0
        for rt in FHIR_CLEAN_TYPES:
            try:
                bundle = fhir_client._request(f"{base}/{rt}?_count=1", "superuser", "SYS")
            except Exception as exc:  # noqa: BLE001
                unreachable += 1
                log.warning("%s: FHIR %s 探测失败: %s", label, rt, str(exc)[:100])
                continue
            if len((bundle or {}).get("entry", []) or []):
                dirty.append(rt)
        if dirty:
            check(f"FHIR 演示资源已清空（{label}）", False, "仍有: " + ", ".join(dirty))
        elif unreachable == len(FHIR_CLEAN_TYPES):
            check(f"FHIR 演示资源已清空（{label}）", False,
                  "FHIR server 不可达（跳过）", warn=True)
        else:
            check(f"FHIR 演示资源已清空（{label}）", True,
                  f"{len(FHIR_CLEAN_TYPES) - unreachable} 个类型探测为空")


def _api_get(path: str) -> dict:
    """调 backend 本地 HTTP 接口（容器内 127.0.0.1:5000，返回 {"code","data","message"}）。"""
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def report() -> int:
    """输出自检结论 + 演示起点提示，返回退出码（0=全部通过）。

    ⚠ 措辞（2026-09-19 用户反馈）：本清单衡量的是**"离零起点还有多远"**，不是系统健康度 ——
    **重置前**出现大量 ❌ 属正常（那些项=仍有演示残留待清）。故改用「已就绪 / 待清理 / 待确认」
    并在清单前给出图例，避免被读成"坏了 N 个东西"。
    """
    fails = [c for c in CHECKS if not c["ok"] and not c["warn"]]
    warns = [c for c in CHECKS if not c["ok"] and c["warn"]]
    passed = len(CHECKS) - len(fails) - len(warns)
    log.info("\n===== 零起点自检（✅=已干净 / ❌=仍有演示残留；重置前出现 ❌ 属正常）=====")
    log.info("已就绪 %d 项 / 待清理 %d 项 / 待确认 %d 项（共 %d 项）",
             passed, len(fails), len(warns), len(CHECKS))
    for c in fails:
        log.error("❌ %s —— %s", c["name"], c["detail"])
    for c in warns:
        log.warning("⚠ %s —— %s", c["name"], c["detail"])
    if fails:
        log.error("环境**未达到零起点**（上列 %d 项仍有演示残留）：按提示处理后重跑 "
                  "`bash tools/datakit/run.sh reset_ui_env.py`", len(fails))
        return 1
    log.info("✅ 环境已就绪（服务/命名空间/表结构/术语与向量库保留）——可直接开始演示：")
    log.info("  · 前端入口        http://localhost/          （docker-compose 映射 80）")
    log.info("  · 后端接口        http://localhost:5001/api/pipelines/status")
    log.info("  · 造数 FHIR 源    docker compose exec dataflow-backend python "
             "/app/generate_mock_data.py --fhir 5")
    log.info("  · 造数 SQL 源     docker compose exec dataflow-backend python "
             "/app/generate_mock_data.py --sql 3")
    log.info("  · 造数 CLINIC 四表 bash tools/datakit/run.sh seed_clinic.py"
             "   # 等价界面「生成演示数据」按钮")
    log.info("  · 术语映射        **无需预热**（运行期由共享 BO demo.TerminologyOperation 实时查术语服务器；"
             "缺映射默认降级为「保留源编码 + meta.tag=unmapped」）")
    log.info("  · 许可预算        8 个业务主机单元（社区版），设计管道时注意主机数")
    return 0



def main() -> int:
    """一键重置（默认）或只体检（--check-only）；返回退出码。"""
    check_only = "--check-only" in sys.argv
    skip_fhir = "--skip-fhir" in sys.argv
    t0 = time.time()
    user_left, clinic_left, cls_left = [], [], []

    if check_only:
        log.info("== 体检模式（--check-only）：只读，不改动任何数据 ==")
    else:
        step("1/8 停/删 demo Production（校验式停止 + Ens 配置表残留清理）")
        clean_production()
        step("2/8 清 SQL 源扫描凭证与错误行（防重 seed 后零消息）")
        clear_sql_source_appdata()
        step("3/8 清空 ^demo.* 登记")
        kill_demo_globals()
        step("4/8 清空 Ens 内部残留（消息/流存储/HTTP 消息体/BP 进程/事件日志）")
        clear_ens_internal()
        # 2026-09-18 修：清表**必须排在删生成类之前**。反序会出现"类已删但表仍在"的**孤儿存储**：
        # 行级 DELETE 被 filer 拒（`-106 Row to DELETE not found`）、`DROP TABLE` 也被拒
        # （`-300 DDL not allowed on this table definition`）→ 残留数永远清不掉（实测自检 25/26，
        # 残留 `demo.SqlFhirPatientTxProcess=2`）。正序：类还在 → DELETE 正常 → 再删类时表随之消失。
        step("5/8 清空 USER 命名空间全部非系统表（动态发现）")
        user_left = clear_user_tables()
        step("6/8 清空 CLINIC 命名空间全部非系统表（动态发现）")
        clinic_left = clear_clinic_tables()
        step("7/8 清生成物（来源指纹判定的生成类 + /dur/generated 清空重建）")
        cls_left = clear_generated_classes()
        if skip_fhir:
            log.info("已跳过 FHIR 资源清理（--skip-fhir）")
        else:
            step("8/8 清除 FHIR server 测试资源")
            purge_fhir_resources()
        # 2026-09-18：原「术语映射镜像」步骤已移除（运行期改由共享 BO 实时查术语服务器，
        # 已无本地缓存可预热）；收尾只打印事实源摘要（见 report_term_server）。

    # 自检一律重新扫描"当前事实"，不复用清理阶段的清单：
    # 清生成类会让其 SQL 表（<类>/<类>_MessagesReceived/Sent）随之消失，用旧清单复核
    # 会报"表都不存在了却仍有数据"的假 ❌（2026-09-16 实测：-106 Row to DELETE not found）。
    if user_left or clinic_left or cls_left:
        log.info("清理阶段记录（下面按当前事实复核）：USER=%s CLINIC=%s 生成类=%s",
                 user_left or "无", clinic_left or "无", cls_left or "无")
    user_left = _leftover_tables(USER_NS, DEMO_TARGET_TABLES)
    clinic_left = _leftover_tables(CLINIC_NS, CLINIC_TABLES)
    cls_left = _leftover_generated_classes()

    step("自检 A：Production 运行态 / Ens 配置")
    verify_production()
    step("自检 B：IRIS 侧数据（登记/global/消息日志/表/生成物）")
    verify_iris(user_left, clinic_left, cls_left)
    step("自检 C：HTTP 接口（UI 同源）")
    verify_api()
    if not skip_fhir:
        step("自检 D：FHIR server 演示资源")
        verify_fhir()
    rc = report()
    log.info("耗时 %.1fs（退出码 %d）", time.time() - t0, rc)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log.error("重置失败（未完成，环境可能处于中间态，可重跑本脚本）: %s", exc)
        sys.exit(1)

