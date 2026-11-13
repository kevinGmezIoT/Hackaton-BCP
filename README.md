# Vigía · MVP Investigation & Remediation Assistant

Vigía is an operational incident investigator and human-in-the-loop remediation assistant implemented in Python with **Hexagonal Architecture**, **Pydantic v2** contracts, and **LangChain** (`Alternative A: Tool-Calling Investigator`).

## Repository Structure

```text
.
├── pyproject.toml              # Package metadata, dependencies, `vigia` console entrypoint
├── uv.lock                     # Reproducible dependency lockfile (uv)
├── README.md                   # Clean-clone setup, testing, packaging, and CLI guide
├── src/
│   └── vigia/                  # Core Python package (domain, application, adapters, runtime, cli, demo)
├── tests/                      # Unit, integration, regression, and wheel-packaging tests
└── docs/                       # Architecture specs, HTML mockups, pitch, and Alternative A guide
```

## Quick Start (Clean Clone with `uv`)

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```powershell
# 1. Create/sync virtual environment and install package + dev dependencies
uv sync --extra dev

# 2. Run all tests (including wheel build + out-of-checkout CLI test)
uv run pytest -v
```

## Running the CLI

### Offline Synthetic Demo (`--demo-offline`)

Reproduces the `Transferencias` -> `pagos-core` timeout incident end-to-end without calling an external LLM API, using packaged resources from `vigia.demo`:

```powershell
# Approve the proposed rollback -> simulated execution -> post-rollback recovery verification (184 ms)
uv run vigia --demo-offline --decision approve

# Reject the proposed rollback -> preventive ticket opened -> case closed as rejected
uv run vigia --demo-offline --decision reject

# Interactive confirmation prompt
uv run vigia --demo-offline
```

### Live LLM Mode (`--model`)

Set your provider API key in the environment (for example `GOOGLE_API_KEY` or `OPENAI_API_KEY`) and pass a LangChain model identifier:

```powershell
uv run vigia --model google_genai:gemini-2.5-flash --decision approve
```

## Building & Running the Distributable Wheel

```powershell
# Build wheel into dist/
uv build --wheel
```

The built wheel bundles `vigia.runtime`, `vigia.entrypoints.cli`, and `vigia.demo` data files so `vigia --demo-offline` works outside the repository checkout.

## Documentation

- [Design Specification (Alternatives A, B, C)](docs/README.md)
- [Alternative A Implementation Guide](docs/alternative_a_implementation.md)
- [Technical MVP Document](docs/Vig%C3%ADa%20Documento%20T%C3%A9cnico.html)
- [Project Pitch](docs/Pitch%20Vig%C3%ADa.html)
- [Console UI Mockup](docs/Consola%20Vig%C3%ADa.html)

