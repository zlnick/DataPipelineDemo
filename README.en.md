# AI-Driven Data Automation Demo

An **AI-driven data pipeline automation** demo built on **InterSystems IRIS for Health**.

Through the UI you register a data source (FHIR / SQL), the platform **analyzes the endpoint** and registers data
assets; you pick a conversion target (DB table or SOAP service); an **AI (OpenAI-compatible LLM)** recommends
"asset → target" matching and field mappings; after confirmation the platform **automatically generates an IRIS
interoperability Production** and delivers the data.

```
FHIR/SQL source → interface analysis → register assets
                              ↘                    ↙
              AI Matching (asset → target + field mappings) → user confirms
                              ↘                    ↙
                    Generate Transformation Plan → IRIS Production → deliver to target
```

> The demo ships a **bilingual UI**: Chinese (default) and English at **`/en`** (language switch in the top bar).

## Feature Highlights

### ✨ AI Decision Scope: what the LLM decides (and what code does NOT)

All **decisions & generation** run through the LLM at runtime. Code only **reads facts, parameterizes, validates and
backfills** — there are no hard-coded mapping / topology / conclusion templates:

| Capability | AI (LLM) decides | Code only does |
| --- | --- | --- |
| Interface / source analysis | Asset semantics, polling-key hints, target write/read direction, runtime-contract interpretation | Read facts (Capability/columns/WSDL), persist |
| Data mapping | Asset→target matching & field mappings (incl. `concat()` expressions) | Structure normalization, integrity checks |
| Data pipeline | Component set & order, naming (per group in multi-pipeline) | Registry fills className/settings; backfills essentials |
| Validation & fix | Judges whether issues are real + chooses fix actions | Fact-check tools, mechanical pruning, explicit fallback |

**AI red line**: an LLM failure is an *explicit* failure surfaced to the user (missing key / timeout / malformed
output, with the Agent name) — never a silent rule result. Rules/registry only do ① parameterization
② completeness validation ③ backfilling essential pieces (flagged `ai_supplemented`). Every generation carries an
auditable `ai` payload (`driven/components/supplemented/c2_rule_rebuilt`) and token usage is logged.

### ✨ AI Validation & Auto-Repair (fix loops)

- **C1 transformation validation-fix**: after mapping confirmation / before pipeline generation — fact checks + LLM
  judgment correct field mappings (target columns, paths, semantic mismatches).
- **C2 pipeline validation-fix**: after generation, automatically validates **topology / compile / start / message
  flow** (single and multi-pipeline unified).
- **Auto-repair**: fact-check tools → **LLM decides the fix** → regenerate & revalidate (≤2 rounds); on failure the
  pipeline is rebuilt once automatically. Unresolved issues are **stored as experience** (`^demo.ValidationIssue`)
  and **fed back into later AI fixes** so the same pitfall is not repeated.
- **Observability**: the Pipelines page has an **AI Audit Log** dialog showing the decision source of each
  generation; backend logs record token usage per Agent.
- **Knowledge loop**: validation experience is deduplicated and restructured by the **Knowledge Polish Agent (LLM)**
  and exported to an Obsidian vault (`export_validation_issues.py`).

### Layered models & AI transformation
The platform separates conversion into three layers instead of assuming a 1:1 "source → target" mapping:
1. **Source asset model**: FHIR resources / SQL tables with fields, types, keys and relations.
2. **Target interface model**: DB tables or SOAP WSDL operation request entities, nested fields and constraints.
3. **Transformation plan**: AI-generated plan expressing multi-table aggregation, message split, JOIN, grouping and
   field mappings; used for pipeline generation only after user confirmation.

Plans are managed through `/api/source-assets`, `/api/target-interfaces` and `/api/transformation-plans`, and can be
fact-verified via `/api/ai/verify`. Only when you click **Generate / Rebuild Pipeline** does the system call AI to
design the Production topology and hand it to IRIS to compile and start.

### AI-driven, layered (industry wording)
- **4 Skills (single-shot LLM)**: *Interface Analyzer* (asset/entity semantics, polling-key hints, write/read
  direction, runtime-contract interpretation — deterministic probes only collect facts), *Transformation Generation*,
  *Pipeline Design* (AI decides component set & order; registry only parameterizes and backfills essentials,
  marked `ai_supplemented`), *Knowledge Polish* (dedupe + restructure validation experience → Obsidian vault).
