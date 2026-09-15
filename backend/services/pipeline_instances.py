# -*- coding: utf-8 -*-
"""数据管道实例（受管理持久实体）。

把「数据管道」从一次性生成产物升级为受管理对象：
- 存储：`^demo.PipelineInstance(id)` = JSON（权威实体；`^demo.Config("pipeline","topology")` 只是渲染输入）；
- 身份 (source_id, target_id, design_skill)：相同 → 更新同一管道；不同 → 新增。id 用确定性 slug
  `PIPE_<source_id>_<target_id>`（缺 id 时退回 `PIPE_<design_skill>`），保证重复生成不产生重复管道；
- 组件归属：生成时把业务主机的 `Category` 渲染为管道类别（category = design_skill）；
  只有**基础设施**（JavaGateway）记为 "shared"——它全局仅 1 个实例、跨管道复用；
  转换 BP（demo.TransformProcess）**按管道各建一个实例**（Ens 主机身份 = Item 名，
  className 可复用，如 `TransformProcess__sql2soap`）→ 归该管道类别、随其启停，
  供 UI 分组、许可预算「按管道启停」、以及 Agent B 的 existing_pipelines 上下文；
- 本模块只做**事实登记与状态维护**（确定性），不参与决策：管道构成仍由 Agent B（LLM）决定。
"""

import logging
import re
from datetime import datetime

from backend.services import iris_connector, pipeline_validator, repository

logger = logging.getLogger(__name__)

GLOBAL = "^demo.PipelineInstance"
SHARED_CATEGORY = "shared"
# 真正的共享基础设施（跨管道复用、全局仅 1 个实例，不能因某条管道启停而改变）：
# 仅 JavaGateway（JDBC 网关）。转换 BP 不在此列 —— 一条管道一个 BP 实例。
SHARED_TYPES = ("JavaGateway",)
INFRA_CLASS_HINTS = ("JavaGateway",)


def slug(text) -> str:
    """把任意文本转成可作 global key / 组件名后缀的 slug（非字母数字统一为下划线）。"""
    return re.sub(r"[^0-9A-Za-z]+", "_", str(text or "")).strip("_")


def category_of(design_skill=None, source_type=None, target_type=None) -> str:
    """管道类别（= Ens 业务主机 Category）：优先 design_skill（与设计 Skill 注册表一致），
    缺失则按「源类型 2 目标类型」回退（sql2soap / fhir2db…），仍缺失为 unknown。"""
    skill = str(design_skill or "").strip()
    if skill:
        return skill
    src = str(source_type or "").strip().lower()
    tgt = str(target_type or "").strip().lower()
    if src and tgt:
        return f"{src}2{tgt}"
    return "unknown"


def pipeline_id(source_id=None, target_id=None, design_skill=None,
                source_type=None, target_type=None) -> str:
    """确定性管道 ID：同一 (源, 目标) 恒等 → 同一 id（重复生成即更新）。"""
    src, tgt = slug(source_id), slug(target_id)
    if src and tgt:
        return f"PIPE_{src}_{tgt}"
    skill = slug(design_skill) or slug(f"{source_type}2{target_type}")
    return f"PIPE_{skill or 'unknown'}"


def split_components(components: list[dict] | None) -> tuple[list[str], list[str]]:
    """按组件定义拆分 (own, shared)：shared = 真正的共享基础设施（仅 JavaGateway）。

    转换 BP 属 own —— 它按管道各有一个实例（TransformProcess__<类别>），随管道启停。
    """
    own: list[str] = []
    shared: list[str] = []
    for c in components or []:
        nm = str((c or {}).get("name") or "").strip()
        if not nm:
            continue
        cls = str((c or {}).get("className") or "")
        is_shared = ((c or {}).get("type") in SHARED_TYPES
                     or any(h in cls for h in INFRA_CLASS_HINTS))
        target = shared if is_shared else own
        if nm not in target:
            target.append(nm)
    return own, shared


