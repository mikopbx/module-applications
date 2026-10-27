# Applying, updating and withdrawing a module

[Русская версия](CONTRIBUTING.ru.md)

This repository accepts one kind of contribution from publishers: a pull request that adds, changes or removes **one**
`applications/<module_id>/application.yaml`. Changes to the tools, rules, workflows or documentation come from MIKO;
a pull request that touches them together with an application is rejected by the checks.

## Before you apply

- Your module is in a **public GitHub repository** that you maintain. The checks fetch it anonymously.
- It follows the mandatory part of [rules/rules.yaml](rules/rules.yaml); [module-template/](module-template/) is the
  smallest module that does.
- You have a publisher account in the MIKO cabinet; `publisher_id` is its id. It only identifies the card the checks
  compare with: it does not prove who you are. MIKO checks that you are the confirmed publisher.
- `support_email` is a public address. It is the same in `module.json`, in the application and on your publisher card.
- Free and paid modules are both accepted. A paid module keeps its `lic_product_id`/`lic_feature_id` in `module.json`
  (whole numbers); the tests check it without a licence key and report what needs one as `license_required`
  ([README.md](README.md#free-and-paid-modules)). Publishing a paid module also needs the verified publisher agreement
  with MIKO, agreed outside the pull request.

## A new module

1. Pick the commit to submit and copy its **full** SHA (`git rev-parse HEAD`, 40 characters). A branch, a tag or a
   short SHA is rejected: MIKO approves exactly one commit.
2. Copy [templates/application.yaml](templates/application.yaml) to `applications/<module_id>/application.yaml`.
   The directory name is your `moduleUniqueID`. Nothing else goes in the directory.
3. Check it as CI will:
   ```sh
   pip install -r tools/requirements.txt
   tools/validate.py --online <module_id>
   tools/check <module_id>          # needs Docker; see module-template/README.md
   ```
4. Open a pull request that changes only that file. Fill in the checklist of the pull request template, including
   **I accept the module-applications terms**.
5. Sign the publisher agreement: post a comment with only the line the bot asks for.
6. Wait for the checks:
   - **Terms**, **Publisher agreement**: your box and your signature.
   - **Validate**: the rules; a triage comment summarises what a reviewer will look at.
   - **Test**: the module installed, enabled, disabled and uninstalled in MikoPBX on each architecture you declared.
     The result (JSON, kept seven days) is attached to the run.
   - **AI review**, after Test passes and a MIKO person starts it: a report on the pull request with blockers (each
     tied to a rule, a file and a line) apart from recommendations, and the status "AI review" on your commit. It
     finds problems; it approves nothing ([docs/ai-review.md](docs/ai-review.md)).
7. Fix what they report by pushing a new commit to your module and updating `commit` in the application. Every change
   to the application starts all the checks again.
8. A MIKO reviewer approves the application. The approval covers this exact commit, this revision of the application
   and the confirmed publisher: any later push to the pull request, a new `commit`, or a change of the rules makes it
   stale, and the checks and the approval are needed again. So does a new MikoPBX test image: MIKO moves the
   pinned image (`config/mikopbx.yaml`) by a reviewed pull request, and results obtained on the old digest must be
   run again.
9. After the merge MIKO registers the module to your publisher account. Registration does not publish a version:
   you upload versions in the cabinet as before.

Each new module id needs its own application. A module id that another application or publisher already holds is
refused.

## Updating an application

Open a pull request that changes your existing `applications/<module_id>/application.yaml`, usually `commit`, and
nothing else. The checks run again; **Test** also installs the previously approved commit and updates it in place to
the new one. MIKO approves the new commit like a new application.

You do not need an update to publish a new version of an admitted module: versions are uploaded in the cabinet. Update
the application when its facts change (repository, contact, compatibility, maintainers) or when MIKO asks for a new
reviewed commit.

## Withdrawing a module

Open a pull request that removes `applications/<module_id>/`. The checks have nothing to test; a MIKO reviewer
confirms the withdrawal with you before merging. What happens to the registered module and its published versions is
decided by MIKO and the publisher agreement.

## What the next stage receives

The source never enters this repository. What the next stages (the AI report, the human decision, the registration)
receive is the merged application plus the results of the checks: module id, publisher id, repository URL, the commit,
the sha256 of the ZIP built from it, the rules and schema versions, the MikoPBX version, architecture and image digest
of each test run, the steps and their outcome, and a link to the run. Those results are evidence, not credentials: the
registration checks that they come from this repository's workflow, for this pull request and this commit.

## `repo` is not the catalogue's `source_url`

`repo` in an application is **required**: it is where the checks build the module from. The catalogue's existing
`source_url` (optional, shown to users) is a different field and is not changed by this process. Modules admitted
before this process are not affected (MIKO's decision of 2026-10-27). Private source is not accepted: `repo` must be a
public GitHub repository.
