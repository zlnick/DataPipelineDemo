# -*- coding: utf-8 -*-
"""单元验证（2026-09-18 Round 6 缺陷回归）：**Agent 生成 BP 的编译期硬约束静态准入**。

背景（实测）：S2（SQL USER.Patient → FHIR Patient，**单表来源**）生成时，Agent 两次产出编译失败的
BP —— ① `Try { … Quit tSC … }` → `#1043 QUIT argument not allowed`；② 形参用 `As %Library.Object`
（该类**不存在**）→ `#5373 Class '%Library.Object' … does not exist`。提示词里已有 ① 的事实却仍被违反
（且白烧 2 轮 LLM 修复、把幻觉代码写进类里），故在生成端**静态准入**兜住：命中即回喂重写，不进入编译。

本测试锁死不变量（纯离线：不调 LLM、不连 IRIS、不写数据）：
A. 跨行 TRY 块内带参数的 `Quit` → 拒（含实测样本）；
B. 顶层 `Quit 变量` → 通过；
C. 单行自闭合 `Try { … } Catch e { … }` → 不误报；
D. 非 TRY 的 `If (…) { Quit 值 }` / `If () Quit 值` → 通过（合法写法）；
E. TRY 块内**裸** `Quit`（带参数才有 #1043）→ 通过；
F. 不存在的类 `%Library.Object` → 拒；合法类型（%Library.DynamicObject 等）→ 通过；
G. 真实缺陷样本（两条错误同现）→ 两条都报。
"""
import os
import re
import sys

sys.path.insert(0, "/app")

from backend.services import generated_bp as GB  # noqa: E402

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


HEAD = ("Class demo.SqlFhirPatientTxProcess Extends demo.TransformProcess\n{\n"
        "Method OnRequest(request As %Library.Persistent, Output response As %Library.Persistent) "
        "As %Status\n{\n")
TAIL = "    Quit $$$OK\n}\n}\n"


def bp(body):
    return {"class_name": "demo.SqlFhirPatientTxProcess", "source": HEAD + body + TAIL}


def errs(body):
    return GB.static_check_generated_bp(bp(body))


print("==== A. 跨行 TRY 块内带参数 Quit（实测 #1043 样本） ====")
A = ("    Try {\n"
     "        Set tSC=..SendRequestSync(\"SQLQueryOp_Encounter\",tReq,.tRsp)\n"
     "        Quit tSC\n"
     "    } Catch e { Set tSC=$$$ERROR($$$GeneralError,\"x\") }\n")
e = errs(A)
check(any("#1043" in x for x in e), "A1 Try 内 Quit tSC → 拒（%s）" % (e[:1],))
check(GB.try_block_quit_value_lines(A) == [3], "A2 定位到片段内行号 3（%s）" % GB.try_block_quit_value_lines(A))

A2 = ("    Try {\n"
      "        If (tPid=\"\") Quit $$$ERROR($$$GeneralError,\"missing\")\n"
      "    } Catch e { Set tSC=$$$ERROR($$$GeneralError,\"x\") }\n")
check(any("#1043" in x for x in errs(A2)), "A3 Try 内 `If () Quit 值` 同样拒（#1043）")

print("\n==== B. 合法：顶层 Quit ====")
check(errs("    Set tSC=$$$OK\n    Quit tSC\n") == [], "B1 顶层 `Quit tSC` → 通过")
check(errs("    Quit $$$OK\n") == [], "B2 顶层 `Quit $$$OK` → 通过")

print("\n==== C/D/E. 不误报 ====")
check(errs("    Try { Set tObj=##class(%DynamicObject).%FromJSON(pBody) } Catch e { Set tObj=\"\" }\n") == [],
      "C1 单行自闭合 Try{…}Catch{…} → 不误报")
check(errs("    If ($$$ISERR(tSC)) Quit tSC\n") == [], "D1 非 Try 的 `If () Quit 值` → 通过（合法）")
check(errs("    Try {\n        Set tB=\"\"\n        Quit\n"
           "    } Catch e { Set tSC=$$$ERROR($$$GeneralError,\"x\") }\n") == [],
      "E1 Try 内**裸** Quit（无参数）→ 通过")

print("\n==== F. 不存在的类（实测 #5373） ====")
BAD_CLS = ("\nClassMethod Peek(x As %Library.Object) As %String\n{\n    Quit \"\"\n}\n")
check(any("does not exist" in x for x in errs(BAD_CLS)), "F1 `As %Library.Object` → 拒（#5373）")
GOOD_CLS = ("\nClassMethod Peek(x As %Library.DynamicObject) As %String\n{\n    Quit \"\"\n}\n")
check(errs(GOOD_CLS) == [], "F2 `As %Library.DynamicObject` → 通过")

