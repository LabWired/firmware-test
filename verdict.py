#!/usr/bin/env python3
"""The run verdict: pass, fail, error, or unproven.

`labwired test` writes `status: pass | fail | error` to result.json. That is
what the simulation saw. It cannot see whether the twin it ran was the design:
a KiCad import builds the twin out of the parts LabWired can model and reports
the rest as design-only. LabWired's compiler writes that report into
system.yaml as one line, `coverage: {...}` (JSON inside YAML, so this file
needs no YAML library), and this module turns it into the verdict every later
step uses.

Rule, mirrored from `ciRunVerdict` in @labwired/board-config (twin-coverage.ts):

    error > unproven > fail > pass

`unproven` applies only to an imported twin (one with a coverage record) and
means: design-only parts are still missing, or the run passed while asserting
nothing. An `error` run never happened, so coverage says nothing about it.

The record is read exactly as `parseCoverageFromSystemYaml` reads it: the same
line match (with JavaScript's line terminators) and the same all-or-nothing
schema check. A record that fails the check is no record, on both sides.
test_action_scripts.py feeds both implementations the same cases.
"""
import json
import os
import re
import sys

COVERAGE_SCHEMA = "labwired.twin-coverage.v1"
# `^coverage:[ \t]*(\{.*\})[ \t]*$` with the JavaScript `m` flag: `.` stops at,
# and `^`/`$` match beside, \n, \r, U+2028 and U+2029. Python's `re` only knows
# \n, so a CRLF manifest would otherwise carry a record TypeScript sees and
# this does not.
COVERAGE_LINE = re.compile(
    r"(?:\A|(?<=[\n\r\u2028\u2029]))coverage:[ \t]*(\{[^\n\r\u2028\u2029]*\})[ \t]*(?=[\n\r\u2028\u2029]|\Z)"
)
SYSTEM_LINE = re.compile(r"^[ \t]*system:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
# Not 3: the CLI already uses 0-3 (pass, assertion fail, config error, runtime
# error), and an incomplete twin must never read as a simulator crash.
EXIT_UNPROVEN = 4


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _reject_constant(name):
    # json.loads accepts NaN and Infinity; JSON.parse does not.
    raise ValueError(f"not JSON: {name}")


def _valid_part(entry):
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("ref"), str)
        and entry["ref"] != ""
        and isinstance(entry.get("value"), str)
        and isinstance(entry.get("reason"), str)
    )


