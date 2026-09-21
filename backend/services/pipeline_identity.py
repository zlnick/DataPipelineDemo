# -*- coding: utf-8 -*-
"""管道**身份与签名**的唯一实现（单/多管道共用）—— 自 `routes/pipelines.py` 迁出。

## 为什么独立成模块（2026-09-20：外部评审 + 本会话缺陷复盘）

`routes/pipelines.py` 曾同时存在**单管道**与**多管道**两条**平行管线**，两者各自实现同一套
「身份 / 签名 / 冻结定义」规则 → 一旦口径漂移就是**静默缺陷**：

- **S2**：入库签名 ≠ 比较签名（两处算法不同）→ 永远判"已变更" → 反复重跑 Agent B；
- **P0a**：缺业务键时退化成 `PIPE_<skill>` 幽灵身份 → 同一管道登记成**两套实例**（"两套 SQL-SOAP"）。

本模块把这些规则收敛为**纯函数 + 唯一实现**：两条路径只能调用同一份，结构上不再可能漂移。
（评审建议 Step 1；离线回归 `test_incremental_pipeline.py` A/B/H 组即为验收。）

## 设计约束

- **纯函数、无 IRIS 副作用**：唯一例外 `running_items()` 读 Production 组件事实，读取失败返回 `{}`
  （不阻断生成，退化为全量生成）；
- **只依赖 services 同层模块**（`pipeline_instances` / `pipeline_validator`），**不反向依赖 `routes/`**
  （否则形成循环导入）；
- 迁移期间 `routes/pipelines.py` 以**别名**导入（`_inc_input_signature = inc_input_signature` 等），
  保持既有调用点与离线回归（`PL._inc_input_signature` / `PL._stored_definition_complete` …）不变。
"""

import logging

from backend.services import pipeline_instances, pipeline_validator

logger = logging.getLogger(__name__)

# 目标侧业务主机（BO）前缀：存储定义里**必须**有它，否则这条管道的转换结果无处投递
BO_PREFIX = {"DB": ("SQLOp_",), "SOAP": ("SOAPOp_",), "FHIR": ("HTTPOp_", "HTTPOperation")}


def group_identity(g: dict) -> tuple[str, str]:
    """组的**业务身份** = (源数据源 id, 目标 id)（与 LLM 选择的 skill 无关）。"""
    return (str(g.get("source_id") or "").strip(), str(g.get("target_id") or "").strip())


def dedup_groups_by_identity(groups: list[dict]) -> tuple[list[dict], list[dict]]:
    """按业务身份 `(source_id, target_id)` 合并重复组（同一管道提交两次 → 只生成一次）。

    合并规则：映射按 id 并集去重；其余取先到者的配置（同身份的正常情况下配置一致）。
    返回 (去重后的组, 被合并掉的组摘要)。
    """
    out: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    merged: list[dict] = []
    for g in groups or []:
        key = group_identity(g)
        prev = index.get(key)
        if prev is None:
            index[key] = g
            out.append(g)
            continue
        seen = {str((m or {}).get("id")) for m in (prev.get("mappings") or []) if isinstance(m, dict)}
        added = []
        for m in (g.get("mappings") or []):
            if isinstance(m, dict) and str(m.get("id")) not in seen:
                prev.setdefault("mappings", []).append(m)
                added.append(str(m.get("id")))
        merged.append({"identity": list(key), "merged_mapping_ids": added,
                       "kept_mapping_ids": [str((m or {}).get("id")) for m in (prev.get("mappings") or [])]})
        logger.warning("增量生成：检测到同身份重复组（源=%s 目标=%s）→ 合并为一条管道（并入映射 %s）",
                       key[0], key[1], added or "无新增")
    return out, merged



