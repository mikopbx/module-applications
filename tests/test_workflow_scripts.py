# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""The scripts the workflows run: take_application.sh with a fake `gh` and a local origin, triage.py with a fake API."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE / ".github" / "scripts"))
sys.path.insert(0, str(HERE))
import module_repos  # noqa: E402
import triage  # noqa: E402

GOOD = (HERE / "fixtures" / "good" / "control" / "applications" / "ModuleAppCheck" / "application.yaml").read_text()


def git(work, *args):
    return module_repos.git(work, *args)


@pytest.fixture
def repository(tmp_path):
    """A base checkout (a copy of this directory) with a bare origin, and a helper to open a pull request on it."""
    origin = tmp_path / "origin.git"
    base = tmp_path / "base"
    shutil.copytree(BASE, base, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "fixtures", "modules"))
    git(base, "init", "--quiet", "--initial-branch=main")
    git(base, "add", "--all")
    git(base, "commit", "--quiet", "--no-gpg-sign", "-m", "base")
    subprocess.run(["git", "clone", "--quiet", "--bare", str(base), str(origin)], check=True)
    git(base, "remote", "add", "origin", str(origin))

    def pull_request(changes):
        """Commit `changes` ({path: text or None}) on a branch in origin; return (head sha, changed paths)."""
        work = tmp_path / "pr"
        shutil.rmtree(work, ignore_errors=True)
        subprocess.run(["git", "clone", "--quiet", str(origin), str(work)], check=True)
        for path, text in changes.items():
            target = work / path
            if text is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        git(work, "add", "--all")
        git(work, "commit", "--quiet", "--no-gpg-sign", "-m", "pr")
        head = git(work, "rev-parse", "HEAD")
        subprocess.run(["git", "-C", str(work), "push", "--quiet", "origin", "HEAD:refs/pull/1/head"], check=True, capture_output=True)
        return head, list(changes)

    return base, pull_request


def run_take(base, tmp_path, head, paths, association="NONE", author="someone", bot=""):
    """Run take_application.sh in `base` with a fake gh that serves this pull request."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "pull.json").write_text(json.dumps({"head": {"sha": head}, "changed_files": len(paths), "author_association": association, "user": {"login": author}}))
    (tmp_path / "files.txt").write_text("".join(f"{p}\n" for p in paths))
    gh = bin_dir / "gh"
    gh.write_text(f"""#!/bin/sh
case "$2" in
  */files*) cat {tmp_path}/files.txt ;;
  *) cat {tmp_path}/pull.json ;;
