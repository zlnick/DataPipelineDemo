# -*- coding: utf-8 -*-
"""离线静态守卫：**父类通用聚合引擎**（`iris/src/demo/TransformProcess.cls`）的关键不变量。

为什么需要它（2026-09-19 实测缺陷）：引擎把「遍历/派发/引用注入/事务组装/回执校验」固化成平台代码后，
这类**手写机制代码**的错误不像 AI 生成物那样有编译回喂兜底 —— 本次真实踩到：
`ProcessFHIRBundle` 在**单表布局**（`query_bos=[]` → `tMaxDepth=0`）时循环体从不执行，
而未预置 `tSC` → 深度循环之后的 `If $$$ISERR(tSC) Quit tSC` 报
`<UNDEFINED>ProcessFHIRBundle+66^demo.TransformProcess.1 *tSC` → `ErrBPTerminated`
（表现：消息 Error、FHIR 0 落地；而多资源布局一切正常 —— 极易漏掉）。

本脚本在**宿主机**跑（IRIS 源码在仓库里，backend 容器不挂载 `iris/`），只做只读静态断言：
  A tSC 初始化顺序（单表布局路径的回归守卫）
  B ProcessChildLevel 成功返回 + 层级记账落位
  C MakeResourceUuid：小写 + 零填充（FHIR `urn:uuid` 只接受小写）
  D BundleHttpOk：必须查 2xx 范围 + 体内 OperationOutcome（防"目标 0 落地却全 Completed"）
  E 回执必须经 BundleHttpOk 返回（错误不被吞）；缺布局/缺 http_bo 必须显式报错（不静默）

用法：python3 tools/check_engine_source.py      # 退出码 0 = 全部通过
"""
import re
import sys
from pathlib import Path

CLS = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parents[1] / "iris" / "src" / "demo" / "TransformProcess.cls")
PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + msg)
    else:
        FAIL += 1
        print("  ✗ " + msg)


def methods(text):
    """把 UDL 源码切成 {方法名: [(行号, 代码行)]}（去掉 注释行/空行）。"""
    out, cur, buf = {}, None, []
    for i, raw in enumerate(text.splitlines(), 1):
        if re.match(r"^(Method|ClassMethod)\s+\w+\(", raw):
            if cur:
                out[cur] = buf
            cur = re.match(r"^(?:Method|ClassMethod)\s+(\w+)\(", raw).group(1)
            buf = []
            continue
        if cur:
            if raw == "}":                  # 方法结束：**列 0** 的 }（缩进的 } 属内部块）
                out[cur] = buf
                cur, buf = None, []
                continue
            s = raw.strip()
            if not s or s.startswith(";") or s.startswith("//") or s.startswith("///"):
                continue
            buf.append((i, raw))
    if cur:
        out[cur] = buf
    return out


def code(M, name):
    return M.get(name) or []


def first_idx(M, name, pred):
    for k, (_, line) in enumerate(code(M, name)):
        if pred(line):
            return k
    return -1


def has(M, name, sub):
    return any(sub in line for _, line in code(M, name))


if not CLS.exists():
    print("[check_engine_source] 找不到 %s" % CLS)
    sys.exit(2)
text = CLS.read_text(encoding="utf-8")
M = methods(text)
print("==== 静态守卫：父类通用聚合引擎（%s） ====" % CLS.name)
print("  解析到方法 %d 个；引擎方法：%s" % (
    len(M), [n for n in ("ProcessFHIRBundle", "ProcessChildLevel", "PackFHIRBundle", "BundleHttpOk",
                         "MakeResourceUuid", "BundleEntry", "InjectBundleRefs") if n in M]))

print("\n---- A. tSC 初始化顺序（单表布局 query_bos=[] 的回归守卫）----")
body = code(M, "ProcessFHIRBundle")
idx_init = first_idx(M, "ProcessFHIRBundle", lambda l: re.search(r"Set\s+tSC\s*=", l) is not None)
idx_loop = first_idx(M, "ProcessFHIRBundle", lambda l: "tDepth <= tMaxDepth" in l)
idx_read = first_idx(M, "ProcessFHIRBundle", lambda l: "$$$ISERR(tSC)" in l or "Quit tSC" in l)
check(idx_init >= 0, "A1 ProcessFHIRBundle 里有 tSC 初始化（Set tSC = $$$OK）")
check(idx_init >= 0 and idx_loop >= 0 and idx_init < idx_loop,
      "A2 tSC 初始化在深度循环之前（init=#%s < loop=#%s）" % (idx_init, idx_loop))
check(idx_read >= 0 and idx_init >= 0 and idx_init < idx_read,
      "A3 首次读取 tSC 晚于初始化（init=#%s < read=#%s）—— 本次缺陷：单表布局时循环体不执行" % (idx_init, idx_read))
check(any("$$$ISERR(tSC)" in l for _, l in body), "A4 循环内子层失败会中断（$$$ISERR(tSC) 判据在位）")