print("\n==== G. 真实缺陷样本（本轮 IRIS 日志原文两类错误同现） ====")
REAL = ("    Try {\n"
        "        Set tSC=..SendRequestSync(\"SQLQueryOp_X\",tReq,.tRsp)\n"
        "        Quit tSC\n"
        "    } Catch e { Set tSC=$$$ERROR($$$GeneralError,\"x\") }\n"
        + BAD_CLS)
e = errs(REAL)
check(len([x for x in e if "#1043" in x]) == 1 and len([x for x in e if "does not exist" in x]) == 1,
      "G1 两类错误各报 1 条（共 %d 条）" % len(e))

print("\n==== H. 引用清单必须从 layout.bundle 读（实测 2026-09-18 静默缺陷） ====")
BAD_REFS = ("    Set tLayout=##class(%DynamicObject).%FromJSON(pLayoutStr)\n"
            "    Set tRefs=tLayout.%Get(\"refs\")\n"
            "    If ('$IsObject(tRefs)) { Set tRefs=##class(%DynamicArray).%New() }\n")
check(any("layout 顶层" in x for x in errs(BAD_REFS)), "H1 `tLayout.%Get(\"refs\")` → 拒（引用注入会静默跳过）")
GOOD_REFS = ("    Set tLayout=##class(%DynamicObject).%FromJSON(pLayoutStr)\n"
             "    Set tBundleCfg=tLayout.%Get(\"bundle\")\n"
             "    Set tRefs=tBundleCfg.%Get(\"refs\")\n")
check(errs(GOOD_REFS) == [], "H2 先取 bundle 再取 refs → 通过")
CHAINED = ("    Set tRefs=tLayout.%Get(\"bundle\").%Get(\"refs\")\n")
check(errs(CHAINED) == [], "H3 链式 `%Get(\"bundle\").%Get(\"refs\")` → 通过（不漏不误）")

print("\n==== I. 运行期错误分类（引用缺陷必须回喂 BP 修复，不是改映射） ====")
from backend.services import pipeline_validator as PV  # noqa: E402

REAL_ERR = ('ERROR <Ens>ErrBPTerminated: Terminating BP SqlFhirPatientTxProcess__sql2fhir_patient_tx_2 '
            '# due to error: ERROR #5001: FHIR rejected: {"resourceType":"OperationOutcome","issue":'
            '[{"severity":"error","code":"invalid","diagnostics":"<HSFHIRErr>MalformedRelativeReference",'
            '"details":{"text":"The reference value \'X115835F\' in property (subject) of Type '
            '\'Encounter\' is malformed"},"expression":["Encounter.subject"]}]}')
cls = PV.classify_runtime_error(REAL_ERR)
check(cls.get("kind") == "bp_code", "I1 MalformedRelativeReference → bp_code（%s）" % cls.get("kind"))
check("bundle.refs" in str(cls.get("advice") or ""), "I2 修复建议指向 layout.bundle.refs")
cls2 = PV.classify_runtime_error('ERROR #5001: FHIR rejected: {"issue":[{"diagnostics":'
                                 '"MissingRequiredProperty"}]}')
check(cls2.get("kind") == "fhir_schema", "I3 其他 OperationOutcome 仍归 fhir_schema（未误伤）")

print("\n==== J. 单行 Try 内带参 Quit（2026-09-18 二次补漏：该形态曾绕过静态准入） ====")
J1 = ('    Try { Set tSC=$$$ERROR($$$GeneralError,"x") Quit tSC } Catch e { Set tSC=e.AsStatus() }\n')
check(any("#1043" in x for x in errs(J1)), "J1 单行 `Try { … Quit tSC } Catch` → 拒")
J2 = '    Try { Set tB="" Quit } Catch e { Set tB="" }\n'
check(errs(J2) == [], "J2 单行 Try 内**裸** Quit → 通过")
J3 = '    Try { If (tA="") { Quit $$$ERROR($$$GeneralError,"x") } } Catch e { Set tA="" }\n'
check(errs(J3) == [], "J3 单行 Try 内含**嵌套花括号** → 放过（保守：漏报不误报）")

print("\n==== K. 父类契约 + 实例方法必须用 ..（2026-09-18 共享术语 BO 改造） ====")
OLD_PARENT = ("Class demo.SqlFhirPatientTxProcess Extends Ens.BusinessProcess\n{\n"
              "Method OnRequest(request As %Library.Persistent, Output response As %Library.Persistent) "
              "As %Status\n{\n    Quit $$$OK\n}\n}\n")
check(any("Extends demo.TransformProcess" in x
          for x in GB.static_check_generated_bp(
              {"class_name": "demo.SqlFhirPatientTxProcess", "source": OLD_PARENT})),
      "K1 旧父类 `Extends Ens.BusinessProcess` → 拒（必须继承 demo.TransformProcess）")
BAD_STATIC = ("    Set tRes=##class(demo.TransformProcess).BuildFHIRResource(\"Patient\",pR,pS,pF)\n")
check(any("静态调用实例方法" in x for x in errs(BAD_STATIC)),
      "K2 `##class(demo.TransformProcess).BuildFHIRResource(...)` → 拒（实例方法必须用 ..）")
