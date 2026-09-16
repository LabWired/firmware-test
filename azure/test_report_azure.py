#!/usr/bin/env python3
"""Tests for azure/report_azure.py: the Azure DevOps reporter and gate.

Run from the repo root with either of:

    python3 azure/test_report_azure.py
    python3 -m unittest discover -s azure -p 'test_*.py'

Stdlib only, like test_action_scripts.py: no third-party runner is assumed.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _path in (str(HERE), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import comment  # noqa: E402
import report_azure  # noqa: E402
import verdict  # noqa: E402

SCHEMA = "labwired.twin-coverage.v1"
COVERAGE = {
    "schema": SCHEMA,
    "source_kind": "kicad_sch",
    "design_only": [{"ref": "U9", "value": "ASIC99", "reason": "not in the catalog"}],
}


def manifest(record):
    return (
        'name: "twin"\nchip: "stm32l476"\ncoverage: '
        + json.dumps(record, separators=(",", ":"))
        + "\n"
    )


def result_json(status="pass", assertions=2, system=None):
    return {
        "result_schema_version": 1,
        "status": status,
        "assertions": [
            {"assertion": {"uart_contains": "READY"}, "passed": status == "pass"}
        ]
        * assertions,
        "config": {"firmware": "fw.elf", "system": system, "script": "test.yaml"},
    }


class AdoTestCase(unittest.TestCase):
    """A temp output dir, a clean fake ADO environment, captured stdout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = self._tmp.name
        self.env = {
            "LABWIRED_OUTPUT_DIR": self.out,
            "LABWIRED_SCRIPT": "",
            "LABWIRED_SYSTEM": "",
            "LABWIRED_ALLOW_UNPROVEN": "false",
            "LABWIRED_PR_COMMENT": "false",
        }

    def write_result(self, status="pass", assertions=2, system=None):
        with open(os.path.join(self.out, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(result_json(status, assertions, system), fh)

    def write_system(self, coverage):
        path = os.path.join(self.out, "system.yaml")
        Path(path).write_text(manifest(coverage), encoding="utf-8")
        return path

    def write_exit_code(self, code):
        Path(os.path.join(self.out, "exit_code")).write_text(str(code), encoding="utf-8")

    def run_main(self, argv=()):
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(stdout):
            code = report_azure.main(list(argv))
        return code, stdout.getvalue()


class VerdictGateTests(AdoTestCase):
    def test_missing_exit_code_is_never_green(self):
        self.write_result("pass")
        code, out = self.run_main(["--verdict"])
        self.assertEqual(code, 1)
        self.assertIn("##vso[task.logissue type=error]", out)
        self.assertIn("recorded no exit code", out)

    def test_pass_on_a_complete_twin_is_green(self):
        self.write_result("pass")
        self.write_exit_code(0)
        code, out = self.run_main(["--verdict"])
        self.assertEqual(code, 0)
        self.assertNotIn("type=error", out)

    def test_failure_keeps_the_cli_code(self):
        self.write_result("fail")
        self.write_exit_code(1)
        code, out = self.run_main(["--verdict"])
        self.assertEqual(code, 1)
        self.assertIn("exit code 1", out)

    def test_error_keeps_the_cli_code(self):
        self.write_result("error", assertions=0)
        self.write_exit_code(3)
        code, out = self.run_main(["--verdict"])
        self.assertEqual(code, 3)
        self.assertIn("exit code 3", out)

    def test_unproven_exits_4_and_names_the_parts(self):
        system = self.write_system(COVERAGE)
        self.env["LABWIRED_SYSTEM"] = system
        self.write_result("pass", system=system)
        self.write_exit_code(0)
        code, out = self.run_main(["--verdict"])
        self.assertEqual(code, verdict.EXIT_UNPROVEN)
        self.assertIn("UNPROVEN", out)
        self.assertIn("U9", out)
        self.assertIn("allow_unproven", out)

    def test_allow_unproven_hands_the_exit_back_to_the_cli(self):
        system = self.write_system(COVERAGE)
        self.env["LABWIRED_SYSTEM"] = system
        self.env["LABWIRED_ALLOW_UNPROVEN"] = "true"
        self.write_result("pass", system=system)
        self.write_exit_code(0)
        code, out = self.run_main(["--verdict"])
        self.assertEqual(code, 0)
        self.assertIn("type=warning", out)

    def test_design_text_cannot_start_a_logging_command(self):
        coverage = {
            **COVERAGE,
            "design_only": [
                {"ref": "U9\n##vso[task.complete result=Succeeded]", "value": "", "reason": "x"}
            ],
        }
        system = self.write_system(coverage)
        self.env["LABWIRED_SYSTEM"] = system
        self.write_result("pass", system=system)
        self.write_exit_code(0)
        code, out = self.run_main(["--verdict"])
        self.assertEqual(code, verdict.EXIT_UNPROVEN)
        self.assertNotIn("\n##vso[task.complete", out)
        self.assertIn("%0A##vso[task.complete", out)


class ReportModeTests(AdoTestCase):
    def test_summary_is_written_and_uploaded(self):
        self.write_result("pass")
        code, out = self.run_main([])
        self.assertEqual(code, 0)
        summary = Path(self.out, "labwired-summary.md").read_text(encoding="utf-8")
        self.assertIn(comment.MARKER, summary)
        self.assertIn("LabWired simulation", summary)
        self.assertEqual(
            summary,
            comment.render(result_json("pass"), "", "", verdict.verdict_of(result_json("pass"))) + "\n",
        )
        self.assertIn("##vso[task.uploadsummary]", out)
        self.assertIn("##vso[build.addbuildtag]labwired-pass", out)

    def test_failure_is_tagged_fail(self):
        self.write_result("fail")
        code, out = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("##vso[build.addbuildtag]labwired-fail", out)

    def test_unproven_is_tagged_and_warns_when_allowed(self):
        system = self.write_system(COVERAGE)
        self.env["LABWIRED_SYSTEM"] = system
        self.env["LABWIRED_ALLOW_UNPROVEN"] = "true"
        self.write_result("pass", system=system)
        code, out = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("##vso[build.addbuildtag]labwired-unproven", out)
        self.assertIn("type=warning", out)

    def test_unproven_report_mode_still_exits_zero_when_not_allowed(self):
        system = self.write_system(COVERAGE)
        self.env["LABWIRED_SYSTEM"] = system
        self.write_result("pass", system=system)
        code, out = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("labwired-unproven", out)
        self.assertNotIn("type=error", out)

    def test_missing_result_json_still_reports(self):
        code, out = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("labwired-unknown", out)
        self.assertIn("##vso[task.uploadsummary]", out)

    def test_uart_tail_is_in_the_summary(self):
        self.write_result("pass")
        Path(self.out, "uart.log").write_text("boot ok\nLED ON\n", encoding="utf-8")
        code, _ = self.run_main([])
        self.assertEqual(code, 0)
        summary = Path(self.out, "labwired-summary.md").read_text(encoding="utf-8")
        self.assertIn("UART output (tail)", summary)
        self.assertIn("LED ON", summary)


class PrThreadTests(AdoTestCase):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def env_for_pr(self):
        self.env.update(
            {
                "SYSTEM_PULLREQUEST_PULLREQUESTID": "42",
                "SYSTEM_ACCESSTOKEN": "secret",
                "BUILD_REPOSITORY_ID": "repo-guid",
                "SYSTEM_COLLECTIONURI": "https://dev.azure.com/acme/",
                "SYSTEM_TEAMPROJECT": "proj",
                "LABWIRED_PR_COMMENT": "true",
            }
        )

    def test_first_run_creates_a_thread(self):
        self.env_for_pr()
        self.write_result("pass")
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.method, request.full_url, request.data))
            if request.method == "GET":
                return self.Response(b'{"count":0,"value":[]}')
            return self.Response(b"{}")

        with mock.patch.object(report_azure.urllib.request, "urlopen", fake_urlopen):
            code, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])
        self.assertIn("/pullRequests/42/threads?api-version=7.1", calls[1][1])
        payload = json.loads(calls[1][2].decode("utf-8"))
        self.assertIn(comment.MARKER, payload["comments"][0]["content"])
        self.assertEqual(payload["status"], 1)

    def test_rerun_edits_the_existing_comment(self):
        self.env_for_pr()
        self.write_result("pass")
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.method, request.full_url, request.data))
            if request.method == "GET":
                body = {
                    "count": 1,
                    "value": [{"id": 7, "comments": [{"id": 3, "content": comment.MARKER + "\nold"}]}],
                }
                return self.Response(json.dumps(body).encode("utf-8"))
            return self.Response(b"{}")

        with mock.patch.object(report_azure.urllib.request, "urlopen", fake_urlopen):
            code, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in calls], ["GET", "PATCH"])
        self.assertIn("/threads/7/comments/3?api-version=7.1", calls[1][1])
        self.assertEqual(json.loads(calls[1][2].decode("utf-8"))["content"].count(comment.MARKER), 1)

    def test_no_pr_id_means_no_network(self):
        self.write_result("pass")

        def explode(request, timeout):
            raise AssertionError("no request expected")

        with mock.patch.object(report_azure.urllib.request, "urlopen", explode):
            code, _ = self.run_main([])
        self.assertEqual(code, 0)

    def test_comment_false_skips_the_thread(self):
        self.env_for_pr()
        self.env["LABWIRED_PR_COMMENT"] = "false"
        self.write_result("pass")

        def explode(request, timeout):
            raise AssertionError("no request expected")

        with mock.patch.object(report_azure.urllib.request, "urlopen", explode):
            code, _ = self.run_main([])
        self.assertEqual(code, 0)

    def test_api_failure_is_a_warning_and_exit_zero(self):
        self.env_for_pr()
        self.write_result("pass")

        def fail(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        with mock.patch.object(report_azure.urllib.request, "urlopen", fail):
            code, out = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("type=warning", out)
        self.assertIn("403", out)

    def test_hostile_design_text_cannot_inject_markdown(self):
        self.env_for_pr()
        self.write_result("pass")
        sent = {}

        def fake_urlopen(request, timeout):
            if request.method == "GET":
                return self.Response(b'{"count":0,"value":[]}')
            sent.update(json.loads(request.data.decode("utf-8")))
            return self.Response(b"{}")

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "system.yaml")
            coverage = {
                **COVERAGE,
                "design_only": [{"ref": "| U9 |", "value": "<b>", "reason": "`x`"}],
            }
            Path(path).write_text(manifest(coverage), encoding="utf-8")
            self.env["LABWIRED_SYSTEM"] = path
            with mock.patch.object(report_azure.urllib.request, "urlopen", fake_urlopen):
                code, _ = self.run_main([])
        self.assertEqual(code, 0)
        content = sent["comments"][0]["content"]
        self.assertIn("\\| U9 \\|", content)
        self.assertIn("\\`x\\`", content)

    def test_urls_are_quoted_and_requests_are_authorized(self):
        self.env_for_pr()
        self.env["SYSTEM_TEAMPROJECT"] = "my project"
        self.write_result("pass")
        seen = []

        def fake_urlopen(request, timeout):
            seen.append(request)
            if request.method == "GET":
                return self.Response(b'{"count":0,"value":[]}')
            return self.Response(b"{}")

        with mock.patch.object(report_azure.urllib.request, "urlopen", fake_urlopen):
            code, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertIn("/my%20project/_apis/git/repositories/repo-guid/", seen[0].full_url)
        for request in seen:
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
            self.assertIn("labwired-firmware-test", request.get_header("User-agent"))

    def test_malformed_thread_listing_creates_instead_of_crashing(self):
        self.env_for_pr()
        self.write_result("pass")
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(request.method)
            if request.method == "GET":
                # ADO-shaped but hostile: null entry, no comments, a comment
                # without an id.
                return self.Response(b'{"count":3,"value":[null,{"id":1},{"id":2,"comments":[{}]}]}')
            return self.Response(b"{}")

        with mock.patch.object(report_azure.urllib.request, "urlopen", fake_urlopen):
            code, _ = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["GET", "POST"])


try:
    import yaml  # noqa: F401

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class PipelineYamlTests(unittest.TestCase):
    def test_pipeline_has_the_required_shape(self):
        import yaml

        doc = yaml.safe_load((HERE / "azure-pipelines.yml").read_text(encoding="utf-8"))
        steps = doc["steps"]
        scripts = [s.get("script", "") for s in steps if "script" in s]
        tasks = [s.get("task", "") for s in steps if "task" in s]
        for fragment in ("labwired test", "report_azure.py", "--verdict"):
            self.assertTrue(any(fragment in s for s in scripts), fragment)
        self.assertIn("PublishTestResults@2", tasks)
        self.assertIn("PublishPipelineArtifact@1", tasks)
        self.assertEqual(doc["variables"]["allow_unproven"], "false")

    def test_every_always_step_has_a_condition(self):
        import yaml

        doc = yaml.safe_load((HERE / "azure-pipelines.yml").read_text(encoding="utf-8"))
        publishing = [
            s
            for s in doc["steps"]
            if s.get("displayName", "").startswith(("Report", "Publish"))
        ]
        self.assertEqual(len(publishing), 3)
        for step in publishing:
            self.assertEqual(step.get("condition"), "always()")


if __name__ == "__main__":
    unittest.main()
