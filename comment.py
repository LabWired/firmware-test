#!/usr/bin/env python3
"""Write the job summary and upsert a single PR comment for this run."""
import json
import os
import re
import sys
import urllib.error
import urllib.request

import verdict

MARKER = "<!-- labwired-report -->"
UART_TAIL_LINES = 20


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_tail(path, lines):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""


def _assertion_label(assertion):
    """assertion is TestAssertion, a serde-untagged enum: an object with one
    key naming the kind (uart_contains, uart_regex, expected_stop_reason,
    memory_value, uds_tester)."""
    if isinstance(assertion, dict) and assertion:
        return next(iter(assertion.keys()))
    return "assertion"


def _assertion_summary(assertion, limit=120):
    if not isinstance(assertion, dict) or not assertion:
        return ""
    label = next(iter(assertion.keys()))
    value = assertion[label]
    text = json.dumps(value, separators=(",", ":")) if not isinstance(value, str) else value
    text = text.replace("\n", " ").replace("|", "\\|")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _cell(text, limit=120):
    """Table-cell text from the design (a part's ref, value, or the importer's
    reason). A schematic in a pull request is the author's to write, and this
    comment is posted with the maintainer's token, so markdown and HTML in it
    are escaped rather than rendered."""
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return re.sub(r"([\\`*_\[\]<>|~&!#])", r"\\\1", text)


def display_status(result, run_verdict=None):
    """What the header and the `status` output say: the verdict when there is a
    result to judge, `unknown` when result.json is missing or has no status."""
    if "status" not in result:
        return "unknown"
    return run_verdict["verdict"] if run_verdict else result["status"]


def allow_unproven_from_env():
    return os.environ.get("LABWIRED_ALLOW_UNPROVEN", "false").strip().lower() == "true"


def _unproven_section(run_verdict, allow_unproven):
    consequence = (
        "`allow_unproven` is set, so this alone does not fail the job."
        if allow_unproven
        else "The job fails on it; set `allow_unproven: true` to accept an unproven run."
    )
    body = [
        "The simulation finished, but the twin it ran on is not the design, so this run proves "
        f"nothing about the design. {consequence}",
        "",
    ]
    if "design_only_parts" in run_verdict["reasons"]:
        body += [
            "| Design-only part | Value | Why it is not on the twin |",
            "| --- | --- | --- |",
        ]
        for part in run_verdict["design_only"]:
            body.append(f"| {_cell(part['ref'])} | {_cell(part['value']) or '—'} | {_cell(part['reason'])} |")
        body.append("")
    if "nothing_asserted" in run_verdict["reasons"]:
        body += ["The run passed without asserting anything, so nothing was asked of the imported circuit.", ""]
    return body


def render(result, uart_tail, report_url, run_verdict=None, allow_unproven=False):
    status = display_status(result, run_verdict)
    icon = {"pass": "✅", "fail": "❌", "error": "⚠️", "unproven": "🟡"}.get(status, "❔")
    body = [MARKER, f"### {icon} LabWired simulation — {status}", ""]
    if status == "unproven":
        body += _unproven_section(run_verdict, allow_unproven)

    assertions = result.get("assertions")
    assertions = assertions if isinstance(assertions, list) else []
    if assertions:
        body += ["| Assertion | Result | Summary |", "| --- | --- | --- |"]
        for entry in assertions:
            if not isinstance(entry, dict):
                continue
            assertion = entry.get("assertion")
            passed = entry.get("passed")
            mark = "✅" if passed is True else "❌"
            body.append(f"| {_assertion_label(assertion)} | {mark} | {_assertion_summary(assertion)} |")
        body.append("")

    if uart_tail.strip():
        # Firmware can print anything, including a bare ``` — a plain triple-
        # backtick fence would let that break out of the code block and
        # inject arbitrary markdown into a comment posted with the
        # maintainer's GITHUB_TOKEN. A longer fence (four backticks) cannot be
        # closed by a three-backtick line inside the captured output.
        fence = "````"
        body += ["<details><summary>UART output (tail)</summary>", "", fence, uart_tail.rstrip(), fence, "", "</details>", ""]

    if report_url:
        body.append(f"[Full report]({report_url}) · listed in the [LabWired gallery](https://app.labwired.com/ci) — set `gallery: false` to opt out.")
    return "\n".join(body)


def api(method, url, token, payload=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def find_existing(base, token, per_page=100):
    """Paginate issue comments until the marker is found or a short page ends
    the listing. Without this, a PR with >100 prior comments would post a
    duplicate instead of editing the existing one."""
    page = 1
    while True:
        existing = api("GET", f"{base}?per_page={per_page}&page={page}", token)
        if not isinstance(existing, list):
            return None
        mine = next((c for c in existing if isinstance(c, dict) and MARKER in (c.get("body") or "")), None)
        if mine:
            return mine
        if len(existing) < per_page:
            return None
        page += 1


def upsert_comment(body):
    token = os.environ.get("GITHUB_TOKEN", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not event_path or not repo:
        return
    event = read_json(event_path)
    number = (event.get("pull_request") or {}).get("number")
    if not number:
        return

    base = f"https://api.github.com/repos/{repo}/issues/{number}/comments"
    try:
        mine = find_existing(base, token)
        if mine:
            api("PATCH", f"https://api.github.com/repos/{repo}/issues/comments/{mine['id']}", token, {"body": body})
        else:
            api("POST", base, token, {"body": body})
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
        print(f"::warning::Could not post the LabWired PR comment ({exc}).")


def run():
    output_dir = os.environ.get("LABWIRED_OUTPUT_DIR", "out/artifacts")
    result = read_json(os.path.join(output_dir, "result.json"))
    uart_tail = read_tail(os.path.join(output_dir, "uart.log"), UART_TAIL_LINES)
    run_verdict = verdict.verdict_from_env(result)
    body = render(
        result,
        uart_tail,
        os.environ.get("LABWIRED_REPORT_URL", ""),
        run_verdict,
        allow_unproven_from_env(),
    )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")

    status_out = display_status(result, run_verdict)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"status={status_out}\n")

    if os.environ.get("LABWIRED_COMMENT", "true") != "false":
        upsert_comment(body)
    return 0


def main():
    # Best-effort by contract: this step must never fail a maintainer's build.
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see contract above
        print(f"::warning::LabWired comment step hit an unexpected error ({exc}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
