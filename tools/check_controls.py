#!/usr/bin/env python3
# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""Run the control modules through tools/check and compare with tests/modules/expected.yaml.

    tools/check_controls.py [--config FILE] [--remote URL] [--output FILE]

The control modules are ModuleAppCheck (passes, with the update from 1.0.0), ModuleAppBroken (must fail) and
ModuleAppProbe (passes only if it reaches nothing). A MikoPBX image is fit to test applications when all three behave
as expected on it. --config tries a candidate image; --remote fetches the modules from the repository that publishes
them (config/controls.yaml) instead of building local repositories from tests/modules/.

Exit codes: 0 if every control module behaves as expected, 1 otherwise.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "tests"))
import module_repos  # noqa: E402

EXPECTED = yaml.safe_load((BASE / "tests" / "modules" / "expected.yaml").read_text())
APPLICATIONS = {
    "ModuleAppCheck": BASE / "tests" / "fixtures" / "good" / "control" / "applications" / "ModuleAppCheck",
    "ModuleAppBroken": BASE / "tests" / "fixtures" / "good" / "broken-module" / "applications" / "ModuleAppBroken",
    "ModuleAppProbe": BASE / "tests" / "fixtures" / "good" / "probe-module" / "applications" / "ModuleAppProbe",
}
CODES = {"passes": 0, "fails": 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=pathlib.Path, default=BASE / "config" / "mikopbx.yaml")
    parser.add_argument("--remote", help="repository with a branch per control module")
    parser.add_argument("--output", type=pathlib.Path, help="write the summary JSON here")
    options = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="controls-") as work:
        work = pathlib.Path(work)
        if options.remote:
            environment = module_repos.remote_environment(options.remote, APPLICATIONS)
        else:
            module_repos.build(work / "repos")
            for name in APPLICATIONS:
                (work / "repos" / name).symlink_to(f"{name}.git")
            environment = module_repos.environment(work / "repos")
        environment = {**os.environ, **environment}

        summary = {"config": str(options.config), "modules": {}, "ok": True}
        for name, application in APPLICATIONS.items():
            expected = EXPECTED[name]
            expect = work / f"{name}.expect.yaml"
            expect.write_text(yaml.safe_dump({k: expected[k] for k in ("workers", "api", "probe_empty") if k in expected}))
            result = work / f"{name}.json"
            command = [sys.executable, str(BASE / "tools" / "check"), str(application), "--config", str(options.config),
                       "--expect", str(expect), "--output", str(result)]
            if "update_from" in expected:
                command += ["--previous-commit", expected["update_from"]]
            print(f"== {name}: expected outcome {expected['outcome']}", file=sys.stderr, flush=True)
            code = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL).returncode
            data = json.loads(result.read_text()) if result.exists() else {}
            as_expected = code == CODES[expected["outcome"]]
            summary["modules"][name] = {"expected": expected["outcome"], "exit_code": code, "outcome": data.get("outcome"),
                                        "as_expected": as_expected, "result": data}
            summary["ok"] &= as_expected

    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if options.output:
        options.output.write_text(text + "\n")
    for name, module in summary["modules"].items():
        print(f"{name:<16} expected {module['expected']:<7} got {module['outcome'] or 'no result'} (exit {module['exit_code']})"
              f"{'' if module['as_expected'] else '  <-- NOT AS EXPECTED'}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
