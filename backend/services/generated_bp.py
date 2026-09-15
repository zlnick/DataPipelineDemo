# -*- coding: utf-8 -*-
"""Agent 生成的 BP（generated_bp）静态准入 + IRIS 落地编译 + 失败回 feed。

边界（与红线一致）：
- BP 类源码由数据管道设计 Agent（LLM）在生成 SQL→FHIR 管道时产出，平台不预置任何 BP；
- 平台只做：① 静态安全/结构准入（包名白名单、禁 xecute/$zf/CSP 操作、类名契约）；
  ② 落地为 /dur/generated/<类名>.cls 并 Load 编译（demo.PipelineQuery.AddGeneratedBpSource）；
  ③ 编译失败把 IRIS 错误文本回 feed Agent 修复（≤ max_rounds 轮）；
  ④ 仍失败 = 面向用户明确失败（不静默用规则/预置替换）。
"""

import json
import logging

from backend.services import iris_connector, llm_client

logger = logging.getLogger(__name__)

BP_CLASS_NAME = "demo.SqlFhirPatientTxProcess"
BP_SHORT_NAME = "SqlFhirPatientTxProcess"
_FORBIDDEN = ["xecute", "$zf", "EnsPortal", "Security.Applications", "IRISApplication"]


def static_check_generated_bp(bp: dict | None) -> list[str]:
    """BP 静态准入检查，返回错误清单（空=通过）。"""
    errors: list[str] = []
    if not isinstance(bp, dict):
        return ["缺少 generated_bp 输出（BP 应由 Agent 生成）"]
    name = str(bp.get("class_name") or "")
    source = str(bp.get("source") or "")
    if name != BP_CLASS_NAME:
        errors.append(f"class_name 必须为 {BP_CLASS_NAME}（当前: {name or '空'}）")
    if not source.strip():
        errors.append("source 为空：Agent 未产出 BP 类源码")
    else:
        if BP_SHORT_NAME not in source or "Extends Ens.BusinessProcess" not in source:
            errors.append("source 必须声明 Class demo.SqlFhirPatientTxProcess Extends Ens.BusinessProcess")
        for tok in _FORBIDDEN:
            if tok in source:
                errors.append(f"source 含被禁内容: {tok}")
        if source.count("}") < 1 or "OnRequest" not in source:
            errors.append("source 缺少 OnRequest（BP 入口）或类结构不完整")
        # 中文编码硬约束：GenericMessage 的 body 是 %RawString（原样字节），
        # 用字符流会在发送时逐字符按单字节输出 → 中文变 "?"（必须 UTF-8 字节流）
        if "EnsLib.HTTP.GenericMessage" in source and "%Stream.GlobalBinary" not in source:
            errors.append("HTTP 发送必须用 UTF-8 字节流：Set tS=##class(%Stream.GlobalBinary).%New() "
                          "Do tS.Write($ZCONVERT(tJSON,\"O\",\"UTF8\")) Set tMsg.Stream=tS"
                          "（GenericMessage body 是 %RawString，字符流会让中文变 '?'）")
    return errors


