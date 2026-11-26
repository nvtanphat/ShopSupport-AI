# ShopSupport AI

[![CI](https://github.com/nvtanphat/support-shop/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/nvtanphat/support-shop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)

A customer-support assistant for an e-commerce shop. It answers policy questions from a versioned policy corpus, looks up orders within the signed-in customer's permissions, and handles cancellations, returns and exchanges. The assistant converses in Vietnamese.

The language model does not write to the database. It creates a proposal that expires after 30 minutes; the change is made only when the customer confirms it through the authenticated API, and the domain services re-check eligibility and compute amounts in code at that point.

![Cancelling an order: the assistant proposes, the customer confirms, and the order then shows as cancelled](docs/images/cancel-flow.gif)

Payments, carrier tracking and SMS/OTP use built-in simulators by default (`INTEGRATION_MODE=simulated`), so no money moves. See [integration modes](docs/integration-modes.md). More screenshots are in [docs/images/](docs/images/).

## Components

| Component | Implementation |
|---|---|
| Agent | LangGraph, 5 nodes, one agent with 11 tools, Gemini |
| Retrieval | BM25 candidates, cross-encoder reranking, a threshold for out-of-scope questions; optional BGE-M3 rank fusion |
| Backend | FastAPI, PostgreSQL (SQLite for local development), transactional outbox, server-sent events |
| Frontend | React, Vite, Tailwind: customer chat and staff portal |
| Evaluation | Scenario harness with its own database and ground truth |
| MCP server | The customer tools over the Model Context Protocol (stdio), without a confirm tool |

## Design

- **Proposals instead of actions.** `propose_*` tools create a pending action with a hash and an expiry. `POST /actions/{id}/confirm` executes it after re-validating the order, the return window and the refund amount.
- **Approval by amount and risk.** Refunds up to 500,000 VND with no risk flag are automatic, up to 2,000,000 VND need a support agent, and anything above that or flagged needs a supervisor. The staff member who inspects a returned parcel cannot approve the resulting refund. Limits are in [config/policy/rules.yaml](config/policy/rules.yaml).
- **Checked tool arguments.** Before a tool runs, its arguments are compared with the conversation and earlier tool results: the order must be one the customer chose, items come from `get_order`, the return reason is stated by the customer or quoted from their words, and an exchange is only used when the customer asked for one. Otherwise the tool returns `MISSING_SLOT` and the assistant asks the customer.
- **Checked replies.** Citations, amounts, order codes and "done" statements are compared with successful tool results and with actions the customer confirmed. A draft that does not match goes back to the model once; if it still does not match, a fixed fallback reply is shown.
- **Bounded turns.** Each turn has limits on iterations, tool calls and time. When a detail is missing twice or the same business refusal repeats, tools are switched off and the assistant asks or explains.
- **One execution per confirmation.** Confirming locks the proposal and then its order, so a double click, a retry or two proposals for one order cancel or refund once; a PostgreSQL test sends 6 confirmations at once (before the fix: 6 refunds, after: 1).
- **Refund payouts through an outbox.** The worker pays approved refunds through the payment port and retries a refused payout up to 3 times with growing delays, each with its own idempotency key.
- **Other safeguards.** Idempotency keys, row locks on conversation counters, ownership checks in services, prompt-injection screening, masking of card and ID numbers. See the [threat model](docs/threat-model.md).

## Architecture

```mermaid
flowchart LR
    Chat[Customer chat] --> API[FastAPI]
    Staff[Staff portal] --> API
    API --> Graph[LangGraph agent]
    API --> Domain[Domain services and policy engine]
    Graph --> LLM[Gemini]
    Graph --> RAG[Policy retrieval]
    Graph --> Domain
    Domain --> DB[(PostgreSQL)]
    DB --> Worker[Outbox worker]
```

One turn runs `load_context`, `screen_input`, `agent` (a bounded loop of model and tool calls), `verify_output` and `persist_emit`. The domain layer does not depend on the model, so the rules that compute amounts are tested without it. Details: [architecture](docs/architecture.md), [decision records](docs/adr/).

```mermaid
flowchart LR
    Q[Question] --> N[Normalize]
    N --> B[BM25 candidates]
    B --> R[Rerank up to 10 candidates]
    R --> T{Score above threshold}
    T -->|yes| A[Clauses with version and date]
    T -->|no| X[No clause covers the question]
```

A BGE-M3 vector stage with rank fusion is implemented but off by default. Fusion only reorders the BM25 candidates, and with 10 clauses BM25 never returns more than the 10 the reranker scores, so the reranker sees the same clauses either way. On the retrieval sets the results were identical with and without it, while it costs about 0.6 GB of disk, 1.1 GB of memory and 150 ms per question. `RETRIEVAL_EMBEDDING_ENABLED=true` turns it on for a larger corpus.

Agent tools: `search_policy`, `lookup_order_guest`, `list_my_orders`, `get_order`, `track_shipment`, `check_return_eligibility`, `check_variant_stock`, `get_case_status`, `propose_cancel_order`, `propose_return`, `escalate_to_human`. Tools are filtered by the caller's role, and none of them confirms a proposal or changes order or payment state.

## Evaluation

Figures come from the harness ([docs/eval-report.md](docs/eval-report.md)) for commit `8ab64d9` with `gemini-3.5-flash-lite`. Ground truth is written from the [business specification](docs/business-spec.md).

**Acceptance set**: 16 scenarios written before the last change to the agent and not used to adjust it, 3 runs each.

| Gate | Threshold | Result |
|---|---|---|
| Task success | ≥ 0.90 | 1.000 (48/48) |
| Tool selection and execution | ≥ 0.95 | 1.000 (51/51) |
| Clarification when a detail is missing | ≥ 0.95 | 1.000 (6/6) |
| Business-policy compliance | ≥ 0.99 | 1.000 (48/48) |
| Unauthorized financial actions, incorrect completion claims | 0 | 0, 0 |
| Cross-customer data exposure | 0 | 0 (0/3) |

This set has no multi-turn scenario. **Multi-turn set** (10 scenarios written before their first run, 2–4 customer turns each, 3 runs): task success 27/30, below the 0.95 multi-turn threshold. All misses are one scenario: the assistant refused an excluded item correctly but from the product name and the policy text instead of the eligibility tool ([details](evals/scenarios_multiturn/README.md)). Two earlier independent sets (`F-*`, `G-*`) scored 101/105 and 59/63, each one run below the clarification threshold; the defects they showed were fixed and a new set was written for acceptance ([history](docs/eval-report.md)).

**Development set**: 67 scenarios, 1 run each, Gemini 67/67. It includes scenarios used to fix the agent, so it does not measure generalization. The rule-based oracle scores 67/67 and the always-refusing null agent 10/67, which shows the gates can fail.

**Retrieval**: 48 held-out questions (28 answerable, 20 out of scope): Recall@1 0.929, Recall@4 0.982, 20/20 out-of-scope questions rejected, 0/28 answerable questions rejected.

**Tests**: 438 backend tests including PostgreSQL, 31 frontend unit tests, 17 browser journeys, and 16 of 16 live business scenarios through the HTTP API, PostgreSQL, Gemini and the retrieval models.

**Limitations**
- Scenarios were written by the project team, and the acceptance set is small. The results describe these scenarios, not a success rate with real customers.
- Gemini is an external service. Overload (HTTP 503) and exhausted daily quotas occurred during measurement; the assistant then returns a fallback reply and the request is not completed.
- Not evaluated: concurrent load with the real model, other hardware, a production server.

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 20+ and Docker (PostgreSQL 17).

```bash
cp .env.example .env                       # set GEMINI_API_KEY
docker compose up -d postgres
uv sync --extra rerank
uv run alembic upgrade head
uv run python -m shopagent.cli seed        # sample data and the SL-DEMO01… demo orders
npm --prefix frontend install

uv run --extra rerank uvicorn shopagent.api.app:app --port 8000
uv run python -m shopagent.worker          # outbox worker, separate terminal
npm --prefix frontend run dev              # http://localhost:5173
```

The first start downloads the reranker model (about 0.6 GB) to `.cache/fastembed`. `seed` drops and recreates all tables: development only. On Windows set `PYTHONUTF8=1`, and start the API with `--loop asyncio:SelectorEventLoop` when it uses PostgreSQL.

Demo accounts: customer `customer1@example.com` / `pass123`; staff `agent@shop.vn` or `supervisor@shop.vn` / `staff123`; administrator `admin@shop.vn` / `admin123`. [Demo data](docs/demo-data.md) describes 24 fixed orders. Settings are listed in [.env.example](.env.example), `config/policy/rules.yaml`, `config/models.yaml` and `config/app.yaml`.

## MCP server

`shopagent.mcp_server` exposes 10 of the agent's tools to MCP clients such as Claude Desktop or Cursor, signed in as one customer: policy search, orders, shipment tracking, return eligibility, stock, case status, the two `propose_*` tools and `escalate_to_human`. The tools, input schemas, access checks and refusals are the agent's own (`create_tools`).

The server has no confirm tool. `propose_cancel_order` and `propose_return` create the same expiring proposal as in the chat, and the order changes only when the customer sends `POST /actions/{id}/confirm` with their own sign-in. A client's model therefore cannot carry out a cancellation or a return by itself.

```json
{
  "mcpServers": {
    "shopagent": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/shop-support-agent", "--extra", "rerank", "python", "-m", "shopagent.mcp_server"],
      "env": { "SHOPAGENT_MCP_EMAIL": "customer1@example.com", "SHOPAGENT_MCP_PASSWORD": "pass123" }
    }
  }
}
```

The database is the one in `DATABASE_URL`. Each server process opens its own conversation for its proposals and tickets. Limitations:
- The web chat does not list that conversation, so a proposal made through MCP is confirmed through the API rather than from a card in the web app.
- The chat agent's argument and reply checks do not run, because the client's model calls the tools directly. The domain checks and the confirmation step still apply.

## Testing

```bash
uv run ruff check backend evals && uv run mypy backend/src
uv run pytest backend/tests -q                 # set TEST_POSTGRES_URL to include PostgreSQL tests
npm --prefix frontend test && npm --prefix frontend run test:e2e

uv run python -m evals.harness.runner --agent oracle --gate                     # no model calls
uv run python -m evals.harness.runner --agent langgraph --set acceptance -k 3   # Gemini, acceptance set
```

Runs are stored in `evals/results/<run>/`; thresholds are in [evals/quality_gates.yaml](evals/quality_gates.yaml). The harness uses its own database. Scenario sets and grading: [docs/development.md](docs/development.md).

## Deployment

`docker-compose.prod.yml` runs the API, the worker, PostgreSQL and Caddy. `ops/` has backup, restore, deploy and rollback scripts ([runbook](docs/runbook.md)). The image builds from the repository root (`docker build -f backend/Dockerfile .`) and has been run locally against PostgreSQL. It has not been deployed to a production server, and rollback does not revert migrations.

## Documentation

Code is in English; the interface and most documents under `docs/` are in Vietnamese.

[Architecture](docs/architecture.md) · [Business specification](docs/business-spec.md) · [Data dictionary](docs/data-dictionary.md) · [Threat model](docs/threat-model.md) · [Policy authoring](docs/policy-authoring.md) · [Demo data](docs/demo-data.md) · [Development](docs/development.md) · [Runbook](docs/runbook.md) · [Evaluation report](docs/eval-report.md) · [ADRs](docs/adr/)

## License

[MIT](LICENSE)
