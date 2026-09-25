"""Harness adapters: invoke one participant headlessly with a projection, return its records.

Each adapter takes the participant's registry entry, the projection text, the worktree to run
in, a timeout, and the participant's local state (session ids). It returns a TurnResult whose
`records` is a list of record dicts (possibly empty), or None when the invocation itself
failed (timeout, non-zero exit, unreachable server). Parsing the agent's reply is shared:
a ```jsonl fence wins, then bare JSONL, then a JSON array (core.extract_records).

Registry knobs (all optional):
  command       executable or argv list; default per harness (claude / codex)
  harness_args  argv appended to the invocation; REPLACES the defaults from default_args()
  session       "resume" (default for claude-code) | "fresh" (default for codex-cli)
  timeout_s     overrides the latency-class default (minutes: 1800, seconds: 120)
  endpoint      llama-server URL (default http://127.0.0.1:8080/v1/chat/completions)
  max_tokens, temperature   llama-server sampling
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from . import core

TIMEOUTS = {"human": None, "minutes": 1800, "seconds": 120}

# Defaults keep each CLI's own sandbox/permission model on. Headless, neither can answer a
# prompt, so anything outside these defaults is denied rather than asked about — widen
# `harness_args` in participants.json per project (e.g. add "Bash(lake:*)" for a Lean repo).
# The no-sandbox flags (--dangerously-skip-permissions, --dangerously-bypass-approvals-and-sandbox)
# are the chair's call to add, never a default here.
def default_args(harness: str, repo: str) -> list:
    if harness == "claude-code":
        return ["--permission-mode", "acceptEdits", "--allowedTools", "Bash(git:*)"]
    if harness == "codex-cli":
        gitdir = os.path.join(repo, ".git")
        return ["--full-auto", "-c", f'sandbox_workspace_write.writable_roots=["{gitdir}"]']
    return []


@dataclass
class TurnResult:
    records: Optional[list]          # None = invocation failed
    raw: str = ""
    error: Optional[str] = None
    note: str = ""
    session_id: Optional[str] = None
    elapsed: float = 0.0
    cmd: list = field(default_factory=list)


def timeout_for(p: dict) -> Optional[int]:
    if p.get("timeout_s"):
        return int(p["timeout_s"])
    return TIMEOUTS.get(p.get("latency", "minutes"), 1800)


def _argv(p: dict, default: str) -> list:
    c = p.get("command", default)
    return list(c) if isinstance(c, list) else shlex.split(c)


def _run(cmd: list, cwd: Optional[str], stdin_text: Optional[str], timeout: Optional[int], env=None) -> tuple:
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, input=stdin_text, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout}s", time.time() - t0
    except FileNotFoundError as e:
        return None, "", f"command not found: {e.filename}", time.time() - t0
    return proc, proc.stdout, (proc.stderr or "").strip(), time.time() - t0


# ----------------------------------------------------------------------------- adapters

def claude_cmd(p: dict, pstate: dict, repo: str) -> list:
    cmd = _argv(p, "claude") + ["-p", "--output-format", "json"]
    sid = pstate.get("session_id") if p.get("session", "resume") == "resume" else None
    if sid:
        cmd += ["--resume", sid]
    return cmd + list(p.get("harness_args", default_args("claude-code", repo)))


def parse_claude_output(out: str) -> tuple:
    """(text, session_id, is_error) from `claude -p --output-format json`; tolerates plain text."""
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return out, None, False
    if not isinstance(obj, dict):
        return out, None, False
    return (obj.get("result") or ""), obj.get("session_id"), bool(obj.get("is_error"))


def run_claude_code(p: dict, projection: str, cwd: str, timeout: Optional[int], pstate: dict, repo: str) -> TurnResult:
    cmd = claude_cmd(p, pstate, repo)
    sid = pstate.get("session_id") if p.get("session", "resume") == "resume" else None
    proc, out, err, dt = _run(cmd, cwd, projection, timeout)
    if proc is None:
        return TurnResult(None, out, err, elapsed=dt, cmd=cmd)
    text, new_sid, is_error = parse_claude_output(out)
    if is_error:
        return TurnResult(None, out, f"claude reported an error: {text[:300]}", elapsed=dt, cmd=cmd)
    if proc.returncode != 0 and not text.strip():
        return TurnResult(None, out, f"exit {proc.returncode}: {err[:300]}", elapsed=dt, cmd=cmd)
    recs, note = core.extract_records(text)
    return TurnResult(recs, text, None, note, new_sid or sid, dt, cmd)


def codex_cmd(p: dict, pstate: dict, repo: str, cwd: str, outfile: str, projection: str) -> list:
    base = _argv(p, "codex")
    if p.get("session", "fresh") == "resume-last" and pstate.get("turns"):
        cmd = base + ["exec", "resume", "--last"]
    else:
        cmd = base + ["exec"]
    return cmd + ["-C", cwd, "-o", outfile] + list(p.get("harness_args", default_args("codex-cli", repo))) + [projection]


def run_codex_cli(p: dict, projection: str, cwd: str, timeout: Optional[int], pstate: dict, repo: str) -> TurnResult:
    fd, outfile = tempfile.mkstemp(prefix="parley-codex-", suffix=".txt")
    os.close(fd)
    cmd = codex_cmd(p, pstate, repo, cwd, outfile, projection)
    proc, out, err, dt = _run(cmd, cwd, None, timeout)
    try:
        text = open(outfile, encoding="utf-8").read() if os.path.exists(outfile) else ""
    finally:
        try:
            os.unlink(outfile)
        except OSError:
            pass
    if proc is None:
        return TurnResult(None, out, err, elapsed=dt, cmd=cmd)
    if not text.strip():
        text = out  # older builds print the last message to stdout
    if proc.returncode != 0 and not text.strip():
        return TurnResult(None, out, f"exit {proc.returncode}: {err[:300]}", elapsed=dt, cmd=cmd)
    recs, note = core.extract_records(text)
    return TurnResult(recs, text, None, note, None, dt, cmd)


def output_schema(record_schema: dict, pid: str, kinds: list) -> dict:
    """The record schema narrowed to what this participant may emit, as a JSON array, in a
    shape llama.cpp's grammar converter handles (oneOf per kind; no if/then; no id/ts/seen)."""
    defs = record_schema["$defs"]
    body_for = {"say": "sayBody", "ask": "askBody", "propose": "proposeBody",
                "accept": "acceptBody", "object": "objectBody", "decide": "decideBody"}
    variants = []
    for k in kinds:
        props = {
            "kind": {"const": k},
            "thread": {"$ref": "#/$defs/recordId"},
            "re": {"type": "array", "items": {"$ref": "#/$defs/recordId"}},
            "to": {"type": "array", "items": {"type": "string"}},
            "next": {"type": "array", "items": {"$ref": "#/$defs/participantId"}},
            "body": {"$ref": f"#/$defs/{body_for[k]}"},
        }
        req = ["kind", "thread", "body"]
        if k == "ask":
            req.append("to")
        if k in ("accept", "object"):
            req.append("re")
        variants.append({"type": "object", "properties": props, "required": req, "additionalProperties": False})
    return {"type": "array", "items": {"oneOf": variants}, "$defs": defs}


