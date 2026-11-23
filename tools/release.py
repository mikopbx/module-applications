#!/usr/bin/env python3
# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""Helpers of .github/workflows/release.yaml (docs/release.md). They read untrusted inputs and untrusted results as data.

    tools/release.py inputs        validate the dispatch inputs (environment), the admitted application, write the
                                   application to test (commit = the release SHA) and print GITHUB_OUTPUT lines
    tools/release.py verify-zip    the ZIP built from the SHA: module.json's moduleUniqueID and version, its sha256
    tools/release.py ai-inputs     context.json and sources.tar for `tools/ai_review.py review` (no model key here)
    tools/release.py result        assemble release-result.json from the checks, the package and the AI report

Inputs come from environment variables (JOB_ID, MODULE_ID, REPO, SHA, VERSION, ARCHITECTURES), never from the command
line, so the workflow never puts a dispatch input into a shell script.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import check_install  # noqa: E402
import validate  # noqa: E402

BASE = validate.BASE
ARCHITECTURES = ("x86_64", "arm64")
RUNNERS = {"x86_64": "ubuntu-latest", "arm64": "ubuntu-24.04-arm"}
JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# https://github.com/<owner>/<name>: no .git, no trailing slash, no port, no userinfo, no query.
REPO = re.compile(r"https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}")
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def is_repo(text):
    """https://github.com/<owner>/<name>, and the name is not . or .. and does not end in .git."""
    return bool(REPO.fullmatch(text)) and text.rsplit("/", 1)[1] not in (".", "..") and not text.endswith(".git")


class Refused(Exception):
    """An input or an admitted-application condition that stops the release."""


def check_inputs(env, root=None):
    """Validate the dispatch inputs and the admission. Return the checked values; raise Refused with the reason."""
    root = root or BASE
    job_id, module_id, repo, sha, version = (env.get(k, "") for k in ("JOB_ID", "MODULE_ID", "REPO", "SHA", "VERSION"))
    requested = env.get("ARCHITECTURES", "") or "x86_64,arm64"
    if not JOB_ID.fullmatch(job_id):
        raise Refused("job_id must be 1-64 characters of letters, digits, '.', '_' or '-', starting with a letter or digit")
    if not validate.NAME.fullmatch(module_id):
        raise Refused("module_id is not a valid module id (Module followed by letters and digits)")
    if not is_repo(repo):
        raise Refused("repo must be https://github.com/<owner>/<name> (no .git, no trailing slash, no other host)")
    if not SHA.fullmatch(sha):
        raise Refused("sha must be exactly 40 lowercase hex characters (a commit, not a branch, tag or short SHA)")
    if not validate.MODULE_VERSION.fullmatch(version):
        raise Refused("version must look like 1.2 or 1.2.3 or 1.2.3.4 (digits and dots)")
    architectures = requested.split(",")
    if any(a not in ARCHITECTURES for a in architectures) or len(set(architectures)) != len(architectures):
        raise Refused(f"architectures must be a comma-separated list without repeats of: {', '.join(ARCHITECTURES)}")

    path = root / "applications" / module_id / validate.CONFIG_FILE
    if not path.is_file() or path.is_symlink():
        raise Refused(f"{module_id} is not admitted: applications/{module_id}/application.yaml does not exist on the default branch")
    application = validate.yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(application, dict) or application.get("module_id") != module_id:
        raise Refused("the admitted application.yaml is not for this module_id")
    if application.get("repo") != repo:
        raise Refused("repo is not the repository of the admitted application")
    declared = application["compatibility"]["architectures"]
    unsupported = [a for a in architectures if a not in declared]
    if unsupported:
        raise Refused(f"the admitted application does not declare architecture(s): {', '.join(unsupported)}")
    application["commit"] = sha
    return {"job_id": job_id, "module_id": module_id, "repo": repo, "sha": sha, "version": version,
            "architectures": architectures, "application": application}


def verify_zip(path, module_id, version):
    """The ZIP's module.json must be the module's and carry `version`. Return {sha256, bytes, version}."""
    data = pathlib.Path(path).read_bytes()
    with zipfile.ZipFile(path) as archive:
        try:
            module = json.loads(archive.read("module.json"))
        except (KeyError, ValueError) as error:
            raise Refused(f"module.json is missing from the ZIP or is not JSON ({type(error).__name__})") from None
    if not isinstance(module, dict):
        raise Refused("module.json is not a JSON object")
    if module.get("moduleUniqueID") != module_id:
        raise Refused("moduleUniqueID in module.json is not the module_id of the release")
    if module.get("version") != version:
        raise Refused(f"version in module.json ({validate.printable(str(module.get('version'))[:60])}) is not the release version "
                      "(a %ModuleVersion% placeholder becomes 0.0.0-<commit>, which is not a release version)")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "version": module["version"]}


