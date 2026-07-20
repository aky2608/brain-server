**ZEROTOBUILT.IN**

**Second Brain --- Project Bible**

Version 3.2 \| July 2026

*Consolidates: v3.1 architecture + full audit + design review sessions
(July 2026)*

Builder: Ashish \| Infrastructure: brain VPS + old VPS (203.57.85.220)

Database: PostgreSQL + pgvector (Supabase self-hosted) \| AI: 1min.ai +
Gemini + Claude API (scoped)

**0. Purpose of this document**

This is the single source of truth for the Second Brain system as of
July 2026. It supersedes the v3.1 architecture document for design
intent, and records not only what the system is, but why each decision
was made and what was deliberately rejected. Upload this document to any
new working session to carry full context.

**Reading order for a new session:** Section 1 (audit) explains what was
broken or missing; Section 2 (decision log) explains every accepted and
dropped change; Sections 3--5 are the current system spec (pages,
agents, memory); Sections 6--13 are detailed feature designs; Sections
14--16 are schema, cost, and build sequence.

**1. System audit --- gaps found and why they matter**

A full audit of v2.0 (live system) against v3.1 (plan) surfaced ten
gaps. Each is listed with severity, the reason it matters, the fix, and
its cost. All fixes are adopted.

**1.1 Critical --- trust and safety of the data**

**Gap 1: No security model**

The system holds SMS, call logs, location, finance, journal entries and
birth data --- the most sensitive dataset Ashish owns --- yet the only
security item in v3.1 was user_profile encryption. Missing:
authentication on the SMS-forwarding webhook (n8n on the old VPS posts
to the new VPS --- it needs a shared secret header), fail2ban / rate
limiting on api.zerotobuilt.in, and above all backup encryption. The
nightly pg_dump leaves the VPS containing everything; unencrypted, it is
the single biggest leak vector in the entire design.

-   **Fix:** age- or GPG-encrypt dumps before off-VPS upload;
    shared-secret auth between n8n and the API; fail2ban on the brain
    VPS. Cost: ₹0, lands in Phase 0.

**Gap 2: No disaster recovery runbook / infra-as-code**

If the brain VPS dies, rebuild time is unknown because Nginx configs,
systemd units, docker-compose and env files exist only on the box. An
untested, undocumented recovery path means the system's continuity
depends on memory.

-   **Fix:** private git repo holding every config file plus a
    RESTORE.md, paired with the quarterly test-restore. Cost: ₹0,
    roughly half a day. This repo later doubles as the safety mechanism
    for the infra-class Coding Agent (Section 11).

**Gap 3: No schema migration discipline**

v3.1 says existing tables are \"extended, not replaced\" but names no
tooling. At 84 items schema changes can be improvised; at 10,000 they
cannot, and an improvised ALTER on live personal data is how data gets
lost.

-   **Fix:** adopt Alembic now, in Phase 0/2. Every schema change in
    this document ships as a migration. Cost: ₹0.

**1.2 Important --- the system never learns**

**Gap 4: No correction feedback loop**

The Capture Agent classifies every item, but when it is wrong and Ashish
fixes a category, nothing records the correction. The system stays
exactly as smart as its first prompt, forever.

-   **Fix:** corrected_category + corrected_at columns on items; a
    monthly rollup reports misclassification rate per category;
    corrections later feed back into the classify prompt as few-shot
    examples. The cheapest intelligence upgrade in the whole system.
    Cost: ₹0 beyond one column.

**Gap 5: No dedup / idempotency**

Offline queue + retries + share-to intent mathematically guarantees
duplicate captures over time.

-   **Fix:** client generates a capture UUID; /capture upserts on it.
    Cost: ₹0.

**Gap 6: Embedding model not versioned**

The schema says VECTOR(1536) but never records which model produced each
embedding. If the model is ever switched, old and new vectors silently
mix and semantic linking quietly degrades with no visible error.

-   **Fix:** embedding_model column + a spec for a batch re-embed job.
    Cost: ₹0 now; avoids a painful forensic migration later.

**1.3 The gap that was felt but not named**

**Gap 7: v3.1 dropped the v2 design soul**

The ADHD design contract (3-second capture, skip-always, ratio streaks,
no-guilt) and the Planner intelligence (50% deadline rule,
rollover-with-note, energy-aware load, max-5-today) appear nowhere in
v3.1. They were not consciously cut --- they simply did not survive the
rewrite. Had the Scheduling Agent shipped from v3.1 as written, it would
have had none of them.

-   **Fix:** the Design Invariants section of this document (Section
    2.1) is carried verbatim into every future version. No version may
    silently drop an invariant; dropping one requires an explicit
    decision-log entry.

**1.4 Minor**

-   **Gap 8 --- stale cost model:** v2's cost table predates the News
    API (\$3--5 / 1k queries), possible Google Vision OCR (\~\$1.50 / 1k
    pages) and the recommended second VPS. Fixed by the consolidated
    table in Section 15.

-   **Gap 9 --- Telegram is a single point of failure:** every
    notification flows through one bot and nothing monitors the bot
    itself. Fix: daily bot self-test ping; the n8n health ping on the
    old VPS is the out-of-band dead-man's channel (it has its own bot
    access, independent of the brain API).

-   **Gap 10 --- no prompt management:** 14 modules each carry prompts
    with no stated home. Fix: prompts live versioned in the repo, loaded
    by name --- never inline strings.

**2. Decision log --- accepted, changed, dropped**

