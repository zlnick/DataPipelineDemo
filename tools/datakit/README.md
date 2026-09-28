# 测试数据工具箱（datakit）

演示/联调时**造数据、查数据、做运维**用的脚本集合。都是从仓库 `tools/`（少量根目录）**复制**过来的副本，
按「在哪执行」分成两个目录，配一个统一入口 `run.sh`，避免"复制到容器里跑 / 在宿主跑"搞混踩坑。

> 目的：把散落在 `tools/` 的几十个脚本里**真正用于加数据与查询**的那些集中起来，便于查阅和日常操作。
> 原脚本仍保留在原位（本目录是副本），更新方式见文末。

## 快速开始

```bash
cd tools/datakit

./run.sh list                                  # 看全部脚本（含用途）

# 造数据：直接跑（脚本自带落地校验并打印结果），不要先查状态
./run.sh gen_test_patient.py --source user --count 3              # SQL→SOAP / SQL→DB：造 USER 库 SQLUser.Patient，校验目标落库
./run.sh gen_test_patient.py --count 2 --family 赵 --given 敏      # FHIR 口径：造 CLINIC 源，校验 FHIR 落地

./run.sh check_fhir.py                         # 查 FHIR 各资源数量
./run.sh diag_msgs.py                          # 查最近消息 + SQL 源扫描凭证
./run.sh reset_ui_env.py                       # 一键重置演示环境
```

`run.sh` 会**自动判断**该在宿主机还是容器内执行（依据脚本是否依赖 `backend.` 包 / `iris` 驱动），
也可以强制：`FORCE_HOST=1 ./run.sh xxx.py` / `FORCE_CONTAINER=1 ./run.sh xxx.py`。

## 目录结构

```
tools/datakit/
├── README.md      本说明
├── run.sh         统一入口（list / 自动选宿主或容器 / 传参）
├── host/          宿主机执行（访问 localhost:5001 后端 API、localhost:52773 FHIR）
└── container/     复制进 dataflow-backend 容器执行（用 backend 包、直连 IRIS 1972）
```

**为什么分两个目录**：`host/` 里的脚本用 HTTP 调后端/FHIR（容器内 `localhost` 指向容器自己，会连不上）；
`container/` 里的脚本 `import backend.services.*` 或 `iris.dbapi` 连 1972 端口（宿主没有 IRIS 驱动与 backend 包）。

---

## 一、造数据（最常用）

| 脚本 | 用途 | 用法示例 |
|---|---|---|
| `host/gen_test_patient.py` ★ | **一键造测试数据并校验落地**（`--source` 决定造哪一侧）：`clinic`＝造 CLINIC 源 → 校验 FHIR 落地/中文/引用；**`user`＝造 USER 库 `SQLUser.Patient` → 校验 SQL→SOAP / SQL→DB 目标落库**（SOAP 口径看 `SQLUser.PatientEntity`） | `./run.sh gen_test_patient.py --source user --count 3`（FHIR 口径：`./run.sh gen_test_patient.py --count 2 --family 赵 --given 敏 --diagnosis 糖尿病 --drug 阿司匹林`） |
| `host/seed_clinic.py` | 生成 CLINIC 演示数据（等价界面「生成演示数据」按钮） | `./run.sh seed_clinic.py` |
| `container/clinic_tables.py` | CLINIC 源库**四表初始化**（幂等，患者/就诊/诊断/药嘱） | `./run.sh clinic_tables.py` |
| `container/clinic_seed_data.py` | CLINIC 样例数据：10 患者 + 就诊/诊断/药嘱（术语只取中文术语集） | `./run.sh clinic_seed_data.py` |
| `container/add_one_patient.py` | **追加 1 个患者**（含就诊/诊断/药嘱，引用完整），**不清空**现有数据 | `./run.sh add_one_patient.py` |
| `container/generate_mock_data.py` | 生成 mock 数据（FHIR/SQL 两侧可分别指定条数） | `./run.sh generate_mock_data.py --fhir 5 --sql 3` |
| `container/seed_target_tables.py` | ★**直接往 USER 目标表** `SQLUser.Patient` / `SQLUser.Observation` 造测试数据（不经过管道） | `./run.sh seed_target_tables.py --count 3`（`--obs 2`、`--family 赵`、`--clear`） |
| `container/seed_fhir_demo.py` | **手动**灌 FHIR 演示样本（10 Patient + 30 Observation，PUT 幂等）。⚠ 已移出 backend 启动链（2026-09-16）：演示数据一律用时现造；只有需要"库里本就有历史存量"时才跑（跑完会把增量同步游标推进到当前时间） | `./run.sh seed_fhir_demo.py` |