def read_results(directory, release, package_sha256, image_digests):
    """Per-architecture tools/check results, each checked against what the release says. Return {arch: result or None}.

    A result is the output of a job that ran the module's code in a container: it is accepted only if it names this
    module, this SHA, this ZIP and a MikoPBX image of config/mikopbx.yaml; it is kept as evidence, not as a credential.
    """
    results = {}
    for arch in release["architectures"]:
        path = pathlib.Path(directory) / f"check-{arch}" / "result.json"
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            results[arch] = None
            continue
        digests = {(r.get("image") or {}).get("digest") for r in result.get("runs") or []}
        if (result.get("module_id") != release["module_id"] or result.get("commit") != release["sha"]
                or (result.get("package") or {}).get("sha256") != package_sha256
                or result.get("architecture") != arch or not digests or not digests <= image_digests):
            result = {"outcome": "rejected: the result is not for this module, SHA, ZIP, architecture or MikoPBX image",
                      "architecture": arch}
        results[arch] = result
    return results


def image_digests():
    config = validate.yaml.safe_load((BASE / "config" / "mikopbx.yaml").read_text(encoding="utf-8"))
    return config["images"]


def summarise(release, package, results, ai, run):
    """The release-result.json. `passed` is true only if every requested architecture passed and the AI review passed."""
    images = image_digests()
    seen = sorted({(r.get("image") or {}).get("digest") for res in results.values() if res for r in res.get("runs") or []} - {None})
    per_arch = {}
    for arch in release["architectures"]:
        res = results.get(arch)
        per_arch[arch] = {"outcome": res.get("outcome") if res else "no result",
                          "runs": [{"mikopbx_version": r.get("mikopbx_version"), "outcome": r.get("outcome"),
                                    "image_digest": (r.get("image") or {}).get("digest"),
                                    "steps": [{"name": s.get("name"), "status": s.get("status")} for s in r.get("steps") or []]} for r in (res or {}).get("runs") or []],
                          "steps": [{"name": s.get("name"), "status": s.get("status")} for s in (res or {}).get("steps") or []]}
    checks_passed = all(v["outcome"] == "passes" for v in per_arch.values())
    ai_passed = bool(ai) and ai.get("status") == "complete" and ai.get("outcome") == "no_blockers"
    return {
        "schema_version": 1,
        "job_id": release["job_id"], "module_id": release["module_id"], "repo": release["repo"], "sha": release["sha"],
        "version": release["version"],
        "zip": {"name": f"{release['module_id']}.zip", **package},
        "architectures": per_arch,
        "ai_review": ({"status": ai.get("status"), "outcome": ai.get("outcome"), "blockers": len(ai.get("blockers") or []),
                       "cache_key": (ai.get("agent") or {}).get("cache_key"), "model": (ai.get("agent") or {}).get("model")}
                      if ai else {"status": "not_run", "outcome": "not_reviewed"}),
        "rules_version": validate.RULES_VERSION, "schema_version_application": validate.SCHEMA_VERSION,
        "mikopbx_images": [{"version": i["version"], "image": i["image"], "tag": i["tag"], "digest": i["digest"]} for i in images],
        "mikopbx_image_digests_used": seen,
        "checks_passed": checks_passed, "ai_review_passed": ai_passed, "passed": checks_passed and ai_passed,
        "workflow_run": run,
        "note": "Evidence, not a decision: the server decides publication (LIC-615).",
    }