def run_llama_server(p: dict, projection: str, cwd: str, timeout: Optional[int], pstate: dict,
                     system_prompt: str, kinds: list, record_schema: dict) -> TurnResult:
    url = p.get("endpoint", "http://127.0.0.1:8080/v1/chat/completions")
    payload = {
        "model": p.get("model", "default"),
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": projection}],
        "temperature": p.get("temperature", 0.2),
        "max_tokens": p.get("max_tokens", 1500),
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "parley_records", "schema": output_schema(record_schema, p["id"], kinds)}},
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout or 120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return TurnResult(None, "", f"llama-server unreachable at {url}: {e}", elapsed=time.time() - t0)
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return TurnResult(None, json.dumps(data)[:500], "unexpected llama-server response", elapsed=time.time() - t0)
    recs, note = core.extract_records(text)
    return TurnResult(recs, text, None, note, None, time.time() - t0)


def run_command(p: dict, projection: str, cwd: str, timeout: Optional[int], pstate: dict, env_extra: dict) -> TurnResult:
    """Generic adapter (also the test harness): run `command` in the worktree with the
    projection on stdin; the reply is stdout."""
    cmd = _argv(p, "") + p.get("harness_args", [])
    if not cmd:
        return TurnResult(None, "", "command harness needs a `command` in the registry")
    env = dict(os.environ)
    env.update(env_extra)
    proc, out, err, dt = _run(cmd, cwd, projection, timeout, env=env)
    if proc is None:
        return TurnResult(None, out, err, elapsed=dt, cmd=cmd)
    if proc.returncode != 0:
        return TurnResult(None, out, f"exit {proc.returncode}: {err[:300]}", elapsed=dt, cmd=cmd)
    recs, note = core.extract_records(out)
    return TurnResult(recs, out, None, note, None, dt, cmd)


def run_turn(p: dict, projection: str, cwd: Optional[str], pstate: dict, *, mode: str,
             system_prompt: str = "", record_schema: Optional[dict] = None, repo: str = "") -> TurnResult:
    harness = p.get("harness", "")
    timeout = timeout_for(p)
    cwd = cwd or os.getcwd()
    repo = repo or os.getcwd()
    env_extra = {"PARLEY_PARTICIPANT": p["id"], "PARLEY_MODE": mode}
    if harness == "claude-code":
        return run_claude_code(p, projection, cwd, timeout, pstate, repo)
    if harness == "codex-cli":
        return run_codex_cli(p, projection, cwd, timeout, pstate, repo)
    if harness == "llama-server":
        kinds = ["object"] if mode == "objection" else ["say", "ask", "propose", "accept", "object"]
        return run_llama_server(p, projection, cwd, timeout, pstate, system_prompt, kinds, record_schema or {})
    if harness in ("command", "fake"):
        return run_command(p, projection, cwd, timeout, pstate, env_extra)
    return TurnResult(None, "", f"unknown harness {harness!r} for {p['id']}")
