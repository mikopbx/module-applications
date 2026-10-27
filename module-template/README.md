# module-template: the smallest module an application can submit

[Русская версия](README.ru.md)

This directory is a minimal MikoPBX module that passes every check of module-applications. Copy it into **your own**
repository and rename `ModuleExample` to your module id; the source stays in your repository, and the application
names a commit of it. For a fuller example (web pages, models, AMI worker) see
[mikopbx/ModuleTemplate](https://github.com/mikopbx/ModuleTemplate).

## What MikoPBX requires (mandatory) and what is advice (recommended)

The full list, with an id for each rule, is [rules/rules.yaml](../rules/rules.yaml) (rules version `0.3.0`); the
application format is [schema/application.schema.json](../schema/application.schema.json) (schema version 1).
The mandatory part for the module itself:

| File | Rule | What |
|---|---|---|
| `module.json` at the repository root | PKG-001, MJ-001..005 | `moduleUniqueID` = your module id (`Module` + letters/digits), `version` like `1.2.3`, `min_pbx_version` like `2024.1.0` (at least 2020.2.468); `lic_product_id`/`lic_feature_id` absent or 0 for a free module, MIKO's whole-number ids for a paid one |
| `Setup/PbxExtensionSetup.php` | PKG-003, CODE-001 | `namespace Modules\<id>\Setup; class PbxExtensionSetup` (extending `PbxExtensionSetupBase` is the easy way) |
| `Lib/<X>Conf.php` (if you use hooks, workers or an API) | CODE-003 | `X` = id without `Module`; `extends ConfigClass`; loads on the current Core |
| every PHP file | CODE-002, LIFE-003 | namespace `Modules\<id>\...` matching its path; `php -l` passes; no fatal errors |
| the whole module | LIFE-001..005 | installs, enables, disables and uninstalls through the REST API, and MikoPBX still works afterwards |

Recommended: `support_email` and `developer` in module.json (MJ-006), `uninstallModule()` (CODE-004),
`Messages/en.php` and `ru.php`, a `SECURITY.md` that names `support_email`, unique `agi-bin` names (CODE-005),
no `db/*.db` or `.env` in the repository (PKG-005).

## Build the ZIP from a commit

The checks never use a ZIP you upload: they build it from the commit in your application. To see exactly what they
build:

```sh
git clone https://github.com/<mikopbx-org>/module-applications && cd module-applications
pip install -r tools/requirements.txt
tools/package https://github.com/<you>/ModuleExample <full 40-character commit SHA> ModuleExample.zip
```

It prints the sha256 of the ZIP. The same commit always gives the same ZIP: entries sorted, dated at the commit time,
`%ModuleVersion%` in module.json replaced by `0.0.0-<commit>`. Files marked `export-ignore` in `.gitattributes`
are left out.

## Run the same checks as CI

Write `applications/<id>/application.yaml` from [templates/application.yaml](../templates/application.yaml), then:

```sh
tools/validate.py --online <id>      # the rules: format, module.json at the commit, layout (no code runs)
tools/check <id>                     # the module in a fresh MikoPBX container (needs Docker)
```

`tools/check` pulls the MikoPBX image named in [config/mikopbx.yaml](../config/mikopbx.yaml) (by digest), starts it on
a Docker network with no route out, installs the ZIP through the REST API, enables the module, looks for its workers
and its `check` API, disables and uninstalls it, scans the logs and checks that MikoPBX still answers. It prints a JSON
result and exits 0 when every step passes. To test an update, add `--previous-commit <the approved commit>`.
On Linux it also adds an iptables rule so the module cannot reach your machine; that needs root or sudo.
Run it on a machine with nothing to lose: the module's code runs in a container on your Docker.

CI runs the same two commands on x86_64 and arm64 GitHub runners for each architecture your application declares.