Every design decision from the July 2026 review sessions, with
reasoning. Dropped items record why, so they are not re-litigated by
accident in future sessions.

**2.1 Design Invariants (restored from v2, now permanent)**

These rules survive every future version. They exist because the system
is built for an ADHD brain and dies the moment it generates friction or
guilt.

-   3-second capture rule --- voice \> text \> share-to. If capture
    takes longer than 3 seconds, it will not happen.

-   Zero organisation tax --- AI classifies everything; the user never
    files, tags or sorts. Slash commands are an optional shortcut, not
    an obligation.

-   No guilt, just data --- streaks are ratios (\"47 of 60 days\"),
    never chains that break. Gaps render neutral gray.

-   Skip is always visible --- every flow, every prompt, every revision
    question has skip. Partial data beats no data.

-   Voice-first, text-always. Cards, not lists. Skeleton loading.

-   Hyperfocus-friendly --- while building, the system stays silent;
    background capture continues.

-   The system escalates friction, never locks (see 2.4). Interrupts are
    tiered and rate-limited by cooldowns.

-   Scheduling Agent invariants: 50% deadline rule (auto-reschedule if
    \<50% done 6h before deadline), rollover-with-note (unfinished
    today-items roll to tomorrow visibly, never vanish), energy-aware
    load (low mood/energy trend → fewer suggested tasks), max 5 items in
    Today.

**2.2 Kanban page --- DROPPED**

**Decision:** the Kanban page is removed. Kanban earns its keep in teams
--- many people, parallel work, status visibility. A single person doing
1--3 things at a time does not need a page to answer \"what am I doing
right now\"; and for an ADHD brain a board of unfinished columns is a
standing guilt surface, violating the invariants above.

-   **Replacement:** status chips inside Planner. Each item cycles todo
    → doing → done on tap; the \"doing\" item floats to the top of Today
    with a highlight; done items collapse into a strike-through count
    (\"3 done today\").

-   **Reversibility:** task_status stays in the schema unchanged, so a
    board view can be resurrected in a weekend if ever missed. Zero
    migration cost.

**2.3 Music page --- MERGED into Tech & Resources**

Music was the thinnest page in the roster. It becomes a filter tag
inside Tech & Resources; mood-tagging of tracks is preserved. Combined
with the Kanban removal, the page count goes 14 → 12.

**2.4 GATE lock-out --- REJECTED (escalating friction adopted instead)**

Proposal considered: lock the system if GATE drills are repeatedly
missed. Rejected on three grounds. First, it violates the no-guilt
invariant. Second, locking the capture system as punishment breaks the
one habit everything else depends on --- miss GATE → locked out →
thoughts stop being captured → system goes stale → system gets
abandoned; it punishes the wrong behaviour. Third, Ashish has root on
the VPS: any lock bypassable in 30 seconds is not a lock, it is a speed
bump that breeds resentment.

**Adopted instead --- escalating friction:** miss 3× → morning brief
leads with GATE before anything else. Miss 5× → Scheduling Agent
auto-places the drill as item #1 inside the protected morning hour and
says so. The system gets pushier, never closed.

**2.5 GATE drill verification --- ADOPTED**

A drill only counts as done with evidence, not a tap:

-   A workbook/notebook drill entry must exist with actual answer
    content --- minimum N questions answered, not an empty submit.

-   Timestamp sanity: open-to-submit time \> 2 minutes; a 10-second
    submit on a 10-question drill is flagged and counts as skipped.

-   Optional scoring: one Gemini Flash call grades answers → stored
    score → Review Agent shows a real GATE readiness trend, not just
    attendance.

-   The watch-rule counter clears only on a verified entry, and
    agent_decisions logs why (\"cleared: 8/10 answered, 14 min, score
    6/10\"). A completed drill always clears the missed-counter --- a
    nag that ignores completed work destroys trust in the interrupt
    system.

**2.6 Workbook --- REDESIGNED into Notebook**

Workbook as specced in v3.1 (templates + drills) was too narrow for how
Ashish actually works across GATE, three ventures, tech learning and
occult studies. It is replaced by a OneNote-style Notebook (Section 7)
that is a strict superset: drills survive as a section type, so all GATE
verification logic carries over unchanged. Page count stays 12 ---
Workbook is renamed, not added to.

**2.7 Other adopted decisions (detailed in later sections)**

-   Slash-command capture with user-editable alias table (Section 6).

-   People entity resolution with provisional → confirmed relationship
    learning (Section 9).

-   Finance upgrade: classification learning, subscriptions, manual/cash
    entries, calendar view, dedicated transactions table (Section 8).

-   Projects and Notebooks are user-creatable and archivable --- never
    hard-deleted (Section 10).

-   Every project auto-creates a linked notebook with an agent-written
    Activity section (Section 10).

-   Coding Agent formalised: two execution tiers (Aider/Flash vs Claude
    Code) and two target classes (project repos vs VPS infra) with hard
    guardrails on infra (Section 11).

-   Revision engine: spaced recall over notebook pages with closed-book
    question generation (Section 12).

-   deadline_mode flag on notebook sections: revision intensity ramps as
    a fixed date approaches --- works for interviews, exams and client
    demos alike (Section 12.4).

-   Obsidian feature parity where cheap; plugins, canvas and publish
    explicitly skipped; portable export produces an Obsidian-compatible
    vault as the anti-lock-in escape hatch (Section 13).

**3. Pages --- final 12 and how each works**

Down from 17 (v2) → 14 (v3.1) → 12 (this document): Kanban removed into
Planner chips, Music merged into Tech & Resources, Notes+Quotes already
merged into Thoughts, Workbook renamed Notebook.

  -------------------------------------------------------------------------------
  **\#**   **Page**    **Owning     **How it works**
                       agent**      
  -------- ----------- ------------ ---------------------------------------------
  1        Dashboard   Personal     Status strip → terse one-liner → interrupt
           (Ask Brain) Agent        banner (only when a watch-rule fires) →
                                    unified timeline (today's plan + Google
                                    Calendar + weekend venture blocks; the
                                    protected GATE morning hour renders as a
                                    fixed immovable slot) → persistent input bar.
                                    Slash commands work from this bar ---
                                    /finance chai 20 is the fastest capture path.
                                    Any message starting with \"why\"/\"explain\"
                                    pulls full reasoning from agent_decisions.

  2        Planner     Scheduling   Buckets: today / this week / this month /
                       Agent        someday / unplanned. 6:30am run reads open
                                    tasks + yesterday's energy + fixed
                                    constraints → writes ≤5 items to Today with
                                    reasoning logged. Reactive rebalance on any
                                    task change. Status chips replace Kanban: tap
                                    cycles todo → doing → done; doing floats to
                                    top; done collapses to a strike-through
                                    count. All Section 2.1 invariants apply (50%
                                    rule, rollover-with-note, energy-aware load).

  3        Journal     Journal      9pm nudge → mood/energy 1--5 + up to 3
                       Agent        prompts, skip-always. Daily rollup: the day's
                                    captures + mood → one summary paragraph (one
                                    Haiku call). The rollup is a linkable
                                    daily-note page (\[\[2026-07-17\]\]). Ratio
                                    streaks; gray gaps.

  4        Thoughts    Thoughts     Fleeting fragments. \[\[wikilinks\]\]
                       Agent        regex-parsed on save (no LLM) → embed →
                                    semantic auto-links (cosine \> 0.82, cap 5).
                                    Backlinks panel + unlinked mentions. Full
                                    graph view plus per-note local graph (1 hop).
                                    Legacy scans link in here tagged
                                    source=legacy_scan.

  5        Notebook    Revision     OneNote-style: Notebook → Section → Page.
                       engine +     Sections typed notes or drill. Markdown
                       Review Agent editing with live preview, \[\[wikilinks\]\]
                       summaries    into the shared graph, #tags, page templates.
                                    Per-section \"what I learned\" weekly summary
                                    at the top of each section --- solves ADHD
                                    re-entry after a gap. Full spec: Section 7.

  6        Finance     Finance      SMS pattern-match (free) → transactions;
                       Agent        unmatched formats get one Flash call.
                                    Manual/cash via /finance. Subscription
                                    detection with monthly total +
                                    next-expected-date + missed-charge badge.
                                    Month calendar view with daily net
                                    spent/received per date. Infra self-cost
                                    card. Full spec: Section 8.

  7        Projects    Project      One view per project: WayClear, Veridh,
                       Agent (+     Dikam, Brain. User-creatable/archivable. Each
                       Coding       project has a linked notebook whose Activity
                       Agent)       section the Project Agent writes
                                    automatically. Build history with
                                    promote/kill controls. Full spec: Sections
                                    10--11.

  8        Tech &      Capture      Share-to URLs, articles, tools, music (as a
           Resources   Agent        filter tag, mood-tagging preserved).
                       (routing)    AI-tagged, searchable. No dedicated agent.

  9        Health      Health Agent Manual form v1: sleep, exercise, meals,
                                    vitals → health_logs. Pure CRUD, no LLM.
                                    Supplement-stack tracking lives here. Google
                                    Fit sync deferred deliberately.

  10       Review      Review Agent Tabs: Weekly / Time Capsule. Sunday cron → 7
                                    days of items → one summary call → stored +
                                    Telegram. Also reports on the system itself:
                                    misclassification rate (Gap 4) and agent cost
                                    for the week from agent_metrics_daily.

  11       People      People Agent Contact records with aliases, mention_count,
                                    relationship status (provisional/confirmed),
                                    last_contact_at. \"Gone quiet\" is a passive
                                    badge, never a Telegram interrupt. Full spec:
                                    Section 9.

  12       Legacy      Legacy       Scan (phone) → upload → OCR → regex date
           Archive     Ingestion    extraction → classify per chunk → link into
                       Agent        the Thoughts graph. OCR pilot: 20--30 pages
                                    through Tesseract (free) vs Google Cloud
                                    Vision (\~\$1.50/1k pages, better on
                                    handwriting) before committing volume.
                                    Ambiguous dates batched to Telegram for
                                    confirmation.
  -------------------------------------------------------------------------------

**4. Agents --- detailed working**

Terminology stands as corrected in v3.1 Section 14: these are
LLM-decision workflows --- fixed code paths with LLM calls at specific
decision points --- not autonomous agents. This is deliberate: fixed
workflows with individually-logged decision points are easier to log,
debug and trust, which is the entire premise of the \"ask why, get a
traceable answer\" experience. The one genuinely agentic component is
Claude Code inside the build pipeline (Section 11), which loops
autonomously in its own process.

Every agent implements the BaseAgent contract (typed input/output
schema, handle(), interrupt_tier, cost_tier, requires_context).
Specialists never write to agent_decisions directly and never call each
other --- every action routes through the Personal Agent, the single
writer to the decision log.

**4.1 Agent roster with mechanics**

  -----------------------------------------------------------------------------------------------------------
  **Agent**        **Trigger**          **Steps (fixed)**                                    **LLM decision
                                                                                             point**
  ---------------- -------------------- ---------------------------------------------------- ----------------
  Personal Agent   Every capture +      Dispatch via explicit map (e.g.                      Phrasing only;
  (orchestrator)   on-demand chat       {\"scheduling\":\"haiku\",\"capture\":\"flash\"}).   routing is a
                                        Conversation: hard fork on \"why/explain\" prefix →  lookup
                                        detailed template with reasoning from                
                                        agent_decisions; else terse one-liner. Sole writer   
                                        to agent_decisions.                                  

  Capture Agent    Instant or batch     Slash-prefix check first (deterministic, skips LLM   One
                   capture              entirely) → else save raw → one classify call →      classification
                                        embed → pgvector link pass → write. Upsert on client call (skipped
                                        capture UUID (dedup). Corrections logged to          for slash
                                        corrected_category.                                  captures)

  Scheduling Agent Cron 6:30am +        Query open tasks, yesterday's energy, fixed          One planning
                   reactive on task     constraints (GATE hour, day job, interview dates) →  call
                   change               one call producing ordering + reason → write plan →  
                                        log → Telegram brief. Applies 50% rule,              
                                        rollover-with-note, energy-aware load, max-5,        
                                        deadline_mode ramps.                                 

  Finance Agent    SMS classified as    Pattern-match amount/merchant/direction → write      Only on
                   finance + /finance   transaction. Unmatched format → one Flash call.      unmatched SMS
                                        Recurrence detector flags subscriptions. Calendar    formats
                                        view = GROUP BY date.                                

  Project Agent    action_class=build   Package spec (project context + repo + task +        None at this
                   or /build            complexity) → dispatch to Aider (small) or Claude    level --- the
                                        Code (feature) → staging deploy → Telegram           handoff target
                                        promote/kill. Writes Activity line to the project    (Claude Code) is
                                        notebook.                                            agentic

  Health Agent     Manual entry         Write structured fields to health_logs.              None --- pure
                                                                                             CRUD

  Review Agent     Cron, Sunday         Query 7 days of items + drill scores +               Summarisation
                                        agent_metrics_daily → one summary call → store +     calls
                                        Telegram. Also writes per-section Notebook learning  
                                        summaries (1--3 Haiku calls, active sections only).  

  Journal Agent    Daily cron           Aggregate day's captures + mood/energy → one call →  One
                                        daily-note page.                                     summarisation
                                                                                             call

  Thoughts Agent   Every capture / page Regex-parse \[\[wikilinks\]\] → embed → pgvector     None --- fully
                   save                 search (cutoff 0.82, cap 5) → write thought_links.   deterministic
                                        Unlinked-mention pass is string matching against     
                                        titles.                                              

  Legacy Ingestion Manual batch upload  OCR (no LLM) → regex dates → one classify call per   One
  Agent                                 chunk → link into graph.                             classification
                                                                                             call per chunk

  News Agent       On-demand only       Question → Brave/Tavily search → one summarising     One
                                        call. Never in the morning brief; isolated from the  summarisation
                                        core loop.                                           call

  Almanac Agent    On-demand only       Swiss Ephemeris computation (pure math: moon phase,  Phrasing only
                                        tithi/nakshatra/yoga, holidays from static dataset)  
                                        → LLM phrases result.                                

  Astrology Agent  On-demand only       Ephemeris math + existing Chaldean-Vedic / Lo Shu    Interpretive
                                        numerology calculator (deterministic) → LLM frames   framing only
                                        interpretation. Reads encrypted user_profile.        

  People Agent     Every capture        Runs only when a capitalised-name heuristic or       One extraction
                   (gated)              /people fires → one Flash extraction call (name,     call, gated ---
                                        relationship phrase) → fuzzy match →                 not on every
                                        provisional/confirmed logic (Section 9) → write      capture
                                        links, bump last_contact_at. Quiet-check is a date   
                                        comparison.                                          

  Revision engine  Daily cron           Pull due pages (next_review_at) → generate-once      Question
  (job, not a chat                      questions (closed-book, one Flash call per page) →   generation +
  agent)                                send 1/day max via Telegram → grade reply (one Flash fuzzy grading
                                        call) → store attempt → update interval + weak-spot  
                                        bias.                                                
  -----------------------------------------------------------------------------------------------------------

**4.2 Watch rules (interrupt layer, not an agent)**

agent_watch_rules holds tunable, data-driven checks: scheduling
conflict, reminder snoozed 3× without progress, GATE drill missed 3× in
a rolling 7 days, interview ≤5 days away with zero prep entries this
week. Each rule carries last_notified_at with a 24h cooldown so one
unresolved condition cannot spam Telegram. Verified drill/prep entries
clear their counters automatically. Interrupt tiers: always-interrupt
(Telegram immediate), morning-brief-only (6:30am batch), log-only
(agent_decisions, visible on request).

**5. Agent memory architecture**

\"Memory\" in this system is not one thing --- it is five distinct
layers, each answering a different question. Naming them prevents the
classic failure of stuffing everything into one blob.

**5.1 Working memory --- LangGraph typed state + Postgres checkpointer**

The shared state is a strict typed schema (Pydantic/TypedDict) with a
fixed field set --- specialists cannot stuff arbitrary keys into it.
Specialist subgraphs receive and return narrow input/output schemas
(state isolation), not the whole state. The Postgres checkpointer
persists state across process restarts, so a crash mid-pipeline resumes
rather than losing the capture. Lifetime: one pipeline run.

**5.2 Episodic memory --- agent_decisions**

Every action any agent takes: (agent_name, item_id, action_taken,
reason, interrupt_tier, created_at). Single writer: the Personal Agent.
This one table is simultaneously the audit trail, the notification
filter, and the source for every \"why did you do that\" answer. It is
what makes the system trustworthy enough to rearrange a day silently.
Lifetime: permanent, log-rotated at the journald level only.

**5.3 Semantic memory --- pgvector embeddings**

Every item and notebook page is embedded. This layer powers Thoughts
auto-linking, People mention matching, agent context retrieval and brain
search. embedding_model is recorded per row (Gap 6) so the corpus can be
re-embedded coherently if the model ever changes. Lifetime: permanent,
exportable including vectors.

**5.4 Procedural / learned memory --- the tables that make it smarter**

-   **corrected_category / corrected_at on items:** every user
    correction, later fed back as few-shot examples into the classify
    prompt. The system's only true learning loop.

-   **capture_shortcuts:** the slash-alias map --- user-taught routing
    that bypasses the LLM.

-   **agent_watch_rules:** tunable thresholds --- behavioural settings
    as data, not code.

-   **revision_questions / revision_attempts:** what the system knows
    about what Ashish knows --- intervals, scores, weak spots.

-   **people (aliases, mention_count, status):** accumulated identity
    knowledge, confirmed through repetition.

**5.5 Context preloading --- requires_context**

Each agent declares what it needs (e.g. Scheduling:
\[\"open_tasks\",\"energy_trend\",\"fixed_constraints\"\]; Astrology:
\[\"user_profile\"\]; People: \[\"people_index\"\]). The orchestrator
preloads exactly that --- state isolation made concrete. No agent gets
the whole brain; every agent gets enough.

**5.6 What agents deliberately do NOT remember**

No agent keeps private hidden state outside these tables. If it is not
in Postgres it does not exist --- which is what makes backup (encrypted
pg_dump + portable JSON export with embeddings) a complete memory
snapshot, and a quarterly test-restore a full test of the system's mind.

**6. Slash-command capture**

**Why:** when the destination is already known, classification is wasted
latency and wasted money. A deterministic prefix check runs before any
LLM call.

> /finance chai 20 -\> category=finance, LLM classify skipped
>
> /wayclear call MCD monday -\> subcategory=wayclear, action_class=task
>
> /thought \... -\> Thoughts Agent
>
> /journal \... -\> mood/journal path
>
> /people Rahul is my cousin -\> People Agent
>
> /gate paging notes \... -\> Notebook: GATE inbox section
>
> /build veridh add pricing FAQ -\> Project Agent build pipeline
>
> /job applied TCS sec architect -\> Job Search notebook

-   Aliases live in a capture_shortcuts table (alias →
    category/subcategory/agent/notebook) --- adding /veridh2 later is a
    row insert, not a deploy. Creating a project or notebook
    auto-registers its alias.

-   Unknown slash falls through to normal classification --- never
    errors. Works identically in Telegram, the app text box, share-to
    captions and the Ask Brain input bar.

-   **Cost implication: negative.** Every slash capture skips one LLM
    call; the highest-volume path becomes free.

**7. Notebook (replaces Workbook)**

**Why:** Ashish works across many domains and needed a section-wise
deliberate notes area with \"what I learned\" rollups ---
OneNote-shaped, not template-shaped. The old Workbook survives inside it
as drill-type sections, so nothing is lost.

> Notebook -\> Section -\> Page
>
> GATE / {Algorithms, OS, Drills(type=drill)}
>
> WayClear, Veridh, Dikam (auto-created per project, Section 10)
>
> Tech Learning, Occult & Numerology, Job Search \...

**7.1 Behaviour**

-   Pages: markdown with live preview, \[\[wikilinks\]\] (indexed into
    the shared graph by the Thoughts Agent), inline #tags merged with
    ai_tags, page templates (drill, JD analysis, decision log).

-   **Boundary rule:** Thoughts = fleeting fragments captured in
    seconds; Notebook = deliberate sitting-down work. Same linking
    engine underneath, different capture intent.

-   **\"What I learned\" rollup:** a lightweight #learned or \"TIL:\"
    marker (regex, no LLM) plus a weekly per-section summary by the
    Review Agent (one Haiku call per active section): \"GATE/OS this
    week: paging, thrashing, 2 drills, avg 7/10\". The summary sits at
    the top of the section --- open GATE after a gap and immediately see
    where you left off. This is the ADHD re-entry problem solved, and
    something OneNote itself does not do.

-   Drill sections keep structured Q&A + progress %; all GATE
    verification logic (Section 2.5) applies unchanged.

**7.2 Schema**

> notebooks (id, title, icon, sort, archived)
>
> sections (id, notebook_id, title, type: notes\|drill, deadline_mode,
> deadline_date, sort)
>
> pages (id, section_id, title, content, next_review_at,
> review_interval, created_at, updated_at)

-   Cost: zero new infra; 1--3 extra Haiku calls weekly (active sections
    only).

**8. Finance --- full specification**

-   **Classification with learning:** SMS pattern-match first (free);
    unmatched formats get one Flash call → category (food, transport,
    infra, subscriptions, family...). Corrections logged exactly like
    capture corrections --- same learning loop.

-   **Manual & cash entries:** /finance chai 20 or /finance received
    5000 dikam client. Direction auto-detected
    (\"received/refund/credit\" → income; default → spend). Cash ---
    invisible to SMS --- finally gets tracked.

-   **Subscriptions:** recurrence detection --- same merchant ± same
    amount at \~monthly interval → flagged, shown in a dedicated card
    with monthly subscription total + next expected date. A missed
    expected charge raises a badge (cancelled, or card failed --- both
    worth knowing). VPS/API infra costs land here automatically, so the
    system reports its own running cost.

-   **Calendar view:** month grid; every date marked with daily net ---
    spent (red) / received (green); tap a date for its transactions.
    Deterministic --- one GROUP BY date query, zero LLM.

> transactions (id, item_id, date, amount, direction, category,
> merchant,
>
> is_subscription, recurrence_id, corrected_category)

A dedicated table rather than overloading items: finance rows are
queried by date/amount/merchant constantly and deserve their own
indexes.

**9. People --- entity resolution and relationship learning**

**Requirement:** once a relationship is stated, repeated mentions should
build the person's record automatically.

-   Extraction: gated --- runs only when a capitalised-name heuristic or
    /people fires (one Flash call), not on every capture. Extracts
    (name, relationship phrase): \"met Rahul, my cousin, for dinner\".

-   Matching: exact/fuzzy name + aliases against people. No match →
    create with status=provisional.

-   Learning: first mention stores the relationship; 2--3 consistent
    mentions → confirmed. A conflicting mention (\"Rahul my
    colleague\"?) could be a different Rahul → ask once via Telegram:
    \"Same Rahul (cousin) or new person?\" --- never silently overwrite.

-   Every linked capture bumps last_contact_at --- which makes the
    \"gone quiet\" badge accurate across all interactions, not
    call-log-only. It stays a passive badge on the People page, never a
    Telegram interrupt.

> people (id, name, relationship, status: provisional\|confirmed,
>
> aliases JSONB, mention_count, last_contact_at, tags)
>
> item_people (item_id, person_id)

**10. Projects, notebooks and the auto-journal**

**10.1 Create / remove**

-   Projects and notebooks are user-creatable from the UI. Creating a
    project auto-registers its slash alias (capture_shortcuts row ---
    works instantly, no deploy) and auto-creates its linked notebook.

-   **Delete is always archive, never destroy:** captures keep their
    history, the graph keeps its edges; archived items hide from active
    views but stay searchable.

**10.2 Project auto-journal (Activity section)**

Every project notebook contains an Activity section written by the
Project Agent:

-   Build staged → \"Staged pricing page → test.veridh.in\"
    (deterministic write, no LLM).

-   Capture tagged /veridh → appended to the project inbox page.

-   Task done / decision made → dated line.

-   Weekly: the section's learning summary becomes a per-project
    progress digest --- open Veridh after three weekends away and the
    top of the notebook says exactly where things stand.

**Why this matters most:** for a weekend-only builder, context recovery
is the tax on every session. This feature pays that tax automatically.
Cost: zero new calls --- activity lines are deterministic writes; the
digest reuses the existing weekly Haiku call.

**11. Coding Agent --- tiers, targets, guardrails**

Lives under the Project Agent; surfaced on the Projects page and via
Telegram. This is the only genuinely agentic component (Claude Code
loops autonomously) and the only Claude API consumer, per the existing
cost scoping.

**11.1 Two execution tiers**

  ----------------------------------------------------------------------------
  **Tier**   **Engine**       **Used for**             **Why**
  ---------- ---------------- ------------------------ -----------------------
  Small      Aider + Gemini   Bug fixes, copy changes, Already in use for
             Flash            single-file edits        Phase 0; near-free

  Feature    Claude Code      Multi-file features, new Capable autonomous
             (headless on     pages, refactors         loop; worth the API
             VPS)                                      spend
  ----------------------------------------------------------------------------

The build request carries a complexity field; the Project Agent
dispatches accordingly. Meaningful API savings for one extra field.

**11.2 Two target classes**

**Class A --- project repos** (WayClear, Veridh, Dikam, Brain app code):
staging branch → test URL → Telegram \"promote or kill?\". Worst case,
staging is broken; production untouched. Auto-staging allowed.

**Class B --- VPS infra** (Nginx, systemd, docker-compose, cron, the
running brain backend): allowed, because the Brain is itself a project
--- but the agent is modifying the system it runs on, and breaking the
brain API kills the very Telegram channel that reports the breakage.
Hence hard rules:

-   Plan-first, always: agent outputs a diff/plan → Telegram → explicit
    approval → then execute. Never auto-apply. (Projects may auto-stage;
    infra may not.)

-   Pre-change snapshot: git commit of config state (the infra-as-code
    repo from Gap 2 is the mechanism) + DB dump if schema is touched.

-   Post-change health gate: after apply, hit /health and the Nginx
    endpoints; on failure auto-rollback to the git snapshot, then alert.

-   Out-of-band alerting: the n8n health ping on the old VPS monitors
    the new one with its own Telegram bot access --- the dead-man's
    channel if the brain silences itself.

-   **Forbidden zone (human-only, permanently):** SSH config, firewall
    rules, and the systemd unit of the agent's own runner. An agent that
    can lock you out or modify its own supervisor is a bad night waiting
    to happen.

Sequencing consequence: Phase 0 (systemd, logging, health checks) is a
prerequisite for Class B capability.

**12. Revision engine**

**12.1 Scheduling (deterministic, no LLM)**

-   Spaced recall: pages resurface at 1 → 3 → 7 → 21 days
    (next_review_at, review_interval on pages).

-   Morning brief carries at most one revision item per day --- one, not
    a stack.

-   Weak-spot bias: low-scoring topics resurface more often; mastered
    ones decay toward monthly. Anki's idea without Anki --- the notes
    are the deck.

-   Skip-always: a skipped question reschedules quietly, never
    guilt-stacks.

**12.2 Question generation --- closed-book, from Ashish's own pages**

Per due page: one Flash call with a strict prompt --- \"From this note
only, generate 2 questions answerable from the text; return JSON:
question, expected_answer (quoted/derived from the note), source_line.\"
Generated once, stored in revision_questions, reused across cycles ---
not regenerated every time (cheaper, consistent).

**Why closed-book is a hard constraint:** (a) if the model invents
questions beyond the notes, the expected answer may be wrong or
unverifiable --- arguing with a hallucination; (b) it keeps the loop
honest: the system can only test what was actually captured. Thin notes
→ shallow questions → that itself is the signal: the agent flags
\"GATE/OS notes too thin for deep questions\", which is a prompt to
study, not a quiz.

**12.3 Grading --- fuzzy by design**

Telegram replies are shorthand, so grading is on meaning, not exact
words: one Flash call scores 0--10 with one-line feedback (\"missed:
thrashing trigger is high page-fault rate\"). Ambiguous grades (4--6)
show the expected answer so Ashish judges --- he is the final authority
on his own notes, not the model. Attempts land in revision_attempts and
feed the weak-spot bias and the GATE readiness trend.

> revision_questions (id, page_id, question, expected_answer,
> source_line)
>
> revision_attempts (question_id, answer_text, score, created_at)

-   Cost: \~2 Flash calls per revision item (generate once + grade per
    attempt) ≈ ₹0.02/day.

**12.4 deadline_mode --- ramping intensity**

GATE revision is steady-state; interviews, exams and client demos need a
ramp. Any notebook section can carry deadline_mode + deadline_date; the
Scheduling Agent then sets sessions_per_week = f(days_remaining),
escalating frequency as the date nears and balancing multiple deadlines
biased toward the earlier one.

**12.5 Worked example --- two job applications**

This flow was used as the generalisation test; it required no new agent.
/job capture → \"Job Search\" notebook, one section per company → JD
pasted into a page → one Flash call extracts required skills vs Ashish's
9-year security background → the gap list becomes prep topics → prep
topics enter the same spaced-recall loop (interview prep IS revision) →
the interview date becomes a fixed Scheduling constraint with
deadline_mode ramping → a watch rule (\"interview ≤5 days AND zero prep
entries this week\") guards against drift → interviewer names flow into
People → the weekly review carries a job-search section: applications
open, prep coverage %, upcoming dates.

**13. Obsidian-style features --- adopted and skipped**

**13.1 Already core**

-   \[\[wikilinks\]\], backlinks panel, force-directed graph --- and
    semantic auto-links via pgvector (cutoff 0.82, cap 5), which
    Obsidian does not have natively.

**13.2 Adopted (cheap, deterministic, no new LLM calls)**

-   Wikilinks everywhere, one graph: Notebook pages, project activity
    and People (\[\[Rahul\]\]) all use the same thought_links engine.
    Obsidian's real power is that everything is linkable --- it is not
    fenced into Thoughts.

-   Unlinked mentions: plain-text mentions of existing titles surface in
    the backlinks panel with \"link all\". String matching, no LLM.

-   Daily note: the Journal rollup is a linkable page
    (\[\[2026-07-17\]\]).

-   Inline #tags parsed and merged with ai_tags; markdown editing with
    live preview; per-page local graph (1 hop) --- more useful daily
    than the full hairball; page templates.

**13.3 Skipped, with reasons**

-   Plugin ecosystem --- the agents are the plugin system.

-   Canvas/whiteboard --- big build, low use; revisit only on felt need.

-   Sync/vault-on-disk --- the vault is Postgres; Publish --- not
    needed.

**13.4 The lock-in tradeoff, answered**

Obsidian is local-first markdown --- portable forever, offline. This
system is server-side Postgres --- smarter (semantic links, agents,
revision) but VPS-dependent. Mitigation: the portable export job
(Gap-era decision) additionally produces an Obsidian-compatible vault
--- a folder of .md files with wikilinks intact. If this system is ever
abandoned, the notes open in Obsidian on day one. Zero lock-in for
roughly a day of export-formatting work.

**14. Consolidated schema changes (all migrate via Alembic)**

  ------------------------------------------------------------------------------------
  **Table / column**                   **Purpose**                    **Introduced
                                                                      by**
  ------------------------------------ ------------------------------ ----------------
  items.corrected_category,            Correction feedback loop       Audit Gap 4
  corrected_at                                                        

  items.capture_uuid (unique)          Client-side dedup / idempotent Audit Gap 5
                                       upsert                         

  items.embedding_model                Embedding version tracking +   Audit Gap 6
                                       re-embed job                   

  capture_shortcuts (alias, category,  Slash-command routing,         Section 6
  subcategory, agent, notebook_id)     user-editable                  

  notebooks / sections / pages         Notebook structure; sections   Section 7
                                       carry type + deadline_mode;    
                                       pages carry review scheduling  

  transactions (..., is_subscription,  Finance: dedicated ledger +    Section 8
  recurrence_id)                       subscription detection         

  people.status, aliases,              Provisional → confirmed        Section 9
  mention_count                        relationship learning          

  revision_questions /                 Closed-book Q&A + scored       Section 12
  revision_attempts                    attempts                       

  agent_watch_rules.last_notified_at   24h cooldown against           v3.1 hardening,
                                       notification spam              retained

  agent_decisions /                    Carried unchanged from v3.1    v3.1
  agent_metrics_daily / thought_links                                 
  / user_profile (encrypted) /                                        
  calendar_events / health_logs /                                     
  item_people                                                         
  ------------------------------------------------------------------------------------

**15. Consolidated cost model**

  -----------------------------------------------------------------------
  **Item**                       **Cost**        **Note**
  ------------------------------ --------------- ------------------------
  Old VPS (Veridh/Dikam etc.)    \$12/mo         Already paying

  Brain VPS                      \~\$12/mo       Separate from business
                                                 VPS to avoid resource
                                                 contention

  Claude Pro (dev sessions)      \$20/mo         Already paying

  1min.ai (Flash/Haiku routing + \$0             Lifetime deal; Gemini
  specialists)                                   free tier as fallback

  Claude API (Coding Agent,      \$0--2/mo       Aider/Flash tier absorbs
  feature tier only)                             small fixes

  News Agent (Brave/Tavily)      \$3--5 / 1k     Only genuinely new
                                 queries         recurring spend;
                                                 on-demand only

  Legacy OCR (Google Cloud       \~\$1.50 / 1k   One-off per batch;
  Vision, if pilot wins)         pages           Tesseract free
                                                 alternative

  Revision engine                ≈ ₹0.02/day     2 Flash calls per item

  Slash commands                 Negative        Skips classify calls on
                                                 the highest-volume path

  Everything else (Whisper,      \$0             Self-hosted / free
  Swiss Ephemeris, Nominatim,                    
  Supabase, LangGraph, Telegram)                 
  -----------------------------------------------------------------------

**Realistic total: \~\$24--28/mo infra + small variable API.** Soft
weekly spend alert via n8n + provider usage API (Telegram ping past
\~₹500/week); a manual \"expensive mode\" flag exists for deliberate
deep-reasoning sessions. The largest real cost remains developer time
under the weekend-only rule --- mobile polish is the schedule risk, not
the agent layer.

**16. Build sequence (updated)**

  ------------------------------------------------------------------------------
  **Phase**   **Scope**                                      **Gate**
  ----------- ---------------------------------------------- -------------------
  0           systemd supervision, structured logging +      Prerequisite for
              rotation, real /health, confirmed bug fixes,   everything; also
              API key rotation, backup encryption, webhook   unlocks Class B
              shared secret, fail2ban, infra-as-code repo +  infra agent later
              RESTORE.md, Alembic                            

  1           Mobile design system extraction; confirmed     ---
              mobile bug fixes (category filter, stats       
              count, dropped category field, mic             
              reliability + 60s cap)                         

  2           Personal Agent + Capture/Scheduling agents     Core loop live
              (with restored invariants); slash commands +   
              dedup + corrections; pgvector linking;         
              agent_decisions + watch rules with cooldowns   

  3           Thoughts native (graph, backlinks, unlinked    Highest visual
              mentions)                                      impact

  4           Ask Brain dashboard (native): timeline with    ---
              protected GATE slot, interrupt banner,         
              conversational input; \"Export Brain\" button  

  5           Notebook (sections, pages, drills, learned     ---
              rollups) + revision engine + deadline_mode;    
              Finance upgrade (transactions, subscriptions,  
              calendar view); People learning; Projects      
              CRUD + auto-journal;                           
              Almanac/Astrology/News/Calendar/Health         

  6           Legacy Archive: OCR pilot (20--30 pages,       ---
              Tesseract vs Cloud Vision) before volume;      
              Obsidian-vault export format                   
  ------------------------------------------------------------------------------

**17. Context block for new sessions**

-   System: self-hosted personal OS on brain VPS; Postgres + pgvector
    (Supabase self-hosted); FastAPI; LangGraph supervisor with Postgres
    checkpointer; n8n = glue/ops only.

-   Architecture truth: LLM-decision workflows, not autonomous agents
    --- deliberate, for traceability. Claude Code inside the build
    pipeline is the one autonomous loop.

-   12 pages: Dashboard, Planner (with status chips --- Kanban removed),
    Journal, Thoughts, Notebook (ex-Workbook), Finance, Projects, Tech &
    Resources (Music merged), Health, Review, People, Legacy Archive.

-   Design invariants (Section 2.1) are permanent; the GATE lock was
    rejected in favour of escalating friction; drills clear counters
    only with verified evidence.

-   Memory layers: LangGraph typed state (working), agent_decisions
    (episodic, single-writer), pgvector (semantic, model-versioned),
    learned tables (corrections, shortcuts, watch rules, revision,
    people), requires_context preloading.

-   Model strategy: Flash/Haiku via 1min.ai for routing + specialists,
    Gemini fallback, fail-loud + queue; Claude API scoped to
    feature-tier builds only.

-   Builder: Ashish --- solo founder, day job, weekend-only venture
    rule, one protected GATE morning hour. ADHD-first design throughout.
