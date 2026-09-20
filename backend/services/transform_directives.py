# -*- coding: utf-8 -*-
"""数据转换受控指令注册表（单一事实源）。

`field_mapping.transform` 是 Agent（数据转换生成 / C1 修复）决策出的**受控指令**，
本模块是它的事实源：Agent 提示词、C1 校验、平台执行（transform_handler）三处共用。

原则：
- 平台只实现登记在册的指令；**未注册指令一律视为错误**（不再静默忽略 → 避免"无声丢值"）；
- 指令的参数形态是硬约束（如 constant 的常量值必须写在 transform 里、source 必须为 null）；
- `direct/date/code/reference` 等属平台已支持的取值方式或类型提示，登记在册但不产生额外语义。
"""

# ---------------------------------------------------------------------------
# 术语判定 Skill 注册表（`term_map:<skill_id>` 的单一事实源）
#
# 职责划分（与"术语服务器 = 事实源"的架构一致）：
# - 这里只登记**平台侧知识**：该 Skill 走哪一对编码体系、判定结果取哪个字段、
#   目标 coding 写进哪个 system URI；
# - "某对体系下有哪些码可用"是**术语服务器**的事实（/mapping/systems、/mapping/availability），
#   平台不复制、不硬编码码表；
# - 判定本身由 LLM 判定 Agent 做（C3-Dx / C3），平台不引入第二套判码规则。
# ---------------------------------------------------------------------------
TERM_SKILLS: dict[str, dict] = {
    "cn2snomed": {
        "label": "中文诊断（国标 ICD-10）→ SNOMED CT",
        "source_system": "urn:cn-nhsa:icd10-gbt2016",
        "target_system": "http://snomed.info/sct",
        "code_field": "code",
        "display_field": "display",
        "agent": "C3-Dx（Diagnosis Mapping Agent）",
    },
    "cn2rx": {
        "label": "中文药品（国家医保目录 NRDL）→ RxNorm",
        "source_system": "urn:cn-nhsa:drug-nrdl",
        "target_system": "http://www.nlm.nih.gov/research/umls/rxnorm",
        "code_field": "rxcui",
        "display_field": "enName",
        "agent": "C3（Drug Mapping Agent）",
    },
}


def term_skill(skill_id: str) -> dict | None:
    """取术语判定 Skill 登记项（未注册返回 None）。"""
    return TERM_SKILLS.get(str(skill_id or "").strip())


def term_skill_ids() -> list[str]:
    """已注册的术语判定 Skill 名（供 Agent 提示词 / 校验 / 目录注入共用）。"""
    return list(TERM_SKILLS)


DIRECTIVES: list[dict] = [
    {"id": "direct", "syntax": "direct（或 transform=null）", "implemented": True,
     "note": "按 source 表达式原样取值（字段路径 / 表.列 前缀 / concat(...) 表达式）"},
    {"id": "date", "syntax": "date", "implemented": True,
     "note": "日期规范化：取前 10 位 YYYY-MM-DD"},
    {"id": "constant", "syntax": "constant:<值>", "implemented": True,
     "note": "常量值（无源列可映射的必填字段用）；source 必须为 null，常量值写在 transform 内"},
    {"id": "term_map", "syntax": "term_map:<skill_id>", "implemented": True,
     "skills": term_skill_ids(),
     "note": "术语判定：保留源编码并追加目标标准体系 coding（判码结果来自术语服务器映射，生成前预检 + 运行期读本地缓存）"},
    {"id": "concat", "syntax": "source=concat(A,' ',B)", "implemented": True,
     "note": "拼接表达式（写在 source 中，不是 transform 指令）"},
    {"id": "code", "syntax": "code", "implemented": True,
     "note": "类型提示：目标为 code/codeableConcept（打包器按列 type 组装，不改变取值）"},
    {"id": "reference", "syntax": "reference", "implemented": True,
     "note": "类型提示：目标为 Reference（引用由资源打包/聚合按布局注入）"},
]

_DIRECTIVE_IDS = {d["id"] for d in DIRECTIVES}
# 不承载"取值"语义的写法（类型提示/表达式）；其余以指令名解析
_NO_VALUE_NEEDED = {"code", "reference"}


def list_directives() -> list[dict]:
    """返回注册表副本（供 Agent 提示词 / API 展示）。"""
    return [dict(d) for d in DIRECTIVES]


def directive_ids() -> set[str]:
    return set(_DIRECTIVE_IDS)


def prompt_summary() -> str:
    """生成给 Agent 的指令目录文本（Agent A / C1 共用）。"""
    lines = ["受控转换指令（transform 只能取以下值之一；未登记写法会被校验判为错误）："]
    for d in DIRECTIVES:
        extra = f"；可选值: {'/'.join(d['skills'])}" if d.get("skills") else ""
        lines.append(f"- {d['id']}：{d['syntax']} —— {d['note']}{extra}")
    return "\n".join(lines)


