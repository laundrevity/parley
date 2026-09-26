#!/usr/bin/env python3
"""A scripted participant for testing the dispatcher without Claude Code or Codex.

Reads a projection on stdin (PROTOCOL §5), parses the headers and the standing block, and
answers mechanically:
  - an owed reply to an ask  -> a `say` re the ask, or a `propose` if the ask's text contains "propose"
  - an owed review           -> `object` (with quote) if the proposal's text contains $FAKE_OBJECT_TO, else `accept`
  - nominated, nothing owed  -> if the delta holds an object to one of my proposals: an amended `propose`
                                (same thread, re = [my proposal, the objection]); else nothing
  - objection pass           -> `object` to any record whose text contains $FAKE_OBJECT_TO and is not mine; else nothing
Knobs (environment): FAKE_OBJECT_TO, FAKE_SLEEP (seconds), FAKE_BAD_ONCE (emit an invalid accept the
first time; marker file in cwd), FAKE_IGNORE_OWED (reply with an unrelated say), FAKE_PROPOSAL_TEXT,
FAKE_DECLINE_NOMINATION (answer a bare nomination with nothing).
Registry: {"harness": "command", "command": ["python3", "<path>/tests/fake_agent.py"]}.
"""
import json
import os
import re
import sys
import time

me = os.environ.get("PARLEY_PARTICIPANT", "fake")
mode = os.environ.get("PARLEY_MODE", "turn")
text = sys.stdin.read()

if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))

HDR = re.compile(r"^\[#(?P<id>[^\]]+)\](?P<self> \(self\))?(?P<new> \(new\))? (?P<kind>\w+) from=(?P<from>\S+) to=(?P<to>\S+)"
                 r"(?: re=(?P<re>\S+))? thread=#(?P<thread>\S+)")

# parse records from the delta
records = []
cur = None
for line in text.splitlines():
    m = HDR.match(line)
    if m:
        cur = dict(m.groupdict(), body=[])
        cur["re"] = [x.lstrip("#") for x in (cur["re"] or "").split(",") if x]
        records.append(cur)
    elif line.startswith("--- standing"):
        cur = None
    elif cur is not None:
        cur["body"].append(line)
for r in records:
    r["text"] = "\n".join(r["body"])
by_id = {r["id"]: r for r in records}

# parse the standing block
owed = re.findall(r"- (reply|accept\|object) re #(\S+) \((\w+) from (\S+), thread #(\S+)\)", text)
nominated = "nominated by: no one" not in text and "nominated by:" in text
obj_to = os.environ.get("FAKE_OBJECT_TO")

out = []
if os.environ.get("FAKE_IGNORE_OWED") and owed:
    out.append({"kind": "say", "thread": owed[0][4], "body": {"text": "unrelated remark"}})
    owed = []

for what, rid, kind, frm, thread in owed:
    src = by_id.get(rid, {"text": ""})
    if what == "reply":
        if "propose" in src["text"].lower():
            out.append({"kind": "propose", "thread": thread, "re": [rid],
                        "body": {"text": os.environ.get("FAKE_PROPOSAL_TEXT", f"Proposal [v1] from {me}: do the thing in one module.")}})
        else:
            out.append({"kind": "say", "thread": thread, "re": [rid], "body": {"text": f"{me}: reply to #{rid} — done."}})
    else:
        bad_marker = os.path.join(os.getcwd(), ".fake_bad_done")
        if os.environ.get("FAKE_BAD_ONCE") and not os.path.exists(bad_marker):
            open(bad_marker, "w").close()
            out.append({"kind": "accept", "thread": thread, "re": [rid], "body": {"text": "lgtm"}})   # invalid: no reasons
        elif obj_to and obj_to in src["text"]:
            out.append({"kind": "object", "thread": thread, "re": [rid],
                        "body": {"quote": obj_to, "reasons": [f"{obj_to} is not acceptable to {me}: it lacks a rollback path."]}})
        else:
            out.append({"kind": "accept", "thread": thread, "re": [rid],
                        "body": {"reasons": [f"{me} checked #{rid} against the ask; consistent.", "No open questions."]}})

if not owed and nominated and mode == "turn" and not os.environ.get("FAKE_DECLINE_NOMINATION"):
    mine = {r["id"] for r in records if r["self"] and r["kind"] == "propose"}
    objs = [r for r in records if r["kind"] == "object" and set(r["re"]) & mine]
    if objs:
        o = objs[-1]
        target = next(x for x in o["re"] if x in mine)
        out.append({"kind": "propose", "thread": o["thread"], "re": [target, o["id"]],
                    "body": {"text": f"Amended proposal [v2] from {me}: the previous design plus a rollback path, answering #{o['id']}."}})

# a chair with ready threads decides them (the standing block of a chair lists them under "decide:")
if re.search(r"^--- standing: you are \S+, chair", text, re.M):
    for root, rest in re.findall(r"^  #(\S+) ready — proposals: (.*)$", text, re.M):
        props = re.findall(r"#(\S+) (?:accepted|contested|pending)", rest)
        accepted = re.findall(r"#(\S+) accepted", rest)
        out.append({"kind": "decide", "thread": root, "re": accepted or props,
                    "body": {"text": f"{me} (chair): closing #{root}; going with " + (", ".join("#" + x for x in accepted) if accepted else "nothing — contested and unresolved") + "."}})

if mode == "objection" and obj_to:
    already = {t for r in records if r["self"] and r["kind"] == "object" for t in r["re"]}
    any_new = any(r["new"] for r in records)
    for r in records:
        if any_new and not r["new"]:
            continue
        if r["id"] in already:
            continue
        if not r["self"] and r["kind"] != "object" and obj_to in r["text"] and r["from"] != me:
            out.append({"kind": "object", "thread": r["thread"], "re": [r["id"]],
                        "body": {"quote": obj_to, "reasons": [f"unbidden: {obj_to} again lacks a rollback path."]}})

print("Some narration the dispatcher must ignore.\n")
print("```jsonl")
for r in out:
    print(json.dumps(r))
print("```")
