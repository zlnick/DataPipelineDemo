"""数据转换验证-修复 Agent（C1）：验证转换关系（字段映射）的合法性。

与管道验证-修复 Agent（C2，validate_agent.py）职责分离：
- C1 验证对象 = 转换关系（source 路径 → target 列 + transform），知识域 = FHIR 字段语义；
- C2 验证对象 = 数据管道（组件拓扑/编译/启动/消息），知识域 = IRIS 互操作性；
- 事实检查工具共享（pipeline_validator），L1/L2/L3 分层修复框架各自实现。
"""

import json
import logging

from backend.services import mapping_source_norm
from backend.services import pipeline_validator
from backend.services import transform_directives
from backend.services.llm_client import _call_llm

logger = logging.getLogger(__name__)

# ===== Agent C1：转换验证-修复（系统提示词，聚焦 FHIR 字段语义） =====
SYSTEM_PROMPT_TRANS_VALIDATE = (
    "你是医疗数据转换专家。给定转换关系（源 FHIR 资产 → 目标表列映射）与验证报告"
    "（目标列存在性/源字段路径等事实检查），判断哪些映射问题是实质性错误并给出修复。"
    "原则：1. 事实检查结果可信（目标列不存在、结构缺失是硬错误），"
    "你负责判断字段语义与选择修复路径。"
    "2. 修复动作从白名单选择："
    '{"action":"fix_mappings","fix":{"mappings":[...]}} 修正字段映射；**fix.mappings 必须是完整 mapping 对象数组**，'
    '每个对象保持原结构 {id, source, target_table, target_type, field_mappings:[{source,target,transform}]}，'
    "只在**对应 mapping 的 field_mappings 中追加/修正**条目；禁止拍平为字段级数组、禁止丢 id/改 target_table、"
    "禁止把 field_mappings 内容直接当 mappings 返回；"
    '{"action":"update_mapping","fix":{"updates":[{"id":"M1","field_mappings":[...]}]}} **推荐使用**：'
    "只提交需要修改的 mapping，field_mappings 里**只列需要新增或修改的条目**"
    "（平台按 mapping id + target 列名逐条合并：同名 target 更新、新 target 追加，"
    "未提及的原有映射保持不动）——不要只给部分条目却期望整体替换，也不要漏掉必须保留的映射；"
    '{"action":"drop_mapping","fix":{"mapping_id":"M1","reason":"..."}} 删除无意义映射；'
    '{"action":"fallback","message":"保留原映射"}；'
    '{"action":"none","message":"无需修复"}。'
    "3. 只修实质性错误（target 列不存在、source 路径明显错误、字段语义错配），"
    "warning 级（源字段不在资产已知字段）仅在语义明显错配时修复。"
    "4. 参考 past_issues 历史解决方案，避免重复错误。"
    "5. check=fhir_required 的 issue 是**硬错误**：必须用 fix_mappings 动作补齐缺失的必填字段映射——"
    "请用 context.assets 中源资产的列名做语义匹配选择 source（例如 Encounter.ClassCode→class_code、"
    "Patient.MRN→identifier、Patient.FamilyName→family、Patient.GivenName→given、Gender→gender、"
    "BirthDate→birthDate），保留原有全部映射并追加缺失项；不得删除已有映射、不得改动 target 列语义。"
    "**无源列可映射的必填字段**（如 MedicationRequest.intent、Condition.verificationStatus/category）"
    "统一用常量指令表达：transform 写成 \"constant:<值>\"、source 置为 null（例：intent 应写 "
    "{\"source\":null,\"target\":\"intent\",\"transform\":\"constant:order\"}）；"
    "不要把常量值放在 source 里（形状 {\"source\":\"order\",\"transform\":\"constant\"} 虽被平台兼容，但不是规范写法）。"
    "7. check=transform_directive 的 issue 是**硬错误**：transform 只能是受控指令集里的写法，"
    "未注册指令会被平台拒绝执行（运行期丢值）；constant 必须写 constant:<值> 且 source=null；"
    "非 constant 指令必须有 source 且源列存在于源资产列清单。修复时按下述指令目录改写 transform/source。\n"
    "8. 目标列元数据含 **constraints**（机器可读结构约束）：fh_property=真实 FHIR 属性名、"
    "value_landing=scalar|object|array（值最终落入的 JSON 形态）、array_element=是否数组元素、"
    "reference_target=引用目标资源类型、choice_property=choice 元素属性名。"
    "引用列（reference_target 非空）请标注 transform=reference（引用由资源聚合/打包按布局注入）；"
    "choice 与数组形态由平台按 path 处理，映射只需给出正确的目标列名与取值。\n"
    "9. check=db_key / db_key_value 的 issue 是**硬错误**：DB 目标表的主键列没有被产出值——"
    "运行期 IRIS 会报 \"<列名> is required\"（该管道目标零落地）。必须用 update_mapping 为该主键列"
    "补一条 field_mapping：source 优先选源表主键或语义等价的业务键（按 context.assets 的源列名匹配，"
    "如源表主键 ID → 目标主键 ID），源侧确实没有可用业务键时才用 transform=\"constant:<稳定值>\" 且 source=null；"
    "不得删除已有映射、不得改动其它目标列的语义。\n"
    "10. 修复时 source 一律写**规范形态**：SQL 源写 \"<源表名>.<列名>\"（如 Patient.FamilyName），"
    "FHIR 源写路径本身或 \"<资产名>.<路径>\"；**不要写 schema/库前缀**"
    "（禁止 SQLUser.Patient.ID 这类三段全限定名——运行期源数据是扁平列名，三段写法会取到空值）。\n"
    "11. **术语双 coding 缺口（完整性检查）**：若某目标列的元数据 note 含『建议双 coding』/『经术语对照』"
    "（US Core 常见于 Condition.code、MedicationRequest.medication、Encounter.reason_code 等），"
    "而该 target 的 transform 不是 term_map:<skill_id>（写成了 code/direct/null），"
    "说明只保留了源编码、**没有目标标准体系 coding** → 用 update_mapping **只补这一条**："
    "source 选源侧编码列（列名含 Code 的那列），transform 写 term_map:<skill_id>"
    "（药品→cn2rx、诊断/症状→cn2snomed，见受控指令目录的 skills）；"
    "`code` 只是 codeableConcept 类型提示，不产生任何目标体系 coding，不得用它代替 term_map。"
    "不要改动/删除该 mapping 的其它条目。\n"
    "12. **明文落 coding（coded_text_gap）**：若检查报 `coded_text_gap` —— 目标列是 CodeableConcept"
    "（如 Encounter.type/reason_code、MedicationRequest.route），但源列在接口分析里**不是编码列**"
    "（field_terms.kind = display_name/plain，如 ClassDisplay=\"门诊\"、Route=\"口服\"）："
    "把中文明文写进 coding.code 语义错误（还会被平台按“源行体系”误挂体系，如 route 挂上药品目录）。"
    "修法（update_mapping 增量、只改这一条）：改用模型提供的**文本列**——`<列名>_text`"
    "（type→type_text、reason_code→reason_text、route→route_text，path 为 `…text`）；"
    "若该列确实应为编码列，则改为映射真正的编码源列并配 term_map:<skill_id>。\n"
    "受控指令目录：\n"
    "6. 若 context.structure_error 非空，说明上一次修复结构非法——本轮必须严格返回完整 mapping 数组结构"
    "（每个 mapping 保留 id/source/target_table/target_type/field_mappings 全部字段），只在其 field_mappings 内增改。"
    "严格输出 JSON（不要输出其他文字），格式："
    '{"assessment":[{"issue":"...","severity":"error|warning","impact":"high|low","reason":"..."}],'
    '"fix":{"action":"...","message":"...","fix":{"..."}},'
    '"experience":{"pattern":"历史问题模式","resolution":"解决方案"}}'
) + "\n" + transform_directives.prompt_summary()

