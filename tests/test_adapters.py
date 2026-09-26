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
from parleylib import validate as V  # noqa: E402


class ClaudeCmd(unittest.TestCase):
    def test_first_turn_has_no_resume_and_safe_defaults(self):
        cmd = harness.claude_cmd({"id": "claude", "harness": "claude-code", "capabilities": ["repo"]}, {}, "/repo")
        self.assertEqual(cmd[:4], ["claude", "-p", "--output-format", "json"])
        self.assertNotIn("--resume", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_text_only_through_the_clis(self):
        fable = {"id": "fable", "harness": "claude-code", "capabilities": [], "effort": "max", "extra_args": ["--model", "fable"]}
        cmd = harness.claude_cmd(fable, {}, "/dir", "THE RULES")
        self.assertIn("--disallowedTools", cmd); self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], "*")
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], "THE RULES")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertNotIn("--permission-mode", cmd)
        astra = {"id": "astra", "harness": "codex-cli", "capabilities": [], "effort": "max", "extra_args": ["-m", "gpt-6-astra"]}
        c = harness.codex_cmd(astra, {}, "/dir", "/dir", "/o", "PROJ", "THE RULES")
        self.assertEqual(c[c.index("--sandbox") + 1], "read-only")
        self.assertIn("--skip-git-repo-check", c)
        self.assertIn('model_reasoning_effort="max"', c)
        self.assertTrue(c[-1].startswith("YOUR RULES") and c[-1].endswith("PROJ"))
        self.assertNotIn("workspace-write", c)

    def test_resume_uses_stored_session(self):
        cmd = harness.claude_cmd({"id": "claude", "harness": "claude-code"}, {"session_id": "abc"}, "/repo")
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")

    def test_fresh_session_ignores_stored_session(self):
        cmd = harness.claude_cmd({"id": "claude", "session": "fresh"}, {"session_id": "abc"}, "/repo")
        self.assertNotIn("--resume", cmd)

    def test_extra_args_append_after_defaults(self):
        cmd = harness.claude_cmd({"id": "claude", "capabilities": ["repo"], "extra_args": ["--model", "opus", "--effort", "high"]}, {}, "/repo")
        self.assertIn("--permission-mode", cmd)               # defaults kept
        self.assertEqual(cmd[-4:], ["--model", "opus", "--effort", "high"])
        c2 = harness.codex_cmd({"id": "codex", "capabilities": ["repo"], "extra_args": ["-m", "gpt-5-codex", "-c", 'model_reasoning_effort="high"']}, {}, "/r", "/w", "/o", "PROMPT")
        self.assertIn("--sandbox", c2)
        self.assertEqual(c2[-5:-1], ["-m", "gpt-5-codex", "-c", 'model_reasoning_effort="high"'])
        env = harness._env({"env": {"MAX_THINKING_TOKENS": "32000"}})
        self.assertEqual(env["MAX_THINKING_TOKENS"], "32000")

    def test_harness_args_replace_defaults(self):
        p = {"id": "claude", "capabilities": ["repo"], "command": ["/opt/bin/claude"], "harness_args": ["--model", "opus"]}
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
        cmd = harness.codex_cmd({"id": "codex", "harness": "codex-cli", "capabilities": ["repo"]}, {}, "/repo", "/wt", "/tmp/o.txt", "PROMPT")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertEqual(cmd[cmd.index("-C") + 1], "/wt")
        self.assertEqual(cmd[cmd.index("-o") + 1], "/tmp/o.txt")
        self.assertEqual(cmd[-1], "PROMPT")
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "workspace-write")
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

    def test_nested_fence_inside_body(self):
        t = 'narration\n```jsonl\n{"kind":"propose","thread":"#001","re":["#001"],"body":"Scope:\\n```lean\\ntheorem t : True := trivial\\n```\\nend"}\n```\ntail'
        recs, note = core.extract_records(t)
        self.assertEqual(note, "fence")
        self.assertIn("```lean", recs[0]["body"])
        r = core.Parley.lenient(dict(recs[0]))
        self.assertEqual((r["thread"], r["re"], r["body"]["text"][:6]), ("001", ["001"], "Scope:"))

    def test_pretty_printed_object_in_json_fence(self):
        recs, note = core.extract_records('```json\n{\n  "kind": "accept",\n  "re": ["002"],\n  "body": {"reasons": ["a"]}\n}\n```')
        self.assertEqual(recs[0]["kind"], "accept")

    def test_bare_jsonl(self):
        recs, note = core.extract_records('{"kind":"say"}\n{"kind":"object"}\n')
        self.assertEqual(note, "bare jsonl")
        self.assertEqual(len(recs), 2)