check(errs("    Set tRes=..BuildFHIRResource(\"Patient\",pR,pS,pF)\n") == [],
      "K3 `..BuildFHIRResource(...)` → 通过")

print("\n==== L. 未定义 `..助手(` 调用（2026-09-18 实测 MPP5376） ====")
L1 = ("    Set tFms=..GetMappingFms(tMid)\n")
check(errs(L1) == [], "L1 父类稳定 API `..GetMappingFms(...)`（未在本类定义）→ 通过")
L2 = ("    Set tFms=..ToFmsJSON(..GetFms(tMid))\n")
check(any("未定义的助手方法" in x for x in errs(L2)),
      "L2 本类没定义也没在父类白名单的 `..GetFms(...)` → 拒（%s）" % (errs(L2)[:1],))
L3 = ("ClassMethod GetFms(pID As %String) As %String\n{\n    Quit \"[]\"\n}\n"
      "Method Helper2()\n{\n    Do ..GetFms(\"x\")\n}\n")
check(errs(L3) == [], "L3 本类**有定义**的 `..GetFms(...)` → 通过")
L4 = ("    Set tSC=..SendRequestSync(\"SQLQueryOp_Encounter\",tReq,.tRsp)\n"
      "    Do ..Reply(tRsp)\n    Do ..SetTimer(1)\n")
check(errs(L4) == [], "L4 Ensemble 宿主 API（SendRequestSync/Reply/SetTimer）→ 不误报")
check(GB.undefined_helper_calls(L2) == ["GetFms"], "L5 检出清单精确为 ['GetFms']（%s）"
      % GB.undefined_helper_calls(L2))

print("\n==== M. PARENT_API 与父类定义一致性（IRIS 活类 优先，文件副本次之） ====")
# 2026-09-19 修：原实现只读容器内类文件，而 backend 容器**不挂载 iris/** → 落到陈旧的
#   /tmp/TransformProcess.cls（实测 Sep-18 副本，缺引擎方法）→ M1 误报"缺 7 个方法"。
#   现优先读 **IRIS 活类**（%Dictionary，与平台上真正编译运行的定义一致），文件仅作兜底。
_defs: set[str] = set()
_src_desc = ""
try:
    # ⚠ 用**独立 dbapi 连接**：iris_connector 的连接可能已被前序查询占用/复用（实测返回 1 行空值）
    import iris.dbapi as _dbapi
    _conn = _dbapi.connect(hostname="iris", port=1972, namespace="USER",
                           username="superuser", password="SYS")
    _cur = _conn.cursor()
    _cur.execute("SELECT Name FROM %Dictionary.MethodDefinition WHERE parent=?",
                 ("demo.TransformProcess",))
    _defs = {str(r[0]).rpartition("(")[0].strip() for r in _cur.fetchall() if r and r[0]}
    _cur.close()
    _conn.close()
    _src_desc = "IRIS 活类 demo.TransformProcess（%d 个方法）" % len(_defs)
except Exception as _exc:  # noqa: BLE001
    print("  … IRIS 字典读取失败（%s），改用类文件" % str(_exc)[:80])
if len(_defs) < 5:                      # 健全性：活类应有多个方法，否则退化为文件口径
    _defs = set()
if not _defs:
    _cls = None
    for _p in ("/shared/src/demo/TransformProcess.cls", "/app/iris/src/demo/TransformProcess.cls",
               "/tmp/TransformProcess.cls"):
        if os.path.exists(_p):
            _cls = _p
            break
    if _cls:
        _txt = open(_cls, encoding="utf-8", errors="replace").read()
        if "ProcessFHIRBundle" not in _txt:
            print("  … 跳过 M1：容器内类副本陈旧（%s 无引擎方法）；"
                  "宿主侧 tools/check_engine_source.py 已覆盖该断言" % _cls)
            _cls = None
        else:
            _defs = set(re.findall(r"^\s*(?:Class)?Method\s+([A-Za-z%][A-Za-z0-9_]*)", _txt, re.M))
            _src_desc = _cls
if not _defs:
    print("  … 跳过：既读不到 IRIS 活类，也无可读类文件")
else:
    _parent = {x for x in GB.PARENT_API if x not in {
        "SendRequestSync", "SendRequestAsync", "SendRequest", "DeferResponse", "Reply", "SetTimer",
        "OnRequest", "OnResponse", "OnMessage", "%New", "%Save", "%OpenId", "%DeleteId",
        "%GetParameter", "%GetSetting", "%SetSetting", "%Validate"}}
    _missing = sorted(x for x in _parent if x not in _defs)
    check(not _missing, "M1 PARENT_API 中属于父类的 %d 项都在 %s 里有定义（缺: %s）"
          % (len(_parent), _src_desc, _missing))

print("\n==== 结果: %d PASS / %d FAIL ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
