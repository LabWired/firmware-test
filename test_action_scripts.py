#!/usr/bin/env python3
"""Tests for upload.py / comment.py against the REAL `labwired test` result.json
schema (crates/cli/src/artifacts.rs): status, assertions (a list of
{"assertion": {<one-key>: ...}, "passed": bool}), cycles, instructions,
steps_executed, config, plus other keys these scripts must never depend on.

Also covers verdict.py, the unproven verdict. Its rule and parser mirror
@labwired/board-config; to run every case here through the TypeScript
implementation too, point LABWIRED_BOARD_CONFIG at a built
packages/board-config/dist/index.js from the labwired monorepo.

Run with `python3 -m unittest` from this directory. No third-party test runner
is assumed to be installed on the CI/dev box, so this deliberately uses only
the stdlib unittest module rather than pytest.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Loaded under its own name first: upload.py and comment.py `import verdict`,
# which the action satisfies by running them from this directory.
verdict = _load("verdict", "verdict.py")
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


# ---------------------------------------------------------------------------
# The unproven verdict (verdict.py), mirrored from ciRunVerdict and
# parseCoverageFromSystemYaml in @labwired/board-config twin-coverage.ts.
# ---------------------------------------------------------------------------

SCHEMA = "labwired.twin-coverage.v1"
U9 = {"ref": "U9", "value": "ASIC99-XYZ", "reason": "symbol Mystery:ASIC99 is not in the catalog"}
COVERAGE = {"schema": SCHEMA, "source_kind": "kicad_sch", "design_only": [U9]}
NONE_DROPPED = {"schema": SCHEMA, "source_kind": "kicad_sch", "design_only": []}

# Keep this table in sync with the ciRunVerdict table in
# packages/board-config/test/twin-coverage.test.ts (labwired monorepo).
RULE_TABLE = [
    ("pass", 2, None, "pass"),
    ("fail", 2, None, "fail"),
    ("error", 0, None, "error"),
    ("pass", 0, None, "pass"),              # no record: not an imported twin, rule does not apply
    ("pass", 2, NONE_DROPPED, "pass"),
    ("pass", 0, NONE_DROPPED, "unproven"),  # imported, nothing asserted
    ("pass", 2, COVERAGE, "unproven"),
    ("fail", 2, COVERAGE, "unproven"),      # a failure on an incomplete twin is not a proven failure
    ("error", 2, COVERAGE, "error"),        # the run never happened; coverage says nothing about it
    ("garbage", 2, None, "error"),
]


def coverage_line(record):
    return "coverage: " + json.dumps(record, separators=(",", ":"))


def manifest(record_line):
    return f'name: "twin"\nchip: "stm32l476"\n{record_line}\nexternal_devices:\n  []\n'


# (label, system.yaml text, expected parse result). Every case is also fed to the
# TypeScript parser by CrossCheckAgainstBoardConfigTests.
PARSE_CASES = [
    ("no record", 'name: "x"\nchip: "inline"\n', None),
    ("well formed", manifest(coverage_line(COVERAGE)), COVERAGE),
    ("empty design_only", manifest(coverage_line(NONE_DROPPED)), NONE_DROPPED),
    ("CRLF line endings", manifest(coverage_line(COVERAGE)).replace("\n", "\r\n"), COVERAGE),
    ("no space after the colon", manifest("coverage:" + json.dumps(COVERAGE)), COVERAGE),
    ("trailing spaces and a tab", manifest(coverage_line(COVERAGE) + " \t "), COVERAGE),
    ("record on the last line, no newline", 'name: "x"\n' + coverage_line(COVERAGE), COVERAGE),
    ("first of two records wins", manifest(coverage_line(COVERAGE) + "\n" + coverage_line(NONE_DROPPED)), COVERAGE),
    ("unknown keys are dropped", manifest(coverage_line({**COVERAGE, "extra": 1, "design_only": [{**U9, "pin": 4}]})), COVERAGE),
    ("escaped U+2028 in a reason", manifest(coverage_line({**COVERAGE, "design_only": [{**U9, "reason": "a b"}]})),
     {**COVERAGE, "design_only": [{**U9, "reason": "a b"}]}),
    ("other schema", manifest(coverage_line({**COVERAGE, "schema": "nope"})), None),
    ("not JSON", manifest("coverage: {not json"), None),
    ("NaN is not JSON", manifest('coverage: {"schema":"%s","source_kind":"k","design_only":[],"n":NaN}' % SCHEMA), None),
    ("indented is not top level", manifest("  " + coverage_line(COVERAGE)), None),
    ("trailing comment", manifest(coverage_line(COVERAGE) + " # imported"), None),
    ("missing source_kind", manifest(coverage_line({"schema": SCHEMA, "design_only": [U9]})), None),
    ("empty source_kind", manifest(coverage_line({**COVERAGE, "source_kind": ""})), None),
    ("design_only not a list", manifest(coverage_line({**COVERAGE, "design_only": {"U9": U9}})), None),
    ("one entry without a value voids the record", manifest(coverage_line({**COVERAGE, "design_only": [U9, {"ref": "U3", "reason": "x"}]})), None),
    ("numeric value", manifest(coverage_line({**COVERAGE, "design_only": [{**U9, "value": 99}]})), None),
    ("empty ref", manifest(coverage_line({**COVERAGE, "design_only": [{**U9, "ref": ""}]})), None),
    ("null reason", manifest(coverage_line({**COVERAGE, "design_only": [{**U9, "reason": None}]})), None),
    ("entry not an object", manifest(coverage_line({**COVERAGE, "design_only": ["U9"]})), None),
]


def result_json(status="pass", assertions=1, system=None):
    return {
        "result_schema_version": 1,
        "status": status,
        "assertions": [{"assertion": {"uart_contains": "READY"}, "passed": status == "pass"}] * assertions,
        "config": {"firmware": "fw.elf", "system": system, "script": "test.yaml"},
    }


class VerdictRuleTests(unittest.TestCase):
    def test_rule_table(self):
        for status, count, coverage, want in RULE_TABLE:
            with self.subTest(status=status, assertions=count, coverage=coverage and len(coverage["design_only"])):
                self.assertEqual(verdict.run_verdict(status, count, coverage)["verdict"], want)

    def test_names_why(self):
        self.assertEqual(
            verdict.run_verdict("pass", 0, COVERAGE)["reasons"],
            ["design_only_parts", "nothing_asserted"],
        )

    def test_parse_cases(self):
        for label, text, want in PARSE_CASES:
            with self.subTest(label):
                self.assertEqual(verdict.parse_coverage(text), want)

    def test_explain_names_every_part(self):
        v = verdict.run_verdict("pass", 1, {**COVERAGE, "design_only": [U9, {"ref": "R7", "value": "", "reason": "x"}]})
        self.assertEqual(verdict.explain(v), "design-only parts not on the twin: U9 (ASIC99-XYZ), R7")


class SystemManifestLookupTests(unittest.TestCase):
    def test_result_config_system_is_what_ran(self):
        with tempfile.TemporaryDirectory() as d:
            ran = os.path.join(d, "ran.yaml")
            other = os.path.join(d, "other.yaml")
            Path(ran).write_text(manifest(coverage_line(COVERAGE)), encoding="utf-8")
            Path(other).write_text(manifest(coverage_line(NONE_DROPPED)), encoding="utf-8")
            v = verdict.verdict_of(result_json(system=ran), None, other)
        self.assertEqual(v["verdict"], "unproven")

    def test_script_system_resolves_against_the_script_like_the_cli(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "tests", "twin"))
            Path(d, "tests", "twin", "system.yaml").write_text(manifest(coverage_line(COVERAGE)), encoding="utf-8")
            script = os.path.join(d, "tests", "labwired.yml")
            Path(script).write_text(
                'schema_version: "1.0"\ninputs:\n  firmware: fw.elf\n  system: "twin/system.yaml"  \n', encoding="utf-8"
            )
            v = verdict.verdict_of({"status": "pass", "assertions": [{}]}, script, None)
        self.assertEqual(v["verdict"], "unproven")
        self.assertEqual([p["ref"] for p in v["design_only"]], ["U9"])

    def test_chip_only_run_has_no_record(self):
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, "t.yml")
            Path(script).write_text("inputs:\n  firmware: fw.elf\n  chip: stm32f103\n", encoding="utf-8")
            v = verdict.verdict_of({"status": "pass", "assertions": []}, script, None)
        self.assertEqual(v["verdict"], "pass")


class VerdictExitCodeTests(unittest.TestCase):
    """main() is the action's last step: its return value is the job's exit code."""

    def _main(self, status, exit_code, coverage, assertions=1, allow=None):
        with tempfile.TemporaryDirectory() as d:
            system = os.path.join(d, "system.yaml")
            Path(system).write_text(manifest(coverage_line(coverage)) if coverage else manifest(""), encoding="utf-8")
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(result_json(status, assertions, system), fh)
            env = {"LABWIRED_OUTPUT_DIR": d, "LABWIRED_SCRIPT": "", "LABWIRED_SYSTEM": ""}
            if exit_code is not None:
                env["LABWIRED_EXIT_CODE"] = str(exit_code)
            if allow is not None:
                env["LABWIRED_ALLOW_UNPROVEN"] = allow
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(stdout):
                if exit_code is None:
                    os.environ.pop("LABWIRED_EXIT_CODE", None)
                code = verdict.main()
        return code, stdout.getvalue()

    def test_pass_on_a_complete_twin_is_green(self):
        self.assertEqual(self._main("pass", 0, NONE_DROPPED)[0], 0)

    def test_hand_written_manifest_is_judged_as_before(self):
        self.assertEqual(self._main("pass", 0, None, assertions=0)[0], 0)
        self.assertEqual(self._main("fail", 1, None)[0], 1)

    def test_unproven_exit_code_is_not_one_the_cli_uses(self):
        # labwired-cli: 0 pass, 1 assertion fail, 2 config error, 3 runtime
        # error (core/crates/cli/src/lib.rs). An incomplete twin must never
        # read as a simulator crash to a script checking the exit code.
        self.assertNotIn(verdict.EXIT_UNPROVEN, (0, 1, 2, 3))

    def test_unproven_pass_exits_4_and_names_the_parts(self):
        code, out = self._main("pass", 0, COVERAGE)
        self.assertEqual(code, verdict.EXIT_UNPROVEN)
        self.assertEqual(code, 4)
        self.assertIn("::error::", out)
        self.assertIn("UNPROVEN", out)
        self.assertIn("U9 (ASIC99-XYZ)", out)

    def test_design_text_cannot_start_a_workflow_command(self):
        forged = {"ref": "U9", "value": "x\n::add-mask::secret", "reason": "100% unknown"}
        code, out = self._main("pass", 0, {**COVERAGE, "design_only": [forged]})
        self.assertEqual(code, 4)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("x%0A::add-mask::secret", out)

    def test_nothing_asserted_on_an_imported_twin_exits_4(self):
        code, out = self._main("pass", 0, NONE_DROPPED, assertions=0)
        self.assertEqual(code, 4)
        self.assertIn("asserted nothing", out)

    def test_failure_on_an_incomplete_twin_is_unproven_and_exits_4(self):
        self.assertEqual(self._main("fail", 1, COVERAGE)[0], 4)

    def test_allow_unproven_hands_the_exit_back_to_the_cli(self):
        code, out = self._main("pass", 0, COVERAGE, allow="true")
        self.assertEqual(code, 0)
        self.assertIn("::warning::", out)
        # Allowing an unproven run never turns a failing assertion green.
        self.assertEqual(self._main("fail", 1, COVERAGE, allow="true")[0], 1)

    def test_error_keeps_the_cli_code(self):
        self.assertEqual(self._main("error", 2, COVERAGE)[0], 2)

    def test_missing_exit_code_is_never_green(self):
        self.assertEqual(self._main("pass", None, None)[0], 1)


class UnprovenCommentTests(unittest.TestCase):
    def _unproven(self, **over):
        return verdict.run_verdict("pass", 1, {**COVERAGE, **over})

    def test_header_and_design_only_table(self):
        fixture = {"status": "pass", "assertions": [{"assertion": {"uart_contains": "READY"}, "passed": True}]}
        body = comment.render(fixture, "", "", self._unproven())
        self.assertIn("### 🟡 LabWired simulation — unproven", body)
        self.assertNotIn("simulation — pass", body)
        self.assertIn("| Design-only part | Value | Why it is not on the twin |", body)
        self.assertIn("| U9 | ASIC99-XYZ | symbol Mystery:ASIC99 is not in the catalog |", body)
        self.assertIn("set `allow_unproven: true`", body)
        # The assertions that passed are still shown.
        self.assertIn("uart_contains", body)

    def test_allow_unproven_changes_the_consequence_line(self):
        body = comment.render({"status": "pass", "assertions": []}, "", "", self._unproven(), allow_unproven=True)
        self.assertIn("`allow_unproven` is set", body)

    def test_design_text_cannot_inject_markdown(self):
        nasty = {"ref": "U1|x", "value": "<img src=x>", "reason": "[click](https://evil)\n| row |"}
        body = comment.render({"status": "pass", "assertions": []}, "", "", self._unproven(design_only=[nasty]))
        row = next(line for line in body.splitlines() if line.startswith("| U1"))
        self.assertEqual(row.count(" | "), 2)  # three cells, no forged column or row
        # Backslash-escaped punctuation renders as literal text in GitHub markdown.
        self.assertIsNone(re.search(r"(?<!\\)<img", body))
        self.assertIsNone(re.search(r"(?<!\\)\[click", body))
        self.assertIn("\\<img src=x\\>", body)

    def test_status_output_is_the_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            system = os.path.join(d, "system.yaml")
            Path(system).write_text(manifest(coverage_line(COVERAGE)), encoding="utf-8")
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(result_json("pass", 1, system), fh)
            gh_output = os.path.join(d, "github_output")
            summary = os.path.join(d, "summary.md")
            env = {
                "LABWIRED_OUTPUT_DIR": d,
                "GITHUB_OUTPUT": gh_output,
                "GITHUB_STEP_SUMMARY": summary,
                "LABWIRED_COMMENT": "false",
                "LABWIRED_SCRIPT": "",
                "LABWIRED_SYSTEM": "",
            }
            with mock.patch.dict(os.environ, env):
                self.assertEqual(comment.main(), 0)
            self.assertIn("status=unproven", Path(gh_output).read_text(encoding="utf-8"))
            self.assertIn("LabWired simulation — unproven", Path(summary).read_text(encoding="utf-8"))

    def test_missing_result_json_is_still_unknown(self):
        self.assertEqual(comment.display_status({}, verdict.run_verdict(None, 0, None)), "unknown")


class UnprovenUploadTests(unittest.TestCase):
    def test_upload_sends_the_verdict_as_status(self):
        sent = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            sent.update(json.loads(request.data.decode("utf-8")))
            return Response(json.dumps({"report_url": "https://app.labwired.com/ci/run/a/b/c"}).encode("utf-8"))

        with tempfile.TemporaryDirectory() as d:
            system = os.path.join(d, "system.yaml")
            Path(system).write_text(manifest(coverage_line(COVERAGE)), encoding="utf-8")
            with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(result_json("pass", 2, system), fh)
            env = {
                "LABWIRED_OUTPUT_DIR": d,
                "LABWIRED_OIDC_TOKEN": "t",
                "LABWIRED_GALLERY": "false",
                "LABWIRED_REPO_PRIVATE": "false",
                "LABWIRED_SCRIPT": "",
                "LABWIRED_SYSTEM": "",
                "GITHUB_OUTPUT": os.path.join(d, "github_output"),
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(upload.urllib.request, "urlopen", fake_urlopen):
                self.assertEqual(upload.main(), 0)
        self.assertEqual(sent["status"], "unproven")
        self.assertEqual(sent["tests_passed"], 2)

    def test_bundle_carries_the_manifest_the_script_named(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "twin"))
            system = os.path.join(d, "twin", "system.yaml")
            Path(system).write_text(manifest(coverage_line(COVERAGE)), encoding="utf-8")
            script = os.path.join(d, "labwired.yml")
            Path(script).write_text("inputs:\n  firmware: fw.elf\n  system: twin/system.yaml\n", encoding="utf-8")
            out_dir = os.path.join(d, "out")
            os.makedirs(out_dir)
            with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(result_json("pass", 1, system), fh)
            sent = {}

            class Response(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            def fake_urlopen(request, timeout):
                sent.update(json.loads(request.data.decode("utf-8")))
                return Response(b"{}")

            env = {
                "LABWIRED_OUTPUT_DIR": out_dir,
                "LABWIRED_OIDC_TOKEN": "t",
                "LABWIRED_GALLERY": "true",
                "LABWIRED_REPO_PRIVATE": "false",
                "LABWIRED_SCRIPT": script,
                "LABWIRED_SYSTEM": "",
                "LABWIRED_FIRMWARE": "",
                "GITHUB_OUTPUT": os.path.join(d, "github_output"),
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(upload.urllib.request, "urlopen", fake_urlopen):
                self.assertEqual(upload.main(), 0)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(sent["bundle_base64"])), mode="r:gz") as tar:
            names = tar.getnames()
            carried = tar.extractfile("inputs/system.yaml").read().decode("utf-8")
        self.assertEqual(names.count("inputs/system.yaml"), 1)
        self.assertEqual(verdict.parse_coverage(carried), COVERAGE)


BOARD_CONFIG = os.environ.get("LABWIRED_BOARD_CONFIG", "")

NODE_CROSS_CHECK = """
import { pathToFileURL } from 'node:url';
const { ciRunVerdict, parseCoverageFromSystemYaml } = await import(pathToFileURL(process.argv[1]).href);
let input = '';
for await (const chunk of process.stdin) input += chunk;
const out = JSON.parse(input).map((c) => {
  const coverage = parseCoverageFromSystemYaml(c.system_yaml);
  return { coverage, ...ciRunVerdict({ status: c.status, assertion_count: c.assertion_count, coverage }) };
});
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(
    BOARD_CONFIG and os.path.isfile(BOARD_CONFIG) and shutil.which("node"),
    "set LABWIRED_BOARD_CONFIG to a built packages/board-config/dist/index.js (and have node) "
    "to run this file's cases through the TypeScript rule as well",
)
class CrossCheckAgainstBoardConfigTests(unittest.TestCase):
    """The same inputs through verdict.py and through @labwired/board-config.
    Both parse the manifest themselves, so this pins the line match and the
    schema check as well as the rule."""

    def test_python_and_typescript_agree(self):
        cases = []
        for status, count, coverage, _ in RULE_TABLE:
            text = manifest(coverage_line(coverage)) if coverage else manifest("")
            cases.append({"label": f"rule {status}/{count}", "system_yaml": text, "status": status, "assertion_count": count})
        for label, text, _ in PARSE_CASES:
            for status, count in (("pass", 1), ("pass", 0), ("fail", 1)):
                cases.append({"label": f"{label} {status}/{count}", "system_yaml": text, "status": status, "assertion_count": count})
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", NODE_CROSS_CHECK, os.path.abspath(BOARD_CONFIG)],
            input=json.dumps([{k: v for k, v in c.items() if k != "label"} for c in cases]),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        ts = json.loads(proc.stdout)
        self.assertEqual(len(ts), len(cases))
        for case, theirs in zip(cases, ts):
            with self.subTest(case["label"]):
                coverage = verdict.parse_coverage(case["system_yaml"])
                ours = verdict.run_verdict(case["status"], case["assertion_count"], coverage)
                self.assertEqual(coverage, theirs["coverage"])
                self.assertEqual(ours, {k: theirs[k] for k in ("verdict", "reasons", "design_only")})


if __name__ == "__main__":
    unittest.main()
