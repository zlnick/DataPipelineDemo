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
    """确定性管道 ID：同一 (源, 目标) 恒等 → 同一 id（重复生成即更新）。

    ⚠ 2026-09-19（P0a 身份稳定）：**只认业务键 (source_id, target_id)** —— 设计 Skill 由 LLM 选择，
    不得影响身份（否则同一业务管道会因 LLM 换了 skill、或某次 source_id 缺失而登记成**两套实例**）。
    缺业务键时返回**空串**（调用方必须显式处理：拒绝生成，而不是退化成 `PIPE_<skill>` 造出幽灵实例）。
    """
    src, tgt = slug(source_id), slug(target_id)
    if src and tgt:
        return f"PIPE_{src}_{tgt}"
    return ""


def pipeline_id_fallback_note(source_id=None, target_id=None) -> str:
    """身份缺失时给调用方的可读说明（源/目标必须能解析出数据源与目标 id）。"""
    miss = []
    if not slug(source_id):
        miss.append("源数据源（source_id）")
    if not slug(target_id):
        miss.append("目标（target_id）")
    return "无法确定管道身份：缺少 %s —— 请在界面上明确选择源数据源与目标后重试" % "、".join(miss)


def _component_fingerprint(c: dict) -> dict:
    """组件**定义**指纹（刻意**不含 `enabled`**：enabled 属运行态，由 P2 增量保留，不参与"是否变更"）。"""
    return {
        "name": str(c.get("name") or ""),
        "className": str(c.get("className") or ""),
        "category": str(c.get("category") or ""),
        "settings": sorted(
            [str(s.get("target") or ""), str(s.get("name") or ""), str(s.get("value") or "")]
            for s in (c.get("settings") or []) if isinstance(s, dict)),
    }


def component_signature(components: list[dict] | None,
                        mapping_ids: list[str] | None = None,
                        extra: dict | None = None) -> str:
    """管道**定义签名**（确定性，供增量生成判断"这条管道是否已存在且未变更"）。

    组成：组件定义指纹（按 name 排序）+ 映射 id（排序）+ `extra`（如 sql2fhir 的 BP 源码/布局哈希）。
    ⚠ 必须含**内容**（mapping_ids / extra）——否则"改了映射却判 unchanged"会静默失效（红线不允许）。
    ⚠ 不含 `enabled`（运行态另由 P2 保留），也不含 `design_skill`（LLM 可改选，不应算变更）。
    """
    import hashlib
    import json as _json

    items = [_component_fingerprint(c) for c in (components or []) if isinstance(c, dict)]
    items.sort(key=lambda x: x["name"])
    payload = {
        "components": items,
        "mappings": sorted(str(x) for x in (mapping_ids or [])),
        "extra": {str(k): extra[k] for k in sorted((extra or {}).keys())},
    }
    blob = _json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def find_instance(source_id: str | None, target_id: str | None) -> dict | None:
    """按**业务身份**（源数据源 + 目标）取管道实例（身份不含 LLM 选择的 skill）。"""
    pid = pipeline_id(source_id, target_id)
    return get_instance(pid) if pid else None


def is_unchanged(source_id: str | None, target_id: str | None,
                 signature: str) -> tuple[bool, dict | None]:
    """该业务身份是否**已存在且定义未变更**（→ 增量生成时复用其存储组件，不重跑 Agent B）。

    返回 (unchanged, existing_record)；签名不一致或不存在 → (False, rec-or-None)。
    """
    rec = find_instance(source_id, target_id)
    if rec and signature and str(rec.get("signature") or "") == str(signature):
        return True, rec
    return False, rec


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
        # 按名去重：同类别多组（如两个 SQL 源的 sql2db）时，各实例的组件名不同（`__sql2db` / `__sql2db_2`），
        # 直接平铺会把重复名累计成双倍计数（2026-09-17 实测）。
        own = sorted({n for r in members for n in (r.get("component_names") or [])})
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


