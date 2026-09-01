"""``darsay mv`` / ``darsay cp`` — relocate or replicate a registered bundle.

rsync into the two-level layout stays a first-class copy (see
``test_transfer.py``); these verbs fold the verify-then-act bookkeeping
into one command and must never do less than that contract.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from darsay import relocate
from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.hashing import hash_file
from darsay.relocate import copy_bundle, move_bundle
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files

BUNDLE_ID = "test--acme--toy@aaaaaaaaaaaa"


def _payload_hashes(bundle: Path) -> dict[str, str]:
    return {
        p.relative_to(bundle).as_posix(): hash_file(p, with_blake3=False)["sha256"]
        for p in (bundle / "model").rglob("*")
        if p.is_file()
    }


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def _registered_bundle(vault: Path, test_provider) -> Path:
    """A registered bundle with every kind of root file a real one may carry."""
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    (bundle / "curation.md").write_text("# curated by hand\n", encoding="utf-8")
    (bundle / "hydration.json").write_text('{"engine": "x"}\n', encoding="utf-8")
    (bundle / "exports.json").write_text('{"exports": []}\n', encoding="utf-8")
    assert (bundle / "transfer.json").is_file()
    return bundle


def _real(*argv: str) -> str:
    return shlex.join(["darsay", *argv])


def _rotting_copy(monkeypatch):
    """Patch the per-file copy so model.safetensors arrives corrupted."""
    real = relocate._copy_file

    def rotting(src, dst):
        method = real(src, dst)
        if Path(src).name == "model.safetensors":
            Path(dst).write_bytes(b"bit-rot on the way over")
        return method

    monkeypatch.setattr(relocate, "_copy_file", rotting)


# --- mv ---------------------------------------------------------------------


def test_mv_same_filesystem_is_a_rename(tmp_path, vault, test_provider, capsys):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    before = _payload_hashes(bundle)
    old_location = load_manifest(bundle)["archive"]["location"]

    assert main(["--vault", str(src_vault), "mv", BUNDLE_ID, str(vault)]) == 0
    out = capsys.readouterr().out
    dest = vault / "test--acme--toy" / "aaaaaaaaaaaa"

    assert "how:      rename in place" in out
    assert "leaves:   hydration.json" in out
    assert f"Moved {BUNDLE_ID} → {dest}  (renamed; bytes untouched)" in out
    assert not bundle.exists()
    assert not bundle.parent.exists(), "empty <vault>/<name>/ is swept"
    assert _payload_hashes(dest) == before
    assert (dest / "curation.md").read_text(encoding="utf-8") == "# curated by hand\n"
    assert (dest / "exports.json").is_file()
    assert (dest / "transfer.json").is_file()
    assert not (dest / "hydration.json").exists()
    assert not (dest / "transfer.lock").exists()

    manifest = load_manifest(dest)
    assert manifest["archive"]["location"] == str(dest.resolve())
    move = manifest["archive"]["moves"][-1]
    assert move["method"] == "rename"
    assert move["from_location"] == old_location == str(bundle.resolve())
    assert str(dest.resolve()) in (dest / "README.md").read_text(encoding="utf-8")
    assert str(dest) in (dest / "VERIFICATION.md").read_text(encoding="utf-8")
    assert str(bundle) not in (dest / "VERIFICATION.md").read_text(encoding="utf-8")

    assert main(["--vault", str(vault), "list", "--ids"]) == 0
    assert BUNDLE_ID in capsys.readouterr().out


def test_mv_across_filesystems_copies_verifies_then_removes(
    tmp_path, vault, test_provider, monkeypatch
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    before = _payload_hashes(bundle)
    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)

    logs: list[str] = []
    dest = move_bundle(bundle, vault, progress=logs.append)

    assert dest == vault / "test--acme--toy" / "aaaaaaaaaaaa"
    assert any("how:      copy" in line for line in logs)
    assert any("Verifying the copy at the destination" in line for line in logs)
    assert any("payload files verified at the destination" in line for line in logs)
    assert not bundle.exists()
    assert _payload_hashes(dest) == before
    assert not list(vault.glob("*/.mv-*")), "staging directory is gone"

    manifest = load_manifest(dest)
    assert manifest["archive"]["moves"][-1]["method"] == "copy"
    assert manifest["archive"]["location"] == str(dest.resolve())
    assert manifest["validation"]["checksum_verification"]["status"] == "pass"
    history = json.loads((dest / "verification.json").read_text(encoding="utf-8"))
    assert history["latest"]["checksum"]["status"] == "pass"
    assert str(dest) in (dest / "VERIFICATION.md").read_text(encoding="utf-8")
    assert (dest / "curation.md").read_text(encoding="utf-8") == "# curated by hand\n"
    assert not (dest / "hydration.json").exists()


def test_mv_rename_refused_by_the_kernel_falls_back_to_copy(
    tmp_path, vault, test_provider, monkeypatch
):
    """st_dev agreed but rename said EXDEV (bind mounts do this): copy path."""
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    monkeypatch.setattr(relocate, "_try_rename", lambda *a, **k: False)

    dest = move_bundle(bundle, vault, progress=silent)
    assert not bundle.exists()
    assert load_manifest(dest)["archive"]["moves"][-1]["method"] == "copy"


def test_mv_verification_failure_leaves_source_and_registers_nothing(
    tmp_path, vault, test_provider, monkeypatch
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    before = _tree(bundle)
    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
    _rotting_copy(monkeypatch)

    with pytest.raises(SystemExit, match="verification FAILED at the destination"):
        move_bundle(bundle, vault, progress=silent)

    assert _tree(bundle) == before, "source is exactly as it was"
    assert not (bundle / "transfer.lock").exists()
    assert list(vault.rglob("manifest.json")) == []
    assert not list(vault.glob("*/.mv-*")), "staging copy removed"


def test_mv_refuses_a_partial_and_names_the_right_verb(
    tmp_path, vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    rc = main(
        [
            "--vault",
            str(src_vault),
            "archive",
            "test:acme/toy",
            "--max-bytes",
            "1",
            "--jobs",
            "1",
        ]
    )
    assert rc == 10
    partial = next(src_vault.glob("*/*/transfer.json")).parent
    before = _tree(partial)
    capsys.readouterr()

    for verb in ("mv", "cp"):
        with pytest.raises(SystemExit) as exc:
            main(["--vault", str(src_vault), verb, str(partial), str(vault)])
        message = str(exc.value)
        assert "is a partial, not a registered bundle" in message
        assert "assemble" in message and "--handoff" in message
    assert _tree(partial) == before
    assert list(vault.rglob("*")) == []


def test_mv_refusals(tmp_path, vault, test_provider):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)

    with pytest.raises(SystemExit, match="destination vault does not exist"):
        move_bundle(bundle, tmp_path / "not-mounted", progress=silent)
    assert not (tmp_path / "not-mounted").exists(), "mv never creates a vault"

    with pytest.raises(SystemExit, match="already in"):
        move_bundle(bundle, src_vault, progress=silent)

    # A bundle with the same id already at the destination.
    other = archive_quiet("test:acme/toy", vault=vault)
    with pytest.raises(SystemExit, match=r"already exists \(use --force"):
        move_bundle(bundle, vault, progress=silent)
    assert bundle.is_dir() and other.is_dir()

    dest = move_bundle(bundle, vault, force=True, progress=silent)
    assert dest == other
    assert (dest / "curation.md").read_text(encoding="utf-8") == "# curated by hand\n"
    assert not bundle.exists()


def test_mv_dry_run_moves_nothing_and_ends_with_the_real_command(
    tmp_path, vault, test_provider, capsys
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    before = _tree(bundle)
    argv = ["--vault", str(src_vault), "mv", BUNDLE_ID, str(vault)]

    assert main([*argv, "-n"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Would move {BUNDLE_ID}\n")
    assert f"  to:       {vault / 'test--acme--toy' / 'aaaaaaaaaaaa'}  (new)" in out
    assert "Dry run: nothing copied, nothing removed. To move:" in out
    assert out.rstrip().endswith("  " + _real(*argv))
    assert _tree(bundle) == before
    assert list(vault.rglob("*")) == []

    # The refusal is the same refusal.
    with pytest.raises(SystemExit, match="destination vault does not exist"):
        main(["--vault", str(src_vault), "mv", BUNDLE_ID, str(tmp_path / "nope"), "-n"])


def test_mv_payload_is_never_modified(tmp_path, vault, test_provider, monkeypatch):
    """Both paths: the bytes under model/ at the destination are the source's."""
    for same_device in (True, False):
        src_vault = tmp_path / f"src-{same_device}"
        src_vault.mkdir()
        dest_vault = tmp_path / f"dst-{same_device}"
        dest_vault.mkdir()
        if same_device:
            test_provider.add_repo("acme/toy", model_files())
        bundle = archive_quiet("test:acme/toy", vault=src_vault)
        before = _payload_hashes(bundle)
        monkeypatch.setattr(relocate, "_same_device", lambda a, b, s=same_device: s)
        dest = move_bundle(bundle, dest_vault, progress=silent)
        assert _payload_hashes(dest) == before