# 修复动作白名单（转换域）
VALID_TRANS_ACTIONS = {"fix_mappings", "update_mapping", "drop_mapping", "fallback", "none"}

# 术语映射判定 Skill 白名单（从受控指令注册表派生，单一事实源）
VALID_TERM_SKILLS = set(
    next((d.get("skills") for d in transform_directives.list_directives()
          if d["id"] == "term_map"), []) or [])


def _assets_index(assets: list[dict] | None) -> dict[str, set[str]]:
    """源资产索引：{资产ID/表名/schema.表 → 列集合}（大小写不敏感）。"""
    idx: dict[str, set[str]] = {}
    for a in assets or []:
        fields = a.get("fields") or (a.get("structure") or {}).get("columns") or []
        cols = {str(c.get("name") if isinstance(c, dict) else c).lower()
                for c in fields if c}
        if not cols:
            continue
        for k in (a.get("id"), a.get("name"), (a.get("structure") or {}).get("table")):
            if k:
                idx[str(k)] = cols
    return idx


def _directive_issues(mappings: list[dict], assets: list[dict] | None = None) -> list[dict]:
    """转换指令事实检查：指令必须是登记在册的受控指令，且形态/取值合法。

    - 未注册指令 = 硬错误（平台不静默忽略，避免运行期无声丢值）；
    - constant 必须写 constant:<值> 且 source=null；
    - 非 constant 指令必须有 source，且源列需存在于源资产列清单。
    """
    from backend.services import transform_directives as TD
    idx = _assets_index(assets)
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        cols = TD.source_columns_of(str(m.get("source") or ""), idx)
        for fm in m.get("field_mappings", []) or []:
            if not isinstance(fm, dict):
                continue
            msgs = TD.syntax_issues(fm) + TD.value_issues(fm, cols)
            for msg in msgs:
                issues.append({"severity": "error", "item": f"{mid}.{fm.get('target', '?')}",
                               "check": "transform_directive", "message": msg})
    return issues


