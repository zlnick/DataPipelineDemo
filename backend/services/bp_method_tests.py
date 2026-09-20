# -*- coding: utf-8 -*-
"""聚合 BP 的**方法级纯函数单测**（Plan → Execute 的第 ③ 道闸）。

为什么：整类生成时，机制性错误（JSON 组装、UUID 生成、行→数组归一、条目构造）往往要到**运行期**
（甚至表现为"目标侧零落地"）才暴露。把**纯函数**方法（输入字符串/JSON、输出字符串/JSON、不依赖
业务主机上下文）在**生成期**用合成输入调用一次并断言，可把这类问题前移。

边界（诚实标注）：
- 依赖宿主上下文的方法（`..SendRequestSync` / `%Process` / `..%ConfigName`）**无法**这样单测 →
  计划里 `unit` 给 null，本模块返回 `skipped`（**不假装测过**）；
- 因此计划应把"可独立验证的纯逻辑"写成 **ClassMethod**（无实例依赖），便于平台单测。
"""

import json
import logging

from backend.services import iris_connector

logger = logging.getLogger(__name__)


def run_unit(bp_class: str, method: str, unit: dict | None) -> dict:
    """按计划给的 unit 样例调用方法并断言，返回 {ok, skipped?, message, actual?}。

    unit 形如 {"args": ["<json 或字符串>", ...], "expect_contains": "…"}；
    缺样例 → `skipped=True`（既不算通过也不算失败，但在结果里**显式可见**）。
    """
    if not isinstance(unit, dict) or not unit.get("args"):
        return {"ok": True, "skipped": True,
                "message": "计划未提供 unit 样例（该方法未做单测）"}
    args = unit["args"]
    expect = str(unit.get("expect_contains") or "")
    try:
        # 经 IRIS 侧 `InvokeMethodJson` 调用：按方法**形参声明类型**把 JSON 文本转成
        # %DynamicObject/%DynamicArray 或原样字符串（与运行期真实调用一致；否则形参是
        # 动态类型的方法会取不到值，实测 2026-09-19 `ExtractMrn` 误判失败）。
        out = iris_connector.class_method_value(
            "demo.PipelineQuery", "InvokeMethodJson", bp_class, method,
            json.dumps(args, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 调用失败即该步未通过（含实例方法/参数类型不匹配）
        return {"ok": False, "message": f"单测调用异常：{str(exc)[:220]}"}
    actual = "" if out is None else str(out)
    if expect and expect not in actual:
        return {"ok": False, "message": f"断言失败：期望包含 {expect!r}，实际 {actual[:220]!r}",
                "actual": actual[:220]}
    return {"ok": True, "message": f"单测通过（输出 {len(actual)} 字符）", "actual": actual[:220]}


def units_summary(steps: dict) -> dict:
    """汇总执行结果里的单测情况：passed / skipped / failed（供响应与日志审计）。"""
    passed, skipped, failed = [], [], []
    for name, st in (steps or {}).items():
        u = (st or {}).get("unit") or {}
        if u.get("skipped"):
            skipped.append(name)
        elif u.get("ok"):
            passed.append(name)
        elif u:
            failed.append(name)
    return {"unit_passed": passed, "unit_skipped": skipped, "unit_failed": failed}
