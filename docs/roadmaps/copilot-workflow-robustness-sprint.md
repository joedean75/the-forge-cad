# Copilot workflow and robustness sprint

Prepared 2026-09-14. Status: **planned; implementation has not started.**

Goal: an assembly request should make measurable progress, stop with an accurate
reason, and continue from saved work without repeating geometry mutations.

## Evidence and limits

Reviewed `forge-dev.log`, `backend-dev.log`,
`/tmp/forge-backend-copilot-context.log`, persisted Copilot trajectories in
`backend/data/forge.db` using a read-only SQLite connection, and the current
planner, operation registry, job API, and browser conversation code.

The frozen database snapshot at **2026-09-14 10:13 UTC** contains 59 Copilot
trajectories from July 30 through September 14, including nested part runs and
apparent test/probe prompts. They are not 59 independent customer tasks. The
earlier pass contained 54 records; use the frozen figures below for this plan.
Some runs predate the recent context improvements. No model identifier was
persisted on any of these trajectory records, so model comparisons are not
possible from this corpus.

| Recorded result | Count |
|---|---:|
| `done` | 29 |
| `planner_error` | 17 |
| `budget` | 9 |
| `verification_failed` | 3 |
| `repeated_failure` | 1 |
| Recorded operation steps | 234 |
| Failed operation steps | 53 |
| Failures naming nonexistent catalog keys | 36 |
| `done` records with no recorded operations | 12 |

These are recorded outcomes, not a verified task-success rate. A zero-operation
answer may legitimately answer a question, while missing parent trace entries
can conceal real work. Planner errors also are not operation errors, so the
53-step count excludes them.

### Findings that determine priority

1. **The latest attempts hit provider failures.** The runtime log contains six
   `403 Forbidden` planner failures (lines 26, 31, 71, 76, 81, 86) and four
   `402 Payment Required` planner failures (438, 440, 512, 514). Two nearby
   `LLM edit request failed: LLM returned empty content` entries occur at 437
   and 508. The associated fuselage child trajectories
   `1cf8ef02-d082-4095-9886-bc275191e506` and
   `9a13056e-60a4-47f8-b0f4-2e4f39566f80` retain only an `ai_edit` failure saying
   “AI edit did not produce valid geometry.” The API accepted and polled jobs
   with HTTP 200 while the planner failed. That is valid asynchronous transport
   behavior, but it cannot serve as the success metric. The logs do not retain
   the provider response needed to establish the exact account/model restriction
   or billing condition; 402 should direct the user to check provider billing.