def _fhir_scope_of(mapping: dict) -> str | None:
    """判断该映射是否属于 FHIR 目标域：是则返回已建模资源名，否则 None。

    **同名跨类型是常态**（DB 表 `Patient` 与 FHIR 资源 `Patient` 同名），故必须先看声明的类型：

    - `target_type=FHIR` → 走 FHIR 域（未建模资源返回 None，交开放候选逻辑处理）；
    - `target_type` 已声明为其它类型（DB/SOAP/…）→ **不做 FHIR 检查**。否则会拿 FHIR 列结构
      去校验 DB 映射（`identifier/family/...` 被判为必填缺失）→ L2 LLM 被误导改写 target 列名，
      实测把 DB 映射 9 个真实列改写成 5 个 FHIR 列名，运行期 `Patient.ID` 必填缺失；
    - `target_type` 未声明 → 按名字推断（历史兼容）。
    """
    from backend.services import fhir_target_model as _ftm
    tt = str(mapping.get("target_type") or "").strip().upper()
    if tt and tt != "FHIR":
        return None
    return _canonical_fhir_resource(str(mapping.get("target_table") or ""), _ftm)


def _fhir_required_issues(mappings: list[dict], assets: list[dict] | None = None) -> list[dict]:
    """FHIR 目标必填字段事实检查（列级 + 值级）。

    - 仅适用于**声明为 FHIR 的目标**（`target_type` 为空时才按名字推断，见 `_fhir_scope_of`）；
    - 列级：US Core 必填列必须出现在 field_mappings.target；
    - **值级**：该映射必须真的能产出值（constant 带值 / source 存在于源表列 / concat 表达式），
      否则虽然"有映射"但运行期资源仍缺该属性 → FHIR MissingRequiredProperty 整体回滚。
    修复由 L2 LLM 决策，平台不代写字段映射。
    """
    from backend.services import fhir_target_model as _ftm
    from backend.services import transform_directives as TD
    idx = _assets_index(assets)
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        res = _fhir_scope_of(m)
        if not res:
            continue
        schema = _ftm.model_schema(res) or {}
        required = [c.get("name") for c in schema.get("columns", [])
                    if c.get("required") and c.get("name")]
        if not required:
            continue
        fms = [fm for fm in (m.get("field_mappings") or []) if isinstance(fm, dict)]
        cols = TD.source_columns_of(str(m.get("source") or ""), idx)
        producible = set()
        for fm in fms:
            if TD.syntax_issues(fm) or TD.value_issues(fm, cols):
                continue
            producible.add(str(fm.get("target") or "").strip())
        mapped = {str(fm.get("target") or "").strip() for fm in fms}
        missing = [c for c in required if c not in mapped]
        no_value = [c for c in required if c in mapped and c not in producible]
        if missing:
            issues.append({
                "severity": "error", "item": f"{mid}.{res}", "check": "fhir_required",
                "message": f"FHIR 资源 {res} 的必填字段未被映射: {', '.join(missing)}"
                           "（US Core 必填；缺字段会导致 FHIR transaction 被拒并整体回滚）"})
        if no_value:
            issues.append({
                "severity": "error", "item": f"{mid}.{res}", "check": "fhir_required_value",
                "message": f"FHIR 资源 {res} 的必填字段虽然被映射但取不到值: {', '.join(no_value)}"
                           "（source 为空/源列不存在/常量指令未带值；请修正该映射使运行期能产出值）"})
    return issues


def _term_gap_issues(mappings: list[dict],
                     assets: list[dict] | None = None) -> list[dict]:
    """术语双 coding 完整性检查（**事实检查**，不代 AI 决策）。

    事实源：平台 FHIR 目标模型里列的 `note`（如 `Condition.code`「建议双 coding：源国标 ICD-10 原码 +
    SNOMED(经术语对照)」、`MedicationRequest.medication`「… + RxNorm(经术语对照)」）——
    该 note 是**平台自有的目标模型元数据**，声明"保留源编码 + 追加目标标准体系"。
    事实：`transform=code/direct/null` 只是类型提示/直通，**不会产生任何目标体系 coding**。

    触发条件（三条同时满足，避免误报）：
    ① 映射声明为 FHIR（`_fhir_scope_of`）；
    ② 该资源的某列 note 含『双 coding』或『经术语对照』，且该列**已映射**（未映射由 fhir_required 管）；
    ③ 该列 transform 不是 `term_map:<skill>`，且**源资产确有编码列**（列名含 code；
       源侧没有编码列时属"需补词表/人工"而非本检查能断言，跳过 → fail-open 不误伤）。

    修复由 C1 的 LLM 决策（选哪个 skill、取哪个源列），平台不写死码表也不代写映射。
    """
    from backend.services import fhir_target_model as _ftm
    from backend.services import transform_directives as TD
    idx = _assets_index(assets)
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        res = _fhir_scope_of(m)
        if not res:
            continue
        schema = _ftm.model_schema(res) or {}
        noted = [c for c in schema.get("columns", []) if c.get("name") and (
            "双 coding" in str(c.get("note") or "") or "经术语对照" in str(c.get("note") or ""))]
        if not noted:
            continue
        cols = TD.source_columns_of(str(m.get("source") or ""), idx) or []
        coded = [c for c in cols if "code" in str(c).lower()]
        if not coded:
            continue
        for col in noted:
            name = col["name"]
            hit = [fm for fm in (m.get("field_mappings") or []) if isinstance(fm, dict)
                   and str(fm.get("target") or "").strip() == name]
            if not hit:                      # 未映射 → 由 fhir_required 负责
                continue
            if any(str(fm.get("transform") or "").startswith("term_map:") for fm in hit):
                continue
            issues.append({
                "severity": "error", "item": f"{mid}.{res}.{name}", "check": "term_gap",
                "message": (f"FHIR 目标列 {res}.{name} 的元数据要求『保留源编码 + 补充目标标准体系』"
                            f"（双 coding / 经术语对照，源侧编码列: {', '.join(coded[:3])}），"
                            f"但该列 transform={hit[0].get('transform')!r} 不是 term_map:<skill_id> —— "
                            f"运行期只会写入源编码、缺目标体系 coding（code 只是类型提示，不产生 coding）。")})
    return issues


