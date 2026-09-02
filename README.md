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

- **数据源管理**：注册 FHIR 数据源（IRIS 自带 FHIR Server），自动分析 CapabilityStatement（Profile / 资源类型 / 支持的操作）。
- **数据资产**：FHIR 接口中的资源类型（如 Patient / Observation / MedicationRequest）即数据资产，由 Profile 分析自动发现并注册。
- **AI 智能匹配**：选择资产后，AI（OpenAI 兼容接口，可配 DeepSeek/通义/智谱/Kimi 等）推荐「资产 → 目标表」匹配、置信度与字段级映射，用户确认。
- **转换关系**：保存并查看已确认的转换关系（源资产 → 目标表 + 字段映射）。
- **数据管道（增量同步 + 传统模式）**：一键生成 IRIS Production 管道：`FHIRSyncService`（定时按 `_lastUpdated` 游标**增量抓取**）→ 队列表 `FHIRQueue` → `FHIRService`（**逐条处理，每条数据独立会话**）→ `TransformProcess`（字段映射）→ `SQL Operation`（`EnsLib.SQL.Operation.GenericOperation`，JDBC UPSERT 投放，按目标表动态生成 `SQLOp_<表>`）。
- **管道监控**：实时消息流转日志（Ens.MessageHeader 真实消息历史）、目标表落库结果。
- **转换目标（JDBC 分步发现）**：用户可**添加数据目标**（JDBC 连接，保障通用性）→ 联通测试 → 选择 schema → 列出表 → 勾选目标表 → **自动分析列结构**；预置「模拟远端数据库」目标（Patient / Observation 表）。
- **类型预留**：数据源（数据库/REST/SOAP）与转换目标（FHIR/REST/SOAP）为预留类型，界面渲染禁用态 + 提示，便于后续扩展。

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
- **FHIRSERVER namespace**：FHIR Server（数据源），承载 CapabilityStatement 与示例资源（10 Patient + 30 Observation）
- **USER namespace**：转换平台——目标表（模拟远端数据库）、互操作性 Production、Mapping 配置

**数据管道（Production）**：`FHIRService`（定时拉取 FHIR，CallInterval=15s）→ `TransformProcess`（Embedded Python 字段映射转换）→ `SQL Operation`（`EnsLib.SQL.Operation.GenericOperation`，JDBC 连接 `localTarget` 指向本实例 USER namespace，`INSERT OR UPDATE` UPSERT 写入目标表）。

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
│   ├── services/             # repository(global) / fhir_client / profile_analyzer / llm_client / iris_connector
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

## 演示步骤（从零开始，每步均有按钮触发）

> 系统启动后处于**空白演示态**：无预置数据源/数据目标/资产/映射，操作者现场逐步完成全流程。

1. **添加数据源**：「数据源管理」→ 表单**已预填默认值**（名称 `IRIS内置FHIR`、端点 `http://iris:52773/csp/healthshare/fhirserver/fhir/r4/`、认证 superuser/SYS）→ 直接点击「注册数据源」。
2. **触发解析数据资产**：注册后点击该行的 **「Profile 分析」** 按钮 → 自动解析 CapabilityStatement，发现 145 个数据资产（Patient / Observation 等）。
3. **添加数据目标**：「转换目标」→ 表单**已预填默认值**（名称 `模拟远端数据库`、JDBC URL `jdbc:IRIS://iris:1972/USER`、驱动类默认、认证 superuser/SYS）→ 直接点击「添加数据目标」。
4. **触发目标发现**：点击该目标的 **「联通测试」** → 通过后进入向导：选择 schema（`SQLUser`）→ 勾选目标表（`Patient` / `Observation`）→ 自动分析列结构 → 保存。
5. **AI 智能匹配**：「AI 智能匹配」→ 选择 `Patient` → 点击「AI 智能匹配」→ 查看推荐的目标表与字段映射 → 确认保存转换关系。
6. **生成数据管道**：「管道监控」→ 点击「生成 / 重建数据管道」→ IRIS Production 自动生成并启动（含 `FHIRSyncService` 增量同步抓取器 + `FHIRService` 队列表逐条处理）。
7. **增量同步演示**：初始游标在预置数据之后，目标表为空；点击 **「🎲 生成模拟数据（演示增量）」** → 平台自动往 FHIR 写入模拟数据（`lastUpdated` 晚于游标）→ 下个同步周期自动增量抓取 → 逐条独立会话转换 → 约 12 秒后自动刷新落库结果。
8. **概览**：返回「项目概览」查看整体统计。

## 说明与限制

- 本项目为技术演示用途，示例数据均为程序生成，不涉及真实患者信息。
- IRIS 登录统一 `superuser` / `SYS`；FHIR 资源读写需 Basic Auth（仅 `/metadata` 匿名公开）。
- 数据源仅实现 **FHIR**；数据库 / REST / SOAP 数据源与 FHIR / REST / SOAP 目标为**预留类型**（界面禁用态，扩展时启用 `enabled` 标记即可）。
- 目标表写入为 UPSERT（存在则更新），管道定时拉取重复执行不冲突。
- `init_data.py` / `init_fhir_data.py` 每次后端启动会重建目标表并重新提交 FHIR 示例数据，适合演示；生产环境不应自动清表。
