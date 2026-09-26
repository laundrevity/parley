#!/usr/bin/env bash
# Participant-agnostic roles: a text-only parley outside any repo (init --dir), a chairless parley
# that ends quiescent with ready threads open, a model chair that decides, --as resolution.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PARLEY="${PARLEY_CMD:-python3 $HERE/../tools/parley}"
FAKE="$HERE/fake_agent.py"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "### $*"; }
fakeify() {  # make every model participant a fake `command` participant
python3 - "$1" "$FAKE" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
for x in p["participants"]:
    if x.get("kind") == "model":
        x["harness"] = "command"; x["command"] = ["python3", sys.argv[2]]; x["timeout_s"] = 30
        for k in ("effort", "session"): x.pop(k, None)
json.dump(p, open(sys.argv[1], "w"), indent=1)
PY
}

step "text-only parley in a plain directory: conor chairs fable and astra"
D="$T/talk"; $PARLEY init --dir "$D" | tail -4
test -f "$D/.parley/participants.json" || fail "no registry"
test ! -d "$D/.git" || fail "init --dir must not create a repo"
python3 - "$D/.parley/participants.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
ids = {x["id"]: x for x in p["participants"]}
assert set(ids) >= {"conor", "fable", "astra"}, ids.keys()
assert "worktree" not in ids["fable"] and ids["fable"]["harness"] == "claude-code" and ids["astra"]["harness"] == "codex-cli"
assert ids["fable"]["capabilities"] == [] and ids["astra"]["capabilities"] == []
assert ids["conor"].get("role") == "chair"
print("registry ok:", sorted(ids))
PY
fakeify "$D/.parley/participants.json"
cd "$D"
$PARLEY ask fable,astra "Please propose a design for the widget." >/dev/null
FAKE_OBJECT_TO="[v1]" $PARLEY run --no-git 2>"$T/e1" >"$T/o1"; cat "$T/e1"
grep -q "you are conor, chair" "$T/o1" || fail "chair block not printed for conor"
grep -q "ready" "$T/o1" || fail "no ready thread after both accepted"
$PARLEY validate | grep -q "RESULT: OK" || fail "invalid log (text-only)"
# both proposed (each answered the ask with a propose) and each reviewed the other
python3 - .parley/log.jsonl <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
kinds = [(r["from"], r["kind"]) for r in recs]
assert kinds.count(("fable", "propose")) >= 1 and kinds.count(("astra", "propose")) >= 1, kinds
assert any(r["kind"] in ("accept", "object") and r["from"] == "fable" for r in recs)
assert any(r["kind"] in ("accept", "object") and r["from"] == "astra" for r in recs)
print("text-only exchange:", kinds)
PY

step "a nomination is an invitation: a nominee with nothing to add is invoked once, and a decide clears it"
# conor nominates astra with a say (nothing owed); the fake answers a bare nomination with nothing
$PARLEY say "over to astra" --thread 001 --next astra >/dev/null
FAKE_DECLINE_NOMINATION=1 $PARLEY run --no-git --no-objection-pass 2>"$T/e1b" >"$T/o1b" || true
n=$(grep -c "astra: invoking" "$T/e1b" || true)
[ "$n" = "1" ] || { cat "$T/e1b"; fail "nominee invoked $n times; expected exactly once"; }
grep -q "nothing to add" "$T/e1b" || fail "expected the 'nothing to add' line"
$PARLEY status | grep -q "astra nominated" || fail "nomination should still be live in the log (nobody consumed it)"
$PARLEY decide 001 "closing; going with what was accepted" >/dev/null
$PARLEY status | grep -q "astra nominated" && fail "decide should void the thread's live nominations"
$PARLEY validate 2>/dev/null | grep -q "D1" || $PARLEY validate --report | grep -q "nomination" || true
FAKE_DECLINE_NOMINATION=1 $PARLEY run --no-git --no-objection-pass 2>"$T/e1c" >/dev/null || true
grep -q "astra: invoking" "$T/e1c" && fail "nobody should be invoked after the decide"
grep -q "quiet" "$T/e1c" || fail "run after decide should be quiet"

step "chairless parley: ends quiescent, ready threads stay open, decide is illegal"
D2="$T/nochair"; $PARLEY init --dir "$D2" --chair none | tail -3
fakeify "$D2/.parley/participants.json"
cd "$D2"
$PARLEY --as conor ask fable "Please propose a name." >/dev/null
$PARLEY --as conor run --no-git 2>"$T/e2" >"$T/o2"; cat "$T/e2"
# without a chair conor is an ordinary reviewer: fable's proposal obligates him too
grep -q "accept|object re #002" "$T/o2" || fail "conor should owe a review as a plain participant"
$PARLEY --as conor accept 002 "Fine by me: the name is short and unused." >/dev/null
$PARLEY --as conor run --no-git 2>"$T/e2" >"$T/o2"; cat "$T/e2"
grep -q "no chair" "$T/o2" || fail "quiescent report should say there is no chair"
grep -q "quiet" "$T/e2" || fail "run should report quiet"
$PARLEY validate --parley .parley 2>/dev/null | grep -q "ready threads (no chair" || $PARLEY validate | grep -q "ready threads (no chair" || fail "validator should label ready threads as staying open"
if $PARLEY --as conor decide 001 "nope" 2>"$T/e2b"; then fail "decide by a non-chair must be rejected"; fi
grep -q "K1" "$T/e2b" || fail "expected K1 on decide without chair"

step "model chair: fable chairs; astra proposes; fable decides"
D3="$T/modelchair"; $PARLEY init --dir "$D3" --chair fable | tail -3
fakeify "$D3/.parley/participants.json"
cd "$D3"
$PARLEY --as conor ask astra "Please propose a plan." >/dev/null
$PARLEY --as conor run --no-git 2>"$T/e3" >"$T/o3"; cat "$T/e3"
grep -q "accept|object re #002" "$T/o3" || fail "conor (plain participant) should owe the review"
$PARLEY --as conor accept 002 "Plan is concrete and bounded." >/dev/null
$PARLEY --as conor run --no-git 2>"$T/e3" >"$T/o3"; cat "$T/e3"
python3 - .parley/log.jsonl <<'PY'
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1])]
kinds = [(r["from"], r["kind"]) for r in recs]
assert ("astra", "propose") in kinds, kinds
assert ("fable", "decide") in kinds, kinds
d = next(r for r in recs if r["kind"] == "decide")
assert d["thread"] == "001", d
print("model chair decided:", kinds)
PY
$PARLEY validate | grep -q "RESULT: OK" || fail "invalid log (model chair)"
grep -q "decide queue" "$T/e3" || fail "chair should have been invoked for its decide queue"

step "--as is required when ambiguous"
D4="$T/twohumans"; $PARLEY init --dir "$D4" --agents fable | tail -1
python3 - "$D4/.parley/participants.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
p["participants"].append({"id": "milo", "kind": "human", "harness": "human", "latency": "human", "capabilities": []})
json.dump(p, open(sys.argv[1], "w"))
PY
cd "$D4"
if $PARLEY ask fable "hi" 2>"$T/e4"; then fail "ambiguous operator should be an error"; fi
grep -q "say who you are" "$T/e4" || fail "expected the --as hint"
$PARLEY --as milo ask fable "hi from milo" | grep -q "appended" || fail "--as milo should work"
python3 -c "import json; r=[json.loads(l) for l in open('.parley/log.jsonl')]; assert r[-1]['from']=='milo'; print('as milo ok')"

echo; echo "ALL ROLE CHECKS PASSED"
