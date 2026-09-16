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


if __name__ == "__main__":
    unittest.main()