def input_signature(source_id: str | None, target_id: str | None,
                    mappings: list[dict] | None, source_type: str = "",
                    target_type: str = "", extra: dict | None = None) -> str:
    """**入参签名**（生成前即可算，不需要 LLM）：判断"这条管道是否已存在且输入未变"。

    组成：业务身份 + 源/目标类型 + **映射内容**（id/源/目标/字段映射，排序）+ `extra`
    （如源/目标运行契约的关键位、sql2fhir 布局推导输入）。
    未变 → 增量生成**跳过 Agent B**，直接复用存储的 `ai_components`（省 LLM 调用与抖动）。
    """
    import hashlib
    import json as _json

    maps = []
    for m in mappings or []:
        if not isinstance(m, dict):
            continue
        fields = sorted(
            [str(f.get("source") or ""), str(f.get("target") or ""), str(f.get("transform") or "")]
            for f in (m.get("field_mappings") or []) if isinstance(f, dict))
        maps.append({
            "id": str(m.get("id") or ""),
            "source": str(m.get("source") or ""),
            "target_table": str(m.get("target_table") or ""),
            "target_type": str(m.get("target_type") or ""),
            "fields": fields,
        })
    maps.sort(key=lambda x: x["id"])
    payload = {
        "identity": [slug(source_id), slug(target_id), str(source_type or ""),
                     str(target_type or "")],
        "mappings": maps,
        "extra": {str(k): extra[k] for k in sorted((extra or {}).keys())},
    }
    blob = _json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def upsert_from_generation(*, source_id: str | None = None, target_id: str | None = None,
                           source_type: str = "", target_type: str = "",
                           design_skill: str | None = None,
                           category_hint: str | None = None,
                           mapping_ids: list[str] | None = None,
                           components: list[dict] | None = None,
                           ai_components: list[dict] | None = None,
                           is_sql2fhir: bool = False,
                           layout: dict | None = None,
                           source_config: dict | None = None,
                           target_config: dict | None = None,
                           signature: str | None = None,
                           applied_signature: str | None = None,
                           routes: dict | None = None, ai: dict | None = None,
                           validation: dict | None = None, name: str | None = None,
                           reused: bool = False,
                           production_items: list[dict] | None = None) -> dict:
    """生成成功后登记/更新管道实例（同身份更新，不新增）。返回实例记录。

    参数:
        source_id/target_id: **业务身份**（相同即同一管道；缺任一 → 拒绝登记，见 P0a）；
        design_skill: 设计 Skill（**属性**，非身份；变更写入 skill_history）；
        components: 本次拓扑组件（用于拆分 own/shared 组件名 + **存完整定义**供增量复用）；
        signature/applied_signature: 定义签名 / 本次实际应用到 Production 的合并签名（增量 diff 依据）；
        routes: 路由表键（如 {"source_bs": "SQLService_Patient"}）；
        ai: AI 驱动审计（driven/supplemented）；validation: 验证报告摘要。
    """
    now = datetime.now().isoformat()
    pid = pipeline_id(source_id, target_id)
    if not pid:
        # P0a：身份缺失**不登记**（否则会退化成 PIPE_<skill> 幽灵实例 = "两套 SQL-SOAP"）
        raise ValueError(pipeline_id_fallback_note(source_id, target_id))
    category = str(category_hint or "").strip() or category_of(
        design_skill, source_type, target_type)
    own, shared = split_components(components)
    own_defs = [dict(c) for c in (components or [])
                if isinstance(c, dict) and str(c.get("name") or "") in set(own)]
    # ⚠ 只存**可渲染**的组件（必须有 className）：曾把 Agent 原始组件（无 className/settings）
    #   误存进定义 → 下次复用会渲染出不合规拓扑（实测 2026-09-20）。缺参数时宁可少存
    #   （判据会因此判"定义不完整" → 走正常生成重建 = 自愈），也不存污染物。
    _no_cls = [str(c.get("name") or c.get("type") or "?") for c in own_defs
               if not str(c.get("className") or "").strip()]
    if _no_cls:
        logger.warning("管道实例登记：丢弃 %d 个无 className 的组件（不可渲染，不写入定义）: %s",
                       len(_no_cls), _no_cls)
        own_defs = [c for c in own_defs if str(c.get("className") or "").strip()]
    # —— 复用用的"冻结定义"：**渲染成功的参数（className/settings）+ 改名前名字** ——
    # 为什么需要它（2026-09-20 实测）：复用组若直通 Agent 原始 `ai_components`（无 className/settings）
    # 会因拓扑校验失败而 500；若走注册表参数化链路又会**丢掉 SOAP 目标 BO**。冻结定义两头都对：
    #   · 参数完整（就是上一轮渲染成功的那份，能直接渲染）；
    #   · 名字是**改名前**形态（`SQLService_Patient` / `SOAPOp_PatientService`）→ 渲染时按同一规则
    #     确定性重命名（`__{类别}`），不会出现 `…__sql2soap__sql2soap` 这类二次后缀。
    _ai_by_type: dict[str, list[str]] = {}
    for _c in (ai_components or []):
        if isinstance(_c, dict):
            _ai_by_type.setdefault(str(_c.get("type") or ""), []).append(str(_c.get("name") or ""))
    _used: dict[str, int] = {}
    render_defs: list[dict] = []
    for _c in own_defs:
        _t = str(_c.get("type") or "")
        _names = _ai_by_type.get(_t) or []
        _i = _used.get(_t, 0)
        _pre = _names[_i] if _i < len(_names) and _names[_i] else str(_c.get("name") or "")
        _used[_t] = _i + 1
        # 兜底：无配对时把 `__{类别}` 后缀剥掉，回到改名前形态
        if not _pre:
            _suf = "__" + slug(category)
            _nm = str(_c.get("name") or "")
            _pre = _nm[:-len(_suf)] if _suf and _nm.endswith(_suf) else _nm
        render_defs.append({**_c, "name": _pre})
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
        # P0（增量生成的地基）：**存完整组件定义**供未变更管道复用（不重跑 Agent B）+ 定义签名
        "components": own_defs,
        # 复用（未变更）时**真正拿来渲染**的定义：渲染成功的参数 + 改名前名字（见上方说明）
        "render_components": render_defs,
        # 复用注入用：本组**未改名**的原始组件（含 infra；名字经 build_multi_pipeline_topology
        # 重新确定性命名 → 与首次生成结果一致）
        "ai_components": [dict(c) for c in (ai_components or rec.get("ai_components") or [])
                          if isinstance(c, dict)],
        "is_sql2fhir": bool(is_sql2fhir or rec.get("is_sql2fhir")),
        "layout": layout if layout is not None else rec.get("layout"),
        # 源/目标配置（供"未提交的既有管道自动并入"时忠实重建：DB 列清单 / FHIR 端点 / SOAP 服务）
        "source_config": source_config if source_config is not None else rec.get("source_config"),
        "target_config": target_config if target_config is not None else rec.get("target_config"),
        "signature": signature or rec.get("signature") or "",
        "applied_signature": applied_signature or rec.get("applied_signature") or "",
        "routes": routes or rec.get("routes") or {},
        "status": "active",
        "ai": ai or rec.get("ai") or {},
        "created_at": rec.get("created_at") or now,
        "updated_at": now,
        # 复用（未变更）的管道**不虚增"生成次数"**：本次只是按存储定义并入，没重新生成
        "generation_count": int(rec.get("generation_count") or 0) + (0 if reused else 1),
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