2. **Assembly catalog discovery is inaccessible.**
   [`list_standard_parts`](../../backend/app/services/cad_operations.py#L2667)
   is registered for documents. Assembly `insert_standard_part` tells the model
   to call it, but assembly tool filtering and dispatch reject document tools.
   This is a current defect, reproduced by inspecting the loaded registry.
   Trajectory `4a858ca0-e16c-43d9-b38e-0313ce345e20` spent 19 of 24 recorded
   actions trying nonexistent wheel keys. Another used 14 `list_components`
   calls in 24 steps. Increasing the budget does not remove this cause.

3. **An empty reply can become success.** In
   [`_plan_next`](../../backend/app/services/copilot.py#L195), no tool call plus
   empty content becomes `{done: true, summary: "done"}`. A mocked empty provider
   response reproduced this on the current code. `_post_chat` discards response
   finish metadata, usage, and request correlation. A clarification such as
   “What would you like me to build?” also shares `done` with a completed build.

4. **Nested traces lose their parent.**
   [`trajectories.start/finish`](../../backend/app/services/trajectories.py#L52)
   replace the active ContextVar and then clear it, rather than restoring the
   enclosing trajectory. An isolated in-memory recorder reproduced this.
   Trajectory `3f7a1e64-5543-452f-b24f-69a1361d2dc8` says “Ran 48 operation(s)”
   but stores zero steps. This defect explains a mechanism for missing steps;
   it does not prove the exact number lost in each historical run.

5. **The repeated-failure guard is easy to bypass unintentionally.**
   [`copilot.run`](../../backend/app/services/copilot.py#L488) compares exact
   operation/argument strings and clears failure counts after any successful
   query. Trying another fabricated key or interleaving `list_components`
   keeps consuming the budget. One eight-step part run queried `bounding_box`
   six times. There is no guard on repeated successful reads with unchanged
   state.

6. **Browser reload recovery is only partial.** Job state remains in
   [`_COPILOT_JOBS`](../../backend/app/routers/operations.py#L296); restart loses
   the job, and `prune_jobs` can remove running entries. Conversations and plans
   persist in browser localStorage, but reference image pixels are deliberately
   excluded. [`waitForCopilot`](../../frontend/src/api/client.ts#L448) abandons
   polling on a single transport error. No idempotent submission key resolves
   “request accepted, response lost.” These are current code risks, not measured
   frequencies in the logs.

7. **Stale plans can still mutate changed geometry.** The operation API already
   supports `expected_revision` for documents, but the Copilot dispatch does not
   pass it. Assemblies lack their own revision counter. A second tab, manual edit,
   or another run can change the subject while the planner is waiting.

Already present and to preserve: scoped selectable conversations, bounded text
history, saved work plans, selected topology/components, viewport references,
explicit units, richer assembly observations, nested part briefs, and focused
tool discovery. Clearance warnings are already distinguished from collisions in
the current completion check; the historical false collision result is a
regression fixture, not another implementation task.

## Sprint scope and capacity

Proposed duration: **10 working days with two engineers**. Budget 15 engineer-days
for the seven tickets below and five for integration, review, and unexpected
failures. For one engineer, allow roughly three weeks plus integration rather
than compressing this into ten days. Estimates are planning assumptions.

| Ticket | Priority | Deliverable | Estimate | Depends on |
|---|---|---|---:|---|
| CP-01 | P0 | Usable assembly catalog discovery | 2 days | — |
| CP-02 | P0 | Typed provider failures and honest completion | 2 days | — |
| CP-03 | P0 | Complete, correlated parent/child traces | 2 days | — |
| CP-04 | P1 | Progress detection and targeted recovery | 2 days | CP-01, CP-02 |
| CP-05 | P0 | Durable jobs and safe mutation recovery | 3 days | CP-03 |
| CP-06 | P1 | Continue/reconnect workflow and context continuity | 2 days | CP-02, CP-05 |
| CP-07 | P0 | Regression scenarios and release evidence | 2 days | All; fixtures start day 1 |

### CP-01 — Make catalog lookup possible from assemblies

Add an assembly-scoped catalog query backed by the existing listing service.
Keep document lookup compatibility; do not globally loosen subject validation.
Include the query in assembly tool schemas and update insertion descriptions
and error remedies to name the callable tool. Return bounded, searchable,
paginated results with exact keys, dimensions, parameter schemas, and fidelity.
On an unknown key, provide a concrete lookup action and relevant suggestions.
Do not silently replace the requested part with an unrelated catalog item.

Acceptance: from an empty assembly, discover an available motor/servo/fastener
and insert its returned key; nonexistent wheels lead to lookup and an explicit
unavailable-part decision, not successive guessed keys. Account-private imported
parts remain scoped correctly, including shared document callers. Regression
tests cover both document and assembly entry points and pagination.

### CP-02 — Separate provider, protocol, and geometry failures

Preserve provider status, sanitized error code/message, request ID when supplied,
finish reason, model, latency, and usage when available. Never record API keys or
authorization headers. Map access, billing, throttling, timeout, malformed output,
empty output, refusal, and geometry evaluation into distinct actionable results.
Carry that information through `ai_edit` and child runs instead of returning
`None` and relabeling every failure as invalid geometry.

An empty reply must not complete a build. Distinguish `needs_input`,
`answered`, `completed`, `paused`, and `failed` at the task-result layer while
keeping execution/job status separate. Build completion needs a nonempty result
and checks appropriate to declared requirements. A status question may finish
without clearing the unfinished design plan. Handle multiple returned tool calls
explicitly: either request a single decision or implement sequential validated
execution; do not silently discard all but the first call.

Use bounded retries only for transient provider/transport failures, respecting
the remaining deadline and cancellation. Do not automatically retry 401/402/403
or replay a mutation. Permit one protocol-repair attempt for empty/malformed
output if the deadline allows. Retrying must not silently change the model.

Acceptance: fixtures for 402/403 show billing/access guidance; 429/timeout follow
bounded retry behavior; empty/truncated/malformed/refusal responses cannot yield
completed geometry; a nested `ai_edit` preserves its actual failure category;
no subsequent mutation occurs after cancellation or deadline expiry.

### CP-03 — Make every run reconstructable

Restore the parent trajectory with ContextVar tokens in `finally`, including
exception and cancellation paths. Persist conversation/turn/job/trajectory IDs,
parent/child IDs, subject kind, model, prompt/schema version, plan checkpoints,
stop reason, provider events, and operation events with sequence numbers.
Return correlation IDs to the UI and support an owner-scoped diagnostics export.
Keep telemetry failure from blocking CAD work, but count and report dropped
events. Append bounded events rather than repeatedly embedding whole geometry.
Store sizes/fingerprints and asset IDs for context; avoid raw image payloads in
logs. Apply explicit retention and redaction to diagnostic exports.

Acceptance: two nested part builds preserve all parent actions before, between,
and after children; exceptions close the correct trace and restore its parent;
two concurrent runs never share events; a provider error has a durable cause,
not just `planner_error`; diagnostic export cannot expose another owner's run.

### CP-04 — Recover from failure families and stop unproductive loops

Track progress by subject revision/fingerprint and verified plan milestones.
Maintain a short window of error families, not just exact JSON arguments.
Successful read-only calls must not erase mutation failure history. After two
unknown catalog keys, require discovery or report the missing item. After a
stale face/feature error, re-query topology before another edit. Repeated reads
of an unchanged subject must trigger replanning or a resumable pause rather
than consuming the remaining budget. Cache identical read results only while
the relevant state fingerprint is unchanged.

Use the existing plan and remaining budget to reserve verification/handoff;
record whether a child produced a partial part so it can be continued by ID.
Separate planner-call, mutation, and elapsed-time counts in the UI. Keep a hard
overall limit; do not solve the problem by increasing default steps.

Acceptance: the guessed-wheel sequence is redirected by its third attempt;
interleaving reads cannot defeat the guard; repeated bounding-box calls at the
same revision pause/replan after a bounded threshold; a real geometry change
permits a fresh query; pauses retain unmet requirements and existing part IDs.

### CP-05 — Durable jobs without duplicate CAD edits

Adapt the existing `services/jobs.py` infrastructure for Copilot rather than
creating another queue. Persist accepted runs and checkpoints before responding.
Add an owner/subject-scoped client submission ID so a lost POST response can be
retrieved safely. Keep active records out of retention pruning and preserve
subject access checks for polling, cancellation, and recovery.

**Do not adopt the generic job runner's whole-job automatic retry for mutating
Copilot runs.** Journal operation intent, result, and observed revisions. Pass
document `expected_revision` on planner mutations. Add an assembly revision or
equivalent checked fingerprint under its write lock, and prevent concurrent
Copilot writers to the same subject. Do not hold a write lock across an LLM call.
On process loss, reconcile committed actions; an ambiguous in-flight mutation
must pause for state reconciliation instead of executing it again. The bounded
sprint deliverable is safe interrupted-state recovery, not automatic resumption
of every native-kernel crash.

Acceptance: restart after insertion preserves the run and does not insert the
part again; lost submission response resolves to the same job; more than 200
submissions cannot evict an active job; manual edits during planning cause a
conflict/replan before mutation; cancellation survives restart. A failure while
building a child leaves an explicit recoverable partial part or cleans up the
empty child, with no unexplained orphan documents.

### CP-06 — Let the user continue the actual task

Back conversation/turn/plan state with server persistence using the existing
account and subject scope. Import browser-only transcripts without duplicating
turns; maintain the selectable chat UI. Offer **Continue**, **Reconnect**,
**Stop**, and a specific blocked-state action (for example, check provider
billing). Show the active phase, last progress, checks, and remaining work.
A polling failure is a connection state, not proof that the backend stopped.
Reuse the existing polling utility with typed transient/permanent error handling.

Keep original requirements, decisions, unresolved work, and current component IDs
when compacting context. Give each persisted reference image an authorized asset
ID and explicit retention, so continuation can resend it. If unavailable, say
which reference needs reattachment instead of implying the model still sees it.
Bound context before submission and report omissions; retain recent exact action
results needed for continuation. A new unrelated goal in the same chat must
explicitly reconcile the prior plan instead of silently inheriting its objective.

Acceptance: reload during a run reconnects; one transient failed poll recovers;
refresh after a budget stop and Continue retain requirements, selected subject,
plan, and available images; switching assembly/chat never imports another scope's
context; the “body and steering first” request preserves that priority.

### CP-07 — Ship with evidence, not only unit coverage

Build deterministic planner/provider fixtures around the observed failure shapes.
Use isolated databases and small real CAD fixtures; no default calls to a paid
provider. Do not copy raw customer images or credentials into tests. Cover:

- Assembly catalog search, unavailable wheel keys, and parameterized insertion.
- A two-plate stand: mating contact passes; actual penetration fails with names
  and depth. Preserve the already-fixed clearance behavior.
- Hollow-shell editing with fresh/stale face IDs and recoverable tool errors.
- Multi-stage RC assembly continuation, including an unfinished child part.
- Empty output, 402/403, transient failure, multiple tool calls, and cancellation.
- Parent/child trace integrity, lost POST responses, interrupted mutations,
  manual edits during planning, two tabs, and page reload.

Run small real geometry checks alongside mocked provider decisions. Track task
completion against explicit fixture requirements, not only returned `done`.
Publish a reproducible result table with model/fixture/version fields, planner
calls, mutations, repeated queries, categorized failures, and context bytes.
Token/cost fields are unknown when the provider does not return them. Any later
live-model comparison needs a fixed model and separately established usage
budget; this planning task makes no paid model calls.

## Delivery order and exit criteria

Days 1–2: fix catalog access and response handling; reproduce all three confirmed
code defects as tests; start trace correlation. Days 3–5: finish trace integrity,
progress guards, and durable-job foundations. Days 6–8: integrate continuation,
conflict handling, and browser recovery. Days 9–10: fault injection, targeted
geometry/browser verification, review, and release evidence.

The sprint is done when all acceptance scenarios pass, an empty reply cannot
claim a completed build, catalog errors stop guessing, provider failures name
the recovery action, and interruption/continuation tests produce **zero duplicate
mutations**. Every test run must have linked parent/child traces and a durable
terminal or explicitly paused outcome. Do not set a production completion-rate
target from this mixed, incomplete historical corpus; establish the controlled
baseline with CP-07 first.

If capacity slips, defer multi-device image/conversation migration and the richer
context preview in CP-06. Keep durable same-browser continuation, typed failures,
catalog discovery, trace integrity, and duplicate-mutation protection in scope.
Defer new CAD feature families, fine-tuning, model replacement, automatic provider
switching, and distributed multi-worker rollout to later work.

## Review performed for this plan

Read-only log/database analysis plus three isolated diagnostic checks confirmed:
assembly catalog lookup is absent from its tool set; mocked empty output returns
`done`; finishing a child trajectory does not restore its parent. The diagnostics
used mocked provider calls and an in-memory recorder. No CAD data was changed,
no backend restart was performed, and no sprint implementation is included here.