def syntax_issues(fm: dict) -> list[str]:
    """校验单条 field_mapping 的指令语法（不含源列存在性）。"""
    issues: list[str] = []
    tr = fm.get("transform")
    tr_s = "" if tr is None else str(tr).strip()
    src = fm.get("source")
    src_s = "" if src is None else str(src).strip()
    target = fm.get("target") or "?"

    if tr_s == "" or tr_s == "direct":
        if not src_s:
            issues.append(f"{target}: 取不到值——source 为空且未使用 constant 指令")
        return issues

    if tr_s.startswith("constant:"):
        if not tr_s[len("constant:"):].strip():
            issues.append(f"{target}: constant 指令缺少常量值（应写 constant:<值>）")
        if src_s:
            issues.append(f"{target}: constant 指令的 source 必须为 null（常量值写在 transform 内；"
                          f"当前 source={src_s}）")
        return issues

    if tr_s == "constant":
        if not src_s:
            issues.append(f"{target}: constant 指令缺少常量值（应写 constant:<值> 且 source=null）")
        else:
            issues.append(f"{target}: constant 写法不规范——应为 constant:<值> 且 source=null"
                          f"（当前 source={src_s}；平台虽兼容执行，但必须按规范修正）")
        return issues

    if tr_s.startswith("term_map:"):
        skill = tr_s[len("term_map:"):].strip()
        allowed = next((d.get("skills") or [] for d in DIRECTIVES if d["id"] == "term_map"), [])
        if not skill:
            issues.append(f"{target}: term_map 缺少判定 Skill 名（应写 term_map:<skill_id>）")
        elif skill not in allowed:
            issues.append(f"{target}: term_map 引用了未注册判定 Skill \"{skill}\""
                          f"（可选: {'/'.join(allowed)}）")
        if not src_s:
            issues.append(f"{target}: term_map 指令缺少源编码字段 source")
        return issues

    if tr_s.startswith("concat("):
        return issues  # 表达式：由 source 求值

    if tr_s in _DIRECTIVE_IDS:
        if (tr_s not in _NO_VALUE_NEEDED) and not src_s and tr_s != "date":
            issues.append(f"{target}: {tr_s} 指令需要 source 提供取值")
        return issues

    issues.append(f"{target}: 未注册的转换指令 \"{tr_s}\"（受控指令集: "
                  f"{'/'.join(sorted(_DIRECTIVE_IDS))}）")
    return issues


def value_issues(fm: dict, source_columns: set[str] | None = None) -> list[str]:
    """校验该映射运行期能否产出值（值级可生成性）。

    source_columns 为该映射所属源表的列清单（大小写不敏感）；为 None 时跳过列存在性检查。
    """
    issues: list[str] = []
    tr = fm.get("transform")
    tr_s = "" if tr is None else str(tr).strip()
    src = fm.get("source")
    src_s = "" if src is None else str(src).strip()
    target = fm.get("target") or "?"
    if tr_s.startswith("constant:"):
        return issues          # 常量取值，无需源列
    if tr_s == "constant" and src_s:
        return issues          # 兼容形态（syntax_issues 会要求规范化）
    if not src_s:
        return issues          # 由 syntax_issues 报告
    if "(" in src_s:           # concat(...) 等表达式
        return issues
    # 源列存在性：既支持 SQL 的「表.列」，也支持 FHIR 的**完整路径**（name[0].family）。
    # 原实现只取 `split(".")[-1]` 的最后一段与源字段清单比对 —— FHIR 资产清单里存的是完整路径
    # （`name[0].family`），于是**正确映射被误报**「源列 family 不在源表已知列中」（2026-09-16 实测，
    # 缺陷 L：噪音告警会误导用户去"修"本来正确的映射，也可能触发无谓的 C1 修复轮）。
    # 现改为逐级剥前缀的多候选匹配：Patient.name[0].family → name[0].family → family。
    if source_columns is not None:
        parts = str(src_s).split(".")
        cands = {".".join(parts[i:]).lower() for i in range(len(parts))} - {""}
        if not (cands & source_columns):
            issues.append(f"{target}: 源列 {src_s} 不在源表已知列中（该映射运行期取不到值）")
    return issues


def source_columns_of(fm_source: str, assets_index: dict[str, set[str]]) -> set[str] | None:
    """按 mapping.source（资产 ID/表名）取该源表列集合；未知返回 None。"""
    if not fm_source:
        return None
    raw = str(fm_source)
    for key in (raw, raw.split(".")[-1]):
        if key in assets_index:
            return assets_index[key]
    return None
