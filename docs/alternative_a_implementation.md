# Vigía · Alternative A Implementation Documentation

This document describes the implementation of **Alternative A (Tool-Calling Investigator)** specified in [docs/README.md](README.md), packaged under standard Python `src/vigia/` layout with Hexagonal Architecture, English naming, and Pydantic v2 domain contracts.

## 1. Repository & Package Layout

The business domain (rules, entities, value objects, ports) and application layer (use cases, approval flows, citation registries) are strictly separated from infrastructure adapters (Git, files, CLI, Draw.io XML, LangChain):

```text
pyproject.toml                    # Package configuration, vigia entrypoint, uv & pytest support
uv.lock                           # Reproducible lockfile managed by uv
README.md                         # Clean-clone install, test, build, and CLI execution guide
docs/
  README.md                       # Alternatives A, B, C design specification
  alternative_a_implementation.md # Alternative A implementation & verification reference
  Consola Vigía.html              # Interactive UI console mockup
  Pitch Vigía.html                # Project pitch & scenario narrative
  Vigía Documento Técnico.html    # Technical MVP contract & architecture specification
  vigia_mvp_arquitectura.drawio   # Architecture diagram source
src/
  vigia/
    __init__.py
    runtime.py                    # Composition root wiring concrete adapters with application & domain
    domain/                       # Core domain entities, value objects, enums, ports
      contracts.py                # Alert, Release, Evidence, Finding, ActionProposal, CaseRecord, etc.
      ports.py                    # Typed Protocols for external sources and sinks
    application/                  # Use cases, approval logic, investigator strategy
      case_service.py             # Case lifecycle orchestration (start, chat turns, queries, verify)
      approval_service.py         # Authoritative risk table, atomic reservation & strict idempotency
      cohort_comparison.py        # Code-calculated pre/post cohort error rate comparisons
      conversation_service.py     # Turn classification, fast dossier answers & follow-up orchestration
      evidence_registry.py        # Per-case EV-xxx evidence registry & citation checks
      tools.py                    # Pydantic-validated tools (incl. read-only follow-up & cohort rates)
      prompts.py                  # System prompts for initial investigation & read-only follow-up
      investigator.py             # Alternative A agent via langchain.agents.create_agent (+ run_follow_up)
    adapters/                     # Technology-specific implementations of ports
      inbound/
        http_api.py               # VigiaHttpApp (webhooks, console API, SSE, embedded HTML UI)
      outbound/
        service_catalog.py        # YamlServiceCatalogAdapter (aliases, onboarding metadata & criteria)
        local_git.py              # LocalGitRepositoryAdapter & JsonReleaseSourceAdapter
        github.py                 # GitHubReleaseSourceAdapter & GitHubRepositoryAdapter
        drawio.py                 # DrawioArchitectureAdapter (Draw.io XML + file/configured revisions)
        telemetry.py              # CsvTelemetryAdapter (metrics, comparative windows, cohort rates)
        http_telemetry.py         # PrometheusLokiTelemetryAdapter (PromQL + LogQL over HTTP)
        knowledge.py              # LocalKnowledgeAdapter (cutoff-bounded TF-IDF incidents & runbooks)
        executor.py               # SimulatedActionExecutor (allowed actions, async dispatch & timeouts)
        case_store.py             # SqliteCaseStoreAdapter (WAL cases, dedup, revisions, SSE, work_queue)
        audit.py                  # JsonlAuditSinkAdapter (audit.jsonl & timeline events)
    entrypoints/
      cli.py                      # Interactive, offline, onboarding probe & HTTP server CLI (`vigia`)
    demo/                         # Packaged synthetic demo scenario resources
      __init__.py                 # copy_demo_scenario(...) via importlib.resources
      services.yaml
      metrics.csv
      logs.txt
      incidents.json
      docs/transfers.drawio
      runbooks/*.md
tests/
  conftest.py                     # Isolated Transfers -> pagos-core workspace & 3-commit Git repo
  test_adapters.py                # Adapter boundaries, cutoff enforcement, Draw.io revision checks
  test_investigation.py           # End-to-end Alternative A loop, SHA linkage, proposal lock, citations
  test_decisions.py               # Approval, rejection, replay conflicts, low-risk state, verification
  test_packaging.py               # Wheel build + out-of-checkout `vigia --demo-offline` verification
  test_pilot.py                   # Pilot Deliveries A–D acceptance tests (onboarding, SQLite, shadow, SSE, RBAC, async reconciliation)
  test_conversational.py          # Conversational follow-up, cohort verdict, concurrency race & backfill tests
```

