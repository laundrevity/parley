# parley — PROTOCOL

**Version:** 0.1 (2026-09-25) · **Status:** v0.1, in use · **Companion:** [SCHEMA.md](SCHEMA.md) (record, kinds, threads, registry)

SCHEMA.md says what a record is and what each kind obligates. This document says how the log is driven: who may speak when, how obligations play out in time, how each participant sees the log, and how repository edits become records. §8 is the block that becomes `CLAUDE.md` / `AGENTS.md` / the small model's system prompt; nothing else in this file is shown to agents. §10 is how the chair operates it: one command, `parley`.

MUST / SHOULD / MAY as in RFC 2119. A "participant" is anything that turns text into text: a human, a model, a program. "A chair" is any participant with `role: chair`; a parley has zero or more, of any kind but tool. "Agents" below means model participants, with or without tools. "The dispatcher" is the appender process (SCHEMA §1, rule 2): `parley run`.

---

## 1. Model

- **Participants** are in the registry (SCHEMA §5): humans, models and programs, any number of each; tool participants that only `say`. The dispatcher's only distinction between them is how to reach them — a model through its harness (an agentic CLI in a worktree, or a plain text API), a human through a prompt, an inbox file, or by stopping the run at their turn.
- **The log** is the single shared state (SCHEMA §1). Each agent's context — a resumed CLI session, a chat history — is a cache. Every turn begins by bringing the cache up to date with a *projection* (§5) and ends with records the appender writes.
- **A turn** is one invocation of one participant: one projection in, zero or more records out, appended contiguously with the same `seen` (SCHEMA §2.4). An agent's turn may take twenty minutes and a hundred tool calls; the protocol sees only its records.
- **A round** is one cycle of the dispatcher: compute who may speak (§3), invoke all of them that it can reach concurrently, append their turns in arrival order, run the objection pass (§3.5), repeat until nobody reachable may speak, then stop — at the humans' turn, or quiescent (§3.6). Agents never wait for the chair except at a decision; the chair gets decisions batched. A run is capped at 20 agent rounds between chair turns (`parley run --rounds N`), because two agents can nominate each other indefinitely and every round costs real invocations.
- **Time** is asymmetric by orders of magnitude: models answer in seconds or minutes, humans in hours. Every rule below is shaped by that, through the latency class, not through anyone's kind.

---

## 2. The appender

Exactly one writer. It receives each turn's records, normalizes them (SCHEMA §2.3), validates them (SCHEMA §6), and appends the turn whole — or rejects the turn whole and tells the author why (§5.4). It also stamps `seen`, which makes the log a record of what each author knew when it spoke.

The appender is `tools/parleylib/core.py` (`Parley.append_turn`); a human's own records go through it too, via `parley ask|say|propose|accept|object|decide` (§10). Chair commands and a running dispatcher may append at the same time; a file lock serializes them and every turn is validated against the log as it is at that moment.

---

## 3. Floor: who may speak

### 3.1 Permission

A participant *P* may open a turn iff at least one of:

| basis | condition |
|---|---|
| **chair** | *P* is a chair. Always. This is preemption. |
| **obligation** | *P* owes something: an unanswered `ask` addressed to it, or a `propose` it has not yet accepted or objected to (SCHEMA §3.1). |
| **nomination** | some record *R* names *P* in `next` (or nominates *P* by default, §3.3) and *P* has not taken a turn since *R*. |
| **objection** | the turn's first record is an `object`. Anyone may object to anything at any time. Every further record in that turn MUST respond to the objection: same thread, or `re` the objection or one of its targets. |
| **opening** | the log is empty. The first record may come from anyone. |
| **free floor** | the parley has no chair, nothing is owed by anyone and nobody is nominated. Where there are chairs, the free floor is theirs; where there are none, it is everyone's. |

Tool participants may `say` at any time and may not do anything else.

Permission is evaluated once per turn, at its first record. With a basis of chair, obligation or nomination the turn may contain records of any kind (`decide` remains chair-only). With objection as the only basis it is constrained as above (`F1`, `F2` in SCHEMA §6).

Permission and obligation are different things. An obligation grants the floor so that it can be discharged; a nomination grants the floor without obligating anything; a chair's `decide` closes threads without obligating anyone — if a chair wants work *owed*, the chair `ask`s. A human who is not a chair has exactly the floor a model has; a human who wants the floor whenever the parley is quiet should be a chair.

### 3.2 Precedence and the derived floor