esac
""")
    gh.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(exist_ok=True)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(runner_temp), "IMAGE_UPDATE_BOT": bot}
    done = subprocess.run([str(base / ".github" / "scripts" / "take_application.sh"), "owner/repo", "1"], cwd=base, capture_output=True, text=True, env=env)
    values = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return done, values


def test_take_application_takes_only_the_application(repository, tmp_path):
    base, pull_request = repository
    head, paths = pull_request({"applications/ModuleAppCheck/application.yaml": GOOD})
    done, values = run_take(base, tmp_path, head, paths)
    assert done.returncode == 0, done.stdout + done.stderr
    assert values == {"head": head, "module_id": "ModuleAppCheck", "previous": ""}
    assert (base / "applications" / "ModuleAppCheck" / "application.yaml").read_text() == GOOD


def test_take_application_refuses_a_workflow_change_before_reading_anything(repository, tmp_path):
    base, pull_request = repository
    head, paths = pull_request({"applications/ModuleAppCheck/application.yaml": GOOD, ".github/workflows/test.yaml": "on: push\n"})
    done, values = run_take(base, tmp_path, head, paths)
    assert done.returncode == 1
    assert "::error::This pull request changes files outside one applications/<module_id>/" in done.stdout
    assert "[APP-006] changes a file outside applications/<module_id>/: .github/workflows/test.yaml" in done.stderr
    assert "module_id" not in values
    assert not (base / "applications" / "ModuleAppCheck").exists()
    assert (base / ".github" / "workflows" / "test.yaml").read_text() != "on: push\n"


def test_take_application_refuses_an_extra_file(repository, tmp_path):
    base, pull_request = repository
    head, paths = pull_request({"applications/ModuleAppCheck/application.yaml": GOOD, "applications/ModuleAppCheck/run.sh": "id\n"})
    done, _ = run_take(base, tmp_path, head, paths)
    assert done.returncode == 1
    assert "may hold application.yaml only" in done.stdout


def test_take_application_reports_the_previous_commit_of_an_update(repository, tmp_path):
    base, pull_request = repository
    previous = yaml.safe_load(GOOD)
    previous["commit"] = "eb13e87500abdf6852b3001ed63b2570ea6d0f89"
    (base / "applications" / "ModuleAppCheck").mkdir(parents=True)
    (base / "applications" / "ModuleAppCheck" / "application.yaml").write_text(yaml.safe_dump(previous))
    git(base, "add", "--all")
    git(base, "commit", "--quiet", "--no-gpg-sign", "-m", "admitted")
    git(base, "push", "--quiet", "origin", "main")
    head, paths = pull_request({"applications/ModuleAppCheck/application.yaml": GOOD})
    done, values = run_take(base, tmp_path, head, paths)
    assert done.returncode == 0, done.stderr
    assert values["previous"] == "eb13e87500abdf6852b3001ed63b2570ea6d0f89"


def test_take_application_withdrawal_tests_nothing(repository, tmp_path):
    base, pull_request = repository
    (base / "applications" / "ModuleAppCheck").mkdir(parents=True)
    (base / "applications" / "ModuleAppCheck" / "application.yaml").write_text(GOOD)
    git(base, "add", "--all")
    git(base, "commit", "--quiet", "--no-gpg-sign", "-m", "admitted")
    git(base, "push", "--quiet", "origin", "main")
    head, paths = pull_request({"applications/ModuleAppCheck/application.yaml": None})
    done, values = run_take(base, tmp_path, head, paths)
    assert done.returncode == 0 and "a withdrawal" in done.stdout
    assert "module_id" not in values


def test_take_application_lets_owners_maintain_without_an_application(repository, tmp_path):
    base, pull_request = repository
    head, paths = pull_request({"tools/validate.py": "# changed\n"})
    done, values = run_take(base, tmp_path, head, paths, association="OWNER")
    assert done.returncode == 0 and "maintenance pull request (OWNER)" in done.stdout
    assert values.get("maintenance") == "true" and "module_id" not in values


def test_take_application_refuses_the_same_change_from_anyone_else(repository, tmp_path):
    base, pull_request = repository
    head, paths = pull_request({"tools/validate.py": "# changed\n"})
    done, values = run_take(base, tmp_path, head, paths, association="CONTRIBUTOR")
    assert done.returncode == 1 and "[APP-006]" in done.stderr and "maintenance" not in values


def test_take_application_lets_the_image_update_app_change_only_the_image_pin(repository, tmp_path):
    base, pull_request = repository
    bot = "mikopbx-image-update[bot]"
    head, paths = pull_request({"config/mikopbx.yaml": "# new image\n"})
    done, values = run_take(base, tmp_path, head, paths, author=bot, bot=bot)
    assert done.returncode == 0 and values.get("maintenance") == "true"
    # Without the variable, or with any other file, the App is treated like anyone else.
    done, values = run_take(base, tmp_path, head, paths, author=bot, bot="")
    assert done.returncode == 1 and "maintenance" not in values


def test_take_application_refuses_more_than_the_image_pin_from_the_image_update_app(repository, tmp_path):
    base, pull_request = repository
    bot = "mikopbx-image-update[bot]"
    head, paths = pull_request({"config/mikopbx.yaml": "# new image\n", "tools/check": "# changed\n"})
    done, values = run_take(base, tmp_path, head, paths, author=bot, bot=bot)
    assert done.returncode == 1 and "maintenance" not in values


def test_an_owner_cannot_mix_an_application_with_other_changes(repository, tmp_path):
    base, pull_request = repository
    head, paths = pull_request({"applications/ModuleAppCheck/application.yaml": GOOD, ".github/workflows/test.yaml": "on: push\n"})
    done, _ = run_take(base, tmp_path, head, paths, association="OWNER")
    assert done.returncode == 1 and "[APP-006]" in done.stderr


class FakeGitHub:
    def __init__(self, answers):
        self.answers = answers
        self.requests = 0

    def get(self, path, accept="application/vnd.github+json"):
        for prefix, answer in self.answers.items():
            if path.startswith(prefix):
                return answer
        return None


def validation(problems=()):
    return {
        "rules_version": "0.1.0", "schema_version": 1, "ok": not problems,
        "applications": [{
            "module_id": "ModuleAppCheck", "repo": "https://github.com/vendor/ModuleAppCheck",
            "commit": "1a4dfa6326c83d1ebcb40838ed7c0aebc24bbcda", "problems": list(problems), "warnings": [],
            "online": {"module_json": {"support_email": "help@vendor.example"}},
            "registry": {"status": "not_checked", "detail": "the licence server API (LIC-613) does not exist yet"},
        }],
    }


ANSWERS = {
    "repos/owner/apps/pulls/1/files": [{"filename": "applications/ModuleAppCheck/application.yaml"}],
    "repos/owner/apps/pulls/1": {"user": {"login": "vendor"}, "body": "- [x] I maintain this module\n- [ ] I accept the [module-applications terms](x)\n"},
    "repos/vendor/ModuleAppCheck/commits/": {"commit": {"committer": {"date": "2026-10-27T00:00:00Z"}}},
    "repos/vendor/ModuleAppCheck/compare/": {"status": "identical"},
    "repos/vendor/ModuleAppCheck/contents/SECURITY.md": "Write to help@vendor.example",
    "repos/vendor/ModuleAppCheck": {"created_at": "2024-01-01T00:00:00Z", "pushed_at": "2026-10-27T00:00:00Z", "stargazers_count": 3,
                                    "forks_count": 1, "license": {"spdx_id": "GPL-3.0"}, "default_branch": "main"},
}


@pytest.mark.parametrize("association, files, fails, maintenance", [
    ("OWNER", ["tools/check", "README.md"], False, "true"),
    ("MEMBER", ["docs/x.md"], False, "true"),
    ("OWNER", ["tools/check", "applications/ModuleX/application.yaml"], False, "false"),
    ("CONTRIBUTOR", ["tools/check"], False, "false"),
    ("NONE", ["docs/x.md"], False, "false"),
    ("OWNER", ["docs/x.md"], True, "false"),
    ("OWNER", [f"docs/{i}.md" for i in range(3000)], False, "false"),
])
def test_agreement_is_not_asked_of_maintenance(tmp_path, association, files, fails, maintenance, author="someone", bot=""):
    """cla.yaml: only a pull request by owners or members that changes nothing under applications/ skips the agreement."""
    workflow = yaml.safe_load((BASE / ".github" / "workflows" / "cla.yaml").read_text())
    scope, agreement = workflow["jobs"]["agreement"]["steps"][:2]
    assert scope["id"] == "scope" and agreement["if"].startswith("steps.scope.outputs.maintenance != 'true' && ")
    (tmp_path / "bin").mkdir()
    (tmp_path / "files").write_text("\n".join(files) + "\n")
    (tmp_path / "bin" / "gh").write_text(f"#!/bin/sh\n{'exit 1' if fails else 'cat ' + str(tmp_path / 'files')}\n")
    (tmp_path / "bin" / "gh").chmod(0o755)
    output = tmp_path / "output"
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output),
           "REPO": "o/r", "NUMBER": "1", "ASSOCIATION": association, "GH_TOKEN": "x", "AUTHOR": author, "IMAGE_UPDATE_BOT": bot}
    subprocess.run(["bash", "-e", "-c", scope["run"]], env=env, check=True, capture_output=True)
    assert output.read_text().strip() == f"maintenance={maintenance}"


@pytest.mark.parametrize("files, bot, maintenance", [
    (["config/mikopbx.yaml"], "mikopbx-image-update[bot]", "true"),
    (["config/mikopbx.yaml", "tools/check"], "mikopbx-image-update[bot]", "false"),
    (["config/mikopbx.yaml"], "", "false"),
])
def test_agreement_is_not_asked_of_the_image_update_app(tmp_path, files, bot, maintenance):
    test_agreement_is_not_asked_of_maintenance(tmp_path, "NONE", files, False, maintenance, author="mikopbx-image-update[bot]", bot=bot)


def test_triage_comment_lists_the_facts():
    comment = triage.build(FakeGitHub(ANSWERS), "owner/apps", 1, validation())
    assert comment.startswith(triage.MARKER)
    assert "| Rules | pass, rules 0.1.0, schema 1 |" in comment
    assert "| Publisher card | not\\_checked:" in comment
    assert "vendor owns the repository" in comment
    assert "on the default branch main" in comment and "SECURITY.md names support_email" in comment
    assert "| Scope | one application |" in comment
    assert "1 of 6 boxes ticked" in comment
    assert "decides nothing" in comment


def test_triage_comment_escapes_external_text_and_reports_problems():
    hostile = dict(ANSWERS)
    hostile["repos/owner/apps/pulls/1"] = {"user": {"login": "x"}, "body": ""}
    problems = [{"rule": "APP-004", "message": "<img src=x onerror=alert(1)> @everyone [link](https://evil.example) | col"}]
    comment = triage.build(FakeGitHub(hostile), "owner/apps", 1, validation(problems))
    assert "**1 problem(s)**" in comment
    assert not re.search(r"(?<!\\)<img", comment) and "\\<img" in comment
    assert "@everyone" not in comment and "](https://" not in comment and not re.search(r"(?<!\\)\| col", comment)


def test_triage_is_silent_without_an_application():
    quiet = dict(ANSWERS)
    quiet["repos/owner/apps/pulls/1/files"] = [{"filename": "README.md"}]
    assert triage.build(FakeGitHub(quiet), "owner/apps", 1, {}) == ""


def test_triage_survives_a_failed_lookup():
    class Failing(FakeGitHub):
        def get(self, path, accept="application/vnd.github+json"):
            if path.startswith("repos/vendor/"):
                raise OSError("network down")
            return super().get(path, accept)

    comment = triage.build(Failing(ANSWERS), "owner/apps", 1, validation())
    assert "could not be checked (OSError)" in comment
