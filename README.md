# AI 数据自动化转换 Demo

> **[English version → README.en.md](README.en.md)**

基于 **InterSystems IRIS for Health** 与 **AI** 的数据自动化转换演示平台。

用户通过界面指定数据源（FHIR 接口），平台**自动分析端点 Profile** 并注册数据资产；指定转换目标（模拟远端数据库）；通过 **AI（OpenAI 兼容 LLM）** 推荐「资产 → 目标表」匹配与字段映射，经用户确认后**自动生成 IRIS 互操作性生产管道（Production）** 完成数据投放。

```
FHIR 端点 → 自动分析 Profile → 注册数据源 → 发现数据资产(资源类型)
      ↘                                                  ↙
         AI 匹配「资产 → 目标表」 + 生成字段映射 → 用户确认
      ↘                                                  ↙
           UI 生成转换关系 → IRIS Production 管道 → 投放模拟远端数据库
```

## 功能清单

### ✨ AI 决策范围：什么由大模型决定、什么不是

平台的「**决策与生成**」全部由运行时大模型（LLM）完成，代码只负责**读取事实、参数化、校验、保底补齐**，绝无写死的映射/拓扑/结论模板：

| 能力 | AI（LLM）决策 | 代码只做 |
| --- | --- | --- |
| 接口 / 数据源分析 | 资产业务语义、轮询键建议、目标写/读方向、运行契约解读 | 读取事实（Capability/列/WSDL）、写回 |
| 数据映射 | 资产→目标匹配与字段映射（支持 `concat()` 等表达式） | 结构归一、完整性校验 |
| 数据管道 | 组件构成与顺序、命名（含多管道逐组） | 注册表补 className/settings、必需件补齐 |
| 验证与修复 | 判定问题是否实质 + 选择修复动作 | 事实检查工具、机械剔除、显式降级 |

**AI 驱动红线**：LLM 失败 = 面向用户的明确失败（缺 key / 超时 / 输出不合规，均带 Agent 名报错），**绝不静默改用规则结果**；规则/注册表仅做 ①参数化 ②完整性校验 ③保底补齐（补齐在返回标注 `ai_supplemented`）。每次生成都带可审计的 `ai` 信息（driven / components / supplemented / c2_rule_rebuilt），LLM 调用留 token 日志。

### ✨ AI 验证与自动修复（验证-修复闭环）

- **C1 转换验证-修复**：映射确认后/管道生成前，用事实检查 + LLM 判断修正字段映射（列存在性/路径/语义错配）。
- **C2 管道验证-修复**：生成后自动验证**拓扑 / 编译 / 启动 / 消息流转**（单、多管道统一管线）。
- **自动修复**：事实检查工具 → **LLM 决策修复** → 重新生成验证，≤2 轮；失败自动重建一次；仍未解决则**沉淀经验**到 `^demo.ValidationIssue`，并在后续修复中**自动回注给 AI** 作为参考（避免重复踩坑）。
- **可观察**：Pipelines 页「🧾 AI 审计日志」按钮展示每次生成的决策来源；backend 日志留每个 Agent 的 token 用量。
- **知识闭环**：验证经验经**知识润色 Agent（LLM）**去重研读，导出为 Obsidian 知识库笔记，供人沉淀复用（`export_validation_issues.py`）。

### ✨ 管道增量生成（以管道为单位）

生成不再"每次全量重算"，而是**以数据管道为单位增量**：

- **身份稳定**：同一 `(源数据源, 目标)` 恒为**同一条管道** —— 无论提交几次、Agent 选了哪个设计 Skill，
  都只更新既有管道（不新增、不产生"两套实例"）。
- **未变更即跳过**：输入（映射内容 / 源·目标契约）没变 → **复用已存组件定义、不重跑 Agent B、不重启 Production**
  （响应 `unchanged=true` / `render_skipped=true`，界面提示"所有数据管道均已存在且未变更"）。
- **只生成变更组**：界面只提交新增/变更的组；**未提交的既有管道按存储定义自动并入**（不会被"整份替换"清掉），
  其运行态（启停 / 许可 / 扫描凭证）保持不变。
- **重复提交免疫**：提交里出现同身份重复组时自动合并为一条，响应回报 `dup_merged`（可审计）。
- **强制重建**：需要重新设计时打开界面 **「强制重新生成」**（`force=true`）。

