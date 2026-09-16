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
import base64
import io
import json
import os
import re
import sys
import tarfile
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
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

# dev.azure.com/{org} or the legacy {org}.visualstudio.com. Self-hosted Azure
# DevOps Server collections are not verifiable server-side and are not sent.
COLLECTION_RE = re.compile(r"^https://(?:dev\.azure\.com/([A-Za-z0-9][A-Za-z0-9-]{0,99})|([A-Za-z0-9][A-Za-z0-9-]{0,99})\.visualstudio\.com)$")


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


def write_summary(result, run_verdict, report_url=""):
    """The Markdown report, uploaded as the build summary and kept as an
    artifact. Same renderer as the GitHub action; the footer appears only when
    the hosted report was uploaded."""
    body = comment.render(
        result,
        comment.read_tail(os.path.join(output_dir(), "uart.log"), UART_TAIL_LINES),
        report_url,
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


def comment_context():
    """Everything the PR thread API needs, or None when this is not a pull
    request, commenting is off, or the access token was not mapped in.

    Shapes follow the Azure DevOps Git Pull Request Threads REST API 7.1
    (create thread / update comment), not a guess: see the tests for the
    request and response fields this relies on."""
    if os.environ.get("LABWIRED_PR_COMMENT", "true").strip().lower() == "false":
        return None
    pr_id = os.environ.get("SYSTEM_PULLREQUEST_PULLREQUESTID", "").strip()
    token = os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    repo_id = os.environ.get("BUILD_REPOSITORY_ID", "").strip()
    collection = os.environ.get("SYSTEM_COLLECTIONURI", "").strip().rstrip("/")
    project = os.environ.get("SYSTEM_TEAMPROJECT", "").strip()
    if not pr_id.isdigit() or not token or not repo_id or not collection or not project:
        return None
    base = (
        f"{collection}/{urllib.parse.quote(project, safe='')}/_apis/git/repositories/"
        f"{urllib.parse.quote(repo_id, safe='')}/pullRequests/{pr_id}"
    )
    return {"token": token, "base": base}


def ado_api(method, url, token, payload=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "labwired-firmware-test/report_azure.py",
        },
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def existing_comment(threads):
    """(thread_id, comment_id) of the comment carrying our marker, or None."""
    if not isinstance(threads, dict):
        return None
    for thread in threads.get("value") or []:
        if not isinstance(thread, dict):
            continue
        for entry in thread.get("comments") or []:
            if isinstance(entry, dict) and MARKER in (entry.get("content") or ""):
                return thread.get("id"), entry.get("id")
    return None


def upsert_thread(body, context):
    threads_url = f"{context['base']}/threads?api-version={API_VERSION}"
    found = existing_comment(ado_api("GET", threads_url, context["token"]))
    if found and found[0] is not None and found[1] is not None:
        thread_id, comment_id = found
        ado_api(
            "PATCH",
            f"{context['base']}/threads/{thread_id}/comments/{comment_id}?api-version={API_VERSION}",
            context["token"],
            {"content": body},
        )
    else:
        ado_api(
            "POST",
            threads_url,
            context["token"],
            {
                "comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
                "status": 1,
            },
        )


def post_pr_comment(body):
    context = comment_context()
    if not context:
        return
    try:
        upsert_thread(body, context)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
        warn(f"Could not post the LabWired PR comment ({exc}).")


def gallery_enabled():
    return os.environ.get("LABWIRED_GALLERY", "true").strip().lower() != "false"


def azure_env():
    """The Azure DevOps identity the ingest endpoint needs, or None when this
    is not an Azure pipeline (or the token was not mapped into the step)."""
    collection = os.environ.get("SYSTEM_COLLECTIONURI", "").strip().rstrip("/")
    project = os.environ.get("SYSTEM_TEAMPROJECT", "").strip()
    build_id = os.environ.get("BUILD_BUILDID", "").strip()
    token = os.environ.get("SYSTEM_ACCESSTOKEN", "").strip()
    match = COLLECTION_RE.match(collection)
    if not match or not project or not build_id.isdigit():
        return None
    if not token:
        warn(
            "No System.AccessToken available, so the hosted LabWired report was skipped. "
            "Map it into this step as SYSTEM_ACCESSTOKEN: $(System.AccessToken)."
        )
        return None
    return {
        "org": match.group(1) or match.group(2),
        "project": project,
        "build_id": build_id,
        "token": token,
    }


def build_bundle():
    """tar.gz of the artifacts dir plus the firmware/system/script inputs.
    Never raises: a broken symlink or permission error degrades to metadata
    only, exactly like the GitHub action's upload.py."""
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if os.path.isdir(output_dir()):
                tar.add(output_dir(), arcname="artifacts")
            seen = set()
            for path in (
                os.environ.get("LABWIRED_FIRMWARE", ""),
                os.environ.get("LABWIRED_SYSTEM", ""),
                os.environ.get("LABWIRED_SCRIPT", ""),
                # The manifest the run loaded, also when the script named it
                # rather than the system input: its coverage line is the only
                # record of which design parts an unproven run was missing.
                verdict.system_manifest_path(
                    read_result(),
                    os.environ.get("LABWIRED_SCRIPT") or None,
                    os.environ.get("LABWIRED_SYSTEM") or None,
                )
                or "",
            ):
                if path and os.path.isfile(path) and os.path.realpath(path) not in seen:
                    seen.add(os.path.realpath(path))
                    tar.add(path, arcname=os.path.join("inputs", os.path.basename(path)))
        return buf.getvalue()
    except (OSError, tarfile.TarError) as exc:
        warn(f"Could not build the LabWired artifact bundle ({exc}); uploading metadata only.")
        return b""


def upload_run(result, run_verdict):
    """POST the run to the hosted ingest, returning its report URL (or "").

    Best-effort by contract. Identity is the pipeline's own token plus the
    build id: the server asks Azure what that build was, so the payload here
    carries evidence only. Private projects are rejected server-side before the
    body is read."""
    identity = azure_env()
    if not identity:
        return ""
    api_url = os.environ.get("LABWIRED_API_URL", "https://api.labwired.com").rstrip("/")
    assertions = result.get("assertions")
    assertions = assertions if isinstance(assertions, list) else []
    payload = {
        # The verdict, not the CLI's raw status: an unproven run is stored as
        # unproven, never as a pass.
        "status": run_verdict["verdict"],
        "tests_passed": sum(1 for a in assertions if isinstance(a, dict) and a.get("passed") is True),
        "tests_failed": sum(1 for a in assertions if isinstance(a, dict) and a.get("passed") is False),
        "cli_version": os.environ.get("LABWIRED_CLI_VERSION") or None,
        "gallery": gallery_enabled(),
    }
    if payload["gallery"]:
        bundle = build_bundle()
        if bundle and len(bundle) <= MAX_BUNDLE_BYTES:
            payload["bundle_base64"] = base64.b64encode(bundle).decode("ascii")
        elif bundle:
            warn("LabWired bundle is over the 8 MB cap; uploading metadata only.")
    request = urllib.request.Request(
        f"{api_url}/v1/ci/runs",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {identity['token']}",
            "X-LabWired-CI-Provider": "azure",
            "X-LabWired-Azure-Org": identity["org"],
            "X-LabWired-Azure-Project": identity["project"],
            "X-LabWired-Azure-Build-Id": identity["build_id"],
            "User-Agent": "labwired-firmware-test/report_azure.py",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
        warn(f"LabWired upload failed ({exc}). Test results are unaffected.")
        return ""
    return body.get("report_url", "") if isinstance(body, dict) else ""


def report_main():
    result = read_result()
    run_verdict = verdict.verdict_from_env(result)
    report_url = upload_run(result, run_verdict)
    body = write_summary(result, run_verdict, report_url)
    add_build_tag(comment.display_status(result, run_verdict))
    annotate(run_verdict)
    post_pr_comment(body)
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