def get_generated_source(class_name: str = BP_CLASS_NAME) -> str:
    """读取 Agent 生成的 BP 类当前源码（经 IRIS 侧读取 /dur/generated）。"""
    try:
        return str(iris_connector.class_method_value(
            "demo.PipelineQuery", "GetGeneratedBpSource", class_name) or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取生成 BP 源码失败: %s", exc)
        return ""


def extract_method(source: str, name: str) -> str:
    """从类源码中提取指定方法文本（Method <name>( … 到匹配的结束花括号）。"""
    if not source or not name:
        return ""
    lines = source.splitlines()
    start = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("Method " + name + "(") or s.startswith("ClassMethod " + name + "("):
            start = i
            break
    if start is None:
        return ""
    depth = 0
    started = False
    for j in range(start, len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        if "{" in lines[j]:
            started = True
        if started and depth <= 0:
            return "\n".join(lines[start:j + 1])
    return "\n".join(lines[start:])


def replace_method(source: str, name: str, new_src: str) -> str:
    """把类源码中的指定方法替换为 new_src（找不到则抛出，交由调用方处理）。"""
    old = extract_method(source, name)
    if not old:
        raise ValueError(f"类中未找到方法 {name}，无法替换")
    return source.replace(old, new_src.strip(), 1)


def apply_method_updates(updates: list[dict], class_name: str = BP_CLASS_NAME) -> dict:
    """方法级受控替换：读取当前类 → 逐个替换 Agent 给出的方法 → 写回编译。"""
    if not isinstance(updates, list) or not updates:
        return {"ok": False, "message": "method_updates 为空"}
    source = get_generated_source(class_name)
    if not source.strip():
        return {"ok": False, "message": "当前生成类源码为空（无法做方法替换）"}
    applied = []
    for up in updates:
        if not isinstance(up, dict):
            return {"ok": False, "message": "method_updates 元素必须是对象"}
        name = str(up.get("name") or "").strip()
        body = str(up.get("source") or "").strip()
        if not name or not body:
            return {"ok": False, "message": "method_updates 元素必须含 name 与 source"}
        if ("Method " + name + "(") not in body and ("ClassMethod " + name + "(") not in body:
            return {"ok": False, "message": f"方法 {name} 的 source 未包含方法声明"}
        try:
            source = replace_method(source, name, body)
            applied.append(name)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
    compiled = _write_and_compile(class_name, source)
    return {**compiled, "applied": applied}


def compile_generated_bp(bp: dict) -> dict:
    """把 generated_bp.source（整类）落地 /dur/generated 并 Load 编译。"""
    name = str(bp.get("class_name") or BP_CLASS_NAME)
    source = str(bp.get("source") or "")
    return _write_and_compile(name, source)


def save_last_good(source: str) -> str:
    """把「编译/验证通过的 Agent BP 源码」存档到 ^demo.Config("bp","last_good")。

    用途：下一次生成若 Agent 产出的 BP 编译或运行失败，平台可自动回滚到这份可用版本，
    保证「新增/追加管道」不会把已有管道的数据通路弄坏。
    """
    source = str(source or "")
    if not source.strip():
        return "empty"
    try:
        return str(iris_connector.class_method_value(
            "demo.PipelineQuery", "SaveBpLastGood", source) or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("BP last_good 存档失败: %s", exc)
        return f"failed:{exc}"


def restore_last_good(class_name: str = BP_CLASS_NAME) -> str:
    """回滚到已存档的 BP 源码并重新编译。返回 restored:<len> / none / failed:...。"""
    try:
        src = str(iris_connector.class_method_value(
            "demo.PipelineQuery", "GetBpLastGood") or "")
    except Exception as exc:  # noqa: BLE001
        return f"failed:{exc}"
    if not src.strip():
        return "none"
    res = _write_and_compile(class_name, src)
    if res.get("ok"):
        logger.warning("已回滚到上一版通过验证的 BP（agent_bp_rolled_back，%d 字符）", len(src))
        return f"restored:{len(src)}"
    return f"failed:{res.get('message')}"


def _write_and_compile(class_name: str, source: str) -> dict:
    """写回 /dur/generated 并 Load 编译（复用 IRIS 侧落地方法）。"""
    data = iris_connector.class_method_value(
        "demo.PipelineQuery", "AddGeneratedBpSource", class_name, source)
    try:
        return json.loads(data or '{"ok": false, "message": "IRIS 无返回"}')
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": f"IRIS 返回解析失败: {data}"}


def repair_from_runtime_errors(*, mappings: list[dict], source_type: str, target_type: str,
                               available_components: list[dict], errors_text: str,
                               source_runtime: dict | None = None,
                               target_runtime: dict | None = None,
                               max_rounds: int = 2) -> dict:
    """运行期 BP 代码错误 → 方法级增量修复（Agent 只输出需修复方法，平台受控替换后编译）。

    与 `generate_and_compile_bp` 的区别：这里给 Agent 的是**运行期错误文本**，
    并要求以 `method_updates` 形式只返回需要修复的方法（不重写整类）。
    """
    feedback = ("运行期 BP 报错，请只输出需要修复的方法（generated_bp.method_updates，"
                "不要输出整类 source）：\n" + str(errors_text)[:2000])
    for attempt in range(1, max_rounds + 1):
        try:
            r = llm_client.recommend_pipeline(
                mappings, source_type=source_type, target_type=target_type,
                available_components=available_components,
                source_runtime=source_runtime, target_runtime=target_runtime,
                bp_compile_feedback=feedback, bp_patch_mode=True)
        except llm_client.AgentError as exc:
            return {"ok": False, "attempts": attempt, "message": f"Agent 调用失败: {exc}"}
        bp = r.get("generated_bp") or {}
        ups = bp.get("method_updates") or []
        if not ups:
            feedback = ("上一轮未返回 method_updates（每项需含 name 与 source）。"
                        "请只输出需要修复的方法源码。")
            continue
        res = apply_method_updates(ups, str(bp.get("class_name") or BP_CLASS_NAME))
        if res.get("ok"):
            return {"ok": True, "attempts": attempt, "applied": res.get("applied"),
                    "message": res.get("message", ""), "ai": {"driven": True}}
        feedback = ("上一轮方法替换后编译失败，请修正该方法：\n"
                    + str(res.get("message"))[:800])
    return {"ok": False, "attempts": max_rounds,
            "message": f"method_updates 修复在 {max_rounds} 轮内未通过编译"}


def generate_and_compile_bp(*, mappings: list[dict], source_type: str, target_type: str,
                            available_components: list[dict],
                            source_runtime: dict | None = None,
                            target_runtime: dict | None = None,
                            max_rounds: int = 3) -> dict:
    """完整循环：Agent 产出 generated_bp → 静态准入 → 编译 → 失败回 feed ≤ max_rounds。

    返回 {"ok": bool, "agent": "数据管道设计Agent", "message": str,
          "attempts": int, "ai": {"driven": true}, ...}
    """
    feedback = None
    attempts = 0
    for attempt in range(1, max_rounds + 1):
        attempts = attempt
        try:
            result = llm_client.recommend_pipeline(
                mappings, source_type=source_type, target_type=target_type,
                available_components=available_components,
                source_runtime=source_runtime, target_runtime=target_runtime,
                bp_compile_feedback=feedback)
        except llm_client.AgentError as exc:
            return {"ok": False, "attempts": attempts, "agent": "数据管道设计Agent",
                    "message": f"Agent B 调用失败: {exc}", "ai": {"driven": True}}
        bp = result.get("generated_bp") if isinstance(result, dict) else None
        static = static_check_generated_bp(bp)
        if static:
            feedback = "上一轮 generated_bp 静态准入失败：" + "；".join(static)
            logger.warning("Agent BP 第 %d 轮静态准入失败: %s", attempt, feedback)
            continue
        compiled = compile_generated_bp(bp)
        if compiled.get("ok"):
            logger.info("Agent BP 生成并编译成功（第 %d 轮，class=%s）", attempt, BP_CLASS_NAME)
            return {"ok": True, "attempts": attempts, "agent": "数据管道设计Agent",
                    "class_name": BP_CLASS_NAME,
                    "message": f"Agent 生成的 BP 已编译（{BP_CLASS_NAME}）",
                    "ai": {"driven": True, "generated_bp": True},
                    "compile": compiled.get("message")}
        feedback = "上一轮 generated_bp 编译失败（IRIS 返回）：" + str(compiled.get("message") or "")
        logger.warning("Agent BP 第 %d 轮编译失败: %s", attempt, feedback)
    return {"ok": False, "attempts": attempts, "agent": "数据管道设计Agent",
            "message": f"Agent 生成的 BP 在 {max_rounds} 轮内未通过编译（已面向用户明确失败，"
                       f"不静默替换）；最后错误：{feedback}",
            "ai": {"driven": True, "generated_bp": True}}