def _db_key_issues(mappings: list[dict],
                   target_models: list[dict] | None = None,
                   assets: list[dict] | None = None) -> list[dict]:
    """DB 目标主键列事实检查（列级 + 值级）。

    事实（2026-09-17 Round 2 实测 P0）：生成的 DB 目标 SQLOperation 是
    `INSERT OR UPDATE INTO <表>(<全部列>) VALUES(*<列>...)`，IRIS 要求**主键列非空**——
    映射没产出主键值时运行期报 `<列名> is required`（3 行源数据 → 9 条消息 Error、目标 0 落地），
    而生成接口此前一路 `code:0`（C1 只有 FHIR 必填检查，没有 DB 侧对应检查）。

    本检查只提供事实（目标表主键列 + 映射是否真能产出值），**修复由 L2 LLM 决策**
    （source 选源表主键/等价业务键，或 `constant:<值>` 指令）。拿不到主键事实 → 跳过（fail-open）。
    """
    from backend.services import db_target_keys as DBTK
    from backend.services import transform_directives as TD
    idx = _assets_index(assets)
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        tt = str(m.get("target_type") or "").strip().upper()
        if tt and tt not in ("DB", "SQL"):
            continue
        table = str(m.get("target_table") or "").strip()
        if not table:
            continue
        keys = DBTK.key_columns_of(table, tt or "DB", str(m.get("schema") or ""),
                                   target_models)
        if not keys:
            continue          # 未登记且探查不到 → 无事实，不判错
        fms = [fm for fm in (m.get("field_mappings") or []) if isinstance(fm, dict)]
        cols = TD.source_columns_of(str(m.get("source") or ""), idx)
        producible: set[str] = set()
        for fm in fms:
            if TD.syntax_issues(fm) or TD.value_issues(fm, cols):
                continue
            producible.add(str(fm.get("target") or "").strip())
        mapped = {str(fm.get("target") or "").strip() for fm in fms}
        missing = [k for k in keys if k not in mapped]
        no_value = [k for k in keys if k in mapped and k not in producible]
        if missing:
            issues.append({
                "severity": "error", "item": f"{mid}.{table}", "check": "db_key",
                "message": f"目标表 {table} 的主键列未被映射: {', '.join(missing)}"
                           f"（IRIS 的 INSERT 要求主键非空，运行期会报 \"{missing[0]} is required\""
                           "并使该管道目标零落地；请为该主键列补一条 field_mapping——"
                           "source 可选源表主键/等价业务键，或用 transform=\"constant:<值>\" 且 source=null）"})
        if no_value:
            issues.append({
                "severity": "error", "item": f"{mid}.{table}", "check": "db_key_value",
                "message": f"目标表 {table} 的主键列虽然被映射但取不到值: {', '.join(no_value)}"
                           "（source 为空/源列不存在/常量指令未带值；请修正该映射使运行期能产出值）"})
    return issues


def _canonical_fhir_resource(name: str, ftm) -> str | None:
    """把目标表/资源名规整到已建模资源 key（大小写不敏感）；非 FHIR 资源返回 None。"""
    if not name:
        return None
    low = name.strip().lower()
    for k in ftm.US_CORE_RESOURCE_MODELS:
        if k.lower() == low:
            return k
    return None


