# Copyright 2026 the module-applications authors
# SPDX-License-Identifier: Apache-2.0
"""tools/image_update.py with a fake registry: nothing changes when the digest matches; a moved tag rewrites only the
image entry of config/mikopbx.yaml."""
import difflib
import json
import pathlib
import sys

import pytest
import yaml

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "tools"))
import image_update  # noqa: E402

CONFIG = (BASE / "config" / "mikopbx.yaml").read_text()
PINNED = yaml.safe_load(CONFIG)["images"][0]
NEW = {
    "index": "sha256:" + "1" * 64, "x86_64": "sha256:" + "2" * 64, "arm64": "sha256:" + "3" * 64,
    "config": "sha256:" + "4" * 64, "version": "2026.7.1",
}


class FakeRegistry:
    def __init__(self, index, amd64, arm64, version, arm=True):
        self.index, self.amd64, self.arm64, self.version, self.arm = index, amd64, arm64, version, arm

    def manifest(self, reference, accept):
        if reference == "latest":
            manifests = [{"digest": self.amd64, "platform": {"architecture": "amd64", "os": "linux"}}]
            if self.arm:
                manifests.append({"digest": self.arm64, "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"}})
            return self.index, {"manifests": manifests}
        assert reference == self.amd64
        return reference, {"config": {"digest": NEW["config"]}}

    def blob(self, digest):
        return {"config": {"Env": ["PATH=/bin", f"MIKOPBX_VERSION={self.version}"]}}


def run(tmp_path, registry, *args):
    config = tmp_path / "mikopbx.yaml"
    config.write_text(CONFIG)
    return config, image_update.main(["--config", str(config), *args], registry=registry)


def test_same_digest_changes_nothing(tmp_path, capsys):
    same = FakeRegistry(PINNED["digest"], PINNED["platforms"]["x86_64"], PINNED["platforms"]["arm64"], PINNED["version"])
    config, code = run(tmp_path, same, "--write", "--json")
    assert code == 0 and config.read_text() == CONFIG
    assert json.loads(capsys.readouterr().out)["behind"] is False


def test_moved_tag_rewrites_only_the_image_entry(tmp_path, capsys):
    moved = FakeRegistry(NEW["index"], NEW["x86_64"], NEW["arm64"], NEW["version"])
    config, code = run(tmp_path, moved, "--write")
    assert code == 0 and capsys.readouterr().out.startswith("behind: pinned 2026.6.34")
    after = yaml.safe_load(config.read_text())
    assert after["images"] == [{
        "version": "2026.7.1", "image": "docker.io/mikopbx/mikopbx", "tag": "2026.7.1", "digest": NEW["index"],
        "platforms": {"x86_64": NEW["x86_64"], "arm64": NEW["arm64"]},
    }]
    before = yaml.safe_load(CONFIG)
    assert {k: v for k, v in after.items() if k != "images"} == {k: v for k, v in before.items() if k != "images"}
    changed = [line for line in difflib.unified_diff(CONFIG.splitlines(), config.read_text().splitlines(), lineterm="", n=0)
               if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    assert sorted(changed) == sorted([
        '-  - version: "2026.6.34"', '+  - version: "2026.7.1"',
        '-    tag: "2026.6.34"', '+    tag: "2026.7.1"',
        f"-    digest: {PINNED['digest']}", f"+    digest: {NEW['index']}",
        f"-      x86_64: {PINNED['platforms']['x86_64']}", f"+      x86_64: {NEW['x86_64']}",
        f"-      arm64: {PINNED['platforms']['arm64']}", f"+      arm64: {NEW['arm64']}",
    ])


def test_without_write_the_config_stays(tmp_path):
    config, _ = run(tmp_path, FakeRegistry(NEW["index"], NEW["x86_64"], NEW["arm64"], NEW["version"]))
    assert config.read_text() == CONFIG


def test_missing_architecture_or_version_is_an_error(tmp_path):
    with pytest.raises(RuntimeError, match="does not have both"):
        run(tmp_path, FakeRegistry(NEW["index"], NEW["x86_64"], NEW["arm64"], NEW["version"], arm=False), "--write")
    with pytest.raises(RuntimeError, match="does not say its MikoPBX version"):
        run(tmp_path, FakeRegistry(NEW["index"], NEW["x86_64"], NEW["arm64"], "latest"), "--write")