print("\n---- B. ProcessChildLevel：成功返回 + 层级记账 ----")
ccl = [l for _, l in code(M, "ProcessChildLevel")]
i_cnt = next((k for k, l in enumerate(ccl) if "pLevelCnt(pDepth)" in l and "=" in l), -1)
i_ok = next((k for k, l in enumerate(ccl) if "Quit $$$OK" in l), -1)
check(i_ok >= 0, "B1 成功路径以 Quit $$$OK 结束（tSC 才能取到有效值）")
check(i_cnt >= 0 and i_ok >= 0 and i_cnt < i_ok, "B2 层级记账（pLevelCnt）在返回前落位（第 %s / %s 行）" % (i_cnt, i_ok))
check(has(M, "ProcessChildLevel", "tFkCol") and has(M, "ProcessChildLevel", "SendRequestSync"),
      "B3 子表查询按布局 fk_col 派发（SendRequestSync）")
check(has(M, "ProcessChildLevel", "MakeResourceUuid"), "B4 子资源 UUID 经 MakeResourceUuid（确定性）")

print("\n---- C. MakeResourceUuid：FHIR urn:uuid 规范 ----")
check(has(M, "MakeResourceUuid", "$ZCONVERT") and has(M, "MakeResourceUuid", '"L"'),
      "C1 转小写（实测大写 urn:uuid 被 FHIR 判 MalformedRelativeReference）")
check(has(M, "MakeResourceUuid", "$JUSTIFY"), "C2 逐字节零填充（8-4-4-4-12）")

print("\n---- D. BundleHttpOk：回执严格校验 ----")
check(has(M, "BundleHttpOk", "$PIECE(tLine") and has(M, "BundleHttpOk", "299"),
      "D1 解析状态码并判定 2xx（非 2xx → 失败；GenericOperation 对 4xx/5xx 不报错）")
check(has(M, "BundleHttpOk", "OperationOutcome"), "D2 体内 OperationOutcome(error) 也算失败")
check("Quit ..BundleHttpOk(" in "\n".join(l for _, l in body), "E1 ProcessFHIRBundle 以回执校验结果返回（错误不被吞）")

print("\n==== E2. 缺契约必须显式失败（不静默）----")
for probe, label in (("缺少布局契约", "缺布局"), ("http_bo", "缺 http_bo"),
                     ("Bundle 消息构造失败", "Pack 失败"), ("患者资源组装失败", "患者组装失败")):
    check(probe in "\n".join(l for _, l in body), "E2 %s → 显式报错" % label)

print("\n==== F. 本轮修复的跨文件事实（宿主可见；容器内由 test_fhir_schema_facts.py 覆盖）====")
REPO = CLS.parents[3]


def _read(rel):
    p = REPO / rel
    return p.read_text(encoding="utf-8") if p.is_file() else ""


_lv = "\n".join(l for _, l in code(M, "LeafValue"))
check("system_from_row" in _lv and "tHasFact" in _lv and "tFromRow" in _lv,
      "F1 LeafValue 只认 schema 声明的 system_from_row（含旧 schema 兼容分支 tHasFact）")
check('ElseIf (tHasFact = 0)' in _lv,
      "F1b 旧 schema（无 coded 标记）仍沿用原兜底 → 未重生成的管道不会丢 system")
_rst = _read("tools/reset_ui_env.py")
check('"^demo.Trace"' in _rst, "F3 重置白名单含 ^demo.Trace（BP 痕迹不再跨轮残留误导）")
_repo = _read("backend/services/repository.py")
check("def list_keys" in _repo and "if raw is not None" not in _repo,
      "F4 repository.list_keys 不再按值过滤（仅下标的键也算 key）")
_tv = _read("backend/services/transformation_validator.py")
check("coded_text_gap" in _tv and "_coded_text_issues" in _tv,
      "F5 C1 新增「明文落 coding」检查（coded_text_gap）")
check("_field_terms_index" in _tv, "F6 C1 用资产 field_terms 事实（缺失即 fail-open）")
# 生成编排/拓扑构建已按职责分层（Step 4）：正文断言在 routes + pipeline_generate 两处查找
_pl = ((_read("backend/routes/pipelines.py") or "")
       + (_read("backend/services/pipeline_generate.py") or ""))
check("fhir_schema_facts" in _pl and "_fsf.apply_facts" in _pl,
      "F7 生成期注入组装事实（fhir_schema_facts.apply_facts）")
check("mappings=[m for _g in groups" in _pl and 'source_id=next((_g.get("source_id")' in _pl,
      "F8 生成期 FHIR 事实注入传 mappings+source_id（唯一调用点 = 多管道路径）")
_ftm = _read("backend/services/fhir_target_model.py")
check(all(c in _ftm for c in ("type_text", "reason_text", "route_text")),
      "F9 目标模型提供 *_text 落点（明文不再塞 coding.code）")
check("source_field_terms" in _read("backend/services/fhir_schema_facts.py"),
      "F10 事实服务提供源字段事实读取（source_field_terms）")

