# -*- coding: utf-8 -*-
"""管道**上下文归一**（源/目标配置重建、DSN/列清单/端点解析、C1 输入装配）—— 自
`routes/pipelines.py` 迁出（方案 A Step 2，2026-09-20）。

这些函数都是**纯读取/纯归一**（无 IRIS 写入、不做 AI 决策），单管道与多管道两条路径共用同一份；
`routes/pipelines.py` 以**私有别名**导入，既有调用点与离线回归口径不变。

设计约束：只依赖 `config` / `services` 同层模块，**不反向依赖 `routes/`**；
需要 IRIS/DB 细节时在函数内**局部导入**（沿用迁移前的写法），避免模块级循环导入。
"""

import logging

from backend.config import Config
from backend.services import repository

logger = logging.getLogger(__name__)


def registered_table_columns(tg: dict) -> tuple[dict, str]:
    """目标登记里的 ({表名: 列清单}, schema)（注册时按**目标 JDBC 元数据**取得 → 权威事实）。

    供 DB 目标 SQLOp 生成 UPSERT 列清单使用：列清单必须反映**目标库**的结构，
    不能拿当前命名空间同名表的列（演示默认目标 = CLINIC，跨库；2026-09-17 实测踩坑）。
    """
    cols_by_table: dict[str, list[str]] = {}
    schema = ""
    for tb in (tg or {}).get("tables") or []:
        name = str(tb.get("table") or tb.get("entity_name") or "").strip()
        cols = []
        for c in (tb.get("columns") or []):
            nm = c.get("name") if isinstance(c, dict) else str(c or "")
            if nm:
                cols.append(str(nm))
        if name and cols:
            cols_by_table[name] = cols
            schema = schema or str(tb.get("schema") or "")
    return cols_by_table, schema


def target_dsn(target_config: dict | None) -> str:
    """DB 目标 DSN：目标归一契约里的 dsn 优先，其次按 jdbc_url 的**命名空间**推导，
    最后才回落历史兜底 `localTarget`（= USER）。

    演示默认口径（2026-09-16）：SQL 源 = `USER`、SQL 目标 = `CLINIC` —— 目标登记的
    `jdbc:IRIS://…/CLINIC` 必须真正决定写入库；DB 目标 SQLOp 曾硬编码 `localTarget`，
    导致转换结果被静默写进 USER（而非目标声明的 CLINIC）。
    """
    cfg = target_config or {}
    dsn = str(cfg.get("dsn") or "").strip()
    if dsn and not dsn.startswith("jdbc:"):
        return dsn
    from backend.services import jdbc_dsn
    # dsn 被误填成 jdbc url 时也按它推导（不再直接退 localTarget）
    url = cfg.get("jdbc_url") or (dsn if dsn.lower().startswith("jdbc:iris:") else "")
    return jdbc_dsn.namespace_of(url) or "localTarget"


def get_table_columns(table: str, target_config: dict | None = None) -> list[str]:
    """DB 目标表列名（**按目标自身的库/命名空间取**：登记事实 → 目标命名空间 → 当前命名空间兜底）。

    ⚠ 原实现固定查当前命名空间（USER）的 information_schema：演示默认目标是 CLINIC（跨库），
    USER 里的同名表列不同 → 目标多出的列（如 CLINIC.Patient.MRN）被**静默丢弃**（映射了也写不进去）、
    目标独有列被漏掉（2026-09-17 Round 2 实测）。解析逻辑见 services/db_target_columns。
    """
    from backend.services import db_target_columns
    return db_target_columns.columns_of(table, target_config)