## 2. Implemented Components & Safety Invariants

### 2.1 Domain Layer (`src/vigia/domain/`)
- **`contracts.py`**:
  - `ConnectorStatus`: Distinguishes `ok`, `no_results`, `connector_unavailable`, and `unauthorized`.
  - `Alert`: Bounded by `effective_cutoff` to prevent reading future or post-rollback data during investigation.
  - `Evidence`: Stores verifiable, citable observations (`EV-001`, `EV-002`, ...) with source and reproducible references.
  - `Finding`: Encapsulates hypotheses, supporting citations, contradicting citations, connector gaps, exposed services, and inferred claims.
  - `ActionProposal`, `HumanDecision`, `ExecutionResult`, `VerificationResult`, `CaseEvent`, `AuditRecord`, and `CaseRecord`.
- **`ports.py`**:
  - Typed `Protocol` interfaces: `ServiceCatalogPort`, `ReleaseSourcePort`, `CodeRepositoryPort`, `ArchitecturePort`, `TelemetryPort`, `KnowledgePort`, `ActionExecutorPort`, and `AuditSinkPort`.

### 2.2 Application Layer (`src/vigia/application/`)
- **`evidence_registry.py`**: Assigns sequential `EV-xxx` identifiers, logs connector gaps, and strictly validates citations before proposal registration (`InvalidCitationError` on fabricated or missing IDs).
- **`approval_service.py`**:
  - Evaluates risk from the authoritative backend policy table (`low`, `medium`, `high`) and assigns deterministic status (`CaseStatus.AWAITING_APPROVAL` for pending medium/high actions; `CaseStatus.MITIGATED` for automatically executed low-risk actions).
  - **Immutable pending proposals**: Rejects overwriting a pending proposal with different parameters (`ProposalConflictError`) and deduplicates equivalent calls via stable `idempotency_key`.
  - **Mandatory release validation for rollbacks**: `rollback_change` rejects placeholder release values (`""`, `"none"`, `"N/A"`) and verifies a concrete active release at both proposal registration and approval time (`InactiveReleaseError`).
  - **Strict decision idempotency**: Replaying a decision with the same `idempotency_key` succeeds only when `decision`, `actor`, and `comment` match; conflicting parameters raise `ProposalAlreadyResolvedError`.
- **`tools.py`**: Implements 11 validated tools with call-budget enforcement and LangChain `StructuredTool` wrappers:
  - `compare_revisions` and `read_snippet` cross-check Git SHAs against active releases in the case environment at cutoff, explicitly marking non-deployed revisions as `undeployed_candidate` (`[UNDEPLOYED CANDIDATE - NOT ACTIVE IN <env> AT CUTOFF]`).
  - `propose_action` catches `ProposalConflictError` (`proposal_locked`) and `InactiveReleaseError` (`validation_error`).
- **`investigator.py`**: Implements Alternative A via `langchain.agents.create_agent`; transitions the case to `CaseStatus.AWAITING_APPROVAL` only when a proposal remains `PENDING`, preserving `CaseStatus.MITIGATED` when a low-risk action was already `EXECUTED`.
- **`case_service.py`**:
  - Orchestrates `start_case`, answers informational queries from recorded facts, delegates approval decisions, and verifies post-action telemetry (`_verify_recovery`).
  - **Post-execution timestamp verification**: Compares telemetry samples strictly after `executed_at` on the aligned execution/simulation clock (`point.timestamp > action_at`). If no strictly post-execution sample exists or latency remains above threshold, the case stays `CaseStatus.OPEN` (`recovered=False`) rather than false-closing on pre-action alert readings.

