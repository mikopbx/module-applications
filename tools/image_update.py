#!/usr/bin/env python3
# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""Find out where mikopbx/mikopbx:latest points, and rewrite config/mikopbx.yaml if it moved.

    tools/image_update.py                 print what latest is and whether the config is behind (exit 0)
    tools/image_update.py --write         also rewrite config/mikopbx.yaml when it is behind
    tools/image_update.py --json          the same, as JSON

The tests pin MikoPBX by digest (config/mikopbx.yaml), never by tag. This only reads the registry: the multi-arch index
of the tag, the per-architecture manifests and the MikoPBX version from the image configuration (MIKOPBX_VERSION).
Nothing is pulled and nothing runs. The new digest becomes the reference only after a pull request with it passes the
control modules and a person merges it (.github/workflows/image-update.yaml).
"""
import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent.parent
CONFIG = BASE / "config" / "mikopbx.yaml"
INDEX_TYPES = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"
MANIFEST_TYPES = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
ARCHITECTURES = {"amd64": "x86_64", "arm64": "arm64"}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class DockerHub:
    """Anonymous, read-only access to one Docker Hub repository."""

    def __init__(self, repository="mikopbx/mikopbx"):
        self.repository = repository
        self.token = None

    def _get(self, url, accept):
        if self.token is None:
            auth = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{self.repository}:pull"
            with urllib.request.urlopen(auth, timeout=30) as response:
                self.token = json.load(response)["token"]
        request = urllib.request.Request(url, headers={"Accept": accept, "Authorization": f"Bearer {self.token}"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.headers.get("Docker-Content-Digest"), response.read(4_000_000)
            except urllib.error.HTTPError as error:
                # Docker Hub answers 429 to anonymous clients now and then (seen on 2026-10-27).
                if error.code not in (429, 500, 502, 503) or attempt == 3:
                    raise
                time.sleep(10 * (attempt + 1))

    def manifest(self, reference, accept):
        """(digest, parsed JSON) of a manifest or index by tag or digest."""
        digest, body = self._get(f"https://registry-1.docker.io/v2/{self.repository}/manifests/{reference}", accept)
        return digest, json.loads(body)

    def blob(self, digest):
        return json.loads(self._get(f"https://registry-1.docker.io/v2/{self.repository}/blobs/{digest}", "application/octet-stream")[1])


def latest(registry, tag="latest"):
    """Where `tag` points: {tag, digest, platforms: {x86_64, arm64}, version}."""
    digest, index = registry.manifest(tag, INDEX_TYPES)
    if not (digest and DIGEST.fullmatch(digest)):
        raise RuntimeError(f"the registry gave no digest for {tag}")
    platforms = {}
    for entry in index.get("manifests", []):
        platform = entry.get("platform") or {}
        arch = ARCHITECTURES.get(platform.get("architecture"))
        if arch and platform.get("os") == "linux" and arch not in platforms:
            platforms[arch] = entry["digest"]
    if set(platforms) != set(ARCHITECTURES.values()):
        raise RuntimeError(f"{tag} does not have both x86_64 and arm64 (found: {', '.join(sorted(platforms)) or 'none'})")
    _, manifest = registry.manifest(platforms["x86_64"], MANIFEST_TYPES)
    config = registry.blob(manifest["config"]["digest"])
    environment = dict(item.split("=", 1) for item in (config.get("config") or {}).get("Env") or [] if "=" in item)
    version = environment.get("MIKOPBX_VERSION", "")
    if not re.fullmatch(r"[0-9]{4}\.[0-9]+\.[0-9]+", version):
        raise RuntimeError(f"the image does not say its MikoPBX version (MIKOPBX_VERSION={version!r})")
    return {"tag": version, "digest": digest, "platforms": platforms, "version": version}


def current(text):
    """The pinned image in config/mikopbx.yaml (the first and only entry of `images`)."""
    import yaml  # only needed here, so the registry part runs without PyYAML
    return yaml.safe_load(text)["images"][0]


def rewrite(text, found):
    """config/mikopbx.yaml with the image entry replaced, comments and everything else kept."""
    entry = (
        f'  - version: "{found["version"]}"\n'
        f"    image: docker.io/mikopbx/mikopbx\n"
        f'    tag: "{found["tag"]}"\n'
        f"    digest: {found['digest']}\n"
        f"    platforms:\n"
        f"      x86_64: {found['platforms']['x86_64']}\n"
        f"      arm64: {found['platforms']['arm64']}\n"
    )
    pattern = re.compile(r"^images:\n(?:  .*\n|\n)*?(?=^\S|\Z)", re.M)
    if not pattern.search(text):
        raise RuntimeError("config/mikopbx.yaml has no images: block")
    return pattern.sub(lambda _: "images:\n" + entry + "\n", text, count=1)


def compare(text, found):
    pinned = current(text)
    behind = pinned["digest"] != found["digest"]
    return {
        "behind": behind,
        "pinned": {"version": pinned["version"], "digest": pinned["digest"], "platforms": pinned.get("platforms")},
        "latest": found,
    }


def main(argv=None, registry=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="rewrite config/mikopbx.yaml when it is behind")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config", type=pathlib.Path, default=CONFIG)
    options = parser.parse_args(argv)
    text = options.config.read_text(encoding="utf-8")
    result = compare(text, latest(registry or DockerHub()))
    if result["behind"] and options.write:
        options.config.write_text(rewrite(text, result["latest"]), encoding="utf-8")
    if options.json:
        print(json.dumps(result, indent=2))
    elif result["behind"]:
        print(f"behind: pinned {result['pinned']['version']} {result['pinned']['digest']}, latest is {result['latest']['version']} {result['latest']['digest']}")
    else:
        print(f"up to date: {result['pinned']['version']} {result['pinned']['digest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
