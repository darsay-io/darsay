from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from modelvault.archiver import load_manifest
from modelvault.export import EXPORT_EXCLUDE, MARKER_NAME, export_bundle, import_bundle
from modelvault.hashing import hash_file
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_export_is_byte_identical_and_excludes_volatile(vault, test_provider, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    (bundle / "hydration.json").write_text("{}\n")
    (bundle / "transfer.json").write_text("{}\n")
    out1 = tmp_path / "e1"
    out2 = tmp_path / "e2"
    tar1 = export_bundle(bundle, out1, progress=silent)
    tar2 = export_bundle(bundle, out2, progress=silent)
    assert _sha256(tar1) == _sha256(tar2)
    # Marker first, then sorted entries.
    with tarfile.open(tar1, "r") as tar:
        names = tar.getnames()
    assert Path(names[0]).name == MARKER_NAME
    payload_names = names[1:]
    assert payload_names == sorted(payload_names)
    assert not any(Path(n).name in EXPORT_EXCLUDE for n in names)
    # Export event is recorded outside the tar.
    exports = json.loads((bundle / "exports.json").read_text())
    assert exports["exports"][0]["sha256"] == _sha256(tar1)


def test_export_refuses_existing(vault, test_provider, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    export_bundle(bundle, tmp_path, progress=silent)
    with pytest.raises(SystemExit, match="already exists"):
        export_bundle(bundle, tmp_path, progress=silent)


def test_import_registers_after_verify(vault, test_provider, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    source_vault = tmp_path / "src-vault"
    source_vault.mkdir()
    bundle = archive_quiet("test:acme/toy", vault=source_vault)
    tar_path = export_bundle(bundle, tmp_path / "exports", progress=silent)
    dest = import_bundle(tar_path, vault, progress=silent)
    imported = load_manifest(dest)
    original = load_manifest(bundle)
    assert imported["inventory"]["bundle_hash"]["value"] == original["inventory"]["bundle_hash"]["value"]
    assert imported["archive"]["imported"]["file_sha256"] == hash_file(
        tar_path, with_blake3=False
    )["sha256"]


def test_import_failure_registers_nothing(vault, test_provider, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    source_vault = tmp_path / "src-vault"
    source_vault.mkdir()
    bundle = archive_quiet("test:acme/toy", vault=source_vault)
    tar_path = export_bundle(bundle, tmp_path / "exports", progress=silent)

    tampered = tmp_path / "tampered.mvb.tar"
    with tarfile.open(tar_path, "r") as src, tarfile.open(tampered, "w") as dst:
        for member in src.getmembers():
            extracted = src.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            if member.name.endswith("model.safetensors"):
                data = b"tampered-weights"
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data))

    with pytest.raises(SystemExit, match="import verification FAILED"):
        import_bundle(tampered, vault, progress=silent)
    assert list(vault.glob("*/*/manifest.json")) == []