`gen_test_patient.py` 常用参数：

| 参数 | 说明 |
|---|---|
| `--source user` | **造 USER 库 `SQLUser.Patient`**（供 **SQL→SOAP / SQL→DB** 管道），并校验目标落库（SOAP 口径 = `SQLUser.PatientEntity`）；不传则默认 `clinic`（造 CLINIC 源、校验 FHIR 落地） |
| `--family 赵` / `--given 敏` | 患者姓名（中文，用于校验编码是否正确落地；`--source user` 时默认「测试/患者N」） |
| `--count 2` | 生成几位患者 |
| `--diagnosis 糖尿病` / `--drug 阿司匹林` | 诊断/药品（走中文术语 → 判码链路） |
| `--force` | 强制全量重扫：停 Production → 清 SQL 源扫描凭证 → 重启 |
| `--no-verify` | 只造数据不做 FHIR 落地校验 |

⚠ 若数据"造了但管道没反应"，先确认：① Production 在运行；② 需要重扫时加 `--force`（或跑 `rescan_sql_source.py`）。

---

## 二、查询与核验

| 脚本 | 用途 | 用法 |
|---|---|---|
| `host/check_fhir.py` ★ | **查 FHIR 落地情况**：各资源数量 + 患者 id 是否确定性 UUID | `./run.sh check_fhir.py` |
| `host/check_name_encoding.py` ★ | **查中文是否正确落地**（打印原始 JSON，而非终端渲染） | `./run.sh check_name_encoding.py` |
| `host/check_new_patient.py` | 核实新患者（MRN-1003）整户资源及其引用关联 | `./run.sh check_new_patient.py` |
| `container/diag_msgs.py` ★ | 最近消息头 + SQL 源扫描凭证（判断重扫是否发生） | `./run.sh diag_msgs.py` |
| `container/diag_errors.py` | Ens 事件日志尾部（查运行错误） | `./run.sh diag_errors.py` |
| `container/check_pair_sink.py` | 双管道联调：SOAP 落库 `PatientEntity` + 最近业务消息 | `./run.sh check_pair_sink.py` |
| `container/list_prod_items.py` | Production 组件名与启用状态（确认 SQL 源 Item 名） | `./run.sh list_prod_items.py` |
| `container/diag_shared_components.py` ★ | **查共享组件到底谁在用**：`TransformProcess` 的 `TargetConfigNames` 派发关系 / `JavaGateway` 的 JGService 依赖方 / `^demo.Config("pipe",*)` 路由表 / `^demo.Config("pipeline","topology")` 渲染输入（看「使用者: 【无】」= 白占许可单元） | `./run.sh diag_shared_components.py` |
| `container/dump_appdata.py` | 排查 `^Ens.AppData`（已处理行 / 凭证 / 错误行） | `./run.sh dump_appdata.py` |
| `host/e2e_diag.py` | 管道状态 + 最近消息（结果写 `/tmp/diag2.log`，规避 shell 引号问题） | `./run.sh e2e_diag.py` |

不想写脚本时，直接用 API 查也行：

```bash
curl -s http://localhost:5001/api/pipelines/status                       # Production 是否在跑
curl -s "http://localhost:5001/api/pipelines/target-data?table=PatientEntity&limit=5"
curl -s http://localhost:5001/api/pipelines/items                        # 组件清单 + 许可
curl -s http://localhost:5001/api/datasources                            # 已登记数据源
curl -s "http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/Patient?_summary=count" -u superuser:SYS
```

**值传导验证**（比"看行数 / 看消息状态"更硬：改源值 → 重扫 → 按**新增资源 id 差集**核对目标字段）：

```bash
./run.sh verify_fhir_value_flow.py --ns CLINIC --table Patient --id P001 --col FamilyName \
    --repo fhirserver --type Patient --field 'name[0].family' --rescan
```

> 判据刻意避开三个已踩的坑：① 中文 `family` 查询在该服务器是**慢查询**（会假"未命中"）；
> ② 按**固定 id** 读（写入可能是 create 语义 → 固定 id 永远是旧值）；③ 靠"最新 lastUpdated"排序（同秒多条顺序不定）。
> 退出码 0 = 传导与还原均 PASS。

---

## 三、运维操作（重置 / 重扫 / 许可 / LLM）

