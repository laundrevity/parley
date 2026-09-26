# parley — SCHEMA

**Version:** 0.1 (2026-09-25) · **Status:** v0.1, in use · **Companion:** [PROTOCOL.md](PROTOCOL.md) (turn-taking, projections, edits)

This document defines the log format: what a record is, what each kind obligates, what a thread is, and who the participants are. Participants are anything that turns text into text — humans, models, programs — and the schema treats them alike; *chair* is a role some of them hold, not a kind. PROTOCOL.md defines how the log is driven. `schema/record.schema.json` and `schema/participants.schema.json` are the canonical machine-readable shapes; `tools/validate.py` is the reference check for everything the schemas cannot say. If this text and the schema files disagree, the files win and the text is a bug.

MUST / SHOULD / MAY are used in the RFC 2119 sense.

---

## 1. Files

A **parley** is a directory containing two files. Convention: `.parley/` at the repository root, on `main`, committed by the chair.

| file | contents |
|---|---|
| `participants.json` | the registry (§5): who is in the parley, what they can do, how slow they are |
| `log.jsonl` | the log: one record per line, UTF-8, append-only |

Rules:

1. **The log is the truth.** Every participant's context is a cache of it. Nothing that is not in the log happened, as far as the other participants are concerned.
2. **Single appender.** Exactly one code path writes `log.jsonl`: the appender (`tools/parleylib/core.py`), used by the dispatcher for agents' turns and by the `parley` command for the chair's. Agents never write it. They emit records (PROTOCOL §5.4); the appender validates, normalizes (§2.3) and appends.
3. **File order is the canonical total order.** `ts` is informational. Threading (`re`, `thread`) and causality (`seen`) are layered on top of file order; they never reorder it.
4. **Append-only.** No record is edited or deleted. A correction is a new record.
5. **A turn is appended whole.** All records of one turn (§2.4) are contiguous in the file.

---

## 2. The record

One JSON object per line. Canonical shape: `schema/record.schema.json` (reproduced in §7).

### 2.1 Fields

| field | type | required | meaning |
|---|---|---|---|
| `id` | string `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` | yes | Unique within the log. Assigned by the appender. Convention: decimal sequence numbers, zero-padded to a fixed width (`001`, `002`, …). Agents may emit provisional ids matching `^tmp-`; the appender rewrites them (§2.3). |
| `ts` | RFC 3339, UTC | yes | When the record was appended. Informational. |
| `from` | participant id | yes | Author. Must be in the registry. |
| `to` | `["*"]` or `[participant id, …]` | default `["*"]` | Addressees. `*` means every participant other than the author. Never includes the author. Addressing is what creates obligations for `ask` and `propose` (§3); for other kinds it is informational. `ask` MUST set `to` explicitly. |
| `re` | `[record id, …]` | default `[]` | The records this one responds to. Each MUST be earlier in the log. May cross threads. Listing several targets is how one record discharges several obligations. |
| `thread` | record id | yes | The id of the thread's root record. A root has `thread == id`. |
| `kind` | `say` `ask` `propose` `accept` `object` `decide` | yes | The speech act. Fixes the body type (§3.2) and the obligations created or discharged (§3.1). |
| `body` | object | yes | Typed by kind (§3.2). |
| `next` | `[participant id, …]` | default `[]` | Nominations: who the author wants to speak next. A nomination is consumed when the nominee takes a turn that saw it (PROTOCOL §3). Empty means no nomination; who holds the floor is then derived (PROTOCOL §3). `next` MUST NOT default to the chair — a permanent nomination is no nomination. |
| `seen` | record id or `null` | stamped by appender | The last record in the author's projection when the turn began; `null` only for a first turn on an empty log. This is the author's causal frontier: it defines turns (§2.4) and concurrency (§4.4). |
| `visibility` | `["*"]` or `[participant id, …]` | default `["*"]` | Who may see the record. Chairs are always viewers, whether or not listed. Every addressee and every nominee MUST be a viewer. A record a participant cannot see is absent from that participant's projections and can never obligate it. |

