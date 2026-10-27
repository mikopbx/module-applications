#!/usr/bin/env python3
# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""The AI review (stage 3): MIKO's analyser reads the module's source as data and reports blockers and
recommendations with evidence. It never approves anything; a person decides.

    tools/ai_review.py prepare --repo OWNER/NAME --run-id ID --out DIR [--force]
        Check where the test results come from (GitHub API), rebuild the package, fetch the source, look for a
        cached report and the rate limit. Writes DIR/context.json and DIR/sources.tar; needs GITHUB_TOKEN (read).
    tools/ai_review.py review --inputs DIR --out DIR
        Run the model on DIR (needs the provider's key, e.g. ANTHROPIC_API_KEY) and write report.json and report.md.
    tools/ai_review.py render REPORT.json
    tools/ai_review.py current REPORT.json [--root .]
        Exit 0 if the report still applies to applications/<id>/ at the root, the rules and the configuration.

The order is fixed: no review unless the deterministic checks and the tests passed (--force only by a person, for
calibration), and a review that times out, runs out of budget, is refused, or answers anything but valid JSON is
`incomplete`, which never counts as passed.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import ai_providers  # noqa: E402
import check_install  # noqa: E402
import validate  # noqa: E402

BASE = validate.BASE
CONFIG = BASE / "config" / "ai-review.yaml"
POLICY = BASE / "ai" / "policy.md"
REPORT_SCHEMA = json.loads((BASE / "schema" / "ai-report.schema.json").read_text(encoding="utf-8"))
TEST_WORKFLOW = ".github/workflows/test.yaml"
REVIEW_WORKFLOW = ".github/workflows/ai-review.yaml"
MAX_FILE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_SOURCE_FILES = 5000

# What the model must answer. The report adds everything else from MIKO's code.
ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "checks_performed", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "checks_performed": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "rule", "file", "line", "evidence", "explanation"],
                "properties": {
                    "kind": {"type": "string", "enum": ["blocker", "recommendation"]},
                    "rule": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "evidence": {"type": "string"},
                    "explanation": {"type": "string"},
                },
            },
        },
    },
}


def sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def load_config(path=CONFIG):
    return validate.yaml.safe_load(path.read_text(encoding="utf-8"))


def config_hash(config):
    return sha256(json.dumps(config, sort_keys=True))


def policy_hash():
    return sha256(POLICY.read_bytes())


def cache_key(commit, package_sha256, application_sha256, rules_version, image_digest, config):
    """The review may be reused only when every input that shapes it is the same."""
    parts = [commit, package_sha256, application_sha256, rules_version, image_digest, config_hash(config), policy_hash()]
    return sha256("\n".join(parts))


# --- The source and the tools over it --------------------------------------------------------------------------

def source_tar(repo, commit, git_dir):
    """`git archive` of the commit: the source as data. Nothing is checked out and nothing runs."""
    git = check_install.fetch(repo, commit, git_dir, timeout=120)
    import subprocess
    return subprocess.run([*git, "archive", "--format=tar", commit], capture_output=True, check=True).stdout


def extract(tar_bytes, root):
    """Regular files only, no absolute or parent paths, within the size limits."""
    root.mkdir(parents=True, exist_ok=True)
    total = files = 0
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        for member in tar.getmembers():
            name = member.name
            if not member.isfile() or name.startswith("/") or ".." in pathlib.PurePosixPath(name).parts:
                continue
            total += member.size
            files += 1
            if total > MAX_SOURCE_BYTES or files > MAX_SOURCE_FILES:
                raise ValueError("the source is larger than the review handles")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
    return root


