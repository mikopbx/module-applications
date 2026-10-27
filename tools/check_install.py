# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""The steps of tools/check: package a module from its commit and take it through its life in a fresh MikoPBX.

Nothing from the module runs on this machine. The repository is read from git's object store and repacked as a ZIP
by this code; the module's PHP runs only inside the MikoPBX container, which sits on a Docker network with no route
out (`docker network create --internal`), gets no volume, no Docker socket, no environment from this machine and no
credentials. Every REST call is made from inside the container to 127.0.0.1, where MikoPBX needs no password.
See docs/mikopbx-docker.md for the calls themselves.
"""
import dataclasses
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import zipfile

API = "http://127.0.0.1/pbxcore/api/v3"
MODULE_API = "http://127.0.0.1/pbxcore/api/modules"
DB = "/cf/conf/mikopbx.db"
MODULES_DIR = "/storage/usbdisk1/mikopbx/custom_modules"
LOGS = ("/storage/usbdisk1/mikopbx/log/system/messages", "/storage/usbdisk1/mikopbx/log/php")
VERSION_PLACEHOLDER = "%ModuleVersion%"
LICENCE_KEYS = ("lic_product_id", "lic_feature_id")
# Core's English messages when a paid module meets a MikoPBX without a licence key (Common/Messages/en/Modules.php).
LICENCE_REFUSAL = re.compile(r"license key to activate the license|activating a license|DisabledByLicense|license", re.I)
# A log line that means code crashed. Only lines that also name the module count against it.
CRASH = re.compile(r"PHP (Fatal|Parse) error|Fatal error|Uncaught|ErrorException|Throwable|Segmentation fault", re.I)


class Failed(Exception):
    """A step failed: the module does not pass."""


class Broken(Exception):
    """The environment failed (Docker, the image, the network): the module could not be checked."""


def say(message):
    print(message, file=sys.stderr, flush=True)


def machine():
    """This machine's architecture, named as in compatibility.architectures."""
    name = platform.machine().lower()
    return {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(name, name)


def run(command, timeout=120, check=True, stdin=None):
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        raise Broken(f"{shlex.join(command[:4])} ... timed out after {timeout}s") from None
    if check and completed.returncode != 0:
        raise Broken(f"{shlex.join(command[:4])} ... exited {completed.returncode}: {completed.stderr.decode(errors='replace').strip()[-400:]}")
    return completed


# --- The package -------------------------------------------------------------------------------------------------

def fetch(repo, commit, git_dir, timeout):
    """Fetch exactly one commit into a bare repository. Nothing is checked out."""
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
    git = ["git", f"--git-dir={git_dir}", "-c", "core.hooksPath=/dev/null"]
    subprocess.run([*git, "init", "--quiet", "--bare"], check=True, capture_output=True)
    completed = subprocess.run([*git, "fetch", "--quiet", "--no-tags", "--depth=1", repo, commit],
                               capture_output=True, timeout=timeout, env=environment)
    if completed.returncode != 0:
        raise Failed(f"cannot fetch {commit} from {repo}: {completed.stderr.decode(errors='replace').strip()[-300:]}")
    head = subprocess.run([*git, "rev-parse", "FETCH_HEAD"], capture_output=True, check=True).stdout.decode().strip()
    if head != commit:
        raise Failed(f"fetching {commit} returned {head}")
    return git


def package(repo, commit, git_dir, out_path, timeout=120, test_copy=None):
    """Build the module ZIP from the commit and return {sha256, bytes, files, version, lic_product_id, lic_feature_id}.

    The ZIP is reproducible: entries sorted, timestamps set to the commit time, permissions 0644 or 0755 only.
    %ModuleVersion% in module.json becomes 0.0.0-<12 hex of the commit>, so the version names the commit.

    For a paid module (lic_* > 0) and test_copy set, a second ZIP is written there: the same files, with
    lic_product_id and lic_feature_id set to 0 in module.json, so the module can go through its life in a MikoPBX
    without a licence key (rule LIFE-007). Its sha256 is returned as test_copy_sha256; the package is still out_path.
    """
    git = fetch(repo, commit, git_dir, timeout)
    stamp = int(subprocess.run([*git, "show", "-s", "--format=%ct", commit], capture_output=True, check=True).stdout)
    when = time.gmtime(max(stamp, 315532800))[:6]  # ZIP cannot store dates before 1980
    archive = subprocess.run([*git, "archive", "--format=tar", commit], capture_output=True, check=True).stdout
    entries = []
    module = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in sorted(tar.getmembers(), key=lambda m: m.name):
            if member.isdir():
                continue
            if not member.isfile():
                raise Failed(f"{member.name} is not a regular file (symbolic links cannot be installed)")
            data = tar.extractfile(member).read()
            if member.name == "module.json":
                text = data.decode("utf-8")
                module = json.loads(text)
                if module.get("version") == VERSION_PLACEHOLDER:
                    data = text.replace(VERSION_PLACEHOLDER, f"0.0.0-{commit[:12]}").encode("utf-8")
            entries.append((member, data))
    version = module.get("version")
    if version == VERSION_PLACEHOLDER:
        version = f"0.0.0-{commit[:12]}"
    licence = {key: licence_id(module, key) for key in LICENCE_KEYS}
    info = {**write_zip(entries, when, out_path), "files": len(entries), "version": version, **licence}
    if test_copy and any(licence.values()):
        unlicensed = [(m, without_licence(d) if m.name == "module.json" else d) for m, d in entries]
        info["test_copy_sha256"] = write_zip(unlicensed, when, test_copy)["sha256"]
        info["test_copy_diff"] = compare_test_copy(out_path, test_copy)
    return info


def compare_test_copy(package_zip, copy_zip):
    """Unpack both ZIPs and prove the test copy differs from the package only in module.json's licence ids.

    Return what differs ({"files": ["module.json"], "module_json_keys": [...]}); raise Failed on anything else.
    """
    with zipfile.ZipFile(package_zip) as package, zipfile.ZipFile(copy_zip) as copy:
        if package.namelist() != copy.namelist():
            raise Failed("the test copy does not hold the same files as the package")
        differing = [name for name in package.namelist() if package.read(name) != copy.read(name)]
        if differing != ["module.json"]:
            raise Failed(f"the test copy differs from the package in {differing}, not only in module.json")
        original, tested = json.loads(package.read("module.json")), json.loads(copy.read("module.json"))
    keys = sorted(k for k in original.keys() | tested.keys() if original.get(k) != tested.get(k))
    if not keys or set(keys) - set(LICENCE_KEYS) or any(tested.get(k) != 0 for k in keys):
        raise Failed(f"module.json in the test copy differs in {keys}, not only in licence ids set to 0")
    return {"files": differing, "module_json_keys": keys}


def write_zip(entries, when, out_path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zip_file:
        for member, data in entries:
            info = zipfile.ZipInfo(member.name, date_time=when)
            info.external_attr = (0o100755 if member.mode & 0o111 else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zip_file.writestr(info, data)
    data = open(out_path, "rb").read()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def licence_id(module, key):
    """The module's lic_* id as Core reads it (absent is 0); tools/validate.py has already refused anything but an int."""
    value = module.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def without_licence(data):
    """module.json with lic_product_id and lic_feature_id set to 0, the rest of the text untouched."""
    text = data.decode("utf-8")
    for key in LICENCE_KEYS:
        text = re.sub(rf'("{key}"\s*:\s*)\d+', r"\g<1>0", text)
    if any(licence_id(json.loads(text), key) for key in LICENCE_KEYS):
        raise Failed("cannot set the licence ids in module.json to 0 for the test copy")
    return text.encode("utf-8")


# --- The MikoPBX container ---------------------------------------------------------------------------------------

@dataclasses.dataclass
class Pbx:
    """One throw-away MikoPBX container on its own internal network."""
    image: dict
    limits: dict
    name: str = dataclasses.field(default_factory=lambda: f"mapp-{secrets.token_hex(6)}")
    retries: list = dataclasses.field(default_factory=list)

    def start(self, pull_timeout=600):
        reference = f"{self.image['image']}@{self.image['digest']}"
        say(f"Pulling {reference}")
        for attempt in range(3):
            pulled = run(["docker", "pull", "--quiet", reference], timeout=pull_timeout, check=False)
            if pulled.returncode == 0:
                break
            say(f"  pull failed ({pulled.stderr.decode(errors='replace').strip()[-200:]}), retrying")
            time.sleep(10 * (attempt + 1))
        else:
            raise Broken(f"cannot pull {reference}")
        inspect = json.loads(run(["docker", "image", "inspect", reference]).stdout)[0]
        network_id = run(["docker", "network", "create", "--internal", "--label", "module-applications=1", self.name]).stdout.decode().strip()
        self.firewall(f"br-{network_id[:12]}")
        run([
            "docker", "run", "--detach", "--name", self.name, "--hostname", "mikopbx",
            "--label", "module-applications=1", "--network", self.name,
            "--cpus", str(self.limits["cpus"]), "--memory", str(self.limits["memory"]),
            "--pids-limit", str(self.limits["pids_limit"]),
            "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            reference,
        ])
        return {
            "image": self.image["image"],
            "tag": self.image["tag"],
            "digest": self.image["digest"],
            "platform_digest": self.image.get("platforms", {}).get(machine()),
            "architecture": inspect.get("Architecture"),
            "mikopbx_version": self.image["version"],
            "isolation": self.isolation,
        }

    def firewall(self, bridge):
        """Drop everything the container sends to this machine itself.

        An --internal network has no route out, but the host still owns the bridge's gateway address, so a service
        on this machine listening on 0.0.0.0 is reachable from the container (found on 2026-10-27). The rule is
        removed in stop(). Without root (or password-less sudo) the run goes on and the result says the host was
        not firewalled; CI always has it.
        """
        prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
        rules = []
        for tool in ("iptables", "ip6tables"):
            rule = [tool, "-I", "INPUT", "-i", bridge, "-j", "DROP"]
            if run([*prefix, *rule], check=False).returncode == 0:
                rules.append([*prefix, tool, "-D", "INPUT", "-i", bridge, "-j", "DROP"])
        self.removals = rules
        self.isolation = {"network": "docker --internal", "bridge": bridge, "host_firewalled": any("iptables" in r for r in rules)}
        if not rules:
            say("  WARNING: could not add the iptables rule: the container can reach services on this machine")

    def stop(self):
        run(["docker", "rm", "--force", "--volumes", self.name], check=False)
        for removal in getattr(self, "removals", []):
            run(removal, check=False)
        run(["docker", "network", "rm", self.name], check=False)

    def sh(self, script, timeout=60, check=True):
        completed = run(["docker", "exec", self.name, "sh", "-c", script], timeout=timeout, check=False)
        if check and completed.returncode != 0:
            raise Broken(f"in the container `{script[:80]}` exited {completed.returncode}: {completed.stderr.decode(errors='replace')[-300:]}")
        return completed.stdout.decode(errors="replace")

    def api(self, method, path, body=None, timeout=60, base=API):
        command = ["docker", "exec", self.name, "curl", "-s", "-m", str(timeout), "-X", method, f"{base}/{path}"]
        if body is not None:
            command += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        output = run(command, timeout=timeout + 15, check=False).stdout.decode(errors="replace")
        try:
            return json.loads(output)
        except ValueError:
            return {"result": False, "messages": [f"not JSON: {output[:300]}"]}

    def boot(self, timeout):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self.api("GET", "system:ping", timeout=5).get("result") is True:
                seconds = round(time.monotonic() - start)
                time.sleep(15)  # the API answers before start-up finishes writing to the database
                return seconds
            time.sleep(3)
        raise Broken(f"MikoPBX did not answer system:ping within {timeout}s")

    def module_row(self, module_id):
        """(version, disabled) from m_PbxExtensionModules, or None. getList does not exist in 2026.6.34."""
        output = self.sh(f"sqlite3 -separator '|' {DB} \"select version, disabled from m_PbxExtensionModules where uniqid='{module_id}'\"").strip()
        if not output:
            return None
        version, disabled = output.split("|")[:2]
        return {"version": version, "disabled": disabled == "1"}

    def workers(self, module_id):
        """Processes running the module's PHP classes."""
        lines = self.sh("ps -o args").splitlines()
        needle = f"Modules\\{module_id}\\"
        return sorted({line.strip() for line in lines if needle in line})

    def module_dir_exists(self, module_id):
        return self.sh(f"test -d {MODULES_DIR}/{module_id} && echo yes || echo no").strip() == "yes"

    def log_size(self):
        return {path: int(self.sh(f"cat {path} {path}/* 2>/dev/null | wc -c").strip() or 0) for path in LOGS}

    def logs_since(self, sizes):
        text = ""
        for path, offset in sizes.items():
            text += self.sh(f"cat {path} {path}/* 2>/dev/null | tail -c +{offset + 1}", check=False)
        return text

    def operation(self, module_id, action, body, timeout):
        """Start a module operation and wait for it. Return the final operation record.

        SQLite's "database is locked" right after boot or after another operation is MikoPBX's own contention, not
        the module's fault: such an operation is retried twice, and the retries are counted in self.retries.
        """
        for attempt in range(3):
            try:
                return self._operation(module_id, action, body, timeout)
            except Failed as failure:
                if "database is locked" not in str(failure) or attempt == 2:
                    raise
                self.retries.append(f"{action}: {failure}")
                say(f"  {action}: database is locked, retrying")
                time.sleep(10)

    def _operation(self, module_id, action, body, timeout):
        started = self.api("POST", f"modules/{module_id}:{action}" if module_id else f"modules:{action}", body)
        operation_id = (started.get("data") or {}).get("operationId")
        if started.get("result") is not True or not operation_id:
            raise Failed(f"{action} was refused: {json.dumps(started.get('messages'))[:400]}")
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            time.sleep(2)
            recent = (self.api("GET", "modules:getOperations").get("data") or {}).get("recent") or []
            for record in recent:
                if record.get("operationId") == operation_id and record.get("terminal"):
                    if record.get("state") != "completed" or record.get("errorMessages"):
                        raise Failed(f"{action} ended {record.get('state')}: {json.dumps(record.get('errorMessages'))[:400]}")
                    return record
        raise Failed(f"{action} did not finish within {timeout}s")

    def upload(self, zip_path):
        """Copy the ZIP in and upload it through the REST API. Return the path MikoPBX stored it at."""
        upload_id = f"mapp{secrets.token_hex(6)}"
        run(["docker", "cp", str(zip_path), f"{self.name}:/tmp/{upload_id}.zip"])
        size = os.path.getsize(zip_path)
        fields = {
            "resumableIdentifier": upload_id, "resumableFilename": f"{upload_id}.zip", "resumableTotalSize": size,
            "resumableChunkNumber": 1, "resumableTotalChunks": 1, "resumableChunkSize": size,
            "resumableCurrentChunkSize": size, "file_mime_type": "application/zip",
        }
        command = ["docker", "exec", self.name, "curl", "-s", "-m", "120", f"{API}/files:upload"]
        for key, value in fields.items():
            command += ["-F", f"{key}={value}"]
        command += ["-F", f"file=@/tmp/{upload_id}.zip"]
        answer = json.loads(run(command, timeout=150).stdout or b"{}")
        path = (answer.get("data") or {}).get("filename")
        if answer.get("result") is not True or not path:
            raise Failed(f"upload was refused: {json.dumps(answer.get('messages'))[:300]}")
        for _ in range(60):
            status = self.api("GET", f"files:uploadStatus?resumableIdentifier={upload_id}").get("data") or {}
            if status.get("d_status") == "UPLOAD_COMPLETE":
                return path, upload_id
            time.sleep(1)
        raise Failed("upload did not complete within 60s")

    def install(self, zip_path, module_id, timeout):
        path, upload_id = self.upload(zip_path)
        metadata = self.api("POST", "modules:getMetadataFromPackage", {"filePath": path})
        uniqid = (metadata.get("data") or {}).get("uniqid")
        if uniqid != module_id:
            raise Failed(f"MikoPBX reads the package as {uniqid!r}, not {module_id!r}: {json.dumps(metadata.get('messages'))[:300]}")
        return self.operation(None, "installFromPackage", {"filePath": path, "fileId": upload_id}, timeout)


# --- The steps ---------------------------------------------------------------------------------------------------

class Steps:
    """Runs named steps, timing each and recording the outcome. A failed step stops the run."""

    def __init__(self):
        self.done = []

    def record(self, name, status, detail="", seconds=0.0, **extra):
        self.done.append({"name": name, "status": status, "detail": detail, "seconds": round(seconds, 1), **extra})
        say(f"[{status:>14}] {name}: {detail}")

    def run(self, name, function):
        start = time.monotonic()
        try:
            detail, extra = function()
        except Failed as failure:
            self.record(name, "fail", str(failure), time.monotonic() - start)
            raise
        except Broken as broken:
            self.record(name, "error", str(broken), time.monotonic() - start)
            raise
        self.record(name, "pass", detail, time.monotonic() - start, **extra)


def lifecycle(pbx, module_id, zip_path, previous_zip, timeouts, steps, expect, evidence, licensed_zip=None):
    """Install (or update), enable, check, disable, uninstall, check the PBX. Raise Failed on the first failure.

    For a paid module, zip_path (and previous_zip) are the test copies without licence ids and licensed_zip is the
    package itself: it is installed first to see Core refuse it for the licence, and only for the licence (LIFE-007).

    Whatever happens, `evidence` gets the module's log lines and the end of the system log of this run.
    """
    sizes = {}
    try:
        _lifecycle(pbx, module_id, zip_path, previous_zip, timeouts, steps, expect, sizes, licensed_zip)
    finally:
        if sizes:
            try:
                text = pbx.logs_since(sizes)
                needles = module_needles(module_id)
                evidence["module_log_lines"] = [line[:1000] for line in text.splitlines() if any(n in line for n in needles)][-200:]
                evidence["system_log_tail"] = [line[:1000] for line in text.splitlines()][-200:]
            except Broken:
                pass


def module_needles(module_id):
    return (f"Modules\\{module_id}\\", f"Modules\\\\{module_id}\\\\", f"custom_modules/{module_id}/", module_id)


def _lifecycle(pbx, module_id, zip_path, previous_zip, timeouts, steps, expect, sizes, licensed_zip=None):

    def boot():
        seconds = pbx.boot(timeouts["boot"])
        sizes.update(pbx.log_size())
        return f"system:ping answered after {seconds}s", {}

    steps.run("boot", boot)

    if licensed_zip is not None:
        start = time.monotonic()
        detail, extra = licence_refusal(pbx, module_id, licensed_zip, timeouts)
        steps.record("license", "license_required", detail, time.monotonic() - start, **extra)

    if previous_zip is None:
        steps.record("update", "not_applicable", "first version: there is no previously approved commit to update from")
    else:
        def update():
            pbx.install(previous_zip, module_id, timeouts["operation"])
            pbx.operation(module_id, "enable", {}, timeouts["operation"])
            before = pbx.module_row(module_id)
            pbx.install(zip_path, module_id, timeouts["operation"])
            after = pbx.module_row(module_id)
            if after is None or after["disabled"]:
                raise Failed(f"after the update the module is {'gone' if after is None else 'disabled'}")
            return f"updated in place from {before['version']} to {after['version']}, still enabled", {"from": before["version"], "to": after["version"]}

        steps.run("update", update)

    def install():
        if previous_zip is None:
            pbx.install(zip_path, module_id, timeouts["operation"])
        row = pbx.module_row(module_id)
        if row is None:
            raise Failed("the install operation completed but the module is not registered in m_PbxExtensionModules")
        if not pbx.module_dir_exists(module_id):
            raise Failed(f"the module is registered but {MODULES_DIR}/{module_id} does not exist")
        return f"registered, version {row['version']}, {'disabled' if row['disabled'] else 'enabled'}", {"version": row["version"]}

    steps.run("install", install)

    def lint():
        output = pbx.sh(f"cd {MODULES_DIR}/{module_id} && find . -name '*.php' -type f | while read f; do php -l \"$f\" >/dev/null 2>/tmp/lint.err || {{ echo \"$f\"; head -c 300 /tmp/lint.err; }}; done", timeout=300)
        if output.strip():
            raise Failed(f"php -l fails: {output.strip()[:500]}")
        return "every PHP file parses (php -l on the image's PHP)", {}

    steps.run("php_lint", lint)

    def enable():
        if pbx.module_row(module_id)["disabled"]:
            pbx.operation(module_id, "enable", {}, timeouts["operation"])
        row = pbx.module_row(module_id)
        if row is None or row["disabled"]:
            raise Failed("the enable operation completed but the module is not enabled")
        return "enabled", {}

    steps.run("enable", enable)

    def services():
        found = []
        # WorkerSafeScriptsCore starts module workers shortly after enable: wait up to 90 s for expected workers,
        # 30 s when none are expected.
        for _ in range(30 if expect.get("workers") else 10):
            found = pbx.workers(module_id)
            if found and len(found) >= expect.get("workers", 0):
                break
            time.sleep(3)
        if len(found) < expect.get("workers", 0):
            raise Failed(f"{len(found)} worker(s) running, {expect['workers']} expected")
        return f"{len(found)} worker process(es)", {"workers": found}

    steps.run("services", services)

    def module_api():
        answer = pbx.api("GET", f"{module_id}/check", base=MODULE_API)
        data = json.dumps(answer, ensure_ascii=False)[:4000]
        if answer.get("result") is True:
            if expect.get("probe_empty"):
                probe = answer.get("data") or {}
                found = {key: probe.get(key) for key in ("secret_like_env", "files", "mounts") if probe.get(key)}
                reached = sorted(k for k, v in (probe.get("network") or {}).items() if v)
                if found or reached or probe.get("dns_github") or "network" not in probe:
                    raise Failed(f"the probe got out: {json.dumps({**found, 'reached': reached, 'dns': probe.get('dns_github')})[:600]}")
                return "the probe found no secret, no host file and no route out", {"response": answer}
            return "GET /pbxcore/api/modules/<id>/check answered result=true", {"response": answer}
        if expect.get("api"):
            raise Failed(f"the module's API does not answer check: {data[:300]}")
        steps.record("module_api", "skip", "the module has no check action in its REST callback", response=answer)
        return None

    try:
        start = time.monotonic()
        outcome = module_api()
        if outcome is not None:
            steps.record("module_api", "pass", outcome[0], time.monotonic() - start, **outcome[1])
    except Failed as failure:
        steps.record("module_api", "fail", str(failure))
        raise

    def disable():
        pbx.operation(module_id, "disable", {}, timeouts["operation"])
        row = pbx.module_row(module_id)
        if row is None or not row["disabled"]:
            raise Failed("the disable operation completed but the module is still enabled")
        for _ in range(20):
            left = pbx.workers(module_id)
            if not left:
                return "disabled, no worker left", {}
            time.sleep(3)
        raise Failed(f"workers still running after disable: {left}")

    steps.run("disable", disable)

    def uninstall():
        # 2026.6.34 needs keepSettings in the body (docs/mikopbx-docker.md).
        pbx.operation(module_id, "uninstall", {"keepSettings": False}, timeouts["operation"])
        if pbx.module_row(module_id) is not None:
            raise Failed("the uninstall operation completed but the module is still registered")
        if pbx.module_dir_exists(module_id):
            raise Failed(f"the uninstall operation completed but {MODULES_DIR}/{module_id} is still there")
        return "unregistered, directory removed", {}

    steps.run("uninstall", uninstall)

    def logs():
        text = pbx.logs_since(sizes)
        needles = module_needles(module_id)
        crashes = [line for line in text.splitlines() if CRASH.search(line) and any(n in line for n in needles)]
        if crashes:
            raise Failed(f"{len(crashes)} error line(s) from the module in the logs, first: {crashes[0][:400]}")
        return "no fatal error or uncaught exception from the module", {"log_bytes": len(text)}

    steps.run("logs", logs)

    def health():
        if pbx.api("GET", "system:ping", timeout=10).get("result") is not True:
            raise Failed("MikoPBX does not answer system:ping after the module was removed")
        uptime = pbx.sh("asterisk -rx 'core show uptime'", check=False).strip()
        if "uptime" not in uptime.lower():
            raise Failed(f"Asterisk does not answer after the module was removed: {uptime[:200]}")
        return "system:ping answers, Asterisk is up", {}

    steps.run("health", health)


def licence_refusal(pbx, module_id, licensed_zip, timeouts):
    """Install the paid package as it is and show that Core refuses it for the licence alone. Leave no trace of it.

    Core without a licence key refuses to install a module with lic_product_id > 0 and keeps a module with
    lic_feature_id > 0 disabled (DisabledByLicense). Any other refusal is the module's fault and fails the run.
    """
    not_checked = "licence activation and capture, and whatever the module does only with a licence"
    try:
        pbx.install(licensed_zip, module_id, timeouts["operation"])
    except Failed as failure:
        if not LICENCE_REFUSAL.search(str(failure)):
            raise Failed(f"the package itself is refused, and not for the licence: {failure}") from None
        leftover = pbx.module_row(module_id) is not None or pbx.module_dir_exists(module_id)
        if leftover:
            raise Failed(f"Core refused the package for the licence but left the module behind: {failure}")
        return (f"without a licence key Core refuses to install the package: {str(failure)[:300]}. Not checked: {not_checked}. "
                "The steps below run on a test copy with lic_product_id and lic_feature_id set to 0"), {"refusal": str(failure)[:600]}
    # Installed: a feature-only module. Enabling must be what the licence stops.
    try:
        pbx.operation(module_id, "enable", {}, timeouts["operation"])
        refusal = None
    except Failed as failure:
        refusal = str(failure)
    row = pbx.module_row(module_id)
    reason = pbx.sh(f"sqlite3 {DB} \"select disableReason from m_PbxExtensionModules where uniqid='{module_id}'\"", check=False).strip()
    pbx.operation(module_id, "uninstall", {"keepSettings": False}, timeouts["operation"])
    if row is not None and not row["disabled"]:
        raise Failed("the paid package installed and enabled without a licence key: its licence ids do nothing")
    if refusal and not LICENCE_REFUSAL.search(refusal) and not LICENCE_REFUSAL.search(reason):
        raise Failed(f"enabling the package itself fails, and not for the licence: {refusal}")
    because = reason or refusal or "disabled"
    return (f"without a licence key the package installs but Core keeps it disabled ({because[:300]}). Not checked: {not_checked}. "
            "The steps below run on a test copy with lic_product_id and lic_feature_id set to 0"), {"refusal": because[:600]}
