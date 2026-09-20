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
import re

from backend.services import iris_connector, llm_client

logger = logging.getLogger(__name__)

BP_CLASS_NAME = "demo.SqlFhirPatientTxProcess"
BP_SHORT_NAME = "SqlFhirPatientTxProcess"
# 聚合 BP 的父类（2026-09-18 起）：继承 demo.TransformProcess 才能用继承来的实例方法
# `..BuildFHIRResource` / `..TermCoding`（后者经**共享 BO** demo.TerminologyOperation 取判码）。
BP_PARENT_CLASS = "demo.TransformProcess"
# 父类（demo.TransformProcess）+ IRIS 业务主机基类（Ens.BusinessProcess 等）提供的**稳定 API**：
# AI 生成的聚合 BP 可直接 `..X(...)` 调用，无需（也不应）在自己的类里重复声明。
# 事实源 = `iris/src/demo/TransformProcess.cls` 的公开方法（新增父类公开方法时同步登记；
# `tools/test_bp_static_admission.py` 会核对清单与类文件一致）+ Ensemble 宿主常用方法。
PARENT_API = {
    # demo.TransformProcess
    "BuildFHIRResource", "LeafValue", "TermCoding", "TermSystem", "SetPath", "CodeableConcept",
    "Reference", "GetMappingFms", "ToFmsJSON", "PackFHIRHTTP", "PackSOAPRequest",
    "UnpackSource", "ResolveSource", "FillEntity", "IsScalarType",
    # 通用聚合引擎（2026-09-19 下移到父类：薄适配 BP 直接调用）
    "ProcessFHIRBundle", "ProcessChildLevel", "PackFHIRBundle", "BundleEntry",
    "InjectBundleRefs", "BundleHttpOk", "MakeResourceUuid",
    # Ensemble 业务主机（Ens.BusinessProcess / Ens.Host）
    "SendRequestSync", "SendRequestAsync", "SendRequest", "DeferResponse", "Reply", "SetTimer",
    "OnRequest", "OnResponse", "OnMessage", "%New", "%Save", "%OpenId", "%DeleteId",
    "%GetParameter", "%GetSetting", "%SetSetting", "%Validate",
}
_FORBIDDEN = ["xecute", "$zf", "EnsPortal", "Security.Applications", "IRISApplication"]
# 形参/返回值里**不存在**的类名 → 正确替代（实测 2026-09-18：#5373 Class '%Library.Object'
# … does not exist）。注意 `(("a", "b"))` 是 2 元素元组而非元组的元组，故用 dict 表达映射。
_BAD_FORMALSPEC_TYPES = {
    "%Library.Object": ("%Library.Persistent / %Library.DynamicObject / %Library.DynamicArray / "
                        "%Stream.GlobalCharacter / %String"),
}


