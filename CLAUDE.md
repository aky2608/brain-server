# Second Brain — brain VPS

Personal OS. Single user (Ashish). Solo builder, weekend-only, ADHD-first design.
Full design spec + rationale: `docs/PROJECT_BIBLE.md` (v3.2). Read it when you need the *why* or a feature spec — do not assume, it is the source of truth over anything in this file.

## Stack
- FastAPI + Postgres/pgvector (Supabase self-hosted), systemd-supervised uvicorn
- LangGraph supervisor + Postgres checkpointer
- React Native + Expo app, sideloaded
- n8n on the OLD VPS (203.57.85.220) = glue/ops only + out-of-band health ping
- Alembic for all schema changes

## Architecture rules
- LLM-decision workflows, NOT autonomous agents: fixed code paths with LLM calls at named points. No autonomous loops except Claude Code in the build pipeline.
- Personal Agent is the ONLY writer to agent_decisions. A specialist writing it directly is a bug.
- Specialists never call each other. All routing goes through the Personal Agent.
- Personal Agent routing is a lookup in an explicit dispatch map, not an LLM decision.
- Every agent implements BaseAgent: typed in/out schema, handle(), interrupt_tier, cost_tier, requires_context.
- Specialist subgraphs take narrow in/out schemas, never the whole state. Enforce with Pydantic.
- If it is not in Postgres, it does not exist. No agent keeps private hidden state.

## Design Invariants — never break without an explicit decision-log entry
Built for an ADHD brain; dies the moment it creates friction or guilt.
- 3-second capture. Voice > text > share-to.
- Zero organisation tax. AI classifies; user never files/tags/sorts.
- No guilt, just data. Streaks are ratios, never breakable chains. Gaps render gray.
- Skip is always visible on every flow.
- Voice-first, text-always. Cards not lists. Skeleton loading.
- Hyperfocus-friendly: silent while building; background capture continues.
- Escalate friction, never lock. Interrupts tiered and cooldown-limited.
- Scheduling: 50% deadline rule, rollover-with-note, energy-aware load, max 5 in Today.
- A completed drill always clears its missed-counter.

## Cost rules
Always state cost implications when proposing anything.
- Default tier: Flash/Haiku via 1min.ai. Gemini free fallback. Fail loud + queue.
- Claude API scoped to feature-tier builds ONLY. Never runtime agent calls.
- Slash commands skip the classify call — deterministic prefix check BEFORE any LLM call.
- Revision questions: generate once, store, reuse. Never regenerate per review.
- People extraction is gated, not on every capture.
- Deterministic first, LLM second.

## Conventions
- Prompts live versioned in prompts/, loaded by name. Never inline prompt strings.
- Every schema change is an Alembic migration. Never an improvised ALTER on live data.
- Secrets from root-owned EnvironmentFile (0600). Never hardcoded, never committed.
- Log per call: request_id, agent_name, latency_ms, model, cost.

## Infra changes (Class B) — the brain modifies the system it runs on
Breaking the brain API kills the Telegram channel that reports the breakage.
- Plan-first, always. Output a diff/plan and STOP for explicit approval. Never auto-apply.
- Pre-change snapshot: git commit of config + DB dump if schema touched.
- Post-change health gate: hit /health; on failure auto-rollback to git snapshot, then alert.
- Forbidden zone, permanently human-only: SSH config, firewall rules, and the systemd unit of the agent's own runner.

## Data sensitivity
DB holds SMS, call logs, location, finance, journal, birth data — the most sensitive dataset the owner has. Backups leave the VPS. Anything touching dumps/exports/logs: encrypt before it leaves, never log payload contents.