class ToolBox:
    """Read-only tools over one directory. Paths are relative; nothing outside the root, no symbolic links."""

    def __init__(self, root, max_read_bytes):
        self.root = pathlib.Path(root).resolve()
        self.budget = max_read_bytes
        self.read_bytes = 0

    def specs(self):
        path = {"type": "string", "description": "path relative to the module root; '.' for the root"}
        return [
            {"name": "list_files", "description": "List files (with sizes) under a directory of the module, recursively, at most 500.",
             "input_schema": {"type": "object", "additionalProperties": False, "required": ["path"], "properties": {"path": path}}},
            {"name": "read_file", "description": "Read lines start_line..end_line (1-based, inclusive; end_line 0 = to the end, at most 2000 lines) of a text file, with line numbers.",
             "input_schema": {"type": "object", "additionalProperties": False, "required": ["path", "start_line", "end_line"],
                              "properties": {"path": path, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}}},
            {"name": "grep", "description": "Search text files under a path for a Python regular expression (case-insensitive); returns file:line: text, at most 200 matches.",
             "input_schema": {"type": "object", "additionalProperties": False, "required": ["pattern", "path"],
                              "properties": {"pattern": {"type": "string"}, "path": path}}},
        ]

    def resolve(self, relative):
        if not isinstance(relative, str) or "\x00" in relative:
            raise ValueError("bad path")
        candidate = (self.root / relative.lstrip("/")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("path outside the module")
        if candidate.is_symlink():
            raise ValueError("symbolic link")
        return candidate

    def text(self, path):
        data = path.read_bytes()[:MAX_FILE_BYTES]
        if b"\x00" in data[:8192]:
            raise ValueError("binary file")
        return data.decode("utf-8", errors="replace")

    def charge(self, text):
        self.read_bytes += len(text)
        if self.read_bytes > self.budget:
            raise ValueError("read budget for this review is spent")
        return text

    def execute(self, name, arguments):
        """(result text, is_error). Results are framed as untrusted data."""
        try:
            if name == "list_files":
                base = self.resolve(arguments["path"])
                entries = sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink()) if base.is_dir() else []
                lines = [f"{p.relative_to(self.root)}  {p.stat().st_size}" for p in entries[:500]]
                more = f"\n... {len(entries) - 500} more" if len(entries) > 500 else ""
                return self.charge("\n".join(lines) + more or "(no files)"), False
            if name == "read_file":
                path = self.resolve(arguments["path"])
                lines = self.text(path).splitlines()
                start = max(1, int(arguments["start_line"]))
                end = int(arguments["end_line"]) or len(lines)
                end = min(end, len(lines), start + 1999)
                body = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1))
                return self.charge(f'<untrusted_file path="{path.relative_to(self.root)}" lines="{start}-{end}" total="{len(lines)}">\n{body}\n</untrusted_file>'), False
            if name == "grep":
                pattern = re.compile(arguments["pattern"], re.I)
                base = self.resolve(arguments["path"])
                matches = []
                files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file() and not p.is_symlink())
                for file in files:
                    try:
                        for number, line in enumerate(self.text(file).splitlines(), 1):
                            if pattern.search(line):
                                matches.append(f"{file.relative_to(self.root)}:{number}: {line[:300]}")
                                if len(matches) >= 200:
                                    break
                    except ValueError:
                        continue
                    if len(matches) >= 200:
                        break
                return self.charge("<untrusted_grep>\n" + ("\n".join(matches) or "(no matches)") + "\n</untrusted_grep>"), False
            return f"unknown tool {name}", True
        except OSError as error:
            return f"error: {error.strerror or type(error).__name__}", True
        except (ValueError, re.error, KeyError, TypeError) as error:
            return f"error: {error}", True


# --- The review ------------------------------------------------------------------------------------------------

def first_message(context, rules_text):
    """Application, rules and test results. All of it except the rules is untrusted."""
    tests = []
    for arch, result in sorted(context["results"].items()):
        runs = result.get("runs") or [{}]
        steps = [f"  {s.get('name')}: {s.get('status')} - {str(s.get('detail', ''))[:300]}" for s in runs[0].get("steps", [])]
        evidence = (runs[0].get("evidence") or {}).get("module_log_lines") or []
        tests.append(f"## {arch}: {result.get('outcome')}\n" + "\n".join(steps) +
                     ("\nModule log lines:\n" + "\n".join(line[:300] for line in evidence[-40:]) if evidence else ""))
    return (
        "Review the module whose source your tools read.\n\n"
        f"# MIKO's rules (trusted), version {context['rules_version']}\n\n```yaml\n{rules_text}\n```\n\n"
        "# The application (untrusted)\n\n<untrusted_application>\n" + context["application_text"] + "\n</untrusted_application>\n\n"
        "# Results of the automatic tests (produced by MIKO's checks; the log lines inside come from the module)\n\n"
        "<untrusted_test_results>\n" + "\n\n".join(tests) + "\n</untrusted_test_results>\n"
    )


