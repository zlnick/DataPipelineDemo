"""数据管道 API：生成、触发、监控。"""

import json
import logging
import random
import time
from datetime import datetime, timezone

from flask import Blueprint, request

from backend.config import FHIRConfig
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
                            target_config: dict | None = None) -> dict:
    """按源/目标类型 + 映射构建完整管道拓扑 JSON（组件带 className + settings）。

    - 源/目标/通用组件模板来自类型注册表（type_registry）；
    - SQLOperation 的 Query/InputParameters 在此由 Python 依据目标表列构建
      （信息来自 information_schema），不再由 IRIS 侧查询；
    - SOAP 目标为「WSDL 导入型」：target_config 提供 wsdl/service/packages，
      先调 %SOAP.WSDL.Reader 生成 BO，再用 BO 类作为 Operation 组件；
    - SQL 源：source_config 提供 dsn/query/key_field 填充 SQLService settings；
    - PipelineGenerator 按通用规则渲染（name/className/settings）；
    - suggested_types: Agent B 建议的组件 type 顺序（仅影响排序，不影响组成）。
    """

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
            query_val = cfg.get("query") or "SELECT * FROM SQLUser.Patient"
            key_field_val = cfg.get("key_field") or "ID"
            dsn_val = cfg.get("dsn") or "localTarget"
            components.append({
                "type": "SQLService", "name": "SQLService",
                "className": c["className"], "comment": c["comment"],
                "settings": [
                    _s("Adapter", "DSN", dsn_val),
                    _s("Host", "Query", query_val),
                    _s("Host", "KeyFieldName", key_field_val),
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
                    "settings": [],
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



@pipelines_bp.post("/generate")
def generate():
    """生成并启动数据管道。支持异构组合（source_type/target_type + target_config）。"""
    body = request.get_json(silent=True) or {}
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
            conn = tg.get("connection") or {}
            target_config = {
                "wsdl": target_config.get("wsdl") or conn.get("wsdl", ""),
                "service": target_config.get("service") or conn.get("service", "default"),
                "packages": target_config.get("packages") or conn.get("packages") or {},
                "bo_class": target_config.get("bo_class") or conn.get("bo_class") or conn.get("boClass") or tg.get("bo_class", ""),
            }

    if target_type == "SOAP" and not target_config.get("bo_class"):
        for tg in repository.list_targets():
            if tg.get("type") == "SOAP":
                conn = tg.get("connection") or {}
                bo_c = conn.get("bo_class") or conn.get("boClass") or tg.get("bo_class") or ""
                if bo_c:
                    target_config["bo_class"] = bo_c
                    if not target_config.get("wsdl"):
                        target_config["wsdl"] = conn.get("wsdl", "")
                    if not target_config.get("service"):
                        target_config["service"] = conn.get("service", "default")
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
        source_config = {
            "dsn": source_config.get("dsn") or cfg.get("dsn") or "localTarget",
            "query": source_config.get("query") or "",
            "key_field": source_config.get("key_field") or cfg.get("key_field", "ID"),
        }

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
            source_config["query"] = "SELECT * FROM SQLUser.Patient"

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
    config_json = json.dumps(config, ensure_ascii=False) if config else ""

    # Agent B：数据管道拓扑设计建议（失败不阻塞生成）
    suggested_types = []
    try:
        p_result = llm_client.recommend_pipeline(
            mappings_effective,
            source_type=source_type, target_type=target_type,
            available_components=AVAILABLE_COMPONENTS,
            source_models=source_models,
            target_models=target_models,
            transformation_plan=plan or {"mappings": mappings_effective})
        pipeline = p_result.get("pipeline")
        if pipeline:
            suggested_types = [c.get("type") for c in pipeline.get("components", [])]
            logger.info("Agent B 已生成管道拓扑建议: %s", suggested_types)
        else:
            logger.warning("Agent B 未返回 pipeline，使用类型注册表拓扑")
    except Exception as exc:
        logger.warning("Agent B 调用失败: %s", exc)

    # Python 依据类型注册表 + 映射构建完整拓扑（权威：含 className/settings）
    topology = build_pipeline_topology(
        mappings_effective,
        source_type=source_type, target_type=target_type,
        suggested_types=suggested_types,
        source_config=source_config, target_config=target_config)
    _save_pipeline_topology(topology)
    logger.info("已构建完整管道拓扑: %s", [c.get("type") for c in topology["components"]])

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


@pipelines_bp.get("/target-data")
def target_data():
    """目标表落库结果。"""
    table = request.args.get("table", "Patient")
    limit = request.args.get("limit", 50, type=int)
    from backend.routes.targets import _target_table_rows
    allowed = {tb["table"] for tb in _target_table_rows()}
    if table not in allowed:
        return error(f"目标表不存在或未注册: {table}"), 404
    data = iris_connector.class_method_value("demo.PipelineQuery", "GetTargetData", table, limit)
    try:
        items = json.loads(data or "[]")
    except json.JSONDecodeError:
        items = []
    return success({"items": items, "count": len(items)})