### ✨ AI 能力目录（Tools / Workflows / Skills / Agents + Skill 目录）

「AI Agents」页按业界口径（Anthropic《Building effective agents》）把平台能力**归类并逐条给出依据**：

| 归类 | 含义 | 本项目条目 |
| --- | --- | --- |
| **Tool** | 确定性、**无 LLM** 的可调用单元 | 连接探查 / 连通门禁 / 事实检查（`pipeline_validator.check_*`）/ 规则检查 / BP 静态准入 / 术语缺口盘点 / 目标列与主键事实 / WSDL 实体分析 / 术语检索 BO（`demo.TerminologyOperation`）/ 类型与 FHIR 模型注册表 |
| **Workflow** | LLM 参与，但**执行路径由代码预定** | 接口分析（工具采集 + 单轮 LLM 归纳）、数据管道设计 Agent B（决策一次 → 平台代码路径渲染） |
| **Skill** | 打包的指令/知识，**单步**、无工具循环 | 数据转换生成（A）、知识润色、术语判定（C3 / C3-Dx：单轮判定 + 术语检索 Tool） |
| **Agent** | **工具 + 多轮自主循环 + 目标** | 转换验证-修复（C1）、管道验证-修复（C2） |

页面同时展示 **Skill 目录**（= AI 决策用的受控清单，`GET /api/agents/skills`）：
**管道设计 Skill ×6**（`sql2fhir-patient-tx` / `sql2db` / `fhir2db` / `sql2soap` / `fhir2soap` / `fhir2fhir`，
含适用"源 → 目标"、状态与组件拓扑角色）与 **术语判码 Skill ×2**（`cn2snomed` / `cn2rx`，含源·目标体系与判定 Agent），
并显示**使用次数**（当前环境实际命中）。口径：平台只按目录**参数化**，选哪个 Skill 仍由 AI 决定。

### 分层模型与 AI 转换

平台将数据转换拆分为三个独立层次，而不是假设“源表 → 目标实体”一一对应：

1. **源资产模型**：描述 FHIR 资源、SQL 表及其字段、类型、主键和关系。
2. **目标接口模型**：描述数据库表或 SOAP WSDL Operation 的 Request 实体、嵌套字段和约束。
3. **转换计划**：由 AI 基于两类模型生成，表达多表聚合、拆分消息、JOIN、分组以及字段映射；用户确认后才用于生成管道。

转换计划可通过 `/api/source-assets`、`/api/target-interfaces` 和
`/api/transformation-plans` 管理，并可通过 `/api/ai/verify` 执行事实验证。
只有用户在管道监控页面点击“生成 / 重建数据管道”后，系统才会调用 AI 设计
Production 拓扑并交给 IRIS 编译启动。

- **数据源（FHIR / SQL）**：
  - FHIR：注册端点后自动分析 CapabilityStatement（Profile / 资源类型 / 操作），发现 FHIR 资源资产。
  - SQL：JDBC 向导（联通测试 → 选 schema → 选表 → 分析列结构），自动生成轮询 Query，源资产=所选表。
- **转换目标（DB / SOAP）**：
  - DB：JDBC 向导（联通测试 → 选 schema → 勾选目标表 → 分析列结构）。
  - SOAP：WSDL 导入型（读 WSDL 自动生成 BO + 实体分析），数据管道把**转换后的实体**作为请求消息投递；
    内置示例为**写入型 AddPatient**（扁平三字段），被调系统由 Python mock 承担（`backend/services/mock_soap.py`，
    收到实体后落库 `PatientEntity` 表并返回回执）。
- **连接运行契约（Connection Contract）**：添加源/目标时由**连接探查 Agent**
  （`connection_profiler`）探测并产出归一 `runtime` 契约（connection / capabilities / poll / delivery / health）——
  FHIR 增量能力、SQL 轮询增量键、SOAP **操作语义判定**（写入型/查询型）、端点可达性。
  该契约为前端向导、AI 上下文、管道生成、自动验证的**单一参数来源**（详见 `docs/ConnectionContract-设计.md`）。