| 脚本 | 用途 |
|---|---|
| `container/reset_ui_env.py` ★ | **一键重置演示环境**（回到零起点）：删 Production（**校验式停止** + `Ens.Config.Production` 记录 + 生成的生产类）→ 清 SQL 源扫描凭证 → 清 `^demo.*`（含 `^demo.PipelineInstance`，有「白名单漏项」兜底告警）→ 清 Ens **内部残留**（消息头/体、`Ens.StreamContainer`、`EnsLib_HTTP.GenericMessage(+/_HTTPHeaders)`、`Ens.BusinessProcess`、`Ens_Util.Log`；`Ens.BusinessProcess` SQL DELETE 被 SQL filer 拒时走 `%KillExtent` 兜底）→ 按 `/dur/generated/<短名>.cls` **来源指纹**删生成类 + 目录清空重建（**必须排在清表之前**：生成 BP 的表随类消失，顺序反了会撞 `-106 Row to DELETE not found`）→ **动态发现**清空 USER / CLINIC 命名空间**全部非系统表**（新增表自动覆盖；DELETE 报错退回「以行数为准」，表已随类删除不算残留）→ 删 FHIR **两个仓库**（源 DemoFHIR / 目标 FHIRSERVER）测试资源；**服务/命名空间/表结构保留**。末尾输出 **26 项自检 ✅/❌ 清单**（含**与 UI 同源**的 HTTP 接口核对，且自检**重新采样当前事实**而不是复用清理阶段清单）并 `exit 0/1` —— 不需要人工/模型再判断是否干净；`--check-only` 只查不改、`--skip-fhir` 跳过 FHIR |
| `container/rescan_sql_source.py` ★ | SQL 源**全量重扫**（**无参 = 自动重置全部 SQL 源 BS**，也可传 Item 名子集）：先走官方 void API（`ClearStaticAppData`/`ClearRuntimeAppData`/`InitializeLastKeyValue`），失败退清 global；**自带「停 Production → 清凭证 → 启 → 核验重扫真的发生」**（`--timeout=90` 调等待上限、`--no-cycle` 只清凭证），`StartProduction != 1` 时**显式报错退出**（⚠ 运行期清凭证＝静默无效：适配器 last key 有内存副本） |
| `container/check_ens_clear_api.py` | Ens 清理 API 调用语义对照实测（`classMethodVoid` ✓ vs `classMethodValue` ✗，用独立测试 Item，不影响在跑管道） |
| `container/term_map_build.py` ★ | **术语映射补录**（AI 判定 → 写回术语服务器）：预检（`term_precheck.precheck()`）列出服务器尚无的源编码（`missing`/`pending` = **待办清单**）→ 逐条走判定 Agent（`cn2snomed`→C3-Dx、`cn2rx`→C3，**LLM**）→ `POST /terminology/mapping/entry` 写回（`method=llm`、附 evidence/confidence）→ 复检覆盖率；`--dry` 只列不判、`--limit N` 控 LLM 次数、`--skill cn2snomed` 限一类、`--force` 连 negative 重判。⚠ 缺映射**不再中止生成**（2026-09-18 起默认降级：落源编码 + `meta.tag=unmapped`）；运行期由**共享 BO** 实时查询 ⇒ 补录后**无需重新生成管道** |
| `container/test_llm.py` ★ | **LLM 连通性自检**：配置 → `/models` → 模型名+max_tokens → `_call_llm` 真实链路 |
| `container/diag_demo_config.py` ★ | **查 `^demo.Config` 配置树**：`bp`（BP 自身参数，权威）/ `bp_target`（源 BS→BP 投递表）/ `pipe`（历史兼容路由表）/ `pipeline`（`active_mapping`·`target_type`·`topology`）/ `soap`；判断 BP 读的是哪一份参数 |
| `container/cleanup_dead_config.py` ★ | **清理 `^demo.Config` 死配置**（键/值指向**不存在组件**的 `bp`/`bp_target` 登记 = 没有读者）：判据与生成期收敛同一实现（`routes/pipelines.prune_stale_bp_config`），保护 `last_good` 存档；**缺省只查不改**，`--apply` 才删；组件清单为空时拒绝执行。**用**：`./run.sh cleanup_dead_config.py` 体检 → `--apply` 执行 |
| `container/check_bp_configname.py` | 验证「BP 能读自己的 Item 名」：`Ens.Host||%ConfigName` 属性存在性（一条数据管道一个 BP 的前提） |
| `container/verify_multi_bp_pipeline.py` ★ | **零破坏验证一管道一 BP**（不落盘/不启动）：两组拓扑各自 BP（`TransformProcess__<类别>`）+ 源 BS 指向自己的 BP + shared 仅 JavaGateway + `RenderProduction` 文本含两个同类 Item |
| `host/regenerate_pipeline.py` | 按当前登记重新生成单管道（等价界面「生成」） |
| `host/gen_multi_pipeline.py` | 按目标分组组装多管道并触发生成 |
| `host/verify_license_budget.py` | 许可预算验证（连生成两条管道，检查自动让路） |
| `host/verify_license_scheduling.py` ★ | **许可调度验证**：超许可上限的分组生成即停用（不再 500）+ 一键切换自动让路；`--check` 只核对现有状态 |
| `host/verify_pipeline_instances.py` | **管道实体验证**（默认全量：重复生成不新增实体 + 类别回显 + 整条启停 + 许可守卫；`--contract-only`/`--items-only`/`--lifecycle-only` 可分别快跑） |
| `host/e2e_multi_pipeline_isolation.py` ★ | **e2e 验证「一条数据管道一个 BP」**：建 SQL 源/DB 目标/SOAP 目标/两条映射 + 造 CLINIC 数据 + 一次提交两条管道（sql2soap+sql2db）+ 核对各自 BP / shared 仅 JavaGateway；`--reuse` 复用已登记只跑生成 |
| `container/diag_sql_source_runtime.py` ★ | **查 SQL 源运行期**：`GetPersistentValue(item,"%LastKey")`（凭证非空=已消费过 → 重新生成同名 BS 后零消息的元凶）+ settings + 消息规模 |
| `container/test_target_dsn.py` ★ | **验证 DSN 按 jdbc_url 命名空间归一**（演示默认 SQL 源 = USER / SQL 目标 = CLINIC）：A 契约推导 / B SQLOp 取值口径 / C 真实 IRIS 探针 DSN 建删 / **E 源侧命名 + 同名冲突守卫 + 登记不变量** / D 现状只读；**22 项** ✅ 判据 |
| `container/test_fhir_source_infra_components.py` ★ | **验证 FHIR 源共享生产者归属**（缺陷 J 回归）：多管道合并拓扑里 `FHIRSyncService` 恰好 1 个、`category=shared`、不在任何许可调度分组内、JavaGateway 仍 1 个；**9 项** ✅（纯离线，不调 LLM） |
| `container/test_target_type_awareness.py` ★ | **验证「按名取目标列」类型感知**（缺陷 M 回归）：`Patient` 同名跨类型（DB 9 列 vs FHIR 8 列）不混用、`_c1_target_models` 不给 DB 映射注入 FHIR 模型、C1 的 FHIR 必填/结构检查对 DB/SOAP 映射不触发、L1 不误剔除 DB 字段、FHIR 检查仍生效；**25 项** ✅（纯离线，不调 LLM、不写数据） |
| `container/test_term_gap_check.py` ★ | **验证术语双 coding 完整性检查（`term_gap`）与 AI 提示词条款**（十六轮遗留②回归）：目标列 note 要求『保留源编码 + 补充目标标准体系』而 transform 写成 `code`/`null` → 报 error（C1 才会调 LLM 用 `update_mapping` 补 `term_map`）；已有 `term_map` 静默、该列未映射交给 `fhir_required`、**源侧无编码列 fail-open 不误报**、非 FHIR 声明跳过（类型感知）；并断言 Agent A / C1 提示词的硬约束与受控指令注册表（`term_map` skills、`code`=类型提示）在位；**13 项** ✅（纯离线，不调 LLM、不写数据） |
| `container/test_term_gate_degrade.py` ★ | **验证术语门禁「默认降级放行 + 严格开关 + 待办清单」**（2026-09-18 口径）：无 `term_map` 决策 → skipped（完全不触发术语服务器）；**缺映射默认 `ok=True` 放行** + `todo` 清单 + 说明含「默认降级放行 / meta.tag / 补录命令」；`strict_terms=true` → `ok=False` + 说明含「严格模式」（调用方 400 `TERM_MAP_INCOMPLETE`）；**盘点异常默认也放行**（运行期降级）、严格模式才失败；生成后 `_term_summary` 有 todo 时并入 validation `warning`（`check=term_map_todo`）**不判失败**；补录口径含「补录后无需重新生成」；**23 项** ✅（纯离线：stub `term_precheck.precheck`） |

