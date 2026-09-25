#!/usr/bin/env python3
"""Generate the agent-facing rules from PROTOCOL.md §8 (PROTOCOL §9).

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
import sys

BEGIN = "<!-- parley:agent-rules:begin -->"
END = "<!-- parley:agent-rules:end -->"
TARGETS = {"claude-code": "CLAUDE.md", "codex-cli": "AGENTS.md", "llama-server": None}


def die(msg: str) -> None:
    print(f"gen-agent-docs.py: {msg}", file=sys.stderr)
    sys.exit(2)


def extract_block(protocol_path: str) -> str:
    text = open(protocol_path, encoding="utf-8").read()
    m = re.search(re.escape(BEGIN) + r"\n(.*?)\n" + re.escape(END), text, re.S)
    if not m:
        die(f"{protocol_path}: agent-rules markers not found")
    return m.group(1).strip("\n")


def header(p: dict, chair: dict, log_rel: str, spec_dir: str) -> str:
    base = chair.get("branch", "main")
    bits = [f"You are **{p['id']}**" + (f" ({p['model']})" if p.get("model") else "") + " in this parley.",
            f"Chair: {chair.get('id', '?')} (base branch `{base}`)."]
    if p.get("branch"):
        bits.append(f"Your branch: `{p['branch']}`.")
    if p.get("worktree"):
        bits.append(f"Your worktree: `{p['worktree']}`.")
    if "repo" in p.get("capabilities", []):
        bits.append(f"The log: `{log_rel}` (your copy may lag; the projection you are given is authoritative).")
        bits.append(f"Full spec: `{os.path.join(spec_dir, 'SCHEMA.md')}` and `{os.path.join(spec_dir, 'PROTOCOL.md')}`.")
    else:
        bits.append("You have no repository access: changes reach you as stat lines and prose, and the projection you are given is all you know.")
    return " ".join(bits)


def adapt_base_branch(body: str, base: str) -> str:
    """The rules are written against `main`; a repository whose chair branch is e.g. master gets that instead."""
    if base == "main":
        return body
    return body.replace("`main`", f"`{base}`").replace("main...parley/", f"{base}...parley/")


def region(body: str, p: dict, chair: dict, log_rel: str, protocol_name: str, spec_dir: str) -> str:
    banner = f"<!-- GENERATED from {protocol_name} §8 by tools/gen-agent-docs.py — edit {protocol_name}, not this block -->"
    body = adapt_base_branch(body, chair.get("branch", "main"))
    return "\n".join([BEGIN, banner, header(p, chair, log_rel, spec_dir), "", body, END])


def splice(existing: str | None, new_region: str) -> str:
    if existing is None:
        return new_region + "\n"
    if BEGIN in existing and END in existing:
        pat = re.escape(BEGIN) + r".*?" + re.escape(END)
        return re.sub(pat, lambda _: new_region, existing, count=1, flags=re.S)
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + new_region + "\n"


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
    # PROTOCOL.md lives in the parley spec repo (next to this tools/ dir), not in the target repo
    spec_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    protocol = a.protocol or os.path.join(spec_repo, "PROTOCOL.md")
    reg_path = os.path.join(parley, "participants.json")
    for path in (protocol, reg_path):
        if not os.path.exists(path):
            die(f"not found: {path}")
    registry = json.load(open(reg_path, encoding="utf-8"))
    parts = registry["participants"]
    chair = next((p for p in parts if p.get("role") == "chair"), {})
    body = extract_block(protocol)
    log_rel = os.path.relpath(os.path.join(parley, "log.jsonl"), repo)
    protocol_name = os.path.basename(protocol)
    spec_dir = os.path.dirname(os.path.abspath(protocol))

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
        reg = region(body, p, chair, log_rel, protocol_name, spec_dir)
        if harness == "llama-server":
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
        print(f"wrote {dest}")
        written += 1
    return 0 if (written or a.stdout) else 1


if __name__ == "__main__":
    sys.exit(main())