def _strip_strings_comments(line: str) -> str:
    """去掉行内字符串字面量与注释（ObjectScript `;` / `//`），供括号计数与关键字扫描使用。"""
    out: list[str] = []
    i, in_str = 0, False
    while i < len(line):
        ch = line[i]
        if in_str:
            if ch == '"':
                if line[i + 1:i + 2] == '"':      # "" 转义
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == ";" or line[i:i + 2] == "//":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def try_block_quit_value_lines(source: str) -> list[int]:
    """返回「在 TRY 块内（跨行或**单行**）出现带参数 `Quit`」的行号（编译报 #1043）。

    保守实现（宁可漏报、不误报）：
    - 跨行 `Try {`…`}` 区块：按花括号深度跟踪（字符串字面量与注释不参与计数）；
    - 单行 `Try { … } Catch …`：仅当块体**不含嵌套花括号**时判定（含嵌套即放过）；
    - 只有裸 `Quit`（无参数、无后置条件值）才是合法返回，不计入。
    """
    out: list[int] = []
    # ① 单行 Try 块（body 不含嵌套花括号）——2026-09-18 实测该形态曾漏过静态准入
    for m in re.finditer(r"\bTry\s*\{([^{}]*)\}", str(source or ""), re.I | re.S):
        for qm in re.finditer(r"\bQuit\b([^\n]*)", m.group(1), re.I):
            rest = qm.group(1).strip()
            if rest.startswith(":"):
                rest = rest.split(None, 1)[1].strip() if " " in rest else ""
            if rest:
                line_no = str(source or "").count("\n", 0, m.start() + qm.start()) + 1
                if line_no not in out:
                    out.append(line_no)
    # ② 跨行 Try 块
    stack: list[int] = []
    depth = 0
    for idx, raw in enumerate(str(source or "").splitlines(), 1):
        line = _strip_strings_comments(raw)
        opens, closes = line.count("{"), line.count("}")
        if re.search(r"\bTry\b\s*\{", line, re.I) and opens > closes:
            stack.append(depth + opens - closes)      # 该 Try 区块闭合后的深度
        if stack:
            m = re.search(r"\bQuit\b(.*)$", line, re.I)
            if m:
                rest = m.group(1).strip()
                if rest.startswith(":"):              # 后置条件 `Quit:cond [值]`
                    rest = rest.split(None, 1)[1].strip() if " " in rest else ""
                if rest and idx not in out:           # 只有裸 `Quit` 才是合法返回
                    out.append(idx)
        depth += opens - closes
        while stack and depth < stack[-1]:
            stack.pop()
    return sorted(out)


def undefined_helper_calls(source: str) -> list[str]:
    """返回「调用了但本类未定义、父类也没有」的 `..X(` 助手方法名（2026-09-18 实测缺陷）。

    场景：Agent 生成的 BP 里 `Set tFms=..GetMappingFms(tQB.%Get("mapping_id"))`，但类里没有再定义
    `GetMappingFms`（上一版 BP 曾自带）→ 编译报 `MPP5376 Method or Property 'GetMappingFms'
    does not exist in this class` → 整个 sql2fhir 组生成失败、白烧修复轮。
    判定：`..Foo(` 的 Foo 既不在本类定义（`ClassMethod|Method Foo(`）也不在 `PARENT_API` → 报错。
    """
    defined = {m.group(1).lower() for m in re.finditer(
        r"^\s*(?:Class)?Method\s+([A-Za-z%][A-Za-z0-9_]*)", source, re.M)}
    allowed = {p.lower() for p in PARENT_API}
    out = []
    for m in re.finditer(r"\.\.\s*([A-Za-z%][A-Za-z0-9_]*)\s*\(", source):
        fn = m.group(1)
        low = fn.lower()
        if low in defined or low in allowed:
            continue
        if fn in out:
            continue
        out.append(fn)
    return out


