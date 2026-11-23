# Release workflow

`.github/workflows/release.yaml` builds, checks and reviews one exact commit of a module that is already admitted
(`applications/<module_id>/application.yaml` is on the default branch). It is started with `workflow_dispatch` by the
server after admission. It publishes nothing: it exposes the ZIP and the results of the run, and the server decides.

## Inputs

| Input | Rule |
|---|---|
| `job_id` | 1-64 characters: letters, digits, `.`, `_`, `-`; starts with a letter or digit |
| `module_id` | `Module` + letters and digits; `applications/<module_id>/application.yaml` must exist on the default branch |
| `repo` | `https://github.com/<owner>/<name>`, no `.git`, no trailing slash; must equal `repo` of the admitted application |
| `sha` | exactly 40 lowercase hex characters (no branch, tag or short SHA) |
| `version` | digits and dots (`1.2`, `1.2.3`, `1.2.3.4`); must equal `version` in `module.json` of that commit |
| `architectures` | comma separated, default `x86_64,arm64`; each must be declared by the admitted application |

The workflow runs only from the default branch. The inputs are checked in the first job by `tools/release.py`
and reach scripts only through `env:`.

## Jobs

1. `intake`: checks the inputs and the admission; TODO(LIC-615) stub "verify the job with the server".
2. `package`: `tools/package <repo> <sha>` builds the ZIP (reproducible, see `tools/package`), checks `module.json`
   (`moduleUniqueID`, `version`) and computes the sha256. A `%ModuleVersion%` placeholder in `module.json` becomes
   `0.0.0-<commit>`, so such a commit cannot be released: set the real version in `module.json`.
3. `check` (one job per architecture): `tools/check` on the application with `commit` = `sha`, in the MikoPBX image
   pinned in `config/mikopbx.yaml`, same isolation as `test.yaml`. The ZIP that `tools/check` builds must have the same sha256 as the
   one from `package`. There is no update test (no previous version is given).
4. `ai-prepare` and `ai-review`: the same analyser, policy and Environment (`ai-review`) as `ai-review.yaml`.
   Only after the checks passed. The per-author daily limit and the report cache of the pull request flow do not apply.
5. `result`: assembles `release-result.json`, uploads the artifact, TODO(LIC-615) stub "hand the result to the server".
   The run fails if a check or the AI review did not pass.

`test.yaml` is not reused as a workflow (its jobs are tied to pull requests). The check step is copied from it;
`tests/test_release.py` checks that the isolation lines stay the same.

## Artifacts

All kept `RETENTION_DAYS` (one constant at the top of the workflow, 7 days, as in `test.yaml`).

- `release-<job_id>`: `<module_id>.zip`, `release-result.json`, `ai-report.json` (when the review ran).
- `zip`, `check-<arch>`, `ai-inputs`, `ai-report`: the stages' intermediate artifacts.

`release-result.json`: `job_id`, `module_id`, `repo`, `sha`, `version`, `zip` (name, sha256, bytes, version),
`architectures` (outcome, runs with MikoPBX version, image digest and steps), `ai_review`, `rules_version`,
`schema_version_application`, `mikopbx_images` (from `config/mikopbx.yaml`), `mikopbx_image_digests_used`,
`checks_passed`, `ai_review_passed`, `passed`, `workflow_run` (run id, attempt, URL, workflow commit).
It is evidence, not a decision: whoever reads it must check the run that produced it.

## Not defined yet: TODO(LIC-615)

- Verify the `job_id` with the server before anything is built (`intake`).
- Hand the result to the server (`result`), or define how the server fetches the artifact of the run.
- Whether the server limits how often it starts a release (the AI review's per-run cost limits apply).
- Whether an update test from the previously released version is wanted (`tools/check --previous-commit`).

No endpoint, URL or secret for the server exists in this repository.
