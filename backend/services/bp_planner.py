# -*- coding: utf-8 -*-
"""聚合 BP 的**计划层**（Plan → Execute 的 Plan 部分）。

为什么需要（2026-09-19 实测）：让 LLM **一次性整份写**聚合 BP（321 行 / completion **34.5k**，
其中大量是模型侧推理 token）→ 单次调用 30~60 分钟，上游多次 timeout，整批 9 组生成被拖死
（10768s 后显式失败）。改为：**先产出计划**（"怎么做"的结构化描述，输出 1~2k token，可审计/可复用/
可断点续跑），再**按方法逐个生成** + 每步编译/单测（见 `generated_bp.generate_from_plan`）。

计划 JSON（schema v1）：

    {
      "schema": 1,
      "class_name": "demo.SqlFhirPatientTxProcess",
      "parent": "demo.TransformProcess",
      "summary": "一句话说明编排思路",
      "methods": [
        {"name": "OnRequest",
         "signature": "Method OnRequest(request As %Library.Persistent, Output response As %Library.Persistent) As %Status",
         "purpose": "入口：读布局/映射 → 逐层派发子表查询 → 组装事务 Bundle → 发送 → 校验回执",
         "steps": ["读布局", "…"],
         "calls_parent": ["GetMappingFms"], "calls_self": ["ProcessPatient"],
         "verification": ["compile", "runtime"],
         "unit": {"args": ["..."], "expect_contains": "..."}}
      ],
      "verification": [{"step": "compile", "tool": "OBJ.Load", "expect": "ok"}],
      "risks": ["…"], "fallback": "整类生成（显式标注）"
    }

设计约束（与项目红线一致）：
- **AI 决策不变**：计划由 LLM 产出（方法编排/调用关系/验证策略都是 AI 决策）；
- 平台只做 **schema 校验**（结构完整性/命名唯一/父类 API 存在/规模上限），不替 AI 编排；
- 计划与进度落 `^demo.Config("bp_plan"|"bp_progress", <类名>)`，可审计、可续跑。
"""

import json
import logging
import re

from backend.services import iris_connector, llm_client

logger = logging.getLogger(__name__)

# 形式参数/返回类型**归一**表：LLM 常写 `String/Integer/…`（非 IRIS 类型名）→ 编译报
# `#5500 method formal argument type … is invalid`（实测 2026-09-19：骨架阶段就失败）。
# 平台只做**参数化归一**（不改变 AI 的方法编排决策），并在计划里留下 `_signature_fixes` 供审计。
_TYPE_FIX = {
    "string": "%String", "int": "%Integer", "integer": "%Integer", "boolean": "%Boolean",
    "object": "%Library.DynamicObject", "dynamicobject": "%Library.DynamicObject",
    "dynamicarray": "%Library.DynamicArray", "array": "%Library.DynamicArray",
    "persistent": "%Library.Persistent", "status": "%Status",
    "stream": "%Stream.GlobalCharacter", "float": "%Double", "double": "%Double",
    "decimal": "%Numeric", "numeric": "%Numeric", "list": "%ListOfDataTypes",
}


def normalize_signature(sig: str) -> tuple[str, list[str]]:
    """把签名里的类型名归一为 IRIS 类型；返回 (新签名, 修正记录)。"""
    fixes: list[str] = []
    out = str(sig or "")

    def _rep(m):
        typ = m.group(1)
        base = typ.split(".")[0].lower()
        if typ.startswith(("%", "Ens.")):
            return m.group(0)
        fixed = _TYPE_FIX.get(base) or _TYPE_FIX.get(typ.lower())
        if fixed:
            fixes.append(f"{typ}→{fixed}")
            return m.group(0).replace(typ, fixed, 1)
        return m.group(0)

    out = re.sub(r"\bAs\s+([A-Za-z][A-Za-z0-9_.]*)", _rep, out)
    return out, fixes


def signature_issues(sig: str) -> list[str]:
    """归一后仍非 IRIS 类型（不含 `%` / 非 `Ens.*`）→ 报问题（让计划显式改正）。"""
    bad = [m.group(1) for m in re.finditer(r"\bAs\s+([A-Za-z][A-Za-z0-9_.]*)", str(sig or ""))
           if not m.group(1).startswith(("%", "Ens."))]
    return [f"签名里的类型 {b} 不是 IRIS 类型（应为 %String/%Integer/%Library.DynamicObject/…）"
            for b in bad]


