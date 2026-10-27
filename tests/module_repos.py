#!/usr/bin/env python3
# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""Turn tests/modules/<Name>/ into git repositories with reproducible commits, for the tests and tools/check.

    python3 tests/module_repos.py DIR            create DIR/<Name>.git for every module and print the commits
    eval "$(python3 tests/module_repos.py DIR --env)"
    python3 tests/module_repos.py DIR --push URL publish them as branches test-modules/<Name> of URL (config/controls.yaml)

The commits are reproducible (fixed author, committer, dates and messages), so their SHAs are written into the test
applications. With --env it prints the GIT_CONFIG_* variables that make git fetch
https://github.com/module-applications-test/<Name> from DIR/<Name>.git, so tools/validate.py --online and tools/check
run unchanged against these repositories, offline. The same commits pushed to a public repository keep their SHAs.

ModuleAppCheck gets two commits: 1.0.0 (its tree with that version), then 1.1.0 (the tree as it is), so the update
from a previous approved commit can be tested.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

MODULES = pathlib.Path(__file__).resolve().parent / "modules"
URL_BASE = "https://github.com/module-applications-test/"
IDENTITY = {
    "GIT_AUTHOR_NAME": "module-applications tests", "GIT_AUTHOR_EMAIL": "module-applications@example.com",
    "GIT_COMMITTER_NAME": "module-applications tests", "GIT_COMMITTER_EMAIL": "module-applications@example.com",
    "GIT_AUTHOR_DATE": "2026-10-27T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-10-27T00:00:00+00:00",
}
# Versions committed before the tree as it is.
HISTORY = {"ModuleAppCheck": ["1.0.0"]}


def git(work, *args):
    environment = {**os.environ, **IDENTITY, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True, env=environment).stdout.decode().strip()


def commit_tree(work, source, message, version=None):
    for path in work.iterdir():
        if path.name != ".git":
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    shutil.copytree(source, work, dirs_exist_ok=True)
    if version:
        module_json = work / "module.json"
        module = json.loads(module_json.read_text())
        text = module_json.read_text().replace(f'"version": "{module["version"]}"', f'"version": "{version}"')
        module_json.write_text(text)
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "--no-gpg-sign", "-m", message)
    return git(work, "rev-parse", "HEAD")


def build(target):
    """Create target/<Name>.git for every module. Return {Name: [commit, ...]} oldest first."""
    target.mkdir(parents=True, exist_ok=True)
    commits = {}
    for source in sorted(p for p in MODULES.iterdir() if p.is_dir()):
        name = source.name
        bare = target / f"{name}.git"
        shutil.rmtree(bare, ignore_errors=True)
        with tempfile.TemporaryDirectory() as work:
            work = pathlib.Path(work)
            git(work, "init", "--quiet", "--initial-branch=main")
            shas = [commit_tree(work, source, f"{name} {v}", v) for v in HISTORY.get(name, [])]
            shas.append(commit_tree(work, source, f"{name}"))
            subprocess.run(["git", "clone", "--quiet", "--bare", str(work), str(bare)], check=True, capture_output=True)
        commits[name] = shas
    return commits


def environment(target):
    """GIT_CONFIG_* variables that redirect URL_BASE to the local repositories."""
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": f"url.file://{target.absolute()}/.insteadOf",
        "GIT_CONFIG_VALUE_0": URL_BASE,
        "GIT_CONFIG_KEY_1": "protocol.file.allow",
        "GIT_CONFIG_VALUE_1": "always",
    }


def remote_environment(repository, names):
    """GIT_CONFIG_* variables that send https://github.com/module-applications-test/<Name> to one repository holding
    every module (a branch each); a commit is fetched by SHA, so the branch does not matter."""
    environment = {"GIT_CONFIG_COUNT": str(len(names))}
    for i, name in enumerate(sorted(names)):
        environment[f"GIT_CONFIG_KEY_{i}"] = f"url.{repository}.insteadOf"
        environment[f"GIT_CONFIG_VALUE_{i}"] = f"{URL_BASE}{name}"
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=pathlib.Path)
    parser.add_argument("--env", action="store_true", help="print shell exports instead of the commits")
    parser.add_argument("--push", metavar="URL", help="push each module's history to the branch test-modules/<Name> of URL")
    options = parser.parse_args()
    commits = build(options.target)
    if options.push:
        for name in commits:
            subprocess.run(["git", "--git-dir", str(options.target / f"{name}.git"), "push", "--quiet", options.push,
                            f"main:refs/heads/test-modules/{name}"], check=True)
    # Bare repositories are also reachable without the .git suffix.
    for name in commits:
        link = options.target / name
        if not link.exists():
            link.symlink_to(f"{name}.git")
    if options.env:
        for key, value in environment(options.target).items():
            print(f"export {key}='{value}'")
    else:
        print(json.dumps(commits, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
