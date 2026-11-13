# Vigía · Alternative A Implementation Documentation

This document describes the implementation of **Alternative A (Tool-Calling Investigator)** specified in [app/README.md](../README.md), organized under standard Hexagonal Architecture conventions directly beneath `src/` with English naming and Pydantic v2 domain contracts.

## 1. Architecture Overview

The business domain (rules, entities, value objects, ports) and application layer (use cases, approval flows, citation registries) are strictly separated from infrastructure concerns (Git, files, CLI, Draw.io XML, LangChain).

```text
app/
  pyproject.toml                  # Package configuration with uv & pytest support
  docs/
    alternative_a_implementation.md
  src/
    domain/                       # Core domain entities, value objects, enums, ports
      contracts.py                # Alert, Release, Evidence, Finding, ActionProposal, etc.
      ports.py                    # Typed Protocols for external sources and sinks
    application/                  # Use cases, approval logic, investigator strategy
      case_service.py             # Case lifecycle coordination (start, queries, verify)
      approval_service.py         # Authoritative backend risk table & idempotent decisions
      evidence_registry.py        # Per-case EV-xxx evidence registry & citation checks
      tools.py                    # Pydantic-validated tool implementations + LangChain wrappers
      prompts.py                  # System prompts and case user prompt formatting
      investigator.py             # Alternative A agent via langchain.agents.create_agent
    adapters/                     # Technology-specific implementations of ports
      outbound/
        service_catalog.py        # YamlServiceCatalogAdapter (aliases & telemetry selectors)
        local_git.py              # LocalGitRepositoryAdapter & JsonReleaseSourceAdapter
        drawio.py                 # DrawioArchitectureAdapter (uncompressed Draw.io XML)
        telemetry.py              # CsvTelemetryAdapter (metrics, Z-score, redacted logs)
        knowledge.py              # LocalKnowledgeAdapter (TF-IDF incidents & Markdown runbooks)
        executor.py               # SimulatedActionExecutor & SimulationState
        audit.py                  # JsonlAuditSinkAdapter (audit.jsonl & timeline events)
      inbound/                    # Inbound adapters (entrypoint drivers)
    entrypoints/
      cli.py                      # Interactive & offline terminal CLI runner
    runtime.py                    # Composition root wiring adapters with application & domain
  tests/
    conftest.py                   # Isolated Transfers -> pagos-core workspace & 3-commit Git repo
    fixtures/transfers/           # services.yaml, Draw.io XML, metrics.csv, logs.txt, incidents, runbooks
    test_adapters.py              # Adapter boundaries, cutoff enforcement, and Draw.io parser tests
    test_investigation.py         # End-to-end Alternative A loop, citations, undeployed commit, outages
    test_decisions.py             # Approval, rejection, replay idempotency, verification, risk levels
```

## 2. Implemented Components

### 2.1 Domain Layer (`src/domain/`)
- **`contracts.py`**:
  - `ConnectorStatus`: Distinguishes `ok`, `no_results`, `connector_unavailable`, and `unauthorized`.
  - `Alert`: Bounded by `effective_cutoff` to prevent reading future or post-rollback data during investigation.
  - `Evidence`: Stores verifiable, citable observations (`EV-001`, `EV-002`, ...) with source and reproducible references.
  - `Finding`: Encapsulates hypotheses, supporting citations, contradicting citations, connector gaps, exposed services, and inferred claims.
  - `ActionProposal`, `HumanDecision`, `ExecutionResult`, `VerificationResult`, `CaseEvent`, `AuditRecord`, and `CaseRecord`.
- **`ports.py`**:
  - Typed `Protocol` interfaces: `ServiceCatalogPort`, `ReleaseSourcePort`, `CodeRepositoryPort`, `ArchitecturePort`, `TelemetryPort`, `KnowledgePort`, `ActionExecutorPort`, and `AuditSinkPort`.

