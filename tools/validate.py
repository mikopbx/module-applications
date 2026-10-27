#!/usr/bin/env python3
# Copyright 2026 Anthropic PBC
# Modified for MikoPBX module-applications, 2026.
# SPDX-License-Identifier: Apache-2.0
#
# Adapted from tools/validate.py in https://github.com/anthropics/oss-scanner (Apache-2.0): the directory and file
# rules, operator-only keys, printable() and the exit codes come from there. The schema, the registry rules and the
# checks of the module's repository are ours.
"""Check applications/<module_id>/ directories against the rules in rules/rules.yaml.

    tools/validate.py                         every application, offline: format, schema, registry
    tools/validate.py --online <module_id>    also fetch `commit` from `repo` and check module.json and the layout
    tools/validate.py --pr-paths FILE         check the paths a pull request changes (one per line); print the module_id

If there are problems, prints one line per problem to stderr and exits 1. Otherwise prints one summary line per
application and exits 0. Recommendations are printed as "warning:" lines and never change the exit code. --json
prints the whole result as JSON on stdout instead (same exit code).

Offline it never touches the network and never writes anything. --online runs `git fetch` of one commit into a
temporary directory and reads files from git's object store: nothing from the repository is checked out or run.

Requires PyYAML and jsonschema (pip install -r tools/requirements.txt).
"""
import argparse
import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

try:
    import jsonschema
    import yaml
except ImportError:
    sys.exit("tools/validate.py needs PyYAML and jsonschema: pip install -r tools/requirements.txt")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import publisher_registry  # noqa: E402

VALIDATOR_VERSION = "1"
BASE = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = json.loads((BASE / "schema" / "application.schema.json").read_text(encoding="utf-8"))
RULES = yaml.safe_load((BASE / "rules" / "rules.yaml").read_text(encoding="utf-8"))
RULES_VERSION = RULES["rules_version"]
SCHEMA_VERSION = SCHEMA["properties"]["schema_version"]["const"]

CONFIG_FILE = "application.yaml"
# Settings controlled by MIKO's operators. A pull request may not set them.
OPERATOR_KEYS = (
    "trust", "trust_level", "verified", "paid", "price", "pricing", "license_product", "lic_product_id",
    "lic_feature_id", "image", "images", "docker_image", "rules", "rules_version", "budget", "budget_usd",
    "approved", "approved_by", "approved_commit", "registered", "workflow", "workflows", "priority",
)
ALLOWED_KEYS = tuple(SCHEMA["properties"])
MAX_CONFIG_BYTES = 64 * 1024
MAX_MODULE_JSON_BYTES = 64 * 1024
# Spec zip_limits (PKG-004). The sizes are those of the files in git, a close lower bound of the unpacked size.
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
MAX_ENTRIES = 20000
# Below this Core disables the module at every upgrade (ModelsBase::MIN_MODULE_MODEL_VER, MJ-003).
MIN_MODULE_PBX_VERSION = (2020, 2, 468)

NAME = re.compile(SCHEMA["properties"]["module_id"]["pattern"])
SHA = re.compile(r"[0-9a-f]{40}")
PBX_VERSION = re.compile(r"[0-9]{4}\.[0-9]+\.[0-9]+")
MODULE_VERSION = re.compile(r"[0-9]+(\.[0-9]+){1,3}")
VERSION_PLACEHOLDER = "%ModuleVersion%"
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# One path segment of a pull request's change: applications/<name>/<file>.
PR_PATH = re.compile(r"applications/([^/]+)/([^/]+)")


@dataclasses.dataclass
class Result:
    """What is known about one application. problems block it; warnings do not."""
    module_id: str
    problems: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)
    config: dict = None
    online: dict = None
    registry: dict = None

    def problem(self, rule, message):
        self.problems.append({"rule": rule, "message": message})

    def warning(self, rule, message):
        self.warnings.append({"rule": rule, "message": message})

    def as_dict(self):
        config = self.config or {}
        return {
            "module_id": self.module_id,
            "ok": not self.problems,
            "repo": config.get("repo"),
            "commit": config.get("commit"),
            "publisher_id": config.get("publisher_id"),
            "problems": self.problems,
            "warnings": self.warnings,
            "online": self.online,
            "registry": self.registry,
        }


def version_tuple(text):
    return tuple(int(part) for part in text.split("."))


# --- The application directory and application.yaml (offline) ------------------------------------------------