print("\n==== G. PARENT_API ⊆ 父类定义（宿主机核对；容器内类副本可能陈旧）====")
_gb = _read("backend/services/generated_bp.py")
_m = re.search(r"PARENT_API\s*=\s*\{([^}]*)\}", _gb, re.S)
_api = set(re.findall(r'"([A-Za-z%][A-Za-z0-9_]*)"', _m.group(1))) if _m else set()
_host_api = {"SendRequestSync", "SendRequestAsync", "SendRequest", "DeferResponse", "Reply",
             "SetTimer", "OnRequest", "OnResponse", "OnMessage", "%New", "%Save", "%OpenId",
             "%DeleteId", "%GetParameter", "%GetSetting", "%SetSetting", "%Validate"}
_need = _api - _host_api
_def_names = set(M)
_missing_api = sorted(x for x in _need if x not in _def_names)
check(bool(_need) and not _missing_api,
      "G1 PARENT_API 中属于父类的 %d 项都在 %s 里有定义（缺: %s）" % (len(_need), CLS.name, _missing_api))

print("\n==== H. 增量生成（P0–P4）接线事实（宿主可见）====")
_pu = _read("backend/routes/pipelines.py")
_route_count = len(re.findall(r"@pipelines_bp\.(?:get|post|delete|put)\(", _pu or ""))
check(_route_count >= 19,
      "J1 /api/pipelines 路由完整性（%d 条 ≥ 19）：Step 3 曾用行区间替换函数体，"
      "把 generate() 之后、generate_mock() 之前的 12 条路由一起删掉 → 重启后 404" % _route_count)
check("def generate_mock" in (_pu or "") and "def _mock_patients" in (_pu or ""),
      "J2 generate_mock / _mock_patients 未被误删")
_inc = [_read(r) for r in ("backend/services/pipeline_instances.py",)]
_inc.append((_read("backend/routes/pipelines.py") or "")
            + (_read("backend/services/pipeline_generate.py") or ""))
check("def input_signature" in _inc[0] and "def component_signature" in _inc[0]
      and "def is_unchanged" in _inc[0],
      "H1 服务层：input_signature / component_signature / is_unchanged")
check('return ""' in _inc[0].split("def pipeline_id_fallback_note")[0].split("def pipeline_id")[1],
      "H2 pipeline_id 缺身份返回空串（不再退化成 PIPE_<skill> 幽灵实例）")
check("def dedup_groups_by_identity" in _read("backend/services/pipeline_identity.py")
      and "dedup_groups_by_identity as _dedup_groups_by_identity" in _inc[1]
      and "def _dedup_groups_by_identity" not in _inc[1]
      and "PIPELINE_IDENTITY_MISSING" in _inc[1],
      "H3 同身份组去重 = **唯一实现**（services/pipeline_identity.py）+ 身份缺失拒绝")
check("def inc_input_signature" in _read("backend/services/pipeline_identity.py")
      and "inc_input_signature as _inc_input_signature" in _inc[1]
      and "def _inc_input_signature" not in _inc[1],
      "H3b 入参签名 = **唯一实现**（单一实现消除 S2「两处各算一次」的结构成因）")
check("跳过 Agent B" in _inc[1] and "render_skipped" in _inc[1],
      "H4 P1/P3：未变更复用组件、全未变更则免渲染重启")
check("_converge_component_enabled" in _inc[1] and "停用(调度)" in _inc[1],
      "H5 P2：许可调度 × 运行态收敛（调度停用的组**整组一致**，不被 P2 回填成半启用）")
check("_force_regen" in _inc[1], "H6 force 逃生开关在位")
_pv = _read("frontend/src/views/Pipelines.vue")
check("forceRegen" in _pv and "generateSkippedUnchanged" in _pv and "generateNothingChanged" in _pv,
      "H7 前端：只提交新增/变更组 + 强制重生成开关")
check("forceRegen" in _read("frontend/src/i18n/locales/zh.js")
      and "forceRegen" in _read("frontend/src/i18n/locales/en.js"),
      "H8 i18n（zh/en）已补增量文案")
_pl_src = _inc[1]
check('_g["_sig_in"] = _sig_in' in _pl_src and "必须**在此刻**固定签名" in _pl_src,
      "H9 多管道在 pop 运行契约前固定签名（否则入库签名 ≠ 比较签名 → 永远判『变更』）")
check("auto_join_existing_pipelines(" in _pl_src and "P1b" in _pl_src,
      "H10 P1b：未提交的既有管道按存储定义自动并入（只提交变更组也不会清掉别的管道）")
check("_generate_multi_pipelines([{" in _pl_src.split("def generate():")[-1]
      and "def _inc_input_signature" not in _pl_src,
      "H11 单管道与多管道走同一条链路（Step 3 合并：签名/校验只有一份实现）")
check("extra=\"allow\"" in _read("backend/schemas/models.py")
      and _pl_src.count("write_mapping_patch(_m)") >= 3,
      "H12 有损 DTO 修复 + 映射修正合并写回（不丢 source_id、不派生重复映射）")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