Unknown fields are rejected (`additionalProperties: false`). Extending the record shape is a change to this document, and goes through `propose` like any other change.

### 2.2 Defaults

Defaults are applied by readers, not written by the appender. A record without `to` means `["*"]`; without `re` means `[]`; and so on. Hand-authored logs stay short.

### 2.3 Normalization by the appender

On appending a turn, the appender:

1. assigns `id` (next in sequence) and `ts`;
2. sets `seen` on every record of the turn to the id of the last record in the projection that produced it;
3. rewrites provisional `tmp-*` ids wherever they appear in that turn's `id`, `thread` and `re` fields (this is how an agent opens a new thread: `"id": "tmp-1", "thread": "tmp-1"`);
4. validates against the schema and the protocol rules (§6) and **rejects the whole turn** if any record fails. A rejected turn is not appended; PROTOCOL §5.4 says what happens next.

The appender never alters `from`, `to`, `re` (beyond tmp rewriting), `kind`, `body`, `next` or `visibility`.

### 2.4 Turns

A **turn** is a maximal contiguous run of records with the same `from` and the same `seen`. Because a participant's next projection always includes its own previous records, its next turn always has a later `seen`, so consecutive turns by one author are distinguishable. In a hand-authored log that omits `seen`, consecutive records by one author count as one turn — stamp `seen` if that matters. Floor permission (PROTOCOL §3) is evaluated once per turn, at its first record.

---

## 3. Kinds

Six kinds. Each carries an obligation, or discharges one, or closes a thread. Nothing else does.

### 3.1 Obligation table

Terms: an *addressee* is a member of the expanded `to`. A *reviewer* is an addressee of a `propose` other than the chairs and other than tool participants. A *chair* is any participant with `role: chair` (§5); a parley has zero or more.