def check_findings(answer, root, rules):
    """Split the model's findings into verified blockers and recommendations, and the ones MIKO's code rejects."""
    by_id = {rule["id"]: rule for rule in rules["rules"] if rule.get("status") != "withdrawn"}
    blockers, recommendations, rejected = [], [], []
    root = pathlib.Path(root).resolve()
    for finding in answer.get("findings", []):
        rule_id = (finding.get("rule") or "").strip() or None
        item = {"rule": rule_id, "level": by_id[rule_id]["level"] if rule_id in by_id else None, "file": finding.get("file", ""),
                "line": max(0, int(finding.get("line") or 0)), "evidence": finding.get("evidence", "")[:2000],
                "explanation": finding.get("explanation", "")[:4000]}
        reason = None
        if rule_id and rule_id not in by_id:
            reason = f"unknown rule {rule_id}"
        else:
            try:
                path = (root / item["file"].lstrip("/")).resolve()
                inside = path == root or root in path.parents
            except (OSError, ValueError):
                inside = False
            if not inside:
                reason = "file outside the module"
            elif item["line"] > 0:
                if not path.is_file():
                    reason = "file not found"
                else:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    window = "\n".join(lines[max(0, item["line"] - 3):item["line"] + 2])
                    quote = [q.strip() for q in item["evidence"].splitlines() if q.strip()]
                    if item["line"] > len(lines):
                        reason = f"line {item['line']} is past the end of the file"
                    elif not quote or not all(q in window for q in quote[:3]):
                        reason = "the evidence is not at that line"
        if reason:
            rejected.append({**finding, "rejected": reason})
        elif finding.get("kind") == "blocker" and item["level"] == "mandatory":
            blockers.append(item)
        else:
            recommendations.append(item)
    return blockers, recommendations, rejected


