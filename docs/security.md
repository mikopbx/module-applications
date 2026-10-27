# Security of the checks

The applications and the modules come from anyone. This page says what each part of the checks may do, what it
trusts, and how that was tested. The threat model of each workflow is also written at the top of the workflow file.

## Three trust zones (TASK.md §4.4)

| Zone | Runs | Has | Built here |
|---|---|---|---|
| Tests (`test.yaml`, `tools/check`) | the module's PHP, inside a MikoPBX container | nothing: `contents: read`, no secrets, no cache | yes |
| AI review (`ai-review.yaml`) | MIKO's analyser; the module's files and logs only as data, three read-only tools | the Anthropic key only, in the `ai-review` environment (a person approves each run) | yes ([docs/ai-review.md](ai-review.md)) |
| Registration (`register.yaml`) | MIKO's code after merge, on the protected branch | the licence-server key only | no (LIC-614) |

No zone hands its key to another, and none runs the module next to a key.

## What protects what

| Threat | Defence | Where |
|---|---|---|
| A pull request edits the check that judges it | `pull_request_target`: workflows, tools, rules and config come from the base branch | all workflows |
| A pull request smuggles files beside the application | only `applications/<id>/application.yaml`, as a regular file from git's object store; anything else fails before any of it is read (APP-006, APP-003) | `take_application.sh`, `validate.py --pr-paths` |
| The application names a moving target | `commit` must be a full SHA; the fetch must return that SHA | `validate.py` APP-004/APP-007, `check_install.fetch` |
| The module's repository runs code on the runner | nothing is checked out; files are read from git objects; the ZIP is built by our code; hooks are off | `check_install.package` |
| The module reads the runner's secrets | the jobs have no secrets; the container gets no runner environment, volume or Docker socket | `test.yaml`, `Pbx.start` |
| The module reaches the network, the metadata service or the runner | Docker `--internal` network; iptables drops traffic from the container's bridge to the runner | `Pbx.start`, `Pbx.firewall` |
| The module exhausts the runner | `--cpus 2 --memory 3g --pids-limit 2048`, step and job timeouts | `config/mikopbx.yaml`, `test.yaml` |
| The module's output issues workflow commands | `::stop-commands::<random>` around everything that prints its output | `test.yaml`, `validate.yaml` |
| External text injects Markdown or mentions into the triage comment | every external string escaped; the comment job only posts | `triage.py`, `validate.yaml` |
| A forged result is presented as a pass | the result artifact is evidence only; the next stages must check the run, the pull request and the commit it names | `test.yaml` (documented), LIC-612/614 |
| The image changes under the same tag | the image is pulled by digest from MIKO's config; a new digest arrives only by a reviewed pull request after the control modules pass on it, and makes earlier results stale | `config/mikopbx.yaml`, `image-update.yaml` |

## How it was tested (x86_64, 2026-10-27)

- `ModuleAppProbe` (tests/modules) reports from inside the container: secret-like environment variable names in any
  process, runner files and mounts, and TCP connections to the metadata address, the Docker host, the internet,
  private ranges and the bridge's gateway. Before the iptables rule, a server on the host listening on 0.0.0.0 was
  reachable through the bridge gateway; with the rule, every connection fails and every list is empty.
  `tools/check --expect probe_empty` turns any finding into a failure.
- `ModuleAppBroken` fails the run (a fatal error in its Conf class surfaces when it is disabled).
- The fixtures in `tests/fixtures/bad/` show that a pull request reaching outside its directory, a short SHA, a
  missing commit, a GUID conflict and an incompatible `module.json` are refused before anything runs.

Still to confirm on GitHub runners (x86_64 and arm64): the same probe results, and that a pull request editing a
workflow is refused by the base branch's workflow.

## Not done here

- The approval of an exact commit, its reset on push and the protected branch (LIC-612, after the AI stage).
- The check of `support_email` against the publisher card and of module-id ownership needs the licence server API
  (LIC-613); `tools/publisher_registry.py` answers "not checked" until then, and that never counts as passed.