def _fhir_structure_issues(mappings: list[dict]) -> list[dict]:
    """FHIR 目标结构约束检查（事实来源于 fhir_target_model 的列元数据 + constraints）。

    - **target 用了 FHIR 属性名而不是模型列名**（如 medicationCodeableConcept / line / start）→ 硬错误
      （平台按 path 组装，映射必须用模型列名）；
    - **引用列**（Reference(X)）需标注 transform=reference：必填引用列缺标注 → 硬错误，非必填 → warning；
    - 数组落点但取不到值 → 由值级检查覆盖。
    """
    from backend.services import fhir_target_model as _ftm
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        res = _fhir_scope_of(m)
        if not res:
            continue
        schema = _ftm.model_schema(res) or {}
        col_by_name = {c.get("name"): c for c in schema.get("columns", [])}
        cons = _ftm.column_constraints(res)
        fh_props = {v.get("fh_property"): k for k, v in cons.items()
                    if v.get("fh_property") and v.get("fh_property") != k}
        for fm in m.get("field_mappings", []) or []:
            if not isinstance(fm, dict):
                continue
            target = str(fm.get("target") or "")
            tr = str(fm.get("transform") or "").strip()
            if target and target not in col_by_name and target in fh_props:
                issues.append({
                    "severity": "error", "item": f"{mid}.{target}", "check": "fhir_structure",
                    "message": f"target 用了 FHIR 属性名 {target}；应使用模型列名 {fh_props[target]}"
                               "（平台按 path 组装为该属性）"})
                continue
            c = cons.get(target) or {}
            if c.get("reference_target") and tr not in ("reference", "constant") \
                    and not tr.startswith("constant:"):
                req = bool((col_by_name.get(target) or {}).get("required"))
                issues.append({
                    "severity": "error" if req else "warning",
                    "item": f"{mid}.{target}", "check": "fhir_structure",
                    "message": f"{target} 是引用列（Reference({c['reference_target']})）："
                               "引用由资源聚合/打包按布局注入，映射需标注 transform=reference"})
    return issues


def _coded_text_issues(mappings: list[dict],
                       assets: list[dict] | None = None) -> list[dict]:
    """「明文落 coding」事实检查（2026-09-19 实测缺陷）。

    事实：目标列是 CodeableConcept（会序列化成 `coding:[{code:…}]`），而源列在接口分析的
    `field_terms` 里**不是** `coded_value`（`display_name` / `plain` / 无条目）→ 运行期会产出
    `{"coding":[{"code":"门诊"}]}` 这种**无语义 coding**（明文塞进编码位），并可能被平台按
    「源行体系」误挂体系（如 route="口服" 挂上药品目录 NRDL、type="门诊" 无体系）。

    触发条件（四条同时成立，避免误报）：
      ① 映射声明 FHIR；② 目标列 type 含 `concept`/`coding`；
      ③ 该列**没有**模型静态体系（`clinicalStatus`/`class_code` 这类 FHIR 固定体系列豁免）；
      ④ 源资产已知且源列 `kind != coded_value`（含源列无 field_terms 条目）→ 中文/自由文本/显示名。

    修法有落点：模型已提供 `*_text` 列（`type_text`/`reason_text`/`route_text` = `…text`），
    由 C1 的 LLM 用既有 `update_mapping` 增量修复（平台不写死映射）。
    """
    from backend.services import fhir_target_model as _ftm
    idx = _assets_index(assets)
    terms_idx = _field_terms_index(assets)
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        res = _fhir_scope_of(m)
        if not res:
            continue
        schema = _ftm.model_schema(res) or {}
        cols = {str(c.get("name")): c for c in schema.get("columns", []) if c.get("name")}
        terms = terms_idx.get(_tail_key(m.get("source"))) or {}
        for fm in (m.get("field_mappings") or []):
            if not isinstance(fm, dict):
                continue
            tgt = str(fm.get("target") or "").split(".")[-1].strip()
            src = str(fm.get("source") or "").strip()
            col = cols.get(tgt)
            if not (col and src):
                continue
            if "concept" not in str(col.get("type") or "").lower() \
                    and "coding" not in str(col.get("type") or "").lower():
                continue
            if col.get("system"):
                continue                      # FHIR 固定体系列（ActCode/condition-* 等）豁免
            srccol = src.split(".")[-1].strip()
            term = terms.get(srccol) or terms.get(srccol.lower()) or {}
            if str(term.get("kind") or "").strip().lower() == "coded_value":
                continue                      # 真编码列 → 由术语/体系事实链负责
            if not (idx and terms):
                continue                      # 资产/字段事实缺失 → fail-open，不误报
            text_col = _text_sibling(cols, tgt)
            issues.append({
                "severity": "error", "item": f"{mid}.{res}.{tgt}", "check": "coded_text_gap",
                "message": (f"FHIR 目标列 {res}.{tgt} 是 CodeableConcept，但源列 {src!r} 不是编码列"
                            f"（接口分析 kind={term.get('kind') or '未标注'}）→ 运行期会把明文写进 "
                            f"coding.code（且可能被误挂体系）。"
                            + (f"请改用文本列 {text_col!r}（path={cols[text_col].get('path')}）"
                               if text_col else "请改映射至合适的编码列或文本列")
                            + "；由 C1 用 update_mapping 增量修复。")})
    return issues


def _text_sibling(cols: dict, name: str) -> str:
    """同名字旁的文本列（`type` → `type_text`），没有则空。"""
    for cand in (f"{name}_text", f"{name}Text"):
        if cand in cols:
            return cand
    return ""


def _tail_key(text) -> str:
    """名称末段小写（`模拟HIS.Patient` / `Patient` → `patient`）。"""
    s = str(text or "").strip().strip('"')
    return (s.split(".")[-1] if "." in s else s).lower()