### 2.3 Adapters Layer (`src/vigia/adapters/outbound/`)
- **`service_catalog.py`**: Parses `services.yaml` and maps aliases (`Transferencias` -> `transferencias`).
- **`local_git.py`**: Scoped Git adapter enforcing repository allowlists and relative path safety; `JsonReleaseSourceAdapter` resolves point-in-time active releases.
- **`drawio.py`**: XML parser for Draw.io architecture diagrams preserving ambiguous edges; stamps real file revisions (`sha256:...` or explicitly configured `diagram_revisions`) and returns `ConnectorStatus.NO_RESULTS` when a nonexistent `version` is requested.
- **`telemetry.py`**: Cutoff-bounded CSV metric series, Z-score + linear projection, and regex-redacted error log sampling.
- **`knowledge.py`**: Pure-Python TF-IDF incident search and Markdown runbook parser.
- **`executor.py` & `audit.py`**: Simulated execution with explicit `SimulationState` and append-only `audit.jsonl`.

### 2.4 Composition Root, Packaged Demo & Entrypoint (`src/vigia/`)
- **`runtime.py`**: `build_case_service(...)` and `build_pilot_case_service(PilotRuntimeConfig)` wire either `RuntimeProfile.DEMO` (synthetic adapters) or `RuntimeProfile.PILOT` (Prometheus/Loki + GitHub + SQLite store + controlled executor), importable as `vigia.runtime`.
- **`demo/`**: Synthetic Transfers -> `pagos-core` scenario bundled inside the wheel as package data and loaded via `importlib.resources.files("vigia.demo")`.
- **`entrypoints/cli.py`**: Console script entrypoint `vigia` supporting `--demo-offline`, `--model`, `--profile`, `--mode`, `--probe-service`, and `--serve` (`--host`, `--port`), closing SQLite handles cleanly on exit.

### 2.5 Pilot Capabilities (`diseno-implementacion-piloto-vigia.md`, Deliveries A–D)
- **Delivery A — Service Onboarding & External Sources**:
  - Extended `ServiceConfig` with pilot metadata (`environment`, `metric_unit`, `version_label`, `impact_metric`, `baseline_ms`, `approver_group`, `runbooks_path`, `config_version`, `allowed_actions`, `recovery_criteria`) and `CaseService.probe_service_onboarding(...)` (`ServiceOnboardingReport`).
  - Shared `compute_anomaly_and_projection` helper supporting `indeterminate` projection trends; `PrometheusLokiTelemetryAdapter` (`http_telemetry.py`) with bounded retries; `GitHubReleaseSourceAdapter` & `GitHubRepositoryAdapter` (`github.py`); cutoff-bounded `LocalKnowledgeAdapter`.
- **Delivery B — Alert Ingestion & Durable Cases**:
  - `AlertEnvelope` and Grafana webhook batch ungrouping + bearer/shared-secret auth in `http_api.py`.
  - `SqliteCaseStoreAdapter` (`case_store.py`, WAL mode): transactional alert deduplication `(source, event_id)`, episode correlation into active cases (`MERGED` revisions preserving earlier cutoffs and evidence), `RESOLVED` timeline recording, ordered `case_events`, and durable `work_queue` for worker restart recovery (`process_pending_work`).
- **Delivery C — Shadow Investigation, Comparative Logs & Console**:
  - Pre/post-deploy comparative `window_start`/`window_end` queries on `search_logs`, per-cohort error rates (`cohort_requests`, `cohort_error_rates`, `uncovered_cohorts`), and `InsufficientRollbackEvidenceError` blocking rollbacks with `partial_findings` and `suggested_next_check` when errors precede deployment or telemetry gaps remain open.
  - `OperationalMode.SHADOW` persists `ProposalStatus.RECOMMENDED` and `CaseStatus.AWAITING_REVIEW` with zero external writes (`ShadowModeOperationError`).
  - `VigiaHttpApp` (`http_api.py`): `POST /api/alerta`, `GET /api/caso`, `GET /api/eventos` (SSE with `Last-Event-ID`), `POST /api/revision`, `POST /api/aprobacion`, `GET /api/piloto/metricas`, `GET /api/servicio/prueba`, and live HTML operator console at `GET /`.