PLAN_SCHEMA = 1
PLAN_MAX_METHODS = 12          # 计划规模护栏（防止"计划"又变成一大坨代码）
PLAN_MAX_TOKENS = 12000        # 计划输出的 token 上限（含模型的推理 token；实测 4000 会被截断）


def _parent_api_ok(name: str, parent_api: set[str]) -> bool:
    """父类 API 名匹配（容忍大小写与可选的 `%` 前缀：AI 常把 `%GetParameter` 写成 `GetParameter`）。"""
    n = str(name or "").strip().lower()
    api = {str(p).strip().lower() for p in parent_api}
    return n in api or n.lstrip("%") in {a.lstrip("%") for a in api}


def plan_schema_issues(plan: dict | None, *, bp_class: str, parent_api: set[str]) -> list[str]:
    """校验计划 schema（结构/命名唯一/父类 API 存在/规模上限）。返回问题清单（空=通过）。"""
    if not isinstance(plan, dict):
        return ["计划不是对象（LLM 未产出 plan）"]
    issues: list[str] = []
    if not plan.get("class_name"):
        issues.append("计划缺少 class_name")
    elif str(plan["class_name"]) != bp_class:
        issues.append(f"class_name 必须为 {bp_class}（当前 {plan['class_name']}）")
    methods = plan.get("methods")
    if not isinstance(methods, list) or not methods:
        issues.append("计划缺少 methods（方法清单）")
        return issues
    if len(methods) > PLAN_MAX_METHODS:
        issues.append(f"方法数 {len(methods)} 超出上限 {PLAN_MAX_METHODS}")
    names = [str(m.get("name") or "").strip() for m in methods
             if isinstance(m, dict) and str(m.get("name") or "").strip()]
    # 两趟：先收集**全部**方法名（允许"引用后面才定义的方法"），再校验引用关系
    for i, m in enumerate(methods):
        if not isinstance(m, dict):
            issues.append(f"methods[{i}] 不是对象")
            continue
        name = str(m.get("name") or "").strip()
        if not name:
            issues.append(f"methods[{i}] 缺少 name")
            continue
        if names.count(name) > 1 and f"方法名重复: {name}" not in issues:
            issues.append(f"方法名重复: {name}")
        if not str(m.get("purpose") or "").strip():
            issues.append(f"方法 {name} 缺少 purpose（用途说明）")
        _sig = str(m.get("signature") or "")
        if _sig:
            issues.extend(signature_issues(_sig))
    for m in methods:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        for callee in (m.get("calls_parent") or []):
            if not _parent_api_ok(str(callee), parent_api):
                issues.append(f"方法 {name} 声明调用父类 API {callee}，但父类没有该方法"
                              f"（请用清单里的原名，如 %GetParameter 要带百分号）")
        for callee in (m.get("calls_self") or []):
            if str(callee) not in names:
                issues.append(f"方法 {name} 声明调用本类方法 {callee}，但清单里没有它")
    if "OnRequest" not in names:
        issues.append("计划必须包含入口方法 OnRequest")
    return issues


def normalize_plan(plan: dict) -> list[str]:
    """把计划里各方法签名的类型归一为 IRIS 类型（平台参数化），返回修正记录（审计用）。"""
    fixes: list[str] = []
    for m in (plan.get("methods") or []):
        if not isinstance(m, dict):
            continue
        sig = str(m.get("signature") or "")
        if not sig:
            continue
        norm, fx = normalize_signature(sig)
        if fx:
            m["signature"] = norm
            fixes.extend(f"{m.get('name')}: {x}" for x in fx)
    return fixes


def plan_hash(plan: dict) -> str:
    """计划指纹（方法名+签名+用途）：用于判断"是否同一份计划"——同一份即可**断点续跑**。"""
    import hashlib
    parts = [f"{m.get('name')}|{m.get('signature')}|{str(m.get('purpose'))[:40]}"
             for m in (plan.get("methods") or []) if isinstance(m, dict)]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def ensure_progress_scope(bp_class: str, plan: dict) -> bool:
    """进度作用域：计划指纹一致 → 保留进度（**续跑**）；否则清空并写入新指纹。

    返回 True 表示"检测到可续跑"（已有同计划的部分方法 ok）。
    """
    h = plan_hash(plan)
    prev = str(load_method_progress(bp_class).get("_plan_hash") or "")
    if prev != h:
        clear_method_progress(bp_class)
        save_method_progress(bp_class, "_plan_hash", h)
        return False
    done = [k for k, v in load_method_progress(bp_class).items()
            if not str(k).startswith("_") and v == "ok"]
    return bool(done)


