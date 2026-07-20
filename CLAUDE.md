# Second Brain — brain VPS

Personal OS. Single user (Ashish). Solo builder, weekend-only, ADHD-first design.
Full design spec + rationale: `docs/PROJECT_BIBLE.md` (v3.2). Read it when you need the *why* or a feature spec — do not assume, it is the source of truth over anything in this file.

## Stack
- FastAPI + Postgres/pgvector (Supabase self-hosted), systemd-supervised uvicorn
- LangGraph supervisor + Postgres checkpointer
- React Native + Expo app, sideloaded
- n8n on the OLD VPS (203.57.85.220) = glue/ops only + out-of-band health ping
- Alembic for all schema changes

## Architecture rules — these are not style preferences

- These are **LLM-decision workflows**, not autonomous agents: fixed code paths with LLM calls at named decision points. Do not add autonomous loops. The one exception is Claude Code inside the build pipeline.
- **Personal Agent is the ONLY writer to `agent_decisions`.** A specialist that writes it directly is a bug.
- **Specialists never call each other.** All routing goes through the Personal Agent.
- Personal Agent routing is a **lookup in an explicit dispatch map**, not an LLM decision. LLM does phrasing only.
- Every agent implements BaseAgent: typed input/output schema, `handle()`, `interrupt_tier`, `cost_tier`, `requires_context`.
- Specialist subgraphs take **narrow** in/out schemas, never the whole state. Enforce with Pydantic — no arbitrary keys into shared state.
- Every agent declares `requires_context`; the orchestrator preloads exactly that. No agent gets the whole brain.
- If it is not in Postgres, it does not exist. No agent keeps private hidden state.

## Design Invariants — never break these without an explicit decision-log entry

These exist because the system is built for an ADHD brain and dies the moment it creates friction or guilt. v3.1 silently dropped them in a rewrite; that is why they are pinned here.

- **3-second capture.** Voice > text > share-to. Slower than 3s = it will not happen.
- **Zero organisation tax.** AI classifies; the user never files, tags or sorts.
- **No guilt, just data.** Streaks are ratios ("47 of 60 days"), never breakable chains. Gaps render neutral gray.
- **Skip is always visible.** Every flow, prompt, revision question. Partial data beats no data.
- **Voice-first, text-always. Cards, not lists. Skeleton loading.**
- **Hyperfocus-friendly.** While building, the system stays silent; background capture continues.
- **Escalate friction, never lock.** Interrupts are tiered and cooldown-rate-limited.
- **Scheduling:** 50% deadline rule (auto-reschedule if <50% done 6h before deadline), rollover-with-note (unfinished items roll visibly, never vanish), energy-aware load, max 5 items in Today.
- **A completed drill always clears its missed-counter.** A nag that ignores completed work destroys trust in the whole interrupt system.

## Cost rules

Always state cost implications when proposing anything.

- Default tier: Flash/Haiku via 1min.ai. Gemini free tier as fallback. Fail loud + queue.
- **Claude API is scoped to feature-tier builds only.** Do not route runtime agent calls to it.
- Slash commands must skip the classify call — deterministic prefix check runs BEFORE any LLM call. This path is the highest volume; its cost is negative.
- Revision questions: generate once, store in `revision_questions`, reuse across cycles. Never regenerate per review.
- People extraction is **gated** (capitalised-name heuristic or `/people` only), not on every capture.
- Deterministic first, LLM second: wikilink parsing, unlinked mentions, finance calendar (GROUP BY date), health CRUD, ephemeris math, activity lines = zero LLM.

## Conventions

- Prompts live versioned in `prompts/`, loaded by name. **Never inline prompt strings.**
- Every schema change is an Alembic migration. Never an improvised ALTER on live personal data.
- Secrets come from the root-owned EnvironmentFile (0600). Never hardcoded, never committed.
- Log per call: request id, agent_name, latency_ms, model, cost.

## Infra changes (Class B) — the brain modifies the system it runs on

Breaking the brain API kills the Telegram channel that would report the breakage. So:

- **Plan-first, always.** Output a diff/plan and stop for explicit approval. Never auto-apply. (Project repos may auto-stage; infra may not.)
- Pre-change snapshot: git commit of config state to the infra repo + DB dump if schema is touched.
- Post-change health gate: hit `/health` and the Nginx endpoints. On failure, auto-rollback to the git snapshot, then alert.
- **Forbidden zone, permanently human-only: SSH config, firewall rules, and the systemd unit of the agent's own runner.** An agent that can lock you out or modify its own supervisor is a bad night waiting to happen.

## Data sensitivity

This DB holds SMS, call logs, location, finance, journal and birth data — the most sensitive dataset the owner has. Backups leave the VPS. Anything touching dumps, exports or logs must assume the artifact will exist off-box: encrypt before it leaves, never log payload contents.