- **AI 智能匹配与转换关系**：AI 推荐「资产 → 目标表/实体」匹配与字段级映射（支持 `concat()` 等表达式），用户确认保存。
- **数据管道（单 Production 可多管道）**：一键生成 IRIS Production 管道，支持异构组合（FHIR→DB / SQL→DB / SQL→SOAP / FHIR→SOAP）
  以及**单 Production 多套并存**（`POST /api/pipelines/generate` body `pipelines: [组1, 组2]`）：
  - 转换 BP **一条管道一个实例**（Ens 业务主机身份 = Item 名，类 `demo.TransformProcess` 可复用，
    如 `TransformProcess__sql2soap`）：本管道源 BS 的 `TargetConfigNames` 指向**自己的** BP，
    转换参数写在 `^demo.Config("bp", <BP名>)` —— 管道之间**零耦合**，可整条启停（许可随管道释放）；
    真正跨管道共享的只剩基础设施 `JavaGateway`（JDBC 网关，恒需）
  - FHIR 增量：`FHIRSyncService`（`_lastUpdated` 游标）→ `FHIRQueue` → `FHIRService`（逐条独立会话）→ 转换 → 投放
  - SQL 轮询：`EnsLib.SQL.Service.GenericService`（Query/KeyFieldName 增量）→ 行 JSON → 转换 → 投放
- **数据管道 = 受管理的持久实体**：每次生成登记一条管道实体（源数据源 + 目标接口 + 设计 Skill），
  重复生成**只更新不新增**（记录生成次数）；组件按 Ens `Category` = 管道类别落地，
  Pipelines 页「数据管道」卡片可**整条启用/停用/删除/同步**，并能看到许可占用（业务主机数 + 后端连接 ≤ 许可单元，
  超容量显式报错而不是把后端打挂）；被新生成取代的管道标记为**已取代（superseded）**，只允许删除后重新生成。
- **自动测试-修复闭环**：generate 前置**连通性检查**（用运行时契约）→ C1 转换验证 → C2 管道验证
  （拓扑/编译/启动/消息）→ 分层修复（规则 → AI ≤2 轮 → 回退），多管道同样走 C2。
- **动态选项**：前端页面（资产/目标/可查看数据表）的选项**由演示过程登记的内容动态生成**（API 驱动，非写死）。
- **管道监控**：实时消息流转日志（Ens.MessageHeader 真实消息历史）、目标表落库结果（动态可选表）。


## 技术架构

| 组件 | 技术栈 |
| ---- | ------ |
| 数据库 / 集成引擎 | InterSystems IRIS for Health（社区版，`containers.intersystems.com/intersystems/irishealth-community:2026.1`） |
| FHIR 数据源 | IRIS 自带 FHIR Server（核心 R4 `hl7.fhir.r4.core@4.0.1`，FHIRSERVER namespace） |
| 后端 API | Flask + IRIS Native SDK（`intersystems-irispython`）+ OpenAI SDK |
| 前端 | Vue 3 + Element Plus + Vite + nginx |
| AI | OpenAI 兼容接口（`base_url` / `api_key` / `model` 可配） |
| 编排部署 | Docker Compose（三容器） |

**IRIS 多角色（单实例）**：
- **FHIRSERVER namespace**：FHIR Server（实例自带）——**演示默认的 FHIR 目标仓库**（转换结果落这里），
  端点 `http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/`
- **DEMOFHIR namespace（第二个 FHIR 存储库）**：与 FHIRSERVER 同构的**独立** FHIR 仓库（主库 `DEMOFHIR` +
  数据仓库库 `DEMOFHIRX0001R/V`），端点 `http://localhost:52773/csp/healthshare/demofhir/fhir/r4/`；
  与 FHIRSERVER **数据完全隔离**（同一资源 id 互不可见）——**演示默认的 FHIR 源仓库**（UI 数据源表单、
  `FHIRConfig.BASE_URL`、模拟数据脚本都默认指向它）。
  创建：`iris/setup.sh` 步骤 2b（容器启动即幂等创建）；运行中的实例可重复执行 `python3 tools/create_fhir_repo.py`（带 15 项自检）
- **USER namespace**：转换平台——目标表（模拟远端库/落库）、互操作性 Production、Mapping 与运行契约配置

**演示数据表（SQLUser schema，结构与语义）**：
| 表 | 语义 | 数据来源 |
| ---- | ---- | ---- |
| `Patient` / `Observation` | FHIR→DB 目标落库（FHIR 资源转换写入） | FHIR 管道 / 亦可用于 SQL 源（向导自行选择即可） |
| `PatientSource` | SQL 源演示表（与 Patient 同结构，模拟“第三方业务库”） | 手工/脚本插入，供 SQL→SOAP 管道轮询 |
| `PatientEntity` | SOAP 投递结果（Python mock 收到 AddPatient 实体后落库） | mock 写入 |
| `FHIRQueue` | FHIR 增量抓取队列表（FHIRSyncService 入队，FHIRService 消费） | FHIRSyncService |

