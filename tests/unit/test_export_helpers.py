from __future__ import annotations

import io
import json
import tarfile

import pytest

from modelvault.export import (
    EXPORT_EXCLUDE,
    MARKER_NAME,
    MVB_FORMAT_VERSION,
    _bundle_files,
    _read_marker,
    _tarinfo,
)


def test_bundle_files_excludes_volatile_and_sorts(tmp_path):
    (tmp_path / "README.md").write_text("r")
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "exports.json").write_text("{}")
    (tmp_path / "hydration.json").write_text("{}")
    (tmp_path / "transfer.json").write_text("{}")
    (tmp_path / "transfer.lock").write_text("{}")
    (tmp_path / ".DS_Store").write_text("x")
    (tmp_path / "model").mkdir()
    (tmp_path / "model" / "a.bin").write_bytes(b"a")
    names = [rel for rel, _ in _bundle_files(tmp_path)]
    assert names == ["README.md", "manifest.json", "model/a.bin"]
    assert EXPORT_EXCLUDE.isdisjoint(set(names))


def test_bundle_files_refuses_symlink(tmp_path):
    target = tmp_path / "real"
    target.write_text("x")
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(SystemExit, match="symlink"):
        _bundle_files(tmp_path)


def test_tarinfo_is_normalized():
    info = _tarinfo("bundle/.mvb.json", 12, mtime=1_700_000_000)
    assert info.uid == info.gid == 0
    assert info.uname == info.gname == ""
    assert info.mode == 0o644
    assert info.mtime == 1_700_000_000


def test_read_marker_requires_leading_marker(tmp_path):
    tar_path = tmp_path / "bad.tar"
    with tarfile.open(tar_path, "w") as tar:
        data = b"not a marker"
        info = tarfile.TarInfo(name="bundle/README.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(SystemExit, match="missing leading"):
        _read_marker(tar_path)


def test_read_marker_rejects_incompatible_major(tmp_path):
    tar_path = tmp_path / "old.tar"
    marker = {
        "mvb_format_version": "99.0",
        "bundle_id": "x@abc",
        "bundle_hash": {"value": "00"},
    }
    payload = (json.dumps(marker) + "\n").encode("utf-8")
    with tarfile.open(tar_path, "w") as tar:
        info = tarfile.TarInfo(name=f"x@abc/{MARKER_NAME}")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    with pytest.raises(SystemExit, match="not supported"):
        _read_marker(tar_path)


def test_current_format_major_is_readable():
    major = MVB_FORMAT_VERSION.split(".")[0]
    assert major == "1"
