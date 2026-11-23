# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""tools/release.py (inputs, ZIP, results) and the structure of .github/workflows/release.yaml."""
import json
import pathlib
import re
import sys
import zipfile

import pytest
import yaml

HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE / "tools"))
import release  # noqa: E402

SHA = "1" * 40
APPLICATION = """\
schema_version: 1
module_id: ModuleExample
name: Example
description: Example module
repo: https://github.com/example-owner/module-example
commit: %s
license: GPL-3.0-or-later
support_email: a@example.com
maintainers: [someone]
compatibility:
  min_pbx_version: "2024.1.0"
  max_pbx_version: null
  architectures: [x86_64]
""" % ("0" * 40)

GOOD = {"JOB_ID": "job-42", "MODULE_ID": "ModuleExample", "REPO": "https://github.com/example-owner/module-example",
        "SHA": SHA, "VERSION": "1.2.3", "ARCHITECTURES": "x86_64"}


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "applications" / "ModuleExample"
    directory.mkdir(parents=True)
    (directory / "application.yaml").write_text(APPLICATION)
    return tmp_path


def refused(env, root, text):
    with pytest.raises(release.Refused, match=text):
        release.check_inputs(env, root)


def test_good_inputs_give_the_application_at_the_release_sha(root):
    checked = release.check_inputs(GOOD, root)
    assert checked["application"]["commit"] == SHA
    assert checked["architectures"] == ["x86_64"]


def test_architectures_default_to_both_but_must_be_declared(root):
    env = {**GOOD, "ARCHITECTURES": ""}
    refused(env, root, "does not declare")  # the example module declares x86_64 only
    (root / "applications" / "ModuleExample" / "application.yaml").write_text(APPLICATION.replace("[x86_64]", "[x86_64, arm64]"))
    assert release.check_inputs(env, root)["architectures"] == ["x86_64", "arm64"]


@pytest.mark.parametrize("sha", ["main", "v1.0", "1" * 7, "1" * 39, "1" * 41, "A" * 40, "g" * 40, SHA + "\n", " " + SHA, ""])
def test_sha_must_be_forty_lowercase_hex(root, sha):
    refused({**GOOD, "SHA": sha}, root, "sha")


@pytest.mark.parametrize("repo", [
    "https://gitlab.com/example-owner/module-example", "http://github.com/example-owner/module-example",
    "https://github.com/example-owner/module-example.git", "https://github.com/example-owner/module-example/",
    "https://github.com/example-owner", "https://github.com/example-owner/module-example/tree/main",
    "https://github.com.evil.example/example-owner/module-example", "https://user@github.com/example-owner/module-example",
    "git@github.com:example-owner/module-example.git", "https://github.com/example-owner/..", "https://github.com/-bad/x",
    "https://github.com/example-owner/module-example\n", "https://github.com/example-owner/module example", "",
])
def test_repo_must_be_a_github_owner_and_name(root, repo):
    refused({**GOOD, "REPO": repo}, root, "repo")


@pytest.mark.parametrize("key,value", [
    ("JOB_ID", ""), ("JOB_ID", "a b"), ("JOB_ID", "$(id)"), ("JOB_ID", "x" * 65), ("JOB_ID", "-a"),
    ("MODULE_ID", "../ModuleExample"), ("MODULE_ID", "module"), ("MODULE_ID", "ModuleExample\n"),
    ("VERSION", ""), ("VERSION", "1"), ("VERSION", "v1.2"), ("VERSION", "%ModuleVersion%"), ("VERSION", "1.2.3; id"),
    ("ARCHITECTURES", "x86_64,x86_64"), ("ARCHITECTURES", "arm"), ("ARCHITECTURES", "x86_64,"),
])
def test_other_inputs_are_checked(root, key, value):
    with pytest.raises(release.Refused):
        release.check_inputs({**GOOD, key: value}, root)


def test_module_must_be_admitted(root):
    refused({**GOOD, "MODULE_ID": "ModuleOther"}, root, "not admitted")


def test_repo_must_be_the_admitted_one(root):
    refused({**GOOD, "REPO": "https://github.com/someone-else/module-example"}, root, "admitted application")


def make_zip(path, module):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("module.json", json.dumps(module))
    return path


def test_zip_version_and_module_id_must_match(tmp_path):
    path = make_zip(tmp_path / "m.zip", {"moduleUniqueID": "ModuleExample", "version": "1.2.3"})
    info = release.verify_zip(path, "ModuleExample", "1.2.3")
    assert re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
    with pytest.raises(release.Refused, match="version"):
        release.verify_zip(path, "ModuleExample", "1.2.4")
    with pytest.raises(release.Refused, match="moduleUniqueID"):
        release.verify_zip(path, "ModuleOther", "1.2.3")


def test_zip_with_the_version_placeholder_is_not_a_release(tmp_path):
    # tools/package turns %ModuleVersion% into 0.0.0-<commit>: never a release version
    path = make_zip(tmp_path / "m.zip", {"moduleUniqueID": "ModuleExample", "version": "0.0.0-111111111111"})
    with pytest.raises(release.Refused, match="version"):
        release.verify_zip(path, "ModuleExample", "1.2.3")


def test_zip_without_module_json_is_refused(tmp_path):
    path = tmp_path / "m.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("other.txt", "x")
    with pytest.raises(release.Refused, match="module.json"):
        release.verify_zip(path, "ModuleExample", "1.2.3")


