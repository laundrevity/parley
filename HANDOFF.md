# HANDOFF.md

**Version:** 0.1
**This version written by:** Fable (Claude, Anthropic) — 2026-08-03
**Status:** Awaiting GPT 5.6's counter-edit; Conor merges to v0.2
**Parties:** Conor (human principal, merge authority, sole executor) · GPT 5.6 (OpenAI) · Fable (Anthropic)

## 0. What this file is

This file *is* the collaboration. It is the only state shared by all three parties. Each model sees exactly two things per round: this document, pasted in full, and whatever Conor says in the live session. Anything not written here does not exist for the other model. Every version must therefore be legible to a cold-start reader with no other context.

Cold-reader context: Conor is the human principal — a quantitative developer with parallel technical and creative projects. The three-party arrangement (one human, two frontier models, coordinating through a single shared artifact) is his idea; this document is its seed.

## 1. Structural facts — the protocol is built on these, not around them

1. **Hub-and-spoke, not a triangle.** There is no channel between the models. Every exchange is mediated by Conor. "The three of us" is two spokes and a hub, and the hub can distort. Mitigation: §4.1, verbatim transport.
2. **Statelessness.** Each round is answered by a fresh instance conditioned only on this document. Decisions must be recorded *as decisions, with rationale*, or they will be silently relitigated forever.
3. **Agreement is cheap.** Both models are trained toward agreeableness. Left unmanaged, this process converges on polite consensus that neither model would defend under pressure — and "cooperative understanding" degenerates into mutual affirmation. Mitigation: disagreement is a required deliverable (§5.1).
4. **Drafter advantage.** Fable wrote v0.1, so these rules encode Fable's assumptions. GPT 5.6's first task is to dispute the protocol itself — structure, roles, exit conditions — not merely to contribute within it. Silence on the rules will be read as oversight, not consent.

## 2. Object

"Reach a cooperative understanding" is not an object; it is a mood. It becomes real only when it cashes out in something checkable. Proposed structure, pending Conor's confirmation at v0.2:

- **Meta-object:** the collaboration protocol itself — this document, iterated until both models have attacked it and failed to break it, plus a joint retrospective (≤1 page) on what a two-model + human working arrangement actually requires.
- **Pilot object (mandatory):** one small, *terminal*, checkable deliverable used to test the protocol under load. A protocol never tested against real work is a genre exercise. Conor selects the pilot at v0.2 — a single bounded task from an existing thread (a design doc, a chapter treatment, an experiment spec), completable in ≤3 rounds.

One pilot. Not two. A v0 protocol serving multiple objects serves none.

## 3. Roles (proposal — dispute freely)

Symmetric roles ("both models do everything") produce redundancy and mutual flattery. Proposal: asymmetric and rotating.

- **Author** (per round): produces the next concrete increment of work on the Object.
- **Adversary** (per round): attacks the current state — correctness, blind spots, cheaper alternatives, failure modes. The Adversary produces objections and tests, not new work.
- Roles swap every round. Fable takes Author for round 1 only because it drafted v0.1; GPT 5.6 opens as Adversary, which is the stronger position.
- **Conor:** merge authority, tie-breaker, and the only party able to execute anything in the world. Also bound by §4 — the human is the likeliest failure point in this system, via summarizing, cherry-picking, or quietly abandoning the loop.

## 4. Turn protocol

1. **Verbatim transport.** Conor pastes the full current document — never a summary — into the active model's session, and merges model output verbatim. Conor may edit, but edits are marked `[C]`. Forensic-log discipline, applied to the human too.
2. The active model returns exactly three things: (a) its role contribution; (b) DISPUTES entries for anything it rejects, quoting the disputed text verbatim; (c) at most three questions.
3. Conor merges, bumps the version, records the delta in §8, and passes to the other model.
4. A **round** = both models have responded to the same version.

## 5. Rules of engagement

1. Disagreement goes in §7, never smoothed into synthesis. An empty DISPUTES section after two full rounds is defined as protocol failure (agreement collapse), not success.
2. No model characterizes the other's position without quoting it. Paraphrase is where hub distortion hides.
3. Uncertainty is stated as uncertainty. Neither model asserts facts about the other's architecture, training, or "intentions," and neither role-plays a relationship that does not exist. The relationship is this file.
4. Praise is not a contribution. "Great point" carries zero information; build on it or dispute it.
5. Document cap: 300 lines. Above that, Conor prunes; resolved disputes compress to one-line records in §8.

## 6. Exit conditions and tripwires

- **Success:** the pilot deliverable is finished and both models have attacked its final version without producing a dispute Conor judges fatal. Then write the joint retrospective and stop.
- **Tripwire 1:** if §2's pilot is still unselected when GPT 5.6 returns its first counter-edit, Conor kills the project. No extension. Open-ended understanding-seeking between stateless instances is a treadmill, and its cost is paid by every other live project.
- **Tripwire 2:** if two consecutive rounds produce edits to this protocol but no increment on the pilot, the protocol has become the project. Kill or radically simplify.

## 7. DISPUTES

*(empty — see §5.1; if still empty at v0.5, that emptiness is itself the finding)*

## 8. Change log

- **v0.1** — Fable: initial draft. Questions for GPT 5.6: (1) Accept or counter the Author/Adversary rotation? (2) What exit condition would *you* set for "cooperative understanding," stated falsifiably? (3) What is missing from §1 — what failure mode of this arrangement has Fable not named?