> **命名空间默认口径（2026-09-16）**：演示默认 **SQL 源 = `USER` 命名空间**（`SQLUser.Patient` / `PatientSource`）、
> **SQL 目标 = `CLINIC` 命名空间**（跨库写入演示）；两边 DSN 都由登记 jdbc_url 的命名空间推导
> （`jdbc:IRIS://iris:1972/CLINIC` → DSN `CLINIC`）。要写平台内置目标表（`Patient`/`Observation`，在 `USER`）
> 就把 DB 目标的 URL 改回 `jdbc:IRIS://iris:1972/USER`。

**数据管道（Production）**：转换 BP 类 `demo.TransformProcess`（Embedded Python 字段映射转换，支持 `concat()` 表达式与 `表.列` 前缀）——
**每条数据管道各建一个 BP 实例**（Ens 业务主机身份 = Item 名，如 `TransformProcess__sql2soap`；类可复用，管道互不干扰）：
- FHIR 源：`FHIRSyncService`（`_lastUpdated` 增量游标）→ `FHIRQueue` → `FHIRService`（逐条独立会话）→ 本管道的转换 BP
- SQL 源：`EnsLib.SQL.Service.GenericService`（JDBC 轮询，Query + KeyFieldName）→ 行 JSON → 本管道的转换 BP
- 目标：`SQLOp_<表>`（JDBC UPSERT；**DSN 按目标登记 jdbc_url 的命名空间推导** —— 演示默认 SQL 目标 = `CLINIC` 命名空间 → DSN `CLINIC`，无 jdbc_url 才回落 `localTarget`）；`SOAPOp_<服务>`（WSDL 导入 BO + Adapter WebServiceURL 指向远端/mock）
- 参数与路由：BP 读**自己的**配置 `^demo.Config("bp", <BP名>)`（mapping / target_type / service|table），
  源 BS 经 `TargetConfigNames`（或 `^demo.Config("bp_target", 源BS名)`）投递给本管道的 BP，
  再由 BP 按 target_type 分发到 `SQLOp_*` / `SOAPOp_*`；旧路由表 `^demo.Config("pipe", 源BS名)` 仅作历史兼容兜底


## 快速启动

前置条件：已安装 Docker 与 Docker Compose。

```bash
# 1. 配置 LLM（AI 推荐功能；不配则 AI 接口返回明确提示）
cp .env.example .env
# 编辑 .env：填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL（任意 OpenAI 兼容服务）

# 2. 一键启动全部服务（首次会自动构建镜像、初始化 FHIR Server 与目标表）
docker compose up -d

# 3. 访问前端
http://localhost
```

停止服务：

```bash
docker compose down
# 如需同时清除 IRIS 数据目录（含 FHIR 数据与目标表）：
docker compose down -v
```

## AI 操作边界（2026-09-14：保留限制，简化机制）

> 背景：本项目曾发生一次 AI **越界删除其它项目容器**的事故（其它 3 个项目的 7 个容器及其网络被删，
> 其中一个 IRIS 库不可恢复）。此后为"AI / 脚本的执行通道"立了边界规则。

1. **规则（最根本）**：AI 只能写/删 **本仓库**、**本项目容器**（`dataflow-*` / `iris-terminology`）、**知识库**；
   其它项目与宿主目录一律只读——只能在"枚举清单 → 用户显式确认 → 执行"之后动。
   详见 [`AGENTS.md`](AGENTS.md) 顶部与 [`.clinerules/`](.clinerules/)。
2. **CLI 守卫**：执行 docker 前 `source tools/guard/docker_guard.sh` —— 本项目之外的破坏性操作被拒（rc=77）；
   路径校验用 `python3 tools/guard/scope_guard.py check <路径>...`。
3. **文件沙箱（可选强化）**：`./tools/guard/ai-session.sh` 起的受限会话（macOS `sandbox-exec`）**写入**只允许
   仓库 / 知识库 / `/tmp` / `~/Library/Caches`，其余内核拒绝。
