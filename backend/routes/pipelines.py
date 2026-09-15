"""数据管道 API：生成、触发、监控。"""

import json
import logging
import random
import time
from datetime import datetime, timezone

from flask import Blueprint, request

from backend.config import Config, FHIRConfig
from backend.schemas.models import PipelineGenerateRequest
from backend.services import (iris_connector, llm_client, pipeline_instances,
                              pipeline_validator, repository,
                              transformation_validator, type_registry,
                              validate_agent, wsdl_importer)
from backend.utils import error, success

logger = logging.getLogger(__name__)

pipelines_bp = Blueprint("pipelines", __name__, url_prefix="/api/pipelines")

# 模板库组件枚举（从类型注册表动态生成，供 Agent B 数据管道设计使用）
AVAILABLE_COMPONENTS = type_registry.get_available_components()



def _save_pipeline_topology(pipeline):
    """保存 Agent B 输出的管道拓扑到 ^demo.Config。"""
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        native.set(json.dumps(pipeline, ensure_ascii=False), "^demo.Config", "pipeline", "topology")
    finally:
        iris_connector.reset_connections()


def _save_fhir_runtime_config(topology: dict | None, target_config: dict | None = None) -> None:
    """把 FHIR 目标 HTTP Operation 的投递名 / 基础路径 / 凭据写入 ^demo.Config。

    TransformProcess 的 FHIR 分支据此构造 REST PUT（GenericOperation 消息契约）。非决策，仅参数化。
    """
    if not topology:
        return
    comp = next((c for c in (topology.get("components") or [])
                 if c.get("className") == "EnsLib.HTTP.GenericOperation"), None)
    if not comp:
        return
    base = (target_config or {}).get("base_url") \
        or (target_config or {}).get("endpoint") or ""
    path = "/csp/healthshare/fhirserver/fhir/r4"
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base).path.rstrip("/")
        if parsed:
            path = parsed
    except Exception:  # noqa: BLE001
        pass
    import iris
    from backend.services import fhir_target_model as _ftm
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        native.set(comp.get("name") or "FHIR_HTTP_Operation", "^demo.Config", "fhir", "operation")
        native.set(path, "^demo.Config", "fhir", "base_path")
        native.set((target_config or {}).get("username") or "superuser",
                   "^demo.Config", "fhir", "username")
        native.set((target_config or {}).get("password") or "SYS",
                   "^demo.Config", "fhir", "password")
        # 已建模资源组装 schema（path/system/type），供 TransformProcess 通用打包器读取
        for _rt in _ftm.DEFAULT_RESOURCE_TYPES:
            _schema = _ftm.model_schema(_rt)
            if _schema:
                native.set(json.dumps(_schema, ensure_ascii=False),
                           "^demo.Config", "fhir", "schema", _rt)
    finally:
        iris_connector.reset_connections()


def _get_table_columns(table: str) -> list[str]:
    """查询目标表列名（information_schema，按 ordinal_position 排序）。"""
    rows = iris_connector.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='SQLUser' AND table_name=? ORDER BY ordinal_position",
        [table])
    return [r[0] for r in rows]