def schema_message(error):
    """A short message for one jsonschema error, naming the field."""
    where = ".".join(str(part) for part in error.absolute_path)
    if error.validator == "additionalProperties":
        extra = sorted(set(error.instance) - set(error.schema.get("properties", {})))
        allowed = ", ".join(error.schema.get("properties", {}))
        return f"unknown field {'.'.join(filter(None, [where, extra[0]]))!r} (allowed: {allowed})" if extra else error.message
    if error.validator == "required":
        missing = error.message.split("'")[1] if "'" in error.message else error.message
        return f"missing {'.'.join(filter(None, [where, missing]))}"
    hints = {
        "commit": "commit must be the full 40-character commit SHA in lower case (a branch, a tag or a short SHA does not pin the code)",
        "module_id": "module_id must be Module followed by 1-60 letters or digits, for example ModuleExample",
        "repo": "repo must be https://github.com/<owner>/<repository> (one public GitHub repository, no .git suffix needed, no #fragment)",
        "support_email": "support_email must be one e-mail address",
        "schema_version": f"schema_version must be {SCHEMA_VERSION}",
        "compatibility.min_pbx_version": "compatibility.min_pbx_version must be a MikoPBX version like \"2024.1.0\" (quoted)",
        "compatibility.max_pbx_version": "compatibility.max_pbx_version must be a MikoPBX version like \"2026.1.0\" or null",
        "compatibility.architectures": "compatibility.architectures must be a non-empty list of x86_64, arm64",
        "license": "license must be an SPDX expression, for example GPL-3.0-or-later",
    }
    field = re.sub(r"\.\d+$", "", where)
    if field in hints:
        return hints[field]
    return f"{where or 'application.yaml'}: {error.message}"


def check_config(config, directory_name):
    """Yield (rule, message) for each problem with a parsed application.yaml."""
    if not isinstance(config, dict):
        yield "APP-001", f"not a mapping (top level is a {type(config).__name__})"
        return
    for key in config:
        if key in OPERATOR_KEYS:
            yield "APP-002", f"{key!r} is set by MIKO's operators, not in application.yaml"
    rest = {key: value for key, value in config.items() if key not in OPERATOR_KEYS}
    validator = jsonschema.Draft202012Validator(SCHEMA)
    for error in sorted(validator.iter_errors(rest), key=lambda e: (list(e.absolute_path), e.message)):
        rule = "APP-004" if list(error.absolute_path) == ["commit"] else "APP-001"
        yield rule, schema_message(error)

    module_id = config.get("module_id")
    if isinstance(module_id, str) and module_id != directory_name:
        yield "APP-003", f"module_id {module_id!r} must equal the directory name {directory_name!r}"
    compatibility = config.get("compatibility")
    if isinstance(compatibility, dict):
        low, high = compatibility.get("min_pbx_version"), compatibility.get("max_pbx_version")
        if all(isinstance(v, str) and PBX_VERSION.fullmatch(v) for v in (low, high)) and version_tuple(high) < version_tuple(low):
            yield "APP-009", f"compatibility.max_pbx_version {high} is below min_pbx_version {low}"


def load_application(directory):
    """Check one applications/<name>/ directory offline. Return a Result.

    This runs on pull requests from anyone, so it never reads through a symbolic link.
    """
    name, config_file = directory.name, directory / CONFIG_FILE
    result = Result(module_id=name)
    if not NAME.fullmatch(name):
        result.problem("APP-003", "the directory name must be the module_id: Module followed by 1-60 letters or digits")
        return result
    if directory.is_symlink():
        result.problem("APP-003", f"symbolic links are not accepted (applications/{name} is one)")
        return result
    if config_file.is_symlink():
        result.problem("APP-003", f"symbolic links are not accepted (applications/{name}/{CONFIG_FILE} is one)")
        return result
    if not config_file.is_file():
        result.problem("APP-003", f"applications/{name}/{CONFIG_FILE} is missing")
        return result
    beside = sorted(path.name for path in directory.iterdir() if path != config_file)
    if beside:
        result.problem("APP-003", f"only {CONFIG_FILE} may sit in applications/{name}/ (found: {', '.join(beside)})")
    if config_file.stat().st_size > MAX_CONFIG_BYTES:
        result.problem("APP-003", f"{CONFIG_FILE} is larger than {MAX_CONFIG_BYTES // 1024} KiB")
        return result
    try:
        config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as error:
        result.problem("APP-001", f"{CONFIG_FILE} does not parse: {type(error).__name__}: {str(error)[:300]}")
        return result
    result.config = config if isinstance(config, dict) else None
    for rule, message in check_config(config if config is not None else {}, name):
        result.problem(rule, message)
    return result