4. 需要全权操作其它项目：在普通终端执行（边界只约束 AI 会话与受守卫的脚本）。

细节、实测数据与已知坑见 [`tools/guard/README.md`](tools/guard/README.md)。

> ⚠ 2026-09-14：此前还上过一层「受限 Docker API 代理」（`DOCKER_HOST` → 中间代理，按 daemon 事实裁决
> 破坏性请求）——**已回滚**：过度复杂，且 `docker cp` 的流式上传体被判不了归属而 fail-closed 误拒，
> 反而打断日常操作。现在不再有代理层，docker 走本机真实 socket。


## 默认访问地址

| 服务 | 地址 | 说明 |
| ---- | ---- | ---- |
| 前端应用 | http://localhost | Vue 3 + Element Plus（六模块） |
| 后端 API | http://localhost:5001 | REST API（nginx 代理 http://localhost/api/*） |
| IRIS 管理门户 | http://localhost:52773/csp/sys/UtilHome.csp | 账号 `superuser`，密码 `SYS` |
| FHIR endpoint（实例自带 = 默认**目标**仓库） | http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/ | `/metadata` 匿名；资源读写需 Basic Auth；转换结果默认落这里 |
| FHIR endpoint（第二个仓库 = 默认**源**仓库） | http://localhost:52773/csp/healthshare/demofhir/fhir/r4/ | 独立命名空间 `DEMOFHIR`；数据源表单/`FHIRConfig.BASE_URL` 默认指向它；与上一行数据**互不可见**；自检 `python3 tools/create_fhir_repo.py --check` |
| IRIS 超级服务器 | localhost:1972 | Native SDK / DB-API 连接端口 |
| **术语服务器：术语集清单页（内置网页）** | http://localhost:52774/terminology/ | 标题「术语服务器 · 术语集」；列出 4 个术语集（中文药品 / RxNorm / 国标 ICD-10 / SNOMED US Core 样本）与各自检索端点；JSON 版 `http://localhost:52774/terminology/systems`；账号 `superuser` / 密码 `SYS` |
| 术语服务器：原生 REST（浏览器可直接看） | http://localhost:52774/terminology/… | 例 `/terminology/icd10/search?q=糖尿病`、`/terminology/drug/search?q=阿司匹林`、`/terminology/uscore-condition/zh-map?q=糖尿病`、`/terminology/vector/search?q=diabetes`；全量路由见 `termsrv/iris/src/Terminology/Production/API.cls` |
| 术语服务器：管理门户 / Production 配置 | http://localhost:52774/csp/sys/UtilHome.csp ｜ http://localhost:52774/csp/user/EnsPortal.ProductionConfig.zen?$NAMESPACE=TERMINOLOGY | 生产 = `Terminology.Production`；⚠ Ensemble 门户挂在 `/csp/user/`（`/csp/sys/` 版 404） |
| 术语服务器：上游 React 演示 UI（Terminology Explorer） | http://localhost:5173（**本环境未部署**） | 需宿主装 Node 后 `cd termsrv/ui && npm install && VITE_API_BASE_URL=http://localhost:52774 npm run dev`；上游另需 `webgateway` 容器（8080） |