def release_values(**extra):
    return {"job_id": "job-42", "module_id": "ModuleExample", "repo": GOOD["REPO"], "sha": SHA, "version": "1.2.3",
            "architectures": ["x86_64", "arm64"], **extra}


def write_result(directory, arch, **change):
    digest = release.image_digests()[0]["digest"]
    result = {"module_id": "ModuleExample", "commit": SHA, "architecture": arch, "outcome": "passes", "package": {"sha256": "a" * 64},
              "runs": [{"mikopbx_version": "x", "outcome": "passes", "image": {"digest": digest}, "steps": []}], "steps": [{"name": "package", "status": "pass"}]}
    result.update(change)
    (directory / f"check-{arch}").mkdir(parents=True)
    (directory / f"check-{arch}" / "result.json").write_text(json.dumps(result))


def test_results_are_accepted_only_for_this_release(tmp_path):
    digests = {i["digest"] for i in release.image_digests()}
    write_result(tmp_path, "x86_64")
    write_result(tmp_path, "arm64", commit="2" * 40)
    results = release.read_results(tmp_path, release_values(), "a" * 64, digests)
    assert results["x86_64"]["outcome"] == "passes"
    assert results["arm64"]["outcome"].startswith("rejected")
    assert release.read_results(tmp_path, release_values(), "b" * 64, digests)["x86_64"]["outcome"].startswith("rejected")
    assert release.read_results(tmp_path, release_values(), "a" * 64, {"sha256:other"})["x86_64"]["outcome"].startswith("rejected")


def test_result_passes_only_if_every_architecture_and_the_review_passed(tmp_path):
    digests = {i["digest"] for i in release.image_digests()}
    write_result(tmp_path, "x86_64")
    write_result(tmp_path, "arm64")
    results = release.read_results(tmp_path, release_values(), "a" * 64, digests)
    package = {"sha256": "a" * 64, "bytes": 1, "version": "1.2.3"}
    ai = {"status": "complete", "outcome": "no_blockers", "blockers": [], "agent": {"cache_key": "k", "model": "m"}}
    summary = release.summarise(release_values(), package, results, ai, {"id": 1})
    assert summary["passed"] is True
    assert summary["zip"]["sha256"] == "a" * 64 and summary["job_id"] == "job-42" and summary["rules_version"]
    assert summary["mikopbx_image_digests_used"]
    assert release.summarise(release_values(), package, results, None, None)["passed"] is False
    results["arm64"] = None
    summary = release.summarise(release_values(), package, results, ai, None)
    assert summary["passed"] is False and summary["architectures"]["arm64"]["outcome"] == "no result"


def test_main_writes_the_application_and_outputs(root, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(release, "BASE", root)
    for key, value in GOOD.items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "application.yaml"
    assert release.main(["inputs", "--application-out", str(out)]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["sha"] == SHA and json.loads(lines["matrix"])["include"] == [{"arch": "x86_64", "runner": "ubuntu-latest"}]
    assert yaml.safe_load(out.read_text())["commit"] == SHA


# --- The workflow file ---------------------------------------------------------------------------------------------

WORKFLOW = yaml.safe_load((BASE / ".github" / "workflows" / "release.yaml").read_text())
WORKFLOW_TEXT = (BASE / ".github" / "workflows" / "release.yaml").read_text()


def steps():
    for name, job in WORKFLOW["jobs"].items():
        for step in job["steps"]:
            yield name, step


def test_workflow_is_dispatch_only_with_read_only_permissions():
    assert set(WORKFLOW[True]) == {"workflow_dispatch"}  # YAML reads `on` as True
    assert set(WORKFLOW[True]["workflow_dispatch"]["inputs"]) == {"job_id", "module_id", "repo", "sha", "version", "architectures"}
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert not [name for name, job in WORKFLOW["jobs"].items() if "permissions" in job]


def test_no_expression_in_a_run_script():
    for name, step in steps():
        assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')} puts an expression in a script; use env"


def test_every_action_is_pinned_to_a_sha():
    for name, step in steps():
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_only_ai_review_has_the_secret_and_the_environment():
    holders = {name for name, step in steps() if "secrets." in json.dumps(step)}
    assert holders == {"ai-review"}
    assert [n for n, j in WORKFLOW["jobs"].items() if "environment" in j] == ["ai-review"]
    assert WORKFLOW["jobs"]["ai-review"]["environment"] == "ai-review"


def test_one_retention_constant():
    assert WORKFLOW["env"] == {"RETENTION_DAYS": 7}
    for name, step in steps():
        if step.get("uses", "").startswith("actions/upload-artifact"):
            assert step["with"]["retention-days"] == "${{ env.RETENTION_DAYS }}", name


def test_contract_stubs_are_marked():
    assert WORKFLOW_TEXT.count("TODO(LIC-615)") >= 2
    assert "curl" not in WORKFLOW_TEXT and "http://" not in WORKFLOW_TEXT


def test_the_check_step_matches_test_yaml():
    # release.yaml copies the isolation-critical lines of test.yaml's `module` job; they must not drift apart.
    test = (BASE / ".github" / "workflows" / "test.yaml").read_text()
    for line in ['resume="$(openssl rand -hex 16)"', 'echo "::stop-commands::$resume"', 'echo "::$resume::"',
                 'python3 tools/check "$MODULE_ID"', "timeout-minutes: 45", "ubuntu-24.04-arm"]:
        assert line in test and (line in WORKFLOW_TEXT or line in release.RUNNERS.values()), line