There is no "whose turn is it" field. At any point the set of participants who may speak is derived from the log: the chairs, plus everyone with an open obligation, plus everyone with a live nomination; plus anyone, for an objection. When that set contains nobody but the chairs, **the floor has returned to them** — a derived fact, the *absence* of obligations and nominations, and it MUST NOT be encoded as a default `next` (a permanent nomination is no nomination; `transcripts/example-01.jsonl` #011–#015 is the worked case). With no chairs, that same state means the floor is free to all, and a parley in it with nothing left to say is over.

### 3.3 Nominations

- Any record may nominate any number of participants in `next`. Nominating a chair is legal and means nothing (chairs always may speak). Nominating oneself is ignored.
- A nomination is **consumed** when the nominee takes a turn that saw it (a turn whose `seen` is at or after the nominating record); a nomination that lands while the nominee is writing a concurrent turn survives that turn, so they still get a turn with it in view. All of a participant's seen nominations are consumed together. A nomination does not expire otherwise; a stale one buys at most one turn.
- An `object` with empty `next` nominates the authors of the records it objects to. The objected party gets the floor to answer without anyone having to arrange it.
- `next` is a request about *order*, not about *obligation*. "Codex next" lets codex speak; it does not make codex owe anything.

### 3.4 Preemption

A chair's records are valid at any moment and need no basis. The dispatcher folds them into the next projections. Turns already running are not cancelled, except that the dispatcher MAY cancel a turn whose thread a chair has just closed. A turn that lands in a thread closed meanwhile has its `say` records appended and its other records rejected (SCHEMA `R7`) with a `dispatch` note; the author re-emits as a new thread if still relevant. `seen` shows the author did not have the decide.

### 3.5 Unbidden objection and the objection pass

"Anyone may speak unbidden only to object" needs a mechanism, because models see records only when invoked. So after each batch of turns is appended, the dispatcher runs an **objection pass**: every model that has unseen visible records and no other basis to speak is invoked once with its delta and a standing block that says *you owe nothing; object, or emit nothing*. Cost: one short invocation per idle agent per pass. For budgeted participants the output grammar is narrowed to "an `object` or an empty record list" (§5.3).

Objections nominate their targets' authors, which feeds the next dispatch; the pass repeats until it produces no records, at most three times per round. An agent that starts a fresh session every turn (§5.5) receives the whole visible log, so in its projection the records it has not been shown before are marked `(new)` and the pass asks it to object only to those; a resumed session receives just the delta, which is all new by construction.

This is the structural half of "disagreement is mandatory": the reviewer obligation forces a position on every proposal, and the objection pass guarantees every record is *seen* by every agent while objecting is still cheap.

### 3.6 Round end

The round ends when nobody the dispatcher can reach may speak and the objection pass is quiet. Two endings: **a human's turn** — someone with `mode: exit` owes something, is nominated, or is a chair with ready threads; the run stops and prints their standing block — or **quiescent**: nothing owed, nobody nominated, no chair with a queue. A quiescent chaired parley waits for a chair; a quiescent chairless one is finished until someone opens a new thread. A human is never *invoked* unless their `mode` says how (prompt, inbox); their projection is available on demand and they may append at any time within the floor rules.

---

## 4. Obligations in play

### 4.1 Discharge

The table is SCHEMA §3.1. Operationally:

- One record discharges every obligation whose source it lists in `re` and for which it is the right kind from the right party. A reviewer facing a proposal and its two amendments writes one `accept`/`object` with three ids in `re`.
- A counter-`ask` does not discharge; it defers. The original stays open until the counter-question is answered.
- An `ask` addressed to `*` obligates every participant but the chairs and the tools. It is legal and expensive; address asks.
- A reply owed is debt, not a gate (amendment 1, §12). It does not hold a thread back from *ready*, a chair may decide over it, and it survives the decide: the addressee still owes, and answers with a `say` in the closed thread. The chair's queue lists replies still owed beside each ready thread.
- A reviewer's remark on a proposal it accepts goes in a `say` (nothing owed) or, if the reviewer wants the remark's fate on record, in an `ask` to the proposer filed after the `accept` in the same turn. The proposer owes a reply — done, or declined with a reason — never compliance; the accept stands either way. Anything approval depends on is an `object`.
- If a participant that owes something cannot discharge it this turn, its turn MUST still `re` the owed record with a `say` explaining why (blocked, needs the chair, needs more time). The obligation stays open; the dispatcher accepts the turn. A turn from an owing participant that mentions none of what it owes is treated as a rejected turn (§5.4).

### 4.2 A chair's queue and provisional proceeding

A chair owes two things and nothing else: a `decide` on every **ready** thread (SCHEMA §4.2) and a reply to every `ask` addressed to it explicitly. Both are open obligations with no deadline; both appear in the chair's standing block and in `validate.py --report`. This is how "quietly abandoning the loop" (HANDOFF §3) becomes visible. With several chairs, any one decide discharges the queue item; a model chair is invoked for its queue once per new record, and its rules (§8, chair section) tell it when to decide and what a ruling contains.

The queue is meant to be worked in one sitting. Meanwhile the others keep working: in a ready thread, participants MAY act on an *accepted* proposal (in their own worktrees, where there is a repository) before the decide (SCHEMA §4.3); contested proposals wait; nothing reaches the base branch without a `decide`. The queue shows, per ready thread, each proposal's state and objection count, and flags threads that closed or would close with zero objections (`D2`) — the descendant of HANDOFF §5.1.

### 4.3 Deadlines, timeouts, failed turns

Deadlines come from the registry's latency class and apply to the dispatcher's *wait*, never to the obligation:

| latency | dispatcher waits per turn (default) | on timeout |
|---|---|---|
| `human` | forever | — |
| `minutes` | 30 min wall clock | append `say` from `dispatch`, `re` the owed records: "codex: no turn within 30m; obligation open". Continue the round without them; re-invoke next round. |
| `seconds` | 2 min | same |

Obligations never expire. Only a `decide` voids them — the reviews still owed in the thread, together with the live nominations made by records of the thread (the decide's own `next` stands): a closed thread has nothing left to review. Replies owed to asks survive (§4.1). A nomination is an invitation, not a debt: the dispatcher invokes a nominated model once per new record, and one that answers with nothing is not asked again until the log has grown.

A turn the appender rejects (schema or protocol violation, or an owing participant that ignored what it owes) is re-run **once** with the validator's message appended to the same projection. If it fails again, the dispatcher appends a `say` naming the error codes (not the content), visible to all, and moves on; the author still owes. After two consecutive failed turns (timeouts included) a participant is not invoked again until the chair has appended something, so a broken harness cannot burn a run. Stuck obligations show in the chair's queue; the chair clears them by `decide` (void) or by `ask`ing someone else.

---

## 5. Projections

A projection is how a participant reads the log: the visible delta since its last turn, rendered into its native input, followed by a standing block that states exactly what it owes. The projection is authoritative — an agent with repo access may also read `.parley/log.jsonl` in its worktree, but that copy is as of its last rebase.

### 5.1 The delta and its rendering

**Input:** the log, the registry, the participant *P*, and `since` = the last record *P* was shown (`.parley/state.json`), which equals the `seen` of *P*'s last turn whenever that turn produced records.

**Delta:** every record visible to *P* whose position is after `since`, in file order. Exclusive of `since`. It includes *P*'s own records, marked `(self)`: the log is the truth, and the agent should see what actually landed — the ids the appender assigned, the `tmp-` ids rewritten, and nothing that was rejected.

**Rendering:** one user message. Per record, a header line then the body:

```
[#013] propose from=codex to=* re=#009,#011 thread=#007 next=claude
Visibility filter now runs before Replay; regression test with a private ask inside a public thread. …
change: parley/codex@8d0a41c (+96/−31) files: tools/project.py, tests/test_project.py
→ git diff main...parley/codex -- tools/project.py tests/test_project.py

[#015] say from=conor to=claude thread=#007 vis=claude
Before you accept 013: run it as each participant, not only as yourself. …

[#016] (self) accept from=claude to=* re=#013 thread=#007
reasons:
  - Ran project.py --for claude, --for codex and --for conor over this log …
  - `#` is consistent across headers, standing block and pointer lines …
  - Boundary test from 009 still passes …
Accepting 013 as the implementation of 004.
```

Header grammar: `[#id]` · `(self)` if `from == P` · `(new)` in a full-log projection for records *P* has not been shown before (a delta has no marker; everything in it is new) · `kind` · `from=` · `to=` (`*` or ids, always shown) · `re=#a,#b` if any · `thread=#root` · `next=ids` if any · `vis=ids` if not `*`. Timestamps are omitted (available on request); ids are always written with `#` so agents copy the form they are shown. Bodies: `text` verbatim; `reasons` as a bulleted list; `quote` as a `>` line above the reasons; `change` per §7.6.

### 5.2 The standing block

Computed from the same ledger the validator replays (`tools/validate.py --report` prints it), over the records visible to *P*, so it can never disagree with the validator. For an agent:

```
--- standing: you are claude · log through #015 ---
you owe:
  - accept|object re #013 (propose from codex, thread #007)
nominated by: #013
threads in this delta:
  #007 open — proposals: #007 contested, #009 contested, #013 pending you
  #010 closed by #014
reply: JSONL records in one ```jsonl fence, nothing else. Omit id/ts/seen.
       Every record you owe on must appear in some record's re. New thread: id and thread both "tmp-<n>".
```

For an objection pass the middle reads `you owe nothing · not nominated · you may object, or emit an empty fence.`

For a chair (delivered when the run stops, and on demand):

```
--- standing: chair · log through #016 · floor: quiet ---
decide:
  #007 ready — #013 accepted (claude); #007, #009 contested by claude
asks to you: none
stuck: none            (else: codex owes review re #… since #…, 42m)
flags: none zero-objection (#001 closed with 1 objection)
```

### 5.3 Budgeted participants and summaries

A participant with `context_budget` set gets a **summarized projection**:

1. the standing block first (small models weight the top of the prompt);
2. verbatim: every record addressed to *P* or that *P* owes on, plus the headers (and short bodies) of their `re` targets, one hop;
3. for every other thread touched by the delta: the newest **summary record** for that thread;
4. if the total still exceeds the budget: drop the oldest thread summaries first and say so in the standing block. Never cut a record mid-body; if a single owed record exceeds the budget, include its header and opening and say so.

**Summary records** are produced by the dispatcher (with whatever model it likes — a tool call, not a turn) and appended as `say` from `dispatch`, `thread` = the summarized thread, `re` = [its root], `visibility` = [*P*]. A thread is re-summarized only when it has new records. Because the summary is in the log, *P*'s `seen` points at a real record and **the log contains everything any participant was ever shown** — the projection is lossy, the log is not.

**Output constraint:** the record schema narrowed to *P* — `kind` restricted to what *P* may emit, `from` fixed, bodies for those kinds — is passed as `response_format: {"type": "json_schema", …}` to `llama-server`, which compiles it to a grammar; the objection-pass variant permits an `object` or an empty list. A small model's records are therefore valid by construction and the appender's validation is protocol-only.

**No `repo` capability** means no diffs beyond `inline_diff_lines` (0 for such participants by default); changes appear as stat lines plus the proposal's prose. Such a participant reviews designs, plans and text, and its `accept.reasons` say what text it checked against what; it is not asked to review code it cannot read (address proposals accordingly; `to` need not be `*`).

### 5.4 The turn contract

For every agent, whatever the harness:

1. **Output** is one fenced block, ```` ```jsonl ````, containing zero or more records, one per line. Prose outside the fence is ignored (CLIs narrate; the fence is what is parsed). No fence, or an empty one, means "nothing to say" and is legal only when the agent owes nothing.
2. **Per record:** `kind`, `thread`, `body`, and `re` as appropriate; `to` on every `ask`; `next` and `visibility` when wanted. `id`, `ts`, `seen` are the appender's; they are ignored if present, except provisional ids. A record without `thread` is placed in the thread of its first `re` target — or, when that thread is closed and the record is not a `say`, roots a new thread that `re`s it (the follow-up-to-a-decide case, §6) — and roots a new thread if it has no `re` at all. The appender also accepts, without comment, the shapes agents produce from the projection alone: ids written `#001`, a scalar where a list is expected, and a bare string body for the `{text}` kinds. A string body on `accept`/`object` is still rejected — the reasons list is the point.
3. **Provisional ids** `tmp-<n>` may be used in `id`, `thread` and `re` within one turn; the appender rewrites them. A new thread is `"id": "tmp-1", "thread": "tmp-1"`.
4. **Validation** is whole-turn (SCHEMA §2.3). A rejected turn is re-run once with the error; then logged (§4.3).
5. **Owed records** must each appear in the `re` of some record in the turn, if only a `say` explaining the delay (§4.1).
6. **Order within a turn.** An `accept` of a proposal precedes any `ask` or `say` about it in the same turn, so record order alone shows that the acceptance did not wait on the answer (`O4` warns otherwise; accept-vs-object #011).

### 5.5 Per harness

**Claude Code** (`harness: claude-code`). Invoked in its worktree as `claude -p --output-format json` with the projection on stdin and the participant's session resumed (`--resume <id>`; the id comes back in the JSON envelope and is kept in `.parley/state.json`), so the delta is the right increment; `session: fresh` sends the whole visible log instead. Protocol rules arrive via the generated `CLAUDE.md` in the worktree (§9). Default flags: `--permission-mode acceptEdits --allowedTools "Bash(git:*)"` — edits and git in its own worktree; anything else is denied rather than asked about, since nobody is there to answer. Widen `harness_args` per project (`"Bash(lake:*)"`, `"Bash(pytest:*)"`); the no-permission flag is the chair's explicit choice, never a default. The `result` field of the envelope is the reply; its fenced block is the turn.

**Codex** (`harness: codex-cli`). `codex exec -C <worktree> -o <file> … "<projection>"`; the last message lands in the file. Default `session: fresh` (whole visible log each turn, `(new)` marks); `session: resume-last` uses `codex exec resume --last` where the CLI supports it. Rules via the generated `AGENTS.md`. Default flags: `--sandbox workspace-write` plus `-c sandbox_workspace_write.writable_roots=["<repo>/.git"]`, because a worktree's git directory lives under the main repository's `.git` and commits would otherwise be blocked by the sandbox (`codex exec` has no `--full-auto`).

**llama.cpp** (`harness: llama-server`). One chat completion per turn against `/v1/chat/completions`: system = the §8 block in its compact form plus the participant header; user = the summarized projection (§5.3); `response_format` = the narrowed schema; low temperature; a capped `max_tokens`. There is no session — every turn is a cold join, which is what the summarized projection is for. Typical registry: `capabilities: []`, `context_budget` set, `inline_diff_lines: 0`, `latency: seconds`.

**Text-only models through the CLIs** (`harness: claude-code` or `codex-cli` with `capabilities: []` and no worktree). The same CLI, run in the parley directory with its tools off — Claude Code with `--disallowedTools "*"` and the rules in `--append-system-prompt`; Codex with `--sandbox read-only --skip-git-repo-check` and the rules leading the prompt. Model and effort as for the agentic case (`extra_args`, `effort`). This is what the `fable` and `astra` template participants are. Billing is the CLI's own login in every case: parley strips `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from each child's environment, because either CLI would otherwise bill the API instead of the subscription; there is no per-token API path in parley.

**A local model** (`harness: llama-server`): an OpenAI-compatible local server, the rules as system prompt, the output constrained by a grammar (§5.3); no login, no billing.

**Any program** (`harness: command`). The projection on stdin, records on stdout, the rules in the environment as `PARLEY_RULES` (with `PARLEY_PARTICIPANT` and `PARLEY_MODE`). Anything that reads and writes text is a participant this way; the test suite's scripted participants are this.

**Humans** (`harness: human`). Never invoked unless `mode` says how. `exit` (default): the run stops at their turn and prints their standing block; they answer with `parley ask|say|propose|accept|object|decide` and run again. `prompt`: when they are the operator (`parley run -i`), the run pauses at their turn with their projection and a prompt. `inbox`: the projection is written to `.parley/inbox/<id>.md` and the run waits for `.parley/inbox/<id>.reply.md` — a person elsewhere, or a process of their own — with no deadline (`latency: human`). The same inbox exchange is available to non-humans as `harness: inbox`.

### 5.6 Cold joins

When *P* has no resumable context — first turn, lost or compacted session, or a `seen` the dispatcher cannot reconstruct — the projection is: the standing block; then the whole visible log if it fits comfortably in *P*'s context (default: 60% of it, or `context_budget`); otherwise summary records per thread plus verbatim owed records plus the last *N* records verbatim. The standing block says it is a fresh session and names the summary records used, so the log shows the join.

---

## 6. Threads in operation

Definitions (identity, states, provisional proceeding, linearization) are SCHEMA §4. Operationally:

- **Opening.** Any participant with the floor opens a thread by emitting a record whose `thread` is its own (provisional) id. The root SHOULD `re` whatever prompted it (a decide, an FYI, a record in another thread). Root kinds are usually `ask`, `propose` or `say`.
- **Continuing.** Set `thread` to the root; `re` the records you respond to. A record belongs to one thread; its `re` may reach into others.
- **One decision per thread.** If two things need separate rulings, they are separate threads. A chair's `decide` names the proposal(s) it rules on in `re` and closes the whole thread regardless. Without a chair, a thread rests at *ready*; the report and the standing blocks say so.
- **After a decide** only `say` is valid in that thread; a reply still owed to an `ask` in it is given as a `say`. Follow-ups that need obligations (the implementation of a decided design, an objection to the decision) are new threads that `re` the decide — see `example-01` #007 → #008.
- **Views.** The full view is file order. A thread view is the file-order filter on `thread`, with one hop of context: for each `re` target outside the thread, its header and a short body. Views never interleave threads in any order but file order.
- **Concurrency.** Turns are appended in arrival order; `seen` records what each author knew; `C1` in the validator lists the concurrent pairs. Two concurrent, conflicting proposals both stand: reviewers owe on both and may object to one by reference to the other; the chair decides. A turn arriving for a thread that closed meanwhile is handled per §3.4.

---

## 7. How edits become records

### 7.1 Worktrees

The integration checkout is the base branch (`main` below). Every repo-capable participant has its own worktree and branch, `parley/<id>`, recorded in the registry; participants without the `repo` capability have neither and see changes as stat lines and prose. Agents never commit to `main` or to each other's branches. `.parley/` lives on `main`; an agent's copy is as of its last rebase, which is why the projection, not the file, is authoritative for the delta.

### 7.2 Announcing

A commit nobody has announced does not exist for the others. Announcing is a record carrying `change`:

| record | meaning | obligation |
|---|---|---|
| `say` + `change` | FYI: WIP, a spike, housekeeping, a chair's own edit on `main` | none |
| `propose` + `change` | review this and merge it | every reviewer owes accept or object |
| `decide` + `change` | landed on `main` (merge, or revert) | none; closes the thread |

Each `propose` names exactly one commit as the review target. Amendments are new commits on the same branch announced by a new `propose` with `re` = [previous propose, the objections it answers]; a reviewer's one record discharges all of them.

### 7.3 Reviewing

Worktrees share one object store, so nothing is fetched: `git diff main...parley/codex -- <files>`, `git show <commit>`, `git log main..parley/codex`. The projection inlines the diff when it is at or under the reviewer's `inline_diff_lines`; otherwise it prints the stat and the command (§7.6). Reviewers with `shell` SHOULD run what they review; `accept.reasons` are what was run and what it showed.

### 7.4 Merging

A chair fast-forwards or merges into `main`, squashing if it wants, and the `decide`'s `change` names whatever commit landed. Rejection is a `decide` without `change` (or with the revert). Generated documents (§9) are regenerated on any merge that touches PROTOCOL.md.

### 7.5 Rebase

Before invoking a repo-capable agent, the dispatcher rebases its branch onto `main` in its worktree (`git rebase --autostash`, so uncommitted work survives; skipped when the branch already sits on the base). Clean: nothing is logged. Conflict: the rebase is aborted and a `dispatch` `say`, visible to that agent and the chair, names the files; it is in the agent's next projection and resolving it is the first thing the agent does in that turn (`parley run --no-rebase` turns this off).

### 7.6 Rendering a change

At or under the participant's `inline_diff_lines`, the stat line is followed by the diff in a ```` ```diff ```` fence:

````
change: parley/codex@8d0a41c (+64/−9) files: tools/project.py, tests/test_project.py
```diff
--- a/tools/project.py
+++ b/tools/project.py
@@ … the whole unified diff …
```
````

Over the cap, the stat line is followed by the command instead:

````
change: parley/codex@8d0a41c (+412/−37) files: tools/project.py, tools/README.md, tests/test_project.py
→ git diff main...parley/codex -- tools/project.py tools/README.md tests/test_project.py
````

The stat is computed by the projector when the commit is reachable. For `diff`-only changes (no commit), inline up to the cap and then `(truncated: N more lines)` — truncation is always named, never silent.

### 7.7 The protocol is not exempt

Changes to SCHEMA.md, PROTOCOL.md, the schemas or the tools are proposals like any other change, from a chair too. `example-01` #011–#015 is the chair shipping a schema change as an FYI and being objected to unbidden.

### 7.8 Hygiene

Commit often; uncommitted work in a worktree is invisible to everyone and the dispatcher does not snapshot it. Keep `main` the chairs' alone.

---

## 8. Rules for participants

This block, between the markers, is what every non-human participant reads, and what a human may read too. Two sections are conditional: `repo` appears only for participants with the `repo` capability, `chair` only for chairs (`gendocs.render_rules` selects them). It is copied verbatim into `CLAUDE.md` (Claude Code), `AGENTS.md` (Codex) and the small model's system prompt by the generator (§9), with a per-participant header prepended. Edit it here only.

<!-- parley:agent-rules:begin -->
## parley: rules for participants

You are one participant in a **parley**: several parties — humans, models, programs — talking through one shared log. The conversation is the file `.parley/log.jsonl`, one JSON record per line (format in `SCHEMA.md`). **The log is the truth; whatever you remember is a cache of it.** Each turn you receive the records appended since your last turn (your own marked `(self)`), then a *standing* block that states exactly what you owe. The projection you are given is authoritative.

### When to speak
- Speak only if the standing block says you **owe** something, you are **nominated**, or you are **objecting**. Otherwise emit an empty fence.
- You may object to any record, by anyone including a chair, at any time, without being asked. This is the one thing you never need permission for. An unbidden turn contains the objection and only records that respond to it.

### How to speak
- Your reply is JSONL records inside one ```` ```jsonl ```` fence — one JSON object per line, `\n` escapes inside strings (a body may itself contain code fences; that is fine). Nothing outside the fence is read.
- Set `kind`, `thread`, `body`, and `re` (the ids you are responding to). Set `to` on every `ask`. Set `next` only when a specific participant should go next. Omit `id`, `ts`, `seen`.
- In prose, refer to records as `#012`; in JSON fields write the bare id, `"012"`. Put every record you are responding to in `re`; one record can discharge several obligations.
- To start a new thread — a new topic, or disagreement with a decision — use `"id": "tmp-1", "thread": "tmp-1"` and `re` what prompted it. Otherwise use the thread you are responding in.

### Record shapes (complete; you may not be able to read SCHEMA.md)
```jsonl
{"kind": "say",     "thread": "001", "re": ["001"], "body": {"text": "…"}}
{"kind": "ask",     "thread": "001", "re": ["004"], "to": ["codex"], "body": {"text": "…?"}}
{"kind": "propose", "thread": "001", "re": ["001"], "body": {"text": "…"}}
{"kind": "accept",  "thread": "001", "re": ["002"], "body": {"reasons": ["what I checked and why it holds", "…"]}}
{"kind": "say",     "thread": "001", "re": ["002"], "body": {"text": "a remark on #002 that asks nothing (after the accept)"}}
{"kind": "ask",     "thread": "001", "re": ["002"], "to": ["claude"], "body": {"text": "a request about #002 whose answer I want on record (after the accept; you owe a reply, not compliance)"}}
{"kind": "object",  "thread": "001", "re": ["002"], "body": {"quote": "the disputed words, verbatim", "reasons": ["one specific defect or risk", "…"], "text": "optional; a better alternative goes in a following propose"}}
{"kind": "propose", "id": "tmp-1", "thread": "tmp-1", "re": ["006"], "body": {"text": "a new thread"}}
```
`body` is always an object. `reasons` is required on accept and object and is a list of strings; an accept body has nothing else. `to`, `re`, `next` are lists of bare ids. Only chairs emit `decide`.

### Kinds
| kind | use it for | it creates |
|---|---|---|
| `say` | a statement, report, answer, or an FYI nobody needs to act on | nothing |
| `ask` | a question, or a request for work, `to` named participants | each addressee owes you a reply |
| `propose` | something for the others to accept or object to: a design, a plan, a claim, a change | every other non-chair participant owes accept or object |
| `accept` | `reasons`: what you checked and why it holds. "Looks good" is not a reason. Reasons only: a remark goes in a `say` after it, a request in an `ask` after it, and if you have a condition you are objecting. For a change, one reason must state that the diff touches only what the decision allowed. | — |
| `object` | `reasons`: specific defects or risks, one per entry; `quote` the disputed text verbatim. Follow with a `propose` if you have a better alternative. | the author you objected to gets the floor |
| `decide` | chairs only. Closes the thread with a ruling and its rationale. | — |

### Obligations
- An `ask` to you is discharged by any non-ask record from you that `re`s it. A `propose` addressed to you is discharged by your `accept` or `object` that `re`s it. Nothing else discharges anything; silence is visible in the log.
- If you cannot discharge something this turn, `say` so with the owed id in `re` — it stays owed, but the log shows why.
- Once everyone has accepted a proposal you may act on it before a chair decides. After a `decide`, only `say` in that thread — including the reply to an `ask` you still owe there; disagree by opening a new thread that `re`s the decide.
- An `ask` you receive after an `accept` of your proposal is a request, not a condition: reply done, or declined with a reason. The accept stands either way.

<!-- parley:section chair -->
### As a chair
- You may `decide` a thread: `{"kind": "decide", "thread": "001", "re": ["002"], "body": {"text": "the ruling and why"}}`. It closes the thread and voids the reviews still owed in it; replies owed to asks survive and are given as `say`. Decide when the thread is *ready* (every reviewer has accepted or objected; open asks do not hold it back), or earlier when waiting is worse than deciding; say which.
- A ruling names what it rules on (`re`), states the outcome plainly, and gives the reason. Where proposals were contested, say why the objection did or did not carry.
- Your `ask` obligates; your `next` only invites. Nominating someone does not make them owe anything.
<!-- /parley:section -->
<!-- parley:section repo -->
### Repository
- Work only in your own worktree and branch (`parley/<your id>`). Never commit to `main` or to another participant's branch.
- A commit nobody has announced does not exist. Announce with `change: {branch, commit, files}` on a `say` (FYI, no review) or a `propose` (review and merge): `{"kind": "propose", "thread": "007", "re": ["006"], "body": {"text": "Implements #004 …", "change": {"branch": "parley/codex", "commit": "3f9c2ab", "files": ["a.lean", "b.lean"]}}}`.
- To read a change under review: `git diff main...parley/<author> -- <files>`. Run what you review; your reasons are what you ran and what it showed, and whether the diff stays inside the decided scope.
<!-- /parley:section -->
### Conduct
- Do not characterize another participant's position without quoting it.
- State uncertainty as uncertainty. Praise is not a contribution; build on it or dispute it.
- Do not restate the log; respond to it. Write for a reader who will see your record once, cold.
<!-- parley:agent-rules:end -->

---

## 9. Generated documents

`CLAUDE.md`, `AGENTS.md` and the llama system prompt are generated from §8 and never hand-maintained. The generator, `tools/gen-agent-docs.py` (run by `parley init` and `parley gen-docs`):

1. extracts the text between `<!-- parley:agent-rules:begin -->` and `<!-- parley:agent-rules:end -->`;
2. prepends a participant header — id, model, chair and base branch, own branch and worktree, the log path, the absolute path of this spec — and, when the base branch is not `main`, substitutes the actual base branch into the rules;
3. writes it into a marker-delimited region of `CLAUDE.md` (for `claude-code`) or `AGENTS.md` (for `codex-cli`) in that participant's worktree (`--out DIR` to redirect, `--stdout` to inspect), creating the file if absent and preserving anything outside the markers, under a banner `GENERATED from PROTOCOL.md §8 — edit there`; the file is then hidden from git in that worktree (`--skip-worktree` if the repository tracks it, the worktree's `info/exclude` otherwise), so the per-participant block never lands in a commit and never blocks a rebase; for `llama-server` it writes `.parley/system-<id>.txt`;
4. is re-run after any change to PROTOCOL.md (`parley gen-docs`).

Hand edits inside the markers are overwritten without notice; that is the point.

---

## 10. Operating it

Everything a human does is one command, `parley` (`cd ~/git/parley && uv tool install --editable .`; or `uv run parley` from the checkout, or `python3 tools/parley`). It works on any repository or directory; the spec and the tools stay in the parley repo, which is why the install is editable.

Two shapes of parley:

```sh
# text only — a conversation, no repository, no tools: Fable through Claude Code, Astra through Codex, you as chair
parley init --dir ~/talks/accept-vs-object            # Fable through Claude Code, Astra through Codex, under their logins
cd ~/talks/accept-vs-object && parley doctor

# in a repository — agentic CLIs in worktrees, edits as records
parley init --repo ~/research/anabelian               # .parley/, worktrees ../anabelian-claude ../anabelian-codex, CLAUDE.md / AGENTS.md
cd ~/research/anabelian && parley doctor              # binaries on PATH, worktrees present, docs generated
```

`--agents` picks participants from the template registry (`fable`, `astra` text-only through the CLIs; `claude`, `codex` agentic in worktrees; `llama` local); `--chair a,b` names the chairs, `--chair none` makes a chairless parley. Any mix works in one parley: a text-only critic next to a coding agent, two humans, a model as chair. Who you are is the only human in the registry, or `--as <id>` (or `$PARLEY_AS`) when there are several.

The loop, from inside the directory (or with `--repo`/`--parley`):

```sh
parley ask fable,astra "<the question or task>"     # your record; both now owe a reply
parley run                                          # rounds until it is a human's turn, or quiet; prints the block and stops
parley decide 001 "<ruling>"                        # or ask / object / say / propose / accept — see parley -h
parley run                                          # … and so on.   parley run -i keeps a prompt open instead.
```

What `parley run` does each round (§1, §3): derives from the log who may speak; invokes everyone it can reach concurrently — a CLI in its worktree (rebased onto the base branch first), an API with the rules as system prompt, an inbox file for a person elsewhere; validates and appends each turn whole; runs the objection pass; stops when only humans in `exit` mode may speak, or when nobody may. It never asks you anything mid-round — timeouts, rejected turns and rebase conflicts become `dispatch` records and appear in your block as *stuck*. `parley status` prints your block at any time (`--for <id>` for anyone's); `parley validate` the full report; `parley project --for astra` exactly what astra would be sent; `parley run --dry-run` every projection that would go out, sending nothing.

Model and effort are registry knobs, one command each: `parley set fable effort max`; `parley set fable extra_args '["--model","fable"]'`, `parley set astra extra_args '["-m","gpt-6-astra"]'`. `extra_args` is appended after the sandbox defaults and leaves permissions alone. `parley doctor` prints the exact command line each participant will get, and notes when an API key in your shell is being kept away from the CLIs.

Permissions are the humans'. The defaults (§5.5) keep each CLI's own sandbox on, so a first run in a new project will typically show an agent saying it could not run the tests; widen that participant's `harness_args` (`parley set claude harness_args '["--permission-mode","acceptEdits","--allowedTools","Bash(git:*)","Bash(lake:*)"]'`) and run again — `harness_args` replaces the defaults, so include the ones you want to keep. Build caches do not come with a worktree: `parley init --share .lake/packages --clone .lake/build` links the shared part into every worktree and clones the per-tree part (copy-on-write where the filesystem allows); it is idempotent.

Cost: at most one invocation per participant per round, plus one short invocation per idle model per objection pass; a run stops after 20 rounds without a human turn (`--rounds`).

Kept outside the log, git-ignored by `parley init`: `.parley/state.json` (what each participant was last shown; CLI session ids), `.parley/inbox/` and `.parley/dispatch.log` (every raw reply and the exact command that produced it). A false start is undone with `parley reset --yes --keep N`. Ctrl+C during a run stops the turns in flight without recording anything against them.

Tests: `tests/test_e2e.sh` drives the whole loop with scripted participants (`tests/fake_agent.py`); `tests/test_roles.sh` covers text-only, chairless and model-chaired parleys and `--as`; `tests/test_adapters.py` covers the adapters' command lines, environment scrubbing and parsing; `tests/test_protocol.py` the rules the schema cannot express, amendments (§12) included.

---

## 11. Open questions for transcripts to settle

- ~~Whether forcing accept-or-object is right, or whether reviewers need a conditional accept.~~ Settled by amendment 1 (§12): the binary stands; what needed a home was the non-blocking remark, and it got `say` / `ask` after the accept.
- Whether a chair-designated class of proposals should auto-close on unanimous accept. Current answer: no; provisional proceeding covers the latency without giving up "only the chair decides".
- Whether `propose` should default `to` a per-participant reviewer set rather than `*`, so a small model is not a reviewer of every diff. Current answer: authors address; `*` is the default only because it is the safe one.
- The cost of the objection pass (one invocation per idle agent per pass) against the alternative of batching objections into the agent's next owed turn, which delays them. Measure it.
- Whether `seen` needs to be a frontier (a list) rather than one id. Not until there is more than one log.

---

## 12. Amendments

The protocol changes by its own means: a thread, objections, a `decide`. Each amendment is listed with the instant it took effect — the `ts` of the decide — because **a record is checked by the shape in force when it was appended**: the validator applies a retired shape to records stamped before the amendment and notes them (`A1`), so no log is rewritten and no old log turns invalid.

| # | effective (UTC) | decided in | change |
|---|---|---|---|
| 1 | `2026-09-26T04:31:42Z` | `~/talks/accept-vs-object` #012, on #008 with #011 (fable, astra; conor chair) | `accept.body` is `{reasons}` only. A reviewer's remark that asks nothing is a `say`; a remark whose fate they want on record is an `ask` to the proposer after the accept — reply debt, not compliance; a condition is an `object`. A reply owed never holds a thread back from *ready* and survives a `decide`, answered by `say` in the closed thread. In one turn the accept precedes the ask or say about the same proposal (`O4`). |
