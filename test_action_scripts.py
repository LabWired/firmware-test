#!/usr/bin/env python3
"""Tests for upload.py / comment.py against the REAL `labwired test` result.json
schema (crates/cli/src/artifacts.rs): status, assertions (a list of
{"assertion": {<one-key>: ...}, "passed": bool}), cycles, instructions,
steps_executed, config, plus other keys these scripts must never depend on.

Run with: python3 -m unittest integrations.labwired-action.test_action_scripts
(or simply `python3 -m unittest` from this directory) — no third-party test
runner is assumed to be installed on the CI/dev box, so this deliberately uses
only the stdlib unittest module rather than pytest.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


upload = _load("labwired_action_upload", "upload.py")
comment = _load("labwired_action_comment", "comment.py")


REAL_SCHEMA_FIXTURE = {
    "result_schema_version": 1,
    "status": "fail",
    "steps_executed": 4,
    "cycles": 123456,
    "instructions": 98765,
    "stop_reason": "assertion_failed",
    "stop_reason_details": None,
    "limits": {"max_cycles": 10_000_000},
    "assertions": [
        {"assertion": {"uart_contains": "boot ok"}, "passed": True},
        {"assertion": {"expected_stop_reason": "halt"}, "passed": False},
        {"assertion": {"memory_value": {"address": 32, "value": 1}}, "passed": True},
    ],
    "cpu_state": {"pc": 0x1000},
    "firmware_hash": "sha256:deadbeef",
    "config": {"board": "stm32f103"},
}


class ReadResultTests(unittest.TestCase):
    def test_pass_fail_counting_from_real_schema(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(REAL_SCHEMA_FIXTURE, fh)
            result = upload.read_result(d)
        assertions = result.get("assertions")
        passed = sum(1 for a in assertions if isinstance(a, dict) and a.get("passed") is True)
        failed = sum(1 for a in assertions if isinstance(a, dict) and a.get("passed") is False)
        self.assertEqual(passed, 2)
        self.assertEqual(failed, 1)

    def test_missing_result_json_returns_empty_dict_no_exception(self):
        with tempfile.TemporaryDirectory() as d:
            result = upload.read_result(d)  # no result.json written
        self.assertEqual(result, {})

    def test_bare_json_array_top_level_returns_empty_dict_no_exception(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump([1, 2, 3], fh)
            result = upload.read_result(d)
        self.assertEqual(result, {})


class CommentRenderTests(unittest.TestCase):
    def test_mixed_pass_fail_markdown(self):
        body = comment.render(REAL_SCHEMA_FIXTURE, "", "https://app.labwired.com/ci/acme/blinky/deadbeef")
        self.assertIn("❌ LabWired simulation — fail", body)
        self.assertIn("uart_contains", body)
        self.assertIn("expected_stop_reason", body)
        self.assertIn("memory_value", body)
        # One passed (✅), one failed (❌) row plus the header icon — at least
        # two check-mark rows and one cross-mark row in the assertion table.
        self.assertGreaterEqual(body.count("✅"), 1)
        self.assertGreaterEqual(body.count("❌"), 1)
        self.assertIn("Full report", body)

    def test_status_error_with_empty_assertions(self):
        fixture = {**REAL_SCHEMA_FIXTURE, "status": "error", "assertions": []}
        body = comment.render(fixture, "", "")
        self.assertIn("⚠️ LabWired simulation — error", body)
        # No assertion table when there are no assertions.
        self.assertNotIn("| Assertion | Result | Summary |", body)

    def test_missing_result_json_renders_unknown_status_no_exception(self):
        with tempfile.TemporaryDirectory() as d:
            result = comment.read_json(os.path.join(d, "result.json"))
        body = comment.render(result, "", "")
        self.assertIn("unknown", body)

    def test_bare_json_array_top_level_renders_unknown_status_no_exception(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "result.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(["not", "an", "object"], fh)
            result = comment.read_json(path)
        self.assertEqual(result, {})
        body = comment.render(result, "", "")
        self.assertIn("unknown", body)

    def test_uart_tail_with_triple_backtick_cannot_break_out_of_fence(self):
        malicious_uart = "normal output\n```\n[malicious injected markdown]\n```\nmore output"
        body = comment.render({"status": "pass", "assertions": []}, malicious_uart, "")
        # A four-backtick fence cannot be closed by a three-backtick line, so
        # the whole captured tail (including the embedded ``` lines) stays
        # inside a single code block.
        self.assertIn("````", body)
        fence_count = body.count("````")
        self.assertEqual(fence_count, 2)


class UploadRunEndToEndSafetyTests(unittest.TestCase):
    """run() must not raise even when result.json is missing/malformed —
    verified via the public entry point main(), which always returns 0."""

    def _run_main_in(self, output_dir, env_overrides):
        env_backup = dict(os.environ)
        try:
            os.environ["LABWIRED_OUTPUT_DIR"] = output_dir
            os.environ["LABWIRED_OIDC_TOKEN"] = ""  # short-circuits before any network call
            os.environ.update(env_overrides)
            with tempfile.TemporaryDirectory() as gh_out_dir:
                gh_output = os.path.join(gh_out_dir, "github_output")
                open(gh_output, "w").close()
                os.environ["GITHUB_OUTPUT"] = gh_output
                return upload.main()
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def test_missing_result_json_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            code = self._run_main_in(d, {})
        self.assertEqual(code, 0)

    def test_bare_array_result_json_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(["x"], fh)
            code = self._run_main_in(d, {})
        self.assertEqual(code, 0)

    def test_private_repo_skips_upload_without_token(self):
        with tempfile.TemporaryDirectory() as d:
            code = self._run_main_in(d, {"LABWIRED_REPO_PRIVATE": "true"})
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