| `container/test_db_key_fact.py` ★ | **验证 DB 目标主键事实链**（2026-09-17 Round 2 缺陷回归）：主键事实解析（模型/登记优先 → JDBC 现场探查并回写登记 → 拿不到即 fail-open）、C1 的 `db_key`/`db_key_value` 检查（缺主键列/取不到值判 error，常量指令放行，FHIR/SOAP 映射按类型跳过）、子表查询派发检查**只看布局声明的查询 BO**（不把 DB 目标 SQLOperation 当查询 BO；单表来源跳过）、落地判定**有界等待**（竞态不误判）、**派发检查的有界等待**（BP 零消息时等首个业务消息，防"已有消息却从不派发"过网）；**33 项** ✅（纯离线，不调 LLM、不写数据） |
| `container/test_fhir_spec_fields.py` ★ | **验证 FHIR 源「无数据也能知道字段」**（2026-09-18）：平台规范快照 `fhir_target_model.source_field_paths`（已建模 11 类给出 `identifier[0]`/`name[0].family` 等标准元素路径；未建模 → 空）+ 采样计划把 **provenance=spec_model/ai_spec 的暂定字段重新采样**（数据优先长期成立）+ AI 规范字段清洗（非法形态/结构元素剔除、去重限量）+ `complete_fhir_resource_fields`（正常/限量/空输出→AgentError、只接受请求过的类型）+ 路由接线静态断言（回写资产、provenance 审计、未补到显式告警）；**25 项** ✅（纯离线，LLM 打桩） |
| `container/test_fhir_engine.py` ★ | **验证父类通用聚合引擎**（2026-09-19 Phase 3：把「遍历/子表派发/层级记账/引用注入/事务组装/回执校验」从 AI 生成的 BP **下移到 `demo.TransformProcess`**）：能力边界 `engine_supported`（root 条目 / 查询 BO 的 `mapping_id` / `depth ∈ {1,2}` / `http_bo`）+ 薄 BP `render_engine_bp` **过平台静态准入**且机制不在薄 BP 内 + `MakeResourceUuid` **小写 8-4-4-4-12**（FHIR `urn:uuid` 规范；实测大写被拒 → `MalformedRelativeReference`）+ 纯函数 `BundleEntry`/`InjectBundleRefs` + 回执校验严格性（live 证据）；**18 项** ✅（真 IRIS + 纯离线混合） |
| `tools/check_engine_source.py` ★（宿主） | **父类通用聚合引擎的静态守卫**（2026-09-19 实测缺陷回归：`ProcessFHIRBundle` 在**单表布局**（`query_bos=[]` → 深度循环体不执行）时未预置 `tSC` → `<UNDEFINED>*tSC` → `ErrBPTerminated`（消息 Error 而 FHIR 0 落地，多资源布局却正常）；**17 项** ✅ 且**自证有效**：把 `Set tSC = $$$OK` 删掉后 A 组立即 FAIL（16 PASS / 1 FAIL）。断言 tSC 初始化顺序 / `ProcessChildLevel` 层级记账落位 / `MakeResourceUuid` 小写零填充 / `BundleHttpOk` 查 2xx + OperationOutcome / 回执经校验返回 / 缺布局·缺 http_bo·组装失败必须显式报错 |
| `container/test_bp_plan_execute.py` ★ | **验证聚合 BP 的 Plan → Execute 链**（2026-09-19 新增；动机：整类生成 ≈34.5k completion / 单次 30~60 分钟且频繁 timeout）：计划 schema 校验（结构/命名唯一/父类 API 存在/必须含 OnRequest/方法数上限）+ 骨架生成（类头+签名+占位实现）+ **单方法静态准入**（多做方法/夹带类头/Try 内带参 Quit/未定义助手/response 赋 %DynamicObject）+ 编排（全量成功 / **断点续跑跳过已 ok** / 静态准入失败→**显式失败** / 单测失败带断言回喂重试）+ 单测三态汇总（passed/skipped/failed）；**28 项** ✅（纯离线：LLM/IRIS 全打桩） |
| `container/test_bp_static_admission.py` ★ | **验证生成 BP 的编译期硬约束静态准入**（2026-09-18 Round 6 缺陷回归 + 本轮扩 L/M 组）：跨行 `Try { … }` 内带参数 `Quit` → 拒（`#1043`，含实测样本与行号定位）、`As %Library.Object`（不存在的类）→ 拒（`#5373`）、**调用未定义助手** `..Foo(`（本类没定义且不在父类白名单）→ 拒（`MPP5376`）、旧父类 `Extends Ens.BusinessProcess` → 拒，而顶层 `Quit 变量`/单行 `Try{…}Catch{…}`/非 Try 的 `If () Quit 值`/Try 内**裸** `Quit`/父类稳定 API（`GetMappingFms`/`SendRequestSync` 等）/本类自定义助手 → 全部放行（不误报）；**29 项** ✅（纯离线，不调 LLM、不写数据） |
| `container/test_dead_config_prune.py` ★ | **验证死配置收敛清理**（Round 10 死配置治理 + 2026-09-18 扩到布局键）：造 3 条指向不存在组件的假键（`bp` / `bp_target` / **`sql2fhir.layout`**）→ `dry_run` **只报不删**、`apply` **只删死键**、组件在位的活键与**全局兜底键** `^demo.Config("sql2fhir","layout")` 完好、再跑**幂等**（removed 全空）；⚠ 判据「活键 = **组件在位**」——仅按"键已存在"会把无组件支撑的陈旧键误当活键（本自测首版即如此写错，已修正）；**12 项** ✅（真 IRIS，只动自造假键） |
| `container/test_terminology_bo.py` ★ | **验证共享术语 BO 的平台侧不变量**（2026-09-18 术语架构改造）：`TerminologyOperation` 是 `role=infra` —— **不进** Agent 可选枚举、**进** `get_common_components()`（拓扑校验放行）；有 `term_map` 决策 → 单/多管道拓扑自动挂 **1 个**实例（多组合并去重）；无决策 → **不挂**（省 1 个许可单元）；`_is_infra_component`=True、不进 `_active_items_from_topology`/`_topology_groups`（不参与许可调度、不被一键切换让路）；**20 项** ✅（纯离线） |
| `container/verify_terminology_bo_render.py` ★ | **零破坏验证共享术语 BO 的 Production 渲染**（不 Load、不启动、不写状态）：`RenderProduction` 文本里出现 `Item Name="TerminologyOperation" ClassName="demo.TerminologyOperation" … Category="shared"` + 三个 Host Settings（TermServer/TermPort/Timeout），且全局**仅 1 个实例**；**8 项** ✅ |
| `container/test_items_enable_disable.py` ★ | **验证组件启停「真的生效」（P3 回归）**：对一个无数据副作用的 BO 做往返 —— 启用后 `^Ens.Runtime Job=1` + 配置 `Enabled=1`；**停用后 `Job=0`（许可真的释放）**、`runtime_still_up` 为空、运行期调用被判为**真实执行而非空操作**；共享件（TerminologyOperation/JavaGateway/FHIRSyncService）全程不受牵连；正常路径 `converged_by_restart=False`；结束恢复原状态。**14 项** ✅（真 IRIS；只动一个 BO 的启停） |
| `container/test_target_columns_source.py` ★ | **验证 DB 目标列清单按「目标库」取**（2026-09-17 Round 2 缺陷回归）：登记事实优先 → 目标命名空间直连 → 当前命名空间兜底（+告警）；真实跨库探针证明 **CLINIC.Patient 9 列（含 MRN）≠ USER.Patient 8 列**（原实现取错库 → 目标多出的列被静默丢弃）；**10 项** ✅（含 4 项需 IRIS 的跨库探针） |
| `container/test_source_normalize.py` ★ | **验证映射 source 口径归一**（缺陷 P 的**上游收口**）：`SQLUser.Patient.ID → Patient.ID`（含多层前缀）、FHIR 路径/字面量/constant 不动、`concat` 参数递归归一、**目标字段与顺序完全不变**（只改写法）、幂等；**18 项** ✅（纯离线） |
| `container/test_pipeline_component_isolation.py` ★ | **验证多管道组件实例隔离 + 显式派发目标**（缺陷 A4/A6）：同类别多组（两个 SQL 源都选 `sql2fhir-patient-tx`）时，聚合 BP + 其查询 BO + HTTP BO **一管道一实例**（组件名唯一、源 BS 指向自己的 BP、DSN 各自正确）、每组记录**专属组件名** `_own_names`（管道实体归属不按 category 取并集 → 一键切换不连带启停孪生管道）；单组时名字保持原样（不加无谓后缀）；命名确定可复现；**A6 新增 E 组**：每组算出**显式派发目标**（`items{表/服务→主机名}` / `item`）、值都在拓扑里（不悬空）、sql2fhir 布局的 `query_bos[].bo_name` 与 `http_bo` 已按最终组件名对齐、BO 用非约定名时键从 Query 的 `INTO` 解析、查询 BO（SELECT）不进派发表、派发目标悬空 → **生成期显式失败**；**26 项** ✅（纯离线，不调 LLM/IRIS） |
| `test_source_path_resolve.py` ★ | **验证映射 source 限定名解析**（2026-09-17 Round 2 缺陷回归）：`schema.表.列` 三段限定名 / `表.列` / 裸列名 / FHIR 路径 / 数组 / dict 归一 / 末段大小写兜底；`transform_resource_json` 端到端产出含主键的非空行；`term_cache._jobs_from_mappings` 三段限定名正确定位源表（不再静默丢判码任务）。⚠ 模块分处两个容器：A/B/C 段在 **IRIS 容器**跑（`docker exec -i dataflow-iris python3 - < tools/test_source_path_resolve.py`），D 段在 **backend 容器**跑（`./run.sh test_source_path_resolve.py`）；两边各跑一次即全覆盖（17 项 + 3 项） |
| `container/test_fhir_schema_facts.py` ★ | **验证 FHIR 组装 schema 的「事实注入」+「明文落 coding」检查**（2026-09-19 用户报文实测缺陷回归）：`Encounter.reasonCode` 国标码缺 system、`MedicationRequest.route`＝「口服」被误挂药品目录体系。A 事实计算（coded/system/`system_from_row`：源列 field_terms + 源表有无 CodeSystem 列 + 术语 Skill 的 `source_system`）/ B 注入（**不覆盖模型静态体系**如 class_code=ActCode、深拷贝、无事实原样返回）/ C `coded_text_gap` 检查（type/route 报 error 且给出 `*_text` 修法；真编码列/固定体系列/引用列/无事实 fail-open 均不报）/ D 接线静态断言；**31 项** ✅（纯离线；iris/tools 未挂载时对应项显式跳过，宿主侧由 `check_engine_source.py` 覆盖） |
| `container/test_mapping_identity.py` ★ | **验证映射身份含「数据源维度」**（2026-09-17 **缺陷 A** 修复 / 3 源 × 3 目标共存前置）：缺陷复现（无 `source_id` 时 3 源 × 3 目标的 9 条同名映射塌成 3 条）→ 修复后 9 条共存互不覆盖；幂等、历史数据认领、**绝不跨源覆盖**、同身份归并 + 管道引用改写、N10 末段归一回归、`/ai/recommend` 出口回填 `source_id`、`/mappings` 入口回填（未解析显式回报）、分组回落**声明数据源优先 + 歧义守卫**（`_datasource_for_mappings` / `_target_for_mappings` 不再猜错源/目标）；**32 项** ✅（纯离线，stub 掉 global 读写，不碰 IRIS） |
| `container/test_source_ambiguity.py` ★ | **验证「同名资产跨数据源不得静默猜测归属」**（2026-09-19 用户手测缺陷回归）：两个源都有 `SQLUser.Patient` 时，旧实现用 `setdefault` 把名字 `patient` 压成**第一个源** → 本属 Clinic 的 `Patient → Patient(FHIR)` 映射被静默填成 USER 源 → 按 (源,目标) 分组后 Clinic 组缺主表 → sql2fhir 布局推导 500。现：**重名不进索引**（不猜）、保存入口**不回填**并回报 `source_ambiguous`、`/ai/recommend` **请求资产优先**（登记里同名源不覆盖，无回归）、唯一名字照旧命中、缺主表错误附「该映射归属哪个源 / 本组源是谁 + 两条修法」；**20 项** ✅（纯离线：登记读取 monkeypatch） |


