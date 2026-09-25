#!/usr/bin/env python3
"""parley projector: render the log for one participant as PROTOCOL.md §5 specifies.

Prints one user message: the visible delta since the participant's last turn (one
header line per record, then its body), followed by the standing block that says
exactly what the participant owes. Budgeted participants (context_budget in the
registry) get the summarized form (§5.3) with the standing block first.

usage:
  tools/project.py --for codex                        # .parley/ in cwd; since = seen of codex's last turn
  tools/project.py --for codex --since 009            # explicit (e.g. after an objection pass that emitted nothing)
  tools/project.py --for codex --since none           # cold join: the whole visible log
  tools/project.py --for conor                        # the chair's block (queue, asks, stuck, flags)
  tools/project.py --for codex --parley path/to/dir   # dir with log.jsonl + participants.json
  tools/project.py --for codex --log transcripts/example-01.jsonl   # registry: <stem>.participants.json

options:
  --ts            include timestamps in headers
  --repo DIR      git repository used for change stats and inline diffs (default: cwd); --no-git disables
  --budget N      override context_budget (tokens; ~4 chars each)
  --standing-only print only the standing block

The standing block is computed by replaying the participant's *visible* records
through validate.Replay, so it cannot disagree with tools/validate.py.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate as V  # noqa: E402

STAR = ["*"]
CHARS_PER_TOKEN = 4


# --------------------------------------------------------------------------- helpers

def parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_age(older: str, newer: str) -> str:
    a, b = parse_ts(older), parse_ts(newer)
    if not a or not b:
        return "?"
    s = int((b - a).total_seconds())
    if s < 3600:
        return f"{max(s // 60, 1)}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def ids(xs) -> str:
    return ",".join("#" + x for x in xs)


def visible_to(r: V.Rec, pid: str, chair: str) -> bool:
    return pid == chair or r.visibility == STAR or pid in r.visibility


class Git:
    """Best-effort stats and diffs. Every failure degrades to 'no stat'."""

    def __init__(self, repo: str, enabled: bool, base: str):
        self.repo, self.enabled, self.base = repo, enabled, base

    def _run(self, *args) -> str | None:
        if not self.enabled:
            return None
        try:
            p = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, timeout=20)
        except Exception:
            return None
        return p.stdout if p.returncode == 0 else None

    def reachable(self, commit: str) -> bool:
        return self._run("rev-parse", "--verify", "--quiet", commit + "^{commit}") is not None

    def diff(self, commit: str, files: list | None) -> str | None:
        if not self.reachable(commit):
            return None
        rng = f"{self.base}...{commit}"
        args = ["diff", rng]
        if files:
            args += ["--", *files]
        out = self._run(*args)
        if out is None:  # no merge base with main: show the commit itself
            args = ["show", "--format=", commit]
            if files:
                args += ["--", *files]
            out = self._run(*args)
        return out

    @staticmethod
    def stat(diff_text: str) -> tuple[int, int]:
        add = dele = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                add += 1
            elif line.startswith("-") and not line.startswith("---"):
                dele += 1
        return add, dele


# --------------------------------------------------------------------------- rendering

class Projector:
    def __init__(self, records: list, registry: dict, pid: str, git: Git, show_ts: bool):
        self.parts = {p["id"]: p for p in registry["participants"]}
        if pid not in self.parts:
            V.die(f"'{pid}' is not in the registry ({', '.join(self.parts)})")
        self.pid = pid
        self.me = self.parts[pid]
        chairs = [p["id"] for p in registry["participants"] if p.get("role") == "chair"]
        self.chair = chairs[0] if chairs else None
        self.is_chair = pid == self.chair
        self.git, self.show_ts = git, show_ts
        self.all = records
        self.visible = [r for r in records if visible_to(r, pid, self.chair)]
        self.pos = {r.id: r.pos for r in records}
        # replay the visible records only (PROTOCOL §5.1: filter first, then replay)
        parts = [V.Participant(p["id"], p.get("kind", "?"), p.get("role", "member"),
                               p.get("latency", "minutes"), p.get("capabilities", []))
                 for p in registry["participants"]]
        self.rp = V.Replay(parts, [])
        for r in self.visible:
            self.rp.feed(r)
        caps = self.me.get("capabilities", [])
        self.new_after_pos = None
        self.has_repo = "repo" in caps
        self.cap = self.me.get("inline_diff_lines", 80 if self.has_repo else 0)

    # -- since / delta
    def default_since(self):
        mine = [r for r in self.visible if r.frm == self.pid]
        if not mine:
            return None
        last = mine[-1]
        return last.seen  # the projection that produced the last turn ended here

    def delta(self, since):
        if since is None:
            return list(self.visible)
        if since not in self.pos:
            V.die(f"--since #{since} is not a record in the log")
        p = self.pos[since]
        return [r for r in self.visible if r.pos > p]

    # -- one record
    def header(self, r: V.Rec) -> str:
        h = f"[#{r.id}]"
        if r.frm == self.pid:
            h += " (self)"
        if self.new_after_pos is not None and r.pos > self.new_after_pos:
            h += " (new)"
        h += f" {r.kind} from={r.frm} to={'*' if r.to == STAR else ','.join(r.to)}"
        if r.re:
            h += f" re={ids(r.re)}"
        h += f" thread=#{r.thread}"
        if r.next:
            h += f" next={','.join(r.next)}"
        if r.visibility != STAR:
            h += f" vis={','.join(r.visibility)}"
        if self.show_ts:
            h += f" ts={r.ts}"
        return h

    def body(self, r: V.Rec) -> list:
        b, out = r.body, []
        if b.get("quote"):
            out += ["> " + line for line in str(b["quote"]).splitlines()]
        if b.get("reasons"):
            out.append("reasons:")
            out += [f"  - {x}" for x in b["reasons"]]
        if b.get("text"):
            out.append(str(b["text"]))
        if b.get("change"):
            out += self.change(b["change"])
        return out

    def change(self, c: dict) -> list:
        branch, commit, files, diff = c.get("branch"), c.get("commit"), c.get("files") or [], c.get("diff")
        ref = (f"{branch}@{commit}" if branch and commit else branch or commit or "?")
        if c.get("repo"):
            ref = f"{c['repo']}:{ref}"
        diff_text = diff
        if diff_text is None and commit:
            diff_text = self.git.diff(commit, files)
        stat = ""
        if diff_text is not None:
            a, d = Git.stat(diff_text)
            stat = f" (+{a}/−{d})"
        line = f"change: {ref}{stat}"
        if files:
            line += " files: " + ", ".join(files)
        out = [line]
        if diff_text is not None:
            lines = diff_text.rstrip("\n").splitlines()
            if self.cap and len(lines) <= self.cap:
                out += ["```diff", *lines, "```"]
            elif diff is not None and not commit:
                # inline-only diff (nothing to point at): show the head, name the cut
                if self.cap:
                    out += ["```diff", *lines[: self.cap], "```", f"(truncated: {len(lines) - self.cap} more lines; over your cap of {self.cap})"]
                else:
                    out.append(f"(diff not shown: {len(lines)} lines; your cap is 0)")
            else:
                out.append(self.pointer(branch, commit, files, len(lines)))
        elif commit:
            out.append(self.pointer(branch, commit, files, None))
        return out

    def pointer(self, branch, commit, files, n) -> str:
        if self.has_repo:
            tail = f" -- {' '.join(files)}" if files else ""
            if branch and branch != self.git.base:
                return f"→ git diff {self.git.base}...{branch}{tail}"
            return f"→ git show {commit}{tail}"      # landed on the base branch, or branch unknown
        size = f"{n} lines; " if n is not None else ""
        return f"(diff not shown: {size}you have no repo access — review the text and the stat)"

    def render(self, r: V.Rec) -> str:
        return "\n".join([self.header(r), *self.body(r)])

    # -- standing
    def my_obligations(self):
        return [o for o in self.rp.obligations.values() if o.party == self.pid]

    def thread_line(self, root: str) -> str:
        t = self.rp.threads[root]
        if t.closed:
            return f"#{root} closed by #{t.closed_by}"
        state = "ready" if root in self.rp.chair_queue else "open"
        s = f"#{root} {state}"
        if t.proposals:
            parts = []
            for pid_, p in t.proposals.items():
                st = p.state(False)
                if st == "pending":
                    who = "you" if self.pid in p.open else ",".join(sorted(p.open))
                    st = f"pending {who}"
                elif st == "contested":
                    st = f"contested by {','.join(sorted(p.objects))}"
                elif st == "accepted":
                    st = f"accepted by {','.join(sorted(p.accepts)) or 'nobody (no reviewers)'}"
                parts.append(f"#{pid_} {st}")
            s += " — proposals: " + "; ".join(parts)
        return s

    def standing_agent(self, delta: list, last_id: str | None, dropped: list) -> str:
        out = [f"--- standing: you are {self.pid} · log through #{last_id or 'none'} ---"]
        obs = self.my_obligations()
        if obs:
            out.append("you owe:")
            for o in sorted(obs, key=lambda o: o.created_pos):
                src = self.rp.recs[o.rec]
                what = "reply" if o.kind == "answer" else "accept|object"
                out.append(f"  - {what} re #{o.rec} ({src.kind} from {src.frm}, thread #{o.thread})")
        else:
            out.append("you owe: nothing")
        noms = self.rp.nominations.get(self.pid) or []
        out.append("nominated by: " + (ids(sorted(set(noms))) if noms else "no one"))
        roots = []
        for r in delta:
            if r.thread not in roots:
                roots.append(r.thread)
        if roots:
            out.append("threads in this delta:")
            out += [f"  {self.thread_line(t)}" for t in roots if t in self.rp.threads]
        if dropped:
            out.append("dropped from this projection (over budget): " + ids(dropped))
        if obs:
            out.append("you may: respond to what you owe (and anything else you have the floor for)")
        elif noms:
            out.append("you may: speak — you are nominated; or emit an empty fence")
        else:
            out.append("you may: object, or emit an empty fence — you owe nothing and are not nominated")
        out.append("reply: JSONL records in one ```jsonl fence, nothing else. Omit id/ts/seen.")
        out.append('       Every record you owe on must appear in some record\'s re. New thread: id and thread both "tmp-<n>".')
        return "\n".join(out)

    def standing_chair(self, delta: list, last_id: str | None) -> str:
        rp = self.rp
        agent_obs = [o for o in rp.obligations.values() if o.party != self.chair]
        live_noms = {p: sorted(set(v)) for p, v in rp.nominations.items() if v and p != self.chair}
        if not agent_obs and not live_noms:
            floor = "quiet"
        else:
            bits = [f"{o.party} owes {'reply' if o.kind == 'answer' else 'review'} re #{o.rec}" for o in agent_obs]
            bits += [f"{p} nominated by {ids(v)}" for p, v in live_noms.items()]
            floor = "; ".join(bits)
        out = [f"--- standing: chair · log through #{last_id or 'none'} · floor: {floor} ---"]
        if rp.chair_queue:
            out.append("decide:")
            for root in sorted(rp.chair_queue, key=lambda x: self.pos.get(x, 0)):
                out.append(f"  {self.thread_line(root)}")
        else:
            out.append("decide: nothing ready")
        asks = [o for o in rp.obligations.values() if o.party == self.chair]
        if asks:
            out.append("asks to you:")
            for o in asks:
                src = rp.recs[o.rec]
                out.append(f"  - #{o.rec} from {src.frm} (thread #{o.thread}): {str(src.body.get('text', ''))[:100]}")
        else:
            out.append("asks to you: none")
        newest = rp.order[-1].ts if rp.order else ""
        if agent_obs:
            out.append("stuck:")
            for o in sorted(agent_obs, key=lambda o: o.created_pos):
                src = rp.recs[o.rec]
                out.append(f"  - {o.party} owes {'reply' if o.kind == 'answer' else 'review'} re #{o.rec} since {fmt_age(src.ts, newest)}")
        else:
            out.append("stuck: none")
        closed = []
        for r in delta:
            if r.kind == "decide" and r.thread in rp.threads and rp.threads[r.thread].closed:
                t = rp.threads[r.thread]
                n_obj = sum(1 for p in t.proposals.values() if p.objects)
                tag = " D2" if t.proposals and n_obj == 0 else ""
                closed.append(f"#{r.thread} closed by #{r.id} ({n_obj} of {len(t.proposals)} proposals objected to){tag}")
        out.append("flags: " + ("; ".join(closed) if closed else "none"))
        return "\n".join(out)

    # -- assembly
    def build(self, since, budget: int | None, standing_only: bool, new_after=None) -> str:
        """new_after: in a full-log projection (since=None), mark records after this id as (new)."""
        if new_after is not None and new_after in self.pos:
            self.new_after_pos = self.pos[new_after]
        delta = self.delta(since)
        last_id = self.visible[-1].id if self.visible else None
        if self.is_chair:
            block = self.standing_chair(delta, last_id)
            if standing_only:
                return block
            body = "\n\n".join(self.render(r) for r in delta)
            head = f"parley · projection for {self.pid} (chair) · {len(delta)} record(s) since #{since or 'start'}"
            return "\n\n".join(x for x in [head, body, block] if x)
        if budget:
            return self.build_budgeted(delta, last_id, budget, standing_only)
        block = self.standing_agent(delta, last_id, [])
        if standing_only:
            return block
        body = "\n\n".join(self.render(r) for r in delta)
        head = f"parley · projection for {self.pid} · {len(delta)} record(s) since #{since or 'start'}"
        return "\n\n".join(x for x in [head, body, block] if x)

    def build_budgeted(self, delta: list, last_id: str | None, budget: int, standing_only: bool) -> str:
        """PROTOCOL §5.3: standing block first; owed/addressed records verbatim (+ one hop of
        context); other threads as their newest dispatch summary; drop oldest summaries first."""
        owed_ids = {o.rec for o in self.my_obligations()}
        verbatim = [r for r in delta if r.id in owed_ids or (r.to != STAR and self.pid in r.to)]
        vids = {r.id for r in verbatim}
        context = []
        for r in verbatim:
            for t in r.re:
                if t in self.rp.recs and t not in vids and self.rp.recs[t] not in context:
                    context.append(self.rp.recs[t])
        other_threads = []
        for r in delta:
            if r.id not in vids and r.thread not in other_threads and r.thread not in {v.thread for v in verbatim}:
                other_threads.append(r.thread)
        summaries = []
        for root in other_threads:
            recs = [r for r in self.visible if r.thread == root]
            summ = [r for r in recs if self.parts.get(r.frm, {}).get("kind") == "tool" and r.kind == "say" and root in r.re]
            if summ:
                s = summ[-1]
                summaries.append((root, f"[summary of thread #{root} by {s.frm}, #{s.id}]\n{s.body.get('text', '')}"))
            else:
                head = recs[0] if recs else self.rp.recs.get(root)
                text = str(head.body.get("text", ""))[:200] if head else ""
                summaries.append((root, f"[thread #{root}: no summary record yet; root {self.header(head) if head else '?'}]\n{text}"))
        dropped = []
        def assemble():
            parts = [f"parley · projection for {self.pid} (budgeted) · {len(delta)} record(s) in delta"]
            parts.append(self.standing_agent(delta, last_id, dropped))
            if verbatim:
                parts.append("records you owe on or that address you:\n\n" + "\n\n".join(self.render(r) for r in verbatim))
            if context:
                parts.append("context (what they respond to):\n\n" + "\n\n".join(self.header(r) + "\n" + str(r.body.get("text", ""))[:300] for r in context))
            if summaries:
                parts.append("other threads:\n\n" + "\n\n".join(s for _, s in summaries))
            return "\n\n".join(parts)
        text = assemble()
        while len(text) > budget * CHARS_PER_TOKEN and summaries:
            root, _ = summaries.pop(0)
            dropped.append(root)
            text = assemble()
        if len(text) > budget * CHARS_PER_TOKEN:
            text += f"\n\n(over budget by ~{(len(text) - budget * CHARS_PER_TOKEN) // CHARS_PER_TOKEN} tokens even with all other threads dropped; nothing cut mid-record)"
        return self.standing_agent(delta, last_id, dropped) if standing_only else text


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--for", dest="pid", required=True, help="participant id")
    ap.add_argument("--since", help="record id, or 'none' for the whole visible log; default: seen of your last turn")
    ap.add_argument("--parley", help="directory with log.jsonl + participants.json (default .parley)")
    ap.add_argument("--log")
    ap.add_argument("--participants")
    ap.add_argument("--repo", default=os.getcwd())
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--ts", action="store_true")
    ap.add_argument("--standing-only", action="store_true")
    ap.add_argument("--new-after", help="with --since none: mark records after this id as (new)")
    a = ap.parse_args(argv)

    target = a.log or a.parley or ".parley"
    log_path, reg_path = V.resolve_inputs(target, a.participants)
    findings: list = []
    registry = V.load_json(reg_path)
    raws = V.load_log(log_path, findings)
    records = [r for r in (V.to_rec(n, o) for n, o in raws) if r is not None]

    chair = next((p for p in registry["participants"] if p.get("role") == "chair"), {})
    git = Git(a.repo, not a.no_git, chair.get("branch", "main"))
    pj = Projector(records, registry, a.pid, git, a.ts)

    if a.since is None:
        since = pj.default_since()
    elif a.since.lower() == "none":
        since = None
    else:
        since = a.since.lstrip("#")
    budget = a.budget or pj.me.get("context_budget")
    print(pj.build(since, budget, a.standing_only, a.new_after))
    return 0


if __name__ == "__main__":
    sys.exit(main())
