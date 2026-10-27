# Setting up the repository

What an administrator of `mikopbx/module-applications` does once, in this order. Everything else lives in the
repository and arrives by pull request. No secret value is ever written into a file, an issue or a log.

## 1. Before the first import

1. **Default branch `main`.** The documentation, `terms.yaml` and the ruleset assume `main`. In an empty repository
   the first branch pushed becomes the default; if Settings → General still shows `master`, rename it to `main` there
   before the ruleset is created.
2. **The first commit goes in by direct push** (README, `.gitignore`), before the ruleset exists. Everything after it
   is a pull request.
3. **Claude GitHub App** (if an AI assistant prepares the pull requests): an organisation owner installs or extends the
   Claude app to this repository (github.com/organizations/mikopbx/settings/installations).

## 2. Actions

Settings → Actions → General:

- Actions permissions: allow GitHub's own actions and the sha-pinned actions the workflows use.
- Workflow permissions: **Read repository contents** (each job asks for more only where it needs it).
- "Allow GitHub Actions to create and approve pull requests": on only while the image update has no GitHub App (the
  fallback in `image-update.yaml` opens its pull request with `GITHUB_TOKEN`).
- Fork pull request workflows: the defaults (the workflows use `pull_request_target` and never run the pull
  request's code).

## 3. Environments

Settings → Environments:

| Environment | Protection | Secrets | Why |
|---|---|---|---|
| `ai-review` | Required reviewers: MIKO's reviewers, "Prevent self-review" on; deployment branches: `main` only | `ANTHROPIC_API_KEY` (an administrator pastes the value; nobody else sees it) | every AI review run is approved by a person and is the only place the key exists (docs/ai-review.md) |
| `image-update` | Deployment branches: `main` only | `IMAGE_UPDATE_APP_PRIVATE_KEY` (option B below) | the App key is used only by the job that opens the image update pull request |

Repository variables (Settings → Secrets and variables → Actions → Variables):

| Variable | Value |
|---|---|
| `IMAGE_UPDATE_APP_ID` | the App id of the image update App (option B) |
| `IMAGE_UPDATE_BOT` | its bot login, `<app-slug>[bot]` |

## 4. The image update App (option B)

An organisation owner creates a GitHub App (Settings → Developer settings → GitHub Apps) named, for example,
`mikopbx-image-update`: no webhook; repository permissions **Contents: read and write**, **Pull requests: read and
write**, Metadata: read; installed on `mikopbx/module-applications` only. Then: its App id goes to
`IMAGE_UPDATE_APP_ID`, a generated private key to the `image-update` environment's `IMAGE_UPDATE_APP_PRIVATE_KEY`,
and its bot login to `IMAGE_UPDATE_BOT`.

With it, `image-update.yaml` opens the pull request as the App, so the required checks run on it; a pull request by
that bot that changes `config/mikopbx.yaml` and nothing else counts as maintenance (no application to test or review,
no terms box, no agreement). It still needs a reviewer's approval. Without the App the workflow falls back to
`GITHUB_TOKEN`: no check runs on that pull request, and an administrator merges it by setting the ruleset to
*Disabled* for that one merge (recorded in the audit log), never through a bypass.

## 5. Branches the workflows use

- `publisher-agreement-signatures`: create it from `main` (the agreement action stores signatures there and fails if
  it is missing). The ruleset does not cover it.
- `test-modules/<Name>`: the control modules, fetched by SHA by the image update and the AI calibration
  (config/controls.yaml). Published once with `python3 tests/module_repos.py DIR --push https://github.com/mikopbx/module-applications`;
  their SHAs are fixed by the test applications.

## 6. CODEOWNERS and the ruleset

1. Put MIKO's reviewers' GitHub logins (or a team) into `.github/CODEOWNERS` by pull request.
2. Settings → Rules → Rulesets → New ruleset → **Import a ruleset**: [docs/ruleset-main.json](ruleset-main.json).
   It is what docs/branch-protection.md describes: no bypass; pull request with 1 approval, stale approvals dismissed,
   approval of the most recent push, code owner review; the five required checks, strict; no deletion, no force push.

   Code owner review and approval of the most recent push need **at least two reviewers** in CODEOWNERS: GitHub does
   not let the author of a pull request approve it. With one, nothing can be merged.
3. Check it as docs/branch-protection.md says (a direct push is refused, a red pull request, one without approval and
   one whose approval was dismissed by a push cannot be merged).

## 7. The first runs

After the import: a test application with a full SHA passes on x86_64 and arm64, a deliberately broken one is
rejected, and the AI review runs after the tests (its environment asks a reviewer to approve).
