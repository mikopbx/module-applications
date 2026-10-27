# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""tools/validate.py on the fixtures: every good application passes, every bad one fails with the expected reasons.

The online cases fetch from local git repositories built from tests/modules/ (tests/module_repos.py), so they run
without a network.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile

import pytest
import yaml

HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE / "tools"))
sys.path.insert(0, str(HERE))

import check_install  # noqa: E402
import module_repos  # noqa: E402
import publisher_registry  # noqa: E402
import validate  # noqa: E402

GOOD = sorted((HERE / "fixtures" / "good").iterdir())
BAD = sorted((HERE / "fixtures" / "bad").iterdir())


@pytest.fixture(scope="session")
def repos(tmp_path_factory):
    """The test modules as git repositories, reachable at https://github.com/module-applications-test/<Name>."""
    target = tmp_path_factory.mktemp("repos")
    commits = module_repos.build(target)
    for name in commits:
        (target / name).symlink_to(f"{name}.git")
    saved = dict(os.environ)
    os.environ.update(module_repos.environment(target))
    yield commits
    os.environ.clear()
    os.environ.update(saved)


def problems_of(results):
    return [(p["rule"], p["message"]) for r in results for p in r.problems]


@pytest.mark.parametrize("case", GOOD, ids=lambda p: p.name)
def test_good_application_passes_offline_and_online(case, repos):
    offline = validate.validate(case)
    assert problems_of(offline) == []
    online = validate.validate(case, online=True)
    assert problems_of(online) == []
    for result in online:
        assert result.online["module_json"]["moduleUniqueID"] == result.module_id
        assert result.registry["status"] == "not_checked"


@pytest.mark.parametrize("case", BAD, ids=lambda p: p.name)
def test_bad_application_fails_with_the_expected_reason(case, repos):
    expected = yaml.safe_load((case / "expected.yaml").read_text())
    if (case / "paths.txt").exists():
        _name, found = validate.check_pr_paths((case / "paths.txt").read_text().splitlines())
        found = [(re.match(r"\[([A-Z]+-\d+)\]", line).group(1), line) for line in found]
    else:
        found = problems_of(validate.validate(case, online=expected["online"]))
    assert found, f"{case.name} passed but must fail"
    for problem in expected["problems"]:
        assert any(rule == problem["rule"] and problem["contains"] in message for rule, message in found), \
            f"{case.name}: no [{problem['rule']}] containing {problem['contains']!r} in {found}"


@pytest.mark.parametrize("case", [c for c in BAD if (c / "applications").exists()], ids=lambda p: p.name)
def test_offline_bad_cases_are_caught_without_network(case):
    """Cases that are not online-only must fail offline too, with no repository at all."""
    expected = yaml.safe_load((case / "expected.yaml").read_text())
    if expected["online"]:
        pytest.skip("needs the repository")
    assert problems_of(validate.validate(case))


def test_cli_exit_codes_and_messages(repos):
    good = HERE / "fixtures" / "good" / "control"
    bad = HERE / "fixtures" / "bad" / "short-sha"
    env = dict(os.environ)
    ok = subprocess.run([sys.executable, str(BASE / "tools" / "validate.py"), "--root", str(good), "--online"], capture_output=True, text=True, env=env)
    assert ok.returncode == 0, ok.stderr
    assert ok.stdout.startswith("ok: 1 application(s), rules ")
    assert "publisher card: not_checked" in ok.stdout
    fail = subprocess.run([sys.executable, str(BASE / "tools" / "validate.py"), "--root", str(bad)], capture_output=True, text=True)
    assert fail.returncode == 1
    assert fail.stderr.startswith("ModuleAppCheck: [APP-004] commit must be the full 40-character commit SHA")
    as_json = subprocess.run([sys.executable, str(BASE / "tools" / "validate.py"), "--root", str(bad), "--json"], capture_output=True, text=True)
    assert as_json.returncode == 1
    report = json.loads(as_json.stdout)
    assert report["ok"] is False and report["rules_version"] == validate.RULES_VERSION
    assert report["applications"][0]["problems"][0]["rule"] == "APP-004"