def refs_read_from_layout_root(source: str) -> bool:
    """检测「从 **layout 顶层** 读 refs」这种错误取值路径（2026-09-18 实测缺陷）。

    事实：布局的结构是 `{ design_skill, patient_table, patient_id_col, query_bos, http_bo,
    bundle: { entries, refs, resource_order } }` —— **refs 在 `bundle` 里，顶层没有该键**。
    BP 若写 `tLayout.%Get("refs")` 并用 `$IsObject` 兜底成空数组，引用注入会被**静默跳过**：
    Bundle 里 `subject/encounter/patient` 仍是映射产出的**裸源键**，FHIR 直接拒收
    （`<HSFHIRErr>MalformedRelativeReference`：The reference value 'X…' in property (subject)
    of Type 'Encounter' is malformed），而消息状态是 ErrBPTerminated —— 极易被误判为"映射/目标问题"。

    保守判定：只有当 `%Get("refs")` 的接收者**不是**任何「由 `%Get("bundle")` 赋值的变量」时才报；
    链式写法（`tLayout.%Get("bundle").%Get("refs")`）不算错（漏报不误报）。
    """
    bundle_vars = {m.group(1).lower() for m in re.finditer(
        r"Set\s+((?:t|p)[A-Za-z0-9_]*)\s*=\s*[^\n]*?%Get\(\s*\"bundle\"\s*\)", source, re.I)}
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\.%Get\(\s*\"refs\"\s*\)", source, re.I):
        if m.group(1).lower() not in bundle_vars:
            return True
    return False


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
        if BP_SHORT_NAME not in source or BP_PARENT_CLASS not in source:
            errors.append(
                f"source 必须声明 Class demo.SqlFhirPatientTxProcess Extends {BP_PARENT_CLASS}"
                "（2026-09-18 起聚合 BP 继承平台基类 demo.TransformProcess —— 术语转换改由**共享 BO** "
                "demo.TerminologyOperation 承载，`..BuildFHIRResource` / `..TermCoding` 是其继承来的实例方法；"
                "用 `Extends Ens.BusinessProcess` 或 `##class(demo.TransformProcess).BuildFHIRResource(...)` "
                "静态调用都会失败）")
        for tok in _FORBIDDEN:
            if tok in source:
                errors.append(f"source 含被禁内容: {tok}")
        # 未定义的 `..Helper(` 调用（实测缺陷 2026-09-18）：BP 调用了自己没定义的助手方法
        # （如 `..GetMappingFms`）→ 编译报 `MPP5376 Method or Property 'X' does not exist`
        # → 整个 sql2fhir 组生成失败并白烧修复轮。父类已提供稳定 API（PARENT_API），
        # 其它 `..X(` 必须在**本类**里有定义（ClassMethod/Method）。
        undefined = undefined_helper_calls(source)
        if undefined:
            errors.append(
                "调用了未定义的助手方法 " + ", ".join(f"..{u}(...)" for u in undefined)
                + "——必须在本类内定义（`ClassMethod X(...)`），或改用父类 "
                f"{BP_PARENT_CLASS} 提供的稳定 API："
                + ", ".join(f"..{p}(...)" for p in sorted(PARENT_API)))
        if source.count("}") < 1 or "OnRequest" not in source:
            errors.append("source 缺少 OnRequest（BP 入口）或类结构不完整")
        # 术语转换改走**共享 BO** 后，BuildFHIRResource / TermCoding 是继承自 demo.TransformProcess 的
        # **实例方法** → `##class(demo.TransformProcess).BuildFHIRResource(...)` 这类静态调用必失败
        # （2026-09-18 起；必须写 `..BuildFHIRResource(...)`）。
        bad_static = re.search(
            r"##class\(\s*demo\.TransformProcess\s*\)\s*\.\s*"
            r"(BuildFHIRResource|LeafValue|TermCoding|TermCacheCoding)\b", source, re.I)
        if bad_static:
            errors.append(
                f"检测到静态调用实例方法 `{bad_static.group(0)}` —— 聚合 BP 继承 demo.TransformProcess，"
                "必须写 `..BuildFHIRResource(...)`（术语转换由平台共享 BO `TerminologyOperation` 承载，"
                "Agent 无需自行查术语）")
        # ObjectScript 编译硬约束 ①：TRY 块内禁止带参数 QUIT（#1043）
        # 实测 2026-09-18 复发：`Try { … Quit tSC … }` 直接编译失败，白烧 2 轮修复并诱发幻觉代码。
        # 提示词里已有该事实仍会被违反 → 生成端静态准入兜住（命中即回喂重写，不进入编译）。
        bad_quit = try_block_quit_value_lines(source)
        if bad_quit:
            errors.append(
                "TRY 块内出现带参数的 QUIT（编译报 `#1043 QUIT argument not allowed`）："
                f"第 {bad_quit[:5]} 行——返回方法值只能写在方法体**顶层**（`Quit 变量`），"
                "Try 块内只做赋值/日志，不做控制流转移；或把该逻辑抽成 ClassMethod。")
        # ObjectScript 编译硬约束 ②：形参/返回值类型必须是真实存在的类（#5373）
        # 实测 2026-09-18：Agent 用 `As %Library.Object`（该类不存在）→ 编译失败。
        for bad_cls, good in _BAD_FORMALSPEC_TYPES.items():
            if re.search(r"\bAs\s+" + re.escape(bad_cls) + r"\b", source):
                errors.append(
                    f"source 使用**不存在**的类 {bad_cls}（编译报 `#5373 Class '{bad_cls}' … does not exist`）："
                    f"形参/返回值类型改用真实存在的类，如 {good}")
        # ObjectScript 编译硬约束 ③：引用清单必须从 `layout.bundle.refs` 读（实测 2026-09-18 静默缺陷）
        if refs_read_from_layout_root(source):
            errors.append(
                "source 从 **layout 顶层** 读 refs（`tLayout.%Get(\"refs\")`）—— 顶层**没有**该键，"
                "配上 `$IsObject` 兜底成空数组会让**引用注入被静默跳过**：Bundle 里 "
                "subject/encounter/patient 仍是裸源键 → FHIR 拒收 `<HSFHIRErr>MalformedRelativeReference`。"
                "正确写法：`Set tBundle=tLayout.%Get(\"bundle\")` 后读 `tBundle.%Get(\"refs\")`；"
                "`$IsObject(tRefs)` 为假时直接 `Quit $$$ERROR(...)`（不要用空数组兜底，那是静默降级）。")
        # 中文编码硬约束：GenericMessage 的 body 是 %RawString（原样字节），
        # 用字符流会在发送时逐字符按单字节输出 → 中文变 "?"（必须 UTF-8 字节流）
        if "EnsLib.HTTP.GenericMessage" in source and "%Stream.GlobalBinary" not in source:
            errors.append("HTTP 发送必须用 UTF-8 字节流：Set tS=##class(%Stream.GlobalBinary).%New() "
                          "Do tS.Write($ZCONVERT(tJSON,\"O\",\"UTF8\")) Set tMsg.Stream=tS"
                          "（GenericMessage body 是 %RawString，字符流会让中文变 '?'）")
        # 响应对象必须是**持久消息类**（实测缺陷）：OnRequest 的 `Output response As %Library.Persistent`
        # 若被赋 %DynamicObject/%Stream 等非持久对象，框架在收尾（Ens.BusinessProcess.%responseGet）
        # 会调用 %OpenId 而报 <METHOD DOES NOT EXIST>%OpenId,%Library.DynamicObject → ErrBPTerminated，
        # 入站消息被标 Error（尽管 Bundle 已成功投递、目标数据已落地 —— 极易被误判为管道失败）。
        import re as _re
        # 类中所有「动态对象变量」（Set tX=##class(%Dynamic...)...）——用于识别 Set response=tX
        _dyn_vars = set(_re.findall(
            r"Set\s+((?:t|p)[A-Za-z0-9_]*)\s*=\s*##class\(\s*%(?:Dynamic|Stream|Array|List)",
            source, _re.I))
        for m in _re.finditer(r"Set\s+(response|pResponse)\s*=\s*(.+)", source, _re.IGNORECASE):
            rhs = m.group(2).strip()
            bad = bool(_re.search(r"##class\(\s*%(?:Dynamic|Stream|Array|List)", rhs, _re.I)) \
                or rhs.split(".")[0].strip() in _dyn_vars
            if not bad:
                continue
            errors.append(
                "response 必须赋**持久消息类**（如 `Set response=##class(Ens.Response).%New()`）："
                f"当前为 `Set {m.group(1)}={rhs}` —— %DynamicObject/%Stream 等非持久对象会让框架 "
                "%responseGet 调 %OpenId 失败 → ErrBPTerminated、消息被标 Error（目标数据其实已落地）。"
                "摘要信息请用 $$$LOGINFO 或赋给 Ens.Response 子类的属性。")
            break
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