def list_instances() -> list[dict]:
    """全部管道实例（按 updated_at 倒序）。"""
    rows = repository.list_json(GLOBAL)
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows


def get_instance(pid: str) -> dict | None:
    """按 ID 取管道实例。"""
    return repository.get_json(GLOBAL, pid)


def delete_instance(pid: str) -> bool:
    """删除管道实例记录（不停止 Production，组件仍按当前 Enabled 状态运行）。"""
    return repository.delete_json(GLOBAL, pid)


def group_by_category(items: list[dict] | None = None,
                      production_items: list[dict] | None = None) -> list[dict]:
    """按 category 折叠分组（UI「管道列表」用）。

    返回 [{"category", "items", "component_count", "enabled_count", "active"}]，
    活动管道组排在前面。

    注意 `enabled_count` 只统计**当前类别归属仍是本组**的已启用组件：同名源 BS 被重新生成给
    别的管道类别后，物理组件仍启用但已归属新管道 —— 若照名字硬计，会同时算进旧组
    （出现「占用许可 1/2」的假象，与 `/pipelines/items` 视图不一致）。
    """
    rows = items if items is not None else list_instances()
    prod = {str(i.get("name")): i for i in (production_items or [])}
    buckets: dict[str, list[dict]] = {}
    for rec in rows or []:
        buckets.setdefault(str(rec.get("category") or "unknown"), []).append(rec)
    groups = []
    for cat, members in buckets.items():
        own = [n for r in members for n in (r.get("component_names") or [])]
        enabled = [n for n in own
                   if int((prod.get(n) or {}).get("enabled") or 0) == 1
                   and str((prod.get(n) or {}).get("category") or "") == cat]
        groups.append({
            "category": cat,
            "items": members,
            "component_count": len(own),
            "enabled_count": len(enabled),
            "active": any(r.get("status") == "active" for r in members),
        })
    groups.sort(key=lambda g: (0 if g["active"] else 1, g["category"]))
    return groups


def _instance_state(rec: dict, prod: dict) -> str:
    """按 Production 事实判定管道状态（active / suspended / superseded）。

    - active：own 组件全部 Enabled=1 且类别归属仍是本管道；
    - superseded：own 组件在 Production 中已不存在，或已被**其它管道类别**接管
      （同名源 BS 被重新生成给了别的管道 → 本管道的组件归属已过期），不需要再启停；
    - suspended：组件仍在且归属未变，只是被许可预算让路（可随时启用）。
    """
    names = rec.get("component_names") or []
    cat = str(rec.get("category") or "")
    if not names or not prod:  # 无组件记录 / Production 不可读 → 以记录状态为准
        return "active" if rec.get("status") == "active" else "suspended"
    gone = 0
    for n in names:
        item = prod.get(n)
        if not item:
            gone += 1
            continue
        pcat = str(item.get("category") or "")
        if cat and pcat and pcat != cat:   # 空类别=历史遗留组件，视为本管道
            gone += 1
    if gone:
        # 有组件已不存在、或归属已被其它管道类别接管 → 本管道的组件归属已过期：
        # 不能"原地启用"（旧名字在新 Production 里可能属于别的管道 → 会误启别人的组件），
        # 只能删除记录后重新生成（UI 只显示删除）。
        return "superseded"
    enabled_all = all(int((prod.get(n) or {}).get("enabled") or 0) == 1 for n in names)
    return "active" if enabled_all else "suspended"


def reconcile_states(production_items: list[dict] | None = None) -> list[dict]:
    """按 Production 实际组件状态回写每条管道 status（active / suspended / superseded）。

    事实驱动：own 组件全部启用 = active；有组件被让路 = suspended；
    组件已被其它管道接管或不再存在 = superseded（UI 提示"已被新生成取代"）。
    返回回写后的实例列表。
    """
    prod_items = production_items if production_items is not None \
        else pipeline_validator.production_items()
    prod = {str(i.get("name")): i for i in prod_items}
    out: list[dict] = []
    for rec in repository.list_json(GLOBAL):
        status = _instance_state(rec, prod)
        if status != rec.get("status"):
            logger.info("管道实例状态变更: %s %s -> %s",
                        rec.get("id"), rec.get("status"), status)
            rec["status"] = status
            rec["updated_at"] = datetime.now().isoformat()
            repository.set_json(GLOBAL, rec.get("id"), rec)
        out.append(rec)
    return out