def _field_terms_index(assets: list[dict] | None) -> dict[str, dict]:
    """源资产的字段事实索引：{表名末段小写: {列: field_term}}（接口分析 AI 产出）。

    事实来源：资产的 `field_terms`（`/ai/recommend` 的 `_enrich_ai_semantics` 与生成链路的
    `_c1_assets` 都会带上）。拿不到 → 空索引 → 依赖它的检查 fail-open（不误报）。
    """
    out: dict[str, dict] = {}
    for a in assets or []:
        if not isinstance(a, dict):
            continue
        ft = a.get("field_terms")
        if not isinstance(ft, dict) or not ft:
            continue
        key = _tail_key(a.get("name") or a.get("id"))
        if key:
            out.setdefault(key, {}).update({str(k): v for k, v in ft.items()
                                            if isinstance(v, dict)})
    return out


def validate_mapping_against_constraints(mappings: list[dict],
                                         target_models: list[dict] | None = None) -> dict:
    """生成前强校验：指令合法性 + 必填（列级/值级）+ FHIR 结构约束 + DB 主键事实。

    target_models 可选（传入则用于解析目标表主键事实，缺省时按登记记录/JDBC 探查）。
    返回 {"ok": bool, "issues": [...], "error_count": int}（供生成端/外部调用）。
    """
    issues = (_directive_issues(mappings)
              + _fhir_required_issues(mappings)
              + _fhir_structure_issues(mappings)
              + _term_map_issues(mappings)
              + _coded_text_issues(mappings)
              + _db_key_issues(mappings, target_models))
    errors = [i for i in issues if i.get("severity") == "error"]
    return {"ok": not errors, "issues": issues, "error_count": len(errors)}


def _shape_ok(new_mappings, current_mappings) -> bool:
    """结构守卫：L2 修复必须保持原 mapping 数组结构（id/target_table/field_mappings）。

    只做结构事实校验，不判断字段语义；结构不合法则拒绝该修复（不写入坏结构）。
    """
    if not isinstance(new_mappings, list) or not new_mappings:
        return False
    cur_ids = {str(m.get("id")) for m in (current_mappings or []) if isinstance(m, dict)}
    new_ids = set()
    for m in new_mappings:
        if not isinstance(m, dict):
            return False
        if not m.get("id") or "field_mappings" not in m:
            return False
        if not isinstance(m.get("field_mappings"), list):
            return False
        new_ids.add(str(m.get("id")))
    return new_ids == cur_ids


def _term_map_issues(mappings: list[dict]) -> list[dict]:
    """校验 field_mapping.transform 的 term_map 指令合法性（引用白名单 + 结构完整）。

    只做受控指令的事实校验（skill 名 / source / target 完整），
    不判语义、不产出标准码——判码由 Skill 在运行期执行。
    """
    issues: list[dict] = []
    for m in mappings or []:
        mid = m.get("id", "?")
        for fm in m.get("field_mappings", []):
            tr = fm.get("transform") or ""
            if not str(tr).startswith("term_map:"):
                continue
            skill = str(tr)[len("term_map:"):].strip()
            if skill not in VALID_TERM_SKILLS:
                issues.append({"severity": "error", "item": f"{mid}.{fm.get('target','')}",
                               "message": f"term_map 引用了未注册判定 Skill: {skill or tr}（白名单: "
                                          f"{'/'.join(sorted(VALID_TERM_SKILLS))}）"})
            if not fm.get("source"):
                issues.append({"severity": "error", "item": f"{mid}.{fm.get('target','')}",
                               "message": "term_map 指令缺少源编码字段 source"})
            if not fm.get("target"):
                issues.append({"severity": "error", "item": mid,
                               "message": "term_map 指令缺少目标字段 target"})
    return issues


def normalize_recommendations(recs: list[dict]) -> list[dict]:
    """把 Agent A 输出（asset/target_table 风格）归一化为映射结构（id/source/target_table）。

    使转换验证（check_recommendations 期望 id/source）与生成管道（MappingItem）结构统一。
    """
    normalized: list[dict] = []
    for i, r in enumerate(recs or [], 1):
        src = (r.get("source") or r.get("asset") or r.get("source_asset")
               or r.get("source_entity") or r.get("source_table") or "")
        tgt = (r.get("target_table") or r.get("target") or r.get("target_entity")
               or r.get("target_model") or r.get("emit") or "")
        normalized.append({
            "id": r.get("id") or f"R{i}",
            "source": src,
            "target_table": tgt,
            "field_mappings": r.get("field_mappings", []),
            "confidence": r.get("confidence") or 0.85,
            "reason": r.get("reason") or "AI 智能匹配",
        })
    return normalized


def judge_transformation(report: dict, context: dict,
                         past_issues: list[dict]) -> dict:
    """Agent C1：判断转换关系问题 + 给出修复动作（LLM 单轮决策）。"""
    user_content = json.dumps({
        "validation_report": report,
        "context": context,
        "past_issues": past_issues,
    }, ensure_ascii=False, indent=2)
    result = _call_llm(SYSTEM_PROMPT_TRANS_VALIDATE, user_content, "转换验证Agent")
    fix = result.get("fix") or {}
    if fix.get("action") not in VALID_TRANS_ACTIONS:
        fix = {"action": "fallback", "message": "LLM 返回未知动作，保留原映射"}
    return {
        "assessment": result.get("assessment", []),
        "fix": fix,
        "experience": result.get("experience", {}) or {},
    }