def method_source_issues(name: str, source: str, *, known_methods: set[str],
                         parent_api: set[str]) -> list[str]:
    """单个方法源码的静态准入（Plan 层用，范围比整类检查更聚焦）。返回问题清单（空=通过）。"""
    issues: list[str] = []
    src = str(source or "")
    if not src.strip():
        return ["方法源码为空"]
    if not re.search(r"^\s*(?:Class)?Method\s+%s\s*\(" % re.escape(name), src, re.M):
        issues.append(f"源码里没有定义方法 {name}（必须含 `Method {name}(...) As …` 方法头）")
    extra = [m.group(1) for m in re.finditer(
        r"^\s*(?:Class)?Method\s+([A-Za-z%][A-Za-z0-9_]*)", src, re.M) if m.group(1) != name]
    if extra:
        issues.append("只能输出一个方法（多做：" + ", ".join(extra[:3]) + "）")
    if re.search(r"^\s*Class\s+demo\.", src, re.M):
        issues.append("不要输出类头（只输出一个方法）")
    for tok in _FORBIDDEN:
        if tok in src:
            issues.append(f"含被禁内容: {tok}")
    bad_quit = try_block_quit_value_lines(src)
    if bad_quit:
        issues.append(f"Try 块内出现带参数 Quit（编译 #1043），行: {bad_quit[:5]}")
    known = {x.lower() for x in known_methods} | {x.lower() for x in parent_api}
    undef = [x for x in undefined_helper_calls(src) if x.lower() not in known]
    if undef:
        issues.append("调用了未定义的助手方法 " + ", ".join(f"..{u}(...)" for u in undef))
    dyn_vars = set(re.findall(
        r"Set\s+((?:t|p)[A-Za-z0-9_]*)\s*=\s*##class\(\s*%(?:Dynamic|Stream|Array|List)",
        src, re.I))
    for m in re.finditer(r"Set\s+(response|pResponse)\s*=\s*(.+)", src, re.IGNORECASE):
        rhs = m.group(2).strip()
        if (re.search(r"##class\(\s*%(?:Dynamic|Stream|Array|List)", rhs, re.I)
                or rhs.split(".")[0].strip() in dyn_vars):
            issues.append("response 必须赋**持久消息类**（如 `Set response=##class(Ens.Response).%New()`），"
                          "不能是 %DynamicObject/%Stream（否则 %responseGet 报错、消息被标 Error）")
            break
    return issues