> **前端与管理门户的分工**：Demo 前端负责**业务配置**（数据源/资产/AI 映射/生成管道）与**业务监控**（消息日志、目标表落库）；
> 生成的 Production 是标准 `Ens.Production`，其**技术管理**（组件配置、启停、消息详情、错误排查）请登录
> [IRIS 管理门户](http://localhost:52773/csp/sys/UtilHome.csp) → 互操作性 → 配置 Production（账号 `superuser` / 密码 `SYS`）。

## API 列表

统一响应格式：`{"code": 0, "data": ..., "message": "success"}`（`code != 0` 表示出错）。

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/api/health` | 健康检查（IRIS 连通性） |
| POST | `/api/datasources` | 注册数据源（名称/类型/端点/认证） |
| GET | `/api/datasources` | 数据源列表 |
| POST | `/api/datasources/<id>/analyze` | 自动分析 Profile（CapabilityStatement → 资源类型/资产） |
| GET | `/api/datasources/<id>/assets` | 指定数据源的资产列表 |
| GET | `/api/targets` | 目标表列表（表结构/行数） |
| GET | `/api/targets/<table>/data` | 目标表数据 |
| POST | `/api/ai/recommend` | AI 推荐（资产→目标表 + 字段映射） |
| POST | `/api/mappings` | 保存转换关系 |
| GET | `/api/mappings` | 转换关系列表 |
| POST | `/api/pipelines/generate` | 生成并启动数据管道（动态生成 Production）；**许可调度**：超许可上限的分组照旧生成但初始停用（响应 `license_budget.scheduled/suspended`） |
| POST | `/api/pipelines/run` | 触发一次转换 |
| GET | `/api/pipelines/status` | 管道运行状态 |
| GET | `/api/pipelines/items` | Production 组件清单（含类别分组、许可单元占用） |
| POST | `/api/pipelines/items/toggle` | 在线启停单个组件（切换管道占用许可） |
| GET | `/api/pipelines/instances` | **数据管道实体列表**（状态 active/suspended/superseded、生成次数、按类别分组、许可占用） |
| POST | `/api/pipelines/instances/<id>/enable` \| `/disable` | 整条管道启用/停用；**一键切换**：许可不足时自动停用其它活动管道腾单元（`disabled_others`），腾不出来才显式失败 |
| DELETE | `/api/pipelines/instances/<id>` | 删除管道实体（并让其组件让路） |
| POST | `/api/pipelines/instances/sync` | 按 Production 事实同步/校正管道实体状态 |
| GET | `/api/pipelines/logs` | 消息流转日志（Ens.MessageHeader） |
| GET | `/api/pipelines/mappings` | Production 正在执行的转换关系 |
| GET | `/api/pipelines/target-data` | 目标表落库结果 |
| GET | `/api/agents` | 已封装 AI 能力（Skills / Agents） |
| GET | `/api/agents/skills` | **Skill 目录**（管道设计 Skill + 术语判码 Skill，含适用源→目标 / 状态 / 使用次数） |

## LLM（AI 推荐）配置

AI 推荐调用 **OpenAI 兼容接口**，可填写任意兼容服务：

```ini
# .env
LLM_BASE_URL=https://api.deepseek.com/v1     # 例如 DeepSeek；默认 https://api.openai.com/v1
LLM_API_KEY=sk-xxxxxxxx                        # 服务商密钥（必填）
LLM_MODEL=deepseek-chat                        # 模型名
```

> 未配置 `LLM_API_KEY` 时，AI 推荐接口返回明确错误提示，其余功能（数据源分析 / 管道生成 / 监控）不受影响。

## 目录结构

```
.
├── docker-compose.yml        # 一键编排（IRIS + 后端 + 前端）
├── .env.example              # 环境变量模板（IRIS/FHIR/LLM）
├── init_data.py              # 建目标表（模拟远端数据库）
├── tools/seed_fhir_demo.py   # （可选）手动灌 FHIR 演示样本；不在 backend 启动链
├── tools/check_component_fidelity.py # 组件保真审计（存储定义 ⊆ Production，防重建丢件）
├── docs/PROJECT_PLAN.md      # 项目专用知识文档（目标/决策/数据模型/坑）
├── backend/                  # Flask 后端
│   ├── app.py                # 应用入口（注册全部路由蓝图）
│   ├── config.py             # IRIS/FHIR/LLM 配置
│   ├── schemas/              # Pydantic 模型
│   ├── services/             # repository(global/归一runtime) / connection_profiler(探查Agent) / mock_soap / fhir_client / profile_analyzer / llm_client / iris_connector / wsdl_importer / pipeline_validator / validate_agent / transformation_validator / type_registry
│   └── routes/               # datasources / targets / ai / mappings / pipelines
├── frontend/                 # Vue 3 + Element Plus
│   └── src/
│       ├── api/              # axios 封装 + dataflow.js（API）+ constants.js（类型枚举预留）
│       ├── views/            # Home/Datasources/Assets/Recommend/Mappings/Pipelines/Targets
│       ├── components/       # TypeSelect（类型选择器，预留禁用）
│       └── router/
├── iris/                     # IRIS 侧代码
│   ├── setup.sh              # 容器启动统一初始化（凭据/FHIR Server/编译）
│   ├── init-password.sh      # superuser/SYS 凭据
│   ├── src/demo/             # Production 组件类 + PipelineGenerator + PipelineQuery
│   └── python/               # transform_handler.py（Embedded Python 转换逻辑）
├── data/                     # IRIS 数据持久化
└── knowledge -> 知识库软链接
```

## 演示步骤（从零开始，页面选项随演示进度动态出现）

> 系统启动后处于**空白演示态**：无预置数据源/目标/资产/映射；页面（资产/目标/可查看表）只出现你已登记的内容。
> 重置环境：`bash tools/datakit/run.sh reset_ui_env.py`
> （一键回到零起点，**脚本自带 26 项自检**：末尾 ✅/❌ 清单 + 退出码 0=干净；只查不改加 `--check-only`。
> 旧脚本 `python cleanup_demo.py` 已过时：它不清 Ens 内部残留/生成类/动态发现的新表。）
> **术语映射不用管**：事实源在**术语服务器**（独立容器 + 独立数据目录，重置不影响），运行期由管道经
> **共享 BO**（`demo.TerminologyOperation`）实时查询——**没有本地缓存要预热**，重置后术语映射天然就位；
> 若某个源编码服务器尚无映射，平台**默认降级**（保留源编码 + `meta.tag=unmapped`，不静默、不阻断），
> 补录：`bash tools/datakit/run.sh term_map_build.py`（判定 Agent 产出候选并写回服务器，补录后**无需重新生成**）。

### A. FHIR → DB（数据源 = FHIR 资源）
1. **添加 FHIR 数据源**：「数据源管理」→ 端点**默认已填** `http://iris:52773/csp/healthshare/demofhir/fhir/r4/`（DemoFHIR = 默认 FHIR 源仓库）、认证 `superuser/SYS` → 注册 → **Profile 分析**（自动产出运行契约：版本/增量能力/健康）。
2. **添加 DB 目标**：「转换目标」→ JDBC（表单**默认已填** `jdbc:IRIS://iris:1972/CLINIC` = SQL 目标默认库；本教程要写**平台内置目标表** `Patient`/`Observation`（在 `USER` 命名空间），故把 URL 改为 `jdbc:IRIS://iris:1972/USER`）、认证 superuser/SYS → 添加 → 联通测试 → 选 schema `SQLUser` → 勾选目标表（`Patient`/`Observation`）→ 分析列结构 → 保存。
3. **AI 智能匹配**：选 `Patient` 资产 → AI 推荐字段映射 → 确认保存。
4. **生成管道**：「管道监控」→ 生成 → FHIR 增量同步自动抓取 FHIR Server 数据 → 转换 → `Patient` 表落库（Pipelines 目标数据下拉动态可选 `Patient` 查看）。
5. **看效果**：往 FHIR 写新资源（或用「生成模拟数据」按钮）→ 增量抓取 → 消息 Completed → 落库。

### B. SQL → SOAP（数据源 = SQL 表，目标 = 第三方 SOAP 接口，Python mock 应答）
1. **添加 SQL 数据源**：数据源向导 → JDBC 连接 → 选 schema → 选表 **`PatientSource`**（与 Patient 同结构的“业务库”演示表）→ 分析列 → 自动生成轮询 Query。
2. **添加 SOAP 目标**：转换目标 → SOAP → WSDL `/tmp/patient.wsdl`（内置**写入型 AddPatient**）→ 导入生成 BO + 实体分析（运行契约自动判定 `AddPatient → 写入型`、endpoint 可达）。
3. **AI 智能匹配**：选 `PatientSource`（SQL 资产，列即字段）→ AI 推荐 → 确认（mapping 自动带 `target_type=SOAP`）。
4. **生成管道**：SQLService 轮询 `PatientSource` → 转换 → `SOAPOp_PatientService` 调用 mock（WebServiceURL）→ mock 收到实体 → 落库 `PatientEntity` 表并回执。
5. **看效果**：往 `PatientSource` 插几行患者 → SQLService 轮询投递 → Pipelines 消息 Completed + `PatientEntity` 可见（下拉动态含 `PatientEntity`/`PatientSource`）。

### C. 多管道并存（单 Production 内 FHIR→DB 与 SQL→SOAP 同时跑）
- 前端一次确认多组转换关系后，生成 body 走 `pipelines: [组1, 组2]`；
  `TransformProcess` 按来源（`SQLService`/`FHIRService`）路由到各自目标，消息互不干扰。
- 注意：SQL 源表与 FHIR 目标表**不要用同一张**（否则 FHIR 写入会被 SQL 源再轮询产生回环，demo 已内置独立 `PatientSource` 表避免）。

### D. 管道增量生成（建议接在 C 之后演示）

1. **不改任何东西再点一次「生成」**：界面提示「所有数据管道均已存在且未变更（未重新生成）」，
   `/api/pipelines/items` 组件清单与实例"生成次数"**不变**（不重跑 AI、不重启生产）。
2. **只勾选其中一组**提交：其余管道组件**原样保留**（后端把未提交的既有管道按存储定义自动并入）；
   若提交里含**同身份重复组**，会被合并并在响应 `dup_merged` 中回报（不产生第二套实例）。
3. **需要复位某条管道**：打开页面 **「强制重新生成」** 开关后点生成（`force=true`），该组会重新设计组件（AI 重新决策）。
4. **确认没有丢件**（可选）：`python3 tools/check_component_fidelity.py`
   —— 逐项比对"每个管道的存储定义 ⊆ Production 组件"，做三次审计（基线 / 复用提交 / 强制重建）并输出缺失清单。

## 说明与限制

- 本项目为技术演示用途，示例数据均为程序生成，不涉及真实患者信息。
- IRIS 登录统一 `superuser` / `SYS`；FHIR 资源读写需 Basic Auth（仅 `/metadata` 匿名公开）。
- **FHIR 源/目标默认分工**（2026-09-16 起）：**源 = `DEMOFHIR`**（第二个独立仓库，UI「数据源管理」表单与
  `FHIRConfig.BASE_URL` 的默认值）、**目标 = `FHIRSERVER`**（实例自带，UI「转换目标」表单与
  `FHIRConfig.TARGET_BASE_URL` 的默认值）；两个仓库数据互不可见，重置脚本会同时清空两者。
- SOAP 目标演示默认指向 **Python mock**（`backend/services/mock_soap.py`，`Config.MOCK_SOAP_URL`）；
  真实接入时在目标连接信息里填真实 endpoint 即可（Adapter `WebServiceURL` 覆盖 WSDL 地址）。
- **术语判定三态语义**（运行期由共享 BO `demo.TerminologyOperation` 实时查询术语服务器）：
  `active` → 追加目标体系 coding（双 coding，如 `E11.900` + SNOMED `44054006`）；
  **`negative`（已判定无匹配，如"依折麦布/阿托伐他汀"复方制剂）→ 只保留源编码、不追加目标编码、也不打 `unmapped` 标记**；
  `missing` / 调用失败 → 保留源编码并在资源打 `meta.tag=urn:cn-nhsa:term-map|unmapped`（不阻断，可补录；
  补录后**无需重新生成**，运行期即时生效）。
- **许可与管道切换**：社区版 IRIS 许可为 **8 个业务主机单元** → 多条管道不能同时运行，超出的分组生成后为 `suspended`；
  在「数据管道」卡片点**启用/停用**做**一键切换**（许可不足时自动让路其它活动管道，响应 `disabled_others` 列出被让路组件）。
- **组件保真自查**：`python3 tools/check_component_fidelity.py`（重建/复用后逐项确认没有丢组件）；
  工具箱清单：`bash tools/datakit/run.sh list`（含 `test_incremental_pipeline.py` 等离线回归与造数脚本）。
- 目标表写入为 UPSERT（存在则更新），管道定时拉取重复执行不冲突。
- `init_data.py` 每次后端启动会**重建目标表**（DROP+CREATE，适合演示；生产环境不应自动清表）。
- FHIR 演示数据**不在启动时灌**（2026-09-16 起）：用时现造（`tools/gen_test_patient.py`、`generate_mock_data.py --fhir N`）；
  需要"库里本就有历史存量"时手动 `bash tools/datakit/run.sh seed_fhir_demo.py`。
- **演 FHIR 源不必先造数**（2026-09-18 起）：数据源的字段发现为「真实数据（优先）→ 服务器 StructureDefinition →
  **平台 FHIR 规范快照**（US Core 已建模 11 类）→ **AI 按 R4 规范补全**（其余类型）」，来源在运行契约
  `note.fields.provenance` 与 UI「运行契约」列可见；有了真实数据后下次分析会自动改用真实数据形态。
  造数仍可选（真实数据形态最准）：`bash tools/datakit/run.sh seed_fhir_demo.py`。
- **注意：不要修改 IRIS 的 Web Application / Security 权限**（管理门户与 Ensemble 门户依赖，属外部环境）。
