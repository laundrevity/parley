#!/usr/bin/env python3
"""Adapter logic as pure functions: command lines the dispatcher would run for Claude Code
and Codex, parsing of Claude Code's JSON envelope, fence extraction, the llama output schema."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from parleylib import core, harness  # noqa: E402
import validate as V  # noqa: E402


class ClaudeCmd(unittest.TestCase):
    def test_first_turn_has_no_resume_and_safe_defaults(self):
        cmd = harness.claude_cmd({"id": "claude", "harness": "claude-code"}, {}, "/repo")
        self.assertEqual(cmd[:4], ["claude", "-p", "--output-format", "json"])
        self.assertNotIn("--resume", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_resume_uses_stored_session(self):
        cmd = harness.claude_cmd({"id": "claude", "harness": "claude-code"}, {"session_id": "abc"}, "/repo")
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")

    def test_fresh_session_ignores_stored_session(self):
        cmd = harness.claude_cmd({"id": "claude", "session": "fresh"}, {"session_id": "abc"}, "/repo")
        self.assertNotIn("--resume", cmd)

    def test_harness_args_replace_defaults(self):
        p = {"id": "claude", "command": ["/opt/bin/claude"], "harness_args": ["--model", "opus"]}
        cmd = harness.claude_cmd(p, {}, "/repo")
        self.assertEqual(cmd, ["/opt/bin/claude", "-p", "--output-format", "json", "--model", "opus"])

    def test_parse_envelope(self):
        out = json.dumps({"type": "result", "result": "hi\n```jsonl\n{\"kind\":\"say\"}\n```", "session_id": "s1", "is_error": False})
        text, sid, err = harness.parse_claude_output(out)
        self.assertEqual(sid, "s1")
        self.assertFalse(err)
        recs, note = core.extract_records(text)
        self.assertEqual(recs, [{"kind": "say"}])

    def test_parse_plain_text_and_error(self):
        self.assertEqual(harness.parse_claude_output("just text"), ("just text", None, False))
        _, _, err = harness.parse_claude_output(json.dumps({"result": "boom", "is_error": True}))
        self.assertTrue(err)


class CodexCmd(unittest.TestCase):
    def test_exec_shape(self):
        cmd = harness.codex_cmd({"id": "codex", "harness": "codex-cli"}, {}, "/repo", "/wt", "/tmp/o.txt", "PROMPT")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[cmd.index("-C") + 1], "/wt")
        self.assertEqual(cmd[cmd.index("-o") + 1], "/tmp/o.txt")
        self.assertEqual(cmd[-1], "PROMPT")
        self.assertIn("--full-auto", cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertTrue(any("writable_roots" in a and "/repo/.git" in a for a in cmd), cmd)

    def test_resume_last_only_after_first_turn(self):
        p = {"id": "codex", "session": "resume-last"}
        self.assertNotIn("resume", harness.codex_cmd(p, {}, "/r", "/w", "/o", "x")[:3])
        self.assertEqual(harness.codex_cmd(p, {"turns": 1}, "/r", "/w", "/o", "x")[1:4], ["exec", "resume", "--last"])


class Fences(unittest.TestCase):
    def test_fence_wins_over_narration(self):
        recs, note = core.extract_records('I did things.\n```jsonl\n{"kind":"say","body":{"text":"a"}}\n{"kind":"ask"}\n```\nmore words')
        self.assertEqual([r["kind"] for r in recs], ["say", "ask"])

    def test_json_fence_and_array(self):
        recs, _ = core.extract_records('```json\n[{"kind":"say"}]\n```')
        self.assertEqual(recs, [{"kind": "say"}])

    def test_empty_fence_and_no_fence(self):
        self.assertEqual(core.extract_records("```jsonl\n```"), ([], "empty fence"))
        self.assertEqual(core.extract_records("no records here"), ([], "no fence"))

    def test_bare_jsonl(self):
        recs, note = core.extract_records('{"kind":"say"}\n{"kind":"object"}\n')
        self.assertEqual(note, "jsonl")
        self.assertEqual(len(recs), 2)


class LlamaSchema(unittest.TestCase):
    def test_output_schema_is_oneof_per_kind_without_if_then(self):
        rs = V.load_json(os.path.join(HERE, "..", "schema", "record.schema.json"))
        sch = harness.output_schema(rs, "llama", ["object"])
        self.assertEqual(sch["type"], "array")
        self.assertEqual(len(sch["items"]["oneOf"]), 1)
        v = sch["items"]["oneOf"][0]
        self.assertEqual(v["properties"]["kind"], {"const": "object"})
        self.assertIn("re", v["required"])
        self.assertNotIn('"if":', json.dumps(sch))


class Normalize(unittest.TestCase):
    def test_tmp_ids_and_thread_inference(self):
        import tempfile, shutil
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, ".parley"))
            shutil.copy(os.path.join(HERE, "..", "transcripts", "example-01.participants.json"), os.path.join(d, ".parley", "participants.json"))
            pl = core.Parley(os.path.join(d, ".parley"))
            pl.append_turn("conor", [core.build_record("ask", text="go", to=["claude"])])
            with self.assertRaises(core.TurnRejected):   # codex owes nothing and is not nominated
                pl.append_turn("codex", [{"kind": "say", "thread": "001", "body": {"text": "unbidden"}}])
            out = pl.append_turn("claude", [
                {"kind": "say", "re": ["001"], "body": {"text": "reply"}},                         # thread inferred from re
                {"id": "tmp-1", "thread": "tmp-1", "kind": "propose", "body": {"text": "new thread"}},
                {"kind": "ask", "thread": "tmp-1", "re": ["tmp-1"], "to": ["conor"], "body": {"text": "ok?"}},
            ])
            self.assertEqual([o["id"] for o in out], ["002", "003", "004"])
            self.assertEqual(out[0]["thread"], "001")
            self.assertEqual(out[1]["thread"], "003")
            self.assertEqual(out[2]["thread"], "003")
            self.assertEqual(out[2]["re"], ["003"])
            self.assertEqual(out[2]["seen"], "001")
            # after claude's propose to *, codex owes a review and may speak
            pl.append_turn("codex", [{"kind": "accept", "re": ["003"], "body": {"reasons": ["checked"]}}])
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=1)
