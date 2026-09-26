#!/usr/bin/env python3
"""Generate the agent-facing rules from PROTOCOL.md §8 (PROTOCOL §9). Entry points: `parley gen-docs`,
`parley init`, or the shim tools/gen-agent-docs.py.

For each model participant, the block between
    <!-- parley:agent-rules:begin -->  and  <!-- parley:agent-rules:end -->
in PROTOCOL.md is copied, with a per-participant header prepended, into:

    harness claude-code   -> <worktree>/CLAUDE.md
    harness codex-cli     -> <worktree>/AGENTS.md
    harness llama-server  -> <repo>/.parley/system-<id>.txt   (plain text, for the system prompt)

The block is written as a marker-delimited region; anything outside the markers in an
existing CLAUDE.md / AGENTS.md is preserved, so a repository's own instructions survive.
Hand edits inside the markers are overwritten. Run it after every change to PROTOCOL.md.

usage:
  tools/gen-agent-docs.py                    # all model participants in .parley/participants.json
  tools/gen-agent-docs.py --for claude       # one participant
  tools/gen-agent-docs.py --out DIR          # write every file into DIR instead of the worktrees
  tools/gen-agent-docs.py --stdout           # print instead of writing
  --parley DIR (default .parley) · --protocol PATH (default: the PROTOCOL.md of the parley spec repo) · --repo DIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

BEGIN = "<!-- parley:agent-rules:begin -->"
END = "<!-- parley:agent-rules:end -->"
TARGETS = {"claude-code": "CLAUDE.md", "codex-cli": "AGENTS.md",
           "llama-server": None, "command": None, "inbox": None}
SECTION_RE = re.compile(r"<!-- parley:section (\w+) -->\n(.*?)<!-- /parley:section -->\n?", re.S)


def die(msg: str) -> None:
    print(f"gen-agent-docs.py: {msg}", file=sys.stderr)
    sys.exit(2)


def extract_block(protocol_path: str) -> str:
    text = open(protocol_path, encoding="utf-8").read()
    m = re.search(re.escape(BEGIN) + r"\n(.*?)\n" + re.escape(END), text, re.S)
    if not m:
        die(f"{protocol_path}: agent-rules markers not found")
    return m.group(1).strip("\n")


def participants_line(registry: dict) -> str:
    bits = []
    for q in registry["participants"]:
        if q.get("kind") == "tool":
            continue
        tag = q.get("kind", "?") + (", chair" if q.get("role") == "chair" else "")
        bits.append(f"{q['id']} ({tag})")
    return ", ".join(bits)


def header(p: dict, registry: dict, base: str, log_rel: str, spec_dir: str) -> str:
    chairs = [q["id"] for q in registry["participants"] if q.get("role") == "chair"]
    bits = [f"You are **{p['id']}**" + (f" ({p['model']})" if p.get("model") else "") + " in this parley.",
            "Participants: " + participants_line(registry) + "."]
    if p.get("role") == "chair":
        bits.append("You are a chair: you may `decide`." if len(chairs) == 1 else
                    f"You are one of the chairs ({', '.join(chairs)}); any chair may `decide`.")
    elif chairs:
        bits.append("Chair" + ("s" if len(chairs) > 1 else "") + f": {', '.join(chairs)}.")
    else:
        bits.append("This parley has no chair: nothing closes a thread; a thread everyone has accepted is simply done.")
    has_repo = "repo" in p.get("capabilities", [])
    if has_repo:
        bits.append(f"Base branch `{base}`.")
    if p.get("branch"):
        bits.append(f"Your branch: `{p['branch']}`.")
    if p.get("worktree"):
        bits.append(f"Your worktree: `{p['worktree']}`.")
    if has_repo:
        bits.append(f"The log: `{log_rel}` (your copy may lag; the projection you are given is authoritative).")
        bits.append(f"Full spec: `{os.path.join(spec_dir, 'SCHEMA.md')}` and `{os.path.join(spec_dir, 'PROTOCOL.md')}`.")
    else:
        bits.append("You have no repository or tool access: the projection you are given is all you know, and your reply is text.")
    return " ".join(bits)


def select_sections(body: str, p: dict) -> str:
    """Keep the sections that apply to this participant: `repo` needs the repo capability,
    `chair` needs the chair role. Unmarked text is for everyone."""
    keep = set()
    if "repo" in p.get("capabilities", []):
        keep.add("repo")
    if p.get("role") == "chair":
        keep.add("chair")
    def sub(m):
        return m.group(2) if m.group(1) in keep else ""
    out = SECTION_RE.sub(sub, body)
    return re.sub(r"\n{3,}", "\n\n", out).strip("\n")


def base_branch_of(registry: dict) -> str:
    if registry.get("base_branch"):
        return registry["base_branch"]
    parts = registry["participants"]
    for q in parts:
        if q.get("role") == "chair" and q.get("branch"):
            return q["branch"]
    for q in parts:
        if q.get("kind") == "human" and q.get("branch"):
            return q["branch"]
    return "main"


def render_rules(p: dict, registry: dict, *, protocol_path: str | None = None, log_rel: str = ".parley/log.jsonl",
                 plain: bool = True) -> str:
    """The rules for one participant as text: header + the applicable sections of PROTOCOL §8.
    Used for the generated files and, at run time, as the system prompt of API/llama participants."""
    from .paths import PROTOCOL_MD
    protocol = protocol_path or PROTOCOL_MD
    body = select_sections(extract_block(protocol), p)
    base = base_branch_of(registry)
    text = header(p, registry, base, log_rel, os.path.dirname(os.path.abspath(protocol))) + "\n\n" + adapt_base_branch(body, base)
    return strip_html_comments(text) if plain else text


def adapt_base_branch(body: str, base: str) -> str:
    """The rules are written against `main`; a repository whose chair branch is e.g. master gets that instead."""
    if base == "main":
        return body
    return body.replace("`main`", f"`{base}`").replace("main...parley/", f"{base}...parley/")


def region(p: dict, registry: dict, log_rel: str, protocol: str) -> str:
    banner = f"<!-- GENERATED from {os.path.basename(protocol)} §8 by `parley gen-docs` — edit {os.path.basename(protocol)}, not this block -->"
    return "\n".join([BEGIN, banner, render_rules(p, registry, protocol_path=protocol, log_rel=log_rel, plain=False), END])


def splice(existing: str | None, new_region: str) -> str:
    if existing is None:
        return new_region + "\n"
    if BEGIN in existing and END in existing:
        pat = re.escape(BEGIN) + r".*?" + re.escape(END)
        return re.sub(pat, lambda _: new_region, existing, count=1, flags=re.S)
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + new_region + "\n"


def hide_from_git(dest_dir: str, name: str) -> str:
    """Keep the generated file out of the agent's commits and out of the way of rebases:
    a tracked file gets --skip-worktree, an untracked one goes into the per-worktree exclude."""
    def g(*a):
        return subprocess.run(["git", *a], cwd=dest_dir, capture_output=True, text=True)
    if g("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return ""
    tracked = g("ls-files", "--error-unmatch", name).returncode == 0
    if tracked:
        g("update-index", "--skip-worktree", name)
        return " (tracked: marked skip-worktree)"
    excl = g("rev-parse", "--git-path", "info/exclude").stdout.strip()
    if excl:
        excl = excl if os.path.isabs(excl) else os.path.join(dest_dir, excl)
        os.makedirs(os.path.dirname(excl), exist_ok=True)
        lines = open(excl, encoding="utf-8").read().splitlines() if os.path.exists(excl) else []
        if f"/{name}" not in lines:
            with open(excl, "a", encoding="utf-8") as f:
                f.write(f"/{name}\n")
        return " (untracked: added to the worktree's info/exclude)"
    return ""


def strip_html_comments(s: str) -> str:
    return re.sub(r"<!--.*?-->\n?", "", s, flags=re.S).strip("\n") + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--for", dest="only", action="append", help="participant id (repeatable)")
    ap.add_argument("--parley", default=".parley")
    ap.add_argument("--protocol")
    ap.add_argument("--repo", help="repository root; default: parent of --parley")
    ap.add_argument("--out", help="write all generated files into this directory")
    ap.add_argument("--stdout", action="store_true")
    a = ap.parse_args(argv)

    parley = os.path.abspath(a.parley)
    repo = os.path.abspath(a.repo) if a.repo else os.path.dirname(parley)
    from .paths import PROTOCOL_MD
    protocol = a.protocol or PROTOCOL_MD
    reg_path = os.path.join(parley, "participants.json")
    for path in (protocol, reg_path):
        if not os.path.exists(path):
            die(f"not found: {path}")
    registry = json.load(open(reg_path, encoding="utf-8"))
    parts = registry["participants"]
    log_rel = os.path.relpath(os.path.join(parley, "log.jsonl"), repo)

    wanted = [p for p in parts if p.get("kind") == "model" and (not a.only or p["id"] in a.only)]
    if a.only:
        missing = set(a.only) - {p["id"] for p in wanted}
        if missing:
            die(f"not model participants in the registry: {', '.join(sorted(missing))}")

    written = 0
    for p in wanted:
        harness = p.get("harness", "")
        if harness not in TARGETS:
            print(f"skip {p['id']}: harness {harness!r} has no generated document (known: {', '.join(TARGETS)})", file=sys.stderr)
            continue
        reg = region(p, registry, log_rel, protocol)
        if TARGETS[harness] is None or not p.get("worktree"):
            # text-only participants: the same rules become their system prompt at run time
            # (render_rules); the file is written for the chair to read
            content, name = strip_html_comments(reg), f"system-{p['id']}.txt"
            dest_dir = a.out or parley
            splice_into_existing = False
        else:
            content, name = reg, TARGETS[harness]
            wt = p.get("worktree")
            if a.out:
                dest_dir = a.out
            elif wt:
                dest_dir = os.path.normpath(os.path.join(repo, wt))
            else:
                print(f"skip {p['id']}: no worktree in the registry (use --out)", file=sys.stderr)
                continue
            splice_into_existing = True
        if a.stdout:
            print(f"===== {p['id']} → {os.path.join(dest_dir, name)}\n{content}")
            continue
        if not os.path.isdir(dest_dir):
            print(f"skip {p['id']}: {dest_dir} does not exist (create the worktree first, or use --out)", file=sys.stderr)
            continue
        dest = os.path.join(dest_dir, name)
        if splice_into_existing:
            existing = open(dest, encoding="utf-8").read() if os.path.exists(dest) else None
            content = splice(existing, reg)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        how = hide_from_git(dest_dir, name) if splice_into_existing else ""
        print(f"wrote {dest}{how}")
        written += 1
    return 0 if (written or a.stdout) else 1


if __name__ == "__main__":
    sys.exit(main())