def upsert_from_generation(*, source_id: str | None = None, target_id: str | None = None,
                           source_type: str = "", target_type: str = "",
                           design_skill: str | None = None,
                           category_hint: str | None = None,
                           mapping_ids: list[str] | None = None,
                           components: list[dict] | None = None,
                           routes: dict | None = None, ai: dict | None = None,
                           validation: dict | None = None, name: str | None = None,
                           production_items: list[dict] | None = None) -> dict:
    """生成成功后登记/更新管道实例（同身份更新，不新增）。返回实例记录。

    参数:
        source_id/target_id/design_skill: 身份三要素（相同即同一管道）；
        components: 本次拓扑组件（用于拆分 own/shared 组件名）；
        routes: 路由表键（如 {"source_bs": "SQLService_Patient"}）；
        ai: AI 驱动审计（driven/supplemented）；validation: 验证报告摘要。
    """
    now = datetime.now().isoformat()
    pid = pipeline_id(source_id, target_id, design_skill, source_type, target_type)
    category = str(category_hint or "").strip() or category_of(
        design_skill, source_type, target_type)
    own, shared = split_components(components)
    rec = repository.get_json(GLOBAL, pid) or {}
    history = list(rec.get("skill_history") or [])
    if rec.get("design_skill") and design_skill and rec["design_skill"] != design_skill:
        if rec["design_skill"] not in history:
            history.append(rec["design_skill"])
        logger.info("管道 %s 的设计 Skill 由 %s 变更为 %s（同源/目标 → 同一管道，保留历史）",
                    pid, rec["design_skill"], design_skill)
    rec.update({
        "id": pid,
        "name": name or rec.get("name") or f"{source_type}→{target_type} {category}",
        "source_type": source_type,
        "target_type": target_type,
        "source_id": source_id,
        "target_id": target_id,
        "mapping_ids": list(mapping_ids or []),
        "design_skill": design_skill or rec.get("design_skill") or category,
        "skill_history": history,
        "category": category,
        "component_names": own,
        "shared_component_names": shared,
        "routes": routes or rec.get("routes") or {},
        "status": "active",
        "ai": ai or rec.get("ai") or {},
        "created_at": rec.get("created_at") or now,
        "updated_at": now,
        "generation_count": int(rec.get("generation_count") or 0) + 1,
    })
    if validation is not None:
        rec["last_validation"] = {
            "ok": bool(validation.get("ok")),
            "error_count": int(validation.get("error_count") or 0),
            "warning_count": int(validation.get("warning_count") or 0),
        }
    repository.set_json(GLOBAL, pid, rec)
    logger.info("管道实例已登记: %s（类别=%s own=%d 共享=%d 第 %d 次生成）",
                pid, category, len(own), len(shared), rec["generation_count"])
    prod_items = production_items if production_items is not None \
        else pipeline_validator.production_items()
    reconcile_states(production_items=prod_items)
    return get_instance(pid) or rec