| `host/clean_old_fhir.py` | 清理 FHIR 里非 UUID 格式的旧残留资源 |
| `host/del_probe_patient.py` | 删除探针患者（保持演示数据干净） |

---

## 四、术语库灌数（演示术语转换前置，可选）

| 脚本 | 用途 |
|---|---|
| `container/icd10_import.py` | 国标 ICD-10 → `Terminology_Icd10.Concept` |
| `container/rxnorm_import.py` | RxNorm RRF 导入：概念表 + 共享向量层 |
| `container/uscore_condition_import.py` | US Core Condition 值集 SNOMED 样本 → `Terminology_SnomedUs` |

更多（未复制，需要时用 `./run.sh <裸名>` 直接跑）：`tools/dx_vectorize.py`（术语全量向量化）、
`tools/term_embed.py`（通用向量化）、`tools/uscore_zhmap.py` / `tools/rxnorm_zhmap.py`（中文映射表灌数）。

---

## 五、运行环境对照

| | `host/` | `container/` |
|---|---|---|
| 在哪执行 | 你的终端（仓库根） | `dataflow-backend` 容器内 |
| 能访问 | `localhost:5001`（后端）、`localhost:52773`（FHIR） | `iris:1972`、backend 包、`/app` |
| 典型依赖 | 只用 Python 标准库（urllib / json / base64） | `import iris`、`iris.dbapi`、`from backend.services import ...` |
| `run.sh` 判断依据 | 无上述依赖 | 命中 `iris.dbapi` / `from backend.` 等关键字 |

