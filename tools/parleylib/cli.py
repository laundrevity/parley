"""The `parley` command.

  parley init --repo PATH [--agents claude,codex]   set up .parley/, worktrees, CLAUDE.md/AGENTS.md in a repo
  parley doctor                                     check binaries, worktrees, generated docs
  parley status [--for ID]                          the chair's queue (or a participant's standing block)
  parley run [-i] [--rounds N] [--dry-run]          the dispatcher; stops when it is your turn (-i: keep a prompt)
  parley ask <to,to> "text" [#re ...]               your records, validated and appended
  parley say "text" [--to ids] [--whisper ids]
  parley propose "text" [--change branch@commit:files]
  parley accept <ids> "reason" ["reason" ...]
  parley object <ids> "reason" [...] [--quote "..."]
  parley decide <thread> "ruling" [--change ...]
  parley project --for ID [--since ID|none]         print a projection
  parley validate                                   validate the log
  parley gen-docs                                   regenerate CLAUDE.md / AGENTS.md / system prompts

Global: --parley DIR (default: nearest .parley upward from cwd) · --repo DIR (default: its parent)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from . import core, dispatch

sys.path.insert(0, core.TOOLS_DIR)
import project as P  # noqa: E402
import validate as V  # noqa: E402

GEN_DOCS = os.path.join(core.TOOLS_DIR, "gen-agent-docs.py")
TEMPLATE_REGISTRY = os.path.join(os.path.dirname(core.TOOLS_DIR), ".parley", "participants.json")
IGNORE_LINES = [".parley/state.json", ".parley/dispatch.log", ".parley/.lock", ".parley/*.tmp"]


def die(msg: str, code: int = 2):
    print(f"parley: {msg}", file=sys.stderr)
    sys.exit(code)


def open_parley(a) -> core.Parley:
    d = a.parley or core.find_parley_dir()
    if not d:
        die("no .parley directory here or above; run `parley init --repo PATH` first")
    try:
        return core.Parley(d, a.repo)
    except FileNotFoundError as e:
        die(str(e))


# ----------------------------------------------------------------------------- chair records

def chair_append(parley: core.Parley, rec: dict) -> list:
    try:
        out = parley.append_turn(parley.chair, [rec])
    except core.TurnRejected as e:
        for f in e.findings:
            print(f"  rejected: {f.code}: {f.msg}", file=sys.stderr)
        sys.exit(1)
    for d in out:
        print(f"#{d['id']} {d['kind']} appended (thread #{d['thread']})")
    return out


def cmd_record(a) -> None:
    parley = open_parley(a)
    kind = a.cmd
    re_ = core.split_ids(getattr(a, "re", None))
    thread = getattr(a, "thread", None)
    to = core.split_ids(getattr(a, "to", None)) or None
    next_ = core.split_ids(getattr(a, "next", None)) or None
    vis = core.split_ids(getattr(a, "whisper", None)) or None
    change = core.parse_change(getattr(a, "change", None))
    if kind == "ask":
        to = core.split_ids(a.targets)
        if not to:
            die("ask needs addressees: parley ask codex \"...\"")
        rec = core.build_record("ask", text=a.text, thread=thread, re_=re_, to=to, next_=next_, visibility=vis)
    elif kind in ("say", "propose"):
        rec = core.build_record(kind, text=a.text, thread=thread, re_=re_, to=to, next_=next_, visibility=vis, change=change)
    elif kind in ("accept", "object"):
        targets = core.split_ids(a.targets)
        if not targets:
            die(f"{kind} needs the id(s) it responds to")
        rec = core.build_record(kind, reasons=list(a.reasons), text=getattr(a, "text", None),
                                quote=getattr(a, "quote", None), thread=thread, re_=(re_ or []) + targets,
                                next_=next_, visibility=vis)
    elif kind == "decide":
        thread_id = a.target.lstrip("#")
        rec = core.build_record("decide", text=a.text, thread=thread_id, re_=re_, next_=next_, change=change)
    else:
        die(f"unknown kind {kind}")
    if vis and parley.chair not in vis:
        rec["visibility"] = vis  # the chair is implicitly a viewer
    chair_append(parley, rec)


# ----------------------------------------------------------------------------- interactive chair

REPL_HELP = """chair>  ask codex: <text>            decide 007: <ruling>          object 012: <reason> | <reason>
        say: <text>  ·  say @claude: <text>  ·  whisper claude: <text>  ·  propose: <text>
        accept 013: <reason> | <reason>   ·   add #id tokens before the colon to set `re`
        status · help · quit"""


def repl_turn(parley: core.Parley) -> bool:
    """One chair interaction. Returns True if a record was appended (keep dispatching)."""
    print(REPL_HELP)
    while True:
        try:
            line = input("chair> ").strip()
        except EOFError:
            print()
            return False
        if not line:
            continue
        if line in ("quit", "q", "exit"):
            return False
        if line == "help":
            print(REPL_HELP)
            continue
        if line == "status":
            print(dispatch.Dispatcher(parley).chair_block())
            continue
        m = re.match(r"^(ask|say|whisper|propose|accept|object|decide)(?:\s+([^:]*?))?\s*:\s*(.*)$", line, re.S)
        if not m:
            print("  ? (type help)")
            continue
        kind, targets, text = m.group(1), (m.group(2) or "").strip(), m.group(3).strip()
        toks = targets.split()
        re_ = [t.lstrip("#") for t in toks if t.startswith("#")]
        who = [t.lstrip("@") for t in toks if not t.startswith("#")]
        try:
            if kind == "ask":
                rec = core.build_record("ask", text=text, re_=re_, to=who)
            elif kind == "say":
                rec = core.build_record("say", text=text, re_=re_, to=who or None)
            elif kind == "whisper":
                rec = core.build_record("say", text=text, re_=re_, to=who, visibility=who)
            elif kind == "propose":
                rec = core.build_record("propose", text=text, re_=re_, to=who or None)
            elif kind in ("accept", "object"):
                ids = [t.lstrip("#") for t in toks]
                reasons = [r.strip() for r in text.split("|") if r.strip()]
                rec = core.build_record(kind, reasons=reasons, re_=ids)
            else:  # decide
                ids = [t.lstrip("#") for t in toks]
                if not ids:
                    print("  decide needs a thread id: decide 007: <ruling>")
                    continue
                rec = core.build_record("decide", text=text, thread=ids[0], re_=ids[1:] or None)
            parley.append_turn(parley.chair, [rec])
            print(f"  appended #{parley.last_id()}")
            return True
        except core.TurnRejected as e:
            for f in e.findings:
                print(f"  rejected: {f.code}: {f.msg}")


# ----------------------------------------------------------------------------- init / doctor

def git(repo: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def cmd_init(a) -> None:
    repo = os.path.abspath(a.repo)
    if not os.path.isdir(os.path.join(repo, ".git")) and git(repo, "rev-parse", "--git-dir").returncode != 0:
        die(f"{repo} is not a git repository")
    pdir = os.path.join(repo, ".parley")
    os.makedirs(pdir, exist_ok=True)
    reg_path = os.path.join(pdir, "participants.json")
    agents = [x.strip() for x in a.agents.split(",") if x.strip()]
    if os.path.exists(reg_path) and not a.force:
        print(f"{reg_path} exists; keeping it (--force to regenerate)")
        registry = V.load_json(reg_path)
    else:
        template = V.load_json(TEMPLATE_REGISTRY)
        base = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        name = os.path.basename(repo)
        parts = []
        for p in template["participants"]:
            keep = p.get("role") == "chair" or p.get("kind") == "tool" or p["id"] in agents
            if not keep:
                continue
            p = dict(p)
            if p.get("role") == "chair":
                p["branch"] = base
            if p.get("worktree"):
                p["worktree"] = f"../{name}-{p['id']}"
            parts.append(p)
        missing = set(agents) - {p["id"] for p in parts}
        if missing:
            die(f"agents not in the template registry {TEMPLATE_REGISTRY}: {', '.join(sorted(missing))}")
        registry = {"name": name, "participants": parts}
        json.dump(registry, open(reg_path, "w", encoding="utf-8"), indent=2)
        print(f"wrote {reg_path} (chair branch: {base}; agents: {', '.join(agents)})")
    open(os.path.join(pdir, "log.jsonl"), "a").close()
    # .gitignore
    gi = os.path.join(repo, ".gitignore")
    existing = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
    add = [l for l in IGNORE_LINES if l not in existing.splitlines()]
    if add:
        with open(gi, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n".join(add) + "\n")
        print(f"added {len(add)} line(s) to .gitignore")
    # worktrees
    if not a.no_worktrees:
        for p in registry["participants"]:
            wt = p.get("worktree")
            if not wt:
                continue
            wt_abs = os.path.normpath(os.path.join(repo, wt))
            branch = p.get("branch", f"parley/{p['id']}")
            if os.path.isdir(wt_abs):
                print(f"worktree exists: {wt_abs}")
                continue
            r = git(repo, "worktree", "add", wt_abs, "-b", branch)
            if r.returncode != 0 and "already exists" in (r.stderr or ""):
                r = git(repo, "worktree", "add", wt_abs, branch)
            if r.returncode != 0:
                print(f"could not add worktree {wt_abs}: {r.stderr.strip()}", file=sys.stderr)
            else:
                print(f"worktree {wt_abs} on {branch}")
    # docs
    if not a.no_docs:
        r = subprocess.run([sys.executable, GEN_DOCS, "--parley", pdir, "--repo", repo], capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
    print("\nnext:\n  parley --repo %s ask %s \"<the task>\"\n  parley --repo %s run" % (repo, agents[0] if agents else "claude", repo))


def cmd_doctor(a) -> None:
    parley = open_parley(a)
    ok = True
    def line(good, msg):
        nonlocal ok
        ok = ok and good
        print(("  ok      " if good else "  MISSING ") + msg)
    print(f"parley: {parley.dir}\nrepo:   {parley.repo} (base branch {parley.base_branch})")
    findings: list = []
    V.shape_check(core.SCHEMA_DIR, [], parley.registry, findings)
    line(not findings, "participants.json matches the schema" + ("" if not findings else f": {findings[0].msg}"))
    line("dispatch" in parley.participants, "a `dispatch` tool participant (timeouts and rejections get logged as records)")
    for p in parley.agents():
        h = p.get("harness", "")
        print(f"{p['id']} ({h}):")
        if h in ("claude-code", "codex-cli"):
            exe = p.get("command", "claude" if h == "claude-code" else "codex")
            exe = exe[0] if isinstance(exe, list) else exe.split()[0]
            line(shutil.which(exe) is not None, f"`{exe}` on PATH")
        if h == "llama-server":
            line(True, f"endpoint {p.get('endpoint', 'http://127.0.0.1:8080/v1/chat/completions')} (not probed)")
            sp = os.path.join(parley.dir, f"system-{p['id']}.txt")
            line(os.path.exists(sp), f"system prompt {sp}")
        wt = parley.worktree(p)
        if wt:
            line(os.path.isdir(wt), f"worktree {wt}")
            doc = os.path.join(wt, "CLAUDE.md" if h == "claude-code" else "AGENTS.md")
            has = os.path.exists(doc) and "parley:agent-rules:begin" in open(doc, encoding="utf-8").read()
            line(has, f"{doc} carries the agent rules")
    print("all good" if ok else "fix the MISSING lines, then `parley run`")
    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------------------- others

def cmd_status(a) -> None:
    parley = open_parley(a)
    pid = a.pid or parley.chair
    pj = P.Projector(parley.records(), parley.registry, pid, P.Git(parley.repo, True, parley.base_branch), False)
    print(pj.build(None, None, True))


def cmd_project(a) -> None:
    parley = open_parley(a)
    argv = ["--for", a.pid, "--parley", parley.dir, "--repo", parley.repo]
    if a.since:
        argv += ["--since", a.since]
    sys.exit(P.main(argv))


def cmd_validate(a) -> None:
    parley = open_parley(a)
    sys.exit(V.main([parley.dir, "--report"]))


def cmd_gen_docs(a) -> None:
    parley = open_parley(a)
    sys.exit(subprocess.call([sys.executable, GEN_DOCS, "--parley", parley.dir, "--repo", parley.repo]))


def cmd_run(a) -> None:
    parley = open_parley(a)
    d = dispatch.Dispatcher(parley, rebase=not a.no_rebase, objection_pass=not a.no_objection_pass,
                            only=core.split_ids(a.only) or None, dry_run=a.dry_run, git_enabled=not a.no_git)
    chair_turn = (lambda: repl_turn(parley)) if a.interactive else None
    outcome = d.run(max_rounds=a.rounds, chair_turn=chair_turn)
    if outcome == "chair" and not a.interactive:
        print("\nyour turn — respond with parley decide/ask/say/object..., then `parley run` again", file=sys.stderr)


# ----------------------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="parley", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parley", help="the .parley directory")
    ap.add_argument("--repo", help="repository root (default: parent of .parley)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="set up a parley in a repository")
    s.add_argument("--repo", dest="repo", required=True)
    s.add_argument("--agents", default="claude,codex")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-worktrees", action="store_true")
    s.add_argument("--no-docs", action="store_true")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("doctor", help="check the setup").set_defaults(fn=cmd_doctor)

    s = sub.add_parser("status", help="the chair's queue")
    s.add_argument("--for", dest="pid")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("project", help="print a projection")
    s.add_argument("--for", dest="pid", required=True)
    s.add_argument("--since")
    s.set_defaults(fn=cmd_project)

    sub.add_parser("validate", help="validate the log").set_defaults(fn=cmd_validate)
    sub.add_parser("gen-docs", help="regenerate agent docs").set_defaults(fn=cmd_gen_docs)

    s = sub.add_parser("run", help="run the dispatcher until it is your turn")
    s.add_argument("-i", "--interactive", action="store_true", help="keep a chair prompt open between rounds")
    s.add_argument("--rounds", type=int)
    s.add_argument("--only", help="comma-separated participant ids")
    s.add_argument("--no-rebase", action="store_true")
    s.add_argument("--no-objection-pass", action="store_true")
    s.add_argument("--no-git", action="store_true", help="no stats/diffs from git in projections")
    s.add_argument("--dry-run", action="store_true", help="print the projections that would be sent, send nothing")
    s.set_defaults(fn=cmd_run)

    def common(s, thread=True, to=False, change=False, whisper=False):
        s.add_argument("--re", help="record ids this responds to")
        if thread:
            s.add_argument("--thread")
        s.add_argument("--next", help="nominate participants")
        if to:
            s.add_argument("--to")
        if whisper:
            s.add_argument("--whisper", help="visibility: only these participants (and you)")
        if change:
            s.add_argument("--change", help="branch@commit[:file,file]")

    s = sub.add_parser("ask", help="ask participants; they owe a reply")
    s.add_argument("targets", help="comma-separated participant ids")
    s.add_argument("text")
    common(s, whisper=True)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("say", help="a statement; no obligation")
    s.add_argument("text")
    common(s, to=True, change=True, whisper=True)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("propose", help="a proposal; every agent owes accept or object")
    s.add_argument("text")
    common(s, to=True, change=True, whisper=True)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("accept", help="accept proposal(s) with reasons")
    s.add_argument("targets")
    s.add_argument("reasons", nargs="+")
    s.add_argument("--text")
    common(s, thread=False)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("object", help="object to record(s) with reasons")
    s.add_argument("targets")
    s.add_argument("reasons", nargs="+")
    s.add_argument("--quote")
    s.add_argument("--text")
    common(s, thread=False)
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("decide", help="close a thread with a ruling")
    s.add_argument("target", help="thread id")
    s.add_argument("text")
    common(s, thread=False, change=True)
    s.set_defaults(fn=cmd_record)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    a.fn(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
