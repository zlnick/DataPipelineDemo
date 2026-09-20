"""映射 source 口径归一（字段路径 → **规范形态**；只改写法，不改字段语义）。

规范形态（Agent A 提示词口径）：
  - SQL 源：`<源表名>.<列名>`（如 `Patient.FamilyName`）——**不带 schema/数据库前缀**；
  - FHIR 源：路径本身（`name[0].family`）或 `<资产名>.<路径>`（`Patient.name[0].family`）；
  - 表达式：`concat(参数, ' ', 参数)`（参数逐个归一）；字面量 `'x'` 保持；
  - `constant:<值>` 指令（source 可为 null）不动。

为什么要有这一层（2026-09-17 Round 2 缺陷 P）：Agent A 对 SQL 源常写**三段全限定名**
`SQLUser.Patient.ID`，而运行期源数据是 SQL 轮询行的**扁平列名**。执行层
（`iris/python/transform_handler._get_field`）已做"多段前缀逐级剥离 + 末段兜底"容错，
但上游口径统一能让"取不到值"少一层依赖——同族既有做法见缺陷 N11（`mapping.source` 归一到资产名）。

边界（遵守 AI 驱动红线）：本模块**只规整路径写法**（剥 schema 前缀），
绝不改变"映射了哪个字段/用哪条指令"——那是 Agent A / C1（LLM）的决策。
"""

import logging

logger = logging.getLogger(__name__)

# 默认识别的 schema/库前缀（配合 assets 里登记的 structure.schema 一起用；比较时小写）
DEFAULT_SCHEMA_TOKENS = {
    "sqluser", "user", "clinic", "fhir", "soap", "hslib", "hssys", "demofhir", "fhirserver",
}


def schema_prefixes(assets: list[dict] | None = None,
                    extra: list[str] | None = None) -> set[str]:
    """收集可剥离的 schema/库前缀（来源：源资产登记的 structure.schema + 默认表 + 调用方补充）。"""
    out = set(DEFAULT_SCHEMA_TOKENS)
    for a in assets or []:
        struct = a.get("structure") or {}
        for key in ("schema", "database", "namespace"):
            v = str(struct.get(key) or a.get(key) or "").strip().lower()
            if v:
                out.add(v)
    for v in extra or []:
        v = str(v or "").strip().lower()
        if v:
            out.add(v)
    return out


def _split_args(inner: str) -> list[str]:
    """按逗号切分函数参数（忽略引号内与括号嵌套内的逗号）。"""
    args, depth, cur, quote = [], 0, "", ""
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            cur += ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
            continue
        cur += ch
    args.append(cur)
    return [a.strip() for a in args if a.strip() != ""]


def normalize_expr(expr: str, prefixes: set[str]) -> str:
    """把单个 source 表达式归一到规范形态（剥掉开头的 schema/库前缀；concat 参数递归处理）。"""
    s = str(expr or "").strip()
    if not s:
        return s
    # 字面量（成对引号包裹）保持原样
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        return s
    low = s.lower()
    if low.startswith("concat(") and s.endswith(")"):
        inner = s[len("concat("):-1]
        return "concat(" + ", ".join(normalize_expr(a, prefixes) for a in _split_args(inner)) + ")"
    parts = s.split(".")
    if len(parts) <= 1:
        return s
    i = 0
    while i < len(parts) - 1 and parts[i].strip().lower() in prefixes:
        i += 1
    return ".".join(parts[i:])


def normalize_field_mappings(field_mappings: list[dict], prefixes: set[str]) -> int:
    """就地归一 field_mappings 的 source，返回**被改动的条数**（只改写法）。"""
    changed = 0
    for fm in field_mappings or []:
        if not isinstance(fm, dict):
            continue
        src = fm.get("source")
        if src is None:            # constant 指令（source=null）不动
            continue
        new = normalize_expr(str(src), prefixes)
        if new != str(src):
            fm["source"] = new
            changed += 1
    return changed


def normalize_mappings(mappings: list[dict], assets: list[dict] | None = None,
                       extra_prefixes: list[str] | None = None) -> int:
    """就地归一 mappings 的全部 field_mappings.source，返回被改动的条数。"""
    prefixes = schema_prefixes(assets, extra_prefixes)
    changed = 0
    for m in mappings or []:
        if not isinstance(m, dict):
            continue
        changed += normalize_field_mappings(m.get("field_mappings") or [], prefixes)
    if changed:
        logger.info("映射 source 口径归一：%d 条去掉了 schema/库前缀", changed)
    return changed