def set_enabled(pid: str, enabled: bool) -> dict:
    """启用/禁用一条管道（占用/释放许可单元）。

    规则：
      - 启用：该管道 own + shared 组件全部启用（shared 供多管道复用）；
      - 禁用：只停 own 组件；仅当「没有其它活动管道」时才连 shared 一起停（多释放单元）。
    返回 {"ok", "result", "items", "instance", "suspended_others", "disabled_others"}。
    disabled_others = 为腾出许可被自动停用的**其它管道**组件（一键切换语义，容量不足时自动让路）。
    """
    rec = get_instance(pid)
    if not rec:
        return {"ok": False, "message": f"管道实例不存在: {pid}"}
    if rec.get("status") == "superseded":
        # 组件已被别的管道接管或不再存在：不能"原地启用"，必须重新生成
        return {"ok": False, "instance": rec,
                "message": (f"管道 {pid} 已被新生成取代（组件 {rec.get('component_names')} "
                            "不再是本管道的组件）：请重新生成该管道，或删除本记录")}
    own = list(rec.get("component_names") or [])
    shared = list(rec.get("shared_component_names") or [])
    # 只对「当前 Production 中确实存在、且类别归属仍是本管道」的组件做启停：
    # 组件名漂移（同名源 BS 被重新生成给别的类别）后，旧名字在新 Production 里可能已属于
    # 别的管道，直接启用会误启别人的组件 → 显式拒绝，让用户删除记录后重新生成。
    prod_map = {str(i.get("name")): i for i in pipeline_validator.production_items()}
    _cat = str(rec.get("category") or "")
    valid_own = [n for n in own
                 if n in prod_map
                 and (not _cat or not (prod_map.get(n) or {}).get("category")
                      or str((prod_map.get(n) or {}).get("category")) == _cat)]
    if enabled and own and len(valid_own) < len(own):
        return {"ok": False, "instance": rec, "stale_components": own,
                "message": (f"管道 {pid} 的组件归属已过期（有效 {len(valid_own)}/{len(own)}：{own}）"
                            "：组件已被新生成接管或移除，不能原地启用；"
                            "请删除本记录后重新生成该管道。")}
    own = valid_own
    others_active = [r for r in repository.list_json(GLOBAL)
                     if r.get("id") != pid and r.get("status") == "active"]
    names = own + shared if (enabled or not others_active) else own
    if not names:
        return {"ok": False,
                "message": f"管道 {pid} 未记录组件，无法启停（可先 POST /api/pipelines/instances/sync 回填）"}
    # 许可容量检查：启用后「已启用业务主机数 + 1（后端连接）」不得超许可单元，否则 IRIS
    # 会拒绝新连接 → 后端接口全线 500。与生成流程同一预算规则，但这里是**一键切换语义**：
    # 容量不够时自动停用其它活动管道来腾单元（而不是直接拒绝），并把让路结果显式返回。
    units = pipeline_validator.license_units()
    disabled_others: list[str] = []   # 为腾出许可而被自动停用的其它管道组件
    if enabled and units:
        prod_now = {str(i.get("name")): i for i in pipeline_validator.production_items()}

        def _billable(nm: str) -> bool:
            """JavaGateway 等基础设施不计入业务主机预算（业务主机常驻各占 1 个单元）。"""
            return "JavaGateway" not in str((prod_now.get(nm) or {}).get("className") or "")

        enabled_now = {n for n, i in prod_now.items()
                       if int(i.get("enabled") or 0) == 1 and _billable(n)}
        will_enable = {n for n in names if n not in enabled_now and _billable(n)}
        projected = len(enabled_now | will_enable) + 1   # +1 = 后端自身连接
        if projected > units:
            victims = sorted([r for r in repository.list_json(GLOBAL)
                              if r.get("id") != pid and r.get("status") == "active"],
                             key=lambda r: str(r.get("updated_at") or ""))
            for v in victims:
                if projected <= units:
                    break
                v_names = [n for n in (v.get("component_names") or [])
                           if n in enabled_now and _billable(n)]
                if not v_names:
                    continue
                r2 = pipeline_validator.set_items_enabled(v_names, False)
                if not r2.get("ok"):
                    logger.warning("许可切换：停用管道 %s 的组件失败: %s",
                                   v.get("id"), r2.get("message") or r2.get("result"))
                    continue
                logger.info("许可切换：为管道 %s 让路，停用 %s 的组件 %s",
                            pid, v.get("id"), v_names)
                disabled_others.extend(v_names)
                enabled_now -= set(v_names)
                projected = len(enabled_now | will_enable) + 1
            if projected > units:
                # 让路后仍不够 → 回滚让路（不留"半停"状态）并显式报错
                if disabled_others:
                    pipeline_validator.set_items_enabled(disabled_others, True)
                    logger.warning("许可切换：让路后仍超容量，已回滚 %s", disabled_others)
                msg = (f"许可不足：启用本管道后需 {projected} 个单元（业务主机 "
                       f"{projected - 1} + 后端连接 1）> 许可单元 {units}；已尝试停用其它"
                       "活动管道仍不够 → 请减少本管道组件数（如少选源表）后重新生成。")
                logger.warning("管道 %s 启用被拒：%s", pid, msg)
                return {"ok": False, "instance": rec, "projected": projected,
                        "units": units, "disabled_others": [], "rolled_back": True,
                        "message": msg}
    res = pipeline_validator.set_items_enabled(names, enabled)
    if not res.get("ok"):
        return {"ok": False, "message": res.get("message") or res.get("result"), "result": res}
    prod_items = pipeline_validator.production_items()
    reconcile_states(production_items=prod_items)
    suspended = [r.get("id") for r in repository.list_json(GLOBAL)
                 if r.get("id") != pid and r.get("status") == "suspended"]
    return {"ok": True, "result": res.get("result"), "items": names,
            "instance": get_instance(pid), "suspended_others": suspended,
            "disabled_others": disabled_others,
            "units": res.get("units"), "enabled": bool(enabled),
            # 运行期双写复核（set_items_enabled）：不一致说明需重启生产才收敛
            "runtime_applied": res.get("runtime_applied") or [],
            "runtime_failed": res.get("runtime_failed") or [],
            "runtime_mismatch": res.get("runtime_mismatch") or []}