def rewrite_mapping_id(old_id: str, new_id: str) -> int:
    """把各管道实例 `mapping_ids` 里对 old_id 的引用改写为 new_id，返回被改写的管道数。

    适用场景：同身份重复映射被 `repository.save_mappings` 归并删除后，管道实体不应该继续
    挂着已删除的 id（否则 UI 的映射芯片指向不存在的记录 —— F4 类「id 漂移」的展示侧表现）。
    幂等：old 不存在即无操作；改写后按序去重（避免 new_id 已存在时出现重复项）。
    """
    old_id, new_id = str(old_id or "").strip(), str(new_id or "").strip()
    if not old_id or not new_id or old_id == new_id:
        return 0
    changed = 0
    for rec in list_instances():
        ids = [str(x) for x in (rec.get("mapping_ids") or [])]
        if old_id not in ids:
            continue
        seen: set[str] = set()
        rewritten: list[str] = []
        for x in ids:
            y = new_id if x == old_id else x
            if y and y not in seen:
                seen.add(y)
                rewritten.append(y)
        rec["mapping_ids"] = rewritten
        rec["updated_at"] = datetime.now().isoformat()
        repository.set_json(GLOBAL, str(rec.get("id")), rec)
        changed += 1
    if changed:
        logger.warning("映射引用改写：%s → %s（影响 %d 条管道实例的 mapping_ids）",
                       old_id, new_id, changed)
    return changed


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
            """单条管道的容量账：**共享基础设施不计入**（判据 = Category=="shared"）。

            打标方是生成端 `routes/pipelines._stamp_categories`：JavaGateway / FHIRSyncService /
            TerminologyOperation 归 `shared`（跨管道共享、全局仅 1 实例、被当前活动的**任何**管道
            共同依赖）。把它们算进某条管道的容量 → 「最大的一条管道永远启不动」：
            2026-09-18 实测（新增共享术语 BO 后）CLINIC→FHIR 组（6 主机）被自身预算判为
            「需 9 个单元 > 8」而拒绝启用，而 IRIS 实际同时运行 9 个主机 + 后端访问正常
            （共享件本就真实占用许可，由 IRIS 兜底；这里只决定"哪条管道能起"）。
            与生成期预算（`pipeline_validator.schedule_groups` 亦不计 infra）口径一致。
            """
            it = prod_now.get(nm) or {}
            cls = str(it.get("className") or "")
            cat = str(it.get("category") or "")
            if cat == "shared":
                return False
            return not ("JavaGateway" in cls or "FHIRSyncService" in cls
                        or "TerminologyOperation" in cls)

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
    # 透明化：共享基础设施仍**真实占用**许可（本函数不计入单管道容量账）→ 若「管道主机 +
    # 共享主机 + 后端」超过许可单元，这里显式告警（不阻断：IRIS 侧才是最终裁决），避免"静默超订"。
    shared_enabled = [n for n, i in {str(x.get("name")): x
                                     for x in pipeline_validator.production_items()}.items()
                      if int(i.get("enabled") or 0) == 1
                      and not _billable(n)] if (enabled and units) else []
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
            # 许可透明化：共享基础设施不计入单管道容量账，但真实占单元 → 一并回报供 UI/诊断判断
            "shared_hosts": shared_enabled,
            "total_hosts": (len(names) + len(shared_enabled)) if enabled else 0,
            # 运行期双写复核（set_items_enabled）：不一致说明需重启生产才收敛
            "runtime_applied": res.get("runtime_applied") or [],
            "runtime_failed": res.get("runtime_failed") or [],
            "runtime_restarted": res.get("runtime_restarted") or [],
            # P3：停用方向的运行期补救 + 兜底收敛（UI/诊断可据此判断"是否真的停了/是否重启过生产"）
            "runtime_retried_stop": res.get("runtime_retried_stop") or [],
            "converged_by_restart": bool(res.get("converged_by_restart")),
            "runtime_still_down": res.get("runtime_still_down") or [],
            "runtime_still_up": res.get("runtime_still_up") or [],
            "runtime_mismatch": res.get("runtime_mismatch") or []}