def build_skeleton(plan: dict, bp_class: str) -> str:
    """由计划生成**可编译骨架**：类头 + 各方法签名 + 占位实现（编译通过 = 起点可用）。"""
    lines = [f"Class {bp_class} Extends {BP_PARENT_CLASS}", "{", ""]
    for m in plan.get("methods") or []:
        sig = str(m.get("signature") or "").strip()
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        if not sig:
            sig = f"Method {name}() As %Status"
        lines.append("/// " + str(m.get("purpose") or "")[:120])
        lines.append(sig)
        lines.append("{")
        ret = sig.split(" As ")[-1].strip() if " As " in sig else "%Status"
        lines.append("    Quit " + ("$$$OK" if ret == "%Status" else '""'))
        lines.append("}")
        lines.append("")
    lines.append("}")
    return "\n".join(lines)


def signature_list(plan: dict) -> str:
    """已规划方法的签名清单（方法级生成的上下文：不重复定义、可互相调用）。"""
    out = []
    for m in plan.get("methods") or []:
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        sig = str(m.get("signature") or "").strip() or f"Method {name}() As %Status"
        out.append(f"{name}: {sig} | 用途: {str(m.get('purpose') or '')[:80]}")
    return "\n".join(out)


def generate_from_plan(*, plan: dict, layout: dict | None, mappings: list[dict] | None,
                       components: list[dict] | None, parent_api: set[str],
                       facts: dict | None = None, bp_class: str = BP_CLASS_NAME,
                       max_rounds_per_method: int = 2) -> dict:
    """**Plan → Execute**：按计划逐个方法生成 → 静态准入 → 编译 →（可选）纯函数单测。

    - 每步失败只重写该步（≤ max_rounds_per_method 轮），编译/单测错误原文回喂；
    - 成功的方法落 `^demo.Config("bp_progress", …)` → **断点续跑**（重跑跳过已 ok 的方法）；
    - 失败**显式**返回（不静默降级、不用规则顶替）。
    """
    from backend.services import bp_method_tests, bp_planner

    methods = [m for m in (plan.get("methods") or [])
               if isinstance(m, dict) and str(m.get("name") or "").strip()]
    if not methods:
        return {"ok": False, "message": "计划里没有方法", "methods": {}}
    names = {str(m["name"]) for m in methods}
    steps: dict[str, dict] = {}
    attempts: dict[str, int] = {}

    skel = build_skeleton(plan, bp_class)          # 起点：可编译骨架
    base = _write_and_compile(bp_class, skel)
    if not base.get("ok"):
        return {"ok": False, "message": f"骨架编译失败：{base.get('message')}", "methods": {}}
    progress = bp_planner.load_method_progress(bp_class)
    resumed = sorted(n for n in names if str(progress.get(n) or "") == "ok")

    for m in methods:
        name = str(m["name"])
        if str(progress.get(name) or "") == "ok":
            steps[name] = {"status": "resumed", "attempts": 0}
            continue
        feedback, ok, last = "", False, ""
        cap = 16000                               # 单方法输出上限；截断则放大一次（天花板 32000）
        for attempt in range(1, max_rounds_per_method + 1):
            attempts[name] = attempt
            try:
                got = llm_client.generate_bp_method(
                    plan_step=m, bp_class=bp_class, skeleton=signature_list(plan),
                    layout=layout or {}, parent_api=sorted(parent_api), facts=facts or {},
                    feedback=feedback, max_tokens=cap)
            except llm_client.AgentError as exc:
                last = f"Agent 调用失败: {exc}"
                _m = str(exc)
                if ("截断" in _m or "finish_reason=length" in _m) and cap < 32000:
                    cap = min(cap * 2, 32000)     # 截断自愈：放大上限重试（仍是 AI 链路）
                    logger.warning("方法 %s 输出被截断 → 放大上限到 %d 重试", name, cap)
                feedback = last
                continue
            src = str((got.get("method") or {}).get("source") or "")
            issues = method_source_issues(name, src, known_methods=names, parent_api=parent_api)
            if issues:
                last = "静态准入未通过: " + "；".join(issues[:3])
                feedback = last
                continue
            res = apply_method_updates([{"name": name, "source": src}], bp_class)
            if not res.get("ok"):
                last = f"编译失败: {str(res.get('message'))[:600]}"
                feedback = last
                continue
            ures = bp_method_tests.run_unit(bp_class, name, m.get("unit"))
            if ures.get("ok"):
                ok = True
                steps[name] = {"status": "ok", "attempts": attempt, "unit": ures}
                break
            # 单测失败：若 Agent 同时给出 `unit_fix`（它认为样例写错）→ 采纳新样例后重试（AI 自证）
            _fix = got.get("unit_fix") if isinstance(got, dict) else None
            if (isinstance(_fix, dict) and isinstance(_fix.get("args"), list) and _fix.get("args")
                    and str(_fix.get("expect_contains") or "")):
                m["unit"] = {"args": _fix.get("args"), "expect_contains": str(_fix.get("expect_contains"))}
                steps.setdefault(name, {})["unit_fix"] = _fix
                _planner_save_unit_fix(bp_class, name, m["unit"])
                last = f"单测未通过（已采纳 Agent 的 unit_fix 重测）: {ures.get('message')}"
                feedback = last
                continue
            last = f"单测未通过: {ures.get('message')}"
            feedback = last
        if not ok:
            bp_planner.save_method_progress(bp_class, name, "failed", last)
            steps[name] = {"status": "failed", "attempts": attempts.get(name, 0), "message": last}
            return {"ok": False, "methods": steps, "resumed": resumed,
                    "message": f"方法 {name} 在 {max_rounds_per_method} 轮内未通过：{last}"}
        bp_planner.save_method_progress(bp_class, name, "ok")
    src = get_generated_source(bp_class)
    if src:
        save_last_good(src)
    return {"ok": True, "methods": steps, "resumed": resumed, "source_len": len(src or ""),
            "message": f"计划执行完成（{len(methods)} 个方法；续跑跳过 {len(resumed)} 个）"}


