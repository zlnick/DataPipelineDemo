# -*- coding: utf-8 -*-
"""离线回归（2026-09-19）：**管道增量生成**（P0–P4）—— 身份稳定 / 不改就复用 / 免重启。

背景（用户实测反馈）：
  · 生成新管道（SQL→FHIR）时，老的 SQL→SOAP 也被重跑 Agent B、重渲染、整份重启；
  · 两次提交同一条 SQL-SOAP 管道 → 因某次 `source_id` 为空（同名资产跨源平台不猜）而走了
    `PIPE_<skill>` 兜底 → 登记成**两套实例**。

修复（本测试锁定）：
  P0  管道实体存**完整组件定义**（`ai_components`，含 infra）+ 输入签名 + 应用签名；
  P0a 身份**只认 `(source_id, target_id)`**（LLM 的 skill 不参与身份）；缺身份**拒绝生成**；
      提交内同身份组**合并**；既有管道信息（身份/skill/mapping_ids/签名）注入 Agent B；
  P1  输入未变 → 复用已存组件、**跳过 Agent B**；P3 全部未变且组件在位 → **跳过重渲染/重启**；
  P2  未变更管道的 `enabled` 在许可调度后**按快照回填**（生成新管道不重置老管道）；
  P4  前端只提交新增/变更组 + `force=true` 逃生开关（静态断言）。

本测试纯离线（registry/IRIS 均打桩）：
  A 身份稳定性（含"两套实例"回归）      B 签名（入参/定义）语义
  C 复用与免重启的判定（is_unchanged / upsert 存定义）  D 静态接线断言（后端 + 前端）
用法（容器内）：docker exec -i dataflow-backend python - < tools/test_incremental_pipeline.py
"""
import copy
import os
import sys

PASS = 0
FAIL = 0