def merge_fhir_target_config(cfg: dict, tg: dict) -> dict:
    """把 FHIR 目标的连接契约（base_url / 凭据）并入 target_config（参数归一，非决策）。

    FHIR 目标组件 = `EnsLib.HTTP.GenericOperation`：Adapter 需要 HTTPServer/HTTPPort
    （由 base_url 解析），请求的 method/URL 头由发送方（BP）按 `^demo.Config("fhir",*)` 构造。
    单/多管道路径都必须走这一步 —— 缺了它 HTTPServer 为空，运行期报
    `Unable to open TCP/IP socket to server :52773`（2026-09-16 实测：FHIR 组消息全 Error、
    目标 0 落地；该症状此前被「FHIR 目标资源组装失败」掩盖，修好组装后才暴露）。
    """
    tconn = tg.get("connection") or {}
    trt = (tg.get("runtime") or {}).get("connection") or {}
    base = (cfg.get("base_url") or trt.get("base_url") or tconn.get("base_url")
            or trt.get("endpoint") or tconn.get("endpoint") or "")
    if base:
        cfg["base_url"] = base
        cfg.setdefault("endpoint", base)
    for k in ("username", "password"):
        v = cfg.get(k) or trt.get(k) or tconn.get(k)
        if v:
            cfg[k] = v
    return cfg