def write_github_output(values):
    for key, value in values.items():
        text = str(value)
        if "\n" in text or "\r" in text:
            raise Refused(f"output {key} has a line break")
        print(f"{key}={text}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inputs")
    p.add_argument("--application-out", type=pathlib.Path, required=True, help="where to write the application with commit = sha")
    z = sub.add_parser("verify-zip")
    z.add_argument("zip", type=pathlib.Path)
    a = sub.add_parser("ai-inputs")
    a.add_argument("--results", type=pathlib.Path, required=True)
    a.add_argument("--zip", type=pathlib.Path, required=True)
    a.add_argument("--application", type=pathlib.Path, required=True)
    a.add_argument("--out", type=pathlib.Path, required=True)
    r = sub.add_parser("result")
    r.add_argument("--results", type=pathlib.Path, required=True)
    r.add_argument("--zip", type=pathlib.Path, required=True)
    r.add_argument("--ai-report", type=pathlib.Path)
    r.add_argument("--out", type=pathlib.Path, required=True)
    options = parser.parse_args(argv)
    env = os.environ

    try:
        if options.command == "inputs":
            checked = check_inputs(env, BASE)
            options.application_out.parent.mkdir(parents=True, exist_ok=True)
            options.application_out.write_text(validate.yaml.safe_dump(checked["application"], sort_keys=False, allow_unicode=True), encoding="utf-8")
            write_github_output({
                "job_id": checked["job_id"], "module_id": checked["module_id"], "repo": checked["repo"], "sha": checked["sha"],
                "version": checked["version"], "architectures": ",".join(checked["architectures"]),
                "matrix": json.dumps({"include": [{"arch": a, "runner": RUNNERS[a]} for a in checked["architectures"]]}),
            })
            return 0

        release = {k.lower(): env.get(k, "") for k in ("JOB_ID", "MODULE_ID", "REPO", "SHA", "VERSION")}
        release["architectures"] = (env.get("ARCHITECTURES") or "x86_64,arm64").split(",")
        if not (JOB_ID.fullmatch(release["job_id"]) and validate.NAME.fullmatch(release["module_id"]) and SHA.fullmatch(release["sha"])
                and is_repo(release["repo"]) and validate.MODULE_VERSION.fullmatch(release["version"])
                and all(x in ARCHITECTURES for x in release["architectures"])):
            raise Refused("the release inputs in the environment are malformed")

        if options.command == "verify-zip":
            info = verify_zip(options.zip, release["module_id"], release["version"])
            write_github_output(info)
            return 0

        package = verify_zip(options.zip, release["module_id"], release["version"])
        digests = {i["digest"] for i in image_digests()}
        results = read_results(options.results, release, package["sha256"], digests)

        if options.command == "ai-inputs":
            application_bytes = options.application.read_bytes()
            application = validate.yaml.safe_load(application_bytes)
            if application.get("commit") != release["sha"] or application.get("repo") != release["repo"]:
                raise Refused("the application to review is not for this repo and SHA")
            usable = {a: r for a, r in results.items() if r and "outcome" in r and not str(r["outcome"]).startswith("rejected")}
            if len(usable) != len(release["architectures"]) or any(r["outcome"] != "passes" for r in usable.values()):
                raise Refused("no AI review: the checks did not pass on every requested architecture")
            with tempfile.TemporaryDirectory() as work:
                git = check_install.fetch(release["repo"], release["sha"], pathlib.Path(work) / "git", 120)
                tar = subprocess.run([*git, "archive", "--format=tar", release["sha"]], capture_output=True, check=True).stdout
            options.out.mkdir(parents=True, exist_ok=True)
            context = {
                "module_id": release["module_id"], "publisher_id": application.get("publisher_id"), "repo": release["repo"],
                "commit": release["sha"], "package_sha256": package["sha256"], "test_copy_sha256": None,
                "application_sha256": hashlib.sha256(application_bytes).hexdigest(),
                "application_text": application_bytes.decode("utf-8", errors="replace"), "rules_version": validate.RULES_VERSION,
                "image_digest": image_digests()[0]["digest"],
                "cache_key": hashlib.sha256(f"release\n{release['sha']}\n{package['sha256']}".encode()).hexdigest(),
                "results": usable, "forced": False, "author": "release",
                "test_run": {"id": int(env["GITHUB_RUN_ID"]) if env.get("GITHUB_RUN_ID", "").isdigit() else None, "url": None,
                             "pull_request": None, "head": release["sha"], "architectures": release["architectures"],
                             "test_outcome": "passed"},
            }
            (options.out / "context.json").write_text(json.dumps(context, indent=2, ensure_ascii=False))
            (options.out / "sources.tar").write_bytes(tar)
            return 0

        ai = json.loads(options.ai_report.read_text(encoding="utf-8")) if options.ai_report and options.ai_report.is_file() else None
        if ai and (ai.get("commit") != release["sha"] or ai.get("package_sha256") != package["sha256"]):
            ai = {"status": "incomplete", "outcome": "rejected: the report is not for this SHA and ZIP"}
        run = None
        if env.get("GITHUB_SERVER_URL") and env.get("GITHUB_REPOSITORY") and env.get("GITHUB_RUN_ID", "").isdigit():
            run = {"id": int(env["GITHUB_RUN_ID"]), "attempt": int(env.get("GITHUB_RUN_ATTEMPT") or 1),
                   "url": f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}/actions/runs/{env['GITHUB_RUN_ID']}",
                   "workflow_sha": env.get("GITHUB_SHA")}
        summary = summarise(release, package, results, ai, run)
        options.out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return 0
    except Refused as refusal:
        print(f"::error::{validate.printable(str(refusal))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
