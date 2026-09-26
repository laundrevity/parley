"""The `parley` command.

  parley init --repo PATH [--agents claude,codex]   set up .parley/, worktrees, CLAUDE.md/AGENTS.md in a repo
  parley doctor                                     check binaries, worktrees, generated docs
  parley status [--for ID]                          the chair's queue (or a participant's standing block)
  parley run [-i] [--rounds N] [--dry-run]          the dispatcher; stops when it is your turn (-i: keep a prompt)
  parley ask <to,to> "text" [#re ...]               your records, validated and appended
  parley say "text" [--to ids] [--whisper ids]
  parley propose "text" [--change branch@commit:files]
  parley accept <ids> "reason" ["reason" ...]       reasons only; a remark is a following say, a request a following ask
  parley object <ids> "reason" [...] [--quote "..."]
  parley decide <thread> "ruling" [--change ...]
  parley project --for ID [--since ID|none]         print a projection
  parley validate                                   validate the log
  parley gen-docs                                   regenerate CLAUDE.md / AGENTS.md / system prompts
  parley set <participant> <key> <json|string>      edit one registry field (e.g. timeout_s 7200, harness_args '[…]')
  parley reset --yes [--keep N]                     false start: truncate the log, forget dispatcher state

Global: --parley DIR (default: nearest .parley upward from cwd) · --repo DIR (default: its parent)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from . import core, dispatch, gendocs, harness
from . import project as P
from . import validate as V
from .paths import TEMPLATE_REGISTRY
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

def who(parley: core.Parley, a) -> str:
    try:
        return parley.me(getattr(a, "as_", None))
    except KeyError as e:
        die(str(e))


def chair_append(parley: core.Parley, rec: dict, as_id: str) -> list:
    try:
        out = parley.append_turn(as_id, [rec])
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
    me = who(parley, a)
    if vis and me not in vis:
        rec["visibility"] = vis + [me]
    chair_append(parley, rec, me)


# ----------------------------------------------------------------------------- interactive chair

REPL_HELP = """chair>  ask codex: <text>            decide 007: <ruling>          object 012: <reason> | <reason>
        say: <text>  ·  say @claude: <text>  ·  whisper claude: <text>  ·  propose: <text>
        accept 013: <reason> | <reason>   ·   add #id tokens before the colon to set `re`
        status · help · quit"""


def repl_turn(parley: core.Parley, me: str) -> bool:
    """One interaction at the keyboard, as participant `me`. Returns True if a record was appended."""
    print(REPL_HELP)
    while True:
        try:
            line = input(f"{me}> ").strip()
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
            print(dispatch.Dispatcher(parley).block_for(me))
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
            parley.append_turn(me, [rec])
            print(f"  appended #{parley.last_id()}")
            return True
        except core.TurnRejected as e:
            for f in e.findings:
                print(f"  rejected: {f.code}: {f.msg}")


# ----------------------------------------------------------------------------- init / doctor

def git(repo: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def cmd_init(a) -> None:
    if not a.repo and not a.dir:
        die("say where: parley init --repo PATH (a git repository) or --dir PATH (any directory, text-only)")
    in_repo = bool(a.repo)
    repo = os.path.abspath(a.repo or a.dir)
    if in_repo and not os.path.isdir(os.path.join(repo, ".git")) and git(repo, "rev-parse", "--git-dir").returncode != 0:
        die(f"{repo} is not a git repository (use --dir for a parley without one)")
    os.makedirs(repo, exist_ok=True)
    pdir = os.path.join(repo, ".parley")
    os.makedirs(pdir, exist_ok=True)
    reg_path = os.path.join(pdir, "participants.json")
    agents = [x.strip() for x in (a.agents or ("claude,codex" if in_repo else "fable,astra")).split(",") if x.strip()]
    chairs = None if a.chair is None else ([] if a.chair.strip().lower() == "none" else core.split_ids(a.chair))
    if os.path.exists(reg_path) and not a.force:
        print(f"{reg_path} exists; keeping it (--force to regenerate)")
        registry = V.load_json(reg_path)
    else:
        template = V.load_json(TEMPLATE_REGISTRY)
        base = (git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main") if in_repo else "main"
        name = os.path.basename(repo)
        parts = []
        for p in template["participants"]:
            keep = p.get("kind") == "human" or p.get("kind") == "tool" or p["id"] in agents
            if not keep:
                continue
            p = dict(p)
            if not in_repo:
                # text-only parley: nothing to check out, nothing to run
                for k in ("worktree", "branch"):
                    p.pop(k, None)
                if p.get("kind") == "human":
                    p["capabilities"] = []
            else:
                if p.get("kind") == "human":
                    p["branch"] = base
                if p.get("worktree"):
                    p["worktree"] = f"../{name}-{p['id']}"
            if chairs is not None:
                if p["id"] in chairs:
                    p["role"] = "chair"
                else:
                    p.pop("role", None)
            parts.append(p)
        missing = set(agents) - {p["id"] for p in parts}
        if missing:
            die(f"agents not in the template registry {TEMPLATE_REGISTRY}: {', '.join(sorted(missing))}")
        if chairs:
            unknown = set(chairs) - {p["id"] for p in parts}
            if unknown:
                die(f"--chair names participants not in this parley: {', '.join(sorted(unknown))}")
        registry = {"name": name, "participants": parts}
        if in_repo:
            registry["base_branch"] = base
        json.dump(registry, open(reg_path, "w", encoding="utf-8"), indent=2)
        who_chairs = [p["id"] for p in parts if p.get("role") == "chair"]
        print(f"wrote {reg_path} ({'repo, base branch ' + base if in_repo else 'text-only, no repository'}; "
              f"participants: {', '.join(p['id'] for p in parts if p.get('kind') != 'tool')}; "
              f"chair{'s' if len(who_chairs) != 1 else ''}: {', '.join(who_chairs) or 'none'})")
    open(os.path.join(pdir, "log.jsonl"), "a").close()
    # .gitignore
    if in_repo:
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
    if in_repo and not a.no_worktrees:
        for p in registry["participants"]:
            wt = p.get("worktree")
            if not wt:
                continue
            wt_abs = os.path.normpath(os.path.join(repo, wt))
            branch = p.get("branch", f"parley/{p['id']}")
            if os.path.isdir(wt_abs):
                print(f"worktree exists: {wt_abs}")
            else:
                r = git(repo, "worktree", "add", wt_abs, "-b", branch)
                if r.returncode != 0 and "already exists" in (r.stderr or ""):
                    r = git(repo, "worktree", "add", wt_abs, branch)
                if r.returncode != 0:
                    print(f"could not add worktree {wt_abs}: {r.stderr.strip()}", file=sys.stderr)
                    continue
                print(f"worktree {wt_abs} on {branch}")
            share_into(repo, wt_abs, a.share, a.clone)
    # docs
    if not a.no_docs:
        gendocs.main(["--parley", pdir, "--repo", repo])
    print("\nnext:\n  cd %s\n  parley ask %s \"<the question or task>\"\n  parley run" % (repo, ",".join(agents) if agents else "claude"))


def share_into(repo: str, wt: str, share: list, clone: list) -> None:
    """Build caches for a worktree: symlink shared parts, clone per-tree parts (cp -c on APFS)."""
    for rel in share:
        src, dst = os.path.join(repo, rel), os.path.join(wt, rel)
        if not os.path.exists(src):
            print(f"  share: {src} does not exist; skipped", file=sys.stderr)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.islink(dst) or os.path.exists(dst):
            if os.path.islink(dst) and os.readlink(dst) == src:
                continue
            print(f"  share: {dst} already exists and is not the expected link; left alone", file=sys.stderr)
            continue
        os.symlink(src, dst)
        print(f"  linked {dst} -> {src}")
    for rel in clone:
        src, dst = os.path.join(repo, rel), os.path.join(wt, rel)
        if not os.path.exists(src):
            print(f"  clone: {src} does not exist; skipped", file=sys.stderr)
            continue
        if os.path.exists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        r = subprocess.run(["cp", "-Rc", src, dst], capture_output=True, text=True)      # APFS clone
        if r.returncode != 0:
            r = subprocess.run(["cp", "-R", src, dst], capture_output=True, text=True)   # plain copy elsewhere
        print(f"  cloned {dst}" if r.returncode == 0 else f"  clone failed for {dst}: {r.stderr.strip()}")


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
    for var in harness.API_KEY_VARS:
        if os.environ.get(var):
            print(f"  note     ${var} is set in your shell; parley removes it from the CLIs' environment so they bill your login, not the API")
    for p in parley.agents():
        h = p.get("harness", "")
        print(f"{p['id']} ({h}):")
        if h in ("claude-code", "codex-cli"):
            exe = p.get("command", "claude" if h == "claude-code" else "codex")
            exe = exe[0] if isinstance(exe, list) else exe.split()[0]
            line(shutil.which(exe) is not None, f"`{exe}` on PATH")
        if h == "llama-server":
            line(True, f"endpoint {p.get('endpoint', 'http://127.0.0.1:8080/v1/chat/completions')} (not probed)")
        wt = parley.worktree(p)
        if wt:
            line(os.path.isdir(wt), f"worktree {wt}")
            doc = os.path.join(wt, "CLAUDE.md" if h == "claude-code" else "AGENTS.md")
            has = os.path.exists(doc) and "parley:agent-rules:begin" in open(doc, encoding="utf-8").read()
            line(has, f"{doc} carries the agent rules")
        rules = "" if wt else "<rules>"
        if h == "claude-code":
            argv = harness.claude_cmd(p, {}, parley.repo, rules)
            print("  command  " + " ".join(shlex.quote(x) for x in argv) + "   [+ --resume <session> after the first turn; projection on stdin]")
            print("  cwd      " + (wt or parley.repo) + ("" if wt else "   (text-only: no worktree, no tools; the rules ride --append-system-prompt)"))
        elif h == "codex-cli":
            argv = harness.codex_cmd(p, {}, parley.repo, wt or parley.repo, "<outfile>", "<projection>", rules)
            print("  command  " + " ".join(shlex.quote(x) for x in argv[:-1]) + (" '<rules> --- <projection>'" if rules else " <projection>"))
            if not wt:
                print("  cwd      " + parley.repo + "   (text-only: read-only sandbox; the rules lead the prompt)")
        if h in ("claude-code", "codex-cli"):
            print(f"  timeout  {harness.timeout_for(p)}s")
    print("all good" if ok else "fix the MISSING lines, then `parley run`")
    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------------------- others

def cmd_status(a) -> None:
    parley = open_parley(a)
    pid = a.pid or who(parley, a)
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


def cmd_set(a) -> None:
    """Edit one registry field without hand-editing JSON: parley set codex timeout_s 7200."""
    parley = open_parley(a)
    reg = parley.registry
    target = next((p for p in reg["participants"] if p["id"] == a.participant), None)
    if target is None:
        die(f"no participant '{a.participant}' (have: {', '.join(p['id'] for p in reg['participants'])})")
    raw = a.value
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw                     # a bare string
    if a.unset:
        target.pop(a.key, None)
    else:
        target[a.key] = value
    findings: list = []
    V.shape_check(core.SCHEMA_DIR, [], reg, findings)
    if findings:
        for f in findings:
            print(f"  rejected: {f.msg}", file=sys.stderr)
        sys.exit(1)
    json.dump(reg, open(parley.reg_path, "w", encoding="utf-8"), indent=2)
    print(f"{a.participant}.{a.key} " + ("unset" if a.unset else f"= {json.dumps(value)}"))


def cmd_reset(a) -> None:
    """False starts: truncate the log to its first N records and forget dispatcher state."""
    parley = open_parley(a)
    if not a.yes:
        die("reset rewrites the log; re-run with --yes (keeps the first --keep N records, default 0)")
    lines = open(parley.log_path, encoding="utf-8").read().splitlines(keepends=True)
    kept = lines[: a.keep]
    with open(parley.log_path, "w", encoding="utf-8") as f:
        f.writelines(kept)
    for name in ("state.json", "dispatch.log"):
        path = os.path.join(parley.dir, name)
        if os.path.exists(path):
            os.remove(path)
    print(f"log truncated to {len(kept)} record(s); state.json and dispatch.log removed")


def cmd_gen_docs(a) -> None:
    parley = open_parley(a)
    sys.exit(gendocs.main(["--parley", parley.dir, "--repo", parley.repo]))


def cmd_run(a) -> None:
    parley = open_parley(a)
    d = dispatch.Dispatcher(parley, rebase=not a.no_rebase, objection_pass=not a.no_objection_pass,
                            only=core.split_ids(a.only) or None, dry_run=a.dry_run, git_enabled=not a.no_git)
    try:
        me = parley.me(getattr(a, "as_", None))
    except KeyError:
        me = None                        # no human at this keyboard (an all-model parley)
    d.me = me
    chair_turn = None
    if a.interactive and me:
        chair_turn = lambda: repl_turn(parley, me)
        def human_prompt(pid, projection):
            print(projection, flush=True)
            before = len(parley.records())
            repl_turn(parley, pid)
            return len(parley.records()) - before
        d.human_prompt = human_prompt
    outcome = d.run(max_rounds=a.rounds, chair_turn=chair_turn)
    if outcome == "human" and not a.interactive:
        names = ", ".join(p["id"] for p in d.waiting)
        print(f"\nturn: {names} — respond with parley decide/ask/say/object... (as that participant), then `parley run` again", file=sys.stderr)
    elif outcome == "quiescent":
        print("\nquiet: nothing owed, nobody nominated." + ("" if me else " (no human here to hand the floor to)"), file=sys.stderr)


# ----------------------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="parley", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--parley", help="the .parley directory")
    ap.add_argument("--repo", help="repository root (default: parent of .parley)")
    ap.add_argument("--as", dest="as_", help="which participant you are (default: the only human, or $PARLEY_AS)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="set up a parley in a repository (--repo) or any directory (--dir)")
    s.add_argument("--repo", dest="repo")
    s.add_argument("--dir", dest="dir", help="a parley outside any repository: no worktrees, no git; text-only participants")
    s.add_argument("--agents", default=None, help="template participant ids (default: claude,codex for --repo; fable,astra for --dir)")
    s.add_argument("--chair", default=None, help="comma-separated chair ids (default: the template's human); 'none' for a chairless parley")
    s.add_argument("--force", action="store_true")
    s.add_argument("--no-worktrees", action="store_true")
    s.add_argument("--no-docs", action="store_true")
    s.add_argument("--share", action="append", default=[], metavar="PATH",
                   help="symlink this path from the main checkout into every worktree (e.g. .lake/packages, node_modules)")
    s.add_argument("--clone", action="append", default=[], metavar="PATH",
                   help="copy this path from the main checkout into every worktree if absent (copy-on-write where the filesystem allows; e.g. .lake/build)")
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

    s = sub.add_parser("set", help="edit one registry field: parley set codex timeout_s 7200")
    s.add_argument("participant")
    s.add_argument("key")
    s.add_argument("value", nargs="?", default="null", help="JSON (lists, numbers, true/false) or a bare string")
    s.add_argument("--unset", action="store_true")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("reset", help="false start: truncate the log and forget dispatcher state")
    s.add_argument("--keep", type=int, default=0, help="keep the first N records")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_reset)

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

    s = sub.add_parser("accept", help="accept proposal(s) with reasons (reasons only: a remark is a following say, a request a following ask)")
    s.add_argument("targets")
    s.add_argument("reasons", nargs="+")
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