def check_unique(results):
    """APP-005: no two applications claim the same module_id, compared without regard to case."""
    seen = {}
    for result in results:
        for claimed in {result.module_id, (result.config or {}).get("module_id")}:
            if isinstance(claimed, str):
                seen.setdefault(claimed.lower(), set()).add(result.module_id)
    for result in results:
        for claimed in {result.module_id, (result.config or {}).get("module_id")}:
            if not isinstance(claimed, str):
                continue
            others = sorted(seen[claimed.lower()] - {result.module_id})
            if others:
                result.problem("APP-005", f"module_id {claimed!r} is already claimed by applications/{', applications/'.join(others)}/")


# --- The repository at `commit` (online) ------------------------------------------------------------------------

class Fetched:
    """One commit fetched into a temporary bare repository. Files are read from git's object store only."""

    def __init__(self, repo, commit, timeout=120):
        self.directory = tempfile.TemporaryDirectory(prefix="module-applications-")
        self.git_dir = self.directory.name
        self.commit = commit
        self.git("init", "--quiet", "--bare")
        environment = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
        self.fetch = self.git("fetch", "--quiet", "--no-tags", "--depth=1", repo, commit, check=False,
                              timeout=timeout, extra_env=environment)

    def git(self, *args, check=True, timeout=60, extra_env=None, binary=False):
        environment = {**os.environ, **(extra_env or {})}
        completed = subprocess.run(
            ["git", f"--git-dir={self.git_dir}", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True, timeout=timeout, env=environment, check=False,
        )
        if check and completed.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed: {completed.stderr.decode(errors='replace').strip()[:300]}")
        return completed if binary or not check else completed.stdout.decode(errors="replace")

    def tree(self):
        """Yield (mode, size or None, path) for every entry at the commit."""
        output = self.git("ls-tree", "-r", "-l", "-z", "--full-tree", self.commit, binary=True).stdout
        for entry in output.split(b"\0"):
            if not entry:
                continue
            meta, _, path = entry.partition(b"\t")
            mode, _kind, _blob, size = meta.split(None, 3)
            yield mode.decode(), (int(size) if size.strip().isdigit() else None), path.decode(errors="replace")

    def read(self, path, limit):
        completed = self.git("cat-file", "blob", f"{self.commit}:{path}", check=False, binary=True)
        if completed.returncode != 0:
            return None
        return completed.stdout[:limit + 1]

    def close(self):
        self.directory.cleanup()


def check_repository(result, fetched):
    """Check the package layout and module.json at the commit. Fills result.online."""
    config = result.config
    module_id = config["module_id"]
    entries = list(fetched.tree())
    paths = {path for _mode, _size, path in entries}
    total = sum(size or 0 for _mode, size, _path in entries)
    result.online = {"entries": len(entries), "bytes": total}

    bad_paths = [p for p in paths if ".." in p or p.startswith("/") or "\\" in p]
    links = [p for mode, _size, p in entries if mode == "120000"]
    submodules = [p for mode, _size, p in entries if mode == "160000"]
    if bad_paths:
        result.problem("PKG-002", f"paths with '..' or '\\' cannot be installed: {printable(', '.join(sorted(bad_paths)[:5]))}")
    if links:
        result.problem("PKG-002", f"symbolic links cannot be installed: {printable(', '.join(sorted(links)[:5]))}")
    if submodules:
        result.problem("PKG-002", f"submodules are not packaged: {printable(', '.join(sorted(submodules)[:5]))}")
    if len(entries) > MAX_ENTRIES or total > MAX_UNPACKED_BYTES:
        result.problem("PKG-004", f"the package has {len(entries)} files and {total // (1024 * 1024)} MiB; the limits are {MAX_ENTRIES} and {MAX_UNPACKED_BYTES // (1024 * 1024)} MiB")
    junk = sorted(p for p in paths if re.search(r"(^|/)(\.env|node_modules|\.idea)(/|$)|^db/.*\.db$", p))
    if junk:
        result.warning("PKG-005", f"files that should not be in a package: {printable(', '.join(junk[:5]))}")
    if "Setup/PbxExtensionSetup.php" not in paths:
        result.problem("PKG-003", "Setup/PbxExtensionSetup.php is missing")

    if "module.json" not in paths:
        result.problem("PKG-001", "module.json is missing from the repository root")
        return
    raw = fetched.read("module.json", MAX_MODULE_JSON_BYTES)
    if raw is None or len(raw) > MAX_MODULE_JSON_BYTES:
        result.problem("MJ-001", f"module.json is not a file of at most {MAX_MODULE_JSON_BYTES // 1024} KiB")
        return
    try:
        if raw.startswith(b"\xef\xbb\xbf"):
            raise ValueError("starts with a UTF-8 byte order mark")
        module = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        result.problem("MJ-001", f"module.json is not valid JSON: {printable(str(error)[:200])}")
        return
    if not isinstance(module, dict):
        result.problem("MJ-001", "module.json must be a JSON object")
        return
    result.online["module_json"] = {key: module.get(key) for key in ("moduleUniqueID", "version", "min_pbx_version", "developer", "support_email", "lic_product_id", "lic_feature_id")}

    unique_id = module.get("moduleUniqueID")
    if not isinstance(unique_id, str) or not NAME.fullmatch(unique_id):
        result.problem("MJ-002", f"module.json moduleUniqueID {printable(repr(unique_id))} must be Module followed by 1-60 letters or digits")
    elif unique_id != module_id:
        result.problem("APP-008", f"module.json moduleUniqueID is {unique_id!r}, the application's module_id is {module_id!r}")

    module_min = module.get("min_pbx_version")
    if not (isinstance(module_min, str) and PBX_VERSION.fullmatch(module_min)):
        result.problem("MJ-003", f"module.json min_pbx_version {printable(repr(module_min))} must be a MikoPBX version like \"2024.1.0\"")
    elif version_tuple(module_min) < MIN_MODULE_PBX_VERSION:
        result.problem("MJ-003", f"module.json min_pbx_version {module_min} is below 2020.2.468: Core disables such a module at every upgrade")
    else:
        declared = config["compatibility"]["min_pbx_version"]
        if version_tuple(declared) < version_tuple(module_min):
            result.problem("APP-009", f"compatibility.min_pbx_version {declared} is below module.json min_pbx_version {module_min}: Core refuses to install the module there")
        high = config["compatibility"].get("max_pbx_version")
        if high and version_tuple(high) < version_tuple(module_min):
            result.problem("APP-009", f"compatibility.max_pbx_version {high} is below module.json min_pbx_version {module_min}")

    version = module.get("version")
    if not (isinstance(version, str) and (version == VERSION_PLACEHOLDER or MODULE_VERSION.fullmatch(version))):
        result.problem("MJ-004", f"module.json version {printable(repr(version))} must be a version like 1.2.3 (or {VERSION_PLACEHOLDER}, which the build replaces)")
    for key in ("lic_product_id", "lic_feature_id"):
        value = module.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            result.problem("MJ-005", f"module.json {key} {printable(repr(value))} must be absent or a whole number ≥ 0 (0 for a free module, MIKO's product or feature id for a paid one)")
    if not (isinstance(module.get("developer"), str) and module["developer"].strip()):
        result.warning("MJ-006", "module.json developer should name who develops the module")
    module_email = module.get("support_email")
    if not (isinstance(module_email, str) and EMAIL.fullmatch(module_email)):
        result.problem("APP-010", f"module.json support_email {printable(repr(module_email))} must be an e-mail address equal to the application's support_email")
    elif module_email.lower() != config["support_email"].lower():
        result.problem("APP-010", f"module.json support_email is {module_email!r}, the application's support_email is {config['support_email']!r}")

    setup = fetched.read("Setup/PbxExtensionSetup.php", 512 * 1024)
    if setup is not None:
        text = setup.decode("utf-8", errors="replace")
        if not re.search(rf"^\s*namespace\s+Modules\\{re.escape(module_id)}\\Setup\s*;", text, re.M):
            result.problem("CODE-001", f"Setup/PbxExtensionSetup.php must declare namespace Modules\\{module_id}\\Setup")
        if not re.search(r"^\s*(?:final\s+)?class\s+PbxExtensionSetup\b", text, re.M):
            result.problem("CODE-001", "Setup/PbxExtensionSetup.php must declare class PbxExtensionSetup")
        if not re.search(r"function\s+uninstallModule\s*\(", text) and "extends PbxExtensionSetupBase" not in text:
            result.warning("CODE-004", "Setup/PbxExtensionSetup.php should implement uninstallModule() or extend PbxExtensionSetupBase")


def check_online(result, registry, timeout=120):
    """APP-007 and everything read from the repository, then the registry (APP-011)."""
    config = result.config
    fetched = Fetched(config["repo"], config["commit"], timeout=timeout)
    try:
        if fetched.fetch.returncode != 0:
            detail = fetched.fetch.stderr.decode(errors="replace").strip().splitlines()
            result.problem("APP-007", f"commit {config['commit']} cannot be fetched from {config['repo']}: {printable((detail or ['git fetch failed'])[-1][:200])}")
            return
        head = fetched.git("rev-parse", "FETCH_HEAD").strip()
        if head != config["commit"]:
            result.problem("APP-007", f"fetching {config['commit']} returned {head}")
            return
        check_repository(result, fetched)
    finally:
        fetched.close()
    verdict = registry.check(config["module_id"], config["publisher_id"], config["support_email"])
    result.registry = dataclasses.asdict(verdict)
    if verdict.status == "mismatch":
        result.problem("APP-011", verdict.detail)


# --- Pull request paths ---------------------------------------------------------------------------------------------

def check_pr_paths(paths):
    """APP-006. Return (module_id or None, problems) for the paths a pull request changes, renamed files under both names."""
    names, problems = set(), []
    for path in paths:
        match = PR_PATH.fullmatch(path)
        if match:
            names.add(match.group(1))
            continue
        shown = path if re.fullmatch(r"[A-Za-z0-9._/-]+", path) else "(a path with unusual characters)"
        problems.append(f"[APP-006] changes a file outside applications/<module_id>/: {shown}")
    if len(names) > 1:
        problems.append(f"[APP-006] changes more than one application ({', '.join(sorted(map(printable, names)))}): one application per pull request")
    name = next(iter(names)) if len(names) == 1 else None
    if name is not None and not NAME.fullmatch(name):
        problems.append("[APP-003] the directory under applications/ must be the module_id: Module followed by 1-60 letters or digits")
        name = None
    return name, problems


# --- Command line -----------------------------------------------------------------------------------------------

def printable(text):
    """Text for the terminal or a CI log: control characters (escape sequences, BEL, newlines) from an application or a
    directory name are shown as \\xNN, never sent raw."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", lambda m: f"\\x{ord(m.group()):02x}", text)


def validate(root, names=(), online=False, registry=None):
    """Validate the applications under root/applications. Return the Results for `names` (all if empty)."""
    applications = root / "applications"
    directories = sorted(p for p in applications.glob("*") if (p.is_dir() or p.is_symlink()) and not p.name.startswith("."))
    stray = sorted(p.name for p in applications.glob("*") if not p.is_dir() and not p.is_symlink() and p.name not in (".gitkeep", "README.md"))
    results = [load_application(directory) for directory in directories]
    check_unique(results)
    for name in stray:
        result = Result(module_id=name)
        result.problem("APP-003", f"applications/{name} is not a directory: an application is applications/<module_id>/{CONFIG_FILE}")
        results.append(result)
    if names:
        known = {result.module_id for result in results}
        results = [r for r in results if r.module_id in names] + [missing(n) for n in names if n not in known]
    if online:
        registry = registry or publisher_registry.default()
        for result in results:
            if not result.problems:
                check_online(result, registry)
    return results


def missing(name):
    result = Result(module_id=name)
    result.problem("APP-003", f"applications/{name}/{CONFIG_FILE} is missing")
    return result


def report(results, online):
    return {
        "validator_version": VALIDATOR_VERSION,
        "rules_version": RULES_VERSION,
        "schema_version": SCHEMA_VERSION,
        "online": online,
        "ok": all(not r.problems for r in results),
        "applications": [r.as_dict() for r in results],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("module_id", nargs="*", help="only these applications (default: all)")
    parser.add_argument("--online", action="store_true", help="also check the repository at `commit` (network)")
    parser.add_argument("--root", type=pathlib.Path, default=BASE, help="directory that holds applications/ (default: this repository)")
    parser.add_argument("--pr-paths", type=pathlib.Path, help="file with the paths a pull request changes, one per line")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    options = parser.parse_args(argv)

    if options.pr_paths:
        paths = [line for line in options.pr_paths.read_text(encoding="utf-8").splitlines() if line]
        name, problems = check_pr_paths(paths)
        if problems:
            print("\n".join(problems), file=sys.stderr)
            return 1
        print(name or "")
        return 0

    results = validate(options.root, options.module_id, options.online)
    if options.json:
        print(json.dumps(report(results, options.online), indent=2, ensure_ascii=False))
        return 0 if all(not r.problems for r in results) else 1

    problems = [printable(f"{r.module_id}: [{p['rule']}] {p['message']}") for r in results for p in r.problems]
    for r in results:
        for w in r.warnings:
            print(printable(f"warning: {r.module_id}: [{w['rule']}] {w['message']}"))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"ok: {len(results)} application(s), rules {RULES_VERSION}, schema {SCHEMA_VERSION}{', online' if options.online else ''}")
    for r in results:
        line = f"  {r.module_id:<28} {r.config['repo']}@{r.config['commit'][:12]}  -> {r.config['support_email']}"
        if r.registry:
            line += f"  (publisher card: {r.registry['status']})"
        print(printable(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