def inc_input_signature(g: dict) -> str:
    """组的**入参签名**（生成前即可算）：身份 + 类型 + 映射内容 + 源/目标运行契约关键位。

    ⚠ **唯一实现**：单管道与多管道两条入口都必须调用本函数（`routes/pipelines.py` 以别名导入）。
    S2 缺陷（入库签名 ≠ 比较签名 → 永远判"已变更"）的根因即为"两处各算一次"，本函数即其结构性修复。
    ⚠ 调用方必须在**改动上下文之前**取值并固定下来：多管道路径的 Agent B 分支会 `pop("_rt_src"/"_rt_tgt")`，
    之后再算会得到"空契约"签名而与入库值不一致。
    """
    _rt_src = g.get("_rt_src") or {}
    _rt_tgt = g.get("_rt_tgt") or {}
    _conn_s = (_rt_src.get("connection") or {}) if isinstance(_rt_src, dict) else {}
    _conn_t = (_rt_tgt.get("connection") or {}) if isinstance(_rt_tgt, dict) else {}
    return pipeline_instances.input_signature(
        g.get("source_id"), g.get("target_id"), g.get("mappings"),
        g.get("source_type") or "", g.get("target_type") or "",
        extra={
            "src": {k: str(_conn_s.get(k) or "") for k in ("dsn", "jdbc_url", "endpoint", "type")},
            "tgt": {k: str(_conn_t.get(k) or "") for k in ("dsn", "jdbc_url", "base_url", "endpoint")},
        })


def running_items() -> dict[str, dict]:
    """当前 Production 组件（名字 → 记录，含 enabled/category）——供增量复用与运行态保留。"""
    try:
        return {str(i.get("name")): i for i in (pipeline_validator.production_items() or [])}
    except Exception as exc:  # noqa: BLE001 - 读取失败按"拿不到"处理（不阻断生成）
        logger.warning("读取 Production 组件失败（增量判定退化为全量生成）: %s", exc)
        return {}


def frozen_defs_from_instance(rec: dict) -> list[dict]:
    """从实例记录取**可渲染的冻结定义**（渲染成功参数 + **改名前**名字）。

    优先 `render_components`（新字段，参数与名字都对）；历史实例退 `components`（渲染后名）→
    把 `__{类别}` 后缀剥掉还原为改名前形态，避免复用渲染时出现二次后缀。
    """
    cat = str(rec.get("category") or "")
    suf = "__" + pipeline_instances.slug(cat) if pipeline_instances.slug(cat) else ""
    out: list[dict] = []
    for c in (rec.get("render_components") or rec.get("components") or []):
        if not isinstance(c, dict):
            continue
        nm = str(c.get("name") or "")
        if suf and nm.endswith(suf):
            nm = nm[:-len(suf)]
        out.append({**c, "name": nm})
    return out


def stored_definition_complete(components: list[dict] | None,
                               target_type: str | None) -> tuple[bool, str]:
    """**存储定义完整性**守卫：能不能拿这份定义去复用（不重跑 Agent B）？

    为什么必须查（2026-09-20 实测）：某条历史实例的存储定义只有「源 BS + 转换 BP」，
    **缺目标 BO**（`SOAPOp_*`）——直接复用会把不完整管道渲染进 Production
    （源能收到数据、转换结果无处投递，运行期才发现，属静默缺陷）。凡不完整 → 不算"未变更"，
    交回正常生成链路（重新问 Agent B / Skill 重建完整定义 = 自愈）。
    """
    names = [str(c.get("name") or "") for c in (components or []) if isinstance(c, dict)]
    if not names:
        return False, "存储定义为空"
    # 可渲染性（2026-09-20 加）：**缺 className 的组件不可渲染** —— 若曾把"Agent 原始组件"
    # （无 className/settings）误存进定义，沿用它会渲染出不合规拓扑（拓扑校验会 500）。
    for c in (components or []):
        if not isinstance(c, dict):
            continue
        if not str(c.get("className") or "").strip():
            return False, "组件 %s 缺 className（不可渲染）" % (c.get("name") or c.get("type") or "?")
    prefs = BO_PREFIX.get(str(target_type or "").upper())
    if not prefs:
        return True, ""
    if not any(n.startswith(prefs) for n in names):
        return False, "存储定义缺目标业务主机（%s）" % "/".join(prefs)
    return True, ""
