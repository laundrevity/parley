"""The dispatcher: PROTOCOL.md §1 (rounds), §3 (floor), §3.5 (objection pass), §4.3
(timeouts and failed turns), §5 (projections), §7.5 (rebase).

One round: everyone who may speak is invoked concurrently, each with its own projection;
their turns are appended in arrival order; then the objection pass gives every agent with
unseen records one chance to object. When no agent may speak and the pass is quiet, it is
the chair's turn: the chair's block is printed and the dispatcher stops (or hands the
prompt to the chair in interactive mode).
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

sys.path.insert(0, core.TOOLS_DIR)
import project as P  # noqa: E402
import validate as V  # noqa: E402

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
        self.record_schema = V.load_json(os.path.join(core.SCHEMA_DIR, "record.schema.json"))

    # ------------------------------------------------------------------ helpers
    def agents(self) -> list:
        ps = self.parley.agents()
        return [p for p in ps if not self.only or p["id"] in self.only]

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
        chair = self.parley.chair
        # released once the chair has said anything after the failures
        for r in reversed(records):
            if r.id == failed_at:
                break
            if r.frm == chair:
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
        if p.get("harness") == "llama-server":
            sp = os.path.join(parley.dir, f"system-{pid}.txt")
            system_prompt = open(sp, encoding="utf-8").read() if os.path.exists(sp) else ""

        what = ("objection pass" if mode == "objection" else
                ("owes " + ", ".join("#" + x for x in owed) if owed else "nominated"))
        eprint(f"  {pid}: invoking ({what}; since #{since or 'start'})")
        parley.log_event(f"INVOKE {pid} mode={mode} since={since} last_visible={last_visible}")

        res = harness.run_turn(p, projection, wt, ps0, mode=mode, system_prompt=system_prompt,
                               record_schema=self.record_schema, repo=parley.repo)
        parley.log_event(f"REPLY {pid} elapsed={res.elapsed:.0f}s error={res.error!r} note={res.note!r}\n{res.raw}\n")

        if res.records is None:
            eprint(f"  {pid}: FAILED — {res.error}")
            self.fail(pid, last_visible, f"{pid}: {res.error}", owed)
            return 0

        appended = self.try_append(p, res, projection, last_visible, owed, mode)
        if appended is None:
            return 0
        self.with_state(lambda st: self._ok(parley.pstate(st, pid), last_visible, res.session_id))
        ids = [d["id"] for d in appended]
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
        def mark(st):
            ps = self.parley.pstate(st, pid)
            ps["failures"] = ps.get("failures", 0) + 1
            ps["failed_at"] = last_visible
            return ps["failures"]
        n = self.with_state(mark)
        self.note(text + (f" (obligations re {', '.join('#' + x for x in owed)} remain open)" if owed else ""), re_=owed)
        if n >= MAX_CONSECUTIVE_FAILURES:
            eprint(f"  {pid}: {n} consecutive failures; will not be invoked again until the chair speaks")

    def try_append(self, p: dict, res: harness.TurnResult, projection: str, last_visible, owed: list, mode: str):
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
                               "\n".join(f"- {f.code}: {f.msg}" for f in e.findings) +
                               "\nReply again with corrected records (same fence format).")
                    parley.log_event(f"REJECTED {pid} attempt {attempt}: {e}")
            if attempt == 1:
                eprint(f"  {pid}: retrying once — {problem.splitlines()[0][:100]}")
                ps0 = self.with_state(lambda st: dict(parley.pstate(st, pid)))
                res = harness.run_turn(p, projection + "\n\n" + problem, parley.worktree(p), ps0, mode=mode,
                                       record_schema=self.record_schema, repo=parley.repo)
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
        r = subprocess.run(["git", "rebase", base], cwd=wt, capture_output=True, text=True)
        if r.returncode == 0:
            return
        subprocess.run(["git", "rebase", "--abort"], cwd=wt, capture_output=True, text=True)
        files = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt,
                               capture_output=True, text=True).stdout.split()
        msg = (f"rebase of {p.get('branch', p['id'])} onto {base} conflicts"
               + (f" in {', '.join(files)}" if files else "") + "; resolve it first (rebase aborted).")
        eprint(f"  {p['id']}: {msg}")
        self.note(f"{p['id']}: {msg}", visibility=[p["id"]])

    # ------------------------------------------------------------------ rounds
    def invoke_all(self, plist: list, mode: str) -> int:
        if not plist:
            return 0
        with ThreadPoolExecutor(max_workers=len(plist)) as ex:
            return sum(ex.map(lambda p: self.take_turn(p, mode), plist))

    def round(self) -> str:
        parley = self.parley
        records = parley.records()
        if not records:
            return "empty"
        rp = parley.replay(records)
        st = parley.load_state()
        agents = [p for p in self.agents() if not self.halted(p, records, st)]
        parley.save_state(st)
        permitted = [p for p in agents if rp.open_for(p["id"]) or rp.nominations.get(p["id"])]
        if permitted:
            eprint(f"[turns] {', '.join(p['id'] for p in permitted)}")
            self.invoke_all(permitted, "turn")
            return "turns"
        if self.objection_pass:
            for n in range(self.max_passes):
                st = parley.load_state()
                cands = [p for p in agents
                         if parley.pstate(st, p["id"]).get("last_shown") != parley.last_visible_id(p["id"], records)]
                if not cands:
                    break
                eprint(f"[objection pass {n + 1}] {', '.join(p['id'] for p in cands)}")
                appended = self.invoke_all(cands, "objection")
                if appended or self.dry_run:
                    return "objections"
                records = parley.records()
        return "chair"

    def chair_block(self) -> str:
        records = self.parley.records()
        pj = P.Projector(records, self.parley.registry, self.parley.chair,
                         P.Git(self.parley.repo, self.git_enabled, self.parley.base_branch), False)
        return pj.build(None, None, True)

    def run(self, max_rounds: Optional[int] = None, chair_turn: Optional[Callable[[], bool]] = None) -> str:
        """Dispatch rounds until it is the chair's turn. max_rounds (default 20) caps the number
        of agent rounds between chair turns: a runaway agent–agent loop costs real invocations."""
        cap = max_rounds or DEFAULT_ROUND_CAP
        rounds = 0
        outcome = "idle"
        while True:
            outcome = self.round()
            rounds += 1
            if outcome == "empty":
                eprint("the log is empty — start with: parley ask <agent> \"<the task>\"")
                if chair_turn and chair_turn():
                    continue
                return outcome
            if outcome in ("turns", "objections"):
                if self.dry_run:
                    return outcome
                if rounds >= cap:
                    eprint(f"round cap ({cap}) reached with agents still active; stopping so you can look. "
                           f"`parley status` shows the state; `parley run --rounds N` continues.")
                    print(self.chair_block(), flush=True)
                    return "capped"
                continue
            # the chair's turn
            print(self.chair_block(), flush=True)
            if chair_turn and chair_turn():
                continue
            return outcome