def test_pr_paths_cli_prints_the_module_id(tmp_path):
    paths = tmp_path / "paths"
    paths.write_text("applications/ModuleAppCheck/application.yaml\n")
    done = subprocess.run([sys.executable, str(BASE / "tools" / "validate.py"), "--pr-paths", str(paths)], capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "ModuleAppCheck"
    paths.write_text("applications/ModuleAppCheck/application.yaml\n.github/workflows/test.yaml\n")
    done = subprocess.run([sys.executable, str(BASE / "tools" / "validate.py"), "--pr-paths", str(paths)], capture_output=True, text=True)
    assert done.returncode == 1 and "[APP-006]" in done.stderr


def test_unusual_path_is_not_echoed():
    _name, problems = validate.check_pr_paths(["::error::x", "applications/ModuleAppCheck/application.yaml"])
    assert problems == ["[APP-006] changes a file outside applications/<module_id>/: (a path with unusual characters)"]


def test_registry_mismatch_is_a_problem_and_stub_is_not_a_pass(repos):
    good = HERE / "fixtures" / "good" / "control"
    wrong = publisher_registry.StaticRegistry({1: {"support_email": "someone-else@example.com"}})
    assert ("APP-011", "support_email 'module-applications@example.com' is not the address on publisher 1's card") in problems_of(
        validate.validate(good, online=True, registry=wrong))
    taken = publisher_registry.StaticRegistry({1: {"support_email": "module-applications@example.com"}}, owners={"ModuleAppCheck": 7})
    assert any(rule == "APP-011" and "registered to another publisher" in m for rule, m in problems_of(validate.validate(good, online=True, registry=taken)))
    right = publisher_registry.StaticRegistry({1: {"support_email": "Module-Applications@example.com"}})
    [result] = validate.validate(good, online=True, registry=right)
    assert result.registry["status"] == "ok" and not result.problems
    assert publisher_registry.default().check("ModuleAppCheck", 1, "a@b.c").status == "not_checked"


def test_template_follows_the_schema(tmp_path):
    directory = tmp_path / "applications" / "ModuleExample"
    directory.mkdir(parents=True)
    (directory / "application.yaml").write_text((BASE / "templates" / "application.yaml").read_text())
    assert problems_of(validate.validate(tmp_path)) == []


def test_rules_are_unique_and_every_cited_rule_exists():
    ids = [rule["id"] for rule in validate.RULES["rules"]]
    assert len(ids) == len(set(ids))
    for rule in validate.RULES["rules"]:
        assert rule["level"] in ("mandatory", "recommended") and rule["status"] in ("accepted", "draft", "withdrawn")
        assert rule["check"] in ("validate", "test", "review")
    cited = set()
    for source in ("validate.py", "check_install.py", "check"):
        cited |= set(re.findall(r'"([A-Z]+-\d{3})"', (BASE / "tools" / source).read_text()))
    for case in BAD:
        cited |= {p["rule"] for p in yaml.safe_load((case / "expected.yaml").read_text())["problems"]}
    assert cited and cited <= set(ids), cited - set(ids)


def test_schema_is_valid_draft_2020_12():
    validate.jsonschema.Draft202012Validator.check_schema(validate.SCHEMA)


def test_printable_escapes_control_characters():
    assert validate.printable("a\x1b[31mb\nc") == "a\\x1b[31mb\\x0ac"


def test_symlinked_directory_is_rejected(tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "application.yaml").write_text("schema_version: 1\n")
    (tmp_path / "applications").mkdir()
    (tmp_path / "applications" / "ModuleAppCheck").symlink_to(real)
    assert ("APP-003", "symbolic links are not accepted (applications/ModuleAppCheck is one)") in problems_of(validate.validate(tmp_path))


def test_package_is_reproducible_and_fills_the_version(tmp_path, repos):
    url = "https://github.com/module-applications-test/ModuleAppCheck"
    commit = repos["ModuleAppCheck"][-1]
    first = check_install.package(url, commit, tmp_path / "a", tmp_path / "a.zip")
    second = check_install.package(url, commit, tmp_path / "b", tmp_path / "b.zip")
    assert first == second and first["version"] == "1.1.0"
    with zipfile.ZipFile(tmp_path / "a.zip") as archive:
        names = archive.namelist()
        assert names == sorted(names) and "module.json" in names and "Setup/PbxExtensionSetup.php" in names

    # A repository with the %ModuleVersion% placeholder gets a version naming the commit.
    work = tmp_path / "placeholder"
    work.mkdir()
    (work / "module.json").write_text('{"moduleUniqueID": "ModuleX", "version": "%ModuleVersion%"}')
    module_repos.git(work, "init", "--quiet")
    module_repos.git(work, "add", "--all")
    module_repos.git(work, "commit", "--quiet", "--no-gpg-sign", "-m", "x")
    sha = module_repos.git(work, "rev-parse", "HEAD")
    info = check_install.package(str(work), sha, tmp_path / "c", tmp_path / "c.zip")
    assert info["version"] == f"0.0.0-{sha[:12]}"
    with zipfile.ZipFile(tmp_path / "c.zip") as archive:
        assert json.loads(archive.read("module.json"))["version"] == info["version"]


def test_paid_module_gets_a_test_copy_without_licence_ids(tmp_path, repos):
    """LIFE-007: the package stays as it is; the test copy differs only in the licence ids, which are 0."""
    url = "https://github.com/module-applications-test/ModuleAppPaid"
    commit = repos["ModuleAppPaid"][-1]
    info = check_install.package(url, commit, tmp_path / "g", tmp_path / "paid.zip", test_copy=tmp_path / "paid.test.zip")
    plain = check_install.package(url, commit, tmp_path / "h", tmp_path / "plain.zip")
    assert info["sha256"] == plain["sha256"] and (info["lic_product_id"], info["lic_feature_id"]) == (9001, 9002)
    assert info["test_copy_sha256"] != info["sha256"]
    with zipfile.ZipFile(tmp_path / "paid.zip") as paid, zipfile.ZipFile(tmp_path / "paid.test.zip") as copy:
        assert paid.namelist() == copy.namelist()
        for name in paid.namelist():
            if name != "module.json":
                assert paid.read(name) == copy.read(name)
        original, tested = json.loads(paid.read("module.json")), json.loads(copy.read("module.json"))
        assert (tested["lic_product_id"], tested["lic_feature_id"]) == (0, 0)
        assert {k: v for k, v in tested.items() if not k.startswith("lic_")} == {k: v for k, v in original.items() if not k.startswith("lic_")}
    assert info["test_copy_diff"] == {"files": ["module.json"], "module_json_keys": ["lic_feature_id", "lic_product_id"]}
    # The comparison catches a copy that differs in anything else.
    with zipfile.ZipFile(tmp_path / "paid.test.zip") as copy, zipfile.ZipFile(tmp_path / "tampered.zip", "w") as tampered:
        for name in copy.namelist():
            data = copy.read(name)
            tampered.writestr(name, data.replace(b"1.0.0", b"9.9.9") if name == "module.json" else data)
    with pytest.raises(check_install.Failed, match="not only in licence ids"):
        check_install.compare_test_copy(tmp_path / "paid.zip", tmp_path / "tampered.zip")
    with zipfile.ZipFile(tmp_path / "paid.test.zip") as copy, zipfile.ZipFile(tmp_path / "extra.zip", "w") as extra:
        for name in copy.namelist():
            extra.writestr(name, copy.read(name) + (b"\n" if name.endswith(".php") else b""))
    with pytest.raises(check_install.Failed, match="not only in module.json"):
        check_install.compare_test_copy(tmp_path / "paid.zip", tmp_path / "extra.zip")
    # A free module gets no test copy.
    free = check_install.package("https://github.com/module-applications-test/ModuleAppCheck", repos["ModuleAppCheck"][-1],
                                 tmp_path / "f", tmp_path / "free.zip", test_copy=tmp_path / "free.test.zip")
    assert "test_copy_sha256" not in free and not (tmp_path / "free.test.zip").exists()


@pytest.mark.parametrize("value, ok", [(None, True), (0, True), (306, True), ("306", False), ("0", False), (-1, False), (1.5, False), (True, False)])
def test_licence_ids_must_be_whole_numbers(tmp_path, repos, value, ok):
    """MJ-005: a paid module is accepted; a licence id must be absent or a whole number ≥ 0."""
    work = tmp_path / "module"
    shutil.copytree(HERE / "modules" / "ModuleAppCheck", work)
    module = json.loads((work / "module.json").read_text())
    if value is not None:
        module["lic_feature_id"] = value
    (work / "module.json").write_text(json.dumps(module))
    module_repos.git(work, "init", "--quiet")
    module_repos.git(work, "add", "--all")
    module_repos.git(work, "commit", "--quiet", "--no-gpg-sign", "-m", "x")
    sha = module_repos.git(work, "rev-parse", "HEAD")
    # Publish it next to the test modules, where https://github.com/module-applications-test/ leads (the repos fixture).
    target = pathlib.Path(os.environ["GIT_CONFIG_KEY_0"].removeprefix("url.file://").removesuffix("/.insteadOf"))
    name = f"LicenceIds{tmp_path.name.replace('_', '')}"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(target / name)], check=True, capture_output=True)
    root = tmp_path / "root"
    app = yaml.safe_load((HERE / "fixtures" / "good" / "control" / "applications" / "ModuleAppCheck" / "application.yaml").read_text())
    (root / "applications" / "ModuleAppCheck").mkdir(parents=True)
    (root / "applications" / "ModuleAppCheck" / "application.yaml").write_text(
        yaml.safe_dump({**app, "repo": f"https://github.com/module-applications-test/{name}", "commit": sha}))
    rules = [rule for rule, _ in problems_of(validate.validate(root, online=True))]
    assert ("MJ-005" not in rules) == ok


