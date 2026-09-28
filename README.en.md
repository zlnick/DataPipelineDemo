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

### ✨ Incremental pipeline generation (pipeline-scoped)

Generation is no longer a full recompute every time — it is **incremental at the pipeline level**:

- **Stable identity**: the same `(source datasource, target)` is always **the same pipeline** — no matter how many
  times you submit, or which design Skill the LLM picks (one `PIPE_<src>_<tgt>` record, updated in place).
- **Skip when unchanged**: if the inputs (mapping content / source·target contracts) did not change, the platform
  **reuses the stored component definitions, does not re-run Agent B and does not restart the Production**
  (`unchanged=true` / `render_skipped=true`; the UI reports "all pipelines already exist and are unchanged").
- **Only changed groups are generated**: the UI submits just the new/changed groups; **existing pipelines that were
  not submitted are merged back from their stored definitions** (never wiped by a full re-render) and keep their
  runtime state (enabled flags / license / scan keys).
- **Duplicate submissions are safe**: identical `(source, target)` groups inside one payload are merged and reported
  in `dup_merged` (auditable).
- **Force rebuild**: turn on the UI switch **"Force regenerate"** (`force=true`) when you really want a redesign.

### ✨ AI capability catalog (Tools / Workflows / Skills / Agents + Skill catalog)

The **AI Agents** page classifies platform capabilities per the industry reference (Anthropic, *Building effective
agents*) and shows the reasoning for every entry:

| Class | Meaning | Items in this project |
| --- | --- | --- |
| **Tool** | Deterministic callable, **no LLM** | Connection profiler / connectivity gate / fact checks (`pipeline_validator.check_*`) / rule checks / BP static admission / terminology gap precheck / target column & key facts / WSDL entity analysis / terminology lookup BO (`demo.TerminologyOperation`) / type & FHIR model registries |
| **Workflow** | LLM involved, but the **path is predefined in code** | Interface analysis (tool-driven facts + one LLM pass); Pipeline Design Agent B (decides once → the platform renders along a fixed path) |
| **Skill** | Packaged instructions/knowledge, **single step**, no tool loop | Transformation generation (A), knowledge polish, terminology judgement (C3 / C3-Dx: single-shot judgement + lookup tool) |
| **Agent** | **Tools + multi-round autonomous loop + goal** | Transformation validation-fix (C1); Pipeline validation-fix (C2) |

The page also shows the **Skill catalog** (the controlled list used for AI decisions, `GET /api/agents/skills`):
**6 pipeline-design Skills** (`sql2fhir-patient-tx` / `sql2db` / `fhir2db` / `sql2soap` / `fhir2soap` / `fhir2fhir`,
with applies-to source→target, status and topology roles) and **2 terminology Skills** (`cn2snomed` / `cn2rx`, with
source/target systems and the judging agent) — each with a **usage counter** for the current environment.
The catalog is only used for **parameterization**; choosing which Skill to use remains an AI decision.

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

**IRIS multi-role (single instance)**:
- `FHIRSERVER` namespace = built-in FHIR Server — **default FHIR *target* repository** (conversion results land here)
  — endpoint `/csp/healthshare/fhirserver/fhir/r4/`
- `DEMOFHIR` namespace = **second, independent FHIR repository** (main DB `DEMOFHIR` + repository data DBs
  `DEMOFHIRX0001R/V`) — endpoint `/csp/healthshare/demofhir/fhir/r4/`; fully isolated from `FHIRSERVER`
  (the same resource id is invisible across repositories). It is the **default FHIR *source* repository**
  (data-source form, `FHIRConfig.BASE_URL` and the mock-data scripts all default to it).
  Created by `iris/setup.sh` step 2b (idempotent); re-runnable on a live instance via
  `python3 tools/create_fhir_repo.py` (with 15 built-in checks).
- `USER` namespace = the platform (targets / Production / Mappings / runtime contracts).

**Demo tables (SQLUser)**: `Patient`/`Observation` (FHIR→DB landing) · `PatientSource` (SQL-source demo table) ·
`PatientEntity` (SOAP mock delivery result) · `FHIRQueue` (incremental fetch queue).

## Quick Start

