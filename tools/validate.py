#!/usr/bin/env python3
"""parley validator: shape-check a log against schema/record.schema.json and
replay the protocol (obligations, floor, threads) as PROTOCOL.md defines it.

usage:
  tools/validate.py <parley-dir>                    # dir with log.jsonl + participants.json
  tools/validate.py <log.jsonl> [--participants P]  # P defaults to <stem>.participants.json,
                                                    # then participants.json beside the log
options:
  --schema-dir DIR   where record.schema.json / participants.schema.json live
                     (default: ../schema relative to this file)
  --report           print thread states, open obligations and the chair's queue
  --json             machine-readable output instead of text
  --quiet            violations only

exit status: 0 clean, 1 violations, 2 usage or I/O error.

Shape checking uses the `jsonschema` package when importable (3.2+ is enough;
the schemas avoid keywords newer than draft 7). Without it, a minimal built-in
shape check runs and says so. The protocol replay is stdlib only and always runs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

KINDS = ("say", "ask", "propose", "accept", "object", "decide")
STAR = ["*"]


# --------------------------------------------------------------------------- data

@dataclass
class Participant:
    id: str
    kind: str
    role: str = "member"
    latency: str = "minutes"
    capabilities: list = field(default_factory=list)


@dataclass
class Rec:
    pos: int            # 1-based line number in the log
    id: str
    ts: str
    frm: str
    to: list
    re: list
    thread: str
    kind: str
    body: dict
    next: list
    seen: Optional[str]
    visibility: list
    raw: dict


@dataclass
class Proposal:
    id: str
    author: str
    thread: str
    reviewers: set            # who owes accept|object
    open: set                 # reviewers who have not yet discharged
    accepts: set = field(default_factory=set)
    objects: set = field(default_factory=set)

    def state(self, closed: bool) -> str:
        if closed:
            return "decided"
        if self.objects:
            return "contested"
        if self.open:
            return "pending"
        return "accepted"


@dataclass
class Thread:
    root: str
    closed_by: Optional[str] = None
    proposals: dict = field(default_factory=dict)   # id -> Proposal

    @property
    def closed(self) -> bool:
        return self.closed_by is not None


@dataclass
class Obligation:
    rec: str          # the ask / propose that created it
    party: str
    kind: str         # 'answer' | 'review'
    thread: str
    created_pos: int


@dataclass
class Finding:
    level: str        # 'violation' | 'warning' | 'info'
    rec: Optional[str]
    code: str
    msg: str

    def __str__(self) -> str:
        where = f"#{self.rec} " if self.rec else ""
        return f"{self.level.upper():9} {where}{self.code}: {self.msg}"


# --------------------------------------------------------------------------- loading

def die(msg: str, code: int = 2) -> None:
    print(f"validate.py: {msg}", file=sys.stderr)
    sys.exit(code)


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_inputs(target: str, participants: Optional[str]):
    if os.path.isdir(target):
        log = os.path.join(target, "log.jsonl")
        reg = participants or os.path.join(target, "participants.json")
    else:
        log = target
        if participants:
            reg = participants
        else:
            d, base = os.path.split(log)
            stem = base[:-len(".jsonl")] if base.endswith(".jsonl") else base
            cand = [os.path.join(d, stem + ".participants.json"), os.path.join(d, "participants.json")]
            reg = next((c for c in cand if os.path.exists(c)), cand[0])
    if not os.path.exists(log):
        die(f"log not found: {log}")
    if not os.path.exists(reg):
        die(f"participant registry not found: {reg}")
    return log, reg


def load_log(path: str, findings: list) -> list:
    raws = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                findings.append(Finding("warning", None, "L0", f"line {n} is blank"))
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                findings.append(Finding("violation", None, "L1", f"line {n} is not JSON: {e}"))
                continue
            if not isinstance(obj, dict):
                findings.append(Finding("violation", None, "L1", f"line {n} is not an object"))
                continue
            raws.append((n, obj))
    return raws


# --------------------------------------------------------------------------- shape

def shape_check(schema_dir: str, raws: list, registry: dict, findings: list) -> str:
    """Returns a one-line description of how shape checking was done."""
    rec_schema = load_json(os.path.join(schema_dir, "record.schema.json"))
    reg_schema = load_json(os.path.join(schema_dir, "participants.schema.json"))
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return _builtin_shape_check(raws, registry, findings)

    # The schemas declare draft 2020-12 but use no keyword newer than draft 7, so an
    # older jsonschema (3.x, Draft7Validator) checks them exactly.
    cls = getattr(jsonschema, "Draft202012Validator", None) or jsonschema.Draft7Validator
    cls.check_schema(rec_schema)
    rv = cls(rec_schema)
    for n, obj in raws:
        rid = obj.get("id") if isinstance(obj.get("id"), str) else None
        for err in sorted(rv.iter_errors(obj), key=lambda e: list(e.path)):
            path = "/".join(str(p) for p in err.path) or "(record)"
            findings.append(Finding("violation", rid, "S1", f"line {n} {path}: {err.message}"))
    cls.check_schema(reg_schema)
    for err in cls(reg_schema).iter_errors(registry):
        path = "/".join(str(p) for p in err.path) or "(registry)"
        findings.append(Finding("violation", None, "S2", f"participants.json {path}: {err.message}"))
    try:
        from importlib.metadata import version as _pkg_version
        ver = _pkg_version("jsonschema")
    except Exception:  # pragma: no cover
        ver = "?"
    return f"shape: jsonschema {ver} ({cls.__name__})"


def _builtin_shape_check(raws: list, registry: dict, findings: list) -> str:
    """Enough to catch the common hand-authoring mistakes. Not a JSON Schema engine."""
    body_required = {
        "say": ["text"], "ask": ["text"], "propose": ["text"],
        "accept": ["reasons"], "object": ["reasons"], "decide": ["text"],
    }
    top_allowed = {"id", "ts", "from", "to", "re", "thread", "kind", "body", "next", "seen", "visibility"}
    for n, obj in raws:
        rid = obj.get("id") if isinstance(obj.get("id"), str) else None
        for k in ("id", "ts", "from", "thread", "kind", "body"):
            if k not in obj:
                findings.append(Finding("violation", rid, "S1", f"line {n}: missing required field '{k}'"))
        extra = set(obj) - top_allowed
        if extra:
            findings.append(Finding("violation", rid, "S1", f"line {n}: unknown fields {sorted(extra)}"))
        kind = obj.get("kind")
        if kind not in KINDS:
            findings.append(Finding("violation", rid, "S1", f"line {n}: kind {kind!r} not in {KINDS}"))
            continue
        body = obj.get("body")
        if not isinstance(body, dict):
            findings.append(Finding("violation", rid, "S1", f"line {n}: body must be an object"))
            continue
        for k in body_required[kind]:
            if k not in body:
                findings.append(Finding("violation", rid, "S1", f"line {n}: {kind} body requires '{k}'"))
        if "reasons" in body and (not isinstance(body["reasons"], list) or not body["reasons"]):
            findings.append(Finding("violation", rid, "S1", f"line {n}: reasons must be a non-empty array"))
        if kind == "ask" and "to" not in obj:
            findings.append(Finding("violation", rid, "S1", f"line {n}: ask requires explicit 'to'"))
        if kind in ("accept", "object") and not obj.get("re"):
            findings.append(Finding("violation", rid, "S1", f"line {n}: {kind} requires non-empty 're'"))
    if not isinstance(registry.get("participants"), list):
        findings.append(Finding("violation", None, "S2", "participants.json: 'participants' must be an array"))
    return "shape: built-in minimal check (install `jsonschema` for the full schema)"


# --------------------------------------------------------------------------- replay

class Replay:
    def __init__(self, participants: list, findings: list):
        self.f = findings
        self.parts = {p.id: p for p in participants}
        chairs = [p for p in participants if p.role == "chair"]
        if len(chairs) != 1:
            self.f.append(Finding("violation", None, "P1", f"exactly one chair required, found {len(chairs)}"))
        self.chair = chairs[0].id if chairs else None
        if chairs and chairs[0].kind != "human":
            self.f.append(Finding("violation", None, "P2", "the chair must be a human participant"))
        self.tools = {p.id for p in participants if p.kind == "tool"}

        self.recs: dict[str, Rec] = {}
        self.order: list[Rec] = []
        self.threads: dict[str, Thread] = {}
        self.obligations: dict[tuple, Obligation] = {}
        self.nominations: dict[str, list] = defaultdict(list)
        self.chair_queue: set = set()
        self.turns: list = []          # dicts: author, first, last, seen, basis
        self._cur_turn = None

    # -- helpers
    def viewers(self, r: Rec) -> set:
        if r.visibility == STAR:
            return set(self.parts)
        return set(r.visibility) | ({self.chair} if self.chair else set())

    def expand(self, aud: list, author: str) -> set:
        if aud == STAR:
            return set(self.parts) - {author}
        return set(aud)

    def open_for(self, party: str) -> list:
        return [o for o in self.obligations.values() if o.party == party]

    def thread_open_nonchair(self, root: str) -> list:
        return [o for o in self.obligations.values() if o.thread == root and o.party != self.chair]

    def recompute_ready(self, root: str) -> None:
        t = self.threads.get(root)
        if not t or t.closed or not t.proposals or self.thread_open_nonchair(root):
            self.chair_queue.discard(root)
        else:
            self.chair_queue.add(root)

    # -- main
    def feed(self, r: Rec) -> None:
        v = lambda code, msg: self.f.append(Finding("violation", r.id, code, msg))
        w = lambda code, msg: self.f.append(Finding("warning", r.id, code, msg))

        # R1..R9 structural
        if r.id in self.recs:
            v("R1", f"duplicate id (first at line {self.recs[r.id].pos})")
        if r.frm not in self.parts:
            v("R3", f"author '{r.frm}' is not in the registry")
        for fld, ids in (("to", r.to), ("next", r.next), ("visibility", r.visibility)):
            if ids == STAR:
                continue
            for p in ids:
                if p not in self.parts:
                    v("R4", f"{fld} names unknown participant '{p}'")
        if r.to != STAR and r.frm in r.to:
            v("R4", "author cannot address itself")
        if r.frm in r.next:
            w("R4", "author nominated itself; ignored")
        for t in r.re:
            if t not in self.recs:
                v("R5", f"re #{t} does not exist earlier in the log")
        seen_pos = None
        if r.seen is not None:
            if r.seen not in self.recs:
                v("R8", f"seen #{r.seen} is not an earlier record")
            else:
                seen_pos = self.recs[r.seen].pos
        vis = self.viewers(r)
        if r.visibility != STAR:
            for p in self.expand(r.to, r.frm) if r.to != STAR else set():
                if p not in vis:
                    v("R9", f"addressee '{p}' cannot see this record")
            for p in r.next:
                if p not in vis:
                    v("R9", f"nominee '{p}' cannot see this record")
            if r.frm not in vis:
                w("R9", "author is not among the viewers of its own record")

        is_root = r.thread == r.id
        th = self.threads.get(r.thread)
        if is_root:
            if th is not None:
                v("R6", "thread root id already used")
            th = Thread(root=r.id)
            self.threads[r.id] = th
        else:
            if th is None:
                v("R6", f"thread #{r.thread} is not an existing thread root")
                th = Thread(root=r.thread)          # keep going
                self.threads[r.thread] = th
            elif th.closed and r.kind != "say":
                v("R7", f"thread #{r.thread} was closed by #{th.closed_by}; only say is allowed. Open a new thread.")

        # K rules
        if r.kind == "decide" and r.frm != self.chair:
            v("K1", f"only the chair ({self.chair}) may decide")
        if r.kind == "accept" and not any(t in self.recs and self.recs[t].kind == "propose" for t in r.re):
            v("K2", "accept must re at least one propose")
        if r.kind == "decide" and th.closed:
            v("K3", f"thread already closed by #{th.closed_by}")
        if r.frm in self.tools and r.kind != "say":
            v("K5", "tool participants may only say")

        # F: floor, evaluated per turn. A turn is a contiguous run of records with the
        # same author and the same `seen` (an author's next turn always has a later
        # `seen`, because its projection includes its own previous records).
        if self._cur_turn and self._cur_turn["author"] == r.frm and self._cur_turn["seen"] == r.seen:
            turn = self._cur_turn
            turn["last"] = r.pos
            if turn["basis"] == ["objection"]:
                first = self.recs[turn["first_id"]]
                if r.thread != first.thread and not (set(r.re) & ({first.id} | set(first.re))):
                    v("F2", "an unbidden turn may only contain the objection and records responding to it")
        else:
            basis = []
            if r.frm == self.chair:
                basis.append("chair")
            elif r.frm in self.tools:
                basis.append("tool")
            else:
                if self.open_for(r.frm):
                    basis.append("obligation")
                if self.nominations.get(r.frm):
                    basis.append("nomination")
                if r.kind == "object":
                    basis.append("objection")
                if not basis:
                    v("F1", f"'{r.frm}' had no floor: nothing owed, not nominated, and not objecting")
            self.nominations[r.frm] = []          # nominations are consumed by taking a turn
            turn = {"author": r.frm, "first": r.pos, "first_id": r.id, "last": r.pos,
                    "seen": r.seen, "seen_pos": seen_pos, "basis": basis, "unseen": []}
            self.turns.append(turn)
            self._cur_turn = turn
            # concurrency: visible records after `seen` and before this turn, by others
            lo = seen_pos or 0
            for prev in self.order:
                if prev.pos > lo and prev.frm != r.frm and r.frm in self.viewers(prev):
                    turn["unseen"].append(prev.id)
            if turn["unseen"]:
                self.f.append(Finding("info", r.id, "C1",
                                      f"turn by {r.frm} did not see " + ", ".join("#" + x for x in turn["unseen"])))

        # register
        self.recs[r.id] = r
        self.order.append(r)

        # effects: obligations created
        if r.kind == "ask":
            addressees = self.expand(r.to, r.frm) - self.tools
            if r.to == STAR and self.chair:
                addressees.discard(self.chair)
            for p in addressees:
                self.obligations[(r.id, p)] = Obligation(r.id, p, "answer", r.thread, r.pos)
        elif r.kind == "propose":
            reviewers = self.expand(r.to, r.frm) - self.tools - ({self.chair} if self.chair else set())
            if not reviewers:
                w("O1", "propose has no reviewers; it is ready immediately")
            th.proposals[r.id] = Proposal(r.id, r.frm, r.thread, set(reviewers), set(reviewers))
            for p in reviewers:
                self.obligations[(r.id, p)] = Obligation(r.id, p, "review", r.thread, r.pos)

        # effects: discharge
        for t in r.re:
            tgt = self.recs.get(t)
            if tgt is None:
                continue
            key = (t, r.frm)
            ob = self.obligations.get(key)
            if ob and ob.kind == "answer" and r.kind != "ask":
                del self.obligations[key]
            if tgt.kind == "propose":
                prop = self.threads[tgt.thread].proposals.get(t)
                if prop and r.kind in ("accept", "object"):
                    (prop.accepts if r.kind == "accept" else prop.objects).add(r.frm)
                    if r.frm in prop.open:
                        prop.open.discard(r.frm)
                        self.obligations.pop(key, None)
                    elif r.kind == "accept":
                        self.f.append(Finding("info", r.id, "O2", f"accept from non-reviewer '{r.frm}' recorded, discharges nothing"))
            if tgt.kind == "ask" and r.kind == "ask" and key in self.obligations:
                self.f.append(Finding("info", r.id, "O3", f"counter-question: obligation re #{t} stays open"))

        # effects: decide
        if r.kind == "decide" and r.frm == self.chair and not th.closed:
            th.closed_by = r.id
            voided = [k for k, o in self.obligations.items() if o.thread == r.thread]
            for k in voided:
                del self.obligations[k]
            if voided:
                self.f.append(Finding("info", r.id, "D1", f"decide voided {len(voided)} open obligation(s) in thread #{r.thread}"))
            if not any(p.objects for p in th.proposals.values()) and th.proposals:
                self.f.append(Finding("warning", r.id, "D2", f"thread #{r.thread} closed with zero objections across {len(th.proposals)} proposal(s) — agreement-collapse check"))

        # effects: nominations
        nominees = [p for p in r.next if p != r.frm]
        if r.kind == "object" and not r.next:
            nominees = sorted({self.recs[t].frm for t in r.re if t in self.recs and self.recs[t].frm != r.frm})
        for p in nominees:
            if p in self.parts:
                self.nominations[p].append(r.id)

        # thread readiness
        self.recompute_ready(r.thread)
        for t in r.re:
            if t in self.recs:
                self.recompute_ready(self.recs[t].thread)

    # -- report
    def report(self) -> dict:
        threads = {}
        for root, t in self.threads.items():
            threads[root] = {
                "state": "closed" if t.closed else ("ready" if root in self.chair_queue else "open"),
                "closed_by": t.closed_by,
                "proposals": {pid: {"author": p.author, "state": p.state(t.closed),
                                    "accepts": sorted(p.accepts), "objects": sorted(p.objects),
                                    "awaiting": sorted(p.open)} for pid, p in t.proposals.items()},
            }
        open_obs = defaultdict(list)
        for o in self.obligations.values():
            open_obs[o.party].append({"re": o.rec, "kind": o.kind, "thread": o.thread})
        floor = {p: sorted(set(ids)) for p, ids in self.nominations.items() if ids}
        counts = defaultdict(int)
        for r in self.order:
            counts[r.kind] += 1
        unbidden = [t["first_id"] for t in self.turns if t["basis"] == ["objection"]]
        return {
            "records": len(self.order),
            "kinds": dict(counts),
            "turns": len(self.turns),
            "unbidden_objections": unbidden,
            "threads": threads,
            "open_obligations": dict(open_obs),
            "chair_queue": sorted(self.chair_queue),
            "live_nominations": floor,
        }


# --------------------------------------------------------------------------- main

def to_rec(n: int, obj: dict) -> Optional[Rec]:
    try:
        return Rec(
            pos=n, id=str(obj["id"]), ts=str(obj.get("ts", "")), frm=str(obj["from"]),
            to=obj.get("to", STAR), re=list(obj.get("re", [])), thread=str(obj["thread"]),
            kind=str(obj["kind"]), body=obj.get("body", {}), next=list(obj.get("next", [])),
            seen=obj.get("seen"), visibility=obj.get("visibility", STAR), raw=obj,
        )
    except (KeyError, TypeError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="parley directory or log.jsonl")
    ap.add_argument("--participants")
    ap.add_argument("--schema-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "schema"))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    log_path, reg_path = resolve_inputs(a.target, a.participants)
    findings: list[Finding] = []
    registry = load_json(reg_path)
    raws = load_log(log_path, findings)
    how = shape_check(a.schema_dir, raws, registry, findings)

    parts = []
    for p in registry.get("participants", []):
        if isinstance(p, dict) and "id" in p:
            parts.append(Participant(p["id"], p.get("kind", "?"), p.get("role", "member"),
                                     p.get("latency", "minutes"), p.get("capabilities", [])))
    rp = Replay(parts, findings)
    for n, obj in raws:
        r = to_rec(n, obj)
        if r is None:
            findings.append(Finding("violation", None, "S1", f"line {n}: unusable record (missing id/from/thread/kind)"))
            continue
        rp.feed(r)

    violations = [f for f in findings if f.level == "violation"]
    if a.json:
        out = {"log": log_path, "participants": reg_path, "shape": how,
               "findings": [f.__dict__ for f in findings], "ok": not violations}
        if a.report:
            out["report"] = rp.report()
        print(json.dumps(out, indent=2))
        return 1 if violations else 0

    if not a.quiet:
        print(f"{log_path}: {len(raws)} records, {len(rp.turns)} turns · {how}")
    for f in findings:
        if a.quiet and f.level != "violation":
            continue
        print(" ", f)
    if a.report:
        rep = rp.report()
        print("\n--- report ---")
        print("kinds:", ", ".join(f"{k}={v}" for k, v in sorted(rep["kinds"].items())))
        print("unbidden objections:", ", ".join("#" + x for x in rep["unbidden_objections"]) or "none")
        for root, t in rep["threads"].items():
            line = f"thread #{root}: {t['state']}" + (f" (by #{t['closed_by']})" if t["closed_by"] else "")
            print(line)
            for pid, p in t["proposals"].items():
                extra = []
                if p["accepts"]:
                    extra.append("accepts=" + ",".join(p["accepts"]))
                if p["objects"]:
                    extra.append("objects=" + ",".join(p["objects"]))
                if p["awaiting"]:
                    extra.append("awaiting=" + ",".join(p["awaiting"]))
                print(f"    propose #{pid} by {p['author']}: {p['state']}" + (" · " + " ".join(extra) if extra else ""))
        if rep["open_obligations"]:
            print("open obligations:")
            for party, obs in rep["open_obligations"].items():
                for o in obs:
                    print(f"    {party} owes {o['kind']} re #{o['re']} (thread #{o['thread']})")
        else:
            print("open obligations: none")
        print("chair queue (ready threads):", ", ".join("#" + x for x in rep["chair_queue"]) or "empty")
        print("live nominations:", ", ".join(f"{p}←{','.join('#' + i for i in ids)}" for p, ids in rep["live_nominations"].items()) or "none")
    print("RESULT:", "FAIL" if violations else "OK", f"({len(violations)} violation(s))")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
