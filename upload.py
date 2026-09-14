#!/usr/bin/env python3
"""Upload a completed LabWired run to the hosted API.

Best-effort by contract: any failure here logs a warning and exits 0, because
a maintainer's build must never go red over our reporting service.
"""
import base64
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request

import verdict

MAX_BUNDLE_BYTES = 8 * 1024 * 1024


def out(key, value):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write(f"{key}={value}\n")


def warn(message):
    print(f"::warning::{message}")


def read_result(output_dir):
    """Parse result.json written by `labwired test`.

    Real schema (verified against labwired-core crates/cli/src/artifacts.rs):
    top-level keys are result_schema_version, status, steps_executed, cycles,
    instructions, stop_reason, stop_reason_details, limits, message (optional),
    assertions, cpu_state, firmware_hash, config, and optional inspect /
    fidelity / logic_edges. There is no "tests" array, no duration_ms, no
    board/target. `assertions` is a list of {"assertion": {...}, "passed": bool}.
    Only ever return a dict — a malformed or non-object document must not crash
    every later .get() call.
    """
    path = os.path.join(output_dir, "result.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def build_bundle(output_dir, extra_paths):
    """tar.gz of the artifacts dir plus the firmware/system/script inputs.

    Never allowed to raise: a broken symlink, permission error, or file
    removed mid-run must degrade to "upload metadata only", not fail the
    build.
    """
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if os.path.isdir(output_dir):
                tar.add(output_dir, arcname="artifacts")
            seen = set()
            for path in extra_paths:
                if path and os.path.isfile(path) and os.path.realpath(path) not in seen:
                    seen.add(os.path.realpath(path))
                    tar.add(path, arcname=os.path.join("inputs", os.path.basename(path)))
        return buf.getvalue()
    except (OSError, tarfile.TarError) as exc:
        warn(f"Could not build the LabWired artifact bundle ({exc}) — uploading metadata only.")
        return b""


def run():
    # Gate client-side: a private repo must never transmit anything, not even
    # metadata. The server also rejects private repos with 403, but only
    # after the (possibly multi-megabyte) firmware bundle has already crossed
    # the wire — this check makes that a defence-in-depth backstop, not the
    # only guard.
    if os.environ.get("LABWIRED_REPO_PRIVATE", "false").strip().lower() == "true":
        print("LabWired: repository is private — skipping upload.")
        out("report_url", "")
        return 0

    token = os.environ.get("LABWIRED_OIDC_TOKEN", "").strip()
    if not token:
        out("report_url", "")
        return 0

    output_dir = os.environ.get("LABWIRED_OUTPUT_DIR", "out/artifacts")
    api_url = os.environ.get("LABWIRED_API_URL", "https://api.labwired.com").rstrip("/")
    result = read_result(output_dir)

    assertions = result.get("assertions")
    assertions = assertions if isinstance(assertions, list) else []
    passed = sum(1 for a in assertions if isinstance(a, dict) and a.get("passed") is True)
    failed = sum(1 for a in assertions if isinstance(a, dict) and a.get("passed") is False)

    # The verdict, not the CLI's raw status: a run on an imported twin that is
    # missing design parts is stored as `unproven`, never as a pass. Anything
    # the CLI did not report as pass or fail is `error`, as before.
    status = verdict.verdict_from_env(result)["verdict"]

    payload = {
        "status": status,
        "tests_passed": passed,
        "tests_failed": failed,
        # No duration/board/description: the CLI reports cycles and
        # instructions, not wall-clock time, and the board lives in the system
        # manifest rather than result.json. The server dropped those columns
        # rather than store perpetual nulls.
        "cli_version": os.environ.get("LABWIRED_CLI_VERSION") or None,
        "gallery": os.environ.get("LABWIRED_GALLERY", "true") != "false",
    }

    gallery_opt_out = not payload["gallery"]
    if gallery_opt_out:
        # gallery: false means "do not send the bundle at all", not merely
        # "do not list it" — the bundle would still be retrievable
        # unauthenticated by hash from the public blob endpoint otherwise.
        print("LabWired: gallery is false — sending metadata only, no bundle.")
    else:
        bundle = build_bundle(
            output_dir,
            [
                os.environ.get("LABWIRED_FIRMWARE", ""),
                os.environ.get("LABWIRED_SYSTEM", ""),
                os.environ.get("LABWIRED_SCRIPT", ""),
                # The manifest the run loaded, also when the script named it
                # rather than the `system` input. Its coverage line is the only
                # record of which design parts an unproven run was missing.
                verdict.system_manifest_path(
                    result,
                    os.environ.get("LABWIRED_SCRIPT") or None,
                    os.environ.get("LABWIRED_SYSTEM") or None,
                )
                or "",
            ],
        )
        if bundle and len(bundle) <= MAX_BUNDLE_BYTES:
            payload["bundle_base64"] = base64.b64encode(bundle).decode("ascii")
        elif bundle:
            warn(f"LabWired bundle is {len(bundle) // (1024 * 1024)} MB, over the 8 MB cap — uploading metadata only.")

    request = urllib.request.Request(
        f"{api_url}/v1/ci/runs",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            # Without an explicit User-Agent, urllib sends "Python-urllib/x.y",
            # which Cloudflare's WAF blocks as a bot user agent (error code
            # 1010) before the request ever reaches the Worker. This was the
            # cause of every real upload silently 403'ing.
            "User-Agent": "labwired-firmware-test/upload.py",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
        warn(f"LabWired upload failed ({exc}). Test results are unaffected.")
        out("report_url", "")
        return 0

    out("report_url", body.get("report_url", "") if isinstance(body, dict) else "")
    return 0


def main():
    # Best-effort by contract: this step must never fail a maintainer's build.
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see contract above
        warn(f"LabWired upload step hit an unexpected error ({exc}). Test results are unaffected.")
        try:
            out("report_url", "")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
