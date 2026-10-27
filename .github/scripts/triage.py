#!/usr/bin/env python3
# Copyright 2026 Anthropic PBC
# Modified for MikoPBX module-applications, 2026.
# SPDX-License-Identifier: Apache-2.0
#
# The idea and the safety rules come from .github/scripts/triage.py in https://github.com/anthropics/oss-scanner
# (Apache-2.0): the pull request and the module's repository are read as data, every external string is escaped
# before it reaches Markdown, and the comment informs the review without deciding anything.
"""Write the triage comment for an application pull request: what a reviewer would otherwise look up by hand.

    .github/scripts/triage.py --repo OWNER/NAME --pr N --validation validation.json

Reads the pull request and the module's repository through the GitHub API with a read-only token (GITHUB_TOKEN),
plus the JSON tools/validate.py --online --json wrote for the application. Prints the comment on stdout, or nothing
if the pull request touches no application. Never fails because a lookup failed: that line says "could not be checked".
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

MARKER = "<!-- module-applications-triage -->"
API = "https://api.github.com"
CHECKLIST = (
    ("maintainer", r"I maintain this module"),
    ("terms", r"I accept the \[module-applications terms\]"),
    ("validate", r"tools/validate\.py --online"),
    ("check", r"tools/check"),
    ("contact", r"support_email"),
    ("commit", r"full commit SHA"),
)


def md(value, limit=200):
    """External text made inert in Markdown: no HTML, links, mentions, emphasis, tables or code spans."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))[:limit]
    text = re.sub(r"([\\`*_{}\[\]()<>#+!|~])", r"\\\1", text)
    return text.replace("@", "@\u200b").replace("://", ":\u200b//")