- **Delivery D — Assisted Mode, RBAC, Timeouts & Sustained Recovery**:
  - RBAC enforcement (`UnauthorizedApproverError` -> HTTP `403`) and optimistic `proposal_version` / stale release checks (`StaleProposalVersionError`, `ProposalAlreadyResolvedError` -> HTTP `409`).
  - Idempotent single-operation dispatch (`dispatch_operation`), timeout tracking (`OperationStatus.TIMEOUT`), and `CaseService.reconcile_operation(...)` scheduling post-execution observation from effective completion time.
  - `RecoveryCriteriaConfig`-driven verification requiring sustained post-execution improvement (`min_consecutive_samples`, `lookback_minutes`, `require_dependency_logs_clear`): isolated dips keep the case in `CaseStatus.OBSERVING`, while sustained recovery closes it.

### 2.6 Conversational Investigation & Atomic Decision Safety (`diseno-investigacion-conversacional.md`)
- **Single-Investigator Read-Only Follow-Up (`investigator.py`, `tools.py`, `prompts.py`)**:
  - `InvestigatorAgent.run_follow_up(...)` reuses the single Alternative A investigator with `read_only=True` on an isolated deep copy of `CaseRecord`, omitting `propose_action` from `build_langchain_tools()` and mutating nothing outside transactional publication.
- **Deterministic Cohort Comparison (`cohort_comparison.py`, `telemetry.py`)**:
  - `compare_service_cohort_rates` and tool `compare_cohort_error_rates` compute pre/post-deploy cohort error rates in code (`failed_requests` vs `log_events_per_request`), enforcing denominator coverage, minimum request volume, and shared resource saturation -> `ComparisonVerdict.SUPPORTS_CAUSAL_CHANGE` (`maintained`), `ComparisonVerdict.CONTRADICTS_DEPLOYMENT` (`withdrawn`), or `ComparisonVerdict.INDETERMINATE` (`inconclusive` with `missing_data`).
- **Two-Path Conversational Orchestration (`conversation_service.py`)**:
  - Classifies operator turns into `EXPLAIN_PROPOSAL`, `APPROVAL_REQUEST`, `AMBIGUOUS` (resolved deterministically without re-evaluating or creating work), or `CHALLENGE_HYPOTHESIS` / `CURRENT_STATUS` (`"¿Y ahora?"`, reusing dossier evidence or queueing a persistent `follow_up` work item).
- **Atomic Concurrency & Historical Snapshots (`case_store.py`, `approval_service.py`, `case_service.py`)**:
  - SQLite `BEGIN IMMEDIATE` transactions (`create_chat_turn_transactional`, `publish_follow_up_transactional`, `reserve_approval_transactional`, `finalize_operation_transactional`) serialize chat re-evaluations against approvals, reserve `operation_reservations` before calling the external executor (`0` executor calls on `409`), backfill legacy `proposal_history`, and reject stale/under-review/withdrawn approvals (`ProposalUnderReviewError`, `ProposalWithdrawnError`, `StaleRevisionError`, `ChatMessageConflictError`).
- **Chat REST + SSE Console (`http_api.py`)**:
  - `POST /api/casos/{case_id}/mensajes` (idempotent per `(case_id, actor, client_message_id)` with `base_revision` checking) and `GET /api/casos/{case_id}/mensajes`, SSE event `sequence_id` / `turn_id` deduplication, and interactive chat + revision comparison in the HTML console.

---

## 3. Package Management & Execution (`uv`)

This project uses [`uv`](https://docs.astral.sh/uv/) with `pyproject.toml` and `uv.lock` at the repository root.

```powershell
# Install dependencies (including dev tools and editable package) from clean clone
uv sync --extra dev

# Run the full test suite (adapters, investigation, decisions, packaging, pilot Deliveries A-D)
uv run pytest -v

# Run the installed CLI entrypoint offline (approval recovery path)
uv run vigia --demo-offline --decision approve

# Run the installed CLI entrypoint offline (rejection preventive-ticket path)
uv run vigia --demo-offline --decision reject

# Run the offline scenario under pilot shadow mode (no external writes)
uv run vigia --profile pilot --mode shadow --demo-offline

# Probe service onboarding readiness in pilot profile
uv run vigia --profile pilot --probe-service --service Transferencias

# Start HTTP/SSE webhook and operator console server
uv run vigia --profile pilot --mode shadow --serve --host 127.0.0.1 --port 8080

# Build wheel & source distribution
uv build

# Run against a live LangChain model (requires provider API key in environment)
uv run vigia --model google_genai:gemini-2.5-flash
```