def build_report(context, config, result, root, rules):
    """The report: trusted metadata from `context`, findings from the model after MIKO's checks."""
    report = {
        "schema_version": 1,
        "status": "incomplete",
        "incomplete_reason": result.reason or None,
        "outcome": "not_reviewed",
        "module_id": context["module_id"],
        "publisher_id": context.get("publisher_id"),
        "repo": context["repo"],
        "commit": context["commit"],
        "package_sha256": context["package_sha256"],
        "test_copy_sha256": context.get("test_copy_sha256"),
        "application_sha256": context["application_sha256"],
        "rules_version": context["rules_version"],
        "image_digest": context["image_digest"],
        "test_run": context["test_run"],
        "agent": {
            "provider": config["provider"], "model": config["model"], "answered_by": result.answered_by,
            "effort": config["effort"], "config_hash": config_hash(config), "policy_sha256": policy_hash(),
            "cache_key": context["cache_key"], "cached_from_run": None,
        },
        "checks_performed": [],
        "blockers": [],
        "recommendations": [],
        "rejected_findings": [],
        "summary": "",
        "usage": result.usage.as_dict(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if result.status != "complete":
        return report
    try:
        answer = json.loads(result.text)
        validate.jsonschema.validate(answer, ANSWER_SCHEMA)
    except (ValueError, validate.jsonschema.ValidationError) as error:
        report["incomplete_reason"] = f"the answer is not the required JSON ({type(error).__name__})"
        return report
    blockers, recommendations, rejected = check_findings(answer, root, rules)
    report.update(status="complete", incomplete_reason=None, outcome="blockers_found" if blockers else "no_blockers",
                  checks_performed=[c[:300] for c in answer["checks_performed"][:100]], blockers=blockers,
                  recommendations=recommendations, rejected_findings=rejected, summary=answer["summary"][:4000])
    return report


def review(context, root, provider, config):
    rules_text = (BASE / "rules" / "rules.yaml").read_text(encoding="utf-8")
    rules = validate.yaml.safe_load(rules_text)
    limits = ai_providers.Limits(**{k: config["limits"][k] for k in ("max_turns", "max_cost_usd", "timeout_seconds", "max_output_tokens", "retries")})
    toolbox = ToolBox(root, config["limits"]["max_read_bytes"])
    result = provider.run(POLICY.read_text(encoding="utf-8"), first_message(context, rules_text), toolbox, ANSWER_SCHEMA, limits)
    report = build_report(context, config, result, root, rules)
    validate.jsonschema.validate(report, REPORT_SCHEMA)
    return report


def passed(report):
    """What a person may lean on: a complete review that found no blockers. Anything else is not a pass."""
    return report.get("status") == "complete" and report.get("outcome") == "no_blockers"


def current(report, root=BASE, config=None):
    """Problems that make `report` stale against the application, rules and configuration at `root` (empty: current)."""
    config = config or load_config(root / "config" / "ai-review.yaml")
    problems = []
    application = root / "applications" / report["module_id"] / "application.yaml"
    if not application.is_file():
        return [f"applications/{report['module_id']}/application.yaml is gone"]
    data = application.read_bytes()
    parsed = validate.yaml.safe_load(data) or {}
    if sha256(data) != report["application_sha256"]:
        problems.append("the application changed since the review")
    if parsed.get("commit") != report["commit"]:
        problems.append(f"the application names commit {parsed.get('commit')}, the review was of {report['commit']}")
    rules_version = validate.yaml.safe_load((root / "rules" / "rules.yaml").read_text())["rules_version"]
    if rules_version != report["rules_version"]:
        problems.append(f"the rules are {rules_version}, the review used {report['rules_version']}")
    digest = validate.yaml.safe_load((root / "config" / "mikopbx.yaml").read_text())["images"][0]["digest"]
    if digest != report["image_digest"]:
        problems.append("the MikoPBX test image changed since the tests the review relied on")
    if config_hash(config) != report["agent"]["config_hash"]:
        problems.append("the AI review configuration changed")
    if (root / "ai" / "policy.md").exists() and sha256((root / "ai" / "policy.md").read_bytes()) != report["agent"]["policy_sha256"]:
        problems.append("the AI review policy changed")
    return problems


def render(report):
    """Markdown for the pull request. External text is escaped like the triage comment's."""
    md = _md
    status = {"complete": "complete", "incomplete": "**INCOMPLETE — not a pass**"}[report["status"]]
    outcome = {"no_blockers": "no blockers found", "blockers_found": f"**{len(report['blockers'])} blocker(s)**", "not_reviewed": "not reviewed"}[report["outcome"]]
    usage = report["usage"]
    lines = [
        "<!-- module-applications-ai-review -->",
        f"### AI review of `{md(report['module_id'])}` at `{report['commit'][:12]}`",
        "",
        f"Status: {status}. Outcome: {outcome}." + (f" Reason: {md(report['incomplete_reason'])}." if report.get("incomplete_reason") else ""),
        "",
    ]
    if report["summary"]:
        lines += [md(report["summary"], 4000), ""]
    for title, items in (("Blockers", report["blockers"]), ("Recommendations", report["recommendations"])):
        if items:
            lines += [f"**{title}**", ""]
            for item in items:
                where = f"{md(item['file'])}:{item['line']}" if item["line"] else md(item["file"])
                lines.append(f"- [{md(item['rule'] or '—')}] `{where}` — {md(item['explanation'], 600)}")
            lines.append("")
    if report["rejected_findings"]:
        lines += [f"{len(report['rejected_findings'])} finding(s) were discarded because MIKO's code could not find their evidence (in the report artifact).", ""]
    lines += [
        "| | |", "|---|---|",
        f"| Package sha256 | `{report['package_sha256']}` |",
        *([f"| Paid module: test copy sha256 | `{report['test_copy_sha256']}` (the package with module.json's licence ids set to 0; licence-gated behaviour not tested) |"]
          if report.get("test_copy_sha256") else []),
        f"| Rules / image | {md(report['rules_version'])} / `{md(report['image_digest'][:19])}…` |",
        f"| Tests | run {report['test_run'].get('id')}: {md(report['test_run'].get('test_outcome'))} on {', '.join(report['test_run'].get('architectures', []))} |",
        f"| Model | {md(report['agent']['model'])}" + (f" (answered by {md(report['agent']['answered_by'])})" if report['agent'].get('answered_by') and report['agent']['answered_by'] != report['agent']['model'] else "") + f", effort {md(report['agent']['effort'])} |",
        f"| Usage | {usage['turns']} turns, {usage['tool_calls']} tool calls, {usage['input_tokens'] + usage['cache_read_input_tokens'] + usage['cache_creation_input_tokens']} input / {usage['output_tokens']} output tokens, ~${usage['cost_usd_estimate']:.2f} (estimate), {usage['duration_seconds']:.0f}s |",
        "",
        "The AI review finds problems; it approves nothing. MIKO decides on this exact commit. A new commit, a change of the application, the rules, the test image or the review configuration makes this report stale.",
    ]
    if report["agent"].get("cached_from_run"):
        lines.append(f"Reused from AI review run {report['agent']['cached_from_run']}: every input that shapes the review is the same.")
    return "\n".join(lines)


def _md(value, limit=300):
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))[:limit]
    text = re.sub(r"([\\`*_{}\[\]()<>#+!|~])", r"\\\1", text)
    return text.replace("@", "@​").replace("://", ":​//")