def parse_coverage(system_yaml):
    """The coverage record in a system manifest, or None. Never raises.

    Same contract as the zod schema: schema literal, non-empty source_kind, and
    every design-only entry a {ref (non-empty), value, reason} of strings. One
    bad entry voids the record. Unknown keys are dropped."""
    if not isinstance(system_yaml, str):
        return None
    match = COVERAGE_LINE.search(system_yaml)
    if not match:
        return None
    try:
        record = json.loads(match.group(1), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("schema") != COVERAGE_SCHEMA:
        return None
    source_kind = record.get("source_kind")
    design_only = record.get("design_only")
    if not isinstance(source_kind, str) or source_kind == "" or not isinstance(design_only, list):
        return None
    if not all(_valid_part(entry) for entry in design_only):
        return None
    return {
        "schema": COVERAGE_SCHEMA,
        "source_kind": source_kind,
        "design_only": [{"ref": e["ref"], "value": e["value"], "reason": e["reason"]} for e in design_only],
    }


def _script_system(script_path):
    """`inputs.system` from the test script, resolved the way the CLI resolves
    it: absolute as written, otherwise relative to the script's directory."""
    try:
        with open(script_path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, ValueError):
        return None
    match = SYSTEM_LINE.search(text)
    if not match:
        return None
    value = match.group(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    else:
        value = re.sub(r"[ \t]+#.*$", "", value)
    if not value.strip():
        return None
    return value if os.path.isabs(value) else os.path.join(os.path.dirname(script_path), value)


def system_manifest_path(result, script_path=None, system_input=None):
    """The manifest the run loaded. result.json's `config.system` is the path
    the CLI opened, so it wins; the `system` input (which overrides the
    script) and the script's own `inputs.system` cover a result.json without
    it. None when the run had no manifest (an `inputs.chip` run)."""
    config = result.get("config") if isinstance(result, dict) else None
    candidates = []
    if isinstance(config, dict) and isinstance(config.get("system"), str) and config["system"]:
        candidates.append(config["system"])
    if system_input:
        candidates.append(system_input)
    elif script_path:
        candidates.append(_script_system(script_path))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def read_coverage(result, script_path=None, system_input=None):
    path = system_manifest_path(result, script_path, system_input)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_coverage(fh.read())
    except (OSError, ValueError):
        return None


def assertion_count(result):
    assertions = result.get("assertions")
    return sum(1 for a in assertions if isinstance(a, dict)) if isinstance(assertions, list) else 0


def run_verdict(status, assertions, coverage):
    """The one rule. Returns {verdict, reasons, design_only}."""
    status = status if status in ("pass", "fail") else "error"
    design_only = coverage["design_only"] if coverage else []
    if status == "error" or coverage is None:
        return {"verdict": status, "reasons": [], "design_only": design_only}
    reasons = []
    if design_only:
        reasons.append("design_only_parts")
    if status == "pass" and assertions == 0:
        reasons.append("nothing_asserted")
    return {"verdict": "unproven" if reasons else status, "reasons": reasons, "design_only": design_only}


def verdict_of(result, script_path=None, system_input=None):
    """The verdict for a parsed result.json and the inputs the run was given."""
    coverage = read_coverage(result, script_path, system_input)
    return run_verdict(result.get("status"), assertion_count(result), coverage)


def verdict_for_run(output_dir, script_path=None, system_input=None):
    return verdict_of(read_json(os.path.join(output_dir, "result.json")), script_path, system_input)


def verdict_from_env(result):
    """The verdict for `result` with the script and system inputs the action
    passes every step as LABWIRED_SCRIPT and LABWIRED_SYSTEM."""
    return verdict_of(result, os.environ.get("LABWIRED_SCRIPT") or None, os.environ.get("LABWIRED_SYSTEM") or None)


def part_label(part):
    return f"{part['ref']} ({part['value']})" if part["value"] else part["ref"]


def explain(verdict):
    """One line naming why a run is unproven; empty otherwise."""
    if verdict["verdict"] != "unproven":
        return ""
    parts = []
    if "design_only_parts" in verdict["reasons"]:
        refs = ", ".join(part_label(p) for p in verdict["design_only"])
        parts.append(f"design-only parts not on the twin: {refs}")
    if "nothing_asserted" in verdict["reasons"]:
        parts.append("the run asserted nothing")
    return "; ".join(parts)


def command_text(text):
    """Text safe to put in a `::error::` workflow command. Part values come from
    the design, which a pull request author writes; an unescaped newline would
    end the command and let the next line be read as a new one."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _exit_code_from_env():
    text = os.environ.get("LABWIRED_EXIT_CODE", "").strip()
    try:
        return int(text)
    except ValueError:
        # The run step always records a code. Its absence means the run step
        # did not finish, which is never a pass.
        print(f"::error::LabWired run step recorded no exit code ({text!r}).")
        return 1


def main():
    """Final step of the action.

    An unproven run exits 4 whatever the CLI returned, so an incomplete twin is
    never a green check. `allow_unproven: true` hands the decision back to the
    CLI's own exit code: a passing run goes green, a failing one stays red. Any
    other verdict exits with the CLI's code.
    """
    code = _exit_code_from_env()
    verdict = verdict_from_env(read_json(os.path.join(os.environ.get("LABWIRED_OUTPUT_DIR", "out/artifacts"), "result.json")))
    if verdict["verdict"] == "unproven":
        why = command_text(explain(verdict))
        allowed = os.environ.get("LABWIRED_ALLOW_UNPROVEN", "false").strip().lower() == "true"
        if not allowed:
            fixes = []
            if "design_only_parts" in verdict["reasons"]:
                fixes.append("place the missing parts on the twin (or wait for the catalog to model them)")
            if "nothing_asserted" in verdict["reasons"]:
                fixes.append("add an assertion")
            fixes.append("set allow_unproven: true to accept an unproven run")
            print(f"::error::LabWired run is UNPROVEN: {why}. To fix it, {', or '.join(fixes)}.")
            return EXIT_UNPROVEN
        print(f"::warning::LabWired run is UNPROVEN ({why}). allow_unproven is set, so the CLI's exit code decides.")
    if code != 0:
        print(f"::error::LabWired tests failed (exit code {code}).")
    return code


if __name__ == "__main__":
    sys.exit(main())
