# 测试数据工具箱（datakit）

演示/联调时**造数据、查数据、做运维**用的脚本集合。都是从仓库 `tools/`（少量根目录）**复制**过来的副本，
按「在哪执行」分成两个目录，配一个统一入口 `run.sh`，避免"复制到容器里跑 / 在宿主跑"搞混踩坑。

> 目的：把散落在 `tools/` 的几十个脚本里**真正用于加数据与查询**的那些集中起来，便于查阅和日常操作。
> 原脚本仍保留在原位（本目录是副本），更新方式见文末。

## 快速开始

```bash
cd tools/datakit

./run.sh list                                  # 看全部脚本（含用途）
./run.sh gen_test_patient.py --count 2 --family 赵 --given 敏 --diagnosis 糖尿病
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
| `host/gen_test_patient.py` ★ | **一键造 FHIR 测试数据**：造 CLINIC 患者 → 触发同步 → 校验 FHIR 落地/中文/引用 | `./run.sh gen_test_patient.py --count 2 --family 赵 --given 敏 --diagnosis 糖尿病 --drug 阿司匹林` |
| `host/seed_clinic.py` | 生成 CLINIC 演示数据（等价界面「生成演示数据」按钮） | `./run.sh seed_clinic.py` |
| `container/clinic_tables.py` | CLINIC 源库**四表初始化**（幂等，患者/就诊/诊断/药嘱） | `./run.sh clinic_tables.py` |
| `container/clinic_seed_data.py` | CLINIC 样例数据：10 患者 + 就诊/诊断/药嘱（术语只取中文术语集） | `./run.sh clinic_seed_data.py` |
| `container/add_one_patient.py` | **追加 1 个患者**（含就诊/诊断/药嘱，引用完整），**不清空**现有数据 | `./run.sh add_one_patient.py` |
| `container/generate_mock_data.py` | 生成 mock 数据（FHIR/SQL 两侧可分别指定条数） | `./run.sh generate_mock_data.py --fhir 5 --sql 3` |
| `container/seed_target_tables.py` | ★**直接往 USER 目标表** `SQLUser.Patient` / `SQLUser.Observation` 造测试数据（不经过管道） | `./run.sh seed_target_tables.py --count 3`（`--obs 2`、`--family 赵`、`--clear`） |
| `container/init_fhir_data.py` | 向 FHIR Server 提交示例资源（transaction Bundle） | `./run.sh init_fhir_data.py` |

`gen_test_patient.py` 常用参数：

| 参数 | 说明 |
|---|---|
| `--family 赵` / `--given 敏` | 患者姓名（中文，用于校验编码是否正确落地） |
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

---

## 三、运维操作（重置 / 重扫 / 许可 / LLM）

| 脚本 | 用途 |
|---|---|
| `container/reset_ui_env.py` ★ | **一键重置演示环境**（回到零起点）：删 Production → 清 SQL 源扫描凭证 → 清 `^demo.*`（含 `^demo.PipelineInstance` 管道实体，并有「白名单漏项」兜底告警）→ 清 Ens 消息 → 清目标表 → 清 CLINIC 四表 → 删 `/dur/generated` 生成物 → 删 FHIR 测试资源；**服务/命名空间/表结构保留**（凭证清理会**枚举 `^Ens.AppData` 一级下标**，覆盖按管道类别改名/去重的 Item） |
| `container/rescan_sql_source.py` ★ | SQL 源**全量重扫**（**无参 = 自动重置全部 SQL 源 BS**，也可传 Item 名子集）：先走官方 void API（`ClearStaticAppData`/`ClearRuntimeAppData`/`InitializeLastKeyValue`），失败退清 global；**自带「停 Production → 清凭证 → 启 → 核验重扫真的发生」**（`--timeout=90` 调等待上限、`--no-cycle` 只清凭证），`StartProduction != 1` 时**显式报错退出**（⚠ 运行期清凭证＝静默无效：适配器 last key 有内存副本） |
| `container/check_ens_clear_api.py` | Ens 清理 API 调用语义对照实测（`classMethodVoid` ✓ vs `classMethodValue` ✗，用独立测试 Item，不影响在跑管道） |
| `container/test_llm.py` ★ | **LLM 连通性自检**：配置 → `/models` → 模型名+max_tokens → `_call_llm` 真实链路 |
| `container/diag_demo_config.py` ★ | **查 `^demo.Config` 配置树**：`bp`（BP 自身参数，权威）/ `bp_target`（源 BS→BP 投递表）/ `pipe`（历史兼容路由表）/ `pipeline`（`active_mapping`·`target_type`·`topology`）/ `soap`；判断 BP 读的是哪一份参数 |
| `container/check_bp_configname.py` | 验证「BP 能读自己的 Item 名」：`Ens.Host||%ConfigName` 属性存在性（一条数据管道一个 BP 的前提） |
| `container/verify_multi_bp_pipeline.py` ★ | **零破坏验证一管道一 BP**（不落盘/不启动）：两组拓扑各自 BP（`TransformProcess__<类别>`）+ 源 BS 指向自己的 BP + shared 仅 JavaGateway + `RenderProduction` 文本含两个同类 Item |
| `host/regenerate_pipeline.py` | 按当前登记重新生成单管道（等价界面「生成」） |
| `host/gen_multi_pipeline.py` | 按目标分组组装多管道并触发生成 |
| `host/verify_license_budget.py` | 许可预算验证（连生成两条管道，检查自动让路） |
| `host/verify_license_scheduling.py` ★ | **许可调度验证**：超许可上限的分组生成即停用（不再 500）+ 一键切换自动让路；`--check` 只核对现有状态 |
| `host/verify_pipeline_instances.py` | **管道实体验证**（默认全量：重复生成不新增实体 + 类别回显 + 整条启停 + 许可守卫；`--contract-only`/`--items-only`/`--lifecycle-only` 可分别快跑） |
| `host/e2e_multi_pipeline_isolation.py` ★ | **e2e 验证「一条数据管道一个 BP」**：建 SQL 源/DB 目标/SOAP 目标/两条映射 + 造 CLINIC 数据 + 一次提交两条管道（sql2soap+sql2db）+ 核对各自 BP / shared 仅 JavaGateway；`--reuse` 复用已登记只跑生成 |
| `container/diag_sql_source_runtime.py` ★ | **查 SQL 源运行期**：`GetPersistentValue(item,"%LastKey")`（凭证非空=已消费过 → 重新生成同名 BS 后零消息的元凶）+ settings + 消息规模 |
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

1. **重置后不要再重启 backend**：`SEED_FHIR` 默认 1，重启会重新灌入 P001..P010 演示种子。
2. **许可上限**：IRIS 社区版许可单元 8 个，每个 Ens 业务主机常驻占 1 个；生成新管道时平台会自动让旧管道让路。
   运行期切换用 `POST /api/pipelines/items/toggle`，查看用 `GET /api/pipelines/items`（界面在 Pipelines 页「许可与组件」卡片）。
3. **改 `.env` 要用 `docker compose up -d backend`** 重建容器（`restart` 不会更新环境变量）。
4. **本目录的 `host/`、`container/` 是副本**，权威版本在仓库 `tools/`（少数在仓库根）；
   ⚠ 副本**不入库**（`.gitignore` 已忽略这两个目录），所以新克隆的仓库里只有 `run.sh` + 本 README，
   `./run.sh <脚本>` 会自动回退到 `tools/` 找同名脚本，功能不受影响；
   原始脚本更新后按需同步副本：

```bash
cd <仓库根>
cp tools/gen_test_patient.py tools/seed_clinic.py tools/check_fhir.py \
   tools/check_new_patient.py tools/check_name_encoding.py tools/clean_old_fhir.py \
   tools/del_probe_patient.py tools/regenerate_pipeline.py tools/gen_multi_pipeline.py \
   tools/verify_license_budget.py tools/e2e_diag.py tools/datakit/host/
cp tools/clinic_tables.py tools/clinic_seed_data.py tools/add_one_patient.py \
   tools/rescan_sql_source.py tools/reset_ui_env.py tools/test_llm.py \
   tools/list_prod_items.py tools/diag_msgs.py tools/diag_errors.py \
   tools/diag_shared_components.py \
   tools/check_pair_sink.py tools/dump_appdata.py tools/icd10_import.py \
   tools/rxnorm_import.py tools/uscore_condition_import.py \
   generate_mock_data.py init_fhir_data.py tools/datakit/container/
```

5. 想跑本目录之外的脚本（诊断/实验类如 `tools/diag_*.py`、`tools/test_*.py`）也可以直接用 `run.sh`：
   `./run.sh diag_name.py` —— 裸名在 `host/`、`container/` 找不到时会在 `tools/` 里查找同名脚本，并按依赖自动选执行环境。