> 这是本工具箱唯一"必须记住"的一点：容器里的 `localhost` 是容器自己，不是你的 Mac。
>
> ⚠ **Windows / WSL 补充**：`run.sh` 的宿主分支已自动处理两件事（2026-09-28 实测）——
> ① **强制 UTF-8 输出**（Windows 控制台默认 GBK，装不下脚本里的 `①…⑪` 等字符：
> `python tools/e2e_ui_flow.py` 曾在 ⑪ 抛 `UnicodeEncodeError: 'gbk' codec`，
> 现在统一加 `-X utf8` / `PYTHONUTF8=1`）；
> ② WSL 下**改用 Windows 侧 `python.exe`**（NAT 模式下 WSL 的 `localhost` 到不了 Windows 上发布的容器端口 —— 实测 `curl http://localhost:5001` → 000，
> 而 `host/` 脚本写的都是 `http://localhost:5001`）。

---

## 六、场景速查（"我要…"）

| 我想… | 命令 |
|---|---|
| 造 2 位糖尿病患者并验证 FHIR 落地 | `./run.sh gen_test_patient.py --count 2 --family 赵 --given 敏 --diagnosis 糖尿病` |
| 再追加 1 位患者（保留现有数据） | `./run.sh add_one_patient.py` |
| 看 FHIR 现在有多少数据 | `./run.sh check_fhir.py` |
| 看中文有没有变成 `?` | `./run.sh check_name_encoding.py` |
| 数据造了但管道没动 | `./run.sh diag_msgs.py` → 再 `./run.sh diag_errors.py` |
| 强制重新扫描 SQL 源 | `./run.sh rescan_sql_source.py`（无参=全部 SQL 源 BS，自带停/启 + 核验；或 `gen_test_patient.py --force`） |
| 清空环境重新演示 | `./run.sh reset_ui_env.py` |
| 看管道实体状态 / 重复生成有没有新增 | `./run.sh verify_pipeline_instances.py --items-only`（或 `curl -s localhost:5001/api/pipelines/instances`） |
| 换了 LLM，验证能否调通 | `./run.sh test_llm.py` |
| 看哪些组件启用 / 许可够不够 | `./run.sh list_prod_items.py` 或 `curl -s localhost:5001/api/pipelines/items` |
| 怀疑有组件白占许可（谁知道它在给谁用） | `./run.sh diag_shared_components.py` → 见「使用者: 【无】」即零使用者；`POST /api/pipelines/items/toggle {"name":"…","enabled":false}` 释放 |
| 重新生成管道 | `./run.sh regenerate_pipeline.py` |