### 2.2 Application Layer (`src/application/`)
- **`evidence_registry.py`**: Assigns sequential `EV-xxx` identifiers, logs connector gaps, and strictly validates citations before proposal registration (`InvalidCitationError` on fabricated or missing IDs).
- **`approval_service.py`**: Evaluates risk from backend policy table (`low`, `medium`, `high`), enforces pauses for `medium`/`high` actions, verifies that the target release remains active (`InactiveReleaseError`), and handles duplicate decision replays idempotently (`ProposalAlreadyResolvedError` on conflicting decisions).
- **`tools.py`**: Implements 11 validated tools with call count budget enforcement and LangChain `StructuredTool` wrappers:
  `query_metrics`, `detect_anomaly`, `query_dependencies`, `resolve_release`, `list_changes`, `compare_revisions`, `read_snippet`, `search_logs`, `search_similar_incidents`, `search_runbook`, and `propose_action`.
- **`investigator.py`**: Implements Alternative A using `langchain.agents.create_agent` with budget and stopping criteria; records `partial_findings` and `suggested_next_check` if the run ends without a proposal.
- **`case_service.py`**: Orchestrates `start_case`, answers informational queries from recorded facts, manages approval resolution, and verifies post-action telemetry (`_verify_recovery`), keeping cases `open` if metrics are missing or still degraded.

### 2.3 Adapters Layer (`src/adapters/outbound/`)
- **`service_catalog.py`**: Parses `services.yaml` and maps aliases (`Transferencias` → `transferencias`).
- **`local_git.py`**: Scoped Git adapter enforcing repository allowlists and relative path safety; `JsonReleaseSourceAdapter` resolves point-in-time active releases.
- **`drawio.py`**: XML parser for Draw.io architecture diagrams preserving ambiguous edges.
- **`telemetry.py`**: Cutoff-bounded CSV metric series, Z-score + linear projection, and regex-redacted error log sampling.
- **`knowledge.py`**: Pure-Python TF-IDF incident search and Markdown runbook parser.
- **`executor.py` & `audit.py`**: Simulated execution with explicit `SimulationState` and append-only `audit.jsonl`.

### 2.4 Composition Root & Entrypoints (`src/runtime.py`, `src/entrypoints/`)
- **`runtime.py`**: `build_case_service(...)` connects concrete adapters to application ports.
- **`entrypoints/cli.py`**: Command-line interface with support for live LLM models (`--model`) and deterministic offline runs (`--demo-offline`).

---

## 3. Package Management & Environment Setup (`uv`)

This project uses [`uv`](https://docs.astral.sh/uv/) as its standard package manager and virtual environment runner, adhering to PEP 517 / PEP 621 via `pyproject.toml`.

### 3.1 Initial Setup with `uv`

From the repository root or `app/` folder:

```powershell
cd app

# Create virtual environment with Python 3.12+
uv venv .venv --python 3.12

# Install project and dependencies (including editable install & dev tools)
uv pip install -e ".[dev]"
```

Alternatively, you can synchronize dependencies directly using `uv sync` if managing lockfiles.

### 3.2 Running Tests and CLI with `uv`

You can run commands directly using `uv run`, which automatically uses the local `.venv`:

```powershell
cd app

# Run full test suite with uv
uv run pytest -v

# Run the Transfers -> pagos-core demo offline (approval flow)
uv run python -m entrypoints.cli --demo-offline --decision approve

# Run the Transfers -> pagos-core demo offline (rejection flow)
uv run python -m entrypoints.cli --demo-offline --decision reject

# Run against a live LangChain model (requires provider API key in environment)
uv run python -m entrypoints.cli --model google_genai:gemini-2.5-flash
```

Or invoke the virtual environment binaries directly:

```powershell
./.venv/Scripts/python.exe -m pytest -v
./.venv/Scripts/python.exe -m entrypoints.cli --demo-offline --decision approve
```