ROOT = os.environ.get("REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for _p in (ROOT, "/app"):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % msg)
    else:
        FAIL += 1
        print("  ✗ %s" % msg)


from backend.services import pipeline_instances as PI          # noqa: E402
from backend.services import repository as repo                # noqa: E402
from backend.routes import pipelines as PL                     # noqa: E402

# ---- 内存替身：^demo.PipelineInstance ----
STORE: dict = {}
repo.set_json = lambda g, k, d: STORE.__setitem__(k, copy.deepcopy(d))       # noqa: E731
repo.get_json = lambda g, k: copy.deepcopy(STORE.get(k))                     # noqa: E731
repo.list_json = lambda g: [copy.deepcopy(v) for v in STORE.values()]        # noqa: E731
PI.pipeline_validator.production_items = lambda: [                           # noqa: E731
    {"name": "SQLService_Patient__sql2soap", "enabled": 1, "category": "sql2soap"},
    {"name": "TransformProcess__sql2soap", "enabled": 1, "category": "sql2soap"},
    {"name": "SOAPOp_PatientService__sql2soap", "enabled": 1, "category": "sql2soap"},
]
PI.reconcile_states = lambda **kw: []                                        # noqa: E731

print("==== A. 身份稳定（P0a）====")
STORE.clear()
check(PI.pipeline_id("DS1", "TG1") == "PIPE_DS1_TG1", "A1 身份 = (source_id, target_id)")
check(PI.pipeline_id("DS1", "TG1", design_skill="sql2soap")
      == PI.pipeline_id("DS1", "TG1", design_skill="完全不同的skill"),
      "A2 **LLM 选什么 skill 都不影响身份**（同一业务键 → 同一 id）")
check(PI.pipeline_id(None, "TG1") == "" and PI.pipeline_id("DS1", None) == "",
      "A3 缺业务键 → 空 id（不再退化成 PIPE_<skill> 幽灵身份）")
check("源数据源" in PI.pipeline_id_fallback_note(None, "TG1"),
      "A4 身份缺失说明可读（点名缺哪一项）")

COMPS = [{"type": "SQLService", "name": "SQLService_Patient", "className": "EnsLib.SQL.Service.GenericService",
          "category": "sql2soap", "settings": [{"target": "Adapter", "name": "DSN", "value": "CLINIC"}]},
         {"type": "JavaGateway", "name": "EnsLib.JavaGateway.Service", "className": "EnsLib.JavaGateway.Service",
          "category": "shared"}]
MAPS = [{"id": "R1", "source": "Patient", "target_table": "PatientEntity", "target_type": "SOAP",
         "field_mappings": [{"source": "Patient.ID", "target": "PatientNo"}]}]
SIG1 = PI.input_signature("DS1", "TG1", MAPS, "FHIR", "SOAP")
PI.upsert_from_generation(source_id="DS1", target_id="TG1", source_type="FHIR", target_type="SOAP",
                          design_skill="fhir2soap", category_hint="fhir2soap", mapping_ids=["R1"],
                          components=COMPS, ai_components=COMPS, signature=SIG1)
PI.upsert_from_generation(source_id="DS1", target_id="TG1", source_type="FHIR", target_type="SOAP",
                          design_skill="**换了个skill**", category_hint="fhir2soap", mapping_ids=["R1"],
                          components=COMPS, ai_components=COMPS, signature=SIG1)
check(len(STORE) == 1, "A5 **两次提交同一管道 → 仍只有 1 条实例**（%d）" % len(STORE))
rec = STORE["PIPE_DS1_TG1"]
check(rec.get("generation_count") == 2 and "**换了个skill**" in (rec.get("skill_history") or [""]) + [rec.get("design_skill")],
      "A6 生成次数递增 + skill 变更记入历史（skill_history）")
check(rec.get("ai_components") and len(rec.get("ai_components")) == 2,
      "A7 **存了完整组件定义**（ai_components，含 infra）→ 供复用")
check(rec.get("signature") == SIG1, "A8 存了入参签名（signature）")
try:
    PI.upsert_from_generation(source_id=None, target_id="TG1", source_type="FHIR", target_type="SOAP",
                              mapping_ids=["R1"], components=COMPS)
    check(False, "A9 缺身份必须拒绝登记")
except ValueError as exc:
    check("源数据源" in str(exc), "A9 缺身份**拒绝登记**（不造幽灵实例）：%s" % str(exc)[:60])
check(len(STORE) == 1, "A10 拒绝后实例数不变（%d）" % len(STORE))


print("\n==== B. 签名语义 ====")
SIG_A = PI.input_signature("DS1", "TG1", MAPS, "FHIR", "SOAP")
SIG_B = PI.input_signature("DS1", "TG1", MAPS, "FHIR", "SOAP")
check(SIG_A == SIG_B, "B1 同输入 → 同签名（确定性）")
MAPS2 = copy.deepcopy(MAPS)
MAPS2[0]["field_mappings"][0]["target"] = "FullName"
check(PI.input_signature("DS1", "TG1", MAPS2, "FHIR", "SOAP") != SIG_A,
      "B2 **映射内容变了 → 签名变**（不会把『改了映射』误判为未变更）")
check(PI.input_signature("DS1", "TG2", MAPS, "FHIR", "SOAP") != SIG_A, "B3 目标变了 → 签名变")
check(PI.input_signature("DS1", "TG1", MAPS, "FHIR", "SOAP", extra={"dsn": "CLINIC"})
      != PI.input_signature("DS1", "TG1", MAPS, "FHIR", "SOAP", extra={"dsn": "USER"}),
      "B4 运行契约（DSN）变了 → 签名变")
C1 = [dict(COMPS[0], enabled=True), dict(COMPS[1])]
C2 = [dict(COMPS[0], enabled=False), dict(COMPS[1])]
check(PI.component_signature(C1, ["R1"]) == PI.component_signature(C2, ["R1"]),
      "B5 定义签名**不含 enabled**（启停属运行态，不算『变更』）")
check(PI.component_signature(C1, ["R1"]) != PI.component_signature(C1, ["R2"]),
      "B6 定义签名含 mapping_ids（映射集变了算变更）")
C3 = copy.deepcopy(C1)
C3[0]["settings"][0]["value"] = "USER"
check(PI.component_signature(C3, ["R1"]) != PI.component_signature(C1, ["R1"]),
      "B7 组件设置（DSN）变了 → 定义签名变")
check(PI.component_signature(C1, ["R1"]) == PI.component_signature(list(reversed(C1)), ["R1"]),
      "B8 组件顺序不敏感（顺序不同不算变更）")

print("\n==== C. 复用 / 免重启判定 ====")
unchanged, hit = PI.is_unchanged("DS1", "TG1", SIG1)
check(unchanged and (hit or {}).get("ai_components"),
      "C1 输入未变 → 判定为未变更且可复用（取到已存组件）")
unchanged2, _ = PI.is_unchanged("DS1", "TG1", "别的签名")
check(not unchanged2, "C2 签名不同 → 判为变更（需重新生成）")
unchanged3, _ = PI.is_unchanged("DS9", "TG9", SIG1)
check(not unchanged3, "C3 不存在的身份 → 非未变更")
check(PI.find_instance("DS1", "TG1") is not None and PI.find_instance("DS1", "TG9") is None,
      "C4 find_instance 按业务身份命中/未命中")
check(PI.set_applied_signature("PIPE_DS1_TG1", "abc123")
      and (STORE["PIPE_DS1_TG1"].get("applied_signature") == "abc123"),
      "C5 可记录『本次实际应用』的合并签名（P3/诊断用）")

print("\n==== D. 组去重 / 身份校验（多管道路径）====")
_g1 = {"source_id": "DS1", "target_id": "TG1", "source_type": "FHIR", "target_type": "SOAP",
       "mappings": [{"id": "R1"}]}
_g2 = {"source_id": "DS1", "target_id": "TG1", "source_type": "FHIR", "target_type": "SOAP",
       "mappings": [{"id": "R2"}]}
_g3 = {"source_id": "DS2", "target_id": "TG1", "source_type": "SQL", "target_type": "SOAP",
       "mappings": [{"id": "R3"}]}
_dedup, _merged = PL._dedup_groups_by_identity([_g1, _g2, _g3])
check(len(_dedup) == 2, "D1 同身份两组 → 合并成 1 组（总组数 3→2，%d）" % len(_dedup))
check([m["id"] for m in _dedup[0]["mappings"]] == ["R1", "R2"],
      "D2 合并后映射并集（不丢：%s）" % [m["id"] for m in _dedup[0]["mappings"]])
check(bool(_merged) and _merged[0]["identity"] == ["DS1", "TG1"], "D3 合并动作被显式回报（可审计）")
check(PL._group_identity(_g1) == ("DS1", "TG1"), "D4 身份取 (source_id, target_id)")
check(PL._group_components({"ai_components": COMPS}) == COMPS
      and PL._group_components({}) == [], "D5 复用组件取值（缺则空，不报错）")
_seq = {"source_id": "DS1", "target_id": "TG1", "source_type": "FHIR", "target_type": "SOAP",
        "mappings": MAPS, "_rt_src": {"connection": {"endpoint": "http://x"}},
        "_rt_tgt": {"connection": {"endpoint": "http://y"}}}
check(PL._inc_input_signature(_seq) == PL._inc_input_signature(dict(_seq)),
      "D6 组入参签名确定性（后端路径可用）")

print("\n==== E. 静态接线断言（后端 + 前端）====")


def _src(rel):
    for base in (os.environ.get("REPO_ROOT"), ROOT, os.getcwd(), "/app", "/"):
        if not base:
            continue
        p = os.path.join(base, rel)
        if os.path.isfile(p):
            return open(p, encoding="utf-8").read()
    return None


def check_file(rel, cond, msg):
    text = _src(rel)
    if text is None:
        print("  – 跳过 %s（%s 未挂载；宿主侧由 tools/check_engine_source.py 覆盖）" % (msg, rel))
        return
    check(cond(text), msg)


_pl = _src("backend/routes/pipelines.py") or ""
_pi = _src("backend/services/pipeline_instances.py") or ""
check("_dedup_groups_by_identity" in _pl and "PIPELINE_IDENTITY_MISSING" in _pl,
      "E1 多管道：身份校验 + 去重接线在位")
check("def _inc_input_signature" not in _pl and "def _dedup_groups_by_identity" not in _pl
      and "from backend.services.pipeline_identity import" in _pl,
      "E1c 身份/签名**已无本地实现**（唯一实现模块 + 别名导入）")
# 单/多管道签名口径必须一致（否则单管道重放永远判"变更"→ 重渲染把别的管道整份换掉）
_single_sig = _pl.split("# ⚠ 签名口径必须与多管道路径")[-1]
check("_inc_input_signature({" in _single_sig, "E1b 单管道路径复用 `_inc_input_signature`（两条路径签名口径一致）")
check("跳过 Agent B" in _pl, "E2 P1：未变更组复用组件并跳过 Agent B（日志可审计）")
check("render_skipped" in _pl, "E3 P3：全部未变且组件在位 → 跳过重渲染/重启")
check("_converge_component_enabled" in _pl and "停用(调度)" in _pl,
      "E4 许可调度 × 运行态收敛（**调度停用的组整组一致**，P2 不覆盖调度结论）")
check("_force_regen" in _pl, "E5 force 逃生开关（强制全量重生成）在位")
check('_g["_sig_in"] = _sig_in' in _pl and "必须**在此刻**固定签名" in _pl,
      "E5b 多管道在 Agent B 分支 pop 运行契约**之前**固定签名（否则入库签名与比较签名不一致）")
check("_auto_join_existing_pipelines(" in _pl and "P1b" in _pl,
      "E5c P1b：未提交的既有管道自动并入（只提交变更组也不会清掉别的管道）")
check("reused=bool(_g.get(\"_unchanged\"))" in _pl, "E5d 复用组不虚增生成次数")
check("所有数据管道均已存在且未变更" in _pl, "E5e 未变更时给出明确消息（不营造『已生成』假象）")
check("单管道请求：检测到其它" in _pl and "_generate_multi_pipelines([{" in _pl,
      "E5f 单管道请求在**存在其它有效管道**时转多管道路径（不整份替换 → 别家管道不消失）")
check("ai_components" in _pl and "component_signature" in _pl,
      "E6 登记时写入组件定义与定义签名")
check("input_signature" in _pi and "component_signature" in _pi and "is_unchanged" in _pi,
      "E7 服务层提供 input_signature / component_signature / is_unchanged")
check_file("frontend/src/views/Pipelines.vue",
           lambda t: "forceRegen" in t and "generateSkippedUnchanged" in t and "instances()" in t,
           "E8 前端：只提交新增/变更组 + 强制重生成开关")
check_file("frontend/src/i18n/locales/zh.js",
           lambda t: "forceRegen" in t and "generateNothingChanged" in t,
           "E9 i18n（zh）已补增量相关文案")
check_file("frontend/src/i18n/locales/en.js",
           lambda t: "forceRegen" in t and "generateNothingChanged" in t,
           "E10 i18n（en）已补增量相关文案")

print("\n==== F. 有损 DTO / 合并写回（本轮定位到的真实缺陷）====")
from backend.schemas.models import MappingItem as _MI   # noqa: E402

_dto = _MI(**{"id": "M1", "source": "Patient", "source_id": "DS1", "target_id": "TG1",
              "status": "confirmed", "target_table": "Patient", "target_type": "FHIR",
              "ai_reason": "自定义附加字段", "field_mappings": [{"source": "Patient.ID", "target": "identifier"}],
              "field_mappings_extra": "x"})
_dump = _dto.model_dump()
check(_dump.get("source_id") == "DS1" and _dump.get("target_id") == "TG1",
      "F1 `MappingItem` 透传数据源/目标身份（不再丢 → 不派生重复映射）")
check(_dump.get("status") == "confirmed" and _dump.get("ai_reason") == "自定义附加字段",
      "F2 `extra=allow`：未知字段原样透传（杜绝同类静默丢字段）")
STORE.clear()
PI.upsert_from_generation(source_id="DS1", target_id="TG1", source_type="SQL", target_type="FHIR",
                          design_skill="fhir2db", category_hint="fhir2db", mapping_ids=["M1"],
                          components=COMPS, ai_components=COMPS)
repo.set_json("^demo.Mapping", "M1", {"id": "M1", "source": "Patient", "source_id": "DS1",
                                      "target_table": "Patient", "target_type": "FHIR",
                                      "field_mappings": [{"source": "Patient.ID", "target": "identifier"}]})
PL._write_mapping_patch({"id": "M1", "field_mappings": [{"source": "Patient.ID", "target": "identifier"},
                                                        {"source": "Patient.MRN", "target": "mrn"}]})
_patched = STORE.get("M1") or {}
check(_patched.get("source_id") == "DS1",
      "F3 映射修正写回**保留** `source_id`（合并写，不整条覆盖）")
check(len(_patched.get("field_mappings") or []) == 2, "F4 修正内容确实生效（字段数 2）")
_pl_text = _src("backend/routes/pipelines.py") or ""
check(_pl_text.count("_write_mapping_patch(_m)") >= 5
      and 'set_json("^demo.Mapping", _m["id"]' not in _pl_text,
      "F5 所有映射写回点都走合并写（不再有整条覆盖）")
check("extra=\"allow\"" in (_src("backend/schemas/models.py") or ""),
      "F6 `MappingItem` 已加 extra=allow（有损 DTO 修复在位）")

print("\n==== G. 未提交的既有管道自动并入（P1b）====")
STORE.clear()
repo.list_mappings = lambda: list(STORE.get("__maps", {}).values())            # noqa: E731
repo.get_datasource = lambda i: {"id": i, "type": "SQL",                                  # noqa: E731
                                 "runtime": {"connection": {"dsn": "USER"}}} if i else None
repo.get_target = lambda i: {"id": i, "type": "SOAP", "config": {"service": "PatientService"},  # noqa: E731
                             "runtime": {"connection": {"endpoint": "http://mock"}}} if i else None
repo.datasource_runtime = lambda ds: (ds or {}).get("runtime") or {}          # noqa: E731
repo.target_runtime = lambda tg: (tg or {}).get("runtime") or {}              # noqa: E731
STORE["__maps"] = {"M9": {"id": "M9", "source": "Patient", "target_table": "PatientEntity",
                          "target_type": "SOAP",
                          "field_mappings": [{"source": "Patient.ID", "target": "PatientNo"}]}}
_grp9 = {"source_id": "DS9", "target_id": "TG9", "source_type": "SQL", "target_type": "SOAP",
         "mappings": list(STORE["__maps"].values()),
         "_rt_src": {"connection": {"dsn": "USER"}}, "_rt_tgt": {"connection": {"endpoint": "http://mock"}}}
SIG9 = PL._inc_input_signature(_grp9)
# 完整定义 = 源 BS + 转换 BP + **目标 BO**（SOAPOp_*）——缺 BO 会被完整性守卫拒绝（G6）
COMPS_SOAP = COMPS + [{"type": "SOAPOperation", "name": "SOAPOp_PatientService",
                       "className": "EnsLib.SOAP.OutboundAdapter", "category": "sql2soap"}]
PI.upsert_from_generation(source_id="DS9", target_id="TG9", source_type="SQL", target_type="SOAP",
                          design_skill="sql2soap", category_hint="sql2soap", mapping_ids=["M9"],
                          components=COMPS_SOAP, ai_components=COMPS_SOAP, signature=SIG9,
                          source_config={"dsn": "USER"}, target_config={"service": "PatientService"})
joined = PL._auto_join_existing_pipelines([], {"PIPE_OTHER_X"})
check(len(joined) == 1 and joined[0].get("_unchanged") is True,
      "G1 未提交的既有管道被并入且判为**未变更**（复用组件、跳过 Agent B）")
check(joined and joined[0].get("ai_components") and joined[0].get("mappings"),
      "G2 并入组带存储组件定义 + 已登记映射（可整份渲染而不丢别的管道）")
joined2 = PL._auto_join_existing_pipelines([], {"PIPE_DS9_TG9"})
check(not joined2, "G3 已提交的组不重复并入（按业务身份去重）")
_rec9 = STORE["PIPE_DS9_TG9"]
_rec9["signature"] = "过了期的签名"
joined3 = PL._auto_join_existing_pipelines([], set())
check(joined3 and not joined3[0].get("_unchanged"),
      "G4 签名不一致（映射被改过）→ 不当作未变更（会真正重新生成）")
_rec9["signature"] = SIG9
_rec9["status"] = "superseded"
joined4 = PL._auto_join_existing_pipelines([], set())
check(not joined4, "G5 `superseded` 实例不并入（不复活幽灵管道）")
# G6/G7：存储定义完整性守卫（实测：历史实例缺目标 BO → 复用会渲染出不完整管道）
_rec9["status"] = "active"
_rec9["ai_components"] = [{"type": "SQLService", "name": "SQLService_Patient", "category": "sql2soap"}]
_rec9["components"] = list(_rec9["ai_components"])          # 冻结定义来源同样缺 BO
_rec9.pop("render_components", None)
joined5 = PL._auto_join_existing_pipelines([], set())
check(joined5 and not joined5[0].get("_unchanged"),
      "G6 存储定义**缺目标 BO** → 不算未变更（交回正常生成链路自愈）")
check(PL._stored_definition_complete(COMPS, "SOAP")[0] is False
      and PL._stored_definition_complete(
          COMPS + [{"type": "SOAPOperation", "name": "SOAPOp_PatientService",
                    "className": "EnsLib.SOAP.OutboundAdapter"}], "SOAP")[0] is True
      and PL._stored_definition_complete([], "SOAP")[0] is False
      and PL._stored_definition_complete(COMPS, "")[0] is True,
      "G7 完整性守卫判据（缺 BO 拒 / 有 BO 放行 / 空定义拒 / 未知目标类型不误拒）")
check(PL._stored_definition_complete(
        [{"type": "SQLService", "name": "SQLService_Patient"},
         {"type": "SOAPOperation", "name": "SOAPOp_PatientService"}], "SOAP")[0] is False,
      "G8 **缺 className 的定义判为不可复用**（否则渲染出不合规拓扑 → 拓扑校验 500）")

print("\n==== H. 复用组渲染保真（本轮实测缺陷：复用时丢了 SOAP 目标 BO）====")
_G = [{"source_id": "DS9", "target_id": "TG9", "source_type": "SQL", "target_type": "SOAP",
       "design_skill": "sql2soap", "_category": "sql2soap", "_unchanged": True,
       "target_config": {}, "mappings": list(STORE["__maps"].values()),
       "ai_components": COMPS_SOAP, "_frozen_components": COMPS_SOAP, "_frozen_infra": []}]
_topo = PL.build_multi_pipeline_topology(_G)
_names = [str(c.get("name")) for c in (_topo.get("components") or [])]
check(any(n.startswith("SOAPOp_") for n in _names),
      "H1 未变更（复用）组渲染后**目标 BO 仍在**（%s）" % _names)


def _present(n):
    return n in _names or any(x.startswith(n + "__") for x in _names)


_keep = [str(c.get("name")) for c in COMPS_SOAP
         if str(c.get("name")) != "EnsLib.JavaGateway.Service"]
check(all(_present(n) for n in _keep),
      "H2 存储定义里的组件**一个都不少**地被渲染（保真：%s ⊆ %s）" % (_keep, _names))
check(any(n.startswith("EnsLib.JavaGateway") for n in _names),
      "H3 基础设施（JavaGateway）仍由外层统一追加")
# H3b：复用组渲染时**源 BS 的投递目标必须改写为本次 BP 名**（否则消息投给别的管道 → 永久 Queued）
_BSB = [{"type": "SQLService", "name": "SQLService_Patient",
         "className": "EnsLib.SQL.Service.GenericService", "category": "sql2soap",
         "settings": [{"target": "Host", "name": "TargetConfigNames", "value": "TransformProcess__sql2soap"}]}]
_BPB = [{"type": "TransformProcess", "name": "TransformProcess__sql2soap",
         "className": "demo.TransformProcess", "category": "sql2soap"}]
_top2 = PL.build_multi_pipeline_topology(
    [{"source_id": "DS9", "target_id": "TG9", "source_type": "SQL", "target_type": "SOAP",
      "_category": "sql2soap", "_unchanged": True, "target_config": {}, "mappings": [],
      "ai_components": _BSB + _BPB, "_frozen_components": _BSB + _BPB, "_frozen_infra": []}])
_bs = next((c for c in (_top2.get("components") or []) if str(c.get("type")) == "SQLService"), {})
_tcn = next((str(s.get("value")) for s in (_bs.get("settings") or [])
             if str(s.get("name")) == "TargetConfigNames"), "")
check(_tcn == "TransformProcess__sql2soap",
      "H3b 复用组：源 BS 的 TargetConfigNames 指向**本次**的本组 BP（%s）" % _tcn)
# H4–H6：许可调度 × 运行态收敛（实测缺陷：调度停用的组被回填成"半启用"、或应启用的组被历史快照停掉）
_t = {"components": [{"name": "A_bs", "category": "sql2soap", "enabled": True},
                     {"name": "A_bp", "category": "sql2soap", "enabled": True},
                     {"name": "B_bs", "category": "sql2fhir-patient-tx", "enabled": True},
                     {"name": "J", "category": "shared", "enabled": True}]}
PL._converge_component_enabled(_t, {"suspended": ["sql2soap"]}, {"sql2soap"}, set())
check(all(c["enabled"] is False for c in _t["components"] if c["category"] == "sql2soap"),
      "H4 **被调度停用的组整组停用**（不允许残留半启用的 BO/BP 白占许可）")
check([c for c in _t["components"] if c["name"] == "B_bs"][0]["enabled"] is True
      and [c for c in _t["components"] if c["name"] == "J"][0]["enabled"] is True,
      "H5 调度判定应启用的组保持启用（不被历史快照停掉）/ 共享件不受影响")
_t2 = {"components": [{"name": "A_bs", "category": "sql2soap", "enabled": True},
                      {"name": "A_bp", "category": "sql2soap", "enabled": True}]}
PL._converge_component_enabled(_t2, {"suspended": []}, {"sql2soap"}, {"sql2soap"})
check(all(c["enabled"] is False for c in _t2["components"]),
      "H6 未变更但实例此前 suspended（用户选择）→ 保持停用")
print("\n==== I. 身份/签名「唯一实现」（Step 1 重构：消除『两处各算一次』）====")
from backend.services import pipeline_identity as _PI2      # noqa: E402

check(PL._inc_input_signature is _PI2.inc_input_signature,
      "I1 routes 的 `_inc_input_signature` 就是 services 的实现（**同一对象**，非副本）")
check(PL._group_identity is _PI2.group_identity
      and PL._dedup_groups_by_identity is _PI2.dedup_groups_by_identity,
      "I2 身份 / 去重 同为唯一实现")
check(PL._stored_definition_complete is _PI2.stored_definition_complete
      and PL._frozen_defs_from_instance is _PI2.frozen_defs_from_instance,
      "I3 完整性守卫 / 冻结定义 同为唯一实现")
check(PL._running_items is _PI2.running_items and PL._BO_PREFIX == _PI2.BO_PREFIX,
      "I4 运行态快照 / 目标 BO 前缀 同为唯一实现")
check(PL._inc_input_signature(_seq) == _PI2.inc_input_signature(_seq),
      "I5 同一输入经两条引用路径得到**同一签名**（S2 类缺陷结构上不可能再发生）")

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