def test_fetch_refuses_a_branch_name(tmp_path, repos):
    with pytest.raises(check_install.Failed):
        check_install.package("https://github.com/module-applications-test/ModuleAppCheck", "main", tmp_path / "g", tmp_path / "x.zip")


def test_module_template_with_the_application_template_passes_online(tmp_path):
    """module-template/ in a repository, named by templates/application.yaml, passes every rule tools/validate.py has."""
    work = tmp_path / "ModuleExample"
    work.mkdir()
    shutil.copytree(BASE / "module-template", work, dirs_exist_ok=True)
    module_repos.git(work, "init", "--quiet")
    module_repos.git(work, "add", "--all")
    module_repos.git(work, "commit", "--quiet", "--no-gpg-sign", "-m", "template")
    sha = module_repos.git(work, "rev-parse", "HEAD")
    application = yaml.safe_load((BASE / "templates" / "application.yaml").read_text())
    application["commit"] = sha
    directory = tmp_path / "root" / "applications" / "ModuleExample"
    directory.mkdir(parents=True)
    (directory / "application.yaml").write_text(yaml.safe_dump(application))
    saved = dict(os.environ)
    os.environ.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.file://{work}.insteadOf", "GIT_CONFIG_VALUE_0": application["repo"]})
    try:
        [result] = validate.validate(tmp_path / "root", online=True)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert result.problems == [] and result.warnings == []
    assert result.online["module_json"]["moduleUniqueID"] == "ModuleExample"


def test_guid_conflict_differing_only_in_case(tmp_path):
    """APP-005 across directories whose names differ only in case. Built here rather than kept as a fixture: such a pair
    cannot be checked out on a case-insensitive file system (macOS, Windows)."""
    good = yaml.safe_load((HERE / "fixtures" / "good" / "control" / "applications" / "ModuleAppCheck" / "application.yaml").read_text())
    applications = tmp_path / "applications"
    (applications / "ModuleAppCheck").mkdir(parents=True)
    if (applications / "ModuleAPPCheck").exists():
        pytest.skip("case-insensitive file system: the two directories are one")
    (applications / "ModuleAPPCheck").mkdir()
    (applications / "ModuleAppCheck" / "application.yaml").write_text(yaml.safe_dump(good))
    (applications / "ModuleAPPCheck" / "application.yaml").write_text(yaml.safe_dump({**good, "module_id": "ModuleAPPCheck"}))
    found = problems_of(validate.validate(tmp_path))
    assert ("APP-005", "module_id 'ModuleAppCheck' is already claimed by applications/ModuleAPPCheck/") in found
    assert ("APP-005", "module_id 'ModuleAPPCheck' is already claimed by applications/ModuleAppCheck/") in found