# --- cp ---------------------------------------------------------------------


def test_cp_copies_verifies_and_records_the_replica_on_both_sides(
    tmp_path, vault, test_provider, capsys
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    payload_before = _payload_hashes(bundle)
    root_before = _tree(bundle)

    assert main(["--vault", str(src_vault), "cp", BUNDLE_ID, str(vault)]) == 0
    out = capsys.readouterr().out
    dest = vault / "test--acme--toy" / "aaaaaaaaaaaa"

    assert out.startswith(f"Copying {BUNDLE_ID}\n")
    assert "how:      copy" in out and "keep the source" in out
    assert "Verifying the copy at the destination" in out
    assert f"Copied {BUNDLE_ID} → {dest}" in out
    assert "replica recorded in both manifests" in out

    # Both exist; the payloads are identical; the source payload is untouched.
    assert bundle.is_dir() and dest.is_dir()
    assert _payload_hashes(dest) == payload_before
    assert _payload_hashes(bundle) == payload_before
    unchanged = {
        rel: data
        for rel, data in root_before.items()
        if rel not in ("manifest.json", "README.md")
    }
    after = _tree(bundle)
    assert {rel: after[rel] for rel in unchanged} == unchanged
    assert (bundle / "hydration.json").is_file(), "vault-local file stays put"
    assert not (dest / "hydration.json").exists()
    assert not (dest / "transfer.lock").exists()
    assert not (bundle / "transfer.lock").exists()
    assert not list(vault.glob("*/.cp-*")), "staging directory is gone"

    source_manifest = load_manifest(bundle)
    copy_manifest = load_manifest(dest)
    assert source_manifest["archive"]["backup_status"] == "replicated"
    assert copy_manifest["archive"]["backup_status"] == "replicated"
    assert [r["location"] for r in source_manifest["archive"]["replicas"]] == [
        str(dest.resolve())
    ]
    assert [r["location"] for r in copy_manifest["archive"]["replicas"]] == [
        str(bundle.resolve())
    ]
    assert copy_manifest["archive"]["location"] == str(dest.resolve())
    assert source_manifest["archive"]["location"] == str(bundle.resolve())
    assert "moves" not in copy_manifest["archive"], "a copy is not a move"
    assert copy_manifest["validation"]["checksum_verification"]["status"] == "pass"
    assert (
        copy_manifest["inventory"]["bundle_hash"]
        == source_manifest["inventory"]["bundle_hash"]
    )
    assert str(dest) in (dest / "VERIFICATION.md").read_text(encoding="utf-8")
    assert "Replicas: 1" in (bundle / "README.md").read_text(encoding="utf-8")
    assert "Replicas: 1" in (dest / "README.md").read_text(encoding="utf-8")

    for root in (src_vault, vault):
        assert main(["--vault", str(root), "list", "--ids"]) == 0
        assert BUNDLE_ID in capsys.readouterr().out


def test_cp_verification_failure_records_nothing_anywhere(
    tmp_path, vault, test_provider, monkeypatch
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    before = _tree(bundle)
    _rotting_copy(monkeypatch)

    with pytest.raises(SystemExit, match="verification FAILED at the destination"):
        copy_bundle(bundle, vault, progress=silent)

    assert _tree(bundle) == before, "source manifest not stamped, payload untouched"
    assert load_manifest(bundle)["archive"]["replicas"] == []
    assert list(vault.rglob("manifest.json")) == []
    assert not list(vault.glob("*/.cp-*"))


def test_cp_refusals_and_force_refresh_dedupes_the_replica(
    tmp_path, vault, test_provider
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)

    with pytest.raises(SystemExit, match="destination vault does not exist"):
        copy_bundle(bundle, tmp_path / "not-mounted", progress=silent)
    assert not (tmp_path / "not-mounted").exists()
    with pytest.raises(SystemExit, match="already in"):
        copy_bundle(bundle, src_vault, progress=silent)

    dest = copy_bundle(bundle, vault, progress=silent)
    with pytest.raises(SystemExit, match=r"already exists \(use --force"):
        copy_bundle(bundle, vault, progress=silent)

    # Refreshing the backup: one replica entry per location, not two.
    (bundle / "curation.md").write_text("# revised notes\n", encoding="utf-8")
    again = copy_bundle(bundle, vault, force=True, progress=silent)
    assert again == dest
    assert (dest / "curation.md").read_text(encoding="utf-8") == "# revised notes\n"
    for side in (bundle, dest):
        replicas = load_manifest(side)["archive"]["replicas"]
        assert len(replicas) == 1, replicas
    assert load_manifest(bundle)["archive"]["replicas"][0]["location"] == str(
        dest.resolve()
    )
    assert load_manifest(dest)["archive"]["replicas"][0]["location"] == str(
        bundle.resolve()
    )


def test_cp_dry_run_copies_nothing_and_ends_with_the_real_command(
    tmp_path, vault, test_provider, capsys
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    before = _tree(bundle)
    argv = ["--vault", str(src_vault), "cp", BUNDLE_ID, str(vault)]

    assert main([*argv, "-n"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Would copy {BUNDLE_ID}\n")
    assert "how:      copy" in out and "disk:     needs" in out
    assert "Dry run: nothing copied. To copy:" in out
    assert out.rstrip().endswith("  " + _real(*argv))
    assert _tree(bundle) == before
    assert list(vault.rglob("*")) == []