def active_categories() -> list[str]:
    """当前处于活动状态的管道类别（许可预算 keep 名单）。"""
    cats = [str(r.get("category")) for r in repository.list_json(GLOBAL)
            if r.get("status") == "active" and r.get("category")]
    return list(dict.fromkeys(cats))


def existing_pipelines_brief(limit: int = 20) -> list[dict]:
    """已存在管道的事实清单（注入 Agent B 上下文；不替代 AI 决策）。

    2026-09-19（P0a）补强：把**业务身份**（source_id/target_id）、既有 `design_skill`、
    `mapping_ids`、`signature`、生成次数一并给出 —— 让 LLM 对同一 (源,目标) **复用同一 Skill/组件编排**，
    避免"两次提交生成两套 SQL-SOAP"；但身份与归属**仍由平台决定**（LLM 只建议 skill）。
    """
    brief: list[dict] = []
    for rec in list_instances()[:limit]:
        brief.append({
            "id": rec.get("id"), "name": rec.get("name"),
            "category": rec.get("category"),
            "source_type": rec.get("source_type"), "target_type": rec.get("target_type"),
            "source_id": rec.get("source_id"), "target_id": rec.get("target_id"),
            "design_skill": rec.get("design_skill"), "status": rec.get("status"),
            "components": rec.get("component_names") or [],
            "mapping_ids": rec.get("mapping_ids") or [],
            "signature": rec.get("signature") or "",
            "generation_count": int(rec.get("generation_count") or 0),
            "identity_note": ("同一 (source_id,target_id) 恒为同一条管道；重复生成只更新不新增"
                              "（design_skill 变更记入 skill_history）"),
        })
    return brief


def set_applied_signature(pid: str, signature: str) -> bool:
    """记录"本次实际应用到 Production 的合并签名"（P3：全量未变更 → 跳过重渲染/重启的判断依据）。"""
    rec = get_instance(pid)
    if not rec:
        return False
    rec["applied_signature"] = str(signature or "")
    rec["updated_at"] = datetime.now().isoformat()
    repository.set_json(GLOBAL, pid, rec)
    return True


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
    skipped: list[str] = []      # 身份反查不到的类别（只记日志/回报，不登记幽灵实例）
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
        if not pid:
            # P0a：身份不完整（反查不到源数据源/目标）→ 不登记幽灵实例，显式记录（迁移场景常见）
            logger.warning("sync_from_production：类别 %s 反查不到业务身份（src=%s tgt=%s）→ 跳过登记",
                           cat, src_id, tgt_id)
            skipped.append(cat)
            continue
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
    logger.info("管道实例回填：新增 %d，更新 %d（类别 %s）%s", created, updated, sorted(by_cat),
                ("；跳过（身份反查不到）%s" % sorted(skipped)) if skipped else "")
    return {"created": created, "updated": updated, "instances": records,
            "skipped": sorted(skipped),
            "categories": sorted(by_cat), "production_items": prod_items}