def active_categories() -> list[str]:
    """当前处于活动状态的管道类别（许可预算 keep 名单）。"""
    cats = [str(r.get("category")) for r in repository.list_json(GLOBAL)
            if r.get("status") == "active" and r.get("category")]
    return list(dict.fromkeys(cats))


def existing_pipelines_brief(limit: int = 20) -> list[dict]:
    """已存在管道的事实清单（注入 Agent B 上下文；不替代 AI 决策）。"""
    brief: list[dict] = []
    for rec in list_instances()[:limit]:
        brief.append({
            "id": rec.get("id"), "name": rec.get("name"),
            "category": rec.get("category"),
            "source_type": rec.get("source_type"), "target_type": rec.get("target_type"),
            "source_id": rec.get("source_id"), "target_id": rec.get("target_id"),
            "design_skill": rec.get("design_skill"), "status": rec.get("status"),
            "components": rec.get("component_names") or [],
        })
    return brief


def _config_value(sub: str, key: str) -> str:
    """读取 ^demo.Config(sub, key)（单值，非 JSON 记录）。"""
    import iris

    conn = iris_connector.get_connection()
    try:
        native = iris.createIRIS(conn)
        return str(native.get("^demo.Config", sub, key) or "")
    finally:
        iris_connector.reset_connections()