# --- Provenance (prepare) --------------------------------------------------------------------------------------

class GitHub:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def request(self, url, accept="application/vnd.github+json"):
        """GET with the token. A redirect (artifact downloads go to blob storage) is followed without the token."""
        headers = {"Accept": accept, "User-Agent": "module-applications-ai-review", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        opener = urllib.request.build_opener(self._NoRedirect)
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
                return response.read(200 * 1024 * 1024)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location") if error.code in (301, 302, 303, 307, 308) else None
            if not location:
                raise
        with urllib.request.urlopen(urllib.request.Request(location, headers={"User-Agent": "module-applications-ai-review"}), timeout=120) as response:
            return response.read(200 * 1024 * 1024)

    def get(self, path, accept="application/vnd.github+json"):
        body = self.request(f"https://api.github.com/repos/{self.repo}/{path}", accept)
        return json.loads(body) if accept.endswith("json") else body

    def artifact(self, artifact):
        """The files of one artifact (a ZIP), as {name: bytes}."""
        data = self.request(artifact["archive_download_url"])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {name: archive.read(name) for name in archive.namelist() if archive.getinfo(name).file_size < 20 * 1024 * 1024}


class Skip(Exception):
    """No review: the reason is reported, and nothing is spent."""


class NoApplication(Exception):
    """A maintenance pull request that changes no application: the review has nothing to do and says so."""

    def __init__(self, pull_request, head):
        super().__init__(f"pull request #{pull_request} changes no application")
        self.pull_request, self.head = pull_request, head


def maintenance(github, run):
    """Raise NoApplication if the test run was for an owners' or members' pull request touching no application, or for
    the image update's App (IMAGE_UPDATE_BOT) changing config/mikopbx.yaml only.

    Checked through the API (the pull request's author, head and files), not inferred from a missing artifact."""
    for listed in run.get("pull_requests") or []:
        pull = github.get(f"pulls/{listed['number']}")
        if pull.get("state") != "open" or pull["head"]["sha"] != listed["head"]["sha"]:
            continue
        files = github.get(f"pulls/{listed['number']}/files?per_page=100")
        bot = os.environ.get("IMAGE_UPDATE_BOT")
        if bot and pull.get("user", {}).get("login") == bot and [f["filename"] for f in files] == ["config/mikopbx.yaml"] \
                and not any(f.get("previous_filename") for f in files):
            raise NoApplication(listed["number"], pull["head"]["sha"])
        if pull.get("author_association") not in ("OWNER", "MEMBER"):
            continue
        if pull.get("changed_files", 0) <= 100 and not any(
                f["filename"].startswith("applications/") or str(f.get("previous_filename", "")).startswith("applications/") for f in files):
            raise NoApplication(listed["number"], pull["head"]["sha"])


def prepare(github, run_id, out, force=False, config=None, now=None):
    """Check the test run against the GitHub API and write the review's inputs. Raise Skip with a reason otherwise."""
    config = config or load_config()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    run = github.get(f"actions/runs/{run_id}")
    if not str(run.get("path", "")).startswith(TEST_WORKFLOW) or run.get("repository", {}).get("full_name") != github.repo:
        raise Skip(f"run {run_id} is not this repository's {TEST_WORKFLOW}")
    if run.get("event") not in ("pull_request_target", "workflow_dispatch") or run.get("status") != "completed":
        raise Skip(f"run {run_id} is not a finished test of a pull request")
    artifacts = {a["name"]: a for a in github.get(f"actions/runs/{run_id}/artifacts?per_page=100")["artifacts"] if not a.get("expired")}
    if "provenance" not in artifacts:
        maintenance(github, run)
        raise Skip("the test run has no provenance (no application was tested)")
    provenance = json.loads(github.artifact(artifacts["provenance"])["provenance.json"])
    module_id, number = provenance["module_id"], int(provenance["pull_request"])
    if not validate.NAME.fullmatch(module_id) or not re.fullmatch(r"[0-9a-f]{40}", provenance["head"]):
        raise Skip("the provenance is malformed")

    pull = github.get(f"pulls/{number}")
    if pull.get("state") != "open":
        raise Skip(f"pull request #{number} is not open")
    if pull["head"]["sha"] != provenance["head"]:
        raise Skip(f"pull request #{number} moved to {pull['head']['sha'][:12]} after the test of {provenance['head'][:12]}: the results are stale")
    application_bytes = github.get(f"contents/applications/{module_id}/application.yaml?ref={provenance['head']}", accept="application/vnd.github.raw")
    if sha256(application_bytes) != provenance["application_sha256"]:
        raise Skip("the application at the pull request's head is not the one the tests used")
    application = validate.yaml.safe_load(application_bytes)

    jobs = github.get(f"actions/runs/{run_id}/jobs?per_page=100")["jobs"]
    architectures = application["compatibility"]["architectures"]
    conclusions = {arch: next((j["conclusion"] for j in jobs if j["name"] == f"module ({arch})"), None) for arch in architectures}
    test_outcome = "passed" if all(c == "success" for c in conclusions.values()) else \
        "failed on " + ", ".join(f"{a} ({c})" for a, c in conclusions.items() if c != "success")
    if test_outcome != "passed" and not force:
        raise Skip(f"the tests did not pass ({test_outcome}): no AI review after a failed cheap check")

    with tempfile.TemporaryDirectory() as work:
        work = pathlib.Path(work)
        package = check_install.package(application["repo"], application["commit"], work / "git-zip", work / "module.zip",
                                        test_copy=work / "module.test.zip")
        tar = source_tar(application["repo"], application["commit"], work / "git-tar")
    image_digest = validate.yaml.safe_load((BASE / "config" / "mikopbx.yaml").read_text())["images"][0]["digest"]
    results = {}
    for arch in architectures:
        artifact = artifacts.get(f"result-{module_id}-{arch}")
        if artifact is None:
            if conclusions[arch] == "success":
                raise Skip(f"the {arch} result is missing although its job succeeded")
            continue
        result = json.loads(github.artifact(artifact)["result.json"])
        # The result was written on a runner that ran the module: check it against what we know independently.
        if result.get("commit") != application["commit"] or result.get("module_id") != module_id:
            raise Skip(f"the {arch} result is for another module or commit: not accepted")
        if (result.get("package") or {}).get("sha256") != package["sha256"]:
            raise Skip(f"the {arch} result names package {str((result.get('package') or {}).get('sha256'))[:12]}, the commit builds {package['sha256'][:12]}: not accepted")
        if (result.get("package") or {}).get("test_copy_sha256") != package.get("test_copy_sha256"):
            raise Skip(f"the {arch} result names test copy {str((result.get('package') or {}).get('test_copy_sha256'))[:12]}, the commit builds {str(package.get('test_copy_sha256'))[:12]}: not accepted")
        digests = {(r.get("image") or {}).get("digest") for r in result.get("runs", [])}
        if digests != {image_digest}:
            raise Skip(f"the {arch} result was not obtained on the current MikoPBX image: stale")
        if (result.get("outcome") == "passes") != (conclusions[arch] == "success"):
            raise Skip(f"the {arch} result says {result.get('outcome')} but its job concluded {conclusions[arch]}: not accepted")
        results[arch] = result

    rules_version = validate.RULES_VERSION
    application_sha = sha256(application_bytes)
    key = cache_key(application["commit"], package["sha256"], application_sha, rules_version, image_digest, config)
    context = {
        "module_id": module_id, "publisher_id": application.get("publisher_id"), "repo": application["repo"],
        "commit": application["commit"], "package_sha256": package["sha256"], "test_copy_sha256": package.get("test_copy_sha256"),
        "application_sha256": application_sha,
        "application_text": application_bytes.decode("utf-8", errors="replace"), "rules_version": rules_version,
        "image_digest": image_digest, "cache_key": key, "results": results, "forced": bool(force and test_outcome != "passed"),
        "test_run": {"id": int(run_id), "url": run.get("html_url"), "pull_request": number, "head": provenance["head"],
                     "architectures": architectures, "test_outcome": test_outcome},
    }

    # Rate limit: requests recorded by earlier runs of this workflow for this pull request in the last day.
    since = now - datetime.timedelta(days=1)
    recent = github.get(f"actions/artifacts?name=ai-request-pr-{number}&per_page=100").get("artifacts", [])
    count = sum(1 for a in recent if _when(a.get("created_at")) >= since)
    limit = config["limits"]["max_runs_per_pull_request_per_day"]

    # Cache: a report from this workflow with the same key.
    cached = None
    for artifact in github.get(f"actions/artifacts?name=ai-report-{key[:40]}&per_page=10").get("artifacts", []):
        source_run = github.get(f"actions/runs/{artifact['workflow_run']['id']}")
        if artifact.get("expired") or not str(source_run.get("path", "")).startswith(REVIEW_WORKFLOW):
            continue
        report = json.loads(github.artifact(artifact)["report.json"])
        if report.get("agent", {}).get("cache_key") == key and report.get("status") == "complete":
            report["agent"]["cached_from_run"] = artifact["workflow_run"]["id"]
            cached = report
            break
    if cached is None and count >= limit:
        raise Skip(f"pull request #{number} had {count} AI reviews in the last day (limit {limit})")

    out.mkdir(parents=True, exist_ok=True)
    (out / "context.json").write_text(json.dumps(context, indent=2, ensure_ascii=False))
    (out / "sources.tar").write_bytes(tar)
    if cached:
        (out / "cached-report.json").write_text(json.dumps(cached, indent=2, ensure_ascii=False))
    return context, cached


def _when(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


# --- Calibration -----------------------------------------------------------------------------------------------

def score(report, entry):
    """Caught / missed expectations and false positives of one calibration review."""
    caught, missed = [], []
    for want in entry.get("must_find", []):
        hit = next((b for b in report["blockers"] if b["rule"] in want["rules"] and b["file"] == want["file"]
                    and (want["line"] == 0 or abs(b["line"] - want["line"]) <= 3)), None)
        (caught if hit else missed).append(want)
    false_positives = report["blockers"] if entry.get("blockers") == "none" else []
    return {"caught": caught, "missed": missed, "false_positives": false_positives}


def calibrate(out, provider=None, config=None, calibration=None):
    """Review every module of config/ai-calibration.yaml and score it. Stops starting new reviews at the budget."""
    config = config or load_config()
    calibration = calibration or validate.yaml.safe_load((BASE / "config" / "ai-calibration.yaml").read_text())
    provider = provider or ai_providers.make(config)
    image_digest = validate.yaml.safe_load((BASE / "config" / "mikopbx.yaml").read_text())["images"][0]["digest"]
    out.mkdir(parents=True, exist_ok=True)
    summary = {"model": config["model"], "effort": config["effort"], "rules_version": validate.RULES_VERSION,
               "budget_usd": calibration["budget_usd"], "spent_usd_estimate": 0.0, "modules": []}
    for entry in calibration["modules"]:
        row = {"module_id": entry["module_id"], "commit": entry["commit"], "note": entry.get("note", "")}
        summary["modules"].append(row)
        if summary["spent_usd_estimate"] >= calibration["budget_usd"]:
            row["status"] = "not run: calibration budget spent"
            continue
        with tempfile.TemporaryDirectory(prefix="calibrate-") as work:
            work = pathlib.Path(work)
            package = check_install.package(entry["repo"], entry["commit"], work / "g1", work / "m.zip")
            root = extract(source_tar(entry["repo"], entry["commit"], work / "g2"), work / "src")
            application_text = (f"# calibration entry (MIKO's config), not an application\nmodule_id: {entry['module_id']}\n"
                                f"repo: {entry['repo']}\ncommit: {entry['commit']}\n")
            context = {
                "module_id": entry["module_id"], "publisher_id": None, "repo": entry["repo"], "commit": entry["commit"],
                "package_sha256": package["sha256"], "application_sha256": sha256(application_text),
                "application_text": application_text, "rules_version": validate.RULES_VERSION, "image_digest": image_digest,
                "cache_key": cache_key(entry["commit"], package["sha256"], sha256(application_text), validate.RULES_VERSION, image_digest, config),
                "results": {}, "forced": True,
                "test_run": {"id": None, "url": None, "pull_request": None, "head": None, "architectures": [],
                             "test_outcome": "not run (calibration)"},
            }
            report = review(context, root, provider, config)
        (out / f"{entry['module_id']}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        (out / f"{entry['module_id']}.md").write_text(render(report) + "\n")
        result = score(report, entry)
        summary["spent_usd_estimate"] = round(summary["spent_usd_estimate"] + report["usage"]["cost_usd_estimate"], 4)
        row.update(status=report["status"], incomplete_reason=report.get("incomplete_reason"), outcome=report["outcome"],
                   blockers=[f"{b['rule']} {b['file']}:{b['line']}" for b in report["blockers"]],
                   recommendations=len(report["recommendations"]), rejected=len(report["rejected_findings"]),
                   caught=len(result["caught"]), missed=[f"{m['rules']} {m['file']}:{m['line']}" for m in result["missed"]],
                   false_positives=[f"{b['rule']} {b['file']}:{b['line']}" for b in result["false_positives"]],
                   usage=report["usage"])
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


def render_calibration(summary):
    lines = [f"### AI review calibration: {summary['model']}, effort {summary['effort']}, rules {summary['rules_version']}",
             "", f"Spent (estimate): ${summary['spent_usd_estimate']:.2f} of ${summary['budget_usd']:.2f}", "",
             "| Module | Status | Outcome | Caught | Missed | False positives | Turns | Tokens in/out | ~USD | s |", "|---|---|---|---|---|---|---|---|---|---|"]
    for row in summary["modules"]:
        u = row.get("usage") or {}
        tokens = f"{u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0)}/{u.get('output_tokens', 0)}" if u else ""
        lines.append(f"| {row['module_id']} | {row.get('status', '')}{(' (' + row['incomplete_reason'] + ')') if row.get('incomplete_reason') else ''} | "
                     f"{row.get('outcome', '')} | {row.get('caught', '')} | {', '.join(row.get('missed', [])) or ''} | "
                     f"{', '.join(row.get('false_positives', [])) or ''} | {u.get('turns', '')} | {tokens} | "
                     f"{u.get('cost_usd_estimate', '')} | {u.get('duration_seconds', '')} |")
    return "\n".join(lines)


# --- Command line ----------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--repo", required=True)
    p.add_argument("--run-id", required=True, type=int)
    p.add_argument("--out", required=True, type=pathlib.Path)
    p.add_argument("--force", action="store_true", help="review even though the tests failed (a person's decision, for calibration)")
    r = sub.add_parser("review")
    r.add_argument("--inputs", required=True, type=pathlib.Path)
    r.add_argument("--out", required=True, type=pathlib.Path)
    d = sub.add_parser("render")
    d.add_argument("report", type=pathlib.Path)
    k = sub.add_parser("calibrate")
    k.add_argument("--out", required=True, type=pathlib.Path)
    c = sub.add_parser("current")
    c.add_argument("report", type=pathlib.Path)
    c.add_argument("--root", type=pathlib.Path, default=BASE)
    options = parser.parse_args(argv)

    if options.command == "prepare":
        github = GitHub(options.repo, os.environ.get("GITHUB_TOKEN"))
        try:
            context, cached = prepare(github, options.run_id, options.out, options.force)
        except Skip as skip:
            print(json.dumps({"decision": "skip", "reason": str(skip)}))
            return 0
        except NoApplication as none:
            print(json.dumps({"decision": "no_application", "reason": str(none), "pull_request": none.pull_request, "head": none.head}))
            return 0
        print(json.dumps({"decision": "cached" if cached else "review", "pull_request": context["test_run"]["pull_request"],
                          "module_id": context["module_id"], "commit": context["commit"], "head": context["test_run"]["head"],
                          "cache_key": context["cache_key"]}))
        return 0

    if options.command == "review":
        context = json.loads((options.inputs / "context.json").read_text())
        options.out.mkdir(parents=True, exist_ok=True)
        cached = options.inputs / "cached-report.json"
        if cached.exists():
            report = json.loads(cached.read_text())
        else:
            with tempfile.TemporaryDirectory(prefix="ai-review-src-") as work:
                root = extract((options.inputs / "sources.tar").read_bytes(), pathlib.Path(work) / "src")
                config = load_config()
                report = review(context, root, ai_providers.make(config), config)
        (options.out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        (options.out / "report.md").write_text(render(report) + "\n")
        print(f"{report['status']}, {report['outcome']}: {len(report['blockers'])} blocker(s), {len(report['recommendations'])} recommendation(s), "
              f"~${report['usage']['cost_usd_estimate']:.2f}, {report['usage']['turns']} turns" + (f" ({report['incomplete_reason']})" if report.get('incomplete_reason') else ""))
        return 0 if passed(report) else 1

    if options.command == "calibrate":
        summary = calibrate(options.out)
        (options.out / "summary.md").write_text(render_calibration(summary) + "\n")
        print(render_calibration(summary))
        return 0

    if options.command == "render":
        print(render(json.loads(options.report.read_text())))
        return 0

    problems = current(json.loads(options.report.read_text()), options.root)
    print("\n".join(problems) or "current")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
