#!/usr/bin/env bash
# Copyright 2026 Anthropic PBC
# Modified for MikoPBX module-applications, 2026.
# SPDX-License-Identifier: Apache-2.0
#
# Adapted from the "Take projects/<name>/ from the pull request" step of .github/workflows/build.yaml in
# https://github.com/anthropics/oss-scanner (Apache-2.0).
#
# Replace applications/<module_id>/ in this checkout (the base branch) with the pull request's application.yaml, read
# from git's object store, after checking that the pull request changes nothing else. Nothing is checked out from the
# pull request and nothing from it runs.
#
#   .github/scripts/take_application.sh REPO PR
#
# Needs GH_TOKEN (read-only), tools/validate.py's dependencies and GITHUB_OUTPUT. Writes to GITHUB_OUTPUT:
#   head        the pull request's head commit
#   module_id   empty if the pull request changes no application or removes one
#   previous    the application's commit on the base branch, if it differs (the version to update from)
#   maintenance true for a pull request by the owners or members that changes no application
# Exits 1, with ::error::, if the pull request changes anything else (rules APP-006, APP-003).
set -euo pipefail
export LC_ALL=C
REPO=$1 PR=$2
fail() { echo "::error::$1"; exit 1; }
commit_of() { python3 -c 'import sys, yaml; print(str((yaml.safe_load(open(sys.argv[1])) or {}).get("commit") or ""))' "$1" 2>/dev/null || true; }

[[ $PR =~ ^[0-9]+$ ]] || fail "Not a pull request number."
pull=$(gh api "repos/$REPO/pulls/$PR")
head=$(jq -r .head.sha <<< "$pull")
[[ $head =~ ^[0-9a-f]{40}$ ]] || fail "Could not read the pull request's head commit."
echo "head=$head" >> "$GITHUB_OUTPUT"
# Only one page of changed files is read below.
[ "$(jq -r .changed_files <<< "$pull")" -le 100 ] || fail "This pull request changes more than 100 files."

# Every changed path, a renamed file under both names, must be inside one applications/<module_id>/.
gh api "repos/$REPO/pulls/$PR/files?per_page=100" --jq '.[] | .filename, (.previous_filename // empty)' > "$RUNNER_TEMP/paths"

# A pull request from the repository's owners or organisation members that touches no application is maintenance
# (tools, rules, workflows, documentation, the image update): there is nothing to take or test. Anyone else, or any
# pull request that also touches applications/, goes through the rules below.
# The image update's GitHub App (repository variable IMAGE_UPDATE_BOT, docs/branch-protection.md) counts as maintenance
# only for a pull request that changes config/mikopbx.yaml and nothing else.
association=$(jq -r .author_association <<< "$pull")
author=$(jq -r .user.login <<< "$pull")
if ! grep -q '^applications/' "$RUNNER_TEMP/paths" && [[ $association == OWNER || $association == MEMBER ]]; then
  echo "A maintenance pull request ($association) that changes no application: nothing to check."
  echo "maintenance=true" >> "$GITHUB_OUTPUT"
  exit 0
fi
if [[ -n ${IMAGE_UPDATE_BOT:-} && $author == "$IMAGE_UPDATE_BOT" && $(cat "$RUNNER_TEMP/paths") == config/mikopbx.yaml ]]; then
  echo "The image update ($author) changes config/mikopbx.yaml only: nothing to take or test here."
  echo "maintenance=true" >> "$GITHUB_OUTPUT"
  exit 0
fi
module_id=$(python3 tools/validate.py --pr-paths "$RUNNER_TEMP/paths") \
  || fail "This pull request changes files outside one applications/<module_id>/ (listed above). An application changes nothing else."
if [ -z "$module_id" ]; then
  echo "This pull request changes no application."
  exit 0
fi

git fetch --quiet --no-tags --depth=1 origin "$head"
git ls-tree -r -z "$head" -- "applications/$module_id/" > "$RUNNER_TEMP/tree"
if [ ! -s "$RUNNER_TEMP/tree" ]; then
  echo "This pull request removes applications/$module_id/: a withdrawal, nothing to check."
  exit 0
fi

# The application as it is on the base branch, if any: its commit is the version to update from.
previous=""
[ ! -f "applications/$module_id/application.yaml" ] || previous=$(commit_of "applications/$module_id/application.yaml")

rm -rf "applications/$module_id"
mkdir "applications/$module_id"
while IFS= read -r -d '' entry; do
  read -r mode _ blob <<< "${entry%%$'\t'*}"
  file=${entry#*$'\t'}
  file=${file#"applications/$module_id/"}
  # 100644 is a regular file. Anything else, or any other name, is refused.
  if [[ $mode != 100644 || $file != application.yaml ]]; then
    fail "applications/$module_id/ may hold application.yaml only, as a regular file."
  fi
  git cat-file blob "$blob" > "applications/$module_id/application.yaml"
done < "$RUNNER_TEMP/tree"

current=$(commit_of "applications/$module_id/application.yaml")
[[ $previous =~ ^[0-9a-f]{40}$ && $previous != "$current" ]] || previous=""
echo "Took applications/$module_id/application.yaml as it is at $head."
{
  echo "module_id=$module_id"
  echo "previous=$previous"
} >> "$GITHUB_OUTPUT"