def sync_from_production(production_items: list[dict] | None = None) -> dict:
    """从 Production（Category）+ 路由表回填管道实例（历史环境迁移/修复用）。

    - 组件来源：Production 的 Category（生成时渲染）→ 每个类别 = 一条管道；
    - 源 BS → `^demo.Config("pipe", <源BS名>)` 路由表 → mapping id / target_type；
    - source_id / target_id 由源资产与目标登记反查；设计 Skill 取类别名；
    - 回填记录带 `recovered: True` 标注（说明来自同步而非生成链路）。
    返回 {"created", "updated", "instances", "categories", "production_items"}。
    """
    import json as _json

    prod_items = production_items if production_items is not None \
        else pipeline_validator.production_items()
    prod = {str(i.get("name")): i for i in prod_items}
    by_cat: dict[str, list[str]] = {}
    shared_names: list[str] = []
    for it in prod_items:
        cat = str(it.get("category") or "")
        nm = str(it.get("name") or "")
        if not nm or not cat:
            continue
        if cat == SHARED_CATEGORY:
            shared_names.append(nm)
            continue
        by_cat.setdefault(cat, []).append(nm)

    def _resolve_ids(source_name: str, tgt_name: str, route: dict) -> tuple[str | None, str | None]:
        """按源资产名/目标表(或服务)名反查 source_id / target_id。"""
        src_id = None
        for asset in repository.list_source_assets():
            names = [str(asset.get("name") or ""), str(asset.get("id") or "")]
            if source_name and any(source_name == n or source_name in n for n in names if n):
                src_id = asset.get("source_id")
                break
        tgt_id = None
        route_service = str(route.get("service") or "")
        for tg in repository.list_targets():
            for tb in (tg.get("tables") or []):
                tb_name = str(tb.get("table") or tb.get("entity_name") or "")
                if tgt_name and tb_name == tgt_name:
                    tgt_id = tg.get("id")
                    break
                if route_service and route_service in (tb_name, str(tg.get("name") or "")):
                    tgt_id = tg.get("id")
                    break
            if tgt_id:
                break
        return src_id, tgt_id

    created, updated, records = 0, 0, []
    for cat, names in by_cat.items():
        src_bs = next((n for n in names
                       if any(h in str((prod.get(n) or {}).get("className") or "")
                              for h in ("SQL.Service", "SQL.Operation", "FHIRService",
                                        "FHIRSyncService"))), "")
        src_cls = str((prod.get(src_bs) or {}).get("className") or "") if src_bs else ""
        src_type = "SQL" if ("SQL." in src_cls and src_bs) else "FHIR"
        route: dict = {}
        if src_bs:
            raw = _config_value("pipe", src_bs)
            if raw:
                try:
                    route = _json.loads(raw)
                except ValueError:
                    logger.warning("路由表 ^demo.Config(\"pipe\",%s) 非 JSON，忽略", src_bs)
        tgt_type = str(route.get("target_type") or "DB")
        mid = str(route.get("mapping") or "")
        mp = repository.get_json("^demo.Mapping", mid) if mid else None
        src_name = str((mp or {}).get("source") or "")
        tgt_name = str((mp or {}).get("target_table") or route.get("table") or "")
        src_id, tgt_id = _resolve_ids(src_name, tgt_name, route)
        pid = pipeline_id(src_id, tgt_id, cat, src_type, tgt_type)
        prev = repository.get_json(GLOBAL, pid) or {}
        own = [n for n in names if n not in shared_names]
        record = {
            "id": pid,
            "name": prev.get("name") or f"{src_type}→{tgt_type} {cat}",
            "source_type": src_type, "target_type": tgt_type,
            "source_id": src_id, "target_id": tgt_id,
            "mapping_ids": [mid] if mid else list(prev.get("mapping_ids") or []),
            "design_skill": prev.get("design_skill") or cat,
            "category": cat,
            "component_names": own,
            "shared_component_names": prev.get("shared_component_names") or shared_names,
            "routes": {"source_bs": src_bs} if src_bs else (prev.get("routes") or {}),
            "status": prev.get("status") or "suspended",
            "ai": prev.get("ai") or {},
            "recovered": True,
            "created_at": prev.get("created_at") or datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "generation_count": int(prev.get("generation_count") or 0),
        }
        repository.set_json(GLOBAL, pid, record)
        if prev:
            updated += 1
        else:
            created += 1
        records.append(record)
    reconcile_states(production_items=prod_items)
    logger.info("管道实例回填：新增 %d，更新 %d（类别 %s）", created, updated, sorted(by_cat))
    return {"created": created, "updated": updated, "instances": records,
            "categories": sorted(by_cat), "production_items": prod_items}

