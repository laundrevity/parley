"""The dispatcher: PROTOCOL.md §1 (rounds), §3 (floor), §3.5 (objection pass), §4.3
(timeouts and failed turns), §5 (projections), §7.5 (rebase).

One round: everyone who may speak and can be invoked is invoked concurrently, each with its
own projection; their turns are appended in arrival order; then the objection pass gives
every model with unseen records one chance to object. Participants are humans, models or
programs alike; the only distinction the dispatcher makes is *how to reach them*: a model
through its harness, a human through a prompt or an inbox file — or, in the default `exit`
mode, by stopping the run and printing their standing block. When nobody who can be reached
may speak, the run ends: at the humans' turn, or quiescent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from . import core, harness
from . import project as P
from . import validate as V

MAX_CONSECUTIVE_FAILURES = 2
DEFAULT_ROUND_CAP = 20


def eprint(*a):
    print(*a, file=sys.stderr, flush=True)


class Dispatcher:
    def __init__(self, parley: core.Parley, *, rebase: bool = True, objection_pass: bool = True,
                 only: Optional[list] = None, dry_run: bool = False, git_enabled: bool = True,
                 max_passes: int = 3):
        self.parley = parley
        self.rebase = rebase
        self.objection_pass = objection_pass
        self.only = set(only) if only else None
        self.dry_run = dry_run
        self.git_enabled = git_enabled
        self.max_passes = max_passes
        self.state_lock = threading.Lock()
        self.interrupted = False
        self.me: Optional[str] = None                 # the participant at this keyboard, if any
        self.human_prompt: Optional[Callable] = None  # (pid, projection) -> records appended
        self.waiting: list = []                       # humans whose turn it is when the run stops
        self.record_schema = V.load_json(os.path.join(core.SCHEMA_DIR, "record.schema.json"))

    # ------------------------------------------------------------------ helpers
    def agents(self) -> list:
        ps = self.parley.agents()
        return [p for p in ps if not self.only or p["id"] in self.only]

    def participants(self) -> list:
        """Everyone who can hold the floor: humans and models (tools only ever `say` via note())."""
        ps = [p for p in self.parley.registry["participants"] if p.get("kind") in ("human", "model")]
        return [p for p in ps if not self.only or p["id"] in self.only]

    @staticmethod
    def human_mode(p: dict) -> str:
        return p.get("mode", "exit") if p.get("kind") == "human" else ""

    def invocable(self, p: dict) -> bool:
        """Can the dispatcher reach this participant itself, right now?"""
        if p.get("kind") == "model":
            return True
        mode = self.human_mode(p)
        if mode == "inbox":
            return True
        if mode == "prompt":
            return self.human_prompt is not None and p["id"] == self.me
        return False

    def has_unseen(self, p: dict, records: list, st: dict) -> bool:
        return self.parley.pstate(st, p["id"]).get("last_shown") != self.parley.last_visible_id(p["id"], records)

    def wants_turn(self, p: dict, rp, records: list, st: dict) -> bool:
        pid = p["id"]
        if rp.open_for(pid):
            return True
        # a nomination or a ready thread is an invitation, not a debt: a model is asked once per new record,
        # and one that answers with nothing is not asked again until the log has grown (humans always wait)
        if rp.nominations.get(pid) or (pid in rp.chairs and rp.chair_queue):
            return p.get("kind") == "human" or self.has_unseen(p, records, st)
        return False

    def with_state(self, fn):
        with self.state_lock:
            st = self.parley.load_state()
            out = fn(st)
            self.parley.save_state(st)
            return out

    def note(self, text: str, re_: Optional[list] = None, visibility: Optional[list] = None) -> None:
        """A `say` from the dispatch participant (PROTOCOL §4.3), or a log line if there is none."""
        self.parley.log_event("NOTE " + text)
        if "dispatch" not in self.parley.participants:
            eprint("  (no `dispatch` participant in the registry; note kept in dispatch.log only)")
            return
        rec = core.build_record("say", text=text, re_=re_ or None, visibility=visibility)
        try:
            self.parley.append_turn("dispatch", [rec])
        except core.TurnRejected as e:
            self.parley.log_event(f"NOTE REJECTED {e}")

    def halted(self, p: dict, records: list, st: dict) -> bool:
        ps = self.parley.pstate(st, p["id"])
        if ps.get("failures", 0) < MAX_CONSECUTIVE_FAILURES:
            return False
        failed_at = ps.get("failed_at")
        releasers = set(self.parley.chairs) | {h["id"] for h in self.parley.humans()}
        # released once a chair or a human has said anything after the failures
        for r in reversed(records):
            if r.id == failed_at:
                break
            if r.frm in releasers:
                ps["failures"] = 0
                return False
        return True

    # ------------------------------------------------------------------ one turn
    def take_turn(self, p: dict, mode: str) -> int:
        pid = p["id"]
        parley = self.parley
        wt = parley.worktree(p)
        if wt and not os.path.isdir(wt):
            eprint(f"  {pid}: worktree {wt} does not exist (run `parley init`); skipping")
            return 0

        if self.rebase and wt and self.git_enabled and not self.dry_run:
            self.do_rebase(p, wt)

        records = parley.records()
        rp = parley.replay(records)
        owed = [o.rec for o in rp.open_for(pid)]
        ps0 = self.with_state(lambda st: dict(parley.pstate(st, pid)))
        fresh = p.get("session", "resume" if p.get("harness") == "claude-code" else "fresh") == "fresh"
        since = None if fresh else ps0.get("last_shown")
        last_visible = parley.last_visible_id(pid, records)

        git = P.Git(parley.repo, self.git_enabled, parley.base_branch)
        pj = P.Projector(records, parley.registry, pid, git, False)
        new_after = ps0.get("last_shown") if fresh else None
        projection = pj.build(since, p.get("context_budget"), False, new_after)
        if mode == "objection":
            scope = "marked (new)" if (fresh and new_after) else "below"
            projection = (f"OBJECTION PASS — you owe nothing and are not nominated. Object to anything {scope} "
                          "that is wrong (an `object` record with reasons and a quote), or reply with an empty fence.\n\n"
                          + projection)
        if fresh and not since:
            projection = ("(fresh session: this is the whole log you can see"
                          + ("; records you have not been shown before are marked (new)" if new_after else "")
                          + ")\n\n" + projection)

        if self.dry_run:
            print(f"===== projection for {pid} ({mode}) =====\n{projection}\n")
            return 0

        system_prompt = ""
        # every participant that has no CLAUDE.md / AGENTS.md of its own gets the rules with the turn
        if p.get("harness") not in ("claude-code", "codex-cli") or not wt:
            from . import gendocs
            log_rel = os.path.relpath(parley.log_path, parley.repo)
            system_prompt = gendocs.render_rules(p, parley.registry, log_rel=log_rel)

        what = ("objection pass" if mode == "objection" else
                ("owes " + ", ".join("#" + x for x in owed) if owed else
                 ("decide queue" if pid in rp.chairs and rp.chair_queue else "nominated")))
        eprint(f"  {pid}: invoking ({what}; since #{since or 'start'})")
        parley.log_event(f"INVOKE {pid} mode={mode} since={since} last_visible={last_visible}")

        if p.get("kind") == "human" and self.human_mode(p) == "prompt" and pid == self.me and self.human_prompt:
            n = self.human_prompt(pid, projection)
            self.with_state(lambda st: self._ok(parley.pstate(st, pid), last_visible, None))
            return n

        res = harness.run_turn(p, projection, wt or parley.repo, ps0, mode=mode, system_prompt=system_prompt,
                               record_schema=self.record_schema, repo=parley.repo, state_dir=parley.dir)
        parley.log_event(f"REPLY {pid} elapsed={res.elapsed:.0f}s error={res.error!r} note={res.note!r} cmd={res.cmd!r}\n{res.raw}\n")

        # a resumed CLI session that is gone (expired, deleted, another machine) is not the participant's
        # failure: forget it and re-run once as a fresh session over the whole visible log
        if res.records is None and not fresh and ps0.get("session_id") and harness.session_lost(res):
            eprint(f"  {pid}: previous session cannot be resumed; starting a fresh one over the whole log")
            parley.log_event(f"SESSION LOST {pid}: {res.error!r}")
            ps0 = dict(ps0); ps0.pop("session_id", None)
            self.with_state(lambda st: parley.pstate(st, pid).pop("session_id", None))
            projection = ("(fresh session: this is the whole log you can see; records you have not been shown before are marked (new))\n\n"
                          + pj.build(None, p.get("context_budget"), False, ps0.get("last_shown")))
            res = harness.run_turn(p, projection, wt or parley.repo, ps0, mode=mode, system_prompt=system_prompt,
                                   record_schema=self.record_schema, repo=parley.repo, state_dir=parley.dir)
            parley.log_event(f"REPLY {pid} elapsed={res.elapsed:.0f}s error={res.error!r} note={res.note!r} cmd={res.cmd!r}\n{res.raw}\n")

        if res.records is None:
            eprint(f"  {pid}: FAILED — {res.error}")
            self.fail(pid, last_visible, f"{pid}: {res.error}", owed)
            return 0

        appended = self.try_append(p, res, projection, last_visible, owed, mode, system_prompt)
        if appended is None:
            return 0
        self.with_state(lambda st: self._ok(parley.pstate(st, pid), last_visible, res.session_id))
        ids = [d["id"] for d in appended]
        if not appended and not owed:
            eprint(f"  {pid}: nothing to add in {res.elapsed:.0f}s (invited, not owing; not asked again until the log grows)")
        else:
            eprint(f"  {pid}: {len(appended)} record(s) appended{(' ' + ', '.join('#' + i for i in ids)) if ids else ''} in {res.elapsed:.0f}s")
        return len(appended)

    @staticmethod
    def _ok(ps: dict, last_visible, session_id):
        ps["last_shown"] = last_visible
        ps["turns"] = ps.get("turns", 0) + 1
        ps["failures"] = 0
        if session_id:
            ps["session_id"] = session_id

    def fail(self, pid: str, last_visible, text: str, owed: list) -> None:
        if self.interrupted:
            self.parley.log_event(f"INTERRUPTED {pid}: {text}")
            return
        def mark(st):
            ps = self.parley.pstate(st, pid)
            ps["failures"] = ps.get("failures", 0) + 1
            ps["failed_at"] = last_visible
            return ps["failures"]
        n = self.with_state(mark)
        self.note(text + (f" (obligations re {', '.join('#' + x for x in owed)} remain open)" if owed else ""), re_=owed)
        if n >= MAX_CONSECUTIVE_FAILURES:
            eprint(f"  {pid}: {n} consecutive failures; will not be invoked again until the chair speaks")

    def try_append(self, p: dict, res: harness.TurnResult, projection: str, last_visible, owed: list, mode: str,
                   system_prompt: str = ""):
        """Validate and append, with one retry on rejection or on ignoring what is owed.
        Returns the appended dicts, or None on final failure (state marked)."""
        pid = p["id"]
        parley = self.parley
        for attempt in (1, 2):
            recs = res.records or []
            covered = set()
            for r in recs:
                covered |= {str(x).lstrip("#") for x in (r.get("re") or [])}
            missing = [o for o in owed if o not in covered] if mode == "turn" else []
            problem = None
            if missing:
                problem = ("Your reply did not address what you owe: " + ", ".join("#" + x for x in missing)
                           + ". Every record you owe on must appear in some record's `re` (a `say` explaining a "
                             "delay is enough). Reply again with the complete set of records.")
            else:
                try:
                    return parley.append_turn(pid, recs, seen=last_visible)
                except core.TurnRejected as e:
                    problem = ("Your previous reply was rejected by the validator:\n" +
                               "\n".join(f"- {f.code}: {f.msg[:300]}" for f in e.findings[:8]) +
                               "\nReply again with corrected records (same fence format; see the record shapes in your rules).")
                    parley.log_event(f"REJECTED {pid} attempt {attempt}: {e}")
            if attempt == 1:
                eprint(f"  {pid}: retrying once — {problem.splitlines()[0][:100]}")
                ps0 = self.with_state(lambda st: dict(parley.pstate(st, pid)))
                res = harness.run_turn(p, projection + "\n\n" + problem, parley.worktree(p) or parley.repo, ps0, mode=mode,
                                       system_prompt=system_prompt,
                                       record_schema=self.record_schema, repo=parley.repo, state_dir=parley.dir)
                parley.log_event(f"REPLY(retry) {pid} error={res.error!r}\n{res.raw}\n")
                if res.records is None:
                    self.fail(pid, last_visible, f"{pid}: {res.error}", owed)
                    return None
        summary = problem.splitlines()[0][:200] if problem else "turn failed"
        eprint(f"  {pid}: FAILED — {summary}")
        self.fail(pid, last_visible, f"{pid}: turn not appended — {summary}", owed)
        return None

    def do_rebase(self, p: dict, wt: str) -> None:
        base = self.parley.base_branch
        g = lambda *a: subprocess.run(["git", *a], cwd=wt, capture_output=True, text=True)
        if g("merge-base", "--is-ancestor", base, "HEAD").returncode == 0:
            return                                   # already on top of base; nothing to do
        r = g("rebase", "--autostash", base)         # autostash: uncommitted work in the worktree survives
        if r.returncode == 0:
            return
        files = g("diff", "--name-only", "--diff-filter=U").stdout.split()
        g("rebase", "--abort")
        if files:
            msg = (f"rebase of {p.get('branch', p['id'])} onto {base} conflicts in {', '.join(files)}; "
                   "resolve it first (rebase aborted, your work is untouched).")
        else:
            first = (r.stderr or r.stdout).strip().splitlines()
            msg = f"rebase of {p.get('branch', p['id'])} onto {base} failed: {first[0] if first else 'unknown error'} (aborted)."
        eprint(f"  {p['id']}: {msg}")
        self.note(f"{p['id']}: {msg}", visibility=[p["id"]])

    # ------------------------------------------------------------------ rounds
    def invoke_all(self, plist: list, mode: str) -> int:
        if not plist:
            return 0
        ex = ThreadPoolExecutor(max_workers=len(plist))
        try:
            return sum(ex.map(lambda p: self.take_turn(p, mode), plist))
        except KeyboardInterrupt:
            self.interrupted = True          # workers still finishing must not record failures
            eprint("\ninterrupted — waiting for the running turns to stop; nothing will be recorded against them")
            raise
        finally:
            ex.shutdown(wait=True)

    def round(self) -> str:
        parley = self.parley
        records = parley.records()
        if not records:
            return "empty"
        rp = parley.replay(records)
        st = parley.load_state()
        people = [p for p in self.participants() if not self.halted(p, records, st)]
        parley.save_state(st)
        active = [p for p in people if self.wants_turn(p, rp, records, st)]
        invoke_now = [p for p in active if self.invocable(p)]
        self.waiting = [p for p in active if not self.invocable(p)]
        if invoke_now:
            eprint(f"[turns] {', '.join(p['id'] for p in invoke_now)}")
            self.invoke_all(invoke_now, "turn")
            return "turns"
        if self.waiting:
            return "human"
        if self.objection_pass:
            models = [p for p in people if p.get("kind") == "model"]
            for n in range(self.max_passes):
                st = parley.load_state()
                cands = [p for p in models if self.has_unseen(p, records, st)]
                if not cands:
                    break
                eprint(f"[objection pass {n + 1}] {', '.join(p['id'] for p in cands)}")
                appended = self.invoke_all(cands, "objection")
                if appended or self.dry_run:
                    return "objections"
                records = parley.records()
        return "quiescent"

    def block_for(self, pid: str) -> str:
        records = self.parley.records()
        pj = P.Projector(records, self.parley.registry, pid,
                         P.Git(self.parley.repo, self.git_enabled, self.parley.base_branch), False)
        return pj.build(None, None, True)

    def chair_block(self) -> str:
        """The operator's block: --as / the only human / the only chair; else the quiescence report."""
        pid = self.me or self.parley.chair
        if pid:
            return self.block_for(pid)
        rp = self.parley.replay()
        ready = sorted(rp.chair_queue)
        return ("--- quiescent: no obligations open, no one nominated; this parley has no chair ---\n"
                + ("ready threads (stay open): " + ", ".join("#" + x for x in ready) if ready else "no ready threads"))

    def run(self, max_rounds: Optional[int] = None, chair_turn: Optional[Callable[[], bool]] = None) -> str:
        """Dispatch rounds until it is a human's turn, or nobody may speak. max_rounds (default 20)
        caps the number of rounds between human turns: a runaway model–model loop costs real
        invocations. chair_turn: interactive callback for the operator (returns True if they spoke)."""
        cap = max_rounds or DEFAULT_ROUND_CAP
        rounds = 0
        outcome = "idle"
        while True:
            try:
                outcome = self.round()
            except KeyboardInterrupt:
                eprint("stopped. Nothing was recorded for the interrupted turns; `parley run` picks up where it left off.")
                return "interrupted"
            rounds += 1
            if outcome == "empty":
                eprint("the log is empty — start with: parley ask <participant> \"<the task>\"")
                if chair_turn and chair_turn():
                    continue
                return outcome
            if outcome in ("turns", "objections"):
                if self.dry_run:
                    return outcome
                if rounds >= cap:
                    eprint(f"round cap ({cap}) reached with participants still active; stopping so you can look. "
                           f"`parley status` shows the state; `parley run --rounds N` continues.")
                    print(self.chair_block(), flush=True)
                    return "capped"
                continue
            if outcome == "human":
                for p in self.waiting:
                    print(self.block_for(p["id"]), flush=True)
                if chair_turn and any(p["id"] == self.me for p in self.waiting) and chair_turn():
                    continue
                return outcome
            # quiescent: nothing owed, nobody nominated, nothing to object to
            print(self.chair_block(), flush=True)
            if chair_turn and chair_turn():
                continue
            return outcome