- **2 Agents (tools + LLM fix loops ≤2 rounds + experience learning)**: *Transformation Validation (C1)* and
  *Pipeline Validation & Fix (C2)* — industry-recognized agentic workflows.
- LLM failures are surfaced as explicit errors (never silently replaced by rules; rule fallback requires
  `allow_rule_fallback=true` and is flagged `rule_fallback:true`).
- Every pipeline generation returns an `ai` audit object (`driven/components/supplemented/c2_rule_rebuilt`), and the
  Pipelines page provides an **AI Audit Log** dialog. LLM calls are logged with token usage.

### Sources & targets
- **FHIR source**: register endpoint → auto analyze CapabilityStatement (profile / resource types) → discover assets;
  incremental sync via `_lastUpdated` cursor.
- **SQL source**: JDBC wizard (test → schema → tables → analyze columns) auto-generates the polling Query; the
  selected tables become assets (columns = fields).
- **DB target**: JDBC wizard discovers schema/tables/columns.
- **SOAP target**: WSDL-import based — reading the WSDL auto-generates a Business Operation (BO) + entity analysis;
  the built-in sample is a **write-type AddPatient** (flat 3 fields) served by a Python mock
  (`backend/services/mock_soap.py`, persists to `PatientEntity` and returns an acknowledgment).

### Pipelines
- One-click **Production generation**, supporting heterogeneous combos (FHIR→DB / SQL→DB / SQL→SOAP / FHIR→SOAP)
  and **multiple pipelines inside a single Production** (`POST /api/pipelines/generate` body `pipelines: [group1, group2]`).
- Conversion BP: **one BP instance per data pipeline** (an Ens business-host identity is the *item Name*, while the
  class `demo.TransformProcess` is reused — e.g. `TransformProcess__sql2soap`). Each pipeline's source BS points
  `TargetConfigNames` at **its own** BP, and the BP reads its own parameters from `^demo.Config("bp", <BP name>)`
  (mapping / target_type / service|table). Pipelines are therefore fully decoupled: each can be enabled/disabled as a
  whole and releases its license units with it. Only infrastructure is shared — `JavaGateway` (one JDBC gateway),
  always required.
- FHIR incremental: `FHIRSyncService` (cursor) → `FHIRQueue` → `FHIRService` (one-by-one, independent sessions).
- SQL polling: `EnsLib.SQL.Service.GenericService` (Query + KeyFieldName).

### Validation–fix loop & knowledge
- `check_connection` gate → C1 transformation validation → C2 pipeline validation (topology/compile/start/messages)
  → layered fix (rules → AI ≤2 rounds → default); multi-pipeline runs through C2 as well.
- Issues are stored in `^demo.ValidationIssue`; they feed C1/C2 prompts at runtime and can be read via
  `GET /api/pipelines/validation-issues`, appended via `POST`, polished by LLM via `POST /validation-issues/polish`,
  and exported to an **Obsidian vault** (04-Pitfalls) with `export_validation_issues.py`.

### Demo-friendly
- Dynamic options: asset/target/viewable-table dropdowns are driven by what you registered (API-driven, not hard-coded).
- Pipeline Monitor: live message log (Ens.MessageHeader) + per-target data viewer.
- Mock data generator: `docker exec dataflow-backend python /app/generate_mock_data.py --fhir N --obs M --sql K`
  (writes FHIR Patient/Observation linked to latest patients and SQL `PatientEntity` rows).
## Technology Stack

| Component | Stack |
| ---- | ------ |
| Database / integration engine | InterSystems IRIS for Health Community (`containers.intersystems.com/intersystems/irishealth-community:2026.1`) |
| FHIR source | Built-in IRIS FHIR Server (core R4, FHIRSERVER namespace) |
| Backend API | Flask + IRIS Native SDK + OpenAI SDK |
| Frontend | Vue 3 + Element Plus + Vite + nginx (bilingual zh/en) |
| AI | OpenAI-compatible endpoint (`base_url` / `api_key` / `model`) |
| Orchestration | Docker Compose (3 containers) |

**IRIS dual role**: FHIRSERVER namespace = FHIR Server (source); USER namespace = platform (targets / Production /
Mappings / runtime contracts).

