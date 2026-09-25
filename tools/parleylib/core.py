"""The appender and everything that reads or writes a parley directory.

A Parley is a directory with participants.json and log.jsonl (SCHEMA.md §1). This module
owns: reading both; assigning ids; rewriting provisional tmp- ids; stamping ts and seen;
whole-turn validation through validate.Replay; the locked, atomic append; the local
state file (what each participant was last shown, harness session ids); and building
chair records from the CLI's shorthand.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)
import validate as V  # noqa: E402

STAR = ["*"]
TMP_RE = re.compile(r"^tmp-[A-Za-z0-9._-]{1,32}$")
SCHEMA_DIR = os.path.join(os.path.dirname(TOOLS_DIR), "schema")


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_parley_dir(start: Optional[str] = None) -> Optional[str]:
    """Walk up from start (cwd) looking for a .parley directory."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        cand = os.path.join(d, ".parley")
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


class TurnRejected(Exception):
    def __init__(self, findings):
        self.findings = findings
        super().__init__("; ".join(str(f) for f in findings))


class Parley:
    def __init__(self, parley_dir: str, repo: Optional[str] = None):
        self.dir = os.path.abspath(parley_dir)
        self.repo = os.path.abspath(repo) if repo else os.path.dirname(self.dir)
        self.log_path = os.path.join(self.dir, "log.jsonl")
        self.reg_path = os.path.join(self.dir, "participants.json")
        self.state_path = os.path.join(self.dir, "state.json")
        self.lock_path = os.path.join(self.dir, ".lock")
        self.dispatch_log = os.path.join(self.dir, "dispatch.log")
        if not os.path.exists(self.reg_path):
            raise FileNotFoundError(f"no participants.json in {self.dir}")
        if not os.path.exists(self.log_path):
            open(self.log_path, "a").close()

    # ------------------------------------------------------------------ registry
    @property
    def registry(self) -> dict:
        return V.load_json(self.reg_path)

    @property
    def participants(self) -> dict:
        return {p["id"]: p for p in self.registry["participants"]}

    @property
    def chair(self) -> str:
        return next(p["id"] for p in self.registry["participants"] if p.get("role") == "chair")

    @property
    def base_branch(self) -> str:
        return self.participants[self.chair].get("branch", "main")

    def agents(self) -> list:
        return [p for p in self.registry["participants"] if p.get("kind") == "model"]

    def worktree(self, p: dict) -> Optional[str]:
        wt = p.get("worktree")
        return os.path.normpath(os.path.join(self.repo, wt)) if wt else None

    # ------------------------------------------------------------------ log
    def raw_records(self) -> list:
        findings: list = []
        return V.load_log(self.log_path, findings)

    def records(self) -> list:
        return [r for r in (V.to_rec(n, o) for n, o in self.raw_records()) if r is not None]

    def replay(self, records: Optional[list] = None, findings: Optional[list] = None) -> V.Replay:
        parts = [V.Participant(p["id"], p.get("kind", "?"), p.get("role", "member"),
                               p.get("latency", "minutes"), p.get("capabilities", []))
                 for p in self.registry["participants"]]
        rp = V.Replay(parts, findings if findings is not None else [])
        for r in (records if records is not None else self.records()):
            rp.feed(r)
        return rp

    def last_id(self, records: Optional[list] = None) -> Optional[str]:
        recs = records if records is not None else self.records()
        return recs[-1].id if recs else None

    def last_visible_id(self, pid: str, records: Optional[list] = None) -> Optional[str]:
        chair = self.chair
        recs = records if records is not None else self.records()
        for r in reversed(recs):
            if pid == chair or r.visibility == STAR or pid in r.visibility:
                return r.id
        return None

    def _next_ids(self, records: list, n: int) -> list:
        nums = [int(r.id) for r in records if r.id.isdigit()]
        start = (max(nums) + 1) if nums else 1
        width = max(3, len(str(max(nums))) if nums else 3)
        return [str(start + i).zfill(width) for i in range(n)]

    # ------------------------------------------------------------------ appending
    def normalize(self, author: str, records: list, seen: Optional[str], existing: list) -> list:
        """Assign ids and ts, stamp seen and from, rewrite tmp- ids. Returns new dicts."""
        ids = self._next_ids(existing, len(records))
        mapping = {}
        for rec, new_id in zip(records, ids):
            old = rec.get("id")
            if isinstance(old, str) and TMP_RE.match(old):
                mapping[old] = new_id
        out = []
        for rec, new_id in zip(records, ids):
            r = dict(rec)
            r["id"] = new_id
            r["ts"] = now_ts()
            r["from"] = author
            r["seen"] = seen
            if isinstance(r.get("thread"), str) and r["thread"] in mapping:
                r["thread"] = mapping[r["thread"]]
            if isinstance(r.get("re"), list):
                r["re"] = [mapping.get(x, x) if isinstance(x, str) else x for x in r["re"]]
            if not r.get("thread"):
                # no thread given: a record with a single re inherits that record's thread; else it roots a new one
                tgt = next((x for x in existing if r.get("re") and x.id == r["re"][0]), None)
                r["thread"] = tgt.thread if tgt else new_id
            for k in ("to", "next", "visibility"):
                if k in r and not r[k]:
                    del r[k]
            if "re" in r and not r["re"]:
                del r["re"]
            # canonical key order for readable logs
            ordered = {}
            for k in ("id", "ts", "from", "to", "re", "thread", "kind", "body", "next", "seen", "visibility"):
                if k in r:
                    ordered[k] = r[k]
            out.append(ordered)
        return out

    def validate_turn(self, new_dicts: list, existing: list) -> list:
        """Findings (violations only) attributable to the new records."""
        findings: list = []
        fake_lines = [(len(existing) + i + 1, d) for i, d in enumerate(new_dicts)]
        V.shape_check(SCHEMA_DIR, fake_lines, self.registry, findings)
        new_recs = [V.to_rec(n, d) for n, d in fake_lines]
        if any(r is None for r in new_recs):
            findings.append(V.Finding("violation", None, "S1", "record missing id/from/thread/kind"))
            return [f for f in findings if f.level == "violation"]
        rp_findings: list = []
        self.replay(existing + new_recs, rp_findings)
        new_ids = {d["id"] for d in new_dicts}
        findings += [f for f in rp_findings if f.rec in new_ids]
        return [f for f in findings if f.level == "violation"]

    def append_turn(self, author: str, records: list, seen: Optional[str] = None) -> list:
        """Normalize, validate and append one turn atomically. Returns the appended dicts.
        Raises TurnRejected with the violations if the turn is invalid; nothing is written then."""
        if not records:
            return []
        with open(self.lock_path, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                existing = self.records()
                if seen is None:
                    seen = self.last_visible_id(author, existing)
                new_dicts = self.normalize(author, records, seen, existing)
                bad = self.validate_turn(new_dicts, existing)
                if bad:
                    raise TurnRejected(bad)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    for d in new_dicts:
                        f.write(json.dumps(d, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                return new_dicts
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ state
    def load_state(self) -> dict:
        if os.path.exists(self.state_path):
            try:
                return json.load(open(self.state_path, encoding="utf-8"))
            except Exception:
                pass
        return {"participants": {}}

    def save_state(self, state: dict) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, self.state_path)

    def pstate(self, state: dict, pid: str) -> dict:
        return state.setdefault("participants", {}).setdefault(pid, {})

    def log_event(self, msg: str) -> None:
        with open(self.dispatch_log, "a", encoding="utf-8") as f:
            f.write(f"{now_ts()} {msg}\n")


# ---------------------------------------------------------------------- record building

def parse_change(spec: Optional[str]) -> Optional[dict]:
    """'branch@commit[:file,file]' or 'commit' -> change object."""
    if not spec:
        return None
    files = None
    if ":" in spec:
        spec, files_s = spec.split(":", 1)
        files = [f for f in files_s.split(",") if f]
    if "@" in spec:
        branch, commit = spec.split("@", 1)
    else:
        branch, commit = None, spec
    c = {}
    if branch:
        c["branch"] = branch
    if commit:
        c["commit"] = commit
    if files:
        c["files"] = files
    return c


def split_ids(s: Optional[str]) -> list:
    if not s:
        return []
    return [x.strip().lstrip("#") for x in re.split(r"[,\s]+", s) if x.strip()]


def build_record(kind: str, *, text: Optional[str] = None, reasons: Optional[list] = None,
                 quote: Optional[str] = None, thread: Optional[str] = None, re_: Optional[list] = None,
                 to: Optional[list] = None, next_: Optional[list] = None, visibility: Optional[list] = None,
                 change: Optional[dict] = None) -> dict:
    body: dict = {}
    if kind in ("say", "ask", "propose", "decide"):
        body["text"] = text or ""
    else:
        body["reasons"] = reasons or []
        if text:
            body["text"] = text
        if kind == "object" and quote:
            body["quote"] = quote
    if change and kind in ("say", "propose", "decide"):
        body["change"] = change
    rec: dict = {"kind": kind, "body": body}
    if thread:
        rec["thread"] = thread.lstrip("#")
    if re_:
        rec["re"] = [x.lstrip("#") for x in re_]
    if to:
        rec["to"] = to
    if next_:
        rec["next"] = next_
    if visibility:
        rec["visibility"] = visibility
    return rec


def extract_records(text: str) -> tuple:
    """Pull records out of an agent's reply. Returns (records, note). A ```jsonl / ```json
    fence wins; failing that, the whole text is tried as JSONL, then as a JSON array."""
    fences = re.findall(r"```(?:jsonl|json)?\s*\n(.*?)```", text, re.S)
    candidates = fences if fences else [text]
    last_err = None
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            return [], "empty fence"
        try:
            arr = json.loads(cand)
            if isinstance(arr, list):
                return [x for x in arr if isinstance(x, dict)], "json array"
            if isinstance(arr, dict):
                return [arr], "json object"
        except json.JSONDecodeError:
            pass
        recs, ok = [], True
        for line in cand.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                ok, last_err = False, str(e)
                break
            if isinstance(obj, dict):
                recs.append(obj)
        if ok and recs:
            return recs, "jsonl"
    if not fences:
        return [], "no fence"
    return [], f"unparseable fence: {last_err}"
