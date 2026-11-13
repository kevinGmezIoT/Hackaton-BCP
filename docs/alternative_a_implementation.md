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
      case_service.py             # Case lifecycle orchestration (start, queries, verify)
      approval_service.py         # Authoritative backend risk table, proposal lock & idempotency
      evidence_registry.py        # Per-case EV-xxx evidence registry & citation checks
      tools.py                    # Pydantic-validated tool implementations + LangChain wrappers
      prompts.py                  # System prompts and case user prompt formatting
      investigator.py             # Alternative A agent via langchain.agents.create_agent
    adapters/                     # Technology-specific implementations of ports
      outbound/
        service_catalog.py        # YamlServiceCatalogAdapter (aliases & telemetry selectors)
        local_git.py              # LocalGitRepositoryAdapter & JsonReleaseSourceAdapter
        drawio.py                 # DrawioArchitectureAdapter (Draw.io XML + file/configured revisions)
        telemetry.py              # CsvTelemetryAdapter (metrics, Z-score, redacted logs)
        knowledge.py              # LocalKnowledgeAdapter (TF-IDF incidents & Markdown runbooks)
        executor.py               # SimulatedActionExecutor & SimulationState
        audit.py                  # JsonlAuditSinkAdapter (audit.jsonl & timeline events)
    entrypoints/
      cli.py                      # Interactive & offline terminal CLI (`vigia` console script)
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
- **`runtime.py`**: `build_case_service(...)` connects concrete adapters to application ports (importable as `vigia.runtime`).
- **`demo/`**: Synthetic Transfers -> `pagos-core` scenario bundled inside the wheel as package data and loaded via `importlib.resources.files("vigia.demo")`.
- **`entrypoints/cli.py`**: Console script entrypoint `vigia` supporting deterministic offline mode (`--demo-offline`) and live LangChain models (`--model`), prompting for human decisions only when `case.proposal.status == ProposalStatus.PENDING`.

---

## 3. Package Management & Execution (`uv`)

This project uses [`uv`](https://docs.astral.sh/uv/) with `pyproject.toml` and `uv.lock` at the repository root.

```powershell
# Install dependencies (including dev tools and editable package) from clean clone
uv sync --extra dev

# Run the full test suite (adapters, investigation, decisions, wheel packaging)
uv run pytest -v

# Run the installed CLI entrypoint offline (approval recovery path)
uv run vigia --demo-offline --decision approve

# Run the installed CLI entrypoint offline (rejection preventive-ticket path)
uv run vigia --demo-offline --decision reject

# Build wheel & source distribution
uv build

# Run against a live LangChain model (requires provider API key in environment)
uv run vigia --model google_genai:gemini-2.5-flash
```