---

## 七、注意事项

1. **后端启动不再灌 FHIR 数据**（2026-09-16 起：`SEED_FHIR` 默认 0，且播种脚本已移出启动链）：
   演示数据一律用时现造（`./run.sh gen_test_patient.py …`）；需要历史存量时手动 `./run.sh seed_fhir_demo.py`。
   ⚠ 但 `restart/up backend` **仍会重跑 `init_data.py`（DROP+重建 5 张目标表）**——重置后别重启 backend。
2. **许可上限**：IRIS 社区版许可单元 8 个，每个 Ens 业务主机常驻占 1 个；生成新管道时平台会自动让旧管道让路。
   运行期切换用 `POST /api/pipelines/items/toggle`，查看用 `GET /api/pipelines/items`（界面在 Pipelines 页「许可与组件」卡片）。
3. **改 `.env` 要用 `docker compose up -d backend`** 重建容器（`restart` 不会更新环境变量）。
4. **本目录的 `host/`、`container/` 是副本**，权威版本在仓库 `tools/`（少数在仓库根）；
   ⚠ 副本**不入库**（`.gitignore` 已忽略这两个目录），所以新克隆的仓库里只有 `run.sh` + 本 README，
   `./run.sh <脚本>` 会自动回退到 `tools/` 找同名脚本，功能不受影响；
   原始脚本更新后**一键同步副本**（推荐）：

```bash
bash tools/datakit/sync.sh            # 同步所有"已有副本"（单向 tools/ → datakit/，绝不反向）
bash tools/datakit/sync.sh --check    # 只报告差异（有差异退出码 1）；并报出孤儿副本
bash tools/datakit/sync.sh --add <脚本名> --to host|container   # 给新脚本登记副本
```

   ⚠ **副本过期会造成「测试假绿/假红」**（`run.sh` 优先执行副本）→ 改完 `tools/` 下的脚本后请跑一次 `sync.sh`。
   （历史做法是一长串手工 `cp`，已由上述脚本取代；需要时可在 git 历史里查回。）

5. 想跑本目录之外的脚本（诊断/实验类如 `tools/diag_*.py`、`tools/test_*.py`）也可以直接用 `run.sh`：
   `./run.sh diag_name.py` —— 裸名在 `host/`、`container/` 找不到时会在 `tools/` 里查找同名脚本，并按依赖自动选执行环境。
