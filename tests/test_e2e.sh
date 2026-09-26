#!/usr/bin/env bash
# End-to-end: a fresh git repo, `parley init`, fake agents, a full exchange
# (ask → propose → object → amended propose → accept → ready → decide → objection pass),
# plus concurrency, a rejected turn with retry, and a timeout. Exits non-zero on any failure.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PARLEY="${PARLEY_CMD:-python3 $HERE/../tools/parley}"   # override: PARLEY_CMD="uv run parley"
FAKE="$HERE/fake_agent.py"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "### $*"; }

step "init"
R="$T/proj"; mkdir -p "$R"; git -C "$R" init -q -b main; echo hi > "$R/README"; git -C "$R" add . ; git -C "$R" commit -qm init
$PARLEY init --repo "$R" --agents claude,codex
python3 - "$R/.parley/participants.json" "$FAKE" <<'EOF'
import json, sys
p = json.load(open(sys.argv[1]))
for x in p["participants"]:
    if x.get("kind") == "model":
        x["harness"] = "command"; x["command"] = ["python3", sys.argv[2]]; x["timeout_s"] = 30
        x.pop("session", None)
json.dump(p, open(sys.argv[1], "w"), indent=1)
EOF
test -d "$T/proj-claude" && test -d "$T/proj-codex" || fail "worktrees not created"
grep -q "parley:agent-rules:begin" "$T/proj-claude/CLAUDE.md" && grep -q "parley:agent-rules:begin" "$T/proj-codex/AGENTS.md" || fail "agent docs not generated into the worktrees"
test -z "$(git -C "$T/proj-claude" status --porcelain)" && test -z "$(git -C "$T/proj-codex" status --porcelain)" || fail "generated docs left the worktrees dirty"
grep -q ".parley/state.json" "$R/.gitignore" || fail ".gitignore not updated"

step "chair asks claude for a proposal; codex objects to [v1]; claude amends; codex accepts"
cd "$R"
$PARLEY ask claude "Please propose a design for the widget."
FAKE_OBJECT_TO="[v1]" $PARLEY run --no-git 2>"$T/run1.err" | tee "$T/run1.out"
cat "$T/run1.err"
grep -q "decide:" "$T/run1.out" || fail "chair block not printed"
grep -q "ready" "$T/run1.out" || fail "thread not ready after accept"
$PARLEY validate | tee "$T/val1.out" | tail -3
grep -q "RESULT: OK" "$T/val1.out" || fail "log invalid after run 1"
# expected kinds: ask, propose(v1), object, propose(v2), accept
python3 - "$R/.parley/log.jsonl" <<'EOF'
import json, sys
kinds = [json.loads(l)["kind"] for l in open(sys.argv[1])]
assert kinds == ["ask", "propose", "object", "propose", "accept"], kinds
recs = [json.loads(l) for l in open(sys.argv[1])]
assert recs[3]["re"] == ["002", "003"], recs[3]["re"]          # amended propose re [v1, objection]
assert recs[2]["seen"] == "002" and recs[4]["seen"] == "004", [r["seen"] for r in recs]
assert all(r["thread"] == "001" for r in recs)
print("kinds and seen values as expected:", kinds)
EOF

step "chair decides; objection pass is quiet; run stops at the chair's turn"
$PARLEY decide 001 "Go with v2."
FAKE_OBJECT_TO="[v1]" $PARLEY run --no-git 2>"$T/run2.err" | tee "$T/run2.out"
cat "$T/run2.err"
grep -q "objection pass" "$T/run2.err" || fail "no objection pass ran"
grep -q "floor: quiet" "$T/run2.out" || fail "floor not quiet after decide"
test "$(wc -l < .parley/log.jsonl)" -eq 6 || fail "unexpected records after objection pass: $(wc -l < .parley/log.jsonl)"

step "unbidden objection: chair says something objectionable; objection pass catches it"
$PARLEY say "Plan: ship [v1] anyway."
FAKE_OBJECT_TO="[v1]" $PARLEY run --no-git 2>"$T/run3.err" >"$T/run3.out"
cat "$T/run3.err"
python3 - "$R/.parley/log.jsonl" <<'EOF'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
objs = [r for r in recs if r["kind"] == "object" and r["re"] == ["007"]]
assert len(objs) == 2, [(r["from"], r["re"]) for r in recs[6:]]     # both agents objected, unbidden
print("unbidden objections from:", sorted(r["from"] for r in objs))
EOF
$PARLEY validate | grep -q "RESULT: OK" || fail "log invalid after unbidden objections"

step "concurrency: ask both at once; both answer in one round"
$PARLEY decide 007 "Noted; not shipping v1."
$PARLEY ask claude,codex "Status?"
$PARLEY run --no-git --no-objection-pass 2>"$T/run4.err" >/dev/null
grep -c "record(s) appended" "$T/run4.err" | grep -q 2 || fail "expected two appended turns"
python3 - "$R/.parley/log.jsonl" <<'EOF'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
last2 = recs[-2:]
assert {r["from"] for r in last2} == {"claude", "codex"} and all(r["kind"] == "say" for r in last2)
assert last2[0]["seen"] == last2[1]["seen"], "both turns should share the same seen (concurrent)"
print("concurrent turns ok; seen =", last2[0]["seen"])
EOF
$PARLEY validate | grep -q "C1" && echo "validator reports the concurrency (C1)" || fail "C1 not reported"

step "rejected turn is retried once and then succeeds"
$PARLEY propose "Chair proposal: rename the widget."
FAKE_BAD_ONCE=1 $PARLEY run --no-git --only codex --no-objection-pass 2>"$T/run5.err" >/dev/null
cat "$T/run5.err"
grep -q "retrying once" "$T/run5.err" || fail "no retry on rejected turn"
grep -q "1 record(s) appended" "$T/run5.err" || fail "retry did not succeed"

step "timeout: dispatch note appended, obligation stays open"
python3 - "$R/.parley/participants.json" <<'EOF'
import json, sys
p = json.load(open(sys.argv[1]))
for x in p["participants"]:
    if x["id"] == "claude": x["timeout_s"] = 1
json.dump(p, open(sys.argv[1], "w"), indent=1)
EOF
FAKE_SLEEP=3 $PARLEY run --no-git --only claude --no-objection-pass 2>"$T/run6.err" >/dev/null || true
cat "$T/run6.err"
grep -q "FAILED" "$T/run6.err" || fail "timeout not reported"
tail -1 .parley/log.jsonl | grep -q '"from": "dispatch"\|"from":"dispatch"' || fail "no dispatch note after timeout"
$PARLEY status > "$T/status6.txt"; cat "$T/status6.txt"; grep -q "claude owes review" "$T/status6.txt" || fail "obligation should still be open after timeout"
$PARLEY validate | grep -q "RESULT: OK" || fail "log invalid at the end"

step "dry run prints projections and sends nothing"
n_before=$(wc -l < .parley/log.jsonl)
$PARLEY run --no-git --dry-run --only codex 2>/dev/null | grep -q "===== projection for codex (objection)" || fail "dry run printed nothing"
test "$(wc -l < .parley/log.jsonl)" -eq "$n_before" || fail "dry run appended records"

echo; echo "ALL E2E CHECKS PASSED ($(wc -l < .parley/log.jsonl) records)"
