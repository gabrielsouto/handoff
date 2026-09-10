#!/usr/bin/env python3
"""Unit tests for tools/handoff.py - standard library only.

Run with:
    python3 -m unittest discover -s tests -v
    python3 tests/tools/test_handoff.py

Fixtures are synthetic and minimal. They reproduce only the record shapes
observed in real Claude Code and Codex transcripts; no real transcript is
copied into the repository.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import handoff  # noqa: E402


HAS_GIT = shutil.which("git") is not None


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            if isinstance(record, str):
                handle.write(record + "\n")
            else:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# --------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------


class TestUtilities(TempDirCase):
    def test_truncate_marks_the_cut_explicitly(self):
        text = "x" * 500
        result = handoff.truncate_text(text, 100, "tool result")
        self.assertIn("[tool result truncated: original length 500 chars]", result)
        self.assertLess(len(result), 200)

    def test_truncate_leaves_short_text_untouched(self):
        self.assertEqual(handoff.truncate_text("short", 100), "short")
        self.assertEqual(handoff.truncate_text("", 10), "")
        self.assertEqual(handoff.truncate_text(None, 10), "")

    def test_atomic_write_replaces_and_leaves_no_temp_file(self):
        target = self.tmp / "out.md"
        handoff.atomic_write(target, "first")
        handoff.atomic_write(target, "second")
        self.assertEqual(target.read_text(encoding="utf-8"), "second")
        leftovers = [p for p in self.tmp.iterdir() if p.name != "out.md"]
        self.assertEqual(leftovers, [])

    def test_atomic_write_creates_missing_parents(self):
        target = self.tmp / "a" / "b" / "c.md"
        handoff.atomic_write(target, "content")
        self.assertTrue(target.is_file())

    def test_sha256_file_streams_the_same_digest_as_hashlib(self):
        import hashlib
        payload = ("line\n" * 5000).encode("utf-8")
        target = self.tmp / "big.bin"
        target.write_bytes(payload)
        self.assertEqual(handoff.sha256_file(target), hashlib.sha256(payload).hexdigest())

    def test_sha256_file_returns_none_for_missing_file(self):
        self.assertIsNone(handoff.sha256_file(self.tmp / "nope.bin"))

    def test_human_size(self):
        self.assertEqual(handoff.human_size(512), "512 B")
        self.assertEqual(handoff.human_size(1536), "1.5 KiB")
        self.assertTrue(handoff.human_size(30 * 1024 * 1024).endswith("MiB"))

    def test_short_head_abbreviates_shas_but_keeps_placeholders(self):
        self.assertEqual(handoff.short_head("a" * 40), "a" * 12)
        self.assertEqual(handoff.short_head("(no commits yet)"), "(no commits yet)")

    def test_safe_filename_component_passes_ordinary_ids_through(self):
        self.assertEqual(handoff.safe_filename_component("4114df3c"), "4114df3c")
        self.assertEqual(handoff.safe_filename_component("claude"), "claude")

    def test_safe_filename_component_strips_path_separators(self):
        for value in ("/", "\\", "a/b", "a\\b", "..", ".", "../../etc/passwd",
                      "/../../../../outside/pwned"):
            with self.subTest(value=value):
                result = handoff.safe_filename_component(value)
                self.assertNotIn("/", result)
                self.assertNotIn("\\", result)
                self.assertNotIn(result, (".", ".."))

    def test_safe_filename_component_falls_back_when_nothing_survives(self):
        self.assertEqual(handoff.safe_filename_component("", "fallback"), "fallback")
        self.assertEqual(handoff.safe_filename_component(None, "fallback"), "fallback")
        self.assertEqual(handoff.safe_filename_component("..", "fallback"), "fallback")
        self.assertEqual(handoff.safe_filename_component("/", "fallback"), "fallback")
        self.assertEqual(handoff.safe_filename_component("///", "fallback"), "fallback")

    def test_timestamp_conversion_keeps_offset_and_survives_garbage(self):
        converted = handoff.ts_to_iso("2026-09-09T18:33:29.650Z")
        self.assertRegex(converted, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertEqual(handoff.ts_to_iso("not a date"), "not a date")
        self.assertIsNone(handoff.ts_to_iso(None))
        self.assertIsNotNone(handoff.ts_to_iso(1788633209))
        self.assertIsNotNone(handoff.ts_to_iso(1788633209000))

    def test_path_comparison_is_separator_and_case_tolerant(self):
        self.assertTrue(handoff.path_is_within("d:\\Repo\\src", "D:/repo"))
        self.assertFalse(handoff.path_is_within("d:\\Other", "D:/repo"))
        self.assertFalse(handoff.path_is_within("", "D:/repo"))

    def test_iter_jsonl_skips_bad_lines_without_aborting(self):
        target = self.tmp / "mixed.jsonl"
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"type": "a"}) + "\n")
            handle.write("{not json\n")
            handle.write("\n")
            handle.write("[1, 2, 3]\n")          # valid JSON, wrong shape
            handle.write(json.dumps({"type": "b"}) + "\n")
            handle.write('{"truncated": tru')     # incomplete final line
        results = list(handoff.iter_jsonl(target))
        good = [record for record, ok in results if ok]
        bad = [record for record, ok in results if not ok]
        self.assertEqual([r["type"] for r in good], ["a", "b"])
        self.assertEqual(len(bad), 3)

    def test_iter_jsonl_reports_unreadable_file_as_handoff_error(self):
        with self.assertRaises(handoff.HandoffError):
            list(handoff.iter_jsonl(self.tmp / "missing.jsonl"))


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


class TestRedaction(unittest.TestCase):
    def scrub(self, text):
        redactor = handoff.Redactor()
        return redactor.scrub(text), redactor.count

    def test_common_secret_shapes_are_replaced(self):
        samples = [
            "Authorization: Bearer abc123def456",
            "api_key=9f8e7d6c5b4a3f2e",
            'apikey: "0123456789abcdef"',
            "token=abcdef123456",
            "password=hunter2xyz",
            "secret: s3cr3t-value-here",
            "sk-abcdefghijklmnopqrstuvwxyz01",
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "xoxb-123456789012-abcdefghijkl",
            "AKIAIOSFODNN7EXAMPLE",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                scrubbed, count = self.scrub(sample)
                self.assertIn(handoff.REDACTED, scrubbed, sample)
                self.assertGreater(count, 0)

    def test_private_key_block_is_removed_entirely(self):
        text = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nsecretmaterial\n"
                "-----END RSA PRIVATE KEY-----")
        scrubbed, count = self.scrub(text)
        self.assertNotIn("secretmaterial", scrubbed)
        self.assertEqual(count, 1)

    def test_jwt_is_redacted(self):
        scrubbed, _ = self.scrub("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.abcdefghijk")
        self.assertIn(handoff.REDACTED, scrubbed)

    def test_ordinary_prose_and_code_survive_untouched(self):
        text = ("def build(): return 1 + 2  # nothing secret here\n"
                "The deploy takes about 3 minutes.")
        scrubbed, count = self.scrub(text)
        self.assertEqual(scrubbed, text)
        self.assertEqual(count, 0)

    def test_secret_key_name_is_preserved_so_the_context_stays_readable(self):
        scrubbed, _ = self.scrub("api_key=supersecretvalue")
        self.assertTrue(scrubbed.startswith("api_key="))
        self.assertNotIn("supersecretvalue", scrubbed)

    def test_credential_paths_are_recognised(self):
        for path in ("~/.claude/.credentials.json", "/home/u/.codex/auth.json",
                     "/srv/app/.env", "C:\\keys\\server.pem", "~/.ssh/id_rsa"):
            with self.subTest(path=path):
                self.assertTrue(handoff.Redactor.looks_like_credential_path(path))
        for path in ("src/main.py", "docs/handoff.md", None, ""):
            with self.subTest(path=path):
                self.assertFalse(handoff.Redactor.looks_like_credential_path(path))

    def test_counter_accumulates_across_calls(self):
        redactor = handoff.Redactor()
        redactor.scrub("token=abcdefgh")
        redactor.scrub("password=abcdefgh")
        self.assertEqual(redactor.count, 2)


class TestInjectedContext(unittest.TestCase):
    def test_machine_injected_blocks_are_detected(self):
        for text in (
            "<recommended_plugins>\n- Airtable\n</recommended_plugins>",
            '<codex_internal_context source="goal">\nContinue\n</codex_internal_context>',
            "<system-reminder>Do not do X</system-reminder>",
            "<recommended_plugins>a</recommended_plugins>\n<environment_context>b</environment_context>",
        ):
            with self.subTest(text=text[:40]):
                self.assertTrue(handoff.is_injected_context(text))

    def test_human_text_is_never_treated_as_injected(self):
        for text in (
            "faça com que aqui eu possa escolher a integração",
            "<div> should stay because I am asking about this markup and writing a real question",
            "",
            None,
        ):
            with self.subTest(text=str(text)[:40]):
                self.assertFalse(handoff.is_injected_context(text))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class TestConfig(TempDirCase):
    def test_defaults_are_used_when_no_file_exists(self):
        config = handoff.load_config(self.tmp / "absent.json")
        self.assertEqual(config["llm"]["model"], handoff.DEFAULT_CONFIG["llm"]["model"])
        self.assertFalse(handoff.llm_is_configured(config))

    def test_stored_values_merge_over_defaults_without_dropping_siblings(self):
        path = self.tmp / "config.json"
        path.write_text(json.dumps({"llm": {"model": "Custom-1"},
                                    "evidence": {"first_events": 3}}), encoding="utf-8")
        config = handoff.load_config(path)
        self.assertEqual(config["llm"]["model"], "Custom-1")
        self.assertEqual(config["llm"]["base_url"], handoff.DEFAULT_CONFIG["llm"]["base_url"])
        self.assertEqual(config["evidence"]["first_events"], 3)
        self.assertEqual(config["evidence"]["last_events"],
                         handoff.DEFAULT_CONFIG["evidence"]["last_events"])

    def test_broken_config_warns_and_falls_back_instead_of_crashing(self):
        path = self.tmp / "config.json"
        path.write_text("{ not json", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            config = handoff.load_config(path)
        self.assertIn("not valid JSON", captured.getvalue())
        self.assertEqual(config["llm"]["model"], handoff.DEFAULT_CONFIG["llm"]["model"])

    def test_environment_overrides_win_and_enable_the_llm(self):
        previous = dict(os.environ)
        try:
            os.environ["HANDOFF_LLM_BASE_URL"] = "http://127.0.0.1:9999"
            os.environ["HANDOFF_LLM_MODEL"] = "Env-Model"
            config = handoff.load_config(None)
            self.assertEqual(config["llm"]["base_url"], "http://127.0.0.1:9999")
            self.assertEqual(config["llm"]["model"], "Env-Model")
            self.assertTrue(config["llm"]["enabled"])
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def test_the_llm_is_off_until_an_endpoint_is_actually_configured(self):
        self.assertFalse(handoff.llm_is_configured(handoff.DEFAULT_CONFIG))
        self.assertEqual(handoff.DEFAULT_CONFIG["llm"]["base_url"], "")
        self.assertEqual(handoff.DEFAULT_CONFIG["llm"]["model"], "")

    def test_filling_base_url_and_model_is_what_turns_it_on(self):
        config = handoff.deep_merge(handoff.DEFAULT_CONFIG, {
            "llm": {"base_url": "http://127.0.0.1:8000", "model": "m"}})
        self.assertTrue(handoff.llm_is_configured(config))

    def test_a_half_configured_endpoint_stays_off(self):
        for partial in ({"base_url": "http://x"}, {"model": "m"}):
            with self.subTest(partial=partial):
                config = handoff.deep_merge(handoff.DEFAULT_CONFIG, {"llm": partial})
                self.assertFalse(handoff.llm_is_configured(config))
                self.assertIn("set", handoff.llm_hint(config))

    def test_enabled_false_is_an_explicit_kill_switch(self):
        config = handoff.deep_merge(handoff.DEFAULT_CONFIG, {"llm": {
            "enabled": False, "base_url": "http://127.0.0.1:8000", "model": "m"}})
        self.assertFalse(handoff.llm_is_configured(config))
        self.assertIn("enabled", handoff.llm_skip_reason(config))

    def test_skip_reason_explains_a_missing_endpoint(self):
        self.assertIn("no LLM integration configured",
                      handoff.llm_skip_reason(handoff.DEFAULT_CONFIG))

    def test_evidence_limit_clamps_bad_values(self):
        self.assertEqual(handoff.evidence_limit({"evidence": {"first_events": "x"}}, "first_events"),
                         handoff.DEFAULT_CONFIG["evidence"]["first_events"])
        self.assertEqual(handoff.evidence_limit({"evidence": {"first_events": -5}}, "first_events"), 0)


# --------------------------------------------------------------------------
# Claude adapter
# --------------------------------------------------------------------------


def claude_records(cwd="/repo"):
    """Minimal reproduction of the Claude Code 2.x record shapes."""
    return [
        {"type": "bridge-session", "sessionId": "sess-1"},
        {"type": "user", "sessionId": "sess-1", "cwd": cwd, "gitBranch": "main",
         "timestamp": "2026-09-09T12:00:00.000Z", "uuid": "u1",
         "message": {"role": "user", "content": [{"type": "text", "text": "build the parser"}]}},
        {"type": "attachment", "sessionId": "sess-1", "cwd": cwd,
         "attachment": {"type": "environment", "snapshot": {"workingDirectory": cwd}}},
        {"type": "assistant", "sessionId": "sess-1", "cwd": cwd,
         "timestamp": "2026-09-09T12:00:05.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "", "signature": "OPAQUE"},
             {"type": "text", "text": "Reading the file first."},
             {"type": "tool_use", "id": "t1", "name": "Read",
              "input": {"file_path": "/repo/src/app.py"}}]}},
        {"type": "user", "sessionId": "sess-1", "cwd": cwd,
         "timestamp": "2026-09-09T12:00:06.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "file body"}]},
         "toolUseResult": {"file": {"filePath": "/repo/src/app.py"}}},
        {"type": "assistant", "sessionId": "sess-1", "cwd": cwd,
         "timestamp": "2026-09-09T12:00:10.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "id": "t2", "name": "Edit",
              "input": {"file_path": "/repo/src/app.py", "old_string": "a", "new_string": "b"}}]}},
        {"type": "user", "sessionId": "sess-1", "cwd": cwd,
         "timestamp": "2026-09-09T12:00:12.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t2",
              "content": "Exit code 1\nTraceback: boom", "is_error": True}]}},
        {"type": "user", "sessionId": "sess-1", "cwd": cwd,
         "timestamp": "2026-09-09T12:00:13.000Z",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "<system-reminder>injected</system-reminder>"}]}},
        {"type": "user", "sessionId": "sess-1", "cwd": cwd, "isCompactSummary": True,
         "timestamp": "2026-09-09T12:00:20.000Z",
         "message": {"role": "user", "content": [
             {"type": "text", "text": "Summary of earlier work: parser started."}]}},
        {"type": "system", "subtype": "compact_boundary", "sessionId": "sess-1",
         "timestamp": "2026-09-09T12:00:21.000Z",
         "compactMetadata": {"trigger": "auto", "preTokens": 150000}},
        {"type": "user", "sessionId": "sess-1", "cwd": cwd,
         "timestamp": "2026-09-09T12:00:30.000Z",
         "message": {"role": "user", "content": "plain string content"}},
        {"type": "file-history-snapshot", "messageId": "u1", "snapshot": {}},
        {"type": "ai-title", "aiTitle": "ignored", "sessionId": "sess-1"},
    ]


class TestClaudeAdapter(TempDirCase):
    def setUp(self):
        super().setUp()
        self.session = self.tmp / "sess-1.jsonl"
        write_jsonl(self.session, claude_records())
        self.adapter = handoff.ClaudeAdapter(Path("/repo"), home=self.tmp)

    def events(self):
        return [event for event, ok in self.adapter.iter_events(self.session) if ok]

    def test_slug_matches_the_observed_naming_scheme(self):
        self.assertEqual(handoff.ClaudeAdapter.slug_for("/var/www/html/soutog/pgedigital"),
                         "-var-www-html-soutog-pgedigital")
        self.assertEqual(handoff.ClaudeAdapter.slug_for("d:\\Vida\\Profissional\\handoff"),
                         "d--Vida-Profissional-handoff")
        self.assertEqual(handoff.ClaudeAdapter.slug_for("C:\\Users\\gabriel"), "C--Users-gabriel")

    def test_bookkeeping_records_produce_no_events(self):
        raw_types = {event.raw_type for event in self.events()}
        for ignored in ("attachment", "file-history-snapshot", "ai-title", "bridge-session"):
            self.assertNotIn(ignored, raw_types)

    def test_message_roles_and_kinds_are_normalized(self):
        events = self.events()
        users = [e for e in events if e.kind == handoff.KIND_USER]
        assistants = [e for e in events if e.kind == handoff.KIND_ASSISTANT]
        self.assertEqual(users[0].text, "build the parser")
        self.assertEqual(assistants[0].text, "Reading the file first.")
        self.assertEqual(users[0].session_id, "sess-1")
        self.assertTrue(users[0].timestamp.startswith("2026-09-09T"))

    def test_string_content_is_accepted_as_well_as_block_lists(self):
        self.assertTrue(any(e.text == "plain string content"
                            for e in self.events() if e.kind == handoff.KIND_USER))

    def test_injected_system_reminder_is_not_counted_as_a_user_request(self):
        self.assertFalse(any("injected" in e.text
                             for e in self.events() if e.kind == handoff.KIND_USER))

    def test_thinking_blocks_and_signatures_never_reach_the_evidence(self):
        self.assertFalse(any("OPAQUE" in (e.text or "") for e in self.events()))

    def test_tool_calls_carry_name_paths_and_mutation_flag(self):
        calls = [e for e in self.events() if e.kind == handoff.KIND_TOOL_CALL]
        names = [e.tool_name for e in calls]
        self.assertEqual(names, ["Read", "Edit"])
        self.assertIn("/repo/src/app.py", calls[0].file_paths)
        self.assertFalse(calls[0].mutating)   # Read
        self.assertTrue(calls[1].mutating)    # Edit

    def test_failed_tool_results_become_tool_errors(self):
        errors = [e for e in self.events() if e.kind == handoff.KIND_TOOL_ERROR]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].is_error)
        self.assertIn("Traceback", errors[0].text)

    def test_both_compaction_shapes_are_captured(self):
        compactions = [e for e in self.events() if e.kind == handoff.KIND_COMPACTION]
        self.assertEqual(len(compactions), 2)
        self.assertTrue(any("parser started" in e.text for e in compactions))
        self.assertTrue(any("preTokens" in e.text for e in compactions))

    def test_probe_reads_session_id_and_cwd_from_the_head(self):
        session_id, cwd = self.adapter.probe(self.session)
        self.assertEqual(session_id, "sess-1")
        self.assertEqual(cwd, "/repo")

    def test_discovery_confirms_sessions_by_recorded_cwd(self):
        projects = self.tmp / ".claude" / "projects" / "-repo"
        projects.mkdir(parents=True)
        write_jsonl(projects / "sess-1.jsonl", claude_records("/repo"))
        write_jsonl(projects / "sess-2.jsonl", claude_records("/somewhere/else"))
        adapter = handoff.ClaudeAdapter(Path("/repo"), home=self.tmp)
        by_name = {info.path.name: info for info in adapter.discover()}
        self.assertEqual(by_name["sess-1.jsonl"].match, "confirmed")
        self.assertEqual(by_name["sess-2.jsonl"].match, "foreign")

    def test_selection_prefers_a_confirmed_session_over_a_foreign_one(self):
        projects = self.tmp / ".claude" / "projects" / "-repo"
        projects.mkdir(parents=True)
        write_jsonl(projects / "old-but-mine.jsonl", claude_records("/repo"))
        write_jsonl(projects / "new-but-foreign.jsonl", claude_records("/other"))
        os.utime(projects / "old-but-mine.jsonl", (1000, 1000))
        os.utime(projects / "new-but-foreign.jsonl", (9_000_000_000, 9_000_000_000))
        adapter = handoff.ClaudeAdapter(Path("/repo"), home=self.tmp)
        self.assertEqual(adapter.select().path.name, "old-but-mine.jsonl")

    def test_selection_refuses_to_guess_when_every_session_is_foreign(self):
        projects = self.tmp / ".claude" / "projects" / "-other"
        projects.mkdir(parents=True)
        write_jsonl(projects / "foreign.jsonl", claude_records("/other"))
        adapter = handoff.ClaudeAdapter(Path("/repo"), home=self.tmp)
        with self.assertRaises(handoff.HandoffError) as caught:
            adapter.select()
        self.assertEqual(caught.exception.code, handoff.EXIT_NO_SESSION)

    def test_explicit_session_file_bypasses_discovery(self):
        info = handoff.ClaudeAdapter(Path("/repo"), home=self.tmp).select(
            session_file=str(self.session))
        self.assertEqual(info.match, "explicit")
        self.assertEqual(info.session_id, "sess-1")

    def test_missing_session_file_is_a_clean_error(self):
        with self.assertRaises(handoff.HandoffError) as caught:
            self.adapter.select(session_file=str(self.tmp / "nope.jsonl"))
        self.assertEqual(caught.exception.code, handoff.EXIT_NO_SESSION)


# --------------------------------------------------------------------------
# Codex adapter
# --------------------------------------------------------------------------


def codex_records(cwd="/repo"):
    """Minimal reproduction of the Codex CLI rollout record shapes."""
    return [
        {"timestamp": "2026-09-09T18:33:29.650Z", "ordinal": 0, "type": "session_meta",
         "payload": {"session_id": "01a072d8-7b22-7af1-99ae-f4848b50ff58", "cwd": cwd,
                     "originator": "codex_vscode", "cli_version": "0.153.0"}},
        {"timestamp": "2026-09-09T18:33:30.220Z", "ordinal": 1, "type": "turn_context",
         "payload": {"cwd": cwd, "workspace_roots": [cwd], "model": "gpt-6-astra"}},
        {"timestamp": "2026-09-09T18:33:30.221Z", "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "role": "developer",
                     "content": [{"type": "input_text", "text": "system boilerplate"}]}},
        {"timestamp": "2026-09-09T18:33:30.222Z", "ordinal": 3, "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text",
              "text": "<recommended_plugins>\nAirtable\n</recommended_plugins>"}]}},
        {"timestamp": "2026-09-09T18:33:31.000Z", "ordinal": 4, "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "continue o trabalho"}]}},
        {"timestamp": "2026-09-09T18:33:31.500Z", "ordinal": 5, "type": "event_msg",
         "payload": {"type": "item_completed", "item": {
             "type": "UserMessage", "id": "item-1",
             "content": [{"type": "text", "text": "continue o trabalho"}]}}},
        {"timestamp": "2026-09-09T18:33:32.000Z", "ordinal": 6, "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "Vou conferir o repositório."}]}},
        {"timestamp": "2026-09-09T18:33:33.000Z", "ordinal": 7, "type": "response_item",
         "payload": {"type": "reasoning", "encrypted_content": "OPAQUE", "summary": []}},
        {"timestamp": "2026-09-09T18:33:37.301Z", "ordinal": 8, "type": "response_item",
         "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                     "input": "text(await tools.exec_command({cmd:\"git status\"}));"}},
        {"timestamp": "2026-09-09T18:33:39.538Z", "ordinal": 9, "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": [
             {"type": "input_text", "text": "Script completed"},
             {"type": "input_text", "text": "{\"exit_code\":0,\"output\":\"clean\"}"}]}},
        {"timestamp": "2026-09-09T18:33:41.000Z", "ordinal": 10, "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": [
             {"type": "input_text", "text": "{\"exit_code\":1,\"output\":\"boom\"}"}]}},
        {"timestamp": "2026-09-09T18:33:45.000Z", "ordinal": 11, "type": "event_msg",
         "payload": {"type": "item_completed", "item": {
             "type": "FileChange", "id": "fc1", "changes": {
                 "/repo/src/app.py": {"type": "update", "unified_diff": "@@ -1 +1 @@"},
                 "/repo/src/new.py": {"type": "add", "unified_diff": "@@ -0,0 +1 @@"}}}}},
        {"timestamp": "2026-09-09T18:33:46.000Z", "ordinal": 12, "type": "event_msg",
         "payload": {"type": "item_completed", "item": {
             "type": "AgentMessage", "id": "am1",
             "content": [{"type": "Text", "text": "Vou conferir o repositório."}]}}},
        {"timestamp": "2026-09-09T18:33:47.000Z", "ordinal": 13, "type": "event_msg",
         "payload": {"type": "token_count", "info": {}}},
        {"timestamp": "2026-09-09T18:34:00.000Z", "ordinal": 14, "type": "compacted",
         "payload": {"message": "", "replacement_history": [{"type": "message"}]}},
        {"timestamp": "2026-09-09T18:34:01.000Z", "ordinal": 15, "type": "event_msg",
         "payload": {"type": "item_completed",
                     "item": {"type": "ContextCompaction", "id": "item-17"}}},
    ]


class TestCodexAdapter(TempDirCase):
    def setUp(self):
        super().setUp()
        self.session = self.tmp / "rollout-2026-09-09T18-33-05-01a072d8-7b22-7af1-99ae-f4848b50ff58.jsonl"
        write_jsonl(self.session, codex_records())
        self.adapter = handoff.CodexAdapter(Path("/repo"), home=self.tmp)

    def events(self):
        return [event for event, ok in self.adapter.iter_events(self.session) if ok]

    def test_session_metadata_is_normalized(self):
        meta = [e for e in self.events() if e.kind == handoff.KIND_METADATA]
        self.assertEqual(len(meta), 1)
        self.assertIn("cwd: /repo", meta[0].text)

    def test_developer_boilerplate_is_not_conversation(self):
        self.assertFalse(any("system boilerplate" in (e.text or "") for e in self.events()))

    def test_injected_plugin_block_is_not_a_user_message(self):
        users = [e for e in self.events() if e.kind == handoff.KIND_USER]
        self.assertEqual([e.text for e in users], ["continue o trabalho"])

    def test_user_turn_is_not_duplicated_by_the_item_completed_record(self):
        users = [e for e in self.events() if e.kind == handoff.KIND_USER]
        self.assertEqual(len(users), 1)

    def test_assistant_turn_is_not_duplicated_by_agent_message(self):
        assistants = [e for e in self.events() if e.kind == handoff.KIND_ASSISTANT]
        self.assertEqual(len(assistants), 1)
        self.assertEqual(assistants[0].text, "Vou conferir o repositório.")

    def test_encrypted_reasoning_never_reaches_the_evidence(self):
        self.assertFalse(any("OPAQUE" in (e.text or "") for e in self.events()))

    def test_tool_call_and_output_are_normalized(self):
        calls = [e for e in self.events() if e.kind == handoff.KIND_TOOL_CALL]
        self.assertIn("exec", [e.tool_name for e in calls])
        results = [e for e in self.events() if e.kind == handoff.KIND_TOOL_RESULT]
        self.assertTrue(any("clean" in e.text for e in results))

    def test_nonzero_exit_code_is_classified_as_a_tool_error(self):
        errors = [e for e in self.events() if e.kind == handoff.KIND_TOOL_ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("boom", errors[0].text)

    def test_file_changes_are_captured_with_their_paths(self):
        changes = [e for e in self.events() if e.tool_name == "FileChange"]
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].mutating)
        self.assertEqual(sorted(changes[0].file_paths), ["/repo/src/app.py", "/repo/src/new.py"])

    def test_both_compaction_shapes_are_captured(self):
        compactions = [e for e in self.events() if e.kind == handoff.KIND_COMPACTION]
        self.assertEqual(len(compactions), 2)

    def test_session_id_falls_back_to_the_rollout_filename(self):
        self.assertEqual(
            handoff.CodexAdapter.session_id_from_path(self.session),
            "01a072d8-7b22-7af1-99ae-f4848b50ff58")

    def test_probe_reads_session_id_and_cwd(self):
        session_id, cwd = self.adapter.probe(self.session)
        self.assertEqual(session_id, "01a072d8-7b22-7af1-99ae-f4848b50ff58")
        self.assertEqual(cwd, "/repo")

    def test_discovery_uses_real_mtime_not_the_date_in_the_path(self):
        base = self.tmp / ".codex" / "sessions"
        old_dir = base / "2026" / "09" / "01"
        new_dir = base / "2026" / "09" / "02"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        older_path_newer_mtime = old_dir / "rollout-2026-09-01T10-00-00-aaaaaaaa.jsonl"
        newer_path_older_mtime = new_dir / "rollout-2026-09-02T10-00-00-bbbbbbbb.jsonl"
        write_jsonl(older_path_newer_mtime, codex_records("/repo"))
        write_jsonl(newer_path_older_mtime, codex_records("/repo"))
        os.utime(older_path_newer_mtime, (9_000_000_000, 9_000_000_000))
        os.utime(newer_path_older_mtime, (1000, 1000))
        adapter = handoff.CodexAdapter(Path("/repo"), home=self.tmp)
        self.assertEqual(adapter.discover()[0].path.name, older_path_newer_mtime.name)


def gemini_header(session_id="e6dfec33"):
    return {"sessionId": session_id, "projectHash": "a7da309f74d7b760",
            "startTime": "2026-09-10T01:06:13.954Z",
            "lastUpdated": "2026-09-10T01:06:13.954Z", "kind": "main"}


def gemini_records():
    """One of each record shape confirmed against @google/gemini-cli 0.59.0's
    own chatRecordingService.js (see GeminiAdapter's docstring); no $set.messages
    or $rewindTo here on purpose - those overwrite/erase and are exercised in
    their own small, isolated fixtures below instead."""
    return [
        gemini_header(),
        {"id": "ctx", "timestamp": "2026-09-10T01:06:13.956Z", "type": "user",
         "content": [{"text": "<session_context>\nThis is the Gemini CLI.\n</session_context>"}]},
        {"id": "u1", "timestamp": "2026-09-10T01:06:14.000Z", "type": "user",
         "content": [{"text": "implement the parser"}]},
        {"id": "g1", "timestamp": "2026-09-10T01:06:15.000Z", "type": "gemini", "model": "gemini-3-pro",
         "content": [{"text": "Sure, reading the file first."},
                     {"functionCall": {"id": "call1", "name": "read_file",
                                       "args": {"absolute_path": "/repo/src/app.py"}}}]},
        {"id": "u1_response", "timestamp": "2026-09-10T01:06:15.500Z", "type": "user",
         "content": [{"functionResponse": {"id": "call1", "name": "read_file",
                                           "response": {"output": "file body"}}}]},
        {"id": "g2", "timestamp": "2026-09-10T01:06:16.000Z", "type": "gemini", "content": "",
         "toolCalls": [{"id": "call2", "name": "run_shell_command",
                        "args": {"command": "python setup.py test"},
                        "result": "Exit code 1\nTraceback: boom"}]},
        {"id": "e1", "timestamp": "2026-09-10T01:06:17.000Z", "type": "error",
         "content": [{"text": "API key not valid. Please pass a valid API key."}]},
        {"id": "i1", "timestamp": "2026-09-10T01:06:17.500Z", "type": "info",
         "content": [{"text": "Binary content received."}]},
        {"id": "w1", "timestamp": "2026-09-10T01:06:17.600Z", "type": "warning",
         "content": [{"text": "rate limited, retrying"}]},
        {"id": "cs1", "timestamp": "2026-09-10T01:06:18.000Z", "type": "user",
         "content": [{"text": "<state_snapshot>\nProject status: parser started.\n</state_snapshot>"}]},
        {"id": "ack1", "timestamp": "2026-09-10T01:06:18.100Z", "type": "gemini",
         "content": [{"text": "Got it. Thanks for the additional context!"}]},
    ]


class TestGeminiAdapter(TempDirCase):
    def setUp(self):
        super().setUp()
        self.session = self.tmp / "session-2026-09-10T01-06-e6dfec33.jsonl"
        write_jsonl(self.session, gemini_records())
        self.adapter = handoff.GeminiAdapter(Path("/repo"), home=self.tmp)

    def events(self):
        return [event for event, ok in self.adapter.iter_events(self.session) if ok]

    def test_slugify_matches_the_project_registrys_own_algorithm(self):
        self.assertEqual(handoff.GeminiAdapter.slugify("gemini_probe"), "gemini-probe")
        self.assertEqual(handoff.GeminiAdapter.slugify("  My Project!! "), "my-project")
        self.assertEqual(handoff.GeminiAdapter.slugify("///"), "project")

    def test_session_id_from_path_reads_the_trailing_short_id(self):
        self.assertEqual(
            handoff.GeminiAdapter.session_id_from_path(self.session), "e6dfec33")

    def test_ignored_context_block_produces_no_user_event(self):
        users = [e for e in self.events() if e.kind == handoff.KIND_USER]
        self.assertEqual([e.text for e in users], ["implement the parser"])

    def test_assistant_text_and_inline_function_call_are_both_captured(self):
        assistants = [e for e in self.events() if e.kind == handoff.KIND_ASSISTANT]
        self.assertEqual(assistants[0].text, "Sure, reading the file first.")
        calls = [e for e in self.events() if e.kind == handoff.KIND_TOOL_CALL]
        read_call = next(c for c in calls if c.tool_name == "read_file")
        self.assertIn("/repo/src/app.py", read_call.file_paths)
        self.assertFalse(read_call.mutating)

    def test_function_response_in_a_user_message_is_a_tool_result_not_chat(self):
        # This is the trap: a "user"-type record carrying a functionResponse
        # part is a tool result, not something a human typed.
        results = [e for e in self.events() if e.kind == handoff.KIND_TOOL_RESULT]
        self.assertTrue(any(e.text == "file body" and e.tool_name == "read_file" for e in results))
        users = [e for e in self.events() if e.kind == handoff.KIND_USER]
        self.assertFalse(any("file body" in (e.text or "") for e in users))

    def test_toolcalls_array_shape_yields_a_call_and_a_failing_result(self):
        calls = [e for e in self.events() if e.kind == handoff.KIND_TOOL_CALL and e.tool_name == "run_shell_command"]
        self.assertEqual(len(calls), 1)
        # "python setup.py test" doesn't match any mutation keyword: not flagged.
        self.assertFalse(calls[0].mutating)
        errors = [e for e in self.events() if e.kind == handoff.KIND_TOOL_ERROR
                 and e.tool_name == "run_shell_command"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Traceback", errors[0].text)

    def test_shell_tool_mutation_is_judged_from_the_command_text(self):
        path = self.tmp / "shell.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"id": "g1", "type": "gemini", "content": "", "toolCalls": [
                {"id": "c1", "name": "run_shell_command",
                 "args": {"command": "echo hi >> out.txt"}, "result": "ok"}]},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        call = next(e for e in events if e.kind == handoff.KIND_TOOL_CALL)
        self.assertTrue(call.mutating)

    def test_error_type_message_becomes_a_tool_error(self):
        errors = [e for e in self.events() if e.kind == handoff.KIND_TOOL_ERROR and e.tool_name is None]
        self.assertTrue(any("API key not valid" in e.text for e in errors))

    def test_info_and_warning_messages_are_dropped(self):
        texts = [e.text for e in self.events()]
        self.assertFalse(any("Binary content received" in t for t in texts))
        self.assertFalse(any("rate limited" in t for t in texts))

    def test_state_snapshot_is_compaction_and_the_ack_reply_is_dropped(self):
        compactions = [e for e in self.events() if e.kind == handoff.KIND_COMPACTION]
        self.assertEqual(len(compactions), 1)
        self.assertIn("Project status", compactions[0].text)
        texts = [e.text for e in self.events()]
        self.assertFalse(any("Got it. Thanks for the additional context!" in t for t in texts))

    def test_set_messages_fully_replaces_the_message_list(self):
        path = self.tmp / "resync.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"id": "a", "type": "user", "content": [{"text": "first"}]},
            {"id": "b", "type": "user", "content": [{"text": "second"}]},
            {"$set": {"messages": [
                {"id": "b", "type": "user", "content": [{"text": "second (edited)"}]}]}},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        texts = [e.text for e in events]
        self.assertEqual(texts, ["second (edited)"])
        self.assertNotIn("first", texts)

    def test_rewind_removes_the_target_and_everything_recorded_after_it(self):
        path = self.tmp / "rewind.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"id": "a", "type": "user", "content": [{"text": "first"}]},
            {"id": "b", "type": "user", "content": [{"text": "second"}]},
            {"id": "c", "type": "user", "content": [{"text": "third"}]},
            {"$rewindTo": "b"},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        self.assertEqual([e.text for e in events], ["first"])

    def test_rewind_to_an_unknown_id_clears_everything(self):
        path = self.tmp / "rewind_unknown.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"id": "a", "type": "user", "content": [{"text": "first"}]},
            {"$rewindTo": "never-existed"},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        self.assertEqual(events, [])

    def test_invalid_lines_are_counted_even_though_events_are_replayed_at_the_end(self):
        path = self.tmp / "mixed.jsonl"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(gemini_header()) + "\n")
            handle.write("{broken\n")
            handle.write(json.dumps({"id": "a", "type": "user",
                                     "content": [{"text": "hello"}]}) + "\n")
        results = list(self.adapter.iter_events(path))
        good = [event for event, ok in results if ok]
        bad = [event for event, ok in results if not ok]
        self.assertEqual([e.text for e in good], ["hello"])
        self.assertEqual(len(bad), 1)

    def test_candidate_files_excludes_nested_subagent_sessions(self):
        base = self.tmp / ".gemini" / "tmp" / "myproj" / "chats"
        base.mkdir(parents=True)
        write_jsonl(base / "session-main.jsonl", gemini_records())
        nested = base / "parent-session-id"
        nested.mkdir()
        write_jsonl(nested / "sub-agent.jsonl", gemini_records())
        adapter = handoff.GeminiAdapter(Path("/repo"), home=self.tmp)
        found = adapter.candidate_files()
        self.assertEqual([p.name for p in found], ["session-main.jsonl"])

    def test_probe_reads_cwd_from_the_project_root_marker(self):
        slug_dir = self.tmp / ".gemini" / "tmp" / "myproj"
        chats = slug_dir / "chats"
        chats.mkdir(parents=True)
        (slug_dir / ".project_root").write_text("/repo", encoding="utf-8")
        session = chats / "session-x.jsonl"
        write_jsonl(session, gemini_records())
        adapter = handoff.GeminiAdapter(Path("/repo"), home=self.tmp)
        session_id, cwd = adapter.probe(session)
        self.assertEqual(cwd, "/repo")
        self.assertEqual(session_id, "e6dfec33")

    def test_discovery_confirms_via_the_project_root_marker(self):
        home_gemini = self.tmp / ".gemini" / "tmp"
        for slug, cwd in (("mine", "/repo"), ("theirs", "/somewhere/else")):
            chats = home_gemini / slug / "chats"
            chats.mkdir(parents=True)
            (home_gemini / slug / ".project_root").write_text(cwd, encoding="utf-8")
            write_jsonl(chats / ("session-%s.jsonl" % slug), gemini_records())
        adapter = handoff.GeminiAdapter(Path("/repo"), home=self.tmp)
        by_slug = {info.path.parent.parent.name: info for info in adapter.discover()}
        self.assertEqual(by_slug["mine"].match, "confirmed")
        self.assertEqual(by_slug["theirs"].match, "foreign")

    def test_path_hint_matches_the_repository_basename_when_no_marker_exists(self):
        adapter = handoff.GeminiAdapter(Path("/some/where/my-repo"), home=self.tmp)
        fake_path = self.tmp / ".gemini" / "tmp" / "my-repo" / "chats" / "session-x.jsonl"
        self.assertTrue(adapter.path_hint_matches(fake_path))
        other_path = self.tmp / ".gemini" / "tmp" / "unrelated" / "chats" / "session-x.jsonl"
        self.assertFalse(adapter.path_hint_matches(other_path))

    def test_memory_scratchpad_is_surfaced_as_a_labeled_compaction_event(self):
        path = self.tmp / "scratchpad.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"id": "u1", "type": "user", "content": [{"text": "let's start"}]},
            {"$set": {"memoryScratchpad": "Project uses FastAPI + Postgres."}},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        scratchpad_events = [e for e in events if e.tool_name == "memory-scratchpad"]
        self.assertEqual(len(scratchpad_events), 1)
        event = scratchpad_events[0]
        self.assertEqual(event.kind, handoff.KIND_COMPACTION)
        self.assertIn("fresh as of the most recent message", event.text)
        self.assertIn("Project uses FastAPI + Postgres.", event.text)

    def test_memory_scratchpad_is_marked_stale_after_a_later_message(self):
        path = self.tmp / "scratchpad_stale.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"$set": {"memoryScratchpad": "Saved early."}},
            {"id": "u1", "type": "user", "content": [{"text": "conversation kept going"}]},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        event = next(e for e in events if e.tool_name == "memory-scratchpad")
        self.assertIn("possibly stale", event.text)

    def test_memory_scratchpad_is_marked_stale_after_a_rewind(self):
        path = self.tmp / "scratchpad_rewind.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"$set": {"memoryScratchpad": "Saved early."}},
            {"$rewindTo": "does-not-exist"},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        event = next(e for e in events if e.tool_name == "memory-scratchpad")
        self.assertIn("possibly stale", event.text)

    def test_clearing_the_scratchpad_removes_the_event(self):
        path = self.tmp / "scratchpad_cleared.jsonl"
        write_jsonl(path, [
            gemini_header(),
            {"$set": {"memoryScratchpad": "Saved early."}},
            {"$set": {"memoryScratchpad": ""}},
        ])
        events = [e for e, ok in self.adapter.iter_events(path) if ok]
        self.assertFalse(any(e.tool_name == "memory-scratchpad" for e in events))

    def test_no_scratchpad_means_no_event(self):
        events = self.events()  # the shared fixture never sets memoryScratchpad
        self.assertFalse(any(e.tool_name == "memory-scratchpad" for e in events))


# --------------------------------------------------------------------------
# Evidence building
# --------------------------------------------------------------------------


class TestEvidenceBuilder(TempDirCase):
    def build(self, records, config=None, agent="claude"):
        path = self.tmp / "session.jsonl"
        write_jsonl(path, records)
        adapter = (handoff.ClaudeAdapter if agent == "claude" else handoff.CodexAdapter)(
            Path("/repo"), home=self.tmp)
        info = handoff.SessionInfo(agent=agent, session_id="s", path=path,
                                   mtime=0.0, size=path.stat().st_size)
        merged = handoff.deep_merge(handoff.DEFAULT_CONFIG, config or {})
        return handoff.EvidenceBuilder(merged).build(adapter, info)

    def chatter(self, count):
        records = []
        for index in range(count):
            records.append({
                "type": "user", "sessionId": "s", "cwd": "/repo",
                "timestamp": "2026-09-09T12:00:00.000Z",
                "message": {"role": "user", "content": [
                    {"type": "text", "text": "message %d" % index}]}})
        return records

    def test_first_and_last_buckets_respect_their_configured_limits(self):
        pack = self.build(self.chatter(100),
                          {"evidence": {"first_events": 5, "last_events": 7}})
        self.assertEqual(len(pack.first_events), 5)
        self.assertEqual(len(pack.last_events), 7)
        self.assertEqual(pack.first_events[0].text, "message 0")
        self.assertEqual(pack.last_events[-1].text, "message 99")

    def test_selected_count_is_distinct_and_never_exceeds_parsed(self):
        pack = self.build(self.chatter(50))
        self.assertLessEqual(pack.selected_count(), pack.total_events)

    def test_error_bucket_is_bounded(self):
        records = []
        for index in range(60):
            records.append({
                "type": "user", "sessionId": "s", "cwd": "/repo",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t%d" % index,
                     "content": "failure %d" % index, "is_error": True}]}})
        pack = self.build(records, {"evidence": {"max_errors": 4}})
        self.assertEqual(len(pack.errors), 4)
        self.assertIn("failure 59", pack.errors[-1].text)

    def test_huge_tool_output_is_truncated_with_an_explicit_marker(self):
        records = [{"type": "user", "sessionId": "s", "cwd": "/repo",
                    "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 200000}]}}]
        pack = self.build(records, {"evidence": {"max_tool_text_chars": 500}})
        text = pack.last_events[-1].text
        self.assertLess(len(text), 1000)
        self.assertIn("truncated: original length 200000 chars", text)

    def test_secrets_are_redacted_inside_the_evidence_pack(self):
        records = [{"type": "user", "sessionId": "s", "cwd": "/repo",
                    "message": {"role": "user", "content": [
                        {"type": "text", "text": "use api_key=abcdef1234567890"}]}}]
        pack = self.build(records)
        self.assertNotIn("abcdef1234567890", pack.last_events[-1].text)
        self.assertGreaterEqual(pack.redactions, 1)

    def test_credential_file_reads_are_dropped_wholesale(self):
        records = [{"type": "assistant", "sessionId": "s", "cwd": "/repo",
                    "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "t1", "name": "Read",
                         "input": {"file_path": "/home/u/.codex/auth.json"}}]}}]
        pack = self.build(records)
        self.assertIn("REDACTED", pack.last_events[-1].text)

    def test_invalid_lines_are_counted_and_do_not_abort_the_pass(self):
        path = self.tmp / "session.jsonl"
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(self.chatter(1)[0]) + "\n")
            handle.write("{broken\n")
            handle.write(json.dumps(self.chatter(1)[0]) + "\n")
            handle.write('{"half": ')
        adapter = handoff.ClaudeAdapter(Path("/repo"), home=self.tmp)
        info = handoff.SessionInfo(agent="claude", session_id="s", path=path,
                                   mtime=0.0, size=path.stat().st_size)
        pack = handoff.EvidenceBuilder(handoff.DEFAULT_CONFIG).build(adapter, info)
        self.assertEqual(pack.invalid_lines, 2)
        self.assertEqual(pack.total_events, 2)

    def test_mutating_tool_calls_and_touched_files_are_tracked(self):
        pack = self.build(claude_records())
        self.assertTrue(any(e.tool_name == "Edit" for e in pack.mutating_tools))
        self.assertIn("/repo/src/app.py", pack.touched_files)

    def test_empty_transcript_yields_an_empty_but_valid_pack(self):
        pack = self.build([])
        self.assertEqual(pack.total_events, 0)
        self.assertEqual(pack.selected_count(), 0)


# --------------------------------------------------------------------------
# Git parsing and rendering
# --------------------------------------------------------------------------


class TestGitParsing(unittest.TestCase):
    def test_porcelain_status_is_classified_by_category(self):
        state = handoff.GitState()
        state.status_porcelain = (
            " M src/modified.py\n"
            "A  src/added.py\n"
            " D src/deleted.py\n"
            "R  old.py -> new.py\n"
            "?? untracked.txt\n"
            "UU conflicted.py\n"
            "MM both.py\n")
        handoff.GitInspector._classify(state)
        self.assertEqual(state.modified, ["src/modified.py", "both.py"])
        self.assertEqual(state.added, ["src/added.py"])
        self.assertEqual(state.deleted, ["src/deleted.py"])
        self.assertEqual(state.renamed, ["new.py"])
        self.assertEqual(state.untracked, ["untracked.txt"])
        self.assertEqual(state.conflicted, ["conflicted.py"])

    def test_changed_paths_are_deduplicated_and_ordered(self):
        state = handoff.GitState(modified=["a.py"], added=["b.py"], untracked=["a.py"])
        self.assertEqual(state.changed_paths(), ["a.py", "b.py"])

    def test_dirty_flag_follows_the_porcelain_output(self):
        self.assertFalse(handoff.GitState().is_dirty)
        self.assertTrue(handoff.GitState(status_porcelain=" M a\n").is_dirty)

    def test_git_state_file_contains_every_expected_section(self):
        state = handoff.GitState(root="/repo", branch="main", head="abc123",
                                 status_porcelain=" M a.py\n")
        rendered = handoff.render_git_state(state, "2026-09-09T17:00:00-03:00")
        for section in ("STATUS", "DIFF STAT", "CHANGED FILES", "STAGED",
                        "RECENT COMMITS", "Repository: /repo", "Branch: main"):
            self.assertIn(section, rendered)
        self.assertIn("(none)", rendered)  # empty sections are explicit


@unittest.skipUnless(HAS_GIT, "git is not installed")
class TestGitExclude(TempDirCase):
    def make_repo(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return repo

    def test_rules_are_added_once_and_never_duplicated(self):
        repo = self.make_repo()
        self.assertEqual(handoff.ensure_git_exclude(repo), list(handoff.EXCLUDE_RULES))
        self.assertEqual(handoff.ensure_git_exclude(repo), [])
        content = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertEqual(content.count("HANDOFF.md"), 1)
        self.assertEqual(content.count(".handoff/"), 1)

    def test_preexisting_exclude_rules_are_preserved(self):
        repo = self.make_repo()
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("# mine\nnode_modules/\n*.log\n", encoding="utf-8")
        handoff.ensure_git_exclude(repo)
        content = exclude.read_text(encoding="utf-8")
        self.assertIn("node_modules/", content)
        self.assertIn("*.log", content)
        self.assertIn("HANDOFF.md", content)

    def test_rules_are_anchored_to_the_repository_root(self):
        for rule in handoff.EXCLUDE_RULES:
            self.assertTrue(rule.startswith("/"), rule)

    def test_docs_handoff_md_is_not_swallowed_by_the_rules(self):
        # An unanchored "HANDOFF.md" matches at any depth, and on a
        # case-insensitive filesystem it also excludes docs/handoff.md.
        repo = self.make_repo()
        handoff.ensure_git_exclude(repo)
        (repo / "docs").mkdir()
        (repo / "docs" / "handoff.md").write_text("doc", encoding="utf-8")
        (repo / "HANDOFF.md").write_text("handoff", encoding="utf-8")
        untracked = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(repo), stdout=subprocess.PIPE, encoding="utf-8").stdout
        self.assertIn("docs/handoff.md", untracked)
        self.assertNotIn("HANDOFF.md\n", untracked.replace("docs/handoff.md", ""))

    def test_legacy_unanchored_rules_are_upgraded_in_place(self):
        repo = self.make_repo()
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("# mine\nnode_modules/\nHANDOFF.md\n.handoff/\n*.log\n",
                           encoding="utf-8")
        changed = handoff.ensure_git_exclude(repo)
        content = exclude.read_text(encoding="utf-8")
        self.assertEqual(len(changed), 2)
        self.assertIn("/HANDOFF.md", content)
        self.assertIn("/.handoff/", content)
        self.assertNotIn("\nHANDOFF.md\n", content)
        self.assertIn("node_modules/", content)
        self.assertIn("*.log", content)
        self.assertEqual(handoff.ensure_git_exclude(repo), [])

    def test_gitignore_is_never_touched(self):
        repo = self.make_repo()
        gitignore = repo / ".gitignore"
        gitignore.write_text("vendor/\n", encoding="utf-8")
        handoff.ensure_git_exclude(repo)
        self.assertEqual(gitignore.read_text(encoding="utf-8"), "vendor/\n")

    def test_finding_the_root_outside_a_repository_is_a_clean_git_error(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.GitInspector(plain).find_root()
        self.assertEqual(caught.exception.code, handoff.EXIT_GIT)


class TestAgentsMd(TempDirCase):
    def test_missing_agents_md_is_never_created(self):
        self.assertEqual(handoff.update_agents_md(self.tmp), "absent")
        self.assertFalse((self.tmp / "AGENTS.md").exists())

    def test_section_is_appended_while_preserving_the_original_content(self):
        path = self.tmp / "AGENTS.md"
        path.write_text("# Project rules\n\nAlways run the tests.\n", encoding="utf-8")
        self.assertEqual(handoff.update_agents_md(self.tmp), "section appended")
        content = path.read_text(encoding="utf-8")
        self.assertIn("Always run the tests.", content)
        self.assertIn("## Agent handoff", content)

    def test_existing_section_is_not_duplicated(self):
        path = self.tmp / "AGENTS.md"
        path.write_text("# Rules\n\n## Agent handoff\n\nCustom text.\n", encoding="utf-8")
        self.assertEqual(handoff.update_agents_md(self.tmp), "already present")
        self.assertEqual(path.read_text(encoding="utf-8").count("Agent handoff"), 1)


# --------------------------------------------------------------------------
# Agent memory - indexed, never ingested
# --------------------------------------------------------------------------


class TestAgentMemory(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.home = self.tmp / "home"
        self.repo.mkdir()
        self.home.mkdir()

    def git_state(self, untracked=()):
        return handoff.GitState(root=str(self.repo), untracked=list(untracked))

    def test_nothing_found_returns_an_empty_list(self):
        entries = handoff.describe_agent_memory(self.repo, self.git_state(), home=self.home)
        self.assertEqual(entries, [])

    def test_claude_memory_directory_is_found_via_the_slug_heuristic(self):
        slug = handoff.ClaudeAdapter.slug_for(str(self.repo))
        memory_dir = self.home / ".claude" / "projects" / slug / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "fact-one.md").write_text("x", encoding="utf-8")
        (memory_dir / "MEMORY.md").write_text("- fact one", encoding="utf-8")

        entries = handoff.describe_agent_memory(self.repo, self.git_state(), home=self.home)
        claude_entries = [e for e in entries if e.agent == handoff.AGENT_CLAUDE
                          and e.label == "Memory directory"]
        self.assertEqual(len(claude_entries), 1)
        self.assertIn("2 file(s)", claude_entries[0].detail)

    def test_claude_memory_directory_reports_zero_files_when_empty(self):
        slug = handoff.ClaudeAdapter.slug_for(str(self.repo))
        (self.home / ".claude" / "projects" / slug / "memory").mkdir(parents=True)
        entries = handoff.describe_agent_memory(self.repo, self.git_state(), home=self.home)
        claude_entries = [e for e in entries if e.label == "Memory directory"]
        self.assertEqual(claude_entries[0].detail, "0 file(s)")

    def test_claude_md_reports_untracked_status(self):
        (self.repo / "CLAUDE.md").write_text("# instructions", encoding="utf-8")

        tracked_entries = handoff.describe_agent_memory(self.repo, self.git_state(), home=self.home)
        tracked = next(e for e in tracked_entries if "CLAUDE.md" in e.label)
        self.assertNotIn("untracked", tracked.detail)

        untracked_entries = handoff.describe_agent_memory(
            self.repo, self.git_state(untracked=["CLAUDE.md"]), home=self.home)
        untracked = next(e for e in untracked_entries if "CLAUDE.md" in e.label)
        self.assertIn("untracked", untracked.detail)

    def test_gemini_md_is_indexed_the_same_way(self):
        (self.repo / "GEMINI.md").write_text("# instructions", encoding="utf-8")
        entries = handoff.describe_agent_memory(self.repo, self.git_state(), home=self.home)
        entry = next(e for e in entries if e.agent == handoff.AGENT_GEMINI)
        self.assertEqual(entry.label, "Project instructions (GEMINI.md)")

    def test_codex_sqlite_files_are_indexed_by_presence_only_never_opened(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True)
        # Deliberately not valid SQLite - proves the file is never opened/parsed.
        (codex_dir / "memories_1.sqlite").write_bytes(b"not a real sqlite file")
        (codex_dir / "goals_1.sqlite").write_bytes(b"also not sqlite")

        entries = handoff.describe_agent_memory(self.repo, self.git_state(), home=self.home)
        codex_entries = {e.label: e for e in entries if e.agent == handoff.AGENT_CODEX}
        self.assertEqual(set(codex_entries), {"memories_1.sqlite", "goals_1.sqlite"})
        for entry in codex_entries.values():
            self.assertIn("not parsed", entry.detail)
            self.assertIn("global", entry.detail)

    def test_render_agent_memory_section_reports_none_found(self):
        rendered = handoff.render_agent_memory_section([])
        self.assertIn("## Agent Memory", rendered)
        self.assertIn("No agent memory found", rendered)

    def test_render_agent_memory_section_lists_every_entry(self):
        entries = [
            handoff.MemoryEntry(handoff.AGENT_CLAUDE, "Memory directory", "/x/memory", "3 file(s)"),
            handoff.MemoryEntry(handoff.AGENT_CODEX, "memories_1.sqlite", "/y/memories_1.sqlite",
                                "40.0 KiB, global (not project-specific), not parsed"),
        ]
        rendered = handoff.render_agent_memory_section(entries)
        self.assertIn("Claude", rendered)
        self.assertIn("/x/memory", rendered)
        self.assertIn("not parsed", rendered)
        self.assertIn("never treated as evidence", rendered)

    def test_memory_section_is_embedded_in_the_full_handoff(self):
        info = handoff.SessionInfo(agent="claude", session_id="s", path=self.tmp / "s.jsonl",
                                   mtime=0.0, size=0)
        pack = handoff.EvidencePack(session=info)
        git = handoff.GitState(root=str(self.repo), branch="main", head="a" * 40)
        entries = [handoff.MemoryEntry(handoff.AGENT_CODEX, "memories_1.sqlite",
                                       "/home/.codex/memories_1.sqlite", "40.0 KiB, not parsed")]
        rendered = handoff.render_deterministic_handoff(
            pack, git, "2026-09-09T17:00:00-03:00", memory_entries=entries)
        self.assertIn("## Agent Memory", rendered)
        self.assertIn("memories_1.sqlite", rendered)
        # Comes after Evidence, before Resume Instructions, in both modes.
        self.assertLess(rendered.index("## Evidence"), rendered.index("## Agent Memory"))
        self.assertLess(rendered.index("## Agent Memory"), rendered.index("## Resume Instructions"))

    def test_memory_section_defaults_to_empty_when_not_passed(self):
        info = handoff.SessionInfo(agent="claude", session_id="s", path=self.tmp / "s.jsonl",
                                   mtime=0.0, size=0)
        pack = handoff.EvidencePack(session=info)
        git = handoff.GitState(root=str(self.repo), branch="main", head="a" * 40)
        rendered = handoff.render_deterministic_handoff(pack, git, "2026-09-09T17:00:00-03:00")
        self.assertIn("No agent memory found", rendered)


# --------------------------------------------------------------------------
# Handoff rendering
# --------------------------------------------------------------------------


REQUIRED_SECTIONS = (
    "## Goal", "## Current State", "## Confirmed Completed Work", "## Relevant Files",
    "## Technical Decisions", "## Failed / Rejected Approaches", "## Known Problems",
    "## Pending Work", "## Suggested Next Steps", "## Unresolved Questions",
    "## Git State", "## Evidence", "## Agent Memory", "## Resume Instructions",
)


class TestHandoffRendering(TempDirCase):
    def make_pack(self):
        info = handoff.SessionInfo(agent="claude", session_id="sess-1",
                                   path=self.tmp / "s.jsonl", mtime=0.0, size=123)
        pack = handoff.EvidencePack(session=info)
        pack.total_events = 10
        pack.first_user_messages = [handoff.NormalizedEvent(
            kind=handoff.KIND_USER, text="implement the parser")]
        pack.last_user_messages = [handoff.NormalizedEvent(
            kind=handoff.KIND_USER, text="now add tests")]
        pack.errors = [handoff.NormalizedEvent(
            kind=handoff.KIND_TOOL_ERROR, text="Exit code 1: boom", is_error=True)]
        return pack

    def git_state(self):
        state = handoff.GitState(root="/repo", branch="main", head="a" * 40,
                                 status_porcelain=" M src/app.py\n")
        handoff.GitInspector._classify(state)
        return state

    def test_deterministic_handoff_has_the_full_stable_skeleton(self):
        rendered = handoff.render_deterministic_handoff(
            self.make_pack(), self.git_state(), "2026-09-09T17:00:00-03:00")
        for section in REQUIRED_SECTIONS:
            self.assertIn(section, rendered)
        self.assertIn("deterministic (no AI)", rendered)

    def test_deterministic_mode_refuses_to_invent_decisions(self):
        rendered = handoff.render_deterministic_handoff(
            self.make_pack(), self.git_state(), "2026-09-09T17:00:00-03:00")
        decisions = rendered.split("## Technical Decisions")[1].split("##")[0]
        self.assertIn("Not automatically determined", decisions)

    def test_first_user_message_is_quoted_rather_than_summarized(self):
        rendered = handoff.render_deterministic_handoff(
            self.make_pack(), self.git_state(), "2026-09-09T17:00:00-03:00")
        self.assertIn("> implement the parser", rendered)
        self.assertIn("> now add tests", rendered)

    def test_ai_body_replaces_the_middle_but_keeps_the_deterministic_footer(self):
        rendered = handoff.render_deterministic_handoff(
            self.make_pack(), self.git_state(), "2026-09-09T17:00:00-03:00",
            ai_body="## Goal\n\nModel written goal.\n")
        self.assertIn("Model written goal.", rendered)
        self.assertIn("AI-consolidated", rendered)
        for section in ("## Git State", "## Evidence", "## Agent Memory", "## Resume Instructions"):
            self.assertIn(section, rendered)

    def test_full_diff_is_never_embedded(self):
        state = self.git_state()
        state.diff_stat = " src/app.py | 2 +-\n"
        rendered = handoff.render_deterministic_handoff(
            self.make_pack(), state, "2026-09-09T17:00:00-03:00")
        self.assertIn("Run `git diff` in this checkout", rendered)

    def test_relevant_files_ignores_paths_outside_the_repository(self):
        pack = self.make_pack()
        pack.touched_files.update({"/repo/src/app.py": 1, "/tmp/scratch/notes.txt": 1})
        rendered = handoff.render_deterministic_handoff(
            pack, self.git_state(), "2026-09-09T17:00:00-03:00")
        self.assertIn("src/app.py", rendered)
        self.assertNotIn("scratch/notes.txt", rendered)

    def test_conversation_tail_labels_its_sections_and_counts(self):
        pack = self.make_pack()
        pack.invalid_lines = 3
        rendered = handoff.render_conversation_tail(pack, "2026-09-09T17:00:00-03:00")
        self.assertIn("# Conversation evidence", rendered)
        self.assertIn("Invalid JSONL lines ignored: 3", rendered)
        self.assertIn("## Errors and failed tool activity", rendered)
        self.assertIn("evidence, not conclusions", rendered)

    def test_session_metadata_is_valid_json_with_the_expected_keys(self):
        rendered = handoff.render_session_meta(
            self.make_pack(), self.git_state(), "2026-09-09T17:00:00-03:00", "deadbeef")
        data = json.loads(rendered)
        for key in ("generated_at", "agent", "session_id", "source_path", "source_size",
                    "source_sha256", "repository", "branch", "head", "events_parsed"):
            self.assertIn(key, data)
        self.assertEqual(data["source_sha256"], "deadbeef")


class TestHistoryArchiving(TempDirCase):
    def workspace(self):
        workspace = handoff.Workspace(self.tmp)
        workspace.history_dir.mkdir(parents=True)
        return workspace

    def test_archive_name_encodes_timestamp_agent_and_session(self):
        workspace = self.workspace()
        info = handoff.SessionInfo(agent="claude", session_id="4114df3c-a38c-49c9",
                                   path=self.tmp / "s.jsonl", mtime=0.0, size=0)
        target = handoff.archive_handoff(workspace, "2026-09-09T17:45:30-03:00", info, "body")
        self.assertEqual(target.name, "2026-09-09_174530_claude_4114df3c.md")
        self.assertTrue(handoff.HISTORY_NAME_RE.match(target.name))

    def test_a_crafted_session_id_cannot_escape_the_history_directory(self):
        # session_id is read straight out of the transcript's JSON; a
        # --session-file an attacker handed the user could set it to anything,
        # including path traversal sequences. The archive must stay inside
        # history_dir regardless.
        workspace = self.workspace()
        for malicious_id in ("../../../../outside/pwned", "/../../etc/passwd",
                             "..", ".", "../../..", "a/../../b"):
            with self.subTest(session_id=malicious_id):
                info = handoff.SessionInfo(agent="claude", session_id=malicious_id,
                                           path=self.tmp / "s.jsonl", mtime=0.0, size=0)
                target = handoff.archive_handoff(
                    workspace, "2026-09-09T17:45:30-03:00", info, "content")
                self.assertEqual(target.resolve().parent, workspace.history_dir.resolve())
                self.assertTrue(target.is_file())

    def test_repeated_archives_never_overwrite_each_other(self):
        workspace = self.workspace()
        info = handoff.SessionInfo(agent="codex", session_id="01a087b6",
                                   path=self.tmp / "s.jsonl", mtime=0.0, size=0)
        first = handoff.archive_handoff(workspace, "2026-09-09T17:45:30-03:00", info, "one")
        second = handoff.archive_handoff(workspace, "2026-09-09T17:45:30-03:00", info, "two")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding="utf-8"), "one")

    def test_previous_handoff_is_preserved_only_when_history_lacks_it(self):
        workspace = self.workspace()
        handoff.atomic_write(workspace.handoff_path, "hand written notes")
        self.assertIsNotNone(handoff.preserve_previous_handoff(workspace))
        self.assertEqual(len(list(workspace.history_dir.glob("*_replaced.md"))), 1)

    def test_a_handoff_already_in_history_is_not_archived_twice(self):
        workspace = self.workspace()
        handoff.atomic_write(workspace.history_dir / "2026-09-09_120000_claude_abc.md", "same body")
        handoff.atomic_write(workspace.handoff_path, "same body")
        self.assertIsNone(handoff.preserve_previous_handoff(workspace))


# --------------------------------------------------------------------------
# LLM client
# --------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    reply = {"choices": [{"message": {"role": "assistant", "content": "## Goal\n\nok\n"}}]}
    status = 200
    captured = {}

    def _send(self, payload, status=200):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._send({"data": [{"id": "DeepSeek-V4-Flash-0731"}, {"id": "other"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        type(self).captured = {"body": json.loads(body),
                               "auth": self.headers.get("Authorization")}
        if type(self).status != 200:
            self._send({"error": "boom"}, type(self).status)
        else:
            self._send(type(self).reply)

    def log_message(self, *args):
        pass


class TestLLMClient(unittest.TestCase):
    def setUp(self):
        _StubHandler.status = 200
        _StubHandler.reply = {"choices": [{"message": {"content": "## Goal\n\nok\n"}}]}
        _StubHandler.captured = {}
        self.server = HTTPServer(("127.0.0.1", 0), _StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def client(self, **overrides):
        config = handoff.deep_merge(handoff.DEFAULT_CONFIG, {"llm": dict(
            {"base_url": self.base_url, "model": "DeepSeek-V4-Flash-0731",
             "timeout_seconds": 10}, **overrides)})
        return handoff.LLMClient(config)

    def test_models_endpoint_is_parsed(self):
        self.assertIn("DeepSeek-V4-Flash-0731", self.client().list_models())

    def test_chat_completion_sends_the_openai_shape_and_returns_the_content(self):
        result = self.client().complete("system text", "user text")
        self.assertEqual(result, "## Goal\n\nok")
        sent = _StubHandler.captured["body"]
        self.assertEqual(sent["model"], "DeepSeek-V4-Flash-0731")
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user"])
        self.assertEqual(sent["messages"][1]["content"], "user text")
        self.assertIn("temperature", sent)

    def test_no_authorization_header_is_sent_when_no_key_is_configured(self):
        previous = os.environ.pop("HANDOFF_LLM_API_KEY", None)
        try:
            self.client().complete("s", "u")
            self.assertIsNone(_StubHandler.captured["auth"])
        finally:
            if previous is not None:
                os.environ["HANDOFF_LLM_API_KEY"] = previous

    def test_api_key_is_read_from_the_environment_only(self):
        os.environ["HANDOFF_LLM_API_KEY"] = "secret-value"
        try:
            self.client().complete("s", "u")
            self.assertEqual(_StubHandler.captured["auth"], "Bearer secret-value")
        finally:
            os.environ.pop("HANDOFF_LLM_API_KEY", None)

    def test_http_error_becomes_a_handoff_error_with_the_llm_exit_code(self):
        _StubHandler.status = 500
        with self.assertRaises(handoff.HandoffError) as caught:
            self.client().complete("s", "u")
        self.assertEqual(caught.exception.code, handoff.EXIT_LLM)

    def test_unreachable_endpoint_becomes_a_handoff_error(self):
        config = handoff.deep_merge(handoff.DEFAULT_CONFIG, {"llm": {
            "base_url": "http://127.0.0.1:1", "model": "m", "timeout_seconds": 2}})
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.LLMClient(config).complete("s", "u")
        self.assertEqual(caught.exception.code, handoff.EXIT_LLM)

    def test_empty_content_is_rejected_rather_than_written_as_a_handoff(self):
        _StubHandler.reply = {"choices": [{"message": {"content": "   "}}]}
        with self.assertRaises(handoff.HandoffError):
            self.client().complete("s", "u")

    def test_response_without_choices_is_rejected(self):
        _StubHandler.reply = {"choices": []}
        with self.assertRaises(handoff.HandoffError):
            self.client().complete("s", "u")


class TestAiInput(TempDirCase):
    def make_inputs(self):
        info = handoff.SessionInfo(agent="claude", session_id="s", path=self.tmp / "s.jsonl",
                                   mtime=0.0, size=30 * 1024 * 1024)
        pack = handoff.EvidencePack(session=info)
        pack.total_events = 5000
        state = handoff.GitState(root="/repo", branch="main", head="a" * 40)
        return pack, state

    def test_payload_carries_git_first_and_marks_the_transcript_as_non_proof(self):
        pack, state = self.make_inputs()
        text = handoff.build_ai_input(pack, state, "conversation body", "GIT STATE BODY",
                                      handoff.Redactor(), 120000)
        self.assertLess(text.index("Current Git state (authoritative)"),
                        text.index("Conversation evidence"))
        self.assertIn("NOT proof of implementation", text)

    def test_payload_is_redacted_before_it_can_be_sent(self):
        pack, state = self.make_inputs()
        redactor = handoff.Redactor()
        text = handoff.build_ai_input(pack, state, "token=abcdef1234567890", "git",
                                      redactor, 120000)
        self.assertNotIn("abcdef1234567890", text)
        self.assertGreaterEqual(redactor.count, 1)

    def test_oversized_evidence_is_trimmed_to_the_budget_with_a_visible_marker(self):
        pack, state = self.make_inputs()
        text = handoff.build_ai_input(pack, state, "z" * 300000, "git",
                                      handoff.Redactor(), 20000)
        self.assertLessEqual(len(text), 20000)
        self.assertIn("evidence pack truncated", text)

    def test_system_prompt_states_the_hierarchy_of_truth(self):
        self.assertIn("FONTES DE VERDADE", handoff.SYSTEM_PROMPT)
        self.assertIn("não confirmado", handoff.SYSTEM_PROMPT)
        for section in ("## Goal", "## Pending Work", "## Unresolved Questions"):
            self.assertIn(section, handoff.SYSTEM_PROMPT)


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


class TestCli(unittest.TestCase):
    def test_every_documented_command_is_registered(self):
        parser = handoff.build_parser()
        actions = [a for a in parser._actions if isinstance(a, type(parser._subparsers._group_actions[0]))]
        commands = set(actions[0].choices)
        self.assertEqual(commands, {"init", "doctor", "status", "snapshot", "recover",
                                    "consolidate", "history", "show"})

    def test_snapshot_and_recover_accept_the_session_and_ai_flags(self):
        parser = handoff.build_parser()
        for command in ("snapshot", "recover"):
            args = parser.parse_args([command, "claude", "--session", "abc",
                                      "--ai", "--dry-run"])
            self.assertEqual(args.agent, "claude")
            self.assertEqual(args.session, "abc")
            self.assertTrue(args.ai)
            self.assertTrue(args.dry_run)

    def test_unknown_agent_is_rejected_by_the_parser(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                handoff.build_parser().parse_args(["snapshot", "grok"])

    def test_running_without_a_command_returns_the_usage_exit_code(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = handoff.main([])
        self.assertEqual(code, handoff.EXIT_USAGE)
        self.assertIn("usage:", buffer.getvalue())

    def test_get_adapter_rejects_unknown_agents_with_a_usage_error(self):
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.get_adapter("grok", Path("/repo"))
        self.assertEqual(caught.exception.code, handoff.EXIT_USAGE)

    def test_get_adapter_resolves_all_three_known_agents(self):
        self.assertIsInstance(handoff.get_adapter("claude", Path("/repo")), handoff.ClaudeAdapter)
        self.assertIsInstance(handoff.get_adapter("codex", Path("/repo")), handoff.CodexAdapter)
        self.assertIsInstance(handoff.get_adapter("gemini", Path("/repo")), handoff.GeminiAdapter)

    def test_repo_flag_is_accepted_before_and_after_the_subcommand(self):
        parser = handoff.build_parser()
        self.assertEqual(parser.parse_args(["--repo", "/x", "doctor"]).repo, "/x")
        self.assertEqual(parser.parse_args(["doctor", "--repo", "/x"]).repo, "/x")
        self.assertEqual(parser.parse_args(["recover", "claude", "--repo", "/x"]).repo, "/x")

    def test_verbose_is_accepted_in_both_positions(self):
        parser = handoff.build_parser()
        self.assertTrue(parser.parse_args(["--verbose", "status"]).verbose)
        self.assertTrue(parser.parse_args(["status", "--verbose"]).verbose)

    def test_without_repo_the_current_directory_is_used(self):
        args = handoff.build_parser().parse_args(["status"])
        previous = os.environ.pop("HANDOFF_REPO", None)
        try:
            self.assertIsNone(handoff.target_repo(args))
        finally:
            if previous is not None:
                os.environ["HANDOFF_REPO"] = previous

    def test_handoff_repo_environment_variable_is_honoured(self):
        args = handoff.build_parser().parse_args(["status"])
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HANDOFF_REPO"] = tmp
            try:
                self.assertEqual(handoff.target_repo(args), Path(tmp))
            finally:
                os.environ.pop("HANDOFF_REPO", None)

    def test_explicit_repo_wins_over_the_environment_variable(self):
        with tempfile.TemporaryDirectory() as chosen, tempfile.TemporaryDirectory() as other:
            args = handoff.build_parser().parse_args(["status", "--repo", chosen])
            os.environ["HANDOFF_REPO"] = other
            try:
                self.assertEqual(handoff.target_repo(args), Path(chosen))
            finally:
                os.environ.pop("HANDOFF_REPO", None)

    def test_a_repo_path_that_is_not_a_directory_is_a_usage_error(self):
        args = handoff.build_parser().parse_args(["status", "--repo", "/definitely/not/here"])
        with self.assertRaises(handoff.HandoffError) as caught:
            handoff.target_repo(args)
        self.assertEqual(caught.exception.code, handoff.EXIT_USAGE)

    def test_exit_codes_are_distinct(self):
        codes = [handoff.EXIT_OK, handoff.EXIT_ERROR, handoff.EXIT_USAGE,
                 handoff.EXIT_NO_SESSION, handoff.EXIT_GIT, handoff.EXIT_LLM]
        self.assertEqual(len(codes), len(set(codes)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
