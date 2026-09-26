"""Harness adapters: invoke one participant headlessly with a projection, return its records.

Each adapter takes the participant's registry entry, the projection text, the worktree to run
in, a timeout, and the participant's local state (session ids). It returns a TurnResult whose
`records` is a list of record dicts (possibly empty), or None when the invocation itself
failed (timeout, non-zero exit, unreachable server). Parsing the agent's reply is shared:
a ```jsonl fence wins, then bare JSONL, then a JSON array (core.extract_records).

Registry knobs (all optional):
  command       executable or argv list; default per harness (claude / codex)
  harness_args  argv appended to the invocation; REPLACES the defaults from default_args()
  extra_args    argv appended AFTER the defaults (or after harness_args): model, effort, anything else
                  claude-code: ["--model", "opus"]        codex-cli: ["-m", "gpt-5-codex", "-c", "model_reasoning_effort=\"high\""]
  env           environment variables for the CLI process, e.g. {"MAX_THINKING_TOKENS": "32000"} for claude-code
  session       "resume" (default for claude-code) | "fresh" (default for codex-cli)
  timeout_s     overrides the latency-class default (minutes: 3600, seconds: 120)
  endpoint      llama-server URL (default http://127.0.0.1:8080/v1/chat/completions)
  max_tokens, temperature   llama-server sampling
  effort        reasoning effort, one knob: claude-code -> --effort, codex-cli -> -c model_reasoning_effort
                  (low|medium|high|xhigh|max; codex also minimal)
  mode          human harness: exit (default: the run stops when it is their turn) | prompt | inbox

Text-only participants (a CLI with capabilities [] and no worktree, llama-server, command, inbox) get the
rules rendered from PROTOCOL §8 for their capabilities and role (gendocs.render_rules) with the turn.

Billing: parley only ever drives the CLIs under their own logins. ANTHROPIC_API_KEY / OPENAI_API_KEY are
stripped from every child environment (API_KEY_VARS) because Claude Code and Codex would otherwise bill
the API instead of the subscription. There is no per-token API path in parley.
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

TIMEOUTS = {"human": None, "minutes": 3600, "seconds": 120}

# Defaults keep each CLI's own sandbox/permission model on. Headless, neither can answer a
# prompt, so anything outside these defaults is denied rather than asked about — widen
# `harness_args` in participants.json per project (e.g. add "Bash(lake:*)" for a Lean repo).
# The no-sandbox flags (--dangerously-skip-permissions, --dangerously-bypass-approvals-and-sandbox)
# are the chair's call to add, never a default here.
def text_only(p: dict) -> bool:
    """A participant without the repo capability is text in, text out — even through an agentic CLI,
    which is how a subscription (rather than API credit) pays for a text-only Fable or GPT."""
    return "repo" not in (p.get("capabilities") or [])


def default_args(harness: str, repo: str, p: Optional[dict] = None) -> list:
    p = p or {}
    if harness == "claude-code":
        if text_only(p):
            return ["--disallowedTools", "*"]                     # no tools at all: a pure text turn
        return ["--permission-mode", "acceptEdits", "--allowedTools", "Bash(git:*)"]
    if harness == "codex-cli":
        if text_only(p):
            return ["--sandbox", "read-only", "--skip-git-repo-check"]   # a text-only parley need not be a repo
        # `codex exec` has no --full-auto; its sandbox flag is --sandbox. workspace-write confines
        # writes to the worktree, and a worktree's git dir lives under the main repo's .git,
        # so that is added as a writable root or commits fail.
        gitdir = os.path.join(repo, ".git")
        return ["--sandbox", "workspace-write", "-c", f'sandbox_workspace_write.writable_roots=["{gitdir}"]']
    return []


def effort_args(harness: str, p: dict) -> list:
    """The `effort` knob for the CLIs (the API adapters read it themselves)."""
    e = p.get("effort")
    if not e:
        return []
    if harness == "claude-code":
        return ["--effort", str(e)]
    if harness == "codex-cli":
        return ["-c", f'model_reasoning_effort="{e}"']
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


# The CLIs bill the API instead of the user's subscription when these are set. parley never
# uses per-token API access, so they are removed from every child environment.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")


def _env(p: dict, extra: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    for k in API_KEY_VARS:
        env.pop(k, None)
    env.update({str(k): str(v) for k, v in (p.get("env") or {}).items()})
    if extra:
        env.update(extra)
    return env


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

def claude_cmd(p: dict, pstate: dict, repo: str, system_prompt: str = "") -> list:
    cmd = _argv(p, "claude") + ["-p", "--output-format", "json"]
    sid = pstate.get("session_id") if p.get("session", "resume") == "resume" else None
    if sid:
        cmd += ["--resume", sid]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]      # the rules, when no CLAUDE.md carries them
    return (cmd + list(p.get("harness_args", default_args("claude-code", repo, p)))
            + effort_args("claude-code", p) + list(p.get("extra_args", [])))


def parse_claude_output(out: str) -> tuple:
    """(text, session_id, is_error) from `claude -p --output-format json`; tolerates plain text."""
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return out, None, False
    if not isinstance(obj, dict):
        return out, None, False
    return (obj.get("result") or ""), obj.get("session_id"), bool(obj.get("is_error"))


SESSION_LOST_MARKERS = ("no conversation found", "session id", "session not found", "could not resume",
                        "unable to resume", "no session")


def session_lost(res) -> bool:
    """Does this failed result say the resumed CLI session is gone (rather than that the turn failed)?"""
    blob = ((res.error or "") + " " + (res.raw or "")[:2000]).lower()
    return any(m in blob for m in SESSION_LOST_MARKERS)


def run_claude_code(p: dict, projection: str, cwd: str, timeout: Optional[int], pstate: dict, repo: str,
                    system_prompt: str = "") -> TurnResult:
    cmd = claude_cmd(p, pstate, repo, system_prompt)
    sid = pstate.get("session_id") if p.get("session", "resume") == "resume" else None
    proc, out, err, dt = _run(cmd, cwd, projection, timeout, env=_env(p))
    if proc is None:
        return TurnResult(None, out, err, elapsed=dt, cmd=cmd)
    text, new_sid, is_error = parse_claude_output(out)
    if is_error:
        return TurnResult(None, out, f"claude reported an error: {text[:300]}", elapsed=dt, cmd=cmd)
    if proc.returncode != 0 and not text.strip():
        return TurnResult(None, out, f"exit {proc.returncode}: {err[:300]}", elapsed=dt, cmd=cmd)
    recs, note = core.extract_records(text)
    return TurnResult(recs, text, None, note, new_sid or sid, dt, cmd)


def codex_cmd(p: dict, pstate: dict, repo: str, cwd: str, outfile: str, projection: str, system_prompt: str = "") -> list:
    base = _argv(p, "codex")
    if p.get("session", "fresh") == "resume-last" and pstate.get("turns"):
        cmd = base + ["exec", "resume", "--last"]
    else:
        cmd = base + ["exec"]
    if system_prompt:                                          # codex has no system-prompt flag: rules lead the prompt
        projection = "YOUR RULES\n\n" + system_prompt.strip() + "\n\n---\n\nTHE LOG\n\n" + projection
    return (cmd + ["-C", cwd, "-o", outfile] + list(p.get("harness_args", default_args("codex-cli", repo, p)))
            + effort_args("codex-cli", p) + list(p.get("extra_args", [])) + [projection])


def run_codex_cli(p: dict, projection: str, cwd: str, timeout: Optional[int], pstate: dict, repo: str,
                  system_prompt: str = "") -> TurnResult:
    fd, outfile = tempfile.mkstemp(prefix="parley-codex-", suffix=".txt")
    os.close(fd)
    cmd = codex_cmd(p, pstate, repo, cwd, outfile, projection, system_prompt)
    proc, out, err, dt = _run(cmd, cwd, None, timeout, env=_env(p))
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


def run_inbox(p: dict, projection: str, timeout: Optional[int], state_dir: str, mode: str) -> TurnResult:
    """Any party that reads a file and writes a file: a human elsewhere, a cron job, another
    system. The projection goes to .parley/inbox/<id>.md; the reply is awaited at
    .parley/inbox/<id>.reply.md (a fenced JSONL block, or bare JSONL)."""
    inbox = os.path.join(state_dir, "inbox")
    os.makedirs(inbox, exist_ok=True)
    prompt_path = os.path.join(inbox, f"{p['id']}.md")
    reply_path = os.path.join(inbox, f"{p['id']}.reply.md")
    if os.path.exists(reply_path):
        os.remove(reply_path)
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(f"<!-- parley: reply by writing {reply_path} (a ```jsonl fence) -->\n\n{projection}\n")
    t0 = time.time()
    while not os.path.exists(reply_path):
        if timeout and time.time() - t0 > timeout:
            return TurnResult(None, "", f"no reply in {reply_path} within {timeout}s", elapsed=time.time() - t0)
        time.sleep(2)
    time.sleep(0.5)                                   # let the writer finish
    text = open(reply_path, encoding="utf-8").read()
    os.remove(reply_path)
    recs, note = core.extract_records(text)
    return TurnResult(recs, text, None, note, None, time.time() - t0, ["inbox", reply_path])


def run_command(p: dict, projection: str, cwd: str, timeout: Optional[int], pstate: dict, env_extra: dict) -> TurnResult:
    """Generic adapter (also the test harness): run `command` in the worktree with the
    projection on stdin; the reply is stdout."""
    cmd = _argv(p, "") + p.get("harness_args", [])
    if not cmd:
        return TurnResult(None, "", "command harness needs a `command` in the registry")
    proc, out, err, dt = _run(cmd, cwd, projection, timeout, env=_env(p, env_extra))
    if proc is None:
        return TurnResult(None, out, err, elapsed=dt, cmd=cmd)
    if proc.returncode != 0:
        return TurnResult(None, out, f"exit {proc.returncode}: {err[:300]}", elapsed=dt, cmd=cmd)
    recs, note = core.extract_records(out)
    return TurnResult(recs, out, None, note, None, dt, cmd)


def run_turn(p: dict, projection: str, cwd: Optional[str], pstate: dict, *, mode: str,
             system_prompt: str = "", record_schema: Optional[dict] = None, repo: str = "",
             state_dir: str = "") -> TurnResult:
    harness = p.get("harness", "")
    timeout = timeout_for(p)
    cwd = cwd or os.getcwd()
    repo = repo or os.getcwd()
    state_dir = state_dir or os.path.join(repo, ".parley")
    env_extra = {"PARLEY_PARTICIPANT": p["id"], "PARLEY_MODE": mode}
    if harness == "claude-code":
        return run_claude_code(p, projection, cwd, timeout, pstate, repo, system_prompt)
    if harness == "codex-cli":
        return run_codex_cli(p, projection, cwd, timeout, pstate, repo, system_prompt)
    if harness == "llama-server":
        kinds = ["object"] if mode == "objection" else ["say", "ask", "propose", "accept", "object"]
        if p.get("role") == "chair" and mode != "objection":
            kinds.append("decide")
        return run_llama_server(p, projection, cwd, timeout, pstate, system_prompt, kinds, record_schema or {})
    if harness in ("command", "fake"):
        return run_command(p, projection, cwd, timeout, pstate, {**env_extra, "PARLEY_RULES": system_prompt})
    if harness in ("inbox",) or (harness == "human" and p.get("mode") == "inbox"):
        return run_inbox(p, projection, timeout, state_dir, mode)
    return TurnResult(None, "", f"unknown harness {harness!r} for {p['id']}")