def fhir_http_host_port(base_url: str | None) -> tuple[str, str]:
    """从 FHIR 目标 base_url（如 http://iris:52773/csp/.../r4）解析 HTTPServer/HTTPPort。

    只做参数归一（非决策）；URL 路径前缀与 method/body 由发送方在消息里携带。
    base_url 缺失 → 主机名返回空并**显式告警**（生成前的 check_connection 门禁会先拦下来；
    若绕过门禁，运行期会以此空主机报 socket 错误，不会静默写到别的服务器）。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        logger.warning("FHIR/HTTP 目标的 base_url 为空：HTTPServer 无法确定"
                       "（应由 merge_fhir_target_config 从目标登记并入）")
        return "", "52773"
    head = base.split("://", 1)[1] if "://" in base else base
    host_port = head.split("/", 1)[0]
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        return host or "", port or "52773"
    return host_port, "80"


def resolve_sql_source_tables(mappings: list[dict], source_id: str | None) -> tuple[list[dict], dict]:
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


def sql_source_meta(mappings: list[dict], source_id: str | None) -> dict:
    """兼容入口：仅返回源资产元数据（{表: {columns, key_hint}}）。"""
    return resolve_sql_source_tables(mappings, source_id)[1]


def source_tables_from_mappings(mappings: list[dict]) -> list[str]:
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


def scope_source_context(runtime: dict | None, models: list[dict] | None,
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


def c1_assets(source_id: str | None) -> list[dict]:
    """C1 转换验证的源资产输入（name + fields + **field_terms 事实**）。

    field_terms 来自接口分析 AI 的产出（`coded_value` / `display_name` / `term_uri` / `plain`）：
    C1 的「明文落 coding」检查（`_coded_text_issues`）与术语体系事实链都依赖它 —— 缺了就 fail-open。
    """
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
        item = {"name": a.get("name") or a.get("id") or "",
                "fields": [c.get("name") if isinstance(c, dict) else str(c) for c in cols]}
        if isinstance(a.get("field_terms"), dict) and a.get("field_terms"):
            item["field_terms"] = a["field_terms"]
        if a.get("source_id") or source_id:
            item["source_id"] = a.get("source_id") or source_id
        out.append(item)
    # 常量指令/体系事实需要知道"列是否编码列" → 登记缺失 field_terms 时用 DataAsset 兜底补一次
    if out and not any(isinstance(x.get("field_terms"), dict) for x in out):
        try:
            by_name = {str((b.get("name") or "")).lower(): b
                       for b in (repository.list_assets(source_id) or []) if isinstance(b, dict)}
            for x in out:
                hit = by_name.get(str(x.get("name") or "").lower())
                if hit and isinstance(hit.get("field_terms"), dict) and hit["field_terms"]:
                    x["field_terms"] = hit["field_terms"]
        except Exception:  # noqa: BLE001 - 事实补齐失败即 fail-open
            pass
    return out


def c1_target_models(mappings: list[dict]) -> list[dict]:
    """C1 的 FHIR 目标模型输入（列结构），避免 L1 把它当 SQL 表列误剔除映射。

    ⚠ **只对声明为 FHIR（或未声明类型）的映射注入**：`Patient` 既是 DB 表名又是 FHIR 资源名，
    对 DB/SOAP 映射也注入 FHIR 模型会让 `get_table_columns("Patient")` 命中 FHIR 列清单
    （DB 映射的 9 个真实列被判"不存在"→ L1 静默剔除 → LLM 按 FHIR 列改写，实测毁掉 DB 映射）。
    """
    from backend.services import fhir_target_model as _ftm
    res: list[str] = []
    for m in mappings or []:
        tt = str(m.get("target_type") or "").strip().upper()
        if tt and tt != "FHIR":
            continue
        rt = str(m.get("target_table") or "").strip().lower()
        for k in _ftm.US_CORE_RESOURCE_MODELS:
            if k.lower() == rt and k not in res:
                res.append(k)
    return _ftm.build_entities(res) if res else []


def configs_for_identity(source_id: str | None, target_id: str | None) -> tuple[dict, dict]:
    """按业务身份从**登记事实**重建源/目标配置（供"未提交的既有管道自动并入"使用）。

    与新鲜路径同口径：源侧取 runtime connection；目标侧 DB 取 dsn/jdbc_url/列清单、
    FHIR 取 base_url/凭据、SOAP 取 service/endpoint。缺登记 → 返回空 dict（调用方容错）。
    """
    src_cfg: dict = {}
    tgt_cfg: dict = {}
    try:
        ds = repository.get_datasource(source_id) if source_id else None
    except Exception:  # noqa: BLE001 - 登记缺失不阻断（复用组通常不需要源配置）
        ds = None
    try:
        tg = repository.get_target(target_id) if target_id else None
    except Exception:  # noqa: BLE001
        tg = None
    if ds:
        rt = (repository.datasource_runtime(ds) or {}).get("connection") or {}
        src_cfg = {k: (rt.get(k) or ds.get(k) or "") for k in
                   ("endpoint", "host", "port", "jdbc_url", "dsn", "username", "password")}
    if tg:
        tt = (tg.get("type") or "").upper()
        rt = (repository.target_runtime(tg) or {}).get("connection") or {}
        if tt == "DB":
            from backend.services import jdbc_dsn as _jdsn
            _tc, _sch = registered_table_columns(tg)
            tgt_cfg = {"jdbc_url": rt.get("jdbc_url") or "", "dsn": rt.get("dsn") or _jdsn.dsn_name_for_target(tg),
                       "driver_class": rt.get("driver_class") or "", "table_columns": _tc,
                       "schema": _sch or "SQLUser"}
        elif tt == "FHIR":
            tgt_cfg = merge_fhir_target_config({}, tg)
        elif tt == "SOAP":
            tgt_cfg = {"service": (tg.get("config") or {}).get("service") or "default",
                       "endpoint": rt.get("endpoint") or Config.MOCK_SOAP_URL}
    return src_cfg, tgt_cfg


def datasource_for_mappings(mappings: list[dict] | None, ds_type: str = "") -> dict | None:
    """按映射的源表/资源名反查所属数据源（参数化兜底，不做任何 AI 决策）。

    用途：调用方未给 source_id（或给的 ID 已失效，如重置环境后重跑旧脚本）时，仍能引用
    正确的数据源 runtime（DSN / jdbc_url / FHIR endpoint），避免默默退化成 localTarget
    去读 USER 库（曾致"源表读不到数据"的假失败）。

    2026-09-16 修缺陷 N11（限定名反查失败）：`mapping.source` 有两种口径 —— LLM/UI 可能落
    **资产 ID**（`DS71120_TSQLUser.Patient`）或**资产名**（`Patient`）。原实现取 `split(".")[0]`
    且把 `DS` 开头的名字当噪声跳过 → 资产 ID 形态被整条丢弃 → names 为空 → 返回 None →
    下游 `resolve_sql_source_tables` 拿不到资产（columns/key_hint 皆空）→ sql2fhir 布局推导
    直接 500「患者主表未能判定」。现改为：取**末段**做候选（同时保留原始串），并与资产的
    `name` **和** `id` 双向、忽略大小写比对。

    2026-09-17 修缺陷 A 的连带风险（3 源共存）：映射现在带 `source_id`（数据源维度）→
    **优先采用声明的 source_id**（权威），因为 3 个源都有名为 `Patient` 的资产时，
    按名字反查必然**歧义**（票数相同会静默选到第一个源 = 静默错源）。名字反查路径也加
    **歧义守卫**：最高票并列时不再"猜一个"，而是返回 None 让下游显式报错（fail loud）。
    """
    declared: list[str] = []
    for m in mappings or []:
        if isinstance(m, dict):
            sid = str(m.get("source_id") or m.get("datasource_id") or "").strip()
            if sid:
                declared.append(sid)
    if declared:
        uniq = set(declared)
        sid = max(uniq, key=declared.count)
        if len(uniq) > 1:
            logger.warning("同组映射声明了多个数据源 %s，按多数取 %s（请检查分组）", sorted(uniq), sid)
        ds = repository.get_datasource(sid)
        if ds and (not ds_type or (ds.get("type") or "") == ds_type):
            return ds
        logger.warning("映射声明的数据源 %s 不存在或类型不符（期望 %s），退回按源表名反查",
                       sid, ds_type or "任意")
    cands: set[str] = set()
    for m in mappings or []:
        if not isinstance(m, dict):
            continue
        raw = str(m.get("source") or m.get("asset") or "").strip()
        if not raw:
            continue
        for part in (raw, raw.split(".")[-1]):
            part = part.strip()
            # 目标侧/聚合表达式不是源资产名，排除
            if part and not part.startswith(("TG", "concat")):
                cands.add(part.lower())
    if not cands:
        return None
    hits: list[tuple[int, dict]] = []
    for ds in repository.list_datasources():
        if ds_type and (ds.get("type") or "") != ds_type:
            continue
        hit = 0
        for a in repository.list_assets(ds.get("id")):
            keys = {str(a.get("name") or "").lower(), str(a.get("id") or "").lower()}
            keys.discard("")
            if keys & cands:
                hit += 1
        if hit > 0:
            hits.append((hit, ds))
    if not hits:
        return None
    best_hit = max(h for h, _ in hits)
    winners = [d for h, d in hits if h == best_hit]
    if len(winners) > 1:
        # 多个数据源命中同名资产 → 无法判定归属；**不猜**（返回 None 由下游显式报错）
        logger.warning("源表名 %s 在多个数据源都有同名资产（%s），无法判定归属；"
                       "请在映射上带 source_id（数据源维度）", sorted(cands),
                       [d.get("id") for d in winners])
        return None
    return winners[0]


def target_for_mappings(mappings: list[dict] | None, target_type: str = "") -> dict | None:
    """按映射的目标表/实体名反查已登记目标（target_id 缺失/失效时的参数化兜底）。

    2026-09-17（缺陷 A 连带，3 目标共存）：`Patient` 同时是 DB 目标表与 FHIR 资源名 →
    同实体名跨类型重名时"第一个命中"就会**静默选错目标**。现：调用方没给 `target_type` 时，
    改用**映射自身声明的 target_type 多数票**（AI/登记产物，权威）做过滤；若仍匹配到多个
    不同目标 → 记日志并放弃（返回 None，由下游显式报"目标无法判定"），不再猜。
    """
    names = {str(m.get("target_table") or "").strip() for m in (mappings or [])
             if isinstance(m, dict)}
    names.discard("")
    if not names:
        return None
    if not target_type:
        declared = [str(m.get("target_type") or "").strip() for m in (mappings or [])
                    if isinstance(m, dict) and str(m.get("target_type") or "").strip()]
        if declared:
            target_type = max(set(declared), key=declared.count)
    matched: list[dict] = []
    for tg in repository.list_targets():
        if target_type and (tg.get("type") or "") != target_type:
            continue
        for tb in (tg.get("tables") or []):
            tb_name = str((tb or {}).get("table") or (tb or {}).get("entity_name") or "")
            if tb_name and tb_name in names:
                matched.append(tg)
                break
    if not matched:
        return None
    if len(matched) > 1:
        logger.warning("目标实体名 %s 命中多个已登记目标（%s），无法判定归属；"
                       "请在提交里带 target_id", sorted(names),
                       [t.get("id") for t in matched])
        return None
    return matched[0]