class ApiAdapters(unittest.TestCase):
    def test_api_keys_never_reach_the_clis(self):
        saved = {k: os.environ.get(k) for k in harness.API_KEY_VARS}
        try:
            for k in harness.API_KEY_VARS:
                os.environ[k] = "sk-should-not-leak"
            env = harness._env({"id": "fable", "env": {"KEEP": "1"}})
            self.assertTrue(all(k not in env for k in harness.API_KEY_VARS))
            self.assertEqual(env["KEEP"], "1")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v




    def test_inbox_roundtrip(self):
        import tempfile, threading, time
        d = tempfile.mkdtemp()
        p = {"id": "milo", "kind": "human", "harness": "human", "mode": "inbox"}
        def reply():
            path = os.path.join(d, "inbox", "milo.md")
            while not os.path.exists(path):
                time.sleep(0.1)
            self.assertIn("PROJECTION", open(path).read())
            open(os.path.join(d, "inbox", "milo.reply.md"), "w").write("thoughts\n```jsonl\n{\"kind\":\"say\",\"thread\":\"001\",\"body\":{\"text\":\"hi\"}}\n```\n")
        t = threading.Thread(target=reply); t.start()
        r = harness.run_inbox(p, "PROJECTION", 10, d, "turn")
        t.join()
        self.assertEqual(r.records[0]["body"]["text"], "hi")
        self.assertFalse(os.path.exists(os.path.join(d, "inbox", "milo.reply.md")))

    def test_rules_by_capability_and_role(self):
        from parleylib import gendocs
        reg = {"participants": [
            {"id": "conor", "kind": "human", "role": "chair", "latency": "human"},
            {"id": "fable", "kind": "model", "model": "m", "harness": "claude-code", "latency": "minutes", "capabilities": []},
            {"id": "codex", "kind": "model", "model": "m", "harness": "codex-cli", "latency": "minutes", "capabilities": ["repo"], "branch": "parley/codex"},
            {"id": "arb", "kind": "model", "model": "m", "harness": "codex-cli", "latency": "minutes", "role": "chair", "capabilities": []}]}
        fable = gendocs.render_rules(reg["participants"][1], reg)
        codex = gendocs.render_rules(reg["participants"][2], reg)
        arb = gendocs.render_rules(reg["participants"][3], reg)
        self.assertNotIn("### Repository", fable); self.assertNotIn("### As a chair", fable)
        self.assertIn("### Repository", codex); self.assertNotIn("### As a chair", codex)
        self.assertIn("### As a chair", arb); self.assertNotIn("### Repository", arb)
        self.assertIn("one of the chairs (conor, arb)", arb)
        self.assertNotIn("<!--", fable)


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
            # chair decides thread 003; a follow-up ask that re's the decide roots a NEW thread
            dec = pl.append_turn("conor", [core.build_record("decide", text="go", thread="003", re_=["003"])])[0]
            ask = pl.append_turn("conor", [core.build_record("ask", text="implement", to=["codex"], re_=[dec["id"]])])[0]
            self.assertEqual(ask["thread"], ask["id"])
            self.assertEqual(ask["re"], [dec["id"]])
            # but a plain say still lands in the closed thread
            say = pl.append_turn("conor", [core.build_record("say", text="fyi", re_=[dec["id"]])])[0]
            self.assertEqual(say["thread"], "003")
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=1)
