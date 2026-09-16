#!/usr/bin/env python3
"""Report a completed LabWired run to Azure DevOps Pipelines.

Two modes with opposite contracts:

- report (default): evidence only, best effort. Writes labwired-summary.md,
  uploads it with ##vso[task.uploadsummary], tags the build, and upserts one PR
  thread comment. Never fails the build; every failure is a warning.
- --verdict: the gate, no network. Exits the LabWired CLI's code, or 4 when the
  run is unproven and allow_unproven is false. An exit code that was never
  recorded is never green.

The verdict rule lives in verdict.py and the Markdown body in comment.render(),
both shared with the GitHub action, so both CI systems say the same thing about
the same run.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# HERE first: a consumer who vendors report_azure.py, verdict.py and comment.py
# into one directory gets them from there. ROOT covers this repository, where
# the two siblings live at the root and the reporter in azure/.
for _candidate in (ROOT, HERE):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import comment  # noqa: E402
import verdict  # noqa: E402

MARKER = comment.MARKER
UART_TAIL_LINES = comment.UART_TAIL_LINES
EXIT_UNPROVEN = verdict.EXIT_UNPROVEN
API_VERSION = "7.1"


def output_dir():
    return os.environ.get("LABWIRED_OUTPUT_DIR", "out/artifacts")


def vso(command):
    print(f"##vso[{command}]")


def warn(message):
    vso(f"task.logissue type=warning]{verdict.command_text(message)}")


def fail(message):
    vso(f"task.logissue type=error]{verdict.command_text(message)}")


def allow_unproven():
    return os.environ.get("LABWIRED_ALLOW_UNPROVEN", "false").strip().lower() == "true"


def read_result():
    return comment.read_json(os.path.join(output_dir(), "result.json"))


def read_exit_code():
    """The code the run step recorded, or None when it recorded nothing."""
    path = os.environ.get("LABWIRED_EXIT_CODE_FILE") or os.path.join(output_dir(), "exit_code")
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def verdict_main():
    """The gate: never green on a run that did not happen or proves nothing."""
    code = read_exit_code()
    if code is None:
        fail("LabWired run step recorded no exit code: the run did not finish, which is never a pass.")
        return 1
    run_verdict = verdict.verdict_from_env(read_result())
    if run_verdict["verdict"] == "unproven":
        why = verdict.explain(run_verdict)
        if not allow_unproven():
            fixes = []
            if "design_only_parts" in run_verdict["reasons"]:
                fixes.append("place the missing parts on the twin (or wait for the catalog to model them)")
            if "nothing_asserted" in run_verdict["reasons"]:
                fixes.append("add an assertion")
            fixes.append("set allow_unproven to true to accept an unproven run")
            fail(f"LabWired run is UNPROVEN: {why}. To fix it, {', or '.join(fixes)}.")
            return EXIT_UNPROVEN
        warn(f"LabWired run is UNPROVEN ({why}). allow_unproven is set, so the CLI's exit code decides.")
    if code != 0:
        fail(f"LabWired tests failed (exit code {code}).")
    return code


def write_summary(result, run_verdict):
    """The Markdown report, uploaded as the build summary and kept as an
    artifact. Same renderer as the GitHub action; no hosted-report footer,
    because Azure DevOps cannot mint a GitHub OIDC token for the upload."""
    body = comment.render(
        result,
        comment.read_tail(os.path.join(output_dir(), "uart.log"), UART_TAIL_LINES),
        "",
        run_verdict,
        allow_unproven(),
    )
    os.makedirs(output_dir(), exist_ok=True)
    path = os.path.join(output_dir(), "labwired-summary.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    vso(f"task.uploadsummary]{os.path.abspath(path)}")
    return body


def add_build_tag(status):
    vso(f"build.addbuildtag]labwired-{status}")


def annotate(run_verdict):
    """Only warnings: the error annotation belongs to the verdict step, which
    is where the build actually reddens."""
    if run_verdict["verdict"] != "unproven":
        return
    if allow_unproven():
        warn(
            f"LabWired run is UNPROVEN ({verdict.explain(run_verdict)}). "
            "allow_unproven is set, so the CLI's exit code decides."
        )


def report_main():
    result = read_result()
    run_verdict = verdict.verdict_from_env(result)
    write_summary(result, run_verdict)
    add_build_tag(comment.display_status(result, run_verdict))
    annotate(run_verdict)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--verdict" in argv:
        # The gate must never break silently: an unexpected error here is a
        # failed run, not a green one.
        try:
            return verdict_main()
        except Exception as exc:  # noqa: BLE001 - see contract above
            fail(f"LabWired verdict step hit an unexpected error ({exc}).")
            return 1
    # Best-effort by contract: reporting must never fail the build.
    try:
        return report_main()
    except Exception as exc:  # noqa: BLE001 - see contract above
        warn(f"LabWired report step hit an unexpected error ({exc}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