Prerequisites: Docker + Docker Compose (Windows: WSL2 + Docker Desktop recommended; the repo enforces **LF** — see prerequisite 7 below).

> **New-environment prerequisites (read this when cloning onto another machine)**
>
> 0. **The IRIS image comes from the InterSystems container registry (free account required).**
>    `irishealth-community` is not hosted on Docker Hub; register (free) at
>    <https://containers.intersystems.com> and log in first:
>    ```bash
>    docker login containers.intersystems.com   # username/password = the ones you registered
>    ```
>    Without login, `docker compose up` fails to pull the image (image distribution is bound by the
>    InterSystems license; this repo does not redistribute it).
>    Also allow **≈ 20 GB of disk** and some patience: the **first** build/init takes **20–40 minutes**
>    (the embedding container downloads and installs torch plus the local model on first start; later builds
>    hit the build cache).
> 1. **Submodule dependency — the terminology server is a separate project.** `termsrv` is pulled in as a
>    **git submodule** ([`zlnick/iris-terminology-server`](https://github.com/zlnick/iris-terminology-server),
>    branch `demo-community`), and the `iris-terminology` container **is built from it**:
>    - Clone with submodules: `git clone --recurse-submodules https://github.com/zlnick/DataPipelineDemo.git`
>    - Already cloned without it? `git submodule update --init --recursive`
>      ⚠ If it fails with Unable to find current revision in submodule path termsrv, the submodule repo
>      (zlnick/iris-terminology-server, branch demo-community) is either not public yet, or that branch has not
>      received the commit recorded here. Self-check: git ls-remote <submodule-url> refs/heads/demo-community
>      (without the submodule the `iris-terminology` build fails; the main platform still runs, but
>      terminology degrades to "keep source coding + `meta.tag=urn:cn-nhsa:term-map|unmapped`",
>      and tools such as `term_map_build.py` are unavailable)
>    - **How it is built**: the compose service uses `build: context: ./termsrv` (`termsrv/iris/Dockerfile`),
>      so `docker compose up -d` **builds it automatically**; to build/rebuild it alone:
>      `docker compose build iris-terminology && docker compose up -d iris-terminology`
>    - **The terminology server keeps its data directory at `./data/iris-terminology`, but the mappings
>      (`/mapping/*`) actually live in a DB *inside* the container**: rebuilding the container **loses them** —
>      re-import with `bash tools/term_map_import.sh` (same as `python3 tools/term_map_sync.py import`;
>      `setup.sh` already does this, idempotently). `tools/term_map_seed.py` is the seed **generator**, not an
>      importer. To only refresh the platform extension classes without a rebuild, hot-load with
>      `bash tools/termsrv_load.sh` (no data loss). Terminology material under `data/terms-inbox/`
>      (GB/T 14396-2016 ICD-10 ~20k entries + NRDL/CBIH Chinese drug catalogues) **is shipped in this repo**
>      and loaded into the terminology server concept tables (`Terminology_Icd10.Concept` /
>      `Terminology_Drug.Code`) by `bash tools/term_data_load.sh` — idempotent, and invoked by `tools/setup.sh`.
>      **The CLINIC demo-data generator depends on it** (diagnosis/drug Chinese names come from the terminology
>      store); re-run it after a container rebuild. Provenance and terms: see `NOTICE`.
>      The CLINIC demo source tables (Patient/Encounter/Diagnosis/MedicationOrder) are created
>      **idempotently** by `bash tools/clinic_init.sh` (create-only-if-missing, never drops data;
>      `setup.sh` runs it, and the backend also re-checks on startup) — without them the CLINIC SQL-source
>      service errors out and the "generate demo data" button fails.
>      Also: a ready-made **mapping seed** (`data/seeds/term_map_seed.json`, 81 mappings) is imported
>      automatically by `tools/setup.sh` (`tools/term_map_sync.py import`); the terminology server platform
>      extensions (`/mapping/*` routes + `CodeMap`) are overlaid from `termsrv-patches/` (idempotent), so
>      terminology conversion works right after cloning regardless of the submodule remote state.

> 2. **JDBC driver jar — one command, no download.** The backend's "test connection / pick schema & tables /
>    analyze columns / DB metadata discovery" uses **JayDeBeApi + JPype** and needs `intersystems-jdbc-*.jar`
>    (a proprietary InterSystems artifact, **not committed to this repo**). The **official IRIS image already
>    ships it**, so a one-shot extraction script is provided:
>    ```bash
>    docker compose up -d iris        # start IRIS first (the driver is inside the image)
>    bash tools/fetch_jdbc_jar.sh     # extract it into ./jdbc/ (no download; same version as IRIS)
>    docker compose up -d             # then start the rest
>    ```
>    (If everything is already running: extract, then `docker compose restart backend`; ⚠ that restart re-runs
>    `init_data.py` and rebuilds the target tables — **don't do it casually on an environment that already holds
>    demo data**. To use a **custom driver**, simply drop the jar into `./jdbc/`.)
> 3. **`.env`**: `cp .env.example .env` and fill `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`
>    (without it AI features fail **explicitly** rather than silently degrading; the platform still starts).
> 4. `data/embedding-model` (local embedding model) needs **no manual step**: the embedding container
>    **downloads it automatically from ModelScope** on first start (`Qwen/Qwen3-Embedding-0.6B`, needs network).
> 5. **On a flaky network, just re-run** `bash tools/setup.sh` — it is **idempotent** (submodule / `.env` /
>    data dirs / build / seed import all skip what is already done); if the submodule fetch was interrupted,
>    re-running recovers it.
> 6. **Terminology vectorization (optional, off by default).** `setup.sh` **needs no vectors** (conversion only
>    uses the prepared mappings; the `embedding` container is **skipped by default**, saving the ~1.1 GB
>    first-run model download). To try vectorization / semantic search / AI mapping back-fill yourself, see the
>    standalone section **[Terminology Vectorization (optional, standalone test)](#terminology-vectorization-optional-standalone-test)**.
> 7. **Cross-platform line endings (LF everywhere)**: `.gitattributes` at the repo root enforces
>    `* text=auto eol=lf` (explicit `eol=lf` for `*.sh/*.bash/*.cls/Dockerfile` and `*.csv/*.tsv`;
>    `*.jar/*.zip/images/fonts/xlsx/pdf` are treated as binary). Windows users (Git for Windows defaults to
>    `core.autocrlf=true`) can end up with CRLF checkouts, which break things such as:
>    - scripts inside containers: `sh: 1: /shared/setup.sh: not found` (IRIS),
>      `exec /app/entrypoint.sh: no such file or directory` (embedding)
>    - host bash: `set: pipefail: invalid option name`
>    Fix: `git config --global core.autocrlf false` and **re-clone** (or `git checkout -- .` so `.gitattributes` applies).

```bash
bash tools/setup.sh        # ONE command: submodule + .env + data dirs + build & up + JDBC extract + CLINIC source-table init + terminology concept load + mapping-seed import + health check
docker compose up -d   # first run builds images & initializes FHIR Server + target tables
http://localhost       # Chinese UI (default)   |   http://localhost/en  # English UI
```

Stop: `docker compose down` (add `-v` to also wipe the IRIS data directory).

## Demo Environment Initialization (what `bash tools/setup.sh` does)

> Goal: turn a **fresh clone** into a **runnable demo with one command**. No vectorization is done by default
> (the demo does not need it, and initialization stays fast).

| Step | Action | Result / notes |
|---|---|---|
| 1 | Fetch the `termsrv` submodule + apply the `termsrv-patches/` overlay | Build source for the terminology server; the overlay is idempotent (`applied=0` = already current) |
| 2 | Prepare `.env` (copied from `.env.example` if missing) | **AI features need `LLM_*`**; without it AI endpoints fail explicitly while the platform still runs |
| 3 | Create `data/` subdirs | `data/iris`, `data/iris-terminology`, `data/terms-inbox`, `data/embedding-model` |
| 4 | Build & start containers | **4 by default**: `iris`, `backend`, `frontend`, `iris-terminology` (add `--with-embedding` to include embedding) |
| 5 | Extract the JDBC driver | One command copies `intersystems-jdbc-*.jar` out of the IRIS image into `./jdbc/` (no downloads) |
| 6 | **Create CLINIC source tables** | `Patient/Encounter/Diagnosis/MedicationOrder` — **idempotent** (create-only-if-missing, never drops data); the first install waits for IRIS to become ready (~1–2 min) |
| 7 | **Load terminology concepts** | ICD-10 **20,484** + drugs NRDL **3,919** / CBIH **19** → `Terminology_Icd10.Concept` / `Terminology_Drug.Code` |
| 8 | **Load the mapping seed** | `data/seeds/term_map_seed.json` **81 entries** → the terminology server's mapping table (source of truth) |
| 9 | Wait for readiness + health check | Bounded wait for backend 200 → prints `backend / frontend / terminology` |

**State after initialization (fresh environment)**: 4 containers up; `http://localhost` shows a **blank demo state**
(no data sources / targets / mappings / pipelines); the terminology server already holds **concepts + mappings**;
`/api/pipelines/status` reports `running:false` (no pipeline generated yet).

**Options**

| Command | Effect |
|---|---|
| `bash tools/setup.sh` | Full flow (**skips embedding by default**) |
| `bash tools/setup.sh --with-embedding` | Also build/start embedding (only needed for vectorization experiments) |
| `bash tools/setup.sh --check` | **Read-only check**: submodule / `.env` / JDBC / docker |
| `bash tools/setup.sh --help` | Usage |

**⚠ After a container rebuild you must reload**: terminology **concepts** and **mappings** live in a DB **inside**
the `iris-terminology` container, so a rebuild loses them → re-run `bash tools/term_data_load.sh` (concepts) +
`bash tools/term_map_import.sh` (mappings), or simply re-run `bash tools/setup.sh` (idempotent; both steps included).
The CLINIC tables are re-checked automatically when the backend starts (create-only-if-missing).

## Terminology Server (`iris-terminology`) — what it does, how the platform uses it

**Role**: an independent container (a trimmed fork of `iris-terminology-server`; host ports **52774**→52773,
**51774**→1972; credentials `superuser / SYS`). It does two things: ① **terminology storage + search/validation**
(CodeSystem / ValueSet); ② **source of truth for conversion mappings** (`/mapping/*`). The actual **mapping
decisions** are made by the platform's AI skills (C3 for drugs, C3-Dx for diagnoses) — not here.

**Web entry points**: `http://localhost:52774/terminology/` (catalog page: concepts / vectors / endpoints per code system)
and `http://localhost:52774/csp/sys/UtilHome.csp` (management portal).

**Built-in code systems** (`GET /terminology/systems`)

| id | Content | Used by the platform for |
|---|---|---|
| `chinese-icd10` | GB/T 14396-2016 ICD-10 (Chinese) | Chinese **diagnosis names** in CLINIC seeding |
| `chinese-drugs` | Chinese drug catalogue (NRDL + CBIH) | Chinese **drug names** in CLINIC seeding |
| `rxnorm` | RxNorm (IN/SCD/BN) | **target** system for drug mappings |
| `snomed-uscore` | SNOMED CT (US Core Condition sample) | **target** system for diagnosis mappings |

**REST capabilities (selection)**

| Area | Endpoints |
|---|---|
| Search / validate | `/terminology/icd10/{search,lookup,validate-code}`, `/terminology/icd/{…}`, `/terminology/drug/{search,lookup,validate-code,codesystems}`, `/terminology/rxnorm/{search,lookup,validate-code,chinese-map}`, `/terminology/snomed/*`, `/terminology/loinc/*`, `/terminology/uscore-condition/*` |
| **Mappings** (used at runtime) | `GET /terminology/mapping/lookup?sourceSystem=&targetSystem=&sourceCode=`, `GET /terminology/mapping/systems`, `POST /terminology/mapping/entries` (idempotent bulk upsert) |
| Vectors | `GET /terminology/vector/search?q=&systemUri=&limit=`, `/terminology/vector/crosswalk` (next section) |
| FHIR terminology | `/terminology/fhir/r4` (`CodeSystem/$lookup`, `$validate-code`, `$subsumes`, `ValueSet/$expand`) |

**Where the platform uses it**

1. **Runtime terminology conversion**: the shared BO `demo.TerminologyOperation` queries `/mapping/lookup` **live** —
   there is **no local cache to warm up**. If a mapping is missing the platform **degrades explicitly** (keeps the
   source code + `meta.tag=…|unmapped`): never silent, never blocking — back-fill below.
2. **CLINIC seeding** (the UI "generate demo data" button): reads the **concept tables** for Chinese names.
3. **AI code mapping (C3 / C3-Dx)**: at generation / back-fill time: **vector recall Top-K** → LLM verdict → write back.

**Data (what is automatic vs. what you generate yourself)**

| Table | Content | Loaded by |
|---|---|---|
| `Terminology_Icd10.Concept` / `Terminology_Drug.Code` | terminology concepts | **`setup.sh` (automatic)** via `tools/term_data_load.sh` |
| `Terminology_Mapping.CodeMap` | conversion mappings (source of truth) | **`setup.sh` (automatic)** via `tools/term_map_import.sh` (81-entry seed) |
| `Terminology_Vector.TermEmbedding` | term vectors | **generate yourself** (next section; not needed by the demo) |

**Operations cheat-sheet**

| Command | Effect |
|---|---|
| `bash tools/term_data_load.sh` | idempotent concept load (the material ships with the repo) |
| `bash tools/term_map_import.sh` | idempotent mapping-seed load (81 entries) |
| `bash tools/termsrv_vector_init.sh [--check]` | vector-capability status (auto-creates the table if missing) + steps |
| `bash tools/termsrv_apply_patches.sh` | overlay the platform extensions (`/mapping/*` + `CodeMap`) into the submodule (idempotent) |
| `bash tools/termsrv_load.sh` | hot-load the extension classes into the running container (no rebuild) |
| `python3 tools/term_map_build.py [--dry] [--limit N]` | AI back-fill of missing mappings (vector recall → LLM verdict → write back) |

## Terminology Vectorization (optional, **standalone test**; the demo does not need it)

**What it is for**: vectorization serves the **production of terminology mappings**, not the demo runtime.
Chinese diagnosis/drug names have **no direct code mapping** to English SNOMED/RxNorm, so the AI code-mapping
skills (C3 / C3-Dx) first need a **semantic recall of Top-5~6 candidates**, which the LLM then judges → written
back to the terminology server. This repo ships the **prepared mappings (81 entries)**, so the demo works
out of the box **without** running any vectorization.

**Prerequisites (all three are ready)**

| Requirement | Note |
|---|---|
| Terminology server **vector table** | `Terminology_Vector.TermEmbedding` — **exists out of the box** (columns `ID/Code/Embedding/Lang/Model/ReleaseId/SystemUri/Text`), **0 rows** by default; if it is missing, `termsrv_vector_init.sh` compiles the class to create it |
| Local **embedding service** | `docker compose up -d embedding` (Qwen3-Embedding-0.6B, ~1.1 GB model auto-downloaded on first start); the terminology server reaches it via `^Config("Vector","EmbeddingHost")` (default `embedding:8000`) |
| **Concept data** (the input) | Chinese ICD-10 / drug concepts, loaded automatically by `setup.sh` ✓ |

**Standalone test procedure (verified in this project)**

```bash
# 1) start the local embedding service (first run downloads the model; watch with
#    docker logs -f dataflow-embedding)
docker compose up -d embedding

# 2) inspect the vector capability: table / row count / per-system counts
#    (auto-creates the table if missing)
bash tools/termsrv_vector_init.sh

# 3) generate vectors (pick one)
python3 tools/dx_vectorize.py --zh                  # full Chinese ICD-10 (~20k rows; ~10-20 rows/s)
python3 tools/term_embed.py --system <uri> --tsv <file.tsv>   # any code system from a TSV
bash run_rxnorm_vec.sh                              # full RxNorm (26k rows; needs YOUR OWN RxNorm data;
                                                    # built-in OOM/overheat self-healing + resume)

# 4) semantic search check (returns candidates with scores)
curl -u superuser:SYS 'http://localhost:52774/terminology/vector/search?q=阿司匹林&limit=3'

# 5) full chain: vector recall -> LLM verdict -> write back the mapping (needs LLM_* in .env)
python3 tools/term_map_build.py --dry        # list missing mappings (no LLM calls, no writes)
python3 tools/term_map_build.py --limit 5    # back-fill 5 entries
```

**Measured reference (2026-09-27)**: embedding dimension **1024**; after inserting 2 rows,
`vector/search?q=阿司匹林` returned `阿司匹林 score 1.0024` and `复方硼砂 score 0.5708` ✓

**Notes**

- **Data size**: full ICD-10 ≈ **19k vectors**, RxNorm SCD/SBD/IN ≈ **30k vectors** (**200 MB+** in total) ⇒
  **not suitable for the repo** — generate them on demand.
- **Resources**: embedding + IRIS together can OOM / overheat the host → cap the CPU (~8 cores) and run in batches
  (`run_rxnorm_vec.sh` already does resume + self-healing).
- **You can stop it anytime**: `docker compose stop embedding` — the platform and the demo are **unaffected**
  (nothing depends on it).
- The demo only reads the **prepared mappings**: `data/seeds/term_map_seed.json` (imported by `setup.sh`).

## Default Endpoints

| Service | URL | Notes |
| ---- | ---- | ---- |
| Frontend | http://localhost | Vue3 (zh/en) |
| Backend API | http://localhost:5001 | REST (proxied at /api/*) |
| IRIS Portal | http://localhost:52773/csp/sys/UtilHome.csp | `superuser` / `SYS` |
| FHIR endpoint (built-in = default **target**) | http://localhost:52773/csp/healthshare/fhirserver/fhir/r4/ | `/metadata` anonymous; data needs Basic Auth; conversion results land here |
| FHIR endpoint (second repository = default **source**) | http://localhost:52773/csp/healthshare/demofhir/fhir/r4/ | Namespace `DEMOFHIR`; the data-source form / `FHIRConfig.BASE_URL` default to it; data invisible to the repository above; check via `python3 tools/create_fhir_repo.py --check` |
| Superserver | localhost:1972 | Native SDK / DB-API |
| **Terminology server: terminology sets page (built-in web page)** | http://localhost:52774/terminology/ | Title “术语服务器 · 术语集”; lists 4 terminology sets (Chinese drugs / RxNorm / national ICD-10 / SNOMED US Core sample) with their endpoints; JSON: `http://localhost:52774/terminology/systems`; `superuser` / `SYS` |
| Terminology server: native REST (browsable) | http://localhost:52774/terminology/… | e.g. `/terminology/icd10/search?q=糖尿病`, `/terminology/drug/search?q=阿司匹林`, `/terminology/uscore-condition/zh-map?q=糖尿病`, `/terminology/vector/search?q=diabetes`; full route map in `termsrv/iris/src/Terminology/Production/API.cls` |
| Terminology server: portal / Production config | http://localhost:52774/csp/sys/UtilHome.csp ｜ http://localhost:52774/csp/user/EnsPortal.ProductionConfig.zen?$NAMESPACE=TERMINOLOGY | Production = `Terminology.Production`; ⚠ the Ensemble portal lives under `/csp/user/` (the `/csp/sys/` variant returns 404) |
| Terminology server: upstream React demo UI (Terminology Explorer) | http://localhost:5173 (**not deployed here**) | Requires Node on the host: `cd termsrv/ui && npm install && VITE_API_BASE_URL=http://localhost:52774 npm run dev`; upstream also ships a `webgateway` container (8080) |

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
| GET | `/api/agents` | Wrapped AI capabilities (Skills / Agents) |
| GET | `/api/agents/skills` | **Skill catalog** (pipeline-design + terminology Skills, with applies-to / status / usage count) |
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
├── init_data.py                  # init target tables (DROP+CREATE, idempotent)
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
├── tools/                        # datakit toolbox (`run.sh list`) + check_component_fidelity.py
└── data/                         # IRIS persistence
```
## Demo Walkthrough (start from a blank state)

> **CLINIC demo data** (the UI "generate demo data" button / `tools/seed_clinic.py`): you must **register a SQL
> data source first** (the demo default is the `CLINIC` namespace — see §B step 1). Without one the script now
> prints a clear hint instead of failing with an `IndexError`.

**Shortest end-to-end path** (~10 minutes): ① register the SQL source (demo default = the `CLINIC` namespace) →
② click **"generate demo data"** → ③ register the DB target (`jdbc:IRIS://iris:1972/USER`, tables
`Patient`/`Observation`) → ④ confirm field mappings via **"AI matching"** → ⑤ **generate the pipeline** in
"Pipeline Monitor" → ⑥ inspect **landed rows / Completed messages** in the target-data dropdown.

### A. FHIR → DB
1. **Add FHIR source** (Data Sources): the endpoint is **pre-filled** with
   `http://iris:52773/csp/healthshare/demofhir/fhir/r4/` (DemoFHIR = default FHIR source repository),
   auth `superuser/SYS` → Register → **Analyze** (produces runtime contract + AI asset semantics).
2. **Add DB target** (Targets): the JDBC field is **pre-filled** with `jdbc:IRIS://iris:1972/CLINIC`
   (default SQL target namespace); this walkthrough writes the platform's built-in target tables
   `Patient`/`Observation`, which live in the `USER` namespace, so change the URL to
   `jdbc:IRIS://iris:1972/USER`, auth superuser/SYS → Add → Test → schema `SQLUser`
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

### D. Incremental pipeline generation (nice right after C)

1. **Click Generate again without changing anything**: the UI reports "all pipelines already exist and are unchanged";
   the component list (`/api/pipelines/items`) and each instance's generation count stay the same
   (no LLM re-run, no Production restart).
2. **Submit only one group**: the other pipelines keep their components (unsubmitted existing pipelines are merged
   back from their stored definitions); identical groups inside one payload are merged and reported in `dup_merged`.
3. **Reset one pipeline**: switch on **"Force regenerate"** and generate again (`force=true`) — that group's
   components are re-designed by AI.
4. **Verify nothing was lost** (optional): `python3 tools/check_component_fidelity.py` audits
   "stored definition ⊆ Production components" three times (baseline / reuse submit / force rebuild).

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
- `init_data.py` rebuilds the target tables on each backend start (DROP+CREATE; fine for demos, do not auto-clear in
  production).
- FHIR demo data is **not** seeded at startup (since 2026-09-16): create it on demand
  (`tools/gen_test_patient.py`, `generate_mock_data.py --fhir N`), or run
  `bash tools/datakit/run.sh seed_fhir_demo.py` when you explicitly need pre-existing history.
- **Analyzing a FHIR source no longer requires data first** (since 2026-09-18): field discovery goes
  real sample data (preferred) → server StructureDefinition → **platform FHIR spec snapshot** (11 modeled
  US Core resources) → **AI completion per FHIR R4 spec** (other types). Provenance is visible in
  `runtime.note.fields.provenance` and in the UI "Runtime Contract" column; once real data exists, the next
  analyze switches back to the real data shape. Seeding stays optional:
  `bash tools/datakit/run.sh seed_fhir_demo.py`.
- The English UI is an interface shell shared with the Chinese UI (`zh.js` / `en.js`); dynamic content produced by
  the backend / AI (asset semantics, error messages, logs, landed data) stays in its source language.
- **Do not modify IRIS Web Applications / Security permissions** (management & Ensemble portals depend on them).
- **Terminology judgement has three outcomes** (queried live from the terminology server through the shared BO
  `demo.TerminologyOperation`): `active` → the target-system coding is appended (dual coding, e.g. `E11.900` +
  SNOMED `44054006`); **`negative` (judged "no equivalent", e.g. a compound drug) → only the source coding is kept,
  no target coding and no `unmapped` tag**; `missing` / call failure → source coding kept plus
  `meta.tag=urn:cn-nhsa:term-map|unmapped` (non-blocking; after curation **no regeneration is needed**).
- **License & pipeline switching**: the community-edition IRIS license allows **8 business-host units**, so several
  pipelines cannot run at the same time — extra groups are generated as `suspended`; use the **enable / disable**
  buttons on the "Pipelines" card for one-click switching (units are freed by disabling other active pipelines,
  listed in `disabled_others`).
- **Component fidelity self-check**: `python3 tools/check_component_fidelity.py`; toolbox index:
  `bash tools/datakit/run.sh list` (includes `test_incremental_pipeline.py` and data-generation scripts).

## License

Apache-2.0 - see `LICENSE`; `NOTICE` lists trademarks and the components that are **not** distributed (JDBC driver, raw terminology data, termsrv submodule).