| kind | who may emit | obligation created | discharged by | effect on thread |
|---|---|---|---|---|
| `say` | anyone; the only kind tool participants may emit | none | — | none |
| `ask` | anyone | each addressee owes a **reply**. Tool participants never owe. Chairs owe only when addressed explicitly, not via `*`. A reply owed is debt, not a gate: it never holds a thread back from *ready* and it survives a `decide`. | any record from that addressee, of any kind except `ask`, with the ask's id in `re` — in a closed thread, a `say`. A counter-`ask` defers: the original stays open until the counter-question is answered. | none |
| `propose` | anyone | each reviewer owes **accept or object**. When every reviewer has discharged, the thread is *ready* and the **chairs owe a `decide`** on it (no deadline; any one chair's decide discharges it). With no chair, *ready* is where a thread rests. | an `accept` or `object` from that reviewer with the propose's id in `re` | registers a proposal (§4.2) |
| `accept` | anyone | none | — | marks the proposal accepted by the author. Discharges only if the author is a reviewer of it; an accept from anyone else is recorded and discharges nothing. |
| `object` | anyone, at any time, **without holding the floor** | none. If `next` is empty, the authors of the objected records are nominated by default. | — | marks the proposal *contested* (from anyone, reviewer or not). An object may target any record, not only proposals. |
| `decide` | chairs only | none | — | **closes the thread** and voids every open *review* obligation in it and every live nomination made by its records (the decide's own `next` stands). Replies owed to asks survive and are given as `say`. After a decide, only `say` is valid in that thread. |

Consequences worth stating:

- **A directive is an `ask`.** "Codex, implement 004 and put up the change" is an `ask` to codex; the reply is the report — a `say`, or a `propose` carrying the change. A `decide` can nominate someone (`next`) but it does not obligate them; if the chair wants the work *owed*, the chair asks.
- **Advice, acknowledgement, condition** (amendment 1, PROTOCOL §12). An `accept` carries reasons and nothing else; it authorizes the proposal as recorded even if every remark around it is ignored. A reviewer's remark that asks nothing is a `say`. A remark whose fate the reviewer wants on record is an `ask` to the proposer, filed *after* the accept in the same turn: the proposer owes a reply — done, or declined with a reason — not compliance; the accept stands either way; the ask never delays a decide and survives one. Anything approval depends on is an `object`. The test is unchanged: if the proposer refused this remark and you would still accept, it is not a condition.
- **Commits.** A commit offered for review and merge is a `propose` with `change`. A commit the others should know about but not review (WIP, a spike, the chair's own housekeeping) is a `say` with `change`. The merge is the chair's `decide` with `change`. There is no `edit` kind: its obligation would be exactly `propose`'s, and under "kinds carry obligations" the same obligation is the same kind.
- **Counter-proposals** are a `propose` in the same thread with `re` = [the original, the objection]. Reviewers now owe on both; one `accept`/`object` listing both in `re` discharges both.
- **Withdrawal** is not first-class. The proposer says so in a `say` re its own proposal; reviewer obligations stand until a chair decides (one line). In practice withdrawal is rare and supersession by amendment is common.
- **Disagreeing with a decision** is a new thread whose root `re`s the decide. "Object before I decide, not after" is a norm; the schema makes the after-the-fact route explicit and visible rather than impossible.
- **Silence is visible.** Nothing but the discharges above, or a decide, closes an obligation. Open obligations appear in every projection's standing block and in the validator's report; the dispatcher logs timeouts as `say` records (PROTOCOL §4.3).

### 3.2 Bodies

| kind | body | required |
|---|---|---|
| `say` | `{text, change?}` | `text` |
| `ask` | `{text}` | `text` |
| `propose` | `{text, change?}` | `text` |
| `accept` | `{reasons}` | `reasons`: one entry per reason; each states **what was checked and why it holds**. "Looks good" is not a reason. Nothing else: a remark is a following `say`, a request a following `ask`, a condition an `object` (§3.1, *Advice, acknowledgement, condition*). |
| `object` | `{reasons, quote?, text?}` | `reasons`: one entry per specific defect or risk. `quote` SHOULD carry the disputed text verbatim (HANDOFF §5.2: no characterizing a position without quoting it). |
| `decide` | `{text, change?}` | `text`: the ruling and its rationale. |

`change` = `{repo?, branch?, commit?, files?, diff?}` with at least one of `commit` (7–40 hex) or `diff` (unified). `diff` is for participants without repo access, or for a patch that is not committed anywhere. `repo` only when a parley spans more than one repository.

`text` is Markdown. Records are read by models through projections, so bodies SHOULD be written to be read once, cold.

### 3.3 Why six, and what is not a kind

The budget is six until real transcripts demand more. Things that look like kinds and are not:

| wanted | expressed as |
|---|---|
| answer | `say` (or `propose`) with the ask in `re` |
| report, status, FYI, note | `say` |
| request, assign, delegate | `ask` |
| edit, commit, patch | `propose` + `change` (for review) or `say` + `change` (FYI) |
| merge | `decide` + `change` |
| vote | `accept` / `object` |
| counter-proposal, amendment | `propose` with `re` = [original, objection] |
| withdraw | `say` re own proposal; chair decides |
| summary for a small model | `say` from a tool participant with `visibility` limited to the recipient (PROTOCOL §5.3) |
| timeout, rejected turn, rebase conflict | `say` from a tool participant |
| private note to one participant | `say` with `visibility: [that participant]` (chairs see it too) |
| chair's note to self | `say` with `visibility: [chair]` |
| role assignment (author/adversary) | policy, not schema: a chair's `ask` ("find the strongest objection to #N") makes the adversary role an owed reply |

---

## 4. Threads

### 4.1 Identity

A thread is identified by its root record's id; every record carries `thread`. Threads are flat: there are no sub-threads. Nesting (a counter-proposal to an amendment to a proposal) is expressed by `re`, and one `decide` closes the whole thread, whichever proposal it names. If two things need separate decisions, they are separate threads. A record's `re` may point into any thread; `thread` says where the record *belongs*.

### 4.2 States

Thread states, derived from the log:

| state | condition | who acts |
|---|---|---|
| **open** | root appended, not closed | whoever holds the floor (PROTOCOL §3) |
| **ready** | open, contains ≥1 proposal, and no open *review* obligation on any non-chair participant in the thread (replies owed to asks do not count; the chair's queue lists them beside the thread) | the chairs owe a `decide` (any one of them). With no chair the thread rests here: everyone owed has taken a position, and nothing closes it. A thread drops back to open if a new `propose` lands in it. |
| **closed** | a `decide` with this `thread` | only `say` may follow |

A thread with no proposal and nothing open is simply open and quiet; it needs no decide, though a chair MAY close it.

Proposal states, per `propose`: **pending** (some reviewer has not discharged) · **accepted** (all reviewers discharged, none objected) · **contested** (at least one object, from anyone) · **decided** (thread closed).

### 4.3 Provisional proceeding

A human chair is the slowest participant by orders of magnitude. So: in a ready thread, participants MAY act on an *accepted* proposal (in their own worktrees, where there is a repository) before a chair decides. Contested proposals wait. Nothing reaches the base branch without a `decide`; a decide that goes the other way costs the provisional work and nothing else.

### 4.4 Linearization and concurrency

The total order is file order (§1, rule 3). The DAG formed by `re` is used for obligation discharge, for scoping thread views, and for nothing else; it never overrides file order.

Two turns are **concurrent** iff neither author had seen the other's records: turn *B* did not see record *r* iff *r* is visible to *B*'s author, was appended after *B*'s `seen`, and before *B*'s first record. The validator reports these (code `C1`). Concurrency is normal — it is what "agents proceed on each other's turns" means — and the log shows exactly who knew what when they spoke.

---

## 5. Participant registry

`participants.json`. Canonical shape: `schema/participants.schema.json`.

| field | type | required | meaning |
|---|---|---|---|
| `id` | `^[a-z][a-z0-9_-]{0,31}$` | yes | Used in `from`, `to`, `next`, `visibility`. Lowercase so that models cannot get the case wrong. |
| `kind` | `human` `model` `tool` | yes | How the dispatcher reaches the participant, nothing more: a human through a prompt, an inbox file or by stopping the run (`mode`); a model through its `harness`. Tool participants (the dispatcher, a summarizer, a CI hook) may only `say`, never owe anything, and are excluded from `*` for obligation purposes. |
| `role` | `chair` `member` | default `member` | Chair is a role, not a species: zero or more participants, human or model (never a tool). Only chairs decide; chairs hold the free floor; chairs see every record; chairs never owe review. Two humans talking are two chairs; two models can have one chair, or none. |
| `model` | string | for `model` | The model string as its harness reports it. |
| `harness` | string | — | How the participant is invoked: `claude-code`, `codex-cli` (the CLIs under their own logins — agentic in a worktree when the participant has `repo`, text-only with tools off when it does not); `llama-server` (a local model); `command` (any program: projection on stdin, records on stdout); `inbox` (files); `human`; `dispatch`. The dispatcher keys on this. |
| `capabilities` | `[string]` | default `[]` | Defined: `repo` (has a worktree; reads code and the log directly), `shell`, `web`. Others free-form. A participant without `repo` never receives a raw diff beyond the inline cap; it gets the stat and the prose. |
| `latency` | `human` `minutes` `seconds` | yes | Sets the dispatcher's per-turn wait (PROTOCOL §4.3). `human`: no deadline, ever. |
| `mode` | `exit` `prompt` `inbox` | default `exit` | Humans only: how the dispatcher reaches them. `exit`: the run stops and prints their standing block; `prompt`: an interactive prompt when they are the operator (`parley run -i`); `inbox`: the projection is written to `.parley/inbox/<id>.md` and the reply awaited in `<id>.reply.md`. |
| `worktree`, `branch` | string | for `repo` | Where the participant works. Convention: `../<repo>-<id>` and `parley/<id>`. The integration branch is the registry's top-level `base_branch` (default: the first chair's `branch`, else `main`). |
| `context_budget` | integer | — | Approximate tokens per projection. Participants with a budget receive summarized projections (PROTOCOL §5.3). |
| `inline_diff_lines` | integer | default 80 | Diffs at or under this length are inlined in the participant's projection; longer ones are referenced. |
| `notes` | string | — | Free text. |
| `command`, `harness_args`, `extra_args`, `env`, `effort`, `session`, `timeout_s`, `endpoint`, `max_tokens`, `temperature` | — | — | Harness knobs read by the dispatcher, not by the protocol: how to invoke the participant (`harness_args` replaces the sandbox defaults, `extra_args` is appended after them — model, effort; `env` sets variables for the CLI process), whether to resume its CLI session between turns (`resume` → delta projections) or start fresh (`fresh` → the whole visible log each turn), the per-turn wall-clock limit, and llama-server settings. `tools/parleylib/harness.py` documents them. |

Example (the three-party registry used by `transcripts/example-01.jsonl`, plus two text-only participants through the same CLIs, a small local model and the dispatcher):

```json
{
  "name": "parley",
  "participants": [
    {"id": "conor",    "kind": "human", "role": "chair", "harness": "human", "latency": "human",
     "capabilities": ["repo", "shell", "web"], "branch": "main"},
    {"id": "claude",   "kind": "model", "model": "claude-fable-5-1", "harness": "claude-code", "latency": "minutes",
     "capabilities": ["repo", "shell", "web"], "worktree": "../parley-claude", "branch": "parley/claude"},
    {"id": "codex",    "kind": "model", "model": "gpt-5-codex", "harness": "codex-cli", "latency": "minutes",
     "capabilities": ["repo", "shell"], "worktree": "../parley-codex", "branch": "parley/codex"},
    {"id": "fable",    "kind": "model", "model": "claude-fable-5-1", "harness": "claude-code", "latency": "minutes",
     "capabilities": [], "effort": "max", "extra_args": ["--model", "fable"]},
    {"id": "astra",    "kind": "model", "model": "gpt-6-astra", "harness": "codex-cli", "latency": "minutes",
     "capabilities": [], "effort": "max", "extra_args": ["-m", "gpt-6-astra"]},
    {"id": "llama",    "kind": "model", "model": "Qwen3-8B-Q6_K.gguf", "harness": "llama-server", "latency": "seconds",
     "capabilities": [], "context_budget": 6000, "inline_diff_lines": 0},
    {"id": "dispatch", "kind": "tool",  "harness": "dispatch", "latency": "seconds"}
  ]
}
```

---

## 6. Validation rules

What `tools/validate.py` enforces, by code. `S`/`L` come from the schema and the file; the rest are protocol rules the schema cannot express. Violations exit 1; warnings and infos do not.

| code | rule |
|---|---|
| `L1` | every line is a JSON object |
| `S1` `S2` | record / registry matches its schema |
| `R1` | `id` unique |
| `R3` `R4` | `from`, `to`, `next`, `visibility` name registered participants; the author is not among its own addressees |
| `R5` | every `re` target exists earlier in the log |
| `R6` | `thread` is the record's own id (root) or an existing root |
| `R7` | in a closed thread, only `say` |
| `R8` | `seen` is `null` or an earlier record |
| `R9` | addressees and nominees are viewers |
| `K1` | `decide` only from a chair |
| `K2` | `accept` has at least one `propose` among its `re` targets |
| `K3` | `decide` on an already-closed thread |
| `K5` | tool participants only `say` |
| `F1` | at the first record of a turn, a non-chair author holds the floor: it owes something, or is nominated, or the record is an `object` — or it is the opening record of the log — or the parley has no chair and the floor is free (nothing owed by anyone, nobody nominated) |
| `F2` | an unbidden turn (permitted only by objecting) contains nothing but the objection and records that respond to it |
| `P1` | chairs are human or model participants, never tools; zero or more |
| `O1` warn | a `propose` with no reviewers (ready immediately) |
| `O2` info | an `accept` from a non-reviewer (recorded, discharges nothing) |
| `O3` info | a counter-question left the original ask open |
| `O4` warn | in one turn, an `ask` about a proposal precedes the `accept` of it; the accept goes first (PROTOCOL §5.4) |
| `A1` info | a record uses a shape retired by an amendment after it was appended (PROTOCOL §12): checked by the shape in force at its `ts` |
| `D1` info | a `decide` voided open reviews and/or live nominations; replies still owed in the thread are counted |
| `D2` warn | a thread closed with zero objections across its proposals — the agreement-collapse check (HANDOFF §5.1) |
| `C1` info | a turn did not see specific earlier records (concurrency) |

Gaps in the numbering (`R2` id shape, `K4` explicit `to` on `ask`) are rules the schema itself enforces and therefore surface as `S1`.

The `--report` flag prints thread and proposal states, open obligations by participant, the chairs' queue (ready threads) and live nominations — the same facts the standing block of a projection is built from.

---

## 7. JSON Schema — record

Canonical: `schema/record.schema.json` (draft 2020-12; deliberately uses no keyword newer than draft 7, so `jsonschema` 3.x validates it). Reproduced here for reading; the file is what runs.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://raw.githubusercontent.com/laundrevity/parley/main/schema/record.schema.json",
  "title": "parley record",
  "type": "object",
  "required": ["id", "ts", "from", "thread", "kind", "body"],
  "additionalProperties": false,
  "properties": {
    "id":         { "$ref": "#/$defs/recordId" },
    "ts":         { "type": "string",
                    "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$" },
    "from":       { "$ref": "#/$defs/participantId" },
    "to":         { "$ref": "#/$defs/audience", "default": ["*"] },
    "re":         { "type": "array", "items": { "$ref": "#/$defs/recordId" }, "uniqueItems": true, "default": [] },
    "thread":     { "$ref": "#/$defs/recordId" },
    "kind":       { "enum": ["say", "ask", "propose", "accept", "object", "decide"] },
    "body":       { "type": "object" },
    "next":       { "type": "array", "items": { "$ref": "#/$defs/participantId" }, "uniqueItems": true, "default": [] },
    "seen":       { "oneOf": [{ "$ref": "#/$defs/recordId" }, { "type": "null" }] },
    "visibility": { "$ref": "#/$defs/audience", "default": ["*"] }
  },
  "allOf": [
    { "if": { "required": ["kind"], "properties": { "kind": { "const": "say" } } },
      "then": { "properties": { "body": { "$ref": "#/$defs/sayBody" } } } },
    { "if": { "required": ["kind"], "properties": { "kind": { "const": "ask" } } },
      "then": { "required": ["to"], "properties": { "body": { "$ref": "#/$defs/askBody" } } } },
    { "if": { "required": ["kind"], "properties": { "kind": { "const": "propose" } } },
      "then": { "properties": { "body": { "$ref": "#/$defs/proposeBody" } } } },
    { "if": { "required": ["kind"], "properties": { "kind": { "const": "accept" } } },
      "then": { "required": ["re"], "properties": { "re": { "minItems": 1 }, "body": { "$ref": "#/$defs/acceptBody" } } } },
    { "if": { "required": ["kind"], "properties": { "kind": { "const": "object" } } },
      "then": { "required": ["re"], "properties": { "re": { "minItems": 1 }, "body": { "$ref": "#/$defs/objectBody" } } } },
    { "if": { "required": ["kind"], "properties": { "kind": { "const": "decide" } } },
      "then": { "properties": { "body": { "$ref": "#/$defs/decideBody" } } } }
  ],
  "$defs": {
    "recordId":      { "type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$" },
    "participantId": { "type": "string", "pattern": "^[a-z][a-z0-9_-]{0,31}$" },
    "audience": { "oneOf": [
        { "const": ["*"] },
        { "type": "array", "items": { "$ref": "#/$defs/participantId" }, "minItems": 1, "uniqueItems": true } ] },
    "change": {
      "type": "object", "additionalProperties": false,
      "properties": {
        "repo":   { "type": "string" },
        "branch": { "type": "string" },
        "commit": { "type": "string", "pattern": "^[0-9a-f]{7,40}$" },
        "files":  { "type": "array", "items": { "type": "string" }, "uniqueItems": true },
        "diff":   { "type": "string" } },
      "anyOf": [{ "required": ["commit"] }, { "required": ["diff"] }] },
    "text":    { "type": "string", "minLength": 1 },
    "reasons": { "type": "array", "items": { "type": "string", "minLength": 1 }, "minItems": 1 },
    "sayBody":     { "type": "object", "required": ["text"],    "additionalProperties": false,
                     "properties": { "text": { "$ref": "#/$defs/text" }, "change": { "$ref": "#/$defs/change" } } },
    "askBody":     { "type": "object", "required": ["text"],    "additionalProperties": false,
                     "properties": { "text": { "$ref": "#/$defs/text" } } },
    "proposeBody": { "type": "object", "required": ["text"],    "additionalProperties": false,
                     "properties": { "text": { "$ref": "#/$defs/text" }, "change": { "$ref": "#/$defs/change" } } },
    "acceptBody":  { "type": "object", "required": ["reasons"], "additionalProperties": false,
                     "properties": { "reasons": { "$ref": "#/$defs/reasons" } } },
    "objectBody":  { "type": "object", "required": ["reasons"], "additionalProperties": false,
                     "properties": { "reasons": { "$ref": "#/$defs/reasons" }, "quote": { "type": "string", "minLength": 1 },
                                     "text": { "$ref": "#/$defs/text" } } },
    "decideBody":  { "type": "object", "required": ["text"],    "additionalProperties": false,
                     "properties": { "text": { "$ref": "#/$defs/text" }, "change": { "$ref": "#/$defs/change" } } }
  }
}
```

The registry schema is `schema/participants.schema.json`; it is short and not reproduced.

A tightened copy of the record schema — `kind` narrowed to the kinds a given participant may emit, `from` fixed to its id — compiles to a GBNF grammar (llama.cpp does this from `response_format: json_schema` directly), so a small model's records are valid by construction (PROTOCOL §5.3).

---

## 8. Examples

One record per kind, from `transcripts/example-01.jsonl` (bodies shortened). Fields at their defaults are omitted.

```jsonl
{"id": "001", "ts": "2026-09-25T19:02:11Z", "from": "conor", "to": ["claude"], "thread": "001", "kind": "ask", "body": {"text": "Next tool: tools/project.py … Propose the design before code."}, "next": ["claude"], "seen": null}
{"id": "002", "ts": "2026-09-25T19:06:40Z", "from": "claude", "re": ["001"], "thread": "001", "kind": "propose", "body": {"text": "Design for tools/project.py, stdlib only. …"}, "next": ["codex"], "seen": "001"}
{"id": "003", "ts": "2026-09-25T19:10:05Z", "from": "codex", "re": ["002"], "thread": "001", "kind": "object", "body": {"quote": "followed by the full unified diff …", "reasons": ["Unbounded. …", "Repo-capable participants have a worktree. …", "… not a budget. …"], "text": "Counter-sketch: …"}, "seen": "002"}
{"id": "005", "ts": "2026-09-25T19:17:48Z", "from": "codex", "re": ["004"], "thread": "001", "kind": "accept", "body": {"reasons": ["The cap is a registry number per participant …", "Overflow policy is explicit and visible …", "Standing block via validate.Replay …"]}, "seen": "004"}
{"id": "006", "ts": "2026-09-25T19:17:48Z", "from": "codex", "re": ["004"], "thread": "001", "kind": "say", "body": {"text": "Starting the implementation on parley/codex now, provisionally, while the chair decides."}, "seen": "004"}
{"id": "007", "ts": "2026-09-25T19:31:02Z", "from": "conor", "re": ["004"], "thread": "001", "kind": "decide", "body": {"text": "004 as amended. Codex implements on parley/codex, Claude reviews. …"}, "next": ["codex"], "seen": "006"}
{"id": "011", "ts": "2026-09-25T20:08:30Z", "from": "conor", "thread": "011", "kind": "say", "body": {"text": "FYI: on main I gave `next` a schema default …", "change": {"branch": "main", "commit": "91c0e2f", "files": ["schema/record.schema.json"]}}, "seen": "010"}
```

The full transcript exercises: an ask answered by a propose (`010` re `009`), a counter-proposal (`004` re `002`,`003`), one record discharging two review obligations (`012` re `008`,`010`), an unbidden objection to the chair (`013`), two concurrent turns (`011`∥`012`, `013`∥`014`), a private record (`016`), an accept whose non-blocking note follows it as a `say` in the same turn (`005`, `006`), provisional proceeding (`006`), and three decides. Commit hashes in it are illustrative.

---

## Appendix A — Coverage check against HANDOFF.md v0.1

HANDOFF.md is the hub-and-spoke predecessor: one human, two models, one shared document, verbatim transport by the human. No turn logs beyond it exist yet (its DISPUTES section is empty; no rounds were recorded), so this check is against the mechanisms HANDOFF defines. Anything HANDOFF does that the kind set cannot express would be a schema bug.

| HANDOFF mechanism | parley expression | notes |
|---|---|---|
| §0 the document is the only shared state | the log (§1, rule 1) | the document becomes a repo file; its versions are commits |
| §1.1 hub-and-spoke, verbatim transport (§4.1) | not needed: all parties read the same log | the hub's distortion channel is gone; the chair's own words are `from: conor` records |
| §1.2 statelessness; decisions recorded with rationale or relitigated | `decide.body.text` carries the ruling and rationale; closed threads reject new proposals (§4.2) | reopening is an explicit new thread, so relitigation is visible |
| §1.3 agreement is cheap; §5.1 empty DISPUTES = failure | reviewer obligation (accept **or** object, with reasons); `D2` flags zero-objection threads | structural, not prompt-level; `D2` is the direct descendant of §5.1 |
| §1.4 drafter advantage; dispute the protocol itself | `object` may target any record, unbidden, including the chair's (`012`) | |
| §2 pilot object, one at a time | a thread | tripwires are chair policy expressed as `decide`s |
| §3 Author / Adversary rotating roles | **policy, not schema.** The chair's `ask` ("find the strongest objection to #N") makes adversary work an owed reply; a `propose` makes every other agent a reviewer. | roles as a registry field were considered and rejected: the obligation table already forces each party to take a position on every proposal |
| §4.2 (a) role contribution | `propose` / `say` | |
| §4.2 (b) DISPUTES entries quoting the disputed text verbatim | `object` with `quote` | `quote` was added to the object body because of this check |
| §4.2 (c) at most three questions | `ask`; the cap is policy | |
| §4.3 Conor merges, bumps version, records the delta | `decide` + `change`; the log is the change log | §8 change log is subsumed |
| §4.1 edits by the human marked `[C]` | `from: conor` | |
| §4.4 a round = both models responded to the same version | a *ready* thread (§4.2) | |
| §5.2 no characterizing without quoting | `object.quote`; `re` always names the target | |
| §5.3 uncertainty stated as uncertainty; §5.4 praise is not a contribution | `accept.reasons` must say what was checked | content norms otherwise; carried into the agent rules (PROTOCOL §8) |
| §5.5 document cap 300 lines; resolved disputes compress | projection budgets and dispatcher summaries (PROTOCOL §5.3) | the log itself never compresses |
| §6 exit conditions and tripwires | `decide` closing the root thread | |
| §3 "the human is the likeliest failure point … quietly abandoning the loop" | the chair owes a `decide` on every ready thread and a reply to every explicit `ask`; both are open obligations in the report | added because of this check: without it the chair's abandonment was invisible |
| §1.1 "the hub can distort" — summaries | a summary shown to any participant is itself a logged record (PROTOCOL §5.3) | the log contains everything anyone was shown |

Nothing in HANDOFF v0.1 is inexpressible. Two things changed in the schema because of the check (`object.quote`, the chair's decide-obligation on ready threads); two are deliberately policy rather than schema (rotating roles, the question cap).
