# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""tools/check on the test modules, in Docker, compared with tests/modules/expected.yaml.

Slow (a MikoPBX container per module) and needs Docker and root or password-less sudo for iptables, so it runs only
with RUN_DOCKER_TESTS=1:

    RUN_DOCKER_TESTS=1 python3 -m pytest tests/test_check.py
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))
import module_repos  # noqa: E402

EXPECTED = yaml.safe_load((HERE / "modules" / "expected.yaml").read_text())
APPLICATIONS = {
    "ModuleAppCheck": HERE / "fixtures" / "good" / "control" / "applications" / "ModuleAppCheck",
    "ModuleAppBroken": HERE / "fixtures" / "good" / "broken-module" / "applications" / "ModuleAppBroken",
    "ModuleAppProbe": HERE / "fixtures" / "good" / "probe-module" / "applications" / "ModuleAppProbe",
    "ModuleAppPaid": HERE / "fixtures" / "good" / "paid-module" / "applications" / "ModuleAppPaid",
}
CODES = {"passes": 0, "fails": 1}

pytestmark = pytest.mark.skipif(os.environ.get("RUN_DOCKER_TESTS") != "1", reason="set RUN_DOCKER_TESTS=1 to run MikoPBX in Docker")


@pytest.fixture(scope="module")
def environment(tmp_path_factory):
    target = tmp_path_factory.mktemp("repos")
    module_repos.build(target)
    for path in list(target.glob("*.git")):
        (target / path.stem).symlink_to(path.name)
    return {**os.environ, **module_repos.environment(target)}


@pytest.mark.parametrize("module_id", sorted(APPLICATIONS))
def test_module_gets_the_expected_outcome(module_id, environment, tmp_path):
    expected = EXPECTED[module_id]
    expect = tmp_path / "expect.yaml"
    expect.write_text(yaml.safe_dump({k: expected[k] for k in ("workers", "api", "probe_empty") if k in expected}))
    command = [sys.executable, str(BASE / "tools" / "check"), str(APPLICATIONS[module_id]), "--expect", str(expect), "--output", str(tmp_path / "result.json")]
    if "update_from" in expected:
        command += ["--previous-commit", expected["update_from"]]
    done = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=1800)
    result = json.loads((tmp_path / "result.json").read_text())
    assert done.returncode == CODES[expected["outcome"]], done.stderr[-2000:]
    assert result["outcome"] == expected["outcome"]
    assert result["package"]["sha256"] and result["commit"] == yaml.safe_load((APPLICATIONS[module_id] / "application.yaml").read_text())["commit"]
    [run] = result["runs"]
    assert run["image"]["digest"].startswith("sha256:")
    # Docker Desktop (macOS) runs containers in its own VM, so there is no host iptables to set; CI is Linux.
    if sys.platform.startswith("linux"):
        assert run["image"]["isolation"]["host_firewalled"]
    steps = {step["name"]: step for step in run["steps"]}
    if expected["outcome"] == "passes":
        assert steps["health"]["status"] == "pass"
        assert steps["update"]["status"] == ("pass" if "update_from" in expected else "not_applicable")
    else:
        assert any(step["status"] == "fail" for step in run["steps"])
    if expected.get("license_required"):
        # LIFE-007: refused for the licence, never a pass; the lifecycle ran on the test copy.
        assert steps["license"]["status"] == "license_required" and "license" in steps["license"]["refusal"].lower()
        assert result["license_required"] and result["package"]["test_copy_sha256"] != result["package"]["sha256"]
    else:
        assert "license" not in steps and "license_required" not in result