def _planner_save_unit_fix(bp_class: str, method: str, unit: dict) -> None:
    """把 Agent 修正的 unit 样例写回计划（审计：方法是"改了方法"还是"改了样例"一目了然）。"""
    try:
        from backend.services import bp_planner as _pl
        plan = _pl.load_plan(bp_class) or {}
        for m in (plan.get("methods") or []):
            if isinstance(m, dict) and str(m.get("name")) == method:
                m["unit"] = unit
                m.setdefault("_unit_fixed_by_agent", True)
        _pl.save_plan(bp_class, plan)
    except Exception as exc:  # noqa: BLE001
        logger.warning("unit_fix 写回计划失败: %s", exc)


def engine_supported(layout: dict | None) -> list[str]:
    """判定布局是否可由**父类通用引擎**处理（`demo.TransformProcess.ProcessFHIRBundle`）。

    返回不支持的原因清单（空 = 支持）。约束（引擎当前能力边界，超出则走 Plan 链/AI 生成）：
      · 必须有 `bundle.entries` 且含一个 `parent_kind=root` 的患者条目；
      · 每个 `query_bos[]` 要有 `bo_name`/`target_resource`/`mapping_id`，`depth` ∈ {1,2}；
      · 必须有 `http_bo`（或全局 `^demo.Config("fhir","operation")` 兜底）。
    """
    issues: list[str] = []
    if not isinstance(layout, dict) or not layout:
        return ["无布局"]
    bundle = layout.get("bundle") or {}
    entries = bundle.get("entries") or []
    if not any(str(e.get("parent_kind") or "") == "root" for e in entries if isinstance(e, dict)):
        issues.append("bundle.entries 缺少 parent_kind=root 的患者条目")
    bos = layout.get("query_bos") or []
    for b in bos:
        if not isinstance(b, dict):
            issues.append("query_bos 含非对象项")
            continue
        if not (b.get("bo_name") and b.get("target_resource")):
            issues.append(f"查询 BO 缺 bo_name/target_resource（{b.get('source_table')}）")
        if not b.get("mapping_id"):
            issues.append(f"查询 BO 缺 mapping_id（{b.get('source_table')}）→ 无法取字段映射")
        if int(b.get("depth") or 0) not in (1, 2):
            issues.append(f"depth={b.get('depth')} 超出引擎能力（仅 1/2，源表 {b.get('source_table')}）")
    if not layout.get("http_bo"):
        issues.append("缺 http_bo（FHIR Operation 实例名）")
    return issues