**Demo tables (SQLUser)**: `Patient`/`Observation` (FHIR→DB landing) · `PatientSource` (SQL-source demo table) ·
`PatientEntity` (SOAP mock delivery result) · `FHIRQueue` (incremental fetch queue).

## Quick Start

Prerequisites: Docker + Docker Compose.

```bash
cp .env.example .env   # then fill LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
docker compose up -d   # first run builds images & initializes FHIR Server + target tables
http://localhost       # Chinese UI (default)   |   http://localhost/en  # English UI
```

Stop: `docker compose down` (add `-v` to also wipe the IRIS data directory).

## Default Endpoints

| Service | URL | Notes |
| ---- | ---- | ---- |
| Frontend | http://localhost | Vue3 (zh/en) |
| Backend API | http://localhost:5001 | REST (proxied at /api/*) |
| IRIS Portal | http://localhost:52773/csp/sys/UtilHome.csp | `superuser` / `SYS` |
| FHIR endpoint | http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/ | `/metadata` anonymous; data needs Basic Auth |
| Superserver | localhost:1972 | Native SDK / DB-API |

> The frontend is for business configuration & monitoring. Technical management of the generated `Ens.Production`
> (component config, start/stop, message details) happens in the IRIS Management Portal → Interoperability.
## Main API Endpoints

Unified response: `{"code": 0, "data": ..., "message": "success"}` (`code != 0` = error).

| Method | Path | Description |
| ---- | ---- | ---- |
| GET | `/api/health` | Health check (IRIS connectivity) |
| POST | `/api/datasources` | Register data source |
| POST | `/api/datasources/<id>/analyze` | Analyze endpoint → assets + AI semantics + runtime contract |
| GET | `/api/datasources/<id>/assets` | Assets of a datasource |
| POST | `/api/ai/recommend` | AI recommend (asset → target + field mappings) |
| POST | `/api/mappings` | Save transformation relations |
| POST | `/api/pipelines/generate` | Generate & start pipelines (single or multi) |
| GET | `/api/pipelines/status` · `/logs` | Status / message log |
| GET | `/api/pipelines/target-data` | Target table landing data |
| GET · POST | `/api/pipelines/validation-issues` | Read / append validation experience |
| POST | `/api/pipelines/validation-issues/polish` | LLM dedupe + restructure for knowledge export |
| GET | `/api/agents` | AI capability catalog (Skills / Agents, `kind` field) |
| GET · POST | `/api/targets` | List / add data targets (DB / SOAP) |

## LLM (AI) Configuration

```ini
# .env
LLM_BASE_URL=https://api.deepseek.com/v1   # any OpenAI-compatible service
LLM_API_KEY=sk-xxxxxxxx                     # required for AI features
LLM_MODEL=deepseek-chat                     # a fast (-flash) model is recommended for demos
```

> Without `LLM_API_KEY` the AI endpoints return explicit errors; the rest keeps working.

## Project Layout

```
.
├── docker-compose.yml            # IRIS + backend + frontend
├── .env.example                  # env template (IRIS/FHIR/LLM)
├── init_data.py / init_fhir_data.py    # init target tables / FHIR samples
├── generate_mock_data.py         # demo data (--fhir/--obs/--sql)
├── export_validation_issues.py   # validation experience → Obsidian knowledge
├── README.en.md                  # this file
├── backend/
│   ├── services/                 # repository / interface_analyzer / llm_client / connection_profiler /
│   │                             # mock_soap / validate_agent / transformation_validator / pipeline_validator /
│   │                             # type_registry / wsdl_importer / ...
│   └── routes/                   # datasources / targets / ai / mappings / pipelines
├── frontend/src/
│   ├── api/                      # axios + dataflow.js + constants.js
│   ├── views/                    # Home/Datasources/Assets/Recommend/Mappings/Pipelines/Targets/Agents
│   └── i18n/locales/             # zh.js + en.js
├── iris/
│   ├── setup.sh                  # container init
│   ├── src/demo/                 # Production components + PipelineGenerator + PipelineQuery
│   └── python/                   # transform_handler.py (Embedded Python)
└── data/                         # IRIS persistence
```
## Demo Walkthrough (start from a blank state)

### A. FHIR → DB
1. **Add FHIR source** (Data Sources): endpoint
   `http://iris:52773/csp/healthshare/fhirserver/fhir/r4/`, auth `superuser/SYS` → Register → **Analyze**
   (produces runtime contract + AI asset semantics).