class GitHub:
    def __init__(self, token):
        self.token = token
        self.requests = 0

    def get(self, path, accept="application/vnd.github+json"):
        self.requests += 1
        if self.requests > 30:
            raise RuntimeError("request budget spent")
        request = urllib.request.Request(f"{API}/{path}", headers={"Accept": accept, "User-Agent": "module-applications-triage",
                                                                   **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read(2_000_000)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise
        return json.loads(body) if accept.endswith("json") else body.decode("utf-8", errors="replace")


def checked(function):
    """Run one lookup; any failure becomes 'could not be checked'."""
    try:
        return function()
    except Exception as error:  # noqa: BLE001 - a lookup must never break the comment
        return f"could not be checked ({type(error).__name__})"


def owner_repo(url):
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?", url or "")
    return match.groups() if match else (None, None)


def build(github, repo, pr, validation):
    pull = github.get(f"repos/{repo}/pulls/{pr}")
    files = github.get(f"repos/{repo}/pulls/{pr}/files?per_page=100") or []
    if not any(f["filename"].startswith("applications/") for f in files):
        return ""
    author = pull["user"]["login"]
    body = pull.get("body") or ""
    rows = []

    application = (validation.get("applications") or [{}])[0]
    named = sorted({f["filename"].split("/")[1] for f in files if f["filename"].startswith("applications/") and f["filename"].count("/") >= 2})
    module_id = application.get("module_id") or (named[0] if len(named) == 1 else "?")
    problems = application.get("problems") or []
    warnings = application.get("warnings") or []
    if not validation:
        rows.append(("Rules", "could not be checked (the validator did not run: see the pull request's scope above)"))
    elif problems:
        rows.append(("Rules", f"**{len(problems)} problem(s)**: " + "; ".join(f"[{md(p['rule'])}] {md(p['message'], 160)}" for p in problems[:8])))
    else:
        rows.append(("Rules", f"pass, rules {md(validation.get('rules_version'))}, schema {md(validation.get('schema_version'))}" +
                     (f"; {len(warnings)} recommendation(s): " + "; ".join(f"[{md(w['rule'])}] {md(w['message'], 120)}" for w in warnings[:5]) if warnings else "")))
    module_json = (application.get("online") or {}).get("module_json") or {}
    product, feature = module_json.get("lic_product_id") or 0, module_json.get("lic_feature_id") or 0
    if module_json:
        rows.append(("Licence", "free module (no licence ids)" if not (product or feature) else
                     f"**paid module**: lic_product_id {md(product)}, lic_feature_id {md(feature)}. The tests run without a licence key: "
                     "what needs a licence is reported as license_required, not checked. Publishing a paid module needs MIKO's verified publisher agreement."))
    registry = application.get("registry") or {}
    rows.append(("Publisher card", f"{md(registry.get('status', 'not checked'))}: {md(registry.get('detail', ''), 200)}"))

    owner, name = owner_repo(application.get("repo"))
    commit = application.get("commit") or ""
    if owner:
        def repository():
            data = github.get(f"repos/{owner}/{name}")
            if data is None:
                return "not found (or not public)"
            created = (data.get("created_at") or "")[:10]
            pushed = (data.get("pushed_at") or "")[:10]
            license_id = (data.get("license") or {}).get("spdx_id") or "none detected"
            flags = ", ".join(f for f, on in (("archived", data.get("archived")), ("fork", data.get("fork"))) if on)
            return (f"{md(owner)}/{md(name)}: {data.get('stargazers_count', 0)} stars, {data.get('forks_count', 0)} forks, created {created}, "
                    f"last push {pushed}, license {md(license_id)}" + (f", **{flags}**" if flags else ""))

        def commit_line():
            data = github.get(f"repos/{owner}/{name}/commits/{commit}")
            if data is None:
                return f"`{commit[:12]}` **not found**"
            date = (data.get("commit", {}).get("committer") or {}).get("date", "")[:10]
            repo_data = github.get(f"repos/{owner}/{name}") or {}
            default = repo_data.get("default_branch", "main")
            compare = github.get(f"repos/{owner}/{name}/compare/{urllib.parse.quote(default)}...{commit}") or {}
            on_default = compare.get("status") in ("identical", "behind")
            if on_default:
                return f"`{commit[:12]}` from {date}, on the default branch {md(default)}"
            return f"`{commit[:12]}` from {date}, **not on the default branch {md(default)}**" + (f" ({md(compare['status'])})" if compare.get("status") else "")

        def maintainer():
            if author.lower() == owner.lower():
                return f"{md(author)} owns the repository"
            contributors = github.get(f"repos/{owner}/{name}/contributors?per_page=100") or []
            for c in contributors:
                if c.get("login", "").lower() == author.lower():
                    return f"{md(author)} has {c.get('contributions', 0)} commit(s) in the repository"
            return f"**{md(author)} is not among the repository's top contributors**"

        def security_contact():
            email = (application.get("online") or {}).get("module_json", {}).get("support_email")
            text = github.get(f"repos/{owner}/{name}/contents/SECURITY.md?ref={commit}", accept="application/vnd.github.raw")
            if text is None:
                return "no SECURITY.md at the commit (recommended)"
            return "SECURITY.md names support_email" if email and email.lower() in text.lower() else "SECURITY.md does **not** name support_email"

        rows.append(("Repository", checked(repository)))
        rows.append(("Commit", checked(commit_line)))
        rows.append(("Opened by", checked(maintainer)))
        rows.append(("Contact", checked(security_contact)))

    scope = {f["filename"] for f in files}
    rows.append(("Scope", "one application" if all(p.startswith(f"applications/{module_id}/") for p in scope) else f"**files outside applications/{md(module_id)}/**"))
    ticked = []
    for key, pattern in CHECKLIST:
        ticked.append(bool(re.search(rf"^\s*[-*]\s+\[[xX]\]\s+.*{pattern}", body, re.M | re.I)))
    rows.append(("Checklist", f"{sum(ticked)} of {len(CHECKLIST)} boxes ticked" + ("" if all(ticked) else ": " + ", ".join(k for (k, _), t in zip(CHECKLIST, ticked) if not t) + " not ticked")))

    lines = [MARKER, f"### Triage for `{md(module_id)}`", "", "| Check | Result |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["", f"This comment informs the review; it decides nothing. The tests run in the **Test** check; MIKO approves an exact commit. Updated {now}."]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--validation", help="JSON from tools/validate.py --online --json (missing or empty: validator did not run)")
    options = parser.parse_args()
    validation = {}
    if options.validation and os.path.exists(options.validation) and os.path.getsize(options.validation):
        with open(options.validation, encoding="utf-8") as handle:
            validation = json.load(handle)
    print(build(GitHub(os.environ.get("GITHUB_TOKEN")), options.repo, options.pr, validation))
    return 0


if __name__ == "__main__":
    sys.exit(main())
