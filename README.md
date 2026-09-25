# parley

An n-party conversation format: one human chair plus several AI agents (Claude Code, Codex, a local llama.cpp model) working in a shared repository. The schema is the product; the harness is thin and replaceable.

| | |
|---|---|
| [SCHEMA.md](SCHEMA.md) | the record (JSONL), the six kinds and the obligation each creates, threads, the participant registry, validation rules, JSON Schema |
| [PROTOCOL.md](PROTOCOL.md) | floor control, obligations in time, projections per agent type, how edits become records, the agent rules (§8), operating it (§10) |
| [HANDOFF.md](HANDOFF.md) | the hub-and-spoke predecessor this replaces; SCHEMA.md Appendix A checks the kind set against it |
| `schema/` | `record.schema.json`, `participants.schema.json` — canonical shapes |
| `tools/parley` | the chair's command: `init`, `ask/say/propose/accept/object/decide`, `run` (the dispatcher), `status`, `doctor`, `validate`, `project`, `gen-docs` |
| `tools/validate.py` | shape check + protocol replay; `--report` prints thread states, open obligations, the chair's queue |
| `tools/project.py` | one participant's projection: the delta since its last turn plus the standing block |
| `tools/gen-agent-docs.py` | writes `CLAUDE.md` / `AGENTS.md` / the llama system prompt from PROTOCOL.md §8 |
| `tools/parleylib/` | the appender, the harness adapters (claude-code, codex-cli, llama-server, command), the dispatcher, the CLI |
| `transcripts/example-01.jsonl` | a 17-record three-party exchange over a small code change: objections, an unbidden objection, concurrency, three decides |
| `tests/` | `test_e2e.sh` drives the whole loop with scripted agents; `test_adapters.py` covers the CLI adapters |
| `.parley/` | the template registry `parley init` copies, and this repository's own (empty) log |

```sh
ln -s "$PWD/tools/parley" ~/bin/parley              # or add tools/ to PATH

parley init --repo ~/research/anabelian             # .parley/, worktrees, CLAUDE.md / AGENTS.md
cd ~/research/anabelian
parley doctor
parley ask claude "<the task>"
parley run                                          # stops when it is your turn, with your queue
parley decide 001 "<ruling>"   # or ask / object / say / propose / accept
parley run
```

```sh
python3 tools/validate.py transcripts/example-01.jsonl --report
python3 tools/project.py --for claude --log transcripts/example-01.jsonl --since 009
bash tests/test_e2e.sh && python3 tests/test_adapters.py
```

Requires Python 3.10+; `jsonschema` (any version ≥ 3.2) for the full shape check, optional.