2. **Add DB target** (Targets): JDBC `jdbc:IRIS://iris:1972/USER`, auth superuser/SYS → Add → Test → schema `SQLUser`
   → check `Patient`/`Observation` → Analyze columns → Save.
3. **AI Matching** (Recommend): select the `Patient` asset → AI recommends field mappings → Confirm & save.
4. **Generate pipeline** (Pipelines): FHIR incremental sync fetches new resources → converts → lands in `Patient`
   (pick the table in the target-data dropdown).
5. **See the effect**: write new resources (or use **Generate Mock Data**) → messages Completed → data lands.

### B. SQL → SOAP (SQL table source → SOAP served by the Python mock)
1. **Add SQL source**: JDBC wizard → schema → table **`PatientSource`** → analyze columns (polling Query generated).
2. **Add SOAP target**: SOAP with WSDL `/tmp/patient.wsdl` (write-type **AddPatient**) → import (BO + entities);
   the runtime contract marks `AddPatient → write`.
3. **AI Matching**: select `PatientSource` → confirm (mapping carries `target_type=SOAP`).
4. **Generate pipeline**: `SQLService` polls `PatientSource` → converts → `SOAPOp_PatientService` calls the mock →
   mock persists to `PatientEntity` and replies.
5. **See the effect**: insert rows into `PatientSource` → polling delivers → Completed + `PatientEntity` rows appear.

### C. Multiple pipelines in one Production
- Confirm several mapping groups and generate with `pipelines: [group1, group2]`; `TransformProcess` routes each
  message by its source BS to its own target.
- Do not use the same table as both a SQL source and a FHIR landing target (loop risk; a dedicated `PatientSource`
  table avoids this in the demo).

## AI Guardrails (limits kept, machinery simplified — 2026-09-14)

This repository was hit by an incident where an AI assistant deleted containers **belonging to other
projects** (7 containers + their networks; one IRIS database was unrecoverable).

1. **Rules (the core)**: an AI may write to / delete **this repo**, **this project's containers**
   (`dataflow-*` / `iris-terminology`) and **the knowledge vault** only; everything else is read-only and
   may only be touched after "enumerate → user confirms → execute". See the top of [`AGENTS.md`](AGENTS.md)
   and [`.clinerules/`](.clinerules/).
2. **CLI guard**: `source tools/guard/docker_guard.sh` before running docker — destructive operations on
   objects outside this project are rejected (rc=77); validate paths with
   `python3 tools/guard/scope_guard.py check <path>...`.
3. **File sandbox (optional hardening)**: the restricted session started by `./tools/guard/ai-session.sh`
   (macOS `sandbox-exec`) may only **write** to the repo, the knowledge vault, `/tmp` and `~/Library/Caches`.
4. **Need full power over other projects?** Use a plain terminal — the boundaries only constrain AI sessions
   and guarded scripts.

See [`tools/guard/README.md`](tools/guard/README.md) for the full design, verification runs and known pitfalls.

> ⚠ 2026-09-14: an earlier "restricted Docker API proxy" layer (`DOCKER_HOST` pointing at an in-between
> proxy that ruled on daemon facts) was **rolled back**: too complex, and it fail-closed on `docker cp`
> streaming bodies (ownership could not be resolved from the streamed tar), which broke everyday work.
> There is no proxy layer now — docker talks to the real local socket.


## Notes & Limitations

- Technical demo: all sample data is programmatically generated; no real patient data.
- IRIS login is `superuser` / `SYS`; FHIR reads/writes require Basic Auth.
- The SOAP target demo points to the Python mock by default; for a real integration fill in the real endpoint in the
  target connection (Adapter `WebServiceURL` overrides the WSDL address).
- Target writes are UPSERTs (idempotent across repeated polling).
- `init_data.py` / `init_fhir_data.py` rebuild target tables and resubmit FHIR samples on each backend start
  (fine for demos; do not auto-clear in production).
- The English UI is an interface shell shared with the Chinese UI (`zh.js` / `en.js`); dynamic content produced by
  the backend / AI (asset semantics, error messages, logs, landed data) stays in its source language.
- **Do not modify IRIS Web Applications / Security permissions** (management & Ensemble portals depend on them).
