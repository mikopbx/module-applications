# MikoPBX module applications

[Русская версия](README.ru.md)

To place a third-party module in the MikoPBX marketplace, its publisher opens a pull request here that adds one
file: `applications/<module_id>/application.yaml`. The module's source stays in the publisher's own GitHub
repository; the application names that repository and **one full commit SHA**. Robots check the application and test
that exact commit in fresh MikoPBX containers on x86_64 and arm64. MIKO then reviews it and approves that commit.
After the merge the module is registered to its confirmed publisher, and the application stays here as the record
that the module was admitted.

**Status: pilot.** The terms and the publisher agreement are drafts until MIKO approves their text; the registration
after merge is not built yet.

```
your repository ──► pull request ──► checks ──► fixes ──► MIKO approves the commit ──► merge ──► registration ──► versions in the cabinet
                    (one application)  │
                                       ├─ Terms: the box in the description
                                       ├─ Publisher agreement: signed by a comment
                                       ├─ Validate: the rules + a triage comment for the reviewer
                                       ├─ Test: install / enable / disable / uninstall in MikoPBX, x86_64 and arm64
                                       └─ AI review: blockers with the rule and evidence, apart from advice (docs/ai-review.md)
```

## Submitting a module

See [CONTRIBUTING.md](CONTRIBUTING.md): a new application, an update and a withdrawal. In short:

1. Your module is in a public GitHub repository and follows [module-template/](module-template/) (the mandatory
   MikoPBX contract is listed there and in [rules/rules.yaml](rules/rules.yaml)).
2. Copy [templates/application.yaml](templates/application.yaml) to `applications/<module_id>/application.yaml`,
   fill it in, and check it locally with `tools/validate.py --online <module_id>` and `tools/check <module_id>`.
3. Open a pull request that changes that one file, tick the boxes, and sign the publisher agreement.

One application is one directory is one module id (GUID). Each new module needs its own application.

### Free and paid modules

Both are accepted. A paid module has `lic_product_id` and/or `lic_feature_id` (MIKO's licence ids) in `module.json`;
a free one has neither, or 0 (rule MJ-005: absent or a whole number ≥ 0). The tests have no licence key, so for a paid
module (rule LIFE-007):

- the package as it is is installed first: MikoPBX must refuse it for the licence, and only for the licence;
- install, enable, workers, the module's API, disable, uninstall, the logs and MikoPBX's health are then checked on a
  test copy of the same commit with the licence ids set to 0. Both ZIPs are unpacked and compared: the copy may differ
  only in `module.json`, only in those ids. The result, the Test summary and the AI report give the sha256 of both;
- licence activation and capture, and whatever the module does only with a licence, are **not checked**: the result
  says `license_required` for them, never "pass", and the triage comment marks the module as paid.

The AI review is the same for free and paid modules: the source is public either way. Whether a paid module is
published, and on what terms, is agreed with MIKO outside the pull request (the verified publisher agreement); the
application cannot set it.

## Limits of the pilot

- Only **public GitHub** repositories. Private sources are not accepted.
- Modules admitted before this process are not affected by it.
- `repo` (where the checks build the module) is not the catalogue's `source_url` ([CONTRIBUTING.md](CONTRIBUTING.md#repo-is-not-the-catalogues-source_url)).
- [TERMS.md](TERMS.md) and [PUBLISHER_AGREEMENT.md](PUBLISHER_AGREEMENT.md) are **drafts not approved by MIKO**;
  signatures collected now are for the pilot.
- Free and paid modules are both accepted ([above](#free-and-paid-modules)).
- **The publisher is not verified yet.** Until the registration API exists (LIC-613), the publisher card check says
  `not_checked`, and that is **not a pass**. MIKO's reviewers may approve and merge an application: the merge records
  that the module was admitted. Nothing registers a module after the merge yet (LIC-614), and when that step is built
  it must refuse an application whose publisher was not verified; until then the module is registered by hand, after
  MIKO has checked the publisher itself.
- Pull requests by the repository's owners and members that change no application (tools, rules, documentation, the
  image update) are maintenance: they need no terms box and no publisher agreement, and have nothing to test or review.
  They still need a reviewer's approval.

## What is in this repository

| Path | What |
|---|---|
| `applications/<module_id>/application.yaml` | Admitted modules (the registry) and, in pull requests, applications |
| `templates/application.yaml` | The application to copy |
| `schema/application.schema.json` | The application format, with its version |
| `rules/rules.yaml` | The requirements, each with an id: the mandatory MikoPBX contract apart from recommendations |
| `module-template/` | The smallest module that passes, with how to build its ZIP and run the checks |
| `tools/validate.py` | The rules, offline (format) and online (the module's repository at the commit) |
| `tools/check` | The module's life in a fresh MikoPBX container; `tools/package` builds just the ZIP |
| `config/mikopbx.yaml` | MIKO's trusted test configuration: the stable MikoPBX image by digest, limits, timeouts. `image-update.yaml` proposes a new digest by pull request after the control modules pass on it; a new image makes earlier results stale |
| `.github/workflows/` | `terms`, `cla` (publisher agreement), `validate` (rules + triage comment), `test` (MikoPBX matrix), `ai-review` (stage 3), `image-update` |
| `docs/` | How the checks work: [MikoPBX in Docker](docs/mikopbx-docker.md), [AI review](docs/ai-review.md), [security of the checks](docs/security.md), [branch protection](docs/branch-protection.md), [setting up the repository](docs/setup.md) |
| `TERMS.md`, `PUBLISHER_AGREEMENT.md` | Drafts: the text is approved by MIKO |

The checks run the same code locally and in CI. The workflows, tools, rules and configuration always come from the
base branch: a pull request cannot change what it is checked against, and one that touches anything outside its
`applications/<module_id>/` is rejected before anything else runs.

## Licence

The tools are Apache-2.0. Parts are adapted from [anthropics/oss-scanner](https://github.com/anthropics/oss-scanner)
(Apache-2.0); each such file says so. `module-template/` is derived from
[mikopbx/ModuleTemplate](https://github.com/mikopbx/ModuleTemplate) (GPL-3.0-or-later).
