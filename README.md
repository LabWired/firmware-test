# LabWired for GitHub Actions

Run firmware tests in CI against a simulated MCU. No hardware, no toolchain, no
self-hosted runner.

```yaml
name: firmware tests
on: [push, pull_request]

jobs:
  labwired:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write        # hosted report
      pull-requests: write   # results comment
    steps:
      - uses: actions/checkout@v4
      - uses: labwired/firmware-test@v1
        with:
          script: tests/labwired.yml
```

That's it — no API key and no signup. The simulation runs on your own GitHub
runner; LabWired hosts the report.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `script` | *(required)* | Path to your LabWired test script YAML. |
| `firmware` | `""` | Firmware ELF, if not set inside the script. |
| `system` | `""` | System manifest YAML, if not set inside the script. |
| `output_dir` | `out/artifacts` | Where `result.json`, `uart.log` and `junit.xml` land. |
| `version` | `v0.18.0` | LabWired CLI release tag. |
| `gallery` | `true` | List public repos at https://app.labwired.com/ci. Set `false` to stay unlisted. |
| `comment` | `true` | Post a results comment on pull requests. |

## Outputs

`status` (`pass`/`fail`/`error`), `report_url`, `artifacts_dir`.

## Privacy

Private repositories are never uploaded: the action checks
`github.event.repository.private` before sending anything, so no request
leaves the runner for a private repo (the API also rejects private repos
server-side, as a backstop).

Public repositories are listed in the public gallery by default and their
result metadata plus firmware artifact bundle are uploaded. Set
`gallery: false` to opt out — this does not merely unlist the run, it changes
what is sent: only run metadata (status, test counts, board, timings) is
uploaded, and the firmware artifact bundle is never sent, because a stored
bundle is retrievable unauthenticated by hash once it exists. Email
andrii@labwired.com to have an existing gallery listing removed.

Uploading is best-effort — if the LabWired API is unreachable, your build still
passes or fails on the test result alone.

## Licence

MIT.
