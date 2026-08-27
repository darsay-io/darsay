from __future__ import annotations

import ast
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from darsay.export import standalone_verify_bytes
from darsay.hashing import bundle_hash
from darsay.standalone_verify import (
    VerifyError,
    bundle_hash_value,
    format_result,
    main,
    payload_root_of,
    verify_path,
)

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "darsay" / "standalone_verify.py"
STDLIB_ALLOW = {
    "__future__",
    "argparse",
    "hashlib",
    "json",
    "os",
    "sys",
    "tarfile",
}


def _manifest(files: list[dict], payload_root: str = "model/") -> dict:
    return {
        "schema_version": "1.6.0",
        "kind": "darsay.bundle",
        "artifact_type": "model" if payload_root.startswith("model") else "dataset",
        "bundle_id": "toy@aaaaaaaaaaaa",
        "inventory": {
            "file_count": len(files),
            "total_size_bytes": sum(f["size"] for f in files),
            "bundle_hash": {
                "algorithm": "sha256-of-sorted-sha256-lines",
                "value": bundle_hash(files, payload_root.rstrip("/"))["value"],
                "covers": payload_root,
            },
            "layout": {"payload_root": payload_root},
            "files": files,
        },
    }


def _write_bundle(tmp_path: Path, blobs: dict[str, bytes], payload_root: str = "model"):
    files = []
    for rel, data in blobs.items():
        path = tmp_path / payload_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(
            {
                "path": f"{payload_root}/{rel}",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps(_manifest(files, f"{payload_root}/")) + "\n", encoding="utf-8"
    )
    return files


def _write_tar(
    tmp_path: Path, blobs: dict[str, bytes], payload_root: str = "model"
) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_bundle(bundle, blobs, payload_root)
    tar_path = tmp_path / "toy@aaaaaaaaaaaa.mvb.tar"
    prefix = "toy@aaaaaaaaaaaa"
    with tarfile.open(tar_path, "w") as tar:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                rel = path.relative_to(bundle).as_posix()
                info = tarfile.TarInfo(name=f"{prefix}/{rel}")
                data = path.read_bytes()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return tar_path


def test_stdlib_only_imports():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                pytest.fail("relative import is not stdlib-only")
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= STDLIB_ALLOW
    assert "darsay" not in imported
    assert "huggingface_hub" not in imported
    assert "blake3" not in imported


def test_export_bytes_are_the_source_file():
    assert standalone_verify_bytes() == SCRIPT.read_bytes()


def test_payload_root_defaults_to_model():
    assert payload_root_of({}) == "model"
    assert (
        payload_root_of({"inventory": {"layout": {"payload_root": "data/"}}}) == "data"
    )


def test_bundle_hash_value_matches_darsay_hashing():
    records = [
        {"path": "model/b.bin", "sha256": "bb"},
        {"path": "model/a.bin", "sha256": "aa"},
    ]
    assert bundle_hash_value(records) == bundle_hash(records)["value"]


def test_verify_dir_pass(tmp_path):
    _write_bundle(tmp_path, {"a.txt": b"alpha", "nested/b.txt": b"beta"})
    result = verify_path(str(tmp_path))
    assert result["status"] == "pass"
    assert result["files_checked"] == 2
    assert result["bundle_hash_match"] is True
    assert format_result(result).startswith("pass:")


def test_verify_dir_dataset_payload_root(tmp_path):
    _write_bundle(tmp_path, {"train.jsonl": b"{}\n"}, payload_root="data")
    result = verify_path(str(tmp_path))
    assert result["status"] == "pass"
    assert result["files_checked"] == 1


def test_verify_dir_skips_cache(tmp_path):
    _write_bundle(tmp_path, {"a.txt": b"alpha"})
    cache = tmp_path / "model" / ".cache" / "partial"
    cache.mkdir(parents=True)
    (cache / "incomplete").write_bytes(b"not payload")
    result = verify_path(str(tmp_path))
    assert result["status"] == "pass"
    assert result["extra"] == []


def test_verify_detects_modified_missing_extra(tmp_path):
    _write_bundle(tmp_path, {"keep.txt": b"keep", "gone.txt": b"gone"})
    (tmp_path / "model" / "keep.txt").write_bytes(b"tampered")
    (tmp_path / "model" / "gone.txt").unlink()
    (tmp_path / "model" / "extra.txt").write_bytes(b"new")
    result = verify_path(str(tmp_path))
    assert result["status"] == "fail"
    assert result["mismatched"] == ["model/keep.txt"]
    assert result["missing"] == ["model/gone.txt"]
    assert result["extra"] == ["model/extra.txt"]
    assert result["bundle_hash_match"] is False
    report = format_result(result)
    assert report.startswith("fail:")
    assert "model/keep.txt" in report


def test_verify_tar_pass(tmp_path):
    tar_path = _write_tar(tmp_path, {"a.txt": b"alpha"})
    result = verify_path(str(tar_path))
    assert result["status"] == "pass"
    assert result["files_checked"] == 1


def test_verify_tar_detects_tamper(tmp_path):
    tar_path = _write_tar(tmp_path, {"a.txt": b"alpha"})
    tampered = tmp_path / "tampered.mvb.tar"
    with tarfile.open(tar_path, "r") as src, tarfile.open(tampered, "w") as dst:
        for member in src.getmembers():
            extracted = src.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            if member.name.endswith("a.txt"):
                data = b"nope"
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data))
    result = verify_path(str(tampered))
    assert result["status"] == "fail"
    assert result["mismatched"] == ["model/a.txt"]


def test_verify_errors_on_missing_manifest(tmp_path):
    with pytest.raises(VerifyError, match="no manifest.json"):
        verify_path(str(tmp_path))


def test_main_pass_and_fail(tmp_path, capsys):
    _write_bundle(tmp_path, {"a.txt": b"alpha"})
    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().out.startswith("pass:")
    (tmp_path / "model" / "a.txt").write_bytes(b"nope")
    assert main([str(tmp_path)]) == 1
    assert capsys.readouterr().out.startswith("fail:")


def test_main_usage_error(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert main([str(missing)]) == 2
    err = capsys.readouterr().err
    assert "does not exist" in err


def test_script_runs_without_darsay_on_path(tmp_path):
    _write_bundle(tmp_path, {"a.txt": b"alpha"})
    clean = {k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        cwd=str(tmp_path),
        env=clean,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("pass:")
