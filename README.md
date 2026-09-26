# parley

An n-party conversation format for participants of any kind — humans, models through their CLIs (text-only, or agentic in a repository), local models, plain programs — talking through one shared log. Chair is a role some of them hold, not a kind. The schema is the product; the harness is thin and replaceable.

| | |
|---|---|
| [SCHEMA.md](SCHEMA.md) | the record (JSONL), the six kinds and the obligation each creates, threads, the participant registry, validation rules, JSON Schema |
| [PROTOCOL.md](PROTOCOL.md) | floor control, obligations in time, projections per agent type, how edits become records, the agent rules (§8), operating it (§10) |
| [HANDOFF.md](HANDOFF.md) | the hub-and-spoke predecessor this replaces; SCHEMA.md Appendix A checks the kind set against it |
| `schema/` | `record.schema.json`, `participants.schema.json` — canonical shapes |
| `parley` (console script; `tools/parley` runs it from a checkout) | the chair's command: `init`, `ask/say/propose/accept/object/decide`, `run` (the dispatcher), `status`, `doctor`, `validate`, `project`, `gen-docs` |
| `tools/validate.py` (`parley validate`) | shape check + protocol replay; `--report` prints thread states, open obligations, the chair's queue |
| `tools/project.py` (`parley project`) | one participant's projection: the delta since its last turn plus the standing block |
| `tools/gen-agent-docs.py` (`parley gen-docs`) | writes `CLAUDE.md` / `AGENTS.md` / the llama system prompt from PROTOCOL.md §8 |
| `tools/parleylib/` | the package: validator, projector, rules generator, the appender, the harness adapters (claude-code, codex-cli, llama-server, command, inbox, human), the dispatcher, the CLI |
| `transcripts/example-01.jsonl` | an 18-record three-party exchange over a small code change: objections, an unbidden objection, concurrency, an accept with its non-blocking note as a following `say`, three decides |
| `tests/` | `test_e2e.sh` drives the whole loop with scripted participants; `test_roles.sh` covers text-only, chairless and model-chaired parleys; `test_adapters.py` covers the adapters; `test_protocol.py` the rules the schema cannot express (amendments included) |
| `.parley/` | the template registry `parley init` copies (fable, astra text-only via the CLIs; claude, codex agentic; llama local), and this repository's own (empty) log |

```sh
cd ~/git/parley && uv tool install --editable .     # `parley` on your PATH; editable, so spec edits take effect at once

# a conversation: Fable through Claude Code, Astra through Codex (your logins pay), you as chair; no repo, no tools
parley init --dir ~/talks/topic && cd ~/talks/topic
parley ask fable,astra "<the question>"
parley run                                          # stops when it is your turn, with your queue
parley decide 001 "<ruling>"   # or ask / object / say / propose / accept
parley run

# in a repository: agentic CLIs in worktrees, commits as records
parley init --repo ~/research/anabelian && cd ~/research/anabelian
parley doctor
parley ask claude,codex "<the task>"
parley run
```

```sh
python3 tools/validate.py transcripts/example-01.jsonl --report
python3 tools/project.py --for claude --log transcripts/example-01.jsonl --since 009
bash tests/test_e2e.sh && bash tests/test_roles.sh && python3 tests/test_adapters.py && python3 tests/test_protocol.py
```

Python 3.10+; the one dependency (`jsonschema`) is installed by uv. The package is meant to be installed editable from this checkout: the tools read `schema/`, `PROTOCOL.md` and the template registry from the repository (`PARLEY_HOME` overrides the lookup).