def _fhir_http_host_port(base_url: str | None) -> tuple[str, str]:
    """从 FHIR 目标 base_url（如 http://iris:52773/csp/.../r4）解析 HTTPServer/HTTPPort。

    只做参数归一（非决策）；URL 路径前缀与 method/body 由发送方在消息里携带。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "", "52773"
    head = base.split("://", 1)[1] if "://" in base else base
    host_port = head.split("/", 1)[0]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        return host or "", port or "52773"
    return host_port, "80"


def _resolve_sql_source_tables(mappings: list[dict], source_id: str | None) -> tuple[list[dict], dict]:
    """把映射的 source 归一到**真实表名**，并构造源资产元数据。

    登记形态兼容：mapping.source 可能是**资产 ID**（如 DS63900_T004）或**表名**（Patient）；
    layout 的 source_table 必须能直接用于 SQL 查询，故统一解析为真实表名；
    同时返回 {表名: {columns, key_hint}} 供 sql2fhir 布局推导（key_hint 取接口分析语义）。
    """
    ds = repository.get_datasource(source_id) if source_id else None
    assets: list[dict] = []
    if source_id:
        try:
            assets = repository.list_source_assets(source_id) or []
        except Exception:  # noqa: BLE001
            assets = []
        if not assets:
            try:
                assets = repository.list_assets(source_id) or []
            except Exception:  # noqa: BLE001
                assets = []

    def _cols(x) -> list[str]:
        return [c["name"] if isinstance(c, dict) else str(c) for c in (x or [])]

    def _hint(a: dict):
        try:
            sem = a.get("ai_semantics") or {}
            return sem.get("key_hint") or a.get("key_hint") or None
        except Exception:  # noqa: BLE001
            return None

    idx: dict[str, dict] = {}
    for a in assets:
        struct = a.get("structure") or {}
        tbl = str(struct.get("table") or a.get("name") or "")
        entry = {"table": tbl,
                 "columns": _cols(a.get("fields") or struct.get("columns")),
                 "key_hint": _hint(a)}
        for k in (a.get("id"), a.get("name"), tbl):
            if k:
                idx[str(k)] = entry
    for tb in (ds or {}).get("tables") or []:
        tbl = str(tb.get("table") or "")
        if not tbl:
            continue
        entry = {"table": tbl, "columns": _cols(tb.get("columns")), "key_hint": None}
        idx.setdefault(tbl, entry)
        sch = tb.get("schema")
        if sch:
            idx.setdefault(f"{sch}.{tbl}", entry)

    out_maps: list[dict] = []
    meta: dict[str, dict] = {}
    for m in mappings or []:
        raw = str(m.get("source") or "")
        if not raw:
            continue
        entry = idx.get(raw) or idx.get(raw.split(".")[-1])
        tbl = str((entry or {}).get("table") or raw.split(".")[-1])
        m2 = dict(m)
        m2["source"] = tbl          # 归一为真实表名（供 SQL 查询与布局）
        out_maps.append(m2)
        meta[tbl] = {"columns": (entry or {}).get("columns") or [],
                     "key_hint": (entry or {}).get("key_hint")}
    return out_maps, meta


def _sql_source_meta(mappings: list[dict], source_id: str | None) -> dict:
    """兼容入口：仅返回源资产元数据（{表: {columns, key_hint}}）。"""
    return _resolve_sql_source_tables(mappings, source_id)[1]


def _save_sql2fhir_layout(layout: dict) -> None:
    """把 sql2fhir 布局 JSON 写入 ^demo.Config("sql2fhir","layout")（Agent 生成的 BP 运行时读取）。"""
    import iris
    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        native.set(json.dumps(layout, ensure_ascii=False), "^demo.Config", "sql2fhir", "layout")
    finally:
        iris_connector.reset_connections()


def _ensure_sql2fhir_bp(p_result, mappings, source_type: str, target_type: str,
                        available, source_runtime, target_runtime) -> tuple[bool, str]:
    """确保 Agent 生成的 BP 编译可用：优先复用本次 p_result.generated_bp（静态+编译通过即用），
    缺失/失败则走 generate_and_compile_bp（编译错误回 feed ≤3 轮）。返回 (ok, message)。"""
    from backend.services import generated_bp as _gbp
    bp = p_result.get("generated_bp") if isinstance(p_result, dict) else None
    if bp:
        errs = _gbp.static_check_generated_bp(bp)
        if not errs:
            comp = _gbp.compile_generated_bp(bp)
            if comp.get("ok"):
                logger.info("Agent 首次输出 BP 即通过编译")
                _gbp.save_last_good(_gbp.get_generated_source())
                return True, comp.get("message", "")
            logger.warning("Agent 首次 BP 编译失败，进入反馈轮: %s", comp.get("message"))
        else:
            logger.warning("Agent 首次 BP 静态准入失败: %s", "；".join(errs))
    res = _gbp.generate_and_compile_bp(
        mappings=mappings, source_type=source_type, target_type=target_type,
        available_components=available,
        source_runtime=source_runtime, target_runtime=target_runtime)
    if res.get("ok"):
        _gbp.save_last_good(_gbp.get_generated_source())
        return True, str(res.get("message") or "")
    # 兜底 1（显式告警，不静默）：回滚到「上次通过验证的 Agent BP」（仍是 Agent 产物）
    rolled = _gbp.restore_last_good()
    if rolled.startswith("restored"):
        logger.warning("本次 Agent BP 未通过（%s）→ 已回滚 last_good BP（agent_bp_rolled_back）",
                       str(res.get("message"))[:200])
        return True, f"agent_bp_rolled_back:{rolled}"
    # 兜底 2：复用已存在且可编译的同类 BP（类库里的上一版）
    try:
        existed = iris_connector.class_method_value(
            "%Dictionary.ClassDefinition", "%ExistsId", "demo.SqlFhirPatientTxProcess")
        if int(existed or 0):
            st = str(iris_connector.class_method_value(
                "%SYSTEM.OBJ", "Compile", "demo.SqlFhirPatientTxProcess", "ck") or "")
            if st.startswith("1"):
                logger.warning("本次 Agent BP 未通过（%s）→ 复用类库里已编译的 BP（agent_bp_reused）",
                               str(res.get("message"))[:200])
                return True, "agent_bp_reused: " + str(res.get("message") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("BP 回退复用检查失败: %s", exc)
    return False, str(res.get("message") or "")


def _source_tables_from_mappings(mappings: list[dict]) -> list[str]:
    """从映射的 source 推导本组涉及的源表/资源名（去重保序）。

    业务背景：多管道共用同一数据源时（如 CLINIC 的 Patient/Encounter/Diagnosis/
    MedicationOrder 四表），若把数据源的**全部表**作为设计输入交给 Agent B，
    Agent 会为每张表生成一个源 BS——多余源会把非本组目标的数据也投递到本组目标
    （SQL→SOAP 组曾生成 4 个 SQLService），属于语义错误。

    这里把设计上下文收敛到"本组映射实际涉及的表"；组件构成与顺序仍由 Agent B
    决策（平台只做上下文精准化，不替 AI 决策）。
    """
    names: list[str] = []
    for m in mappings or []:
        src = str((m or {}).get("source") or "").strip()
        if not src or src.startswith("constant:") or src.startswith("expr:"):
            continue
        name = src.split(".")[0].split("[")[0].split("(")[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def _scope_source_context(runtime: dict | None, models: list[dict] | None,
                          tables: list[str]) -> tuple[dict | None, list[dict] | None]:
    """把源运行契约/源模型收敛到本组涉及的表（Agent B 上下文精准化）。"""
    if not tables:
        return runtime, models
    rt = dict(runtime) if isinstance(runtime, dict) else runtime
    if isinstance(rt, dict) and rt.get("assets"):
        scoped = [a for a in rt["assets"] if str((a or {}).get("name") or "") in tables]
        if scoped and len(scoped) < len(rt["assets"]):
            logger.info("Agent B 源上下文收敛: assets %d -> %d（本组涉及表 %s）",
                        len(rt["assets"]), len(scoped), tables)
            rt["assets"] = scoped
    if models:
        scoped_models = [m for m in models
                         if str((m or {}).get("table") or (m or {}).get("name") or "") in tables]
        if scoped_models:
            models = scoped_models
    return rt, models


def _c1_assets(source_id: str | None) -> list[dict]:
    """C1 转换验证的源资产输入（name + fields），供 LLM 补齐必填映射时选 source 列。"""
    assets: list[dict] = []
    try:
        assets = repository.list_source_assets(source_id) if source_id else []
    except Exception:  # noqa: BLE001
        assets = []
    if not assets:
        try:
            assets = repository.list_assets(source_id) or []
        except Exception:  # noqa: BLE001
            assets = []
    out = []
    for a in assets:
        cols = a.get("fields") or (a.get("structure") or {}).get("columns") or []
        out.append({"name": a.get("name") or a.get("id") or "",
                    "fields": [c.get("name") if isinstance(c, dict) else str(c) for c in cols]})
    return out


def _c1_target_models(mappings: list[dict]) -> list[dict]:
    """C1 的 FHIR 目标模型输入（列结构），避免 L1 把它当 SQL 表列误剔除映射。"""
    from backend.services import fhir_target_model as _ftm
    res: list[str] = []
    for m in mappings or []:
        rt = str(m.get("target_table") or "").strip().lower()
        for k in _ftm.US_CORE_RESOURCE_MODELS:
            if k.lower() == rt and k not in res:
                res.append(k)
    return _ftm.build_entities(res) if res else []


def build_pipeline_topology(mappings: list[dict], source_type: str = "FHIR",
                            target_type: str = "DB",
                            suggested_types: list[str] | None = None,
                            source_config: dict | None = None,
                            target_config: dict | None = None,
                            ai_components: list[dict] | None = None,
                            bp_name: str = "TransformProcess") -> dict:
    """按源/目标类型 + 映射构建完整管道拓扑 JSON（组件带 className + settings）。

    - ai_components（Agent B/LLM 输出）非空时：**由 AI 决定组件构成与顺序**，
      Python 注册表只负责补全 className/settings（AI 决策 + 模板参数化），
      仅在 AI 遗漏必需组件时补齐保底，并把补齐清单写入 ai_supplemented 键。
    - 源/目标/通用组件模板来自类型注册表（type_registry）；
    - SQLOperation 的 Query/InputParameters 在此由 Python 依据目标表列构建
      （信息来自 information_schema），不再由 IRIS 侧查询；
    - SOAP 目标为「WSDL 导入型」：target_config 提供 wsdl/service/packages，
      先调 %SOAP.WSDL.Reader 生成 BO，再用 BO 类作为 Operation 组件；
    - SQL 源：source_config 提供 dsn/query/key_field 填充 SQLService settings；
    - PipelineGenerator 按通用规则渲染（name/className/settings）；
    - suggested_types: 仅影响排序（保留兼容旧规则路径）。
    - bp_name：转换 BP 的**业务主机名**（Ens 组件身份 = Item 名，class 可复用）。
      多管道并存时每条管道给自己的 BP 取名（如 TransformProcess__sql2soap），
      源 BS 的 TargetConfigNames 指向本管道自己的 BP —— 管道之间零耦合。
    """
    if ai_components:
        return _build_from_ai_components(
            mappings, source_type=source_type, target_type=target_type,
            ai_components=ai_components,
            source_config=source_config, target_config=target_config,
            bp_name=bp_name)

    def _s(target: str, name: str, value: str) -> dict:
        """构造一个 Setting 项：{"target": "Adapter|Host", "name": ..., "value": ...}。"""
        return {"target": target, "name": name, "value": value}

    def _from_template(template: dict, name: str, comment: str = "") -> dict:
        """按注册表模板构建组件（adapter_settings → Adapter，host_settings/settings → Host）。"""
        settings = []
        for k, v in template.get("adapter_settings", {}).items():
            settings.append(_s("Adapter", k, v))
        for k, v in template.get("host_settings", {}).items():
            settings.append(_s("Host", k, v))
        for k, v in template.get("settings", {}).items():
            settings.append(_s("Host", k, v))
        return {
            "type": template["type"],
            "name": name,
            "className": template["className"],
            "comment": comment or template.get("comment", ""),
            "settings": settings,
        }

    components = []

    # 1. 源组件（按 source_type）
    for c in type_registry.get_source_components(source_type):
        if c["type"] == "SQLService":
            # SQL 源：用 source_config 填充 DSN（Adapter）/ Query、KeyFieldName（Host）
            cfg = source_config or {}
            query_val = cfg.get("query") or "SELECT * FROM SQLUser.PatientSource"
            key_field_val = cfg.get("key_field") or "ID"
            dsn_val = cfg.get("dsn") or "localTarget"
            components.append({
                "type": "SQLService", "name": "SQLService",
                "className": c["className"], "comment": c["comment"],
                "settings": [
                    _s("Adapter", "DSN", dsn_val),
                    _s("Adapter", "Query", query_val),
                    _s("Adapter", "KeyFieldName", key_field_val),
                    _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                    _s("Host", "TargetConfigNames", bp_name),
                ],
            })
        else:
            components.append(_from_template(c, c["type"]))

    # 2. 转换组件（转换 BP：本管道的实例名由调用方给定，class 可跨管道复用）
    for c in type_registry.get_common_components():
        if c["type"] == "TransformProcess":
            components.append(_from_template(c, bp_name))

    # 3. 目标组件（按 target_type）
    for c in type_registry.get_target_components(target_type):
        if c["type"] == "SQLOperation":
            # DB 目标：每个目标表一个 SQLOp，UPSERT 幂等
            for m in mappings:
                table = m.get("target_table", "")
                if not table:
                    continue
                cols = _get_table_columns(table)
                if not cols:
                    logger.warning("目标表 %s 列结构为空，跳过 SQLOp", table)
                    continue
                col_sql = ", ".join(cols)
                q_marks = ", ".join("?" for _ in cols)
                query = f"INSERT OR UPDATE INTO {table} ({col_sql}) VALUES ({q_marks})"
                params = ",".join(f"*{col}" for col in cols)
                components.append({
                    "type": "SQLOperation", "name": f"SQLOp_{table}",
                    "className": c["className"], "comment": f"写入目标表 {table}",
                    "settings": [
                        _s("Adapter", "DSN", "localTarget"),
                        _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                        _s("Host", "Query", query),
                        _s("Host", "InputParameters", params),
                    ],
                })
        elif c.get("import_based"):
            # SOAP 目标（WSDL 导入型）：先导入 WSDL 生成 BO，再用 BO 类作为 Operation 组件
            wsdl = (target_config or {}).get("wsdl", "")
            service = (target_config or {}).get("service", "default")
            bo_class = (target_config or {}).get("bo_class", "")

            imp = {}
            if wsdl:
                imp = wsdl_importer.import_soap_operation(
                    wsdl, service, (target_config or {}).get("packages"))

            if imp.get("ok") and imp.get("boClass"):
                bo_class = imp["boClass"]
            elif not bo_class:
                bo_class = wsdl_importer.get_bo_class(service)

            if bo_class:
                wsdl_importer._save_bo_mapping(service, bo_class)
                components.append({
                    "type": "SOAPOperation", "name": f"SOAPOp_{service}",
                    "className": bo_class,
                    "comment": f"SOAP 调用 {service}（WSDL 导入 BO）",
                    "settings": [
                        # Adapter WebServiceURL 覆盖 WSDL 内 soap:address，指向远端 SOAP 服务
                        # （demo 默认 Python mock 第三方系统，见 services/mock_soap.py / Config.MOCK_SOAP_URL）
                        _s("Adapter", "WebServiceURL", target_config.get("endpoint") or Config.MOCK_SOAP_URL),
                    ],
                })
            else:
                logger.warning("SOAP 目标 WSDL 导入失败或未找到 bo_class: %s", imp.get("message"))
        elif c["type"] == "HTTPOperation":
            # FHIR 目标 = 通用 HTTP Operation：Adapter 配远端 server/port（参数归一，非决策）
            base = (target_config or {}).get("base_url") \
                or (target_config or {}).get("endpoint") or ""
            host, port = _fhir_http_host_port(base)
            components.append({
                "type": "HTTPOperation", "name": "HTTPOperation",
                "className": c["className"], "comment": c["comment"],
                "settings": [
                    _s("Adapter", "HTTPServer", host),
                    _s("Adapter", "HTTPPort", port),
                ],
            })
        else:
            components.append(_from_template(c, c["type"]))

    # 4. JavaGateway（通用组件）
    for c in type_registry.get_common_components():
        if c["type"] == "JavaGateway":
            components.append(_from_template(c, c["className"]))

    # 5. 按 Agent B 建议的 type 顺序排序（未出现的排在后；sort 稳定保持原序）
    if suggested_types:
        fallback = len(suggested_types)
        components.sort(
            key=lambda c: (suggested_types.index(c["type"])
                           if c["type"] in suggested_types else fallback))

    return {"production": "demo.DataflowProduction", "components": components}


def _build_from_ai_components(mappings: list[dict], *, source_type: str, target_type: str,
                              ai_components: list[dict],
                              source_config: dict | None = None,
                              target_config: dict | None = None,
                              bp_name: str = "TransformProcess") -> dict:
    """Agent B（LLM）驱动拓扑构建：AI 决定组件构成与顺序，注册表只补全参数。

    - 每个 AI 组件的 type 必须来自可用枚举（源/目标/通用模板全集），按 AI 的
      name/table 生成；className 与连接参数（DSN/Query/表 UPSERT 列等）由注册表注入；
    - AI 遗漏确属必需的组件（源服务全集/TransformProcess/目标操作/JavaGateway）时
      按模板补齐保底，补齐清单写入 topology["ai_supplemented"]（Python 不再自行
      决定整体构成，仅做完整性校验与参数化）。
    - bp_name：转换 BP 的主机名（Ens Item 名）；源 BS 的 TargetConfigNames 指向它。
      Agent B 若自带该组件名，仍统一改写为 bp_name —— 保证「一管道一 BP」的隔离。
    """

    def _s(target: str, name: str, value: str) -> dict:
        return {"target": target, "name": name, "value": value}

    def _from_template(template: dict, name: str, comment: str = "") -> dict:
        settings = []
        for k, v in template.get("adapter_settings", {}).items():
            settings.append(_s("Adapter", k, v))
        for k, v in template.get("host_settings", {}).items():
            settings.append(_s("Host", k, v))
        for k, v in template.get("settings", {}).items():
            settings.append(_s("Host", k, v))
        return {"type": template["type"], "name": name,
                "className": template["className"],
                "comment": comment or template.get("comment", ""),
                "settings": settings}

    templates: dict[str, dict] = {}
    for c in type_registry.get_source_components(source_type):
        templates[c["type"]] = c
    for c in type_registry.get_target_components(target_type):
        templates[c["type"]] = c
    for c in type_registry.get_common_components():
        templates[c["type"]] = c

    def build_component(tpl: dict, name: str | None = None,
                        table: str | None = None) -> dict | None:
        """按模板 + AI 命名/表选择构建单个组件；无法构建（如表列空）返回 None。"""
        ttype = tpl["type"]
        cfg_src = source_config or {}
        cfg_tgt = target_config or {}
        if ttype == "SQLService":
            return {
                "type": "SQLService", "name": name or "SQLService",
                "className": tpl["className"], "comment": tpl["comment"],
                "settings": [
                    _s("Adapter", "DSN", cfg_src.get("dsn") or "localTarget"),
                    _s("Adapter", "Query",
                       cfg_src.get("query") or "SELECT * FROM SQLUser.PatientSource"),
                    _s("Adapter", "KeyFieldName", cfg_src.get("key_field") or "ID"),
                    _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                    _s("Host", "TargetConfigNames", bp_name),
                ],
            }
        if ttype == "TransformProcess":
            # 转换 BP：统一用本管道给定的主机名（AI 给的名字不采用，保证一管道一实例）
            return _from_template(tpl, bp_name)
        if ttype == "JavaGateway":
            return {
                "type": "JavaGateway", "name": name or tpl["className"],
                "className": tpl["className"], "comment": tpl["comment"],
                "settings": [_s("Host", k, v) for k, v in tpl.get("settings", {}).items()],
            }
        if ttype == "SQLOperation":
            tbl = table or ""
            if not tbl and name:
                tbl = name[len("SQLOp_"):] if name.startswith("SQLOp_") else name
            if not tbl:
                for _m in mappings:
                    if _m.get("target_table"):
                        tbl = _m["target_table"]
                        break
            cols = _get_table_columns(tbl) if tbl else []
            if not cols:
                logger.warning("目标表 %s 列结构为空，跳过 SQLOp（Agent B 表选择不可用）", tbl)
                return None
            col_sql = ", ".join(cols)
            q_marks = ", ".join("?" for _ in cols)
            return {
                "type": "SQLOperation", "name": name or f"SQLOp_{tbl}",
                "className": tpl["className"], "comment": f"写入目标表 {tbl}",
                "settings": [
                    _s("Adapter", "DSN", "localTarget"),
                    _s("Adapter", "JGService", "EnsLib.JavaGateway.Service"),
                    _s("Host", "Query",
                       f"INSERT OR UPDATE INTO {tbl} ({col_sql}) VALUES ({q_marks})"),
                    _s("Host", "InputParameters", ",".join(f"*{c}" for c in cols)),
                ],
            }
        if ttype == "SOAPOperation":
            service = cfg_tgt.get("service") or "default"
            bo_class = cfg_tgt.get("bo_class", "")
            imp = {}
            if cfg_tgt.get("wsdl"):
                imp = wsdl_importer.import_soap_operation(
                    cfg_tgt["wsdl"], service, cfg_tgt.get("packages"))
            if imp.get("ok") and imp.get("boClass"):
                bo_class = imp["boClass"]
            elif not bo_class:
                bo_class = wsdl_importer.get_bo_class(service)
            if not bo_class:
                logger.warning("SOAP 目标 bo_class 未找到（service=%s），跳过 SOAPOp", service)
                return None
            wsdl_importer._save_bo_mapping(service, bo_class)
            return {
                "type": "SOAPOperation", "name": name or f"SOAPOp_{service}",
                "className": bo_class,
                "comment": f"SOAP 调用 {service}（WSDL 导入 BO）",
                "settings": [_s("Adapter", "WebServiceURL",
                                cfg_tgt.get("endpoint") or Config.MOCK_SOAP_URL)],
            }
        if ttype == "HTTPOperation":
            base = cfg_tgt.get("base_url") or cfg_tgt.get("endpoint") or ""
            host, port = _fhir_http_host_port(base)
            return {
                "type": "HTTPOperation", "name": name or "HTTPOperation",
                "className": tpl["className"], "comment": tpl["comment"],
                "settings": [
                    _s("Adapter", "HTTPServer", host),
                    _s("Adapter", "HTTPPort", port),
                ],
            }
        return _from_template(tpl, name or ttype)

    def _name_table(n: str) -> str:
        return n[len("SQLOp_"):] if n.startswith("SQLOp_") else n

    components: list[dict] = []
    supplemented: list[str] = []
    for ai in ai_components:
        ttype = ai.get("type", "")
        tpl = templates.get(ttype)
        if not tpl:
            logger.warning("Agent B 选择了未注册组件 type=%s，忽略", ttype)
            continue
        comp = build_component(tpl, name=ai.get("name"), table=ai.get("table"))
        if comp:
            components.append(comp)

    def add_if_missing(ttype: str, *, name: str | None = None, table: str | None = None):
        """AI 未提供该必需组件时按注册表模板补齐保底（不覆盖 AI 已有决策）。"""
        if any(c["type"] == ttype for c in components):
            return
        comp = build_component(templates[ttype], name=name, table=table)
        if comp:
            components.append(comp)
            supplemented.append(comp["type"])

    # 保底补齐（仅限确属必需而 AI 遗漏者；AI 已提供则尊重 AI 的命名/表）
    for c in type_registry.get_source_components(source_type):
        if not any(x["type"] == c["type"] for x in components):
            add_if_missing(c["type"])
    add_if_missing("TransformProcess", name=bp_name)
    if target_type == "DB":
        have_tables = {_name_table(c.get("name", "")).lower()
                       for c in components if c["type"] == "SQLOperation"}
        for m in mappings:
            tbl = m.get("target_table", "")
            if not tbl or tbl.lower() in have_tables:
                continue
            add_if_missing("SQLOperation", name=f"SQLOp_{tbl}", table=tbl)
            have_tables.add(tbl.lower())
        add_if_missing("SQLOperation")
    else:
        # 非 DB 目标（SOAP/FHIR 等）：按当前 target_type 的注册表目标组件模板补齐保底
        for tpl in type_registry.get_target_components(target_type):
            add_if_missing(tpl["type"])
    add_if_missing("JavaGateway")

    topology = {"production": "demo.DataflowProduction", "components": components}
    if supplemented:
        topology["ai_supplemented"] = supplemented
        logger.info("Agent B 已生成拓扑：AI 决策 %d 个组件，保底补齐缺失 %s",
                    len(components) - len(supplemented), supplemented)
    return topology


def _dedupe_components(components: list[dict]) -> list[dict]:
    """组件按 name 去重合并；同名不同 className（真冲突）自动加序号后缀。"""
    seen: dict[str, dict] = {}
    out: list[dict] = []
    for c in components:
        name = c.get("name", "")
        prev = seen.get(name)
        if prev is not None:
            if prev.get("className") == c.get("className"):
                continue  # 完全同定义，去重
            n = 2
            while seen.get(f"{name}_{n}"):
                n += 1
            c = {**c, "name": f"{name}_{n}"}
        seen[c.get("name")] = c
        out.append(c)
    return out


def _common_component(template: dict) -> dict:
    """按注册表模板构造共享组件（settings → Host）。"""
    settings = []
    for k, v in (template.get("settings") or {}).items():
        settings.append({"target": "Host", "name": k, "value": v})
    return {
        "type": template["type"],
        "name": template["className"] if template["type"] == "JavaGateway" else template["type"],
        "className": template["className"],
        "comment": template.get("comment", ""),
        "settings": settings,
    }


def build_multi_pipeline_topology(pipelines: list[dict]) -> dict:
    """按「多管道组」构建合并拓扑（单 Production 内每管道一套 BS/BP/BO + 共享基础设施）。

    **一条数据管道一个转换 BP**（Ens 组件身份 = Ens.Config.Item 的 Name，class 可复用）：
    - 每组给自己的 BP 取名 `TransformProcess__{管道类别}`（同类别多组时加序号），
      源 BS 的 TargetConfigNames 指向**本组自己的 BP** → 管道之间零耦合：
      停一条管道不会带动另一条，也不会出现「共享 BP 被豁免启停 → 许可不放」；
    - 自带聚合 BP 的 Skill 组（sql2fhir-patient-tx 的 SqlFhirPatientTxProcess）不生成它；
    - 真正跨管道共享的只剩基础设施 **JavaGateway**（JDBC 网关，多管道共用 1 个实例）。

    源 BS 命名加固（消除「名字漂移」）：源 BS 同名跨多组冲突时，**按管道类别统一改名**为
    `{原名}__{类别}`（如 SQLService_Patient__sql2soap）——与组顺序无关，重复生成名字恒定；
    未冲突的组保持原名。
    """
    def _bp_name_for(cat: str, taken: set[str]) -> str:
        """本组转换 BP 的主机名（`TransformProcess__{类别}`，重复类别加序号，结果确定）。"""
        base = f"TransformProcess__{pipeline_instances.slug(cat) or 'pipeline'}"
        name, k = base, 2
        while name in taken:
            name = f"{base}_{k}"
            k += 1
        taken.add(name)
        return name

    def _group_components(g: dict, bp_name: str) -> tuple[list[dict], bool, list[str]]:
        """生成单组组件（剔除基础设施 JavaGateway，稍后统一追加）+ 该组是否需要转换 BP。

        返回 (own_components, needs_router, supplemented)：needs_router 依据**本组原始拓扑**
        是否含 TransformProcess 判定——通用组（源 BS 的 TargetConfigNames 指向它）需要；
        Skill 自带 BP 的组不需要（本组拓扑里根本没有它）。本组自己的 BP 属本组组件，
        不再当共享件剔除。supplemented = 本组 Agent B 遗漏后**注册表保底补齐**的组件类型
        （红线审计：必须按组可区分，不能与 AI 决策混在一起）。
        """
        topo = build_pipeline_topology(
            g.get("mappings") or [],
            source_type=g.get("source_type") or "FHIR",
            target_type=g.get("target_type") or "DB",
            suggested_types=g.get("suggested_types"),
            source_config=g.get("source_config") or {},
            target_config=g.get("target_config") or {},
            ai_components=g.get("ai_components"),
            bp_name=bp_name)
        if g.get("_sql2fhir"):
            # sql2fhir 组：拓扑由 Skill executor 按布局生成（含 Agent 编译的 BP），直接采用
            topo = {"production": "demo.DataflowProduction",
                    "components": g.get("ai_components") or []}
        raw = topo.get("components") or []
        needs_router = any(str(c.get("type") or "") == "TransformProcess" for c in raw)
        supplemented = [str(x) for x in (topo.get("ai_supplemented") or [])]
        return [c for c in raw if not _is_infra_component(c)], needs_router, supplemented

    categories = [str(g.get("_category") or pipeline_instances.category_of(
        g.get("design_skill"), g.get("source_type"), g.get("target_type")))
        for g in pipelines]
    # 每组一个专属 BP（按管道类别命名；同类别多组自动加序号）
    taken_bp: set[str] = set()
    bp_names = [_bp_name_for(cat, taken_bp) for cat in categories]
    group_parts = [_group_components(g, bp)
                   for g, bp in zip(pipelines, bp_names)]
    group_comps = [p[0] for p in group_parts]
    need_router = any(p[1] for p in group_parts)
    # 每组的「注册表保底补齐」清单落回组上（管道实体的 ai.supplemented 必须按组归属，
    # 否则多管道提交时该标记会丢失 → 规则补齐被误报成纯 AI 决策）
    for g, part in zip(pipelines, group_parts):
        if part[2]:
            g["_ai_supplemented"] = part[2]
    # 统计源 BS 重名（同名源 BS 出现在多组 = 冲突：按管道类别改名，消除"名字漂移"与互相顶掉）
    name_counts: dict[str, int] = {}
    for comps in group_comps:
        for c in comps:
            if str(c.get("type") or "") in ("SQLService", "FHIRService"):
                _n = str(c.get("name") or "")
                name_counts[_n] = name_counts.get(_n, 0) + 1

    components: list[dict] = []
    used_names: set[str] = set()
    for gi, (comps, cat) in enumerate(zip(group_comps, categories)):
        src_names: list[str] = []
        for c in comps:
            ctype = str(c.get("type") or "")
            nm = str(c.get("name") or ctype)
            if ctype in ("SQLService", "FHIRService") and name_counts.get(nm, 0) > 1:
                base = f"{nm}__{pipeline_instances.slug(cat)}"
                new_nm, k = base, 2
                while new_nm in used_names:
                    new_nm = f"{base}_{k}"
                    k += 1
                logger.info("源组件同名跨管道 → 按管道类别命名: %s -> %s", nm, new_nm)
                c["name"] = new_nm
                nm = new_nm
            c["category"] = cat
            used_names.add(nm)
            if ctype in ("SQLService", "FHIRService"):
                src_names.append(nm)
            components.append(c)
        # 该组实际源 BS 名（供路由表 ^demo.Config("pipe", <BS名>) 使用，必须与拓扑一致）
        if src_names:
            pipelines[gi]["_src_bn"] = src_names[0]
        # 本组专属转换 BP 名（供 ^demo.Config("bp", <BP名>) 写转换参数）
        pipelines[gi]["_bp_name"] = bp_names[gi]
    components = _dedupe_components(components)
    # 基础设施（真正跨管道共享，各 1 个）：JavaGateway（JDBC 网关）；
    # 转换 BP 不再共享——每组在自己的拓扑里各有一个专属实例（见 _bp_name_for）。
    for t in type_registry.get_common_components():
        if t["type"] == "JavaGateway":
            comp = _common_component(t)
            comp["category"] = "shared"
            components.append(comp)
    if not need_router:
        logger.info("本次拓扑无组走通用路由 → 不生成转换 BP（无需该组件，省 1 个许可单元）")
    return {"production": "demo.DataflowProduction",
            "categories": categories, "components": components}



def _active_items_from_topology(topology: dict) -> list[str]:
    """本次管道要启用的业务主机名（许可预算用；JavaGateway 等基础设施由 IRIS 侧保留）。"""
    names: list[str] = []
    for c in (topology or {}).get("components", []) or []:
        cls = str((c or {}).get("className") or "")
        if "JavaGateway" in cls:
            continue
        n = str((c or {}).get("name") or "").strip()
        if n and n not in names:
            names.append(n)
    return names


def _is_infra_component(comp: dict) -> bool:
    """基础设施组件判定（真正跨管道共享、全局仅 1 个实例）：仅 JavaGateway（JDBC 网关）。

    注意：转换 BP（TransformProcess）**不是**共享组件——Ens 的业务主机身份 = Item 名
    （className 可复用），每条数据管道各有一个专属 BP 实例（如 TransformProcess__sql2soap），
    随其管道一起启停/让路，不再靠 `shared` 豁免（否则停管道时许可不放）。
    """
    ttype = str((comp or {}).get("type") or "")
    cls = str((comp or {}).get("className") or "")
    return ttype == "JavaGateway" or "JavaGateway" in cls


def _is_shared_component(comp: dict) -> bool:
    """共享组件判定（兼容旧名，等价 _is_infra_component）：category="shared" 的成员。"""
    return _is_infra_component(comp)


def _stamp_categories(topology: dict, category: str) -> dict:
    """给拓扑组件打「管道类别」：本管道组件 = 管道类别，基础设施组件 = "shared"。

    类别会渲染为 Ens 业务主机的 Category（PipelineGenerator.RenderComponent），
    是「管道分组 / 按管道启停 / LLM 上下文」的事实来源；转换 BP 属本管道（随管道启停）。
    """
    for comp in (topology or {}).get("components") or []:
        comp["category"] = "shared" if _is_shared_component(comp) else category
    return topology


def _pipeline_categories(*categories) -> list[str]:
    """本次生成的管道类别集合（许可预算 keep 名单；shared 由 IRIS 侧自动保留）。"""
    return [c for c in dict.fromkeys(str(x) for x in categories if x)]


def _topology_groups(topology: dict) -> list[dict]:
    """按组件 Category 聚合「管道分组」（许可调度粒度；基础设施 JavaGateway 不参与预算）。

    返回 [{"category", "items": [业务主机名, ...]}]，顺序 = 拓扑中组件首次出现顺序
    （即用户提交的分组顺序），保证调度结果确定（先进先服务）。
    """
    order: list[str] = []
    buckets: dict[str, list[str]] = {}
    for c in (topology or {}).get("components") or []:
        if _is_infra_component(c):
            continue
        cat = str((c or {}).get("category") or "unknown")
        nm = str((c or {}).get("name") or "").strip()
        if not nm:
            continue
        if cat not in buckets:
            buckets[cat] = []
            order.append(cat)
        if nm not in buckets[cat]:
            buckets[cat].append(nm)
    return [{"category": cat, "items": buckets[cat]} for cat in order]


def _mark_suspended_components(topology: dict, plan: dict) -> list[str]:
    """把被许可调度停用的分组组件标 `enabled=False`（渲染为 Ens Item Enabled="false"）。

    「许可调度」的落地点：放不下的分组**组件照旧生成**（Production 里可查、可供验证），
    只是初始为停用状态（不占许可单元），用户在 UI 一键切换即可启用。返回被标记的组件名。
    """
    cats = {str(c) for c in (plan.get("suspended") or [])}
    marked: list[str] = []
    for c in (topology or {}).get("components") or []:
        if _is_infra_component(c):
            continue
        if str((c or {}).get("category") or "") in cats:
            c["enabled"] = False
            nm = str((c or {}).get("name") or "")
            if nm:
                marked.append(nm)
    return marked


def _license_plan(topology: dict) -> dict:
    """本拓扑的许可容量调度计划（超容量的分组停用而非失败，见 schedule_groups）。"""
    return pipeline_validator.schedule_groups(_topology_groups(topology))


def _source_bs_names(topology: dict) -> list[str]:
    """拓扑中的源业务主机名（SQLService / FHIRService / FHIRSyncService）。"""
    names: list[str] = []
    for c in (topology or {}).get("components") or []:
        if str((c or {}).get("type") or "") in ("SQLService", "FHIRService"):
            n = str((c or {}).get("name") or "").strip()
            if n and n not in names:
                names.append(n)
    return names


def _all_pipeline_source_bs_names() -> list[str]:
    """全部已登记管道的源 BS 名（判码缓存刷新遇许可不足时可按需暂停腾单元）。"""
    names: list[str] = []
    try:
        for rec in pipeline_instances.list_instances():
            routes = rec.get("routes") or {}
            cand = list(routes.get("source_bs_names") or [])
            if routes.get("source_bs"):
                cand.append(routes["source_bs"])
            for n in cand:
                if n and n not in names:
                    names.append(n)
    except Exception as exc:  # noqa: BLE001 - 诊断性读取失败不影响生成
        logger.warning("读取已登记管道源 BS 列表失败: %s", exc)
    return names


def _refresh_term_cache(mappings: list[dict] | None, topology: dict | None,
                        validation: dict | None) -> dict:
    """刷新判码缓存（`term_map` 运行期执行的前提）并把结果**显式**带回生成响应。

    背景（实测 2026-09-14）：生成后刷新时许可已被 Ens 业务主机占满（6 业务主机 +
    JavaGateway + 后端 = 8/8）→ `Unable to allocate a license` → Agent A 的
    `term_map:cn2snomed` 决策在运行期悄悄失效（FHIR Condition 只剩国标 ICD-10 coding、
    缺 SNOMED 双 coding）。这里：① 用 `term_cache.build_term_cache_safe`（许可不足自动
    临时暂停源 BS 腾单元，刷完恢复）；② 把「有 term_map 指令但缓存缺项」作为验证告警并入
    validation（UI 可见）；③ 返回状态供响应与前端展示（AI 决策未落地 = 显式失败，不静默）。

    返回 {"ok", "attempts", "paused_items", "restored_items", "error", "required", "missing", "note"}。
    """
    from backend.services import term_cache

    pause_items = _source_bs_names(topology or {}) + _all_pipeline_source_bs_names()
    report = term_cache.build_term_cache_safe(mappings or [], pause_items=pause_items)
    missing = list(report.get("missing") or [])
    required = int(report.get("required") or 0)
    ok = bool(report.get("ok")) and not missing
    if not ok:
        msg = ("term_map 判码缓存未就绪：AI 的术语映射决策无法在运行期落地"
               "（目标 FHIR 资源会缺目标系统 coding，如 Condition 缺 SNOMED 双 coding）")
        if report.get("error"):
            msg += f"；错误：{str(report['error'])[:200]}"
        if missing:
            msg += f"；缺 {len(missing)} 项（{', '.join(missing[:6])}）"
        logger.error("判码缓存未就绪: %s", msg)
        if isinstance(validation, dict):
            validation.setdefault("issues", []).append(
                {"severity": "warning", "check": "term_map_cache", "message": msg})
    return {"ok": ok, "attempts": report.get("attempts"),
            "paused_items": report.get("paused_items") or [],
            "restored_items": report.get("restored_items") or [],
            "error": str(report.get("error") or ""), "required": required,
            "cached": int(report.get("cached_total") or 0),
            "negative": int(report.get("negative_total") or 0),
            "new_cached": int(report.get("cached") or 0),
            "new_negative": int(report.get("negative") or 0),
            "missing": missing,
            "note": ("term_map 决策的判码 Skill 缓存：cached/negative=缓存累计（cached=判出目标码，"
                     "运行期追加双 coding；negative=Skill 判定池内无对应，不追加、非失败）；"
                     "new_cached/new_negative=本次新判定；missing=尚未判定（会缺目标系统 coding）")}


def _datasource_for_mappings(mappings: list[dict] | None, ds_type: str = "") -> dict | None:
    """按映射的源表/资源名反查所属数据源（参数化兜底，不做任何 AI 决策）。

    用途：调用方未给 source_id（或给的 ID 已失效，如重置环境后重跑旧脚本）时，仍能引用
    正确的数据源 runtime（DSN / jdbc_url / FHIR endpoint），避免默默退化成 localTarget
    去读 USER 库（曾致"源表读不到数据"的假失败）。
    """
    names = set()
    for m in mappings or []:
        if not isinstance(m, dict):
            continue
        tbl = str(m.get("source") or "").split(".")[0].strip()
        if tbl and not tbl.startswith(("DS", "M", "TG", "concat")):
            names.add(tbl)
    if not names:
        return None
    best, hit_best = None, 0
    for ds in repository.list_datasources():
        if ds_type and (ds.get("type") or "") != ds_type:
            continue
        hit = len({str(a.get("name") or "") for a in repository.list_assets(ds.get("id"))
                   if str(a.get("name") or "") in names})
        if hit > hit_best:
            best, hit_best = ds, hit
    return best


def _target_for_mappings(mappings: list[dict] | None, target_type: str = "") -> dict | None:
    """按映射的目标表/实体名反查已登记目标（target_id 缺失/失效时的参数化兜底）。"""
    names = {str(m.get("target_table") or "").strip() for m in (mappings or [])
             if isinstance(m, dict)}
    names.discard("")
    if not names:
        return None
    for tg in repository.list_targets():
        if target_type and (tg.get("type") or "") != target_type:
            continue
        for tb in (tg.get("tables") or []):
            tb_name = str((tb or {}).get("table") or (tb or {}).get("entity_name") or "")
            if tb_name and tb_name in names:
                return tg
    return None


def _reset_ens_messages_for_generate() -> None:
    """生成前停 Production 并清空 Ens 消息历史。

    为什么：消息历史里遗留的 Error 消息（如上一版 Agent BP 的运行期报错）会被 smoke 校验
    当成本次生成的问题 → 触发"自动重建一次"后仍失败。清空后校验只反映本次生成的结果。
    """
    try:
        iris_connector.class_method_value("Ens.Director", "StopProduction", 30)
    except Exception as exc:  # noqa: BLE001 - 未运行/类不存在时忽略
        logger.info("生成前停 Production 跳过: %s", str(exc)[:120])
    try:
        iris_connector.execute("DELETE FROM Ens.MessageBody")
        iris_connector.execute("DELETE FROM Ens.MessageHeader")
        logger.info("已清空 Ens 消息历史（避免历史错误干扰 smoke 校验）")
    except Exception as exc:  # noqa: BLE001 - 清理失败不阻断生成
        logger.warning("清空消息历史失败（不阻断）: %s", str(exc)[:120])


def _generate_multi_pipelines(pipelines: list[dict]) -> dict:
    """多管道生成：单 Production 内多套 BS/BP/BO（阶段1，简化验证闭环）。

    每组 pipeline（body.pipelines 元素）:
      {source_type?, source_id?, source_config?, target_type?, target_id?, target_config?, mappings:[...]}

    流程：逐组补齐源/目标配置 → build_multi_pipeline_topology 合并拓扑 →
          写路由表 ^demo.Config("pipe", <源BS名>) → PipelineGenerator 渲染启动。
    返回 success data（result/validation）。
    """
    import iris

    # 生成前先停 Production 并清消息历史（否则历史 Error 消息会让 smoke 校验误判本次失败）
    _reset_ens_messages_for_generate()

    groups: list[dict] = []
    all_mappings: list[dict] = []
    fhir_cfg: dict = {}

    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        for g in pipelines:
            src_type = g.get("source_type") or ""
            tgt_type = g.get("target_type") or ""
            src_cfg = dict(g.get("source_config") or {})
            tgt_cfg = dict(g.get("target_config") or {})

            # —— 源配置补齐（source_id → 数据源；ID 缺失/失效时按映射源表反查）——
            ds = repository.get_datasource(g.get("source_id")) if g.get("source_id") else None
            if not ds:
                ds = _datasource_for_mappings(g.get("mappings"), "SQL" if src_type == "SQL" else "")
                if ds:
                    logger.info("管道组 source_id=%s 无效，按映射源表反查到数据源 %s",
                                g.get("source_id"), ds.get("id"))
            dcfg = (ds.get("config") or {}) if ds else {}
            # 运行参数统一取归一化契约 connection（repository.datasource_runtime）；
            # 无数据源时留空字典，避免未绑定变量（曾致多管道生成直接 500）
            rconn = (((ds.get("runtime") or {}).get("connection")
                      or repository.datasource_runtime(ds)["connection"]) if ds else {})
            if ds:
                src_type = ds.get("type") or src_type
                src_cfg.setdefault("dsn", rconn.get("dsn") or dcfg.get("dsn") or "localTarget")
                src_cfg.setdefault("query", rconn.get("query") or dcfg.get("query") or "")
                src_cfg.setdefault("key_field", rconn.get("key_field") or dcfg.get("key_field") or "ID")
                src_cfg.setdefault("endpoint", rconn.get("endpoint") or "")
                src_cfg.setdefault("username", rconn.get("username") or "")
                src_cfg.setdefault("password", rconn.get("password") or "")
            if src_type == "SQL":
                # 多管道关键：源 BS 的 Query 必须以**本组映射的源表**为准。
                # 否则会沿用数据源的默认 query（可能是另一张表）—— 曾导致 SQL→SOAP 组误读
                # Diagnosis 表，把诊断 ID 当成患者号投递给 SOAP 目标。
                for _m in g.get("mappings") or []:
                    _tbl = str(_m.get("source") or "").split(".")[0].strip()
                    if _tbl and not _tbl.startswith(("DS", "M", "TG", "concat")):
                        src_cfg["query"] = f"SELECT * FROM SQLUser.{_tbl}"
                        break
                if (src_cfg.get("dsn") or "").startswith("jdbc:"):
                    src_cfg["dsn"] = "localTarget"
                # DSN：优先引用数据源已注册的 DSN；未注册则按 jdbc_url 幂等创建（同单管道）
                from backend.services import jdbc_dsn as _jdbc_dsn
                _registered = _jdbc_dsn.register_for_datasource(ds)
                if _registered:
                    src_cfg["dsn"] = _registered
                src_cfg["jdbc_url"] = (src_cfg.get("jdbc_url") or rconn.get("jdbc_url")
                                       or dcfg.get("jdbc_url") or "")
                _jdbc_dsn.ensure_for({
                    "dsn": src_cfg.get("dsn"), "jdbc_url": src_cfg["jdbc_url"],
                    "username": src_cfg.get("username") or rconn.get("username") or "superuser",
                    "password": src_cfg.get("password") or rconn.get("password") or "SYS"})
                if not src_cfg.get("query"):
                    src_cfg["query"] = "SELECT * FROM SQLUser.PatientSource"
                    for _m in g.get("mappings") or []:
                        _s = (_m.get("source") or "").split(".")[0] or _m.get("source")
                        if _s and not _s.startswith(("DS", "M", "TG", "concat")):
                            src_cfg["query"] = f"SELECT * FROM SQLUser.{_s}"
                            break
            if not src_type:
                src_type = "SQL" if src_cfg.get("dsn") else "FHIR"

            # —— 目标类型推断 + 目标配置补齐（target_id → 目标；ID 缺失/失效时按目标表反查）——
            if not tgt_type:
                for _m in g.get("mappings") or []:
                    if _m.get("target_type") == "SOAP":
                        tgt_type = "SOAP"
                        break
            if not tgt_type:
                tgt_type = "DB"
            tg = repository.get_target(g.get("target_id")) if g.get("target_id") else None
            if not tg:
                tg = _target_for_mappings(g.get("mappings"), tgt_type)
                if tg:
                    logger.info("管道组 target_id=%s 无效，按映射目标表反查到目标 %s",
                                g.get("target_id"), tg.get("id"))
            if tg:
                tgt_type = tg.get("type") or tgt_type
                tconn = tg.get("connection") or {}
                if (tg.get("type") or "") == "SOAP":
                    tgt_cfg.setdefault("wsdl", tconn.get("wsdl") or "")
                    tgt_cfg.setdefault("service", tconn.get("service") or "default")
                    tgt_cfg.setdefault("packages", tconn.get("packages") or {})
                    tgt_cfg.setdefault("bo_class", tconn.get("bo_class") or tconn.get("boClass") or tg.get("bo_class") or "")
                    tgt_cfg.setdefault("endpoint", tconn.get("endpoint") or Config.MOCK_SOAP_URL)
            # SOAP 目标默认投递地址（未显式指定时指向 Python mock，与单管道路径一致）
            if tgt_type == "SOAP":
                tgt_cfg.setdefault("service", tgt_cfg.get("service") or "default")
                tgt_cfg.setdefault("endpoint", tgt_cfg.get("endpoint") or Config.MOCK_SOAP_URL)

            # —— mapping 校正 target_type 并写回 ——
            maps = []
            for _m in g.get("mappings") or []:
                if not isinstance(_m, dict) or not _m.get("id"):
                    continue
                _m["target_type"] = tgt_type
                repository.set_json("^demo.Mapping", _m["id"], _m)
                maps.append(_m)
            if not maps:
                raise ValueError("管道组缺少 mappings")

            all_mappings.extend(maps)
            if src_type == "FHIR":
                fhir_cfg.setdefault("endpoint", src_cfg.get("endpoint") or "")
                fhir_cfg.setdefault("username", src_cfg.get("username") or "")
                fhir_cfg.setdefault("password", src_cfg.get("password") or "")

            groups.append({
                "source_type": src_type,
                "source_config": src_cfg,
                "target_type": tgt_type,
                "target_config": tgt_cfg,
                "mappings": maps,
                "source_id": (ds.get("id") if ds else g.get("source_id")),
                "target_id": (tg.get("id") if tg else g.get("target_id")),
                # Agent B 设计输入：源/目标运行契约（归一 runtime，密码由 llm_client 脱敏）
                "_rt_src": repository.datasource_runtime(ds) if ds else None,
                "_rt_tgt": repository.target_runtime(tg) if tg else None,
            })

        # —— 合并拓扑 + 写路由表 ——
        # C1：逐组转换验证-修复（字段/结构校验，映射修正后写回）
        for _g in groups:
            _g_maps = _g.get("mappings") or []
            if not _g_maps:
                continue
            try:
                _fix = transformation_validator.validate_and_fix_transformation(
                    _g_maps, assets=_c1_assets(_g.get("source_id")),
                    target_models=_c1_target_models(_g_maps))
                if _fix.get("status") == "ok" and _fix.get("mappings"):
                    for _m in _fix["mappings"]:
                        if not isinstance(_m, dict) or not _m.get("id"):
                            continue
                        _m["target_type"] = _g["target_type"]
                        repository.set_json("^demo.Mapping", _m["id"], _m)
                    _g["mappings"] = _fix["mappings"]
                else:
                    logger.warning("多管道 C1 转换验证未完全通过: %s", _fix.get("message"))
            except Exception as _ce:  # noqa: BLE001
                logger.warning("多管道 C1 转换验证异常: %s", _ce)
        all_mappings = [m for _g in groups for m in (_g.get("mappings") or [])]

        # —— Agent B（LLM）：逐组设计管道拓扑（AI 决定各组件的构成与顺序；sql2fhir 组按 Skill 分发）——
        for _g in groups:
            try:
                # 上下文精准化：只把本组映射涉及的表交给 Agent（否则会为数据源每张表生成源 BS）
                _g_tables = _source_tables_from_mappings(_g.get("mappings") or [])
                _g_rt_src, _g_models = _scope_source_context(
                    _g.pop("_rt_src", None), _c1_assets(_g.get("source_id")), _g_tables)
                _p = llm_client.recommend_pipeline(
                    _g.get("mappings") or [],
                    source_type=_g["source_type"], target_type=_g["target_type"],
                    available_components=AVAILABLE_COMPONENTS,
                    source_models=_g_models,
                    transformation_plan={"mappings": _g.get("mappings") or []},
                    source_runtime=_g_rt_src,
                    target_runtime=_g.pop("_rt_tgt", None),
                    # 已登记管道事实（同一源/目标 → 更新既有管道而非新增；决策仍归 LLM）
                    existing_pipelines=pipeline_instances.existing_pipelines_brief())
                _comps = ((_p.get("pipeline") or {}).get("components") or [])
                if not _comps:
                    raise ValueError("Agent B 未返回任何组件")
                _g["design_skill"] = _p.get("design_skill") if isinstance(_p, dict) else None
                # sql2fhir-patient-tx 组：布局 executor 推导 → 写 config → Agent 生成 BP → 组件由 Skill 产出
                if (_g["source_type"] == "SQL" and _g["target_type"] == "FHIR"
                        and _g["design_skill"] == "sql2fhir-patient-tx"):
                    from backend.services import sql2fhir_executor as _sfx
                    maps_layout, meta = _resolve_sql_source_tables(
                        _g.get("mappings") or [], _g.get("source_id"))
                    try:
                        layout = _sfx.derive_sql2fhir_layout(maps_layout, meta)
                    except Exception as _de:  # noqa: BLE001 - 包装为可定位的错误
                        raise ValueError(
                            "该组映射无法推导 sql2fhir 布局（组内源表: "
                            f"{[str(m.get('source')) for m in (_g.get('mappings') or [])]}）：{_de}")
                    layout = _sfx.enrich_layout_with_mappings(layout, maps_layout)
                    _save_sql2fhir_layout(layout)
                    _bp_ok, _bp_msg = _ensure_sql2fhir_bp(
                        _p, _g.get("mappings") or [], "SQL", "FHIR",
                        AVAILABLE_COMPONENTS, None, None)
                    if not _bp_ok:
                        raise ValueError(f"Agent 生成 BP 失败（组 sql2fhir）: {_bp_msg}")
                    _g["ai_components"] = _sfx.build_sql2fhir_components(
                        layout, _g.get("source_config") or {}, _g.get("target_config") or {})
                    _g["_sql2fhir"] = True
                    _g["_src_bn"] = f"SQLService_{layout['patient_table']}"
                    logger.info("多管道 sql2fhir 组：布局+Agent BP 就绪（%d 组件）",
                                len(_g["ai_components"]))
                else:
                    _g["ai_components"] = _comps
                    logger.info("多管道 Agent B 设计 %s→%s 完成（%d 组件）：%s",
                                _g["source_type"], _g["target_type"], len(_comps),
                                [c.get("type") for c in _comps])
            except Exception as _be:  # noqa: BLE001 - Agent B 失败即报错，不静默回退规则
                raise ValueError(
                    f"Agent B 数据管道 AI 生成失败（组 {_g['source_type']}→{_g['target_type']}）：{_be}"
                ) from _be

        # 管道类别（= 设计 Skill，来自 Agent B 决策）：渲染为 Ens 业务主机 Category，
        # 供管道分组 / 按管道启停 / LLM 上下文；同源同目标的两组同名源 BS 按此改名（消除名字漂移）
        for _g in groups:
            _g["_category"] = pipeline_instances.category_of(
                _g.get("design_skill"), _g["source_type"], _g["target_type"])
        topology = build_multi_pipeline_topology(groups)
        # 许可调度（社区版 KeyLicenseUnits=8，业务主机常驻各占 1 个）：按组顺序装箱，
        # 放不下的分组**组件照旧生成、初始停用**（不再直接失败）→ 用户在「数据管道」卡片一键切换
        _plan = _license_plan(topology)
        _susp = _mark_suspended_components(topology, _plan)
        if _susp:
            logger.warning("许可调度：以下组件生成后处于停用状态 %s", _susp)
        _save_pipeline_topology(topology)
        _save_fhir_runtime_config(
            topology,
            next((g.get("target_config") for g in groups if g["target_type"] == "FHIR"), None))
        for _g in groups:
            src_bn = _g.get("_src_bn") or ("SQLService" if _g["source_type"] == "SQL"
                                           else "FHIRService")
            _tt = _g["target_type"]
            _m0 = _g["mappings"][0]
            pipe = {"mapping": _m0["id"], "target_type": _tt}
            if _tt == "SOAP":
                pipe["service"] = (_g["target_config"] or {}).get("service") or "default"
            else:
                pipe["table"] = _m0.get("target_table") or ""
            # ① 权威：本管道**自己的 BP** 读自己的参数（一管道一 BP，互不干扰）
            #    demo.TransformProcess.OnRequest: $Get(^demo.Config("bp", ..%ConfigName))
            bp_name = _g.get("_bp_name") or "TransformProcess"
            native.set(json.dumps(pipe, ensure_ascii=False), "^demo.Config", "bp", bp_name)
            # ② 源 BS → BP 的投递目标（供代码内显式投递的 BS 读取，如 demo.FHIRService）
            native.set(bp_name, "^demo.Config", "bp_target", src_bn)
            # ③ 兼容：旧的「按源 BS 路由」表 + 全局单值键（历史管道 / 兜底路径）
            native.set(json.dumps(pipe, ensure_ascii=False), "^demo.Config", "pipe", src_bn)
            native.set(_m0["id"], "^demo.Config", "pipeline", "active_mapping")
            native.set(_tt, "^demo.Config", "pipeline", "target_type")

        logger.info("多管道拓扑: %s", [c.get("name") for c in topology["components"]])
        mappings_json = json.dumps(all_mappings, ensure_ascii=False)
        config_json = json.dumps(fhir_cfg, ensure_ascii=False) if fhir_cfg else ""
        _src_types = sorted({g["source_type"] for g in groups})
        _tgt_types = sorted({g["target_type"] for g in groups})

        # 许可预算：社区版 KeyLicenseUnits=8，每个 Ens 业务主机常驻占 1 个；让不属于本次
        # 管道的旧管道组件让出许可（等价于用户手工"生成新管道前先关闭旧管道组件"），
        # 否则 Production 启动后许可立即耗尽 → 后端连不上 IRIS → 生成/校验必然失败
        # 许可预算：让不属于本次管道的旧管道组件让出许可（等价于用户手工"生成新管道前先关闭
        # 旧管道组件"）；本次拓扑内部由 apply_license_budget(groups=...) 做容量调度
        # （放不下的分组已在上面标 enabled=false，不会占许可单元）
        license_budget = pipeline_validator.apply_license_budget(
            keep_categories=_pipeline_categories(*[_g["_category"] for _g in groups]),
            groups=_topology_groups(topology))

        # 前置连通门禁（按组构造源/目标 runtime，check_connection 现场探测）
        _cc_src = [{"kind": g["source_type"], "role": "source",
                    "connection": dict(g.get("source_config") or {})} for g in groups]
        _cc_tgt = [{"kind": g["target_type"], "role": "target",
                    "connection": dict(g.get("target_config") or {})} for g in groups]
        _conn_check = pipeline_validator.check_connection(sources=_cc_src, targets=_cc_tgt)
        if not _conn_check.get("ok"):
            _msgs = [i.get("message", "") for i in _conn_check.get("issues", [])][:3]
            raise ValueError("源/目标连通性检查未通过: " + "; ".join(_msgs))

        def _gen_and_validate():
            _r = iris_connector.class_method_value(
                "demo.PipelineGenerator", "Generate", mappings_json, config_json)
            _v = pipeline_validator.run_pipeline_validation(
                topology, source_types=_src_types, target_types=_tgt_types)
            return _r, _v

        result, validation = _gen_and_validate()
        # 运行期错误回喂（有界 1 次）：FHIR 结构类 → 逐组 C1 修映射；BP 代码类 → method_updates 修复
        if result != "OK" or not validation.get("ok"):
            logger.warning("多管道验证未通过明细: %s",
                           json.dumps(validation.get("issues") or [], ensure_ascii=False)[:900])
            try:
                _rt = (validation.get("results") or {}).get("runtime") or {}
                _kinds = _rt.get("kinds") or []
                _err_text = "\n".join(i.get("message", "") for i in (_rt.get("issues") or []))
                if "fhir_schema" in _kinds:
                    logger.info("多管道运行期 FHIR 结构错误 → 逐组 C1 修复映射")
                    for _g in groups:
                        if _g.get("target_type") != "FHIR":
                            continue
                        _f2 = transformation_validator.validate_and_fix_transformation(
                            _g.get("mappings") or [], assets=_c1_assets(_g.get("source_id")),
                            target_models=_c1_target_models(_g.get("mappings") or []))
                        if _f2.get("status") == "ok" and _f2.get("mappings"):
                            for _m in _f2["mappings"]:
                                if isinstance(_m, dict) and _m.get("id"):
                                    repository.set_json("^demo.Mapping", _m["id"], _m)
                            _g["mappings"] = _f2["mappings"]
                    all_mappings = [m for _g in groups for m in (_g.get("mappings") or [])]
                    mappings_json = json.dumps(all_mappings, ensure_ascii=False)
                    result, validation = _gen_and_validate()
                if "bp_code" in _kinds:
                    from backend.services import generated_bp as _gbp
                    _sf_group = next((g for g in groups
                                      if g["source_type"] == "SQL" and g["target_type"] == "FHIR"),
                                     None)
                    if _sf_group:
                        logger.info("多管道运行期 BP 代码错误 → method_updates 增量修复")
                        _reps = _gbp.repair_from_runtime_errors(
                            mappings=_sf_group.get("mappings") or [],
                            source_type="SQL", target_type="FHIR",
                            available_components=AVAILABLE_COMPONENTS,
                            errors_text=_err_text)
                        if _reps.get("ok"):
                            try:
                                iris_connector.class_method_value("Ens.Director", "StopProduction", 10)
                                iris_connector.class_method_value(
                                    "Ens.Director", "StartProduction", "demo.DataflowProduction")
                            except Exception as _se:  # noqa: BLE001
                                logger.warning("多管道修复后重启 Production 失败: %s", _se)
                            result, validation = _gen_and_validate()
                        else:
                            logger.warning("多管道 BP 方法修复未通过: %s", _reps.get("message"))
                            # 回滚到上一版通过验证的 BP（显式告警）→ 重启 Production 复验，
                            # 保证「新增管道」不会因本次 Agent BP 质量波动而把数据通路弄坏
                            _rb = _gbp.restore_last_good()
                            if _rb.startswith("restored"):
                                logger.warning("多管道 BP 已回滚 last_good（%s）→ 重启并复验", _rb)
                                try:
                                    iris_connector.class_method_value(
                                        "Ens.Director", "StopProduction", 10)
                                    iris_connector.class_method_value(
                                        "Ens.Director", "StartProduction", "demo.DataflowProduction")
                                except Exception as _se:  # noqa: BLE001
                                    logger.warning("回滚后重启 Production 失败: %s", _se)
                                result, validation = _gen_and_validate()
            except Exception as _re:  # noqa: BLE001 - 回喂失败不阻断主流程
                logger.warning("多管道运行期回喂失败（不影响主流程）: %s", _re)
        # 失败自动重建一次（Ens 重启/瞬态类问题）；仍失败则沉淀经验后返回错误
        if result != "OK" or not validation.get("ok"):
            logger.warning("多管道首次生成/验证未通过（%s），自动重建一次", result)
            result, validation = _gen_and_validate()
        if result != "OK" or not validation.get("ok"):
            try:
                _emsg = "；".join([i.get("message", "") for i in validation.get("issues", [])
                                   if i.get("severity") == "error"][:3])
                pipeline_validator.save_validation_issue(
                    pattern="multi-pipeline %s -> %s" % ("/".join(_src_types), "/".join(_tgt_types)),
                    resolution=f"多管道生成失败: {result} / {_emsg}", source="multi-generate")
            except Exception as _se:  # noqa: BLE001
                logger.warning("多管道经验沉淀失败: %s", _se)
        # 术语判码缓存：mapping 含 term_map 时按源表编码值预判定（Skill 执行；AI 决策能否
        # 在运行期落地取决于此缓存，故失败/缺项必须显式带回响应与 validation，不静默）
        term_cache_report: dict = {}
        if result == "OK":
            try:
                term_cache_report = _refresh_term_cache(all_mappings, topology, validation)
            except Exception as _tc:  # noqa: BLE001 - 刷新异常也显式报告
                logger.error("多管道判码缓存刷新异常: %s", _tc)
                term_cache_report = {"ok": False, "error": str(_tc),
                                     "note": "判码缓存刷新异常（term_map 决策将无法落地）"}

        # 管道实体登记（受管理持久对象）：按 (source_id, target_id, design_skill) 逐组登记，
        # 同一身份重复生成 = 更新同一管道（不新增）。生成失败则登记为失败原因（不写成功记录）。
        instance_records: list[dict] = []
        instance_error = ""
        if result == "OK":
            try:
                prod_items = pipeline_validator.production_items()
                for _g in groups:
                    _cat = _g["_category"]
                    _comps = [c for c in topology.get("components") or []
                              if c.get("category") == _cat]
                    instance_records.append(pipeline_instances.upsert_from_generation(
                        source_id=_g.get("source_id"), target_id=_g.get("target_id"),
                        source_type=_g["source_type"], target_type=_g["target_type"],
                        design_skill=_g.get("design_skill"), category_hint=_cat,
                        mapping_ids=[m.get("id") for m in (_g.get("mappings") or []) if m.get("id")],
                        components=_comps or (_g.get("ai_components") or []),
                        routes={"source_bs": _g.get("_src_bn") or ""},
                        ai={"driven": True,
                            "supplemented": (_g.get("_ai_supplemented")
                                             or topology.get("ai_supplemented") or [])},
                        validation=validation, production_items=prod_items))
                pipeline_instances.reconcile_states(production_items=prod_items)
            except Exception as _pie:  # noqa: BLE001 - 登记失败不改变"生成成功"事实，但必须显式暴露
                instance_error = str(_pie)
                logger.error("管道实体登记失败（生成已成功，实体未登记）: %s", _pie)

        return {"result": result, "production": "demo.DataflowProduction",
                "validation": validation,
                # 许可预算：本次生成让旧管道组件让出的许可单元（社区版仅 8 个；避免"启动即超限"）
                "license_budget": license_budget,
                # 术语判码缓存（term_map 决策的运行期前提）：ok=false 表示 AI 决策未能落地
                "term_cache": term_cache_report,
                # 管道实体（受管理对象）：本次生成新建/更新的管道
                "pipelines": instance_records,
                "pipeline_error": instance_error,
                # AI 驱动信息：每组管道均由 Agent B（LLM）设计拓扑
                "ai": {
                    "driven": True,
                    "groups": [{
                        "source_type": _g["source_type"], "target_type": _g["target_type"],
                        "category": _g.get("_category"),
                        "design_skill": _g.get("design_skill"),
                        "components": [c.get("type") for c in (_g.get("ai_components") or [])],
                    } for _g in groups],
                }}
    finally:
        iris_connector.reset_connections()


@pipelines_bp.post("/generate")
def generate():
    """生成并启动数据管道。支持异构组合（source_type/target_type + target_config）与多管道（pipelines 列表）。"""
    body = request.get_json(silent=True) or {}
    # 多管道模式：body.pipelines = [组1, 组2, ...]，单 Production 内多套 BS/BP/BO
    pipelines_arg = body.get("pipelines")
    if pipelines_arg:
        try:
            multi = _generate_multi_pipelines(pipelines_arg)
        except Exception as exc:  # noqa: BLE001
            logger.error("多管道生成失败: %s", exc)
            return error(f"多管道生成失败: {exc}"), 500
        if multi.get("result") != "OK":
            return error(f"多管道生成失败: {multi.get('result')}"), 500
        v = multi.get("validation") or {}
        if not v.get("ok"):
            msgs = [i.get("message") for i in v.get("issues", [])
                    if i.get("severity") == "error"][:3]
            return error(f"多管道验证未通过: {'; '.join(msgs) or '未知'}"), 500
        return success(multi, "多管道已生成并启动")
    mappings = body.get("mappings") or []
    plan = body.get("transformation_plan") or {}
    if not mappings:
        mappings = plan.get("mappings") or []
    config = body.get("config")
    source_type = body.get("source_type") or ""
    target_type = body.get("target_type") or ""
    source_config = body.get("source_config") or {}
    target_config = body.get("target_config") or {}
    source_models = body.get("source_models") or plan.get("source_models") or []
    target_models = body.get("target_models") or plan.get("target_models") or []

    # 1. 自动推导/提取 target_type, target_id, target_config
    target_id = body.get("target_id")
    if not target_id and target_models:
        target_id = target_models[0].get("target_id") or target_models[0].get("id")

    for m in mappings:
        if isinstance(m, dict):
            if m.get("target_type") == "SOAP":
                target_type = "SOAP"
                break
            tbl = m.get("target_table") or ""
            for tg in repository.list_targets():
                if tg.get("type") == "SOAP":
                    for tb in (tg.get("tables") or []):
                        if tb.get("table") == tbl or tb.get("entity_name") == tbl or tb.get("name") == tbl:
                            target_type = "SOAP"
                            if not target_id:
                                target_id = tg.get("id")
                            break

    if not target_type and target_models:
        if any(m.get("type") == "SOAP" for m in target_models if isinstance(m, dict)):
            target_type = "SOAP"

    if target_type == "SOAP" and not target_id:
        for tg in repository.list_targets():
            if tg.get("type") == "SOAP":
                target_id = tg.get("id")
                break

    if not target_type:
        target_type = "DB"

    if target_id:
        tg = repository.get_target(target_id)
        if tg:
            target_type = tg.get("type") or target_type
            # 目标运行参数统一取归一化契约 connection（repository.target_runtime）
            rconn = (tg.get("runtime") or {}).get("connection") \
                or repository.target_runtime(tg)["connection"]
            target_config = {
                "wsdl": target_config.get("wsdl") or rconn.get("wsdl", ""),
                "service": target_config.get("service") or rconn.get("service", "default"),
                "packages": target_config.get("packages") or rconn.get("packages") or {},
                "bo_class": target_config.get("bo_class") or rconn.get("bo_class") or tg.get("bo_class", ""),
                # 远端 SOAP 地址：目标登记时可指定 endpoint；默认指向 Python mock 演示第三方系统
                "endpoint": target_config.get("endpoint") or rconn.get("endpoint") or Config.MOCK_SOAP_URL,
            }

    if target_type == "SOAP" and not target_config.get("bo_class"):
        for tg in repository.list_targets():
            if tg.get("type") == "SOAP":
                rconn = (tg.get("runtime") or {}).get("connection") \
                    or repository.target_runtime(tg)["connection"]
                bo_c = rconn.get("bo_class") or tg.get("bo_class") or ""
                if bo_c:
                    target_config["bo_class"] = bo_c
                    if not target_config.get("wsdl"):
                        target_config["wsdl"] = rconn.get("wsdl", "")
                    if not target_config.get("service"):
                        target_config["service"] = rconn.get("service", "default")
                    break

    # 2. 自动推导/提取 source_type, source_id, source_config
    source_id = body.get("source_id")
    if not source_id and source_models:
        source_id = source_models[0].get("source_id") or source_models[0].get("id")

    # 提取全部涉及的源资产标识（映射/模型）
    mapped_sources = []
    for m in mappings:
        if isinstance(m, dict):
            if m.get("source"):
                mapped_sources.append(m["source"])
            for sa in (m.get("source_assets") or []):
                mapped_sources.append(sa)
            if m.get("source_type") == "SQL" or m.get("asset_type") == "SQL_TABLE":
                source_type = "SQL"
    for sm in source_models:
        if isinstance(sm, dict):
            if sm.get("name"):
                mapped_sources.append(sm["name"])
            if sm.get("id"):
                mapped_sources.append(sm["id"])
            struct = sm.get("structure") or {}
            if struct.get("table"):
                mapped_sources.append(struct.get("table"))
                if struct.get("schema"):
                    mapped_sources.append(f"{struct['schema']}.{struct['table']}")
            if sm.get("type") == "SQL_TABLE" or sm.get("asset_type") == "SQL_TABLE":
                source_type = "SQL"

    all_datasources = repository.list_datasources()
    all_assets = repository.list_assets()

    # 从映射名/资产匹配判断 SQL 数据源
    for src_name in mapped_sources:
        for ds in all_datasources:
            if ds.get("type") == "SQL":
                for a in all_assets:
                    if a.get("source_id") == ds.get("id"):
                        if a.get("name") == src_name or a.get("id") == src_name or a.get("name") in src_name:
                            source_type = "SQL"
                            if not source_id:
                                source_id = ds.get("id")
                            break

    if source_type == "SQL" and not source_id:
        for ds in all_datasources:
            if ds.get("type") == "SQL":
                source_id = ds.get("id")
                break

    if not source_type and all_datasources:
        for ds in all_datasources:
            if ds.get("type") == "SQL":
                source_type = "SQL"
                source_id = ds.get("id")
                break

    if not source_type:
        source_type = "FHIR"

    ds_obj = repository.get_datasource(source_id) if source_id else None
    if ds_obj:
        source_type = ds_obj.get("type") or source_type
        cfg = ds_obj.get("config") or {}
        rconn = (ds_obj.get("runtime") or {}).get("connection") \
            or repository.datasource_runtime(ds_obj)["connection"]
        source_config = {
            "dsn": source_config.get("dsn") or rconn.get("dsn") or "localTarget",
            "query": source_config.get("query") or rconn.get("query") or "",
            "key_field": source_config.get("key_field") or rconn.get("key_field", "ID"),
        }
        # DSN：优先引用数据源已注册的 DSN（连通测试/选表时写回 runtime）；
        # 未注册则按 jdbc_url 幂等创建（避免生成出 'USER' 之类不存在的 DSN）
        from backend.services import jdbc_dsn as _jdbc_dsn
        _registered = _jdbc_dsn.register_for_datasource(ds_obj)
        if _registered:
            source_config["dsn"] = _registered
        source_config["jdbc_url"] = (source_config.get("jdbc_url") or rconn.get("jdbc_url")
                                     or cfg.get("jdbc_url") or "")
        _jdbc_dsn.ensure_for({
            "dsn": source_config.get("dsn"), "jdbc_url": source_config["jdbc_url"],
            "username": rconn.get("username") or cfg.get("username") or "superuser",
            "password": rconn.get("password") or cfg.get("password") or "SYS"})
        # FHIR 源 endpoint/auth 也归一进 source_config（供 generate 后段/验证读取）
        if source_type == "FHIR":
            source_config["endpoint"] = source_config.get("endpoint") or rconn.get("endpoint") or ""
            source_config["username"] = source_config.get("username") or rconn.get("username") or ""
            source_config["password"] = source_config.get("password") or rconn.get("password") or ""

    if source_type == "SQL":
        if not source_config.get("dsn") or source_config.get("dsn").startswith("jdbc:"):
            source_config["dsn"] = "localTarget"

        user_explicit_query = (body.get("source_config") or {}).get("query")
        user_explicit_key = (body.get("source_config") or {}).get("key_field")

        query_built = ""
        key_field_built = ""

        # 精确依据 mapped_sources 匹配 DataAsset 或 DataSource tables
        for ms in mapped_sources:
            # 1. 匹配 DataAsset
            for a in all_assets:
                if a.get("id") == ms or a.get("name") == ms:
                    struct = a.get("structure") or {}
                    sch = struct.get("schema") or "SQLUser"
                    tbl = struct.get("table") or a.get("name")
                    if tbl:
                        query_built = f"SELECT * FROM {sch}.{tbl}" if sch else f"SELECT * FROM {tbl}"
                        cols = struct.get("columns") or a.get("fields") or []
                        if cols:
                            c0 = cols[0]
                            key_field_built = c0.get("name") if isinstance(c0, dict) else str(c0)
                        break
            if query_built:
                break

            # 2. 匹配 DataSource 表
            for ds in all_datasources:
                if ds.get("type") == "SQL":
                    for tb in (ds.get("tables") or []):
                        tbl_name = tb.get("table") or ""
                        schema_tbl = f"{tb.get('schema', '')}.{tbl_name}"
                        if ms == tbl_name or ms == schema_tbl or ms.endswith(f".{tbl_name}") or tbl_name.endswith(f".{ms}"):
                            sch = tb.get("schema") or "SQLUser"
                            query_built = f"SELECT * FROM {sch}.{tbl_name}" if sch else f"SELECT * FROM {tbl_name}"
                            cols = tb.get("columns") or []
                            if cols:
                                c0 = cols[0]
                                key_field_built = c0.get("name") if isinstance(c0, dict) else str(c0)
                            break
                    if query_built:
                        break

        if not query_built and mapped_sources:
            for ms in mapped_sources:
                if not ms.startswith("DS") and not ms.startswith("M") and not ms.startswith("TG"):
                    schema_prefix = "SQLUser." if "." not in ms else ""
                    query_built = f"SELECT * FROM {schema_prefix}{ms}"
                    break

        if user_explicit_query:
            source_config["query"] = user_explicit_query
        elif query_built:
            source_config["query"] = query_built
        elif ds_obj and (ds_obj.get("config") or {}).get("query"):
            source_config["query"] = ds_obj["config"]["query"]
        else:
            source_config["query"] = "SELECT * FROM SQLUser.PatientSource"

        if user_explicit_key:
            source_config["key_field"] = user_explicit_key
        elif key_field_built:
            source_config["key_field"] = key_field_built
        elif ds_obj and (ds_obj.get("config") or {}).get("key_field"):
            source_config["key_field"] = ds_obj["config"]["key_field"]
        else:
            source_config["key_field"] = "ID"
    if not mappings:
        mappings = repository.list_mappings()
    if not mappings:
        return error("缺少 mappings"), 400

    try:
        req = PipelineGenerateRequest(**{**body, "mappings": mappings})
    except Exception as exc:
        return error(f"参数校验失败: {exc}"), 400
    repository.save_mappings([m.model_dump() for m in req.mappings])

    # Agent C1：转换验证-修复（生成前，先保证转换关系合法：列存在/结构完整）
    mappings_effective = [m.model_dump() for m in req.mappings]
    trans_fix = transformation_validator.validate_and_fix_transformation(
        mappings_effective, assets=_c1_assets(source_id),
        target_models=_c1_target_models(mappings_effective))
    # 采纳 C1 的修复进展：即使未完全解决也不丢弃已修正的映射（避免"全有或全无"把
    # 引用标注 transform=reference / 必填常量等修复回退，导致 FHIR 事务校验整体被拒）
    if trans_fix.get("mappings"):
        mappings_effective = trans_fix["mappings"]
    if trans_fix["status"] != "ok":
        logger.warning("转换验证-修复未完全解决（已采纳部分修复）: %s", trans_fix["message"])
    repository.save_mappings(mappings_effective)

    mappings_json = json.dumps(mappings_effective, ensure_ascii=False)
    # FHIR 源配置兜底：统一从数据源运行契约读取（datasource_runtime 归一 endpoint/auth），
    # 保证 FHIRSyncService 能抓取（前端可能取不到字段的历史问题由此根治）
    if not (config or {}).get("endpoint"):
        for _ds in repository.list_datasources():
            if _ds.get("type") == "FHIR":
                _rc = (_ds.get("runtime") or {}).get("connection") \
                    or repository.datasource_runtime(_ds)["connection"]
                _ep = _rc.get("endpoint") or ""
                if _ep:
                    config = {
                        **(config or {}),
                        "endpoint": _ep,
                        "username": (config or {}).get("username") or _rc.get("username") or "superuser",
                        "password": (config or {}).get("password") or _rc.get("password") or "SYS",
                    }
                    break
    config_json = json.dumps(config, ensure_ascii=False) if config else ""

    # Agent B（LLM）：设计数据管道拓扑——AI 决定组件构成与顺序，注册表只补参数。
    # LLM 不可用时不再静默走规则；如确需规则兜底（如无 LLM key 的演示环境）须显式 allow_rule_fallback。
    allow_rule_fallback = bool(body.get("allow_rule_fallback"))
    ai_components: list[dict] | None = None
    p_result: dict | None = None
    design_skill = None
    try:
        # 上下文精准化：源模型/运行契约收敛到本组映射涉及的表（组件构成仍由 Agent 决策）
        _src_tables = _source_tables_from_mappings(mappings_effective)
        _rt_src_scoped, _src_models_scoped = _scope_source_context(
            (ds_obj or {}).get("runtime") if ds_obj else None, source_models, _src_tables)
        p_result = llm_client.recommend_pipeline(
            mappings_effective,
            source_type=source_type, target_type=target_type,
            available_components=AVAILABLE_COMPONENTS,
            source_models=_src_models_scoped,
            target_models=target_models,
            transformation_plan=plan or {"mappings": mappings_effective},
            # 源/目标运行契约（归一 runtime）注入，驱动 AI 基于轮询/投递能力决策（密码由 llm_client 脱敏）
            source_runtime=_rt_src_scoped,
            target_runtime=(tg.get("runtime") if locals().get("tg") else None),
            # 已登记管道事实（同一 (源,目标) → 更新既有管道而非新增；决策仍归 LLM）
            existing_pipelines=pipeline_instances.existing_pipelines_brief())
        pipeline = p_result.get("pipeline")
        design_skill = p_result.get("design_skill") if isinstance(p_result, dict) else None
        if pipeline and pipeline.get("components"):
            ai_components = pipeline["components"]
            logger.info("Agent B 数据管道设计 Agent 选 Skill=%s（AI 决策 %d 组件）: %s",
                        design_skill, len(ai_components),
                        [c.get("type") for c in ai_components])
        else:
            raise llm_client.AgentError("Agent B 未返回任何组件")
    except Exception as exc:
        if not allow_rule_fallback:
            logger.error("Agent B 数据管道 AI 生成失败（不静默回退规则）: %s", exc)
            return error(f"Agent B 数据管道 AI 生成失败: {exc}"
                         "（如需以类型注册表规则兜底，请显式传 allow_rule_fallback=true）"), 500
        logger.warning("Agent B 调用失败，已显式允许规则兜底: %s", exc)

    # 校正 mapping.target_type：前端确认/plan 常缺省存成 "DB"（MappingItem 默认值），
    # 会导致 TransformProcess 对 SOAP 目标走错 DB 路由。统一按推导的最终 target_type 回写
    # （SOAP 目标→SOAPOp，DB 目标→SQLOp），并持久化到 ^demo.Mapping。
    for _m in mappings_effective or []:
        if isinstance(_m, dict) and _m.get("id"):
            _m["target_type"] = target_type
            repository.set_json("^demo.Mapping", _m["id"], _m)

    # —— sql2fhir-patient-tx 分发（Skill 布局 executor + Agent 生成 BP，无平台预置 BP）——
    # 触发条件：目标=FHIR、Agent B 已选 design_skill=sql2fhir-patient-tx（AI 决策），
    # 且源为 SQL 并映射覆盖患者主表。布局与 BP 由 Skill/Agent 链路完成，平台只做参数化与准入。
    sql2fhir_flow = False
    if (target_type == "FHIR" and design_skill == "sql2fhir-patient-tx"
            and ai_components is not None):
        from backend.services import sql2fhir_executor as _sfx
        try:
            maps_layout, meta = _resolve_sql_source_tables(mappings_effective, source_id)
            layout = _sfx.derive_sql2fhir_layout(maps_layout, meta)
            layout = _sfx.enrich_layout_with_mappings(layout, maps_layout)
        except Exception as _lexc:  # noqa: BLE001
            logger.error("sql2fhir 布局推导失败（Skill executor）: %s", _lexc, exc_info=True)
            return error(f"sql2fhir 布局推导失败（Skill executor）: {_lexc}"), 500
        _save_sql2fhir_layout(layout)
        _tg_rt = (locals().get("tg") or {}).get("runtime") if locals().get("tg") else None
        _bp_ok, _bp_msg = _ensure_sql2fhir_bp(
            p_result, mappings_effective, source_type, target_type,
            AVAILABLE_COMPONENTS,
            (ds_obj or {}).get("runtime") if ds_obj else None, _tg_rt)
        if not _bp_ok:
            logger.error("Agent 生成 BP 失败（sql2fhir-patient-tx）: %s", _bp_msg)
            return error(f"Agent 生成 BP 失败（sql2fhir-patient-tx）: {_bp_msg}"), 500
        ai_components = _sfx.build_sql2fhir_components(layout, source_config, target_config)
        sql2fhir_flow = True
        logger.info("sql2fhir-patient-tx 布局+Agent BP 就绪（%d 组件）", len(ai_components))

    # 依据 Agent B（LLM）组件构建拓扑；ai_components=None（规则兜底）时才用纯注册表规则
    if sql2fhir_flow:
        # sql2fhir 拓扑由 Skill executor 按布局生成（含 Agent 编译的 BP），不再走注册表参数化重建
        topology = {"production": "demo.DataflowProduction", "components": ai_components,
                    "design_skill": "sql2fhir-patient-tx", "ai_supplemented": []}
    else:
        topology = build_pipeline_topology(
            mappings_effective,
            source_type=source_type, target_type=target_type,
            suggested_types=None,
            source_config=source_config, target_config=target_config,
            ai_components=ai_components)
    # 管道类别（= Agent B 选定的设计 Skill）：渲染为 Ens 业务主机 Category（分组/启停/LLM 上下文的事实）
    _category = pipeline_instances.category_of(
        topology.get("design_skill") or design_skill, source_type, target_type)
    _stamp_categories(topology, _category)
    # 许可调度：放不下的管道分组标 enabled=false（组件照旧生成、初始停用，UI 一键切换）
    _plan = _license_plan(topology)
    _susp = _mark_suspended_components(topology, _plan)
    if _susp:
        logger.warning("许可调度：以下组件生成后处于停用状态 %s", _susp)
    _save_pipeline_topology(topology)
    _save_fhir_runtime_config(topology, target_config)
    # 以及管道目标类型（TransformProcess 路由权威依据，防止 mapping.target_type 缺省 DB 误路由）
    # 转换 BP 的**自身配置**（单管道只有一条 → BP 名固定 TransformProcess）：
    # demo.TransformProcess.OnRequest 首选 ^demo.Config("bp", ..%ConfigName)，
    # 不再依赖全局单值键（后者在多管道下会被后来者覆盖）；同时写：
    #  - bp_target[源BS] = BP 名（供代码内显式投递的源 BS 读取，如 demo.FHIRService）
    #  - pipe[源BS] / pipeline.* （兼容历史路径与兜底）
    if mappings_effective:
        import iris
        conn = iris_connector.get_connection()
        try:
            native = iris.createIRIS(conn)
            _m0 = mappings_effective[0]
            _pipe = {"mapping": _m0.get("id", ""), "target_type": target_type}
            if target_type == "SOAP":
                _pipe["service"] = (target_config or {}).get("service") or "default"
            else:
                _pipe["table"] = _m0.get("target_table") or ""
            _pipe_json = json.dumps(_pipe, ensure_ascii=False)
            native.set(_pipe_json, "^demo.Config", "bp", "TransformProcess")
            for _bn in _source_bs_names(topology):
                native.set("TransformProcess", "^demo.Config", "bp_target", _bn)
                native.set(_pipe_json, "^demo.Config", "pipe", _bn)
            native.set(_m0.get("id", ""),
                       "^demo.Config", "pipeline", "active_mapping")
            native.set(target_type,
                       "^demo.Config", "pipeline", "target_type")
        finally:
            iris_connector.reset_connections()
    logger.info("已构建完整管道拓扑: %s", [c.get("type") for c in topology["components"]])

    # 许可预算：让不属于本次管道的旧管道业务主机让出许可单元（社区版仅 8 个）；
    # 本次拓扑内部由 apply_license_budget(groups=...) 做容量调度——放不下的分组在上面已标
    # enabled=false（生成但停用），因此不再出现"超容量即失败"，用户可在 UI 一键切换
    license_budget = pipeline_validator.apply_license_budget(
        keep_categories=_pipeline_categories(_category),
        groups=_topology_groups(topology))

    # 生成前连通性检查（源/目标运行契约门禁：参数/可达性错误在生成前拦截）
    try:
        _conn_rt_src = [ds_obj.get("runtime")] if ds_obj else []
        _conn_rt_tgt = [tg.get("runtime")] if (tg := locals().get("tg")) else []
        _conn_check = pipeline_validator.check_connection(sources=_conn_rt_src, targets=_conn_rt_tgt)
        if not _conn_check.get("ok"):
            _msg = "; ".join(i.get("message", "") for i in _conn_check.get("issues", [])[:3])
            return error(f"源/目标连通性检查未通过: {_msg}"), 500
    except Exception as _exc:  # noqa: BLE001 - 连通检查失败不阻断生成主流程
        logger.warning("连通性检查异常（跳过）: %s", _exc)

    try:
        result = iris_connector.class_method_value(
            "demo.PipelineGenerator", "Generate", mappings_json, config_json)
    except Exception as exc:
        logger.error("生成管道失败: %s", exc)
        return error(f"生成管道失败: {exc}"), 500

    # Agent C2：管道验证（拓扑/编译/启动/消息流转 + 目标落地效果 + 运行期错误分类）
    validation = pipeline_validator.run_pipeline_validation(
        topology, source_type, target_type)

    # 运行期错误回喂（有界 1 次）：FHIR 结构类错误 → C1 修映射后重生成；
    # BP 代码类错误 → 记录 method_updates 修复建议（自动 patch 由 Skill 通道执行）。
    if result != "OK" or not validation.get("ok"):
        try:
            _rt = (validation.get("results") or {}).get("runtime") or {}
            _kinds = _rt.get("kinds") or []
            if "fhir_schema" in _kinds:
                logger.info("运行期 FHIR 结构错误 → 回喂 C1 修复映射后重生成")
                _fix2 = transformation_validator.validate_and_fix_transformation(
                    mappings_effective, assets=_c1_assets(source_id),
                    target_models=_c1_target_models(mappings_effective))
                if _fix2.get("status") == "ok" and _fix2.get("mappings"):
                    for _m in _fix2["mappings"]:
                        if isinstance(_m, dict) and _m.get("id"):
                            repository.set_json("^demo.Mapping", _m["id"], _m)
                    mappings_effective = _fix2["mappings"]
                    result = iris_connector.class_method_value(
                        "demo.PipelineGenerator", "Generate",
                        json.dumps(mappings_effective, ensure_ascii=False), config_json)
                    validation = pipeline_validator.run_pipeline_validation(
                        topology, source_type, target_type)
                    logger.info("运行期回喂重生成结果: result=%s 验证ok=%s",
                                result, validation.get("ok"))
            elif "bp_code" in _kinds:
                from backend.services import generated_bp as _gbp
                logger.info("运行期 BP 代码错误 → method_updates 增量修复")
                _reps = _gbp.repair_from_runtime_errors(
                    mappings=mappings_effective, source_type=source_type,
                    target_type=target_type, available_components=AVAILABLE_COMPONENTS,
                    errors_text="\n".join(i.get("message", "") for i in (_rt.get("issues") or [])),
                    source_runtime=(ds_obj or {}).get("runtime") if ds_obj else None,
                    target_runtime=(locals().get("tg") or {}).get("runtime"))
                if _reps.get("ok"):
                    logger.info("BP 方法修复完成: %s", _reps.get("applied"))
                    try:
                        iris_connector.class_method_value("Ens.Director", "StopProduction", 10)
                        iris_connector.class_method_value("Ens.Director", "StartProduction",
                                                          "demo.DataflowProduction")
                    except Exception as _se:  # noqa: BLE001
                        logger.warning("修复后重启 Production 失败（不阻断）: %s", _se)
                    result = iris_connector.class_method_value(
                        "demo.PipelineGenerator", "Generate",
                        json.dumps(mappings_effective, ensure_ascii=False), config_json)
                    validation = pipeline_validator.run_pipeline_validation(
                        topology, source_type, target_type)
                else:
                    logger.warning("BP 方法修复未通过: %s", _reps.get("message"))
        except Exception as _re:  # noqa: BLE001 - 回喂失败不阻断主流程
            logger.warning("运行期错误回喂失败（不影响主流程）: %s", _re)

    # 管道验证-修复闭环：生成失败或验证不通过时，由 Agent C2 判断并分层修复
    fix_result: dict | None = None
    if result != "OK" or not validation["ok"]:
        logger.warning("生成/管道验证未通过（生成=%s 验证错误=%d），进入管道验证-修复闭环",
                       result, validation["error_count"])

        def _generate_with_topology(topo, mp):
            """按给定拓扑 + 映射重新生成（供管道验证-修复 Agent 的 generate_fn）。"""
            _save_pipeline_topology(topo)
            mp_json = json.dumps(mp, ensure_ascii=False)
            return iris_connector.class_method_value(
                "demo.PipelineGenerator", "Generate", mp_json, config_json)

        fix_result = validate_agent.validate_and_fix_pipeline(
            mappings_effective, topology,
            generate_fn=_generate_with_topology,
            source_type=source_type, target_type=target_type)
        if fix_result["status"] != "ok":
            return error(
                f"生成管道失败，验证-修复闭环未解决: {result}（{fix_result['message']}）"), 500
        result = "OK"
        validation = fix_result["report"]
        topology = fix_result["topology"]
        # C2 修复可能改写了拓扑（规则补齐/重建）→ 重新打类别，保证 Category 与最终拓扑一致
        _stamp_categories(topology, _category)
        logger.info("管道验证-修复闭环完成: 状态=%s 轮次=%d",
                    fix_result["status"], len(fix_result["rounds"]))

    # 管道实体登记（受管理持久对象）：同一 (source_id, target_id, design_skill) 更新，不新增
    pipeline_rec: dict | None = None
    pipeline_error = ""
    if result == "OK":
        try:
            _src_bs = _source_bs_names(topology)
            pipeline_rec = pipeline_instances.upsert_from_generation(
                source_id=source_id, target_id=target_id,
                source_type=source_type, target_type=target_type,
                design_skill=topology.get("design_skill") or design_skill,
                category_hint=_category,
                mapping_ids=[m.get("id") for m in mappings_effective if m.get("id")],
                components=topology.get("components"),
                routes={"source_bs": _src_bs[0] if _src_bs else "",
                        "source_bs_names": _src_bs},
                ai={"driven": ai_components is not None,
                    "rule_fallback": ai_components is None,
                    "supplemented": (topology or {}).get("ai_supplemented", [])},
                validation=validation)
            pipeline_instances.reconcile_states()
        except Exception as _pie:  # noqa: BLE001 - 登记失败不改变"生成成功"事实，但必须显式暴露
            pipeline_error = str(_pie)
            logger.error("管道实体登记失败（生成已成功，实体未登记）: %s", _pie)

    # 术语判码缓存：mapping 含 term_map 时，按当前源表编码值预判定（Skill 执行；结果显式回传）
    term_cache_report: dict = {}
    if result == "OK":
        try:
            term_cache_report = _refresh_term_cache(mappings_effective, topology, validation)
        except Exception as _tc:  # noqa: BLE001 - 刷新异常也显式报告
            logger.error("生成后判码缓存刷新异常: %s", _tc)
            term_cache_report = {"ok": False, "error": str(_tc),
                                 "note": "判码缓存刷新异常（term_map 决策将无法落地）"}

    return success({
        "result": result,
        "production": "demo.DataflowProduction",
        "validation": validation,
        # 许可预算：本次生成让旧管道组件让出的许可单元（社区版仅 8 个许可单元）
        "license_budget": license_budget,
        # 术语判码缓存（term_map 决策的运行期前提）：ok=false 表示 AI 决策未能落地
        "term_cache": term_cache_report,
        # AI 驱动信息：Agent B（LLM）决定组件构成；supplemented 为注册表保底补齐（校验性，非替代）
        "ai": {
            "driven": ai_components is not None,
            "rule_fallback": ai_components is None,
            "components": [c.get("type") for c in (ai_components or [])],
            "supplemented": (topology or {}).get("ai_supplemented", []),
            # 红线审计：C2 修复中规则是否改写了 AI 决策的组件构成（True 则结果非纯 AI）
            "c2_rule_rebuilt": bool((fix_result or {}).get("rule_applied")),
        },
        # 管道实体（受管理对象）：本次生成新建/更新的管道；同身份重复生成只更新不新增
        "pipeline": pipeline_rec,
        "pipelines": [pipeline_rec] if pipeline_rec else [],
        "pipeline_error": pipeline_error,
    }, "数据管道已生成并启动")



@pipelines_bp.post("/run")
def run():
    """触发一次转换（FHIRService 定时拉取中，本接口确认状态）。"""
    result = iris_connector.class_method_value("demo.PipelineQuery", "TriggerFetch")
    return success({"result": result})


@pipelines_bp.get("/status")
def status():
    """管道运行状态。"""
    running = iris_connector.class_method_value("demo.PipelineQuery", "ProductionStatus")
    return success({"running": bool(running), "production": "demo.DataflowProduction"})


@pipelines_bp.get("/items")
def items():
    """Production 组件清单 + 许可容量 + 按管道类别分组（供「组件启停」面板与许可预算可视化）。

    背景：IRIS 社区版 `KeyLicenseUnits = 8`，每个 Ens 业务主机（BS/BP/BO）常驻占 1 个
    许可单元。组件的 Category 即所属数据管道类别（sql2fhir-patient-tx / sql2soap / shared…），
    categories 字段给出每个类别的组件数与已启用数，用于判断哪条管道正在占用许可。
    """
    rows = pipeline_validator.production_items()
    buckets: dict[str, dict] = {}
    for r in rows:
        cat = str(r.get("category") or "")
        if not cat:
            continue
        grp = buckets.setdefault(cat, {"category": cat, "items": [], "enabled_count": 0})
        grp["items"].append(r.get("name"))
        if int(r.get("enabled") or 0) == 1:
            grp["enabled_count"] += 1
    return success({
        "items": rows,
        "units": pipeline_validator.license_units(),
        "enabled_count": len([r for r in rows if int(r.get("enabled") or 0) == 1]),
        "categories": sorted(buckets.values(), key=lambda g: g["category"]),
    })


@pipelines_bp.post("/items/toggle")
def toggle_items():
    """启用/禁用 Production 组件（等价于手工切换管道占用许可）。

    body: {"names": ["SQLService_Patient"], "enabled": true}
      或 {"name": "SQLService_Patient", "enabled": false}
    注意：启用组件会常驻占用 1 个许可单元；启用过多会导致后端无法连接 IRIS（接口 500）。
    生成流程会在启动前自动做许可预算（让非本次管道组件让路）。
    """
    body = request.get_json(silent=True) or {}
    names = body.get("names") or ([body["name"]] if body.get("name") else [])
    if not names:
        return error("请指定要启停的组件名（name 或 names）"), 400
    enabled = bool(body.get("enabled"))
    res = pipeline_validator.set_items_enabled(names, enabled)
    if not res.get("ok"):
        return error(f"组件启停失败: {res.get('message') or res.get('result')}"), 500
    return success(res, "组件已启用" if enabled else "组件已禁用")


@pipelines_bp.get("/instances")
def pipeline_instances_list():
    """数据管道清单（受管理持久实体）+ 按管道类别分组 + 许可占用。

    管道实体 = 「源 + 目标 + 设计 Skill」的稳定对象：组件 Category 即管道类别。
    status 按 Production 实际 Enabled 回写（active/suspended），不是生成时的历史快照。
    """
    prod_items = pipeline_validator.production_items()
    recs = pipeline_instances.reconcile_states(production_items=prod_items)
    return success({
        "items": recs,
        "groups": pipeline_instances.group_by_category(recs, prod_items),
        "units": pipeline_validator.license_units(),
        "enabled_count": len([r for r in prod_items if int(r.get("enabled") or 0) == 1]),
        "active_count": len([r for r in recs if r.get("status") == "active"]),
    })


@pipelines_bp.get("/instances/<pid>")
def pipeline_instance_detail(pid):
    """单条数据管道详情（含组件清单、路由、AI 决策与最近验证结果）。"""
    rec = pipeline_instances.get_instance(pid)
    if not rec:
        return error(f"管道实例不存在: {pid}"), 404
    return success(rec)


@pipelines_bp.delete("/instances/<pid>")
def pipeline_instance_delete(pid):
    """删除管道实体记录（仅删实体，不改动 Production 组件与其启停状态）。"""
    if not pipeline_instances.get_instance(pid):
        return error(f"管道实例不存在: {pid}"), 404
    pipeline_instances.delete_instance(pid)
    return success({"id": pid}, "管道实体已删除（Production 组件未改动）")


@pipelines_bp.post("/instances/<pid>/enable")
def pipeline_instance_enable(pid):
    """启用一条数据管道（启用其业务主机组件，占用许可单元）。

    注意许可上限：启用后若「已启用组件数 + 1（后端连接）」超过 license_units()，
    后端连接会被 IRIS 拒绝（接口 500）——此时请先停掉另一条管道。
    """
    res = pipeline_instances.set_enabled(pid, True)
    if not res.get("ok"):
        return error(f"启用管道失败: {res.get('message')}"), 500
    return success(res, "管道已启用")


@pipelines_bp.post("/instances/<pid>/disable")
def pipeline_instance_disable(pid):
    """停用一条数据管道（停其业务主机组件，释放许可单元；无其它活动管道时连共享组件一起停）。"""
    res = pipeline_instances.set_enabled(pid, False)
    if not res.get("ok"):
        return error(f"停用管道失败: {res.get('message')}"), 500
    return success(res, "管道已停用（许可单元已释放）")


@pipelines_bp.post("/instances/sync")
def pipeline_instance_sync():
    """从 Production（组件 Category）+ 路由表回填管道实体（历史环境迁移/修复）。

    适用：升级前生成过的 Production 只有组件没有管道实体；或手工改过组件类别需要归位。
    """
    res = pipeline_instances.sync_from_production()
    return success({"created": res.get("created"), "updated": res.get("updated"),
                    "categories": res.get("categories"),
                    "instances": res.get("instances")}, "管道实体已同步")


@pipelines_bp.get("/logs")
def logs():
    """消息流转日志（Ens.MessageHeader 真实消息历史）。"""
    count = request.args.get("count", 50, type=int)
    data = iris_connector.class_method_value("demo.PipelineQuery", "GetLogs", count)
    try:
        items = json.loads(data or "[]")
    except json.JSONDecodeError:
        items = []
    return success({"items": items})


@pipelines_bp.post("/validation-issues")
def save_validation_issue_route():
    """沉淀一条验证-修复经验到 ^demo.ValidationIssue（外部工具/审计用）。

    请求体: {"pattern": "...", "resolution": "...", "source": "..."}
    """
    body = request.get_json(silent=True) or {}
    pattern = (body.get("pattern") or "").strip()
    resolution = (body.get("resolution") or "").strip()
    if not pattern or not resolution:
        return error("缺少 pattern/resolution"), 400
    result = pipeline_validator.save_validation_issue(
        pattern, resolution, body.get("source") or "api")
    if not result.get("ok"):
        return error(result.get("message") or "沉淀失败"), 500
    return success({"pattern": pattern[:60]}, "经验已沉淀")


@pipelines_bp.post("/validation-issues/polish")
def polish_validation_issues_route():
    """LLM 研读润色 + 去重验证经验（^demo.ValidationIssue → 结构化知识）。

    供宿主导出到 Obsidian 知识库使用；LLM 失败返回明确错误（不静默回退原文）。
    """
    body = request.get_json(silent=True) or {}
    limit = min(max(int(body.get("limit") or 500), 1), 1000)
    items = pipeline_validator.load_validation_issues(limit)
    try:
        polished = llm_client.polish_validation_issues(items)
    except llm_client.AgentError as exc:
        return error(f"知识润色 Agent（LLM）失败: {exc}"), 500
    except Exception as exc:  # noqa: BLE001
        logger.error("知识润色失败: %s", exc)
        return error(f"知识润色失败: {exc}"), 500
    return success({"items": polished, "count": len(polished),
                    "raw_count": len(items)}, "知识润色完成")


@pipelines_bp.get("/validation-issues")
def list_validation_issues():
    """验证-修复 Agent 积累的经验（^demo.ValidationIssue）。

    供审计/导出知识库用（外部脚本可据此转为 IRIS-Dev-Vault 的 04-Pitfalls 笔记）。
    """
    limit = request.args.get("limit", 100, type=int)
    limit = min(max(limit, 1), 500)
    items = pipeline_validator.load_validation_issues(limit)
    return success({"items": items, "count": len(items)})


def _mock_patients(count: int) -> list[dict]:
    """生成 count 条模拟 Patient（lastUpdated > 增量游标，触发增量同步）。"""
    families = ["张", "李", "王", "刘", "陈", "杨", "赵", "黄", "周", "吴"]
    givens = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "娟", "涛", "明", "超"]
    cities = ["北京", "上海", "广州", "深圳", "成都", "杭州"]
    now_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    patients = []
    stamp = int(time.time() * 1000) % 100000
    for i in range(count):
        pid = f"M{stamp}{i}"
        city = random.choice(cities)
        patients.append({
            "resourceType": "Patient",
            "id": pid,
            "meta": {"lastUpdated": now_ts},
            "name": [{"family": random.choice(families), "given": [random.choice(givens)]}],
            "gender": "male" if i % 2 == 0 else "female",
            "birthDate": f"{random.randint(1970, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
            "telecom": [{"system": "phone", "value": f"13{random.randint(0, 9)}{random.randint(10000000, 99999999)}"}],
            "address": [{"line": [f"{city}演示街道{random.randint(1, 99)}号"], "city": city}],
        })
    return patients


@pipelines_bp.post("/generate-mock")
def generate_mock():
    """演示：按需生成模拟 Patient 数据写入 FHIR（lastUpdated > 增量游标）。"""
    count = request.args.get("count", 3, type=int)
    count = min(max(count, 1), 10)
    patients = _mock_patients(count)
    try:
        from backend.services import fhir_client
        result = fhir_client.submit_bundle(
            FHIRConfig.BASE_URL, FHIRConfig.USERNAME, FHIRConfig.PASSWORD, patients)
    except Exception as exc:
        logger.error("生成模拟数据失败: %s", exc)
        return error(f"生成模拟数据失败: {exc}"), 500
    ok = sum(1 for e in result.get("entry", [])
             if e.get("response", {}).get("status", "").startswith("2"))
    ids = [p["id"] for p in patients]
    return success({"count": ok, "ids": ids},
                   f"已生成 {ok} 条模拟数据，增量同步将自动抓取并转换")


@pipelines_bp.get("/mappings")
def pipeline_mappings():
    """已生效的转换关系（Production 正在执行的）。"""
    data = iris_connector.class_method_value("demo.PipelineQuery", "GetMappings")
    try:
        items = json.loads(data or "[]")
    except json.JSONDecodeError:
        items = []
    return success({"items": items})


def _viewable_tables() -> list[str]:
    """当前可查看数据表（动态，由演示过程登记的数据源/目标决定，非写死）：

    - 已登记目标表/实体（_target_table_rows：DB 目标表 + SOAP 目标实体）
    - SQL 数据源下已选定的源资产表（便于查看源数据）
    仅保留 SQLUser schema 中物理存在的表（过滤 SOAP 非落库实体如 AddPatientResult）。
    按演示进度变化：未登记的源/目标不会出现在列表。
    """
    from backend.routes.targets import _target_table_rows

    tables = {tb.get("table") or "" for tb in _target_table_rows()}
    ds_map = {d.get("id"): d for d in repository.list_datasources()}
    for a in repository.list_assets():
        ds = ds_map.get(a.get("source_id"))
        if ds and ds.get("type") == "SQL":
            nm = a.get("name") or (a.get("structure") or {}).get("table") or ""
            if nm:
                tables.add(str(nm))
    rows = iris_connector.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='SQLUser'")
    exist = {r[0] for r in rows}
    return sorted(t for t in tables if t and t in exist)


@pipelines_bp.get("/view-tables")
def view_tables():
    """可查看数据表列表（动态，见 _viewable_tables），供前端目标数据下拉使用。"""
    items = [{"table": t} for t in _viewable_tables()]
    return success({"items": items})


@pipelines_bp.get("/target-data")
def target_data():
    """目标表落库结果。"""
    table = request.args.get("table", "Patient")
    limit = request.args.get("limit", 50, type=int)
    from backend.routes.targets import _target_table_rows
    allowed = set(_viewable_tables())
    if table not in allowed:
        return error(f"目标表不存在或未注册: {table}"), 404
    data = iris_connector.class_method_value("demo.PipelineQuery", "GetTargetData", table, limit)
    try:
        items = json.loads(data or "[]")
    except json.JSONDecodeError:
        items = []
    return success({"items": items, "count": len(items)})
