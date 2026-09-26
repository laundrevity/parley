#!/usr/bin/env python3
"""Protocol rules the schema cannot express, checked through the appender (core.Parley) and the
validator, on synthetic logs. Amendment 1 (PROTOCOL §12) lives here: accept is reasons only, a reply
owed is debt not a gate, and the accept precedes the ask about the same proposal within a turn."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from parleylib import core, validate as V  # noqa: E402

REGISTRY = {"participants": [
    {"id": "conor", "kind": "human", "role": "chair", "latency": "human", "harness": "human"},
    {"id": "fable", "kind": "model", "model": "m", "latency": "minutes", "harness": "command", "command": ["true"], "capabilities": []},
    {"id": "astra", "kind": "model", "model": "m", "latency": "minutes", "harness": "command", "command": ["true"], "capabilities": []},
    {"id": "dispatch", "kind": "tool", "latency": "seconds", "harness": "dispatch"},
]}


def fresh():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".parley"))
    with open(os.path.join(d, ".parley", "participants.json"), "w") as f:
        json.dump(REGISTRY, f)
    open(os.path.join(d, ".parley", "log.jsonl"), "w").close()
    return core.Parley(os.path.join(d, ".parley"), d)


def rec(kind, **kw):
    return core.build_record(kind, **kw)


def findings_of(parley):
    out = []
    parley.replay(parley.records(), out)
    return out


def validate_cli(parley):
    r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "validate.py"), parley.dir],
                       capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


class Amendment1(unittest.TestCase):
    def setUp(self):
        self.p = fresh()
        self.p.append_turn("conor", [rec("ask", text="design?", to=["fable", "astra"], thread="tmp-1") | {"id": "tmp-1"}])
        self.p.append_turn("fable", [rec("propose", text="plan A", thread="001", re_=["001"])])

    def test_accept_is_reasons_only(self):
        bad = {"kind": "accept", "re": ["002"], "body": {"reasons": ["checked"], "text": "a note"}}
        with self.assertRaises(core.TurnRejected) as cm:
            self.p.append_turn("astra", [bad])
        self.assertIn("reasons only", str(cm.exception))
        self.assertEqual(len(self.p.records()), 2, "nothing appended from a rejected turn")

    def test_build_record_drops_text_on_accept_but_not_object(self):
        self.assertNotIn("text", rec("accept", reasons=["r"], text="x", re_=["002"])["body"])
        self.assertIn("text", rec("object", reasons=["r"], text="x", re_=["002"])["body"])

    def test_remark_as_say_after_accept(self):
        self.p.append_turn("astra", [rec("accept", reasons=["checked; consistent with #001"], re_=["002", "001"]),
                                     rec("say", text="one remark, no reply needed", re_=["002"])])
        rp = self.p.replay()
        self.assertEqual(sorted(rp.chair_queue), ["001"])
        self.assertEqual([o for o in rp.obligations.values() if o.party == "fable" and o.kind == "answer"], [])

    def test_reply_debt_is_not_a_gate_and_survives_decide(self):
        # accept first, then the ask: thread is ready although fable owes astra a reply
        self.p.append_turn("astra", [rec("accept", reasons=["checked"], re_=["002", "001"]),
                                     rec("ask", text="would you also cover X?", to=["fable"], re_=["002"])])
        rp = self.p.replay()
        self.assertIn("001", rp.chair_queue, "an open reply must not hold the thread back from ready")
        self.assertEqual([(o.party, o.kind) for o in rp.obligations.values()], [("fable", "answer")])
        self.assertFalse(any(f.code == "O4" for f in findings_of(self.p)), "accept before ask is the right order")
        # the chair decides over it; the reply is still owed
        self.p.append_turn("conor", [rec("decide", text="A it is", thread="001", re_=["002"])])
        rp = self.p.replay()
        self.assertTrue(rp.threads["001"].closed)
        self.assertEqual([(o.party, o.kind, o.rec) for o in rp.obligations.values()], [("fable", "answer", "004")])
        d1 = [f for f in findings_of(self.p) if f.code == "D1"]
        self.assertTrue(d1 and "1 reply(ies) still owed" in d1[0].msg, d1)
        # discharged by a say in the closed thread; after that fable has no floor
        self.p.append_turn("fable", [rec("say", text="declined: out of scope for A", re_=["004"])])
        self.assertEqual(list(self.p.replay().obligations), [])
        with self.assertRaises(core.TurnRejected):
            self.p.append_turn("fable", [rec("say", text="more", thread="001")])
        code, out = validate_cli(self.p)
        self.assertEqual(code, 0, out)

    def test_ask_before_accept_warns_O4(self):
        self.p.append_turn("astra", [rec("ask", text="also X?", to=["fable"], re_=["002"]),
                                     rec("accept", reasons=["checked"], re_=["002", "001"])])
        codes = [f.code for f in findings_of(self.p)]
        self.assertIn("O4", codes)
        code, out = validate_cli(self.p)
        self.assertEqual(code, 0, "O4 is a warning, not a violation")

    def test_pre_amendment_accept_text_is_tolerated_with_A1(self):
        # a log written before the amendment: hand-append a legacy accept with an old ts
        log = self.p.log_path
        with open(log, "a") as f:
            f.write(json.dumps({"id": "003", "ts": "2026-09-25T18:38:33Z", "from": "astra", "re": ["002", "001"], "thread": "001",
                                "kind": "accept", "body": {"reasons": ["checked"], "text": "legacy remark"}, "seen": "002"}) + "\n")
        code, out = validate_cli(self.p)
        self.assertEqual(code, 0, out)
        self.assertIn("A1", out)
        # but the same record stamped after the amendment is a violation
        with open(log) as f:
            lines = f.read().splitlines()
        legacy = json.loads(lines[-1]); legacy["ts"] = "2026-09-26T05:00:00Z"
        with open(log, "w") as f:
            f.write("\n".join(lines[:-1] + [json.dumps(legacy)]) + "\n")
        code, out = validate_cli(self.p)
        self.assertEqual(code, 1, out)
        self.assertIn("reasons only", out)


class Nominations(unittest.TestCase):
    def test_decide_voids_the_threads_nominations_but_not_its_own(self):
        p = fresh()
        p.append_turn("conor", [rec("ask", text="q", to=["fable"], thread="tmp-1") | {"id": "tmp-1"}])
        p.append_turn("fable", [rec("propose", text="P", thread="001", re_=["001"])])
        p.append_turn("astra", [rec("object", reasons=["r"], quote="P", re_=["002"])])   # nominates fable by default
        self.assertEqual(p.replay().nominations.get("fable"), ["003"])
        p.append_turn("conor", [rec("decide", text="closing", thread="001", re_=["002"], next_=["astra"])])
        rp = p.replay()
        self.assertEqual(rp.nominations.get("fable", []), [])
        self.assertEqual(rp.nominations.get("astra"), ["004"], "the decide's own next stands")

    def test_nomination_survives_a_concurrent_turn_that_did_not_see_it(self):
        p = fresh()
        p.append_turn("conor", [rec("ask", text="q", to=["fable", "astra"], thread="tmp-1") | {"id": "tmp-1"}])
        p.append_turn("fable", [rec("propose", text="P", thread="001", re_=["001"])])
        # astra's turn was written having seen only #001 (concurrent with #002) and nominates nobody...
        p.append_turn("astra", [rec("say", text="thinking", re_=["001"], next_=["fable"])], seen="001")
        # ...fable's next turn, written having seen #002 only, must not consume #003's nomination of it
        p.append_turn("fable", [rec("say", text="addendum", re_=["001"])], seen="002")
        self.assertEqual(p.replay().nominations.get("fable"), ["003"])
        p.append_turn("fable", [rec("say", text="answering the nomination", re_=["003"])])
        self.assertEqual(p.replay().nominations.get("fable", []), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