def render_engine_bp(bp_class: str = BP_CLASS_NAME) -> str:
    """渲染**薄适配 BP**：把聚合机制交给父类通用引擎（`..ProcessFHIRBundle`）。

    平台渲染（与 `DataflowProduction.cls` 同构）：AI 仍决定拓扑/映射/布局，BP 只是"调用引擎"的接线，
    因此不再需要 LLM 生成整份聚合 BP（实测整类生成 ≈34.5k completion、30~60 分钟且细节易错）。
    """
    short = bp_class.split(".")[-1]
    return (
        f"Class {bp_class} Extends {BP_PARENT_CLASS}\n"
        "{\n\n"
        "/// 平台渲染的**薄适配 BP**（2026-09-19）：聚合/派发/引用注入/事务投递均由父类\n"
        "/// `demo.TransformProcess.ProcessFHIRBundle` 承担；本类只做『解包 → 调引擎』的接线。\n"
        "/// 拓扑与字段映射仍由 AI（Agent B/A）决定，布局契约由平台注入。\n"
        "Method OnRequest(request As %Library.Persistent, Output response As %Library.Persistent) As %Status\n"
        "{\n"
        "    Set response = ##class(Ens.Response).%New()\n"
        "    Set tRow = ..UnpackSource(request)\n"
        "    If tRow = \"\" {\n"
        "        Quit $$$ERROR($$$GeneralError, \"源消息解包为空（UnpackSource 返回空）：\" _ $CLASSNAME(request))\n"
        "    }\n"
        "    Quit ..ProcessFHIRBundle(tRow, \"\")\n"
        "}\n\n"
        "}\n"
    )


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
