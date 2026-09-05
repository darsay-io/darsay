"""``darsay mv`` / ``darsay cp`` — relocate or replicate a registered bundle.

rsync into the two-level layout stays a first-class copy (see
``test_transfer.py``); these verbs fold the verify-then-act bookkeeping
into one command and must never do less than that contract.
"""

from __future__ import annotations

import json
import shlex
import shutil
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
    report_md = (dest / "VERIFICATION.md").read_text(encoding="utf-8")
    assert f"Re-run with: `darsay verify {dest}`" in report_md
    assert f"- **Where:** `{bundle.resolve()}`" in report_md, (
        "a rename verifies nothing; the last run was before the move, and says where"
    )
    assert report_md.count(str(bundle.resolve())) == 1

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

    # A destination holding payload files this record does not list is another
    # pin of the same revision: refused, naming the files and `rm` for either side.
    other = archive_quiet("test:acme/toy", vault=vault)
    (other / "model" / "extra-print.gguf").write_bytes(b"not in the record")
    before = _tree(bundle)
    with pytest.raises(SystemExit) as refused:
        move_bundle(bundle, vault, progress=silent)
    message = str(refused.value)
    assert "already holds 1 payload file this bundle's record does not list" in message
    assert "model/extra-print.gguf  (17 B)" in message
    assert f"darsay --vault {vault} rm {BUNDLE_ID} --yes" in message
    assert _tree(bundle) == before
    assert (other / "model" / "extra-print.gguf").is_file()


def test_cp_all_copies_every_registered_bundle(tmp_path, test_provider, capsys):
    src = tmp_path / "src"
    src.mkdir()
    test_provider.add_repo("acme/toy", model_files())
    test_provider.add_repo("acme/other", model_files())
    archive_quiet("test:acme/toy", vault=src)
    archive_quiet("test:acme/other", vault=src)
    dest = tmp_path / "drive"
    dest.mkdir()

    assert main(["--vault", str(src), "cp", "--all", str(dest)]) == 0
    ids = {p.parent.parent.name for p in dest.glob("*/*/manifest.json")}
    assert ids == {"test--acme--toy", "test--acme--other"}
    # Sources kept: cp is a replica.
    assert (src / "test--acme--toy" / "aaaaaaaaaaaa" / "manifest.json").is_file()


def test_verify_all_and_named_many(tmp_path, test_provider, capsys):
    src = tmp_path / "src"
    src.mkdir()
    test_provider.add_repo("acme/toy", model_files())
    test_provider.add_repo("acme/other", model_files())
    a = archive_quiet("test:acme/toy", vault=src)
    b = archive_quiet("test:acme/other", vault=src)

    assert main(["--vault", str(src), "verify", "--all"]) == 0
    out = capsys.readouterr().out
    assert "Verified 2 bundles: all pass." in out

    # A named pair verifies too; a summary only for more than one.
    assert main(["--vault", str(src), "verify", str(a), str(b)]) == 0
    assert "Verified 2 bundles: all pass." in capsys.readouterr().out

    # A tampered payload makes the batch exit non-zero and name the bundle.
    (b / "model" / "model.safetensors").write_bytes(b"tampered")
    assert main(["--vault", str(src), "verify", "--all"]) == 1
    out = capsys.readouterr().out
    assert "1 FAILED" in out and "test--acme--other" in out


def test_batch_verbs_refuse_bundles_with_all(tmp_path, test_provider):
    src = tmp_path / "src"
    src.mkdir()
    _registered_bundle(src, test_provider)
    dest = tmp_path / "drive"
    dest.mkdir()
    with pytest.raises(SystemExit, match="not both"):
        main(["--vault", str(src), "cp", "--all", BUNDLE_ID, str(dest)])
    with pytest.raises(SystemExit, match="needs a bundle"):
        main(["--vault", str(src), "verify"])


def test_cp_refuses_an_unmounted_volume_stub(tmp_path, test_provider, monkeypatch):
    """An empty folder where a volume mounts but is not mounted: cp refuses
    rather than filling the boot disk (the leftover after an eject)."""
    import darsay.vault as vault_mod

    mounts = tmp_path / "mounts"
    monkeypatch.setattr(vault_mod, "MOUNT_ROOTS", (str(mounts),))
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)

    stub = mounts / "USB"
    stub.mkdir(parents=True)  # exists, empty, not a mount point
    with pytest.raises(SystemExit, match="not a mounted volume"):
        copy_bundle(bundle, stub, progress=silent)
    assert list(stub.iterdir()) == [], "cp wrote nothing into the stub"

    missing = mounts / "GONE"
    with pytest.raises(SystemExit, match="may not be mounted under"):
        copy_bundle(bundle, missing, progress=silent)


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