def build_plan(*, bp_class: str, parent_api: set[str], layout: dict | None,
               mappings: list[dict] | None, components: list[dict] | None,
               design_skill: str = "", facts: dict | None = None) -> dict:
    """调 LLM 产出计划 → schema 校验 → 落库。失败抛 `llm_client.AgentError`（显式，不静默）。

    截断自愈：实测 `finish_reason=length`（含模型推理 token 时 4k 上限不够）→ **自动放大一次**
    上限重试（仍是同一条 AI 链路，不做规则顶替；第二次仍失败则显式报错）。
    """
    cap = PLAN_MAX_TOKENS
    for attempt in (1, 2):
        try:
            raw = llm_client.plan_bp_source(
                layout=layout or {}, mappings=mappings or [], components=components or [],
                design_skill=design_skill, bp_class=bp_class, parent_api=sorted(parent_api),
                facts=facts or {}, max_tokens=cap)
            break
        except llm_client.AgentError as exc:
            _msg = str(exc)
            if attempt == 1 and ("截断" in _msg or "finish_reason=length" in _msg):
                logger.warning("计划输出被截断（上限 %d）→ 放大上限重试一次", cap)
                cap = cap * 2
                continue
            raise
    plan = raw.get("plan") if isinstance(raw, dict) else None
    plan = dict(plan) if isinstance(plan, dict) else {}
    plan.setdefault("schema", PLAN_SCHEMA)
    plan.setdefault("class_name", bp_class)
    plan.setdefault("parent", "demo.TransformProcess")
    _fixes = normalize_plan(plan)          # 类型归一（String→%String 等），可审计
    if _fixes:
        plan["_signature_fixes"] = _fixes
        logger.info("计划签名类型已归一（%d 处）：%s", len(_fixes), "；".join(_fixes[:4]))
    issues = plan_schema_issues(plan, bp_class=bp_class, parent_api=parent_api)
    if issues:
        raise llm_client.AgentError("BP 计划不合规：" + "；".join(issues[:4]))
    saved = save_plan(bp_class, plan)
    logger.info("BP 计划已生成（%d 个方法，%d 字符，%s）",
                len(plan.get("methods") or []), len(json.dumps(plan, ensure_ascii=False)), saved)
    return plan


def save_plan(bp_class: str, plan: dict) -> str:
    """落 `^demo.Config("bp_plan", <类名>)`（IRIS 侧，供审计与复用）。"""
    try:
        return str(iris_connector.class_method_value(
            "demo.PipelineQuery", "SaveBpPlan", bp_class,
            json.dumps(plan, ensure_ascii=False)) or "")
    except Exception as exc:  # noqa: BLE001 - 落库失败不阻断（计划仍在内存里用）
        logger.warning("BP 计划落库失败: %s", exc)
        return f"failed:{exc}"


def load_plan(bp_class: str) -> dict | None:
    """读回计划（空=None）。"""
    try:
        raw = iris_connector.class_method_value("demo.PipelineQuery", "GetBpPlan", bp_class)
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def save_method_progress(bp_class: str, method: str, status: str, note: str = "") -> None:
    """记录方法级状态（ok/failed/pending）——**断点续跑**的依据。"""
    try:
        iris_connector.class_method_value("demo.PipelineQuery", "SaveBpMethodProgress",
                                          bp_class, method, status, note[:200])
    except Exception as exc:  # noqa: BLE001
        logger.warning("BP 方法进度落库失败（%s.%s=%s）: %s", bp_class, method, status, exc)


def load_method_progress(bp_class: str) -> dict:
    """读回 {方法: 状态}（空 dict = 无进度）。"""
    try:
        raw = iris_connector.class_method_value("demo.PipelineQuery", "GetBpMethodProgress", bp_class)
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def clear_method_progress(bp_class: str) -> None:
    """清空进度（重新生成时调用）。"""
    try:
        iris_connector.class_method_value("demo.PipelineQuery", "ClearBpMethodProgress", bp_class)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清空 BP 方法进度失败: %s", exc)

