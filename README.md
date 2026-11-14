# GitHub Copilot Docs ↔ Gemini Notebook Autonomous Sync

An automated synchronization infrastructure that monitors updates and diffs from the official GitHub documentation repository ([`github/docs`](https://github.com/github/docs) under `content/copilot/`) and autonomously syncs them to Google Docs via the Google Docs API Tabs feature. Designed specifically for knowledge grounding and RAG ingestion in Gemini Notebook (formerly NotebookLM).

By partitioning extensive documentation into topic-driven volumes, this system avoids the Google Docs API limit of 100 tabs per document while maximizing citation accuracy and retrieval precision in Gemini Notebook.

---

## Key Features

- **8-Volume Clustering Architecture**: Evenly distributes documentation across 8 volumes (40–85 files per document based on actual file counts), maintaining a safe margin below the 100-tab hard limit.
- **Automatic Exclusion of Navigation Files (`index.md`)**: Automatically filters out empty frontmatter-only navigation files to prevent tab exhaustion and search noise.
- **3-Stage Liquid Resolution Pipeline**:
  1. **Reusable Inlining**: Recursively resolves and inlines shared procedure snippets under `data/reusables/` (up to 3 levels deep).
  2. **UI & Control Tag Flattening**: Converts environment-specific UI tabs (e.g., `codetab`, `vscode`, `jetbrains`) into clean H4 Markdown headings, and strips version-branching directives (`ifversion`, etc.).
  3. **Variable Token Replacement**: Replaces all `{% data variables.* %}` tokens using key-value dictionaries loaded from `data/variables/*.yml`.
- **Two-Step Synchronization Pipeline**:
  - **Phase 1 (Structural Sync)**: Reconciles the tab topology via `addDocumentTab` and `deleteTab` operations.
  - **Phase 2 (Content Sync)**: Ingests markdown content in atomic batches of up to 10 tabs per chunk to prevent HTTP and socket timeouts.
- **Fail-Safe & Self-Healing Guardrails**:
  - **100-Tab All-or-Nothing Guard**: Calculates predicted tab count before execution; halts immediately with an abort error if the result exceeds 100, preserving commit SHA caches.
  - **Accidental Wipe Protection**: Halts immediately if an unexpected condition attempts to delete all existing tabs.
  - **Tab Underflow Prevention**: Always creates new tabs prior to deleting obsolete ones to strictly enforce Google Docs API's minimum tab requirement.

---

## Document Volume Distribution (8-Volume Architecture)

| Volume    | Title & Scope                                  | Target Directory Paths                                                                                                                                                                                             | Active Files |
| :-------- | :--------------------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------- |
| **Doc 1** | **Concepts & Getting Started** (Active)        | `get-started/`, `concepts/`, `responsible-use/`, `copilot-on-ghes/`                                                                                                                                                | 85           |
| **Doc 2** | **CLI & Terminal Workflows** (Active)          | `how-tos/copilot-cli/`, `how-tos/github-agentic-workflows/`                                                                                                                                                        | 45           |
| **Doc 3** | **Surfaces & Interactive Coding** (Active)     | `how-tos/copilot-in-your-ide/`, `how-tos/copilot-on-github/`, `how-tos/github-copilot-app/`                                                                                                                        | 74           |
| **Doc 4** | **Autonomous Agents & SDK** (Active)           | `how-tos/copilot-sdk/`, `how-tos/use-copilot-agents/`, `how-tos/copilot-integrations/`                                                                                                                             | 67           |
| **Doc 5** | **Enterprise Administration & Security** (Active) | `how-tos/administer-copilot/`, `how-tos/cloud-and-local-sandboxes/`, `how-tos/configure-content-exclusion/`, `how-tos/manage-and-track-spending/`, `how-tos/manage-your-account/`, `how-tos/troubleshoot-copilot/` | 61           |
| **Doc 6** | **Cookbook & Customization** (Active)          | `tutorials/copilot-cookbook/`, `tutorials/customization-library/`                                                                                                                                                  | 43           |
| **Doc 7** | **Tutorials, Adoption & Scale** (Active)       | `tutorials/` (root), `tutorials/budgets/`, `tutorials/cloud-agent/`, `tutorials/roll-out-at-scale/`                                                                                                                | 44           |
| **Doc 8** | **Technical Reference** (Active)               | `reference/`                                                                                                                                                                                                       | 48           |

---

## Directory Structure

```text
├── .agents/
│   ├── AGENTS.md                  # Master context, rules, and guardrails for AI agents
│   ├── rules/
│   │   └── coding-rules.md        # Python coding standards and conventions
│   └── skills/
│       ├── docs_tabs_logic.md     # Google Docs API tabs and transaction specs
│       └── github_sync_logic.md   # GitHub diff retrieval & Liquid resolution specs
├── .github/
│   └── workflows/
│       └── sync.yml               # GitHub Actions scheduled workflow
├── docs/
│   ├── System_Overview.md         # System architecture and 8-volume cluster design
│   └── Detailed_Logic.md          # Algorithms, regex definitions, and data contracts
├── src/
│   ├── config.py                  # Configuration loader and validator
│   ├── docs_client.py             # Google Docs API client (Phase 1 & Phase 2)
│   ├── github_client.py           # GitHub Compare and Git Trees client
│   ├── liquid_resolver.py         # 3-stage Liquid resolution engine
│   ├── main.py                    # Sync orchestrator entry point
│   └── models.py                  # Data models and type definitions
├── tests/                         # Unit and integration test suites
├── config.yaml                    # Watch folder definitions and Google Doc mappings
├── credentials.json               # GCP Service Account key (Git-ignored)
└── requirements.txt               # Python package dependencies

```

---

## Setup & Usage

### 1. Prerequisites

- Python 3.10+
- Google Cloud Project with the Google Docs API enabled

- Service Account Private Key (`credentials.json`)

### 2. Environment Setup

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

```

### 3. Google Docs Configuration

1. Create a new Google Document.

2. Grant **Editor** access to the Service Account email address.
3. Paste the Document ID into the `google_doc_id` field in `config.yaml`.

### 4. Running the Sync

```bash
# Incremental synchronization (detects diffs since last commit)
python src/main.py

# Full baseline sync (forces ingestion and reconciles orphaned tabs)
python src/main.py --force

```

---

## License

[MIT License](https://www.google.com/search?q=LICENSE)
