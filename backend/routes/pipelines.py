"""数据管道 API：生成、触发、监控。"""

import json
import logging
import random
import time
from datetime import datetime, timezone

from flask import Blueprint, request

from backend.config import Config, FHIRConfig
from backend.schemas.models import PipelineGenerateRequest
from backend.services import (iris_connector, llm_client, pipeline_validator,
                              repository, transformation_validator,
                              type_registry, validate_agent, wsdl_importer)
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
        conn.close()


def _get_table_columns(table: str) -> list[str]:
    """查询目标表列名（information_schema，按 ordinal_position 排序）。"""
    rows = iris_connector.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='SQLUser' AND table_name=? ORDER BY ordinal_position",
        [table])
    return [r[0] for r in rows]


def build_pipeline_topology(mappings: list[dict], source_type: str = "FHIR",
                            target_type: str = "DB",
                            suggested_types: list[str] | None = None,
                            source_config: dict | None = None,
                            target_config: dict | None = None,
                            ai_components: list[dict] | None = None) -> dict:
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
    """
    if ai_components:
        return _build_from_ai_components(
            mappings, source_type=source_type, target_type=target_type,
            ai_components=ai_components,
            source_config=source_config, target_config=target_config)

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
                    _s("Host", "TargetConfigNames", "TransformProcess"),
                ],
            })
        else:
            components.append(_from_template(c, c["type"]))

    # 2. 转换组件（TransformProcess）
    for c in type_registry.get_common_components():
        if c["type"] == "TransformProcess":
            components.append(_from_template(c, c["type"]))

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
                              target_config: dict | None = None) -> dict:
    """Agent B（LLM）驱动拓扑构建：AI 决定组件构成与顺序，注册表只补全参数。

    - 每个 AI 组件的 type 必须来自可用枚举（源/目标/通用模板全集），按 AI 的
      name/table 生成；className 与连接参数（DSN/Query/表 UPSERT 列等）由注册表注入；
    - AI 遗漏确属必需的组件（源服务全集/TransformProcess/目标操作/JavaGateway）时
      按模板补齐保底，补齐清单写入 topology["ai_supplemented"]（Python 不再自行
      决定整体构成，仅做完整性校验与参数化）。
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
                    _s("Host", "TargetConfigNames", "TransformProcess"),
                ],
            }
        if ttype == "TransformProcess":
            return _from_template(tpl, "TransformProcess")
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
    add_if_missing("TransformProcess")
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
        add_if_missing("SOAPOperation")
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
    """按「多管道组」构建合并拓扑（单 Production 内多套 BS/OP + 共享 TransformProcess/JavaGateway）。

    参数:
        pipelines: 管道组列表，每组含 source_type/source_config/target_type/target_config/mappings，
                   由 build_pipeline_topology 逐组生成源与目标组件后合并去重。

    返回: {"production": "demo.DataflowProduction", "components": [...]}
    """
    components: list[dict] = []
    for g in pipelines:
        topo = build_pipeline_topology(
            g.get("mappings") or [],
            source_type=g.get("source_type") or "FHIR",
            target_type=g.get("target_type") or "DB",
            suggested_types=g.get("suggested_types"),
            source_config=g.get("source_config") or {},
            target_config=g.get("target_config") or {},
            ai_components=g.get("ai_components"))
        for c in topo.get("components", []):
            if c.get("type") in ("TransformProcess", "JavaGateway"):
                continue  # 共享组件合并后统一追加一个
            components.append(c)
    components = _dedupe_components(components)
    # 共享组件各 1 个：TransformProcess（多源路由 BP）+ JavaGateway（JDBC）
    for t in type_registry.get_common_components():
        if t["type"] in ("TransformProcess", "JavaGateway"):
            components.append(_common_component(t))
    return {"production": "demo.DataflowProduction", "components": components}



