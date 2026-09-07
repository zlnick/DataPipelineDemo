# AI 数据自动化转换 Demo

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
  - 路由 BP `TransformProcess` 按**消息来源**（源 BS）查路由表 `^demo.Config("pipe", 源名)` 分发到对应目标（SOAPOp / SQLOp）
  - FHIR 增量：`FHIRSyncService`（`_lastUpdated` 游标）→ `FHIRQueue` → `FHIRService`（逐条独立会话）→ 转换 → 投放
  - SQL 轮询：`EnsLib.SQL.Service.GenericService`（Query/KeyFieldName 增量）→ 行 JSON → 转换 → 投放
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

**IRIS 双角色（单实例）**：
- **FHIRSERVER namespace**：FHIR Server（数据源），承载 CapabilityStatement 与示例资源（Patient / Observation 等）
- **USER namespace**：转换平台——目标表（模拟远端库/落库）、互操作性 Production、Mapping 与运行契约配置

**演示数据表（SQLUser schema，结构与语义）**：
| 表 | 语义 | 数据来源 |
| ---- | ---- | ---- |
| `Patient` / `Observation` | FHIR→DB 目标落库（FHIR 资源转换写入） | FHIR 管道 / 亦可用于 SQL 源（向导自行选择即可） |
| `PatientSource` | SQL 源演示表（与 Patient 同结构，模拟“第三方业务库”） | 手工/脚本插入，供 SQL→SOAP 管道轮询 |
| `PatientEntity` | SOAP 投递结果（Python mock 收到 AddPatient 实体后落库） | mock 写入 |
| `FHIRQueue` | FHIR 增量抓取队列表（FHIRSyncService 入队，FHIRService 消费） | FHIRSyncService |

**数据管道（Production）**：`TransformProcess`（路由 BP，Embedded Python 字段映射转换，支持 `concat()` 表达式与 `表.列` 前缀）：
- FHIR 源：`FHIRSyncService`（`_lastUpdated` 增量游标）→ `FHIRQueue` → `FHIRService`（逐条独立会话）→ `TransformProcess`
- SQL 源：`EnsLib.SQL.Service.GenericService`（JDBC 轮询，Query + KeyFieldName）→ 行 JSON → `TransformProcess`
- 目标：`SQLOp_<表>`（JDBC `localTarget` UPSERT）；`SOAPOp_<服务>`（WSDL 导入 BO + Adapter WebServiceURL 指向远端/mock）
- 路由：`TransformProcess` 用消息来源（`SourceConfigName`）查 `^demo.Config("pipe", 源BS名)` 分发（单 Production 多套并存）


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

## 默认访问地址

| 服务 | 地址 | 说明 |
| ---- | ---- | ---- |
| 前端应用 | http://localhost | Vue 3 + Element Plus（六模块） |
| 后端 API | http://localhost:5001 | REST API（nginx 代理 http://localhost/api/*） |
| IRIS 管理门户 | http://localhost:52773/csp/sys/UtilHome.csp | 账号 `superuser`，密码 `SYS` |
| FHIR endpoint | http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/ | `/metadata` 匿名；资源读写需 Basic Auth |
| IRIS 超级服务器 | localhost:1972 | Native SDK / DB-API 连接端口 |

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
| POST | `/api/pipelines/generate` | 生成并启动数据管道（动态生成 Production） |
| POST | `/api/pipelines/run` | 触发一次转换 |
| GET | `/api/pipelines/status` | 管道运行状态 |
| GET | `/api/pipelines/logs` | 消息流转日志（Ens.MessageHeader） |
| GET | `/api/pipelines/mappings` | Production 正在执行的转换关系 |
| GET | `/api/pipelines/target-data` | 目标表落库结果 |

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
├── init_fhir_data.py         # 加载 FHIR 示例资源（10 Patient + 30 Observation）
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
> 重置环境：`python cleanup_demo.py`（清配置/Production/表数据/消息历史）。

### A. FHIR → DB（数据源 = FHIR 资源）
1. **添加 FHIR 数据源**：「数据源管理」→ 端点 `http://iris:52773/csp/healthshare/fhirserver/fhir/r4/`、认证 `superuser/SYS` → 注册 → **Profile 分析**（自动产出运行契约：版本/增量能力/健康）。
2. **添加 DB 目标**：「转换目标」→ JDBC `jdbc:IRIS://iris:1972/USER`、认证 superuser/SYS → 添加 → 联通测试 → 选 schema `SQLUser` → 勾选目标表（`Patient`/`Observation`）→ 分析列结构 → 保存。
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

## 说明与限制

- 本项目为技术演示用途，示例数据均为程序生成，不涉及真实患者信息。
- IRIS 登录统一 `superuser` / `SYS`；FHIR 资源读写需 Basic Auth（仅 `/metadata` 匿名公开）。
- SOAP 目标演示默认指向 **Python mock**（`backend/services/mock_soap.py`，`Config.MOCK_SOAP_URL`）；
  真实接入时在目标连接信息里填真实 endpoint 即可（Adapter `WebServiceURL` 覆盖 WSDL 地址）。
- 目标表写入为 UPSERT（存在则更新），管道定时拉取重复执行不冲突。
- `init_data.py` / `init_fhir_data.py` 每次后端启动会重建目标表并重新提交 FHIR 示例数据，适合演示；生产环境不应自动清表。
- **注意：不要修改 IRIS 的 Web Application / Security 权限**（管理门户与 Ensemble 门户依赖，属外部环境）。