def test_mv_onto_an_empty_directory_is_a_fresh_landing(tmp_path, vault, test_provider):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    (vault / "test--acme--toy" / "aaaaaaaaaaaa").mkdir(parents=True)

    logs: list[str] = []
    dest = move_bundle(bundle, vault, progress=logs.append)
    assert any("  (new)" in line for line in logs)
    assert load_manifest(dest)["archive"]["moves"][-1]["method"] == "rename"
    assert not bundle.exists()


# --- landing on a copy already there ------------------------------------------


def _rsync(bundle: Path, dest_vault: Path) -> Path:
    """What ``rsync -a`` leaves: the whole directory at ``<vault>/<name>/<rev>/``."""
    dest = dest_vault / bundle.parent.name / bundle.name
    shutil.copytree(bundle, dest)
    return dest


def test_mv_lands_on_an_rsync_made_before_the_record_was_migrated(
    tmp_path, vault, capsys
):
    """rsync, migrate, mv — the order an operator actually does them in.

    The copy at the destination carries the older record. ``mv`` hashes
    its payload in place, copies nothing, replaces the record with the
    migrated one, and removes the source: one read of the destination.
    """
    from darsay import SCHEMA_VERSION
    from tests.integration.test_migrate import REV, TOY, place

    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = place(src_vault, TOY)
    bundle_id = f"{TOY}@{REV}"
    copy = _rsync(bundle, vault)
    assert json.loads((copy / "manifest.json").read_text())["schema_version"] == "1.8.0"
    payload_before = _payload_hashes(bundle)

    with pytest.raises(SystemExit, match="predates this darsay"):
        main(["--vault", str(src_vault), "mv", bundle_id, str(vault)])
    assert main(["--vault", str(src_vault), "migrate", bundle_id]) == 0
    capsys.readouterr()

    assert main(["--vault", str(src_vault), "mv", bundle_id, str(vault)]) == 0
    out = capsys.readouterr().out
    assert f"  to:       {copy}  (exists)" in out
    assert (
        "how:      all 7 payload files are already there at the recorded size — "
        "hash them in place, copy nothing, then remove the source"
    ) in out
    assert (
        f"Landing on {copy}: hashing 7 payload files in place, copying 0 (0 B)" in out
    )
    assert "Verification: PASS (7 files; 0 modified, 0 missing, 0 extra)" in out
    assert (
        f"Moved {bundle_id} → {copy}  (7 payload files verified at the destination "
        "— 7 already there, 0 copied — before the source was removed)"
    ) in out

    assert not bundle.exists() and not bundle.parent.exists()
    assert _payload_hashes(copy) == payload_before
    assert not list(vault.glob("*/.mv-*"))
    manifest = load_manifest(copy)
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["archive"]["migrations"][-1]["from_schema"] == "1.8.0"
    move = manifest["archive"]["moves"][-1]
    assert move["from_location"] == str(bundle.resolve())
    assert (move["method"], move["adopted"], move["copied"]) == ("adopt", 7, 0)
    assert "replaced" not in move
    assert manifest["archive"]["location"] == str(copy.resolve())
    assert manifest["validation"]["checksum_verification"]["status"] == "pass"
    assert str(copy) in (copy / "VERIFICATION.md").read_text(encoding="utf-8")
    assert not (copy / "transfer.lock").exists()

    assert main(["--vault", str(vault), "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["bundle_id"], r["status"]) for r in rows] == [(bundle_id, "have")]


def test_mv_lands_copying_only_what_is_missing_or_wrong(
    tmp_path, vault, test_provider, capsys
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    before = _payload_hashes(bundle)
    total = len(before)
    copy = _rsync(bundle, vault)
    # Since the rsync: one file rotted in place, one was cut short, one never
    # arrived, the notes went stale, and the destination hydrated its own copy.
    rotted = copy / "model" / "model.safetensors"
    rotted.write_bytes(b"x" * rotted.stat().st_size)
    (copy / "model" / "config.json").write_bytes(b"{")
    (copy / "model" / "generation_config.json").unlink()
    (copy / "curation.md").write_text("# stale notes\n", encoding="utf-8")
    (copy / "hydration.json").write_text('{"engine": "dest"}\n', encoding="utf-8")

    assert main(["--vault", str(src_vault), "mv", BUNDLE_ID, str(vault)]) == 0
    out = capsys.readouterr().out
    assert (
        f"how:      {total - 2} of {total} payload files are already there at the "
        "recorded size — hash them in place, copy the other 2 ("
    ) in out
    assert (
        "notes:    the destination's curation.md differs and is replaced by this "
        "bundle's"
    ) in out
    assert (
        "model/model.safetensors  differs from the record at the destination — "
        "copying from the source"
    ) in out
    assert (
        f"{total} payload files verified at the destination — {total - 3} already "
        "there, 3 copied"
    ) in out

    assert not bundle.exists()
    assert _payload_hashes(copy) == before
    assert (copy / "curation.md").read_text(encoding="utf-8") == "# curated by hand\n"
    assert (copy / "hydration.json").read_text(encoding="utf-8") == (
        '{"engine": "dest"}\n'
    ), "the destination's vault-local file is its own"
    assert not (copy / "transfer.lock").exists()
    move = load_manifest(copy)["archive"]["moves"][-1]
    assert (move["method"], move["adopted"], move["copied"], move["replaced"]) == (
        "adopt",
        total - 3,
        3,
        ["model/model.safetensors"],
    )
    history = json.loads((copy / "verification.json").read_text(encoding="utf-8"))
    assert history["latest"]["checksum"]["status"] == "pass"
    assert history["latest"]["checksum"]["files_checked"] == total


def test_mv_onto_existing_keeps_notes_written_there_when_the_bundle_has_none(
    tmp_path, vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = archive_quiet("test:acme/toy", vault=src_vault)
    (bundle / "curation.md").unlink()  # the template archive seeds; no notes yet
    copy = _rsync(bundle, vault)
    (copy / "curation.md").write_text(
        "# written at the destination\n", encoding="utf-8"
    )

    assert main(["--vault", str(src_vault), "mv", BUNDLE_ID, str(vault)]) == 0
    out = capsys.readouterr().out
    assert (
        "notes:    the destination's curation.md is kept — this bundle has none" in out
    )
    assert (copy / "curation.md").read_text(encoding="utf-8") == (
        "# written at the destination\n"
    )


def test_mv_dry_run_onto_existing_touches_nothing(
    tmp_path, vault, test_provider, capsys
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    copy = _rsync(bundle, vault)
    (copy / "model" / "generation_config.json").unlink()
    source_before = _tree(bundle)
    copy_before = _tree(copy)

    assert main(["--vault", str(src_vault), "mv", BUNDLE_ID, str(vault), "-n"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Would move {BUNDLE_ID}\n")
    assert f"  to:       {copy}  (exists)" in out
    assert (
        "already there at the recorded size — hash them in place, copy the other 1 ("
        in out
    )
    assert "Dry run: nothing copied, nothing removed. To move:" in out
    assert _tree(bundle) == source_before
    assert _tree(copy) == copy_before


def test_mv_landing_failure_leaves_both_sides_as_they_were(
    tmp_path, vault, test_provider, monkeypatch
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    copy = _rsync(bundle, vault)
    (copy / "model" / "model.safetensors").unlink()
    source_before = _tree(bundle)
    record_before = (copy / "manifest.json").read_bytes()
    _rotting_copy(monkeypatch)

    with pytest.raises(SystemExit, match="does not match the record after copying"):
        move_bundle(bundle, vault, progress=silent)

    assert _tree(bundle) == source_before
    assert (copy / "manifest.json").read_bytes() == record_before
    assert not (bundle / "transfer.lock").exists()
    assert not (copy / "transfer.lock").exists()


def test_rsync_command_is_the_line_the_docs_show():
    from darsay.relocate import rsync_command

    line = rsync_command(Path("/src/v/n/rev"), Path("/Volumes/my nas/v/n/rev"))
    assert line == (
        "rsync -aP --exclude=hydration.json --exclude=transfer.lock "
        "--exclude=.DS_Store /src/v/n/rev/ '/Volumes/my nas/v/n/rev'/"
    )
    assert "--delete" not in line


def _network(monkeypatch) -> None:
    import darsay.transfer as transfer

    monkeypatch.setattr(transfer, "is_network_filesystem", lambda path: True)


def test_mv_onto_existing_over_a_network_mount_prints_the_local_way(
    tmp_path, vault, test_provider, monkeypatch
):
    from darsay.readme_gen import human_size
    from darsay.relocate import rsync_command

    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    copy = _rsync(bundle, vault)
    payload_bytes = load_manifest(bundle)["inventory"]["total_size_bytes"]
    _network(monkeypatch)

    logs: list[str] = []
    move_bundle(bundle, vault, progress=logs.append, dry_run=True)
    text = "\n".join(logs)
    assert (
        f"  warning:  {vault} is a network mount: hashing the 7 payload files "
        f"already there reads {human_size(payload_bytes)} back over the wire. "
        "To hash the bytes where the disk is instead:"
    ) in text
    assert (
        f"{rsync_command(bundle, copy)}    # the record; payload files already there are skipped"
        in text
    )
    assert (
        f"darsay verify {copy}    # on the host that owns the disk, by its own "
        "path for that directory"
    ) in text
    assert f"darsay rm {BUNDLE_ID} --yes    # here, once that passed" in text
    assert (
        "Or name the host that owns the disk once, and every verb hashes there" in text
    )
    assert f"darsay --vault {shlex.quote(str(vault))} config host.ssh=" in text
    assert "Continuing over the wire" not in text, "a dry run continues nothing"

    logs.clear()
    move_bundle(bundle, vault, progress=logs.append)
    assert any(
        "Continuing over the wire; Ctrl-C at any point leaves the source untouched."
        in line
        for line in logs
    )
    assert not bundle.exists()


def test_mv_fresh_copy_over_a_network_mount_counts_two_trips(
    tmp_path, vault, test_provider, monkeypatch
):
    from darsay.readme_gen import human_size

    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)
    _network(monkeypatch)
    total = sum(p.stat().st_size for _rel, p in relocate.bundle_files(bundle))

    logs: list[str] = []
    move_bundle(bundle, vault, progress=logs.append, dry_run=True)
    text = "\n".join(logs)
    assert (
        f"copying {human_size(total)} over the wire and reading it all back to "
        "verify is two trips" in text
    )
    assert "    # one trip" in text
    assert f"darsay rm {BUNDLE_ID} --yes" in text


def test_cp_over_a_network_mount_has_no_rm_line(
    tmp_path, vault, test_provider, monkeypatch
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    _network(monkeypatch)

    logs: list[str] = []
    copy_bundle(bundle, vault, progress=logs.append, dry_run=True)
    text = "\n".join(logs)
    assert "rsync -aP" in text and "darsay verify" in text
    assert "darsay rm" not in text
    assert (
        "(that copy is not recorded as a replica; the bytes are just as good)" in text
    )


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


def test_cp_refusals_and_a_second_cp_refreshes_the_backup(
    tmp_path, vault, test_provider
):
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = _registered_bundle(src_vault, test_provider)
    payload = _payload_hashes(bundle)

    with pytest.raises(SystemExit, match="destination vault does not exist"):
        copy_bundle(bundle, tmp_path / "not-mounted", progress=silent)
    assert not (tmp_path / "not-mounted").exists()
    with pytest.raises(SystemExit, match="already in"):
        copy_bundle(bundle, src_vault, progress=silent)

    dest = copy_bundle(bundle, vault, progress=silent)

    # Refreshing the backup: the notes changed at the source and one file
    # rotted on the backup disk. The backup is hashed in place, the rotted
    # file is the only payload byte copied, and each side lists the other once.
    (bundle / "curation.md").write_text("# revised notes\n", encoding="utf-8")
    rotted = dest / "model" / "model.safetensors"
    rotted.write_bytes(b"x" * rotted.stat().st_size)
    logs: list[str] = []
    again = copy_bundle(bundle, vault, progress=logs.append)
    assert again == dest
    assert any(
        "how:      all 7 payload files are already there at the recorded size — "
        "hash them in place, copy nothing, and keep the source; both manifests "
        "record the replica" in line
        for line in logs
    )
    assert any(
        "model/model.safetensors  differs from the record at the destination" in line
        for line in logs
    )
    assert logs[-1].endswith(
        "(7 payload files verified at the destination — 6 already there, 1 copied; "
        "source kept, replica recorded in both manifests)"
    )
    assert _payload_hashes(dest) == payload
    assert (dest / "curation.md").read_text(encoding="utf-8") == "# revised notes\n"
    assert "moves" not in load_manifest(dest)["archive"], "a copy is not a move"
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