def _generate_multi_pipelines(pipelines: list[dict]) -> dict:
    """多管道生成：单 Production 内多套 BS/BP/BO（阶段1，简化验证闭环）。

    每组 pipeline（body.pipelines 元素）:
      {source_type?, source_id?, source_config?, target_type?, target_id?, target_config?, mappings:[...]}

    流程：逐组补齐源/目标配置 → build_multi_pipeline_topology 合并拓扑 →
          写路由表 ^demo.Config("pipe", <源BS名>) → PipelineGenerator 渲染启动。
    返回 success data（result/validation）。
    """
    import iris

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

            # —— 源配置补齐（source_id → 数据源）——
            ds = repository.get_datasource(g.get("source_id")) if g.get("source_id") else None
            if ds:
                src_type = ds.get("type") or src_type
                dcfg = ds.get("config") or {}
                # 运行参数统一取归一化契约 connection（repository.datasource_runtime）
                rconn = (ds.get("runtime") or {}).get("connection") \
                    or repository.datasource_runtime(ds)["connection"]
                src_cfg.setdefault("dsn", rconn.get("dsn") or dcfg.get("dsn") or "localTarget")
                src_cfg.setdefault("query", rconn.get("query") or dcfg.get("query") or "")
                src_cfg.setdefault("key_field", rconn.get("key_field") or dcfg.get("key_field") or "ID")
                src_cfg.setdefault("endpoint", rconn.get("endpoint") or "")
                src_cfg.setdefault("username", rconn.get("username") or "")
                src_cfg.setdefault("password", rconn.get("password") or "")
            if src_type == "SQL":
                if (src_cfg.get("dsn") or "").startswith("jdbc:"):
                    src_cfg["dsn"] = "localTarget"
                if not src_cfg.get("query"):
                    src_cfg["query"] = "SELECT * FROM SQLUser.PatientSource"
                    for _m in g.get("mappings") or []:
                        _s = (_m.get("source") or "").split(".")[0] or _m.get("source")
                        if _s and not _s.startswith(("DS", "M", "TG", "concat")):
                            src_cfg["query"] = f"SELECT * FROM SQLUser.{_s}"
                            break
            if not src_type:
                src_type = "SQL" if src_cfg.get("dsn") else "FHIR"

            # —— 目标配置补齐（target_id → 目标）——
            tg = repository.get_target(g.get("target_id")) if g.get("target_id") else None
            if tg:
                tgt_type = tg.get("type") or tgt_type
                tconn = tg.get("connection") or {}
                if (tg.get("type") or "") == "SOAP":
                    tgt_cfg.setdefault("wsdl", tconn.get("wsdl") or "")
                    tgt_cfg.setdefault("service", tconn.get("service") or "default")
                    tgt_cfg.setdefault("packages", tconn.get("packages") or {})
                    tgt_cfg.setdefault("bo_class", tconn.get("bo_class") or tconn.get("boClass") or tg.get("bo_class") or "")
                    tgt_cfg.setdefault("endpoint", tconn.get("endpoint") or Config.MOCK_SOAP_URL)
            if not tgt_type:
                for _m in g.get("mappings") or []:
                    if _m.get("target_type") == "SOAP":
                        tgt_type = "SOAP"
                        break
            if not tgt_type:
                tgt_type = "DB"
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
                _fix = transformation_validator.validate_and_fix_transformation(_g_maps)
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

        # —— Agent B（LLM）：逐组设计管道拓扑（AI 决定各组件的构成与顺序）——
        for _g in groups:
            try:
                _p = llm_client.recommend_pipeline(
                    _g.get("mappings") or [],
                    source_type=_g["source_type"], target_type=_g["target_type"],
                    available_components=AVAILABLE_COMPONENTS,
                    transformation_plan={"mappings": _g.get("mappings") or []},
                    source_runtime=_g.pop("_rt_src", None),
                    target_runtime=_g.pop("_rt_tgt", None))
                _comps = ((_p.get("pipeline") or {}).get("components") or [])
                if not _comps:
                    raise ValueError("Agent B 未返回任何组件")
                _g["ai_components"] = _comps
                logger.info("多管道 Agent B 设计 %s→%s 完成（%d 组件）：%s",
                            _g["source_type"], _g["target_type"], len(_comps),
                            [c.get("type") for c in _comps])
            except Exception as _be:  # noqa: BLE001 - Agent B 失败即报错，不静默回退规则
                raise ValueError(
                    f"Agent B 数据管道 AI 生成失败（组 {_g['source_type']}→{_g['target_type']}）：{_be}"
                ) from _be

        topology = build_multi_pipeline_topology(groups)
        _save_pipeline_topology(topology)
        for _g in groups:
            src_bn = "SQLService" if _g["source_type"] == "SQL" else "FHIRService"
            _tt = _g["target_type"]
            _m0 = _g["mappings"][0]
            pipe = {"mapping": _m0["id"], "target_type": _tt}
            if _tt == "SOAP":
                pipe["service"] = (_g["target_config"] or {}).get("service") or "default"
            else:
                pipe["table"] = _m0.get("target_table") or ""
            native.set(json.dumps(pipe, ensure_ascii=False), "^demo.Config", "pipe", src_bn)
            # 兼容单值键（单管道 / 旧逻辑兜底用最后一组）
            native.set(_m0["id"], "^demo.Config", "pipeline", "active_mapping")
            native.set(_tt, "^demo.Config", "pipeline", "target_type")

        logger.info("多管道拓扑: %s", [c.get("name") for c in topology["components"]])
        mappings_json = json.dumps(all_mappings, ensure_ascii=False)
        config_json = json.dumps(fhir_cfg, ensure_ascii=False) if fhir_cfg else ""
        _src_types = sorted({g["source_type"] for g in groups})
        _tgt_types = sorted({g["target_type"] for g in groups})

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
        return {"result": result, "production": "demo.DataflowProduction",
                "validation": validation,
                # AI 驱动信息：每组管道均由 Agent B（LLM）设计拓扑
                "ai": {
                    "driven": True,
                    "groups": [{
                        "source_type": _g["source_type"], "target_type": _g["target_type"],
                        "components": [c.get("type") for c in (_g.get("ai_components") or [])],
                    } for _g in groups],
                }}
    finally:
        conn.close()


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
        mappings_effective, assets=None)
    if trans_fix["status"] == "ok":
        mappings_effective = trans_fix["mappings"]
    else:
        logger.warning("转换验证-修复未完全解决: %s", trans_fix["message"])
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
    try:
        p_result = llm_client.recommend_pipeline(
            mappings_effective,
            source_type=source_type, target_type=target_type,
            available_components=AVAILABLE_COMPONENTS,
            source_models=source_models,
            target_models=target_models,
            transformation_plan=plan or {"mappings": mappings_effective},
            # 源/目标运行契约（归一 runtime）注入，驱动 AI 基于轮询/投递能力决策（密码由 llm_client 脱敏）
            source_runtime=(ds_obj or {}).get("runtime") if ds_obj else None,
            target_runtime=(tg.get("runtime") if locals().get("tg") else None))
        pipeline = p_result.get("pipeline")
        if pipeline and pipeline.get("components"):
            ai_components = pipeline["components"]
            logger.info("Agent B 生成管道拓扑（AI 决策 %d 组件）: %s",
                        len(ai_components),
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

    # 依据 Agent B（LLM）组件构建拓扑；ai_components=None（规则兜底）时才用纯注册表规则
    topology = build_pipeline_topology(
        mappings_effective,
        source_type=source_type, target_type=target_type,
        suggested_types=None,
        source_config=source_config, target_config=target_config,
        ai_components=ai_components)
    _save_pipeline_topology(topology)
    # 记录当前激活的转换关系（供 TransformProcess 对无 MappingId 的源消息兜底，如 SQL 轮询行）
    # 以及管道目标类型（TransformProcess 路由权威依据，防止 mapping.target_type 缺省 DB 误路由）
    if mappings_effective:
        import iris
        conn = iris_connector.get_connection()
        try:
            native = iris.createIRIS(conn)
            native.set(mappings_effective[0].get("id", ""),
                       "^demo.Config", "pipeline", "active_mapping")
            native.set(target_type,
                       "^demo.Config", "pipeline", "target_type")
        finally:
            conn.close()
    logger.info("已构建完整管道拓扑: %s", [c.get("type") for c in topology["components"]])

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

    # Agent C2：管道验证（拓扑/编译/启动/消息流转，不含转换关系）
    validation = pipeline_validator.run_pipeline_validation(
        topology, source_type, target_type)

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
        logger.info("管道验证-修复闭环完成: 状态=%s 轮次=%d",
                    fix_result["status"], len(fix_result["rounds"]))

    return success({
        "result": result,
        "production": "demo.DataflowProduction",
        "validation": validation,
        # AI 驱动信息：Agent B（LLM）决定组件构成；supplemented 为注册表保底补齐（校验性，非替代）
        "ai": {
            "driven": ai_components is not None,
            "rule_fallback": ai_components is None,
            "components": [c.get("type") for c in (ai_components or [])],
            "supplemented": (topology or {}).get("ai_supplemented", []),
            # 红线审计：C2 修复中规则是否改写了 AI 决策的组件构成（True 则结果非纯 AI）
            "c2_rule_rebuilt": bool((fix_result or {}).get("rule_applied")),
        },
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