def _l1_fix_mappings(mappings: list[dict],
                      target_models: list[dict] | None = None) -> tuple[list[dict], bool]:
    """L1 转换规则修复：剔除指向不存在目标列的字段映射（机械规则，**类型感知**）。

    ⚠ 同名跨类型（DB 表 `Patient` / FHIR 资源 `Patient`）必须按映射声明的 `target_type`
    取列结构——否则 DB 映射会被拿 FHIR 列清单判为"坏列"而**整批静默剔除**（实测）。
    且**取不到可信列清单时一律不剔除**（fail-open：宁可留待 LLM/运行期暴露，也不静态删映射）。
    """
    changed = False
    fixed: list[dict] = []
    for m in mappings or []:
        table = m.get("target_table", "")
        cols = (pipeline_validator._get_table_columns(table, target_models, m.get("target_type"))
                if table else [])
        fms = []
        for fm in m.get("field_mappings", []):
            target = fm.get("target", "")
            if target and cols and target not in cols:
                changed = True
                continue
            fms.append(fm)
        if len(fms) != len(m.get("field_mappings", [])):
            changed = True
        fixed.append({**m, "field_mappings": fms})
    return fixed, changed


def validate_and_fix_transformation(mappings: list[dict],
                                    assets: list[dict] | None = None,
                                    target_models: list[dict] | None = None,
                                    max_rounds: int = 3) -> dict:
    """转换验证-修复闭环（≤2 轮）：事实检查 → L1 规则 → L2 LLM 决策 → 验证。

    参数:
        mappings: 转换关系列表（Agent A 输出或用户确认）
        assets: 源资产结构（含 fields），用于源路径检查（可选）
        target_models: 目标模型列表（含 columns/fields，可选）
    返回:
        {"status": "ok"|"failed", "rounds": [...], "mappings", "report", "message"}
    """
    # 入口口径归一（只规整写法：剥掉 source 的 schema/库前缀；concat 参数递归）——
    # 让后续 source 取值/源列存在性检查少依赖执行层容错（缺陷 P 的上游收口）。
    _norm_cnt = mapping_source_norm.normalize_mappings(mappings, assets)
    if _norm_cnt:
        logger.info("C1 入口口径归一：%d 条字段映射的 source 已规范（不改变字段语义）", _norm_cnt)
    context = {
        "assets": assets or [],
        "target_models": target_models or [],
        "mapping_count": len(mappings or []),
    }
    past_issues = pipeline_validator.load_validation_issues()
    current_mappings = list(mappings or [])
    rounds_log: list[dict] = []
    report: dict = {}
    structure_error = ""

    for round_i in range(max_rounds + 1):
        report = pipeline_validator.run_transformation_validation(
            current_mappings, assets, target_models)
        # 术语映射指令合法性（skill 白名单 + 结构完整；不判码）
        _term_issues = _term_map_issues(current_mappings)
        if _term_issues:
            for _ti in _term_issues:
                report.setdefault("issues", []).append({"check": "term_map", **_ti})
        # FHIR 目标必填字段事实检查（US Core 必填缺失 → FHIR transaction 整体回滚）
        _req_issues = _fhir_required_issues(current_mappings, assets)
        if _req_issues:
            for _ri in _req_issues:
                report.setdefault("issues", []).append(_ri)
        # DB 目标主键列事实检查（IRIS INSERT 要求主键非空，缺则运行期 `<列> is required`）
        _key_issues = _db_key_issues(current_mappings, target_models, assets)
        if _key_issues:
            for _ki in _key_issues:
                report.setdefault("issues", []).append(_ki)
        # 术语双 coding 完整性检查（目标列 note 要求保留源码+补标准体系，却只写了 code/null）
        _gap_issues = _term_gap_issues(current_mappings, assets)
        if _gap_issues:
            for _gi in _gap_issues:
                report.setdefault("issues", []).append(_gi)
        # 明文落 coding 检查（CodeableConcept 列由中文/自由文本源列供给 → 应由 *_text 列承接）
        _ct_issues = _coded_text_issues(current_mappings, assets)
        if _ct_issues:
            for _ci in _ct_issues:
                report.setdefault("issues", []).append(_ci)
        # 转换指令事实检查（受控指令注册表：未注册/形态不规范/取不到值）
        _dir_issues = _directive_issues(current_mappings, assets)
        if _dir_issues:
            for _di in _dir_issues:
                report.setdefault("issues", []).append(_di)
        # FHIR 目标结构约束检查（引用列/数组落点；warning 级）
        _struct_issues = _fhir_structure_issues(current_mappings)
        if _struct_issues:
            for _si in _struct_issues:
                report.setdefault("issues", []).append(_si)
        errors = [i for i in report["issues"] if i.get("severity") == "error"]
        err_summary = "；".join(f"[{i.get('check')}] {i.get('message')}" for i in errors)
        rounds_log.append({"round": round_i, "error_count": len(errors),
                           "summary": err_summary or "无错误"})
        if not errors:
            return {"status": "ok", "rounds": rounds_log,
                    "mappings": current_mappings, "report": report,
                    "message": "转换验证通过"}

        # L1 规则修复（机械规则：剔除坏列映射）
        new_mappings, changed = _l1_fix_mappings(current_mappings, target_models)
        if changed:
            current_mappings = new_mappings
            logger.info("转换 L1 规则修复生效: 映射改动=True")
            continue

        # L2：LLM 决策修复
        context["structure_error"] = structure_error
        decision = judge_transformation(report, context, past_issues)
        fix = decision.get("fix", {})
        action = fix.get("action", "fallback")
        logger.info("转换验证 Agent 决策: action=%s message=%s",
                    action, fix.get("message", ""))
        structure_error = ""
        if action == "fix_mappings":
            new_ms = fix.get("fix", {}).get("mappings")
            if isinstance(new_ms, list) and new_ms:
                if _shape_ok(new_ms, current_mappings):
                    current_mappings = new_ms
                else:
                    structure_error = ("上一次 fix_mappings 返回结构不合法：必须返回完整 mapping 对象数组"
                                       "（保留 id/source/target_table/target_type/field_mappings），"
                                       "只允许在 field_mappings 内追加/修正条目")
                    logger.warning("转换 L2 修复结构不合法，已拒绝: %s", structure_error)
        elif action == "update_mapping":
            # 增量修复（推荐）：只提交需要修改的 mapping，平台按 id + target 逐条合并，其余保持不变
            ups = (fix.get("fix") or {}).get("updates") or []
            cur_by_id = {str(m.get("id")): dict(m) for m in current_mappings
                         if isinstance(m, dict) and m.get("id")}
            changed = False
            for u in ups:
                if not isinstance(u, dict) or not u.get("id"):
                    continue
                mid = str(u["id"])
                if mid not in cur_by_id:
                    continue
                merged = dict(cur_by_id[mid])
                if isinstance(u.get("field_mappings"), list):
                    # **逐条合并**（不能整段替换）：LLM 通常只提交需要新增/修改的条目，
                    # 按 target 列名匹配——同名条目更新（保留未提及字段），新目标列追加，
                    # 未在 updates 中出现的原有映射一律保持不动。
                    base = [dict(fm) for fm in (merged.get("field_mappings") or [])
                            if isinstance(fm, dict)]
                    idx_by_target = {str(fm.get("target")): i for i, fm in enumerate(base)
                                     if fm.get("target")}
                    for fm in u["field_mappings"]:
                        if not isinstance(fm, dict) or not fm.get("target"):
                            continue
                        t = str(fm["target"])
                        if t in idx_by_target:
                            orig = base[idx_by_target[t]]
                            new_fm = {**orig, **fm}
                            if "transform" not in fm:  # LLM 未提及 transform 时保留原指令
                                new_fm["transform"] = orig.get("transform")
                            base[idx_by_target[t]] = new_fm
                        else:
                            base.append(dict(fm))
                            idx_by_target[t] = len(base) - 1
                    merged["field_mappings"] = base
                for k in ("source", "target_table", "target_type"):
                    if u.get(k):
                        merged[k] = u[k]
                cur_by_id[mid] = merged
                changed = True
            if changed:
                current_mappings = [cur_by_id[str(m.get("id"))] for m in current_mappings
                                    if isinstance(m, dict) and str(m.get("id")) in cur_by_id]
                structure_error = ""
                # 记录增量修复明细（诊断用：确认 transform/reference 等标注是否真的写入）
                _summ = []
                for u in ups:
                    if isinstance(u, dict) and u.get("id"):
                        _trs = [str(f.get("transform")) for f in (u.get("field_mappings") or [])
                                if isinstance(f, dict) and f.get("transform")]
                        _summ.append(f"{u['id']}[{','.join(_trs)}]")
                logger.info("转换 L2 增量修复已合并: %s", " ".join(_summ))
                # LLM 新增/改写的条目同样做口径归一（只规整写法，不改字段语义）
                _n2 = mapping_source_norm.normalize_mappings(current_mappings, assets)
                if _n2:
                    logger.info("L2 修复后口径归一：%d 条 source 已规范", _n2)
            else:
                structure_error = ("update_mapping 未提供有效 updates（每项需含 id 与 field_mappings）")
                logger.warning("转换 L2 增量修复无效: %s", structure_error)
        elif action == "drop_mapping":
            mid = fix.get("fix", {}).get("mapping_id")
            if mid:
                current_mappings = [m for m in current_mappings
                                    if m.get("id") != mid]

        # 经验沉淀
        exp = decision.get("experience") or {}
        if exp.get("pattern") and exp.get("resolution"):
            saved = pipeline_validator.save_validation_issue(
                exp["pattern"], exp["resolution"], source="trans-validate")
            logger.info("转换验证经验已沉淀: %s", saved.get("ok"))

    return {"status": "failed", "rounds": rounds_log,
            "mappings": current_mappings, "report": report,
            "message": "超过最大修复轮数仍存在问题"}

