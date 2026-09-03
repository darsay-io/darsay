"""``SHA256SUMS`` — the payload's hash list, verifiable with coreutils alone.

Its own sha256 is the bundle hash, so a reader with ``sha256sum`` and no
darsay can bind the list to the record and the bytes to the list.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tarfile

import pytest

from darsay import SCHEMA_VERSION
from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.export import export_bundle, import_bundle
from darsay.hashing import SHA256SUMS_NAME, sha256sums_text
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def _native_check() -> list[str] | None:
    if shutil.which("sha256sum"):
        return ["sha256sum", "-c", SHA256SUMS_NAME]
    if shutil.which("shasum"):
        return ["shasum", "-a", "256", "-c", SHA256SUMS_NAME]
    return None


def test_archive_writes_the_hash_list_and_coreutils_verifies_it(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    manifest = load_manifest(bundle)
    text = (bundle / SHA256SUMS_NAME).read_text(encoding="utf-8")

    assert text == sha256sums_text(manifest["inventory"]["files"])
    lines = text.splitlines()
    assert len(lines) == manifest["inventory"]["file_count"]
    assert all(line.split("  ", 1)[1].startswith("model/") for line in lines)
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1])
    assert (
        hashlib.sha256(text.encode("utf-8")).hexdigest()
        == manifest["inventory"]["bundle_hash"]["value"]
    ), "sha256sum SHA256SUMS is the bundle hash"

    check = _native_check()
    if check is None:
        pytest.skip("neither sha256sum nor shasum is installed here")
    run = subprocess.run(check, cwd=bundle, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.count(": OK") == len(lines)

    (bundle / "model" / "config.json").write_bytes(b"{")
    run = subprocess.run(check, cwd=bundle, capture_output=True, text=True)
    assert run.returncode != 0
    assert "model/config.json: FAILED" in run.stdout


def test_regen_verify_and_migrate_write_the_hash_list(vault, test_provider, capsys):
    from tests.integration.test_migrate import REV, TOY, place

    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    sums = bundle / SHA256SUMS_NAME
    good = sums.read_bytes()

    sums.unlink()
    assert main(["--vault", str(vault), "regen", str(bundle)]) == 0
    assert f"Regenerated {sums}" in capsys.readouterr().out
    assert sums.read_bytes() == good

    sums.write_bytes(b"stale\n")
    assert main(["--vault", str(vault), "verify", str(bundle)]) == 0
    capsys.readouterr()
    assert sums.read_bytes() == good, (
        "verify heals the list; the inventory never changes"
    )

    old = place(vault, TOY)
    assert not (old / SHA256SUMS_NAME).exists(), "a 1.x record never had one"
    assert main(["--vault", str(vault), "migrate", f"{TOY}@{REV}"]) == 0
    out = capsys.readouterr().out
    assert (
        f"Wrote manifest.json, README.md, SHA256SUMS  (schema {SCHEMA_VERSION})" in out
    )
    migrated = load_manifest(old)
    text = (old / SHA256SUMS_NAME).read_text(encoding="utf-8")
    assert (
        hashlib.sha256(text.encode("utf-8")).hexdigest()
        == migrated["inventory"]["bundle_hash"]["value"]
    )


def test_export_carries_the_hash_list_and_import_unpacks_it(
    vault, test_provider, tmp_path
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    expected = sha256sums_text(load_manifest(bundle)["inventory"]["files"]).encode()

    tar1 = export_bundle(bundle, tmp_path / "e1", progress=silent)
    (bundle / SHA256SUMS_NAME).write_bytes(b"stale on disk\n")
    tar2 = export_bundle(bundle, tmp_path / "e2", progress=silent)
    assert tar1.read_bytes() == tar2.read_bytes(), "the member is generated, never read"

    with tarfile.open(tar1, "r") as tar:
        name = next(n for n in tar.getnames() if n.endswith("/" + SHA256SUMS_NAME))
        member = tar.extractfile(name)
        assert member is not None and member.read() == expected

    other = tmp_path / "other"
    other.mkdir()
    dest = import_bundle(tar1, other, progress=silent)
    assert (dest / SHA256SUMS_NAME).read_bytes() == expected
