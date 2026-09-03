"""The far side, end to end, through a fake ssh that runs the script here.

The fake drops ssh's options and the host and hands the command line to a
local shell with the same stdin — exactly what a real ssh does on the
host — so the POSIX far-side script is the one under test, hasher and
all. The vault's ``[host] path`` is its own path here.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest

from darsay import relocate
from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.config import vault_config_path
from darsay.hashing import hash_file
from darsay.relocate import move_bundle
from darsay.verify import verify_bundle
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files

BUNDLE_ID = "test--acme--toy@aaaaaaaaaaaa"

FAKE_SSH = """#!/bin/sh
# A stand-in for ssh: run the command here, with the same stdin.
[ -n "${FAKE_SSH_LOG:-}" ] && printf '%s\\n' "$*" >> "$FAKE_SSH_LOG"
if [ "${FAKE_SSH:-}" = "down" ]; then
  echo "ssh: connect to host nas port 22: No route to host" >&2
  exit 255
fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift
if [ "${FAKE_SSH:-}" = "bare" ]; then export PATH="$FAKE_SSH_BARE_PATH"; fi
exec /bin/sh -c "$*"
"""


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "ssh"
    script.write_text(FAKE_SSH, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.delenv("FAKE_SSH", raising=False)
    return log


def _owned_by_nas(vault: Path) -> Path:
    """This vault, owned by a host the fake ssh reaches: its path there is its path here."""
    config = vault_config_path(vault)
    config.write_text(
        f'[host]\nssh = "root@nas"\npath = "{vault.resolve()}"\n', encoding="utf-8"
    )
    return config


def _payload_hashes(bundle: Path) -> dict[str, str]:
    return {
        p.relative_to(bundle).as_posix(): hash_file(p, with_blake3=False)["sha256"]
        for p in (bundle / "model").rglob("*")
        if p.is_file()
    }


def test_verify_hashes_on_the_far_side(vault, test_provider, fake_ssh, capsys):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    _owned_by_nas(vault)

    assert main(["--vault", str(vault), "verify", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert "Re-hashing 7 files on root@nas ..." in out
    assert "Verification: PASS (7 files; 0 modified, 0 missing, 0 extra)" in out
    calls = fake_ssh.read_text(encoding="utf-8").splitlines()
    assert calls == [
        f"-o ConnectTimeout=15 root@nas sh -s -- {shlex.quote(str(bundle.resolve()))}/model"
    ]

    (bundle / "model" / "config.json").write_bytes(b"{")
    assert main(["--vault", str(vault), "verify", str(bundle)]) == 1
    assert "1 modified" in capsys.readouterr().out


def test_mv_lands_hashing_on_the_far_side(
    tmp_path, vault, test_provider, fake_ssh, monkeypatch, capsys
):
    import darsay.transfer as transfer

    test_provider.add_repo("acme/toy", model_files())
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = archive_quiet("test:acme/toy", vault=src_vault)
    before = _payload_hashes(bundle)
    copy = vault / bundle.parent.name / bundle.name
    shutil.copytree(bundle, copy)
    rotted = copy / "model" / "model.safetensors"
    rotted.write_bytes(b"x" * rotted.stat().st_size)
    config = _owned_by_nas(vault)
    monkeypatch.setattr(transfer, "is_network_filesystem", lambda path: True)

    assert main(["--vault", str(src_vault), "mv", BUNDLE_ID, str(vault)]) == 0
    out = capsys.readouterr().out
    assert (
        "how:      all 7 payload files are already there at the recorded size — "
        "hash them on root@nas, copy nothing, then remove the source"
    ) in out
    assert (
        f"hash:     on root@nas — [host] in {config}; nothing is read back over "
        "the wire"
    ) in out
    assert "warning:" not in out
    assert (
        f"Landing on {copy}: hashing 7 payload files on root@nas, copying 0 (0 B)"
    ) in out
    assert "model/model.safetensors  differs from the record at the destination" in out
    assert (
        "7 payload files verified at the destination — 6 already there, 1 copied" in out
    )

    calls = fake_ssh.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2, calls
    everything = " ".join(sorted(p.name for p in (copy / "model").iterdir()))
    assert calls[0].endswith(f"/model {everything}"), "every file there at its size"
    assert calls[1].endswith("/model model.safetensors"), (
        "only the copy is hashed again"
    )
    assert not bundle.exists()
    assert _payload_hashes(copy) == before
    manifest = load_manifest(copy)
    assert manifest["archive"]["moves"][-1]["replaced"] == ["model/model.safetensors"]
    assert manifest["validation"]["checksum_verification"]["status"] == "pass"


def test_mv_fresh_copy_verifies_its_staging_copy_on_the_far_side(
    tmp_path, vault, test_provider, fake_ssh, monkeypatch
):
    test_provider.add_repo("acme/toy", model_files())
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = archive_quiet("test:acme/toy", vault=src_vault)
    _owned_by_nas(vault)
    monkeypatch.setattr(relocate, "_same_device", lambda a, b: False)

    logs: list[str] = []
    dest = move_bundle(bundle, vault, progress=logs.append)
    assert any(
        "re-hash the 7 payload files at the destination on root@nas" in line
        for line in logs
    )
    assert any("Re-hashing 7 files on root@nas ..." in line for line in logs)
    calls = fake_ssh.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert (
        f"{vault.resolve()}/test--acme--toy/.mv-aaaaaaaaaaaa/aaaaaaaaaaaa/model"
        in (calls[0])
    ), "the staging copy is hashed where it is, by its path on the host"
    assert load_manifest(dest)["archive"]["moves"][-1]["method"] == "copy"
    assert not bundle.exists()


def test_a_far_side_that_is_down_is_a_refusal_naming_the_fix(
    vault, test_provider, fake_ssh, monkeypatch
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    config = _owned_by_nas(vault)
    monkeypatch.setenv("FAKE_SSH", "down")

    with pytest.raises(SystemExit) as refused:
        verify_bundle(bundle, progress=silent)
    message = str(refused.value)
    assert (
        "error: cannot hash on root@nas: ssh: connect to host nas port 22: "
        "No route to host"
    ) in message
    assert f"[host] in {config} names it as the host that owns the disk" in message
    assert "drop the table to hash over the wire" in message


def test_a_far_side_without_a_sha256_tool_says_so(
    tmp_path, vault, test_provider, fake_ssh, monkeypatch
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    _owned_by_nas(vault)
    bare = tmp_path / "bare-bin"
    bare.mkdir()
    (bare / "sh").symlink_to("/bin/sh")
    monkeypatch.setenv("FAKE_SSH", "bare")
    monkeypatch.setenv("FAKE_SSH_BARE_PATH", str(bare))

    with pytest.raises(SystemExit) as refused:
        verify_bundle(bundle, progress=silent)
    assert (
        "cannot hash on root@nas: root@nas has no sha256sum, shasum, sha256, or openssl"
    ) in str(refused.value)


def test_a_wrong_host_path_is_caught_not_trusted(
    tmp_path, vault, test_provider, fake_ssh
):
    """[host] path naming another place must not verify the wrong bytes."""
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / bundle.parent.name / bundle.name / "model").mkdir(parents=True)
    vault_config_path(vault).write_text(
        f'[host]\nssh = "root@nas"\npath = "{elsewhere}"\n', encoding="utf-8"
    )

    with pytest.raises(SystemExit) as refused:
        verify_bundle(bundle, progress=silent)
    message = str(refused.value)
    assert "root@nas has no LICENSE under" in message
    assert "[host] path in" in message
    assert "does not name this vault's location on that host" in message


def test_far_side_git_blob_sha1_matches_the_local_one(vault, test_provider, fake_ssh):
    import subprocess

    from darsay.farside import hash_where_it_lives
    from darsay.hashing import iter_payload_files

    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    _owned_by_nas(vault)
    payload = bundle / "model"
    files = sorted(rel for rel, _path in iter_payload_files(payload))

    far = hash_where_it_lives(vault, payload, files, git_sha1=True, progress=silent)
    assert set(far) == set(files)
    for rel in files:
        local = hash_file(payload / rel, with_blake3=False, with_git_sha1=True)
        assert far[rel]["sha256"] == local["sha256"], rel
        assert far[rel]["git_sha1"] == local["git_sha1"], rel
    calls = fake_ssh.read_text(encoding="utf-8").splitlines()
    assert calls[0].startswith("-o ConnectTimeout=15 root@nas sh -s -- -g ")
    if shutil.which("git"):
        by_git = subprocess.run(
            ["git", "hash-object", str(payload / "config.json")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert far["config.json"]["git_sha1"] == by_git


def test_assemble_rehash_hashes_dest_on_the_far_side(
    vault, test_provider, tmp_path, fake_ssh, monkeypatch
):
    from darsay.transfer import assemble_partials, load_ledger
    from tests.integration.test_transfer import (
        _archive_half,
        _hashed_under,
        _plant_bundle_in_vault,
        _track_hashes,
    )

    test_provider.add_repo("acme/big", model_files(param_shape=[64, 64]))
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    source = _archive_half("test:acme/big", vault=laptop)
    dest = _plant_bundle_in_vault(source, vault)
    _owned_by_nas(vault)
    monkeypatch.setattr("darsay.transfer.is_network_filesystem", lambda path: True)
    hashed = _track_hashes(monkeypatch)

    logs: list[str] = []
    assemble_partials([source], vault, progress=logs.append, handoff=True, rehash=True)
    text = "\n".join(str(item) for item in logs)
    assert _hashed_under(hashed, dest / "model") == [], "nothing at dest is read here"
    assert "warning: the destination is on a network mount" not in text
    assert "against the pin on root@nas" in text
    assert "re-hashed dest against the pin on root@nas" in text
    calls = fake_ssh.read_text(encoding="utf-8").splitlines()
    assert calls, "the far side was asked"
    wants_git = any(
        not item.get("lfs_sha256") for item in load_ledger(dest)["expected"]
    )
    assert any(" -g " in call for call in calls) == wants_git, (
        "files git holds itself are checked by blob sha1; LFS files by sha256"
    )
    verified = {
        rel: state
        for rel, state in load_ledger(dest)["files"].items()
        if state.get("status") == "verified"
    }
    assert verified
    assert all(
        state["verified_against_upstream"] is not False for state in verified.values()
    )
    assert all(state["blake3"] is None for state in verified.values())


def test_assemble_adopts_unrecorded_dest_bytes_on_the_far_side(
    vault, test_provider, tmp_path, fake_ssh, monkeypatch, capsys
):
    from darsay.transfer import assemble_partials
    from tests.integration.test_transfer import (
        _archive_half,
        _hashed_under,
        _plant_bundle_in_vault,
        _track_hashes,
    )

    test_provider.add_repo("acme/big", model_files(param_shape=[64, 64]))
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    source = _archive_half("test:acme/big", vault=laptop)
    dest = _plant_bundle_in_vault(source, vault)
    (dest / "transfer.json").unlink()  # an rsync of model/ alone: bytes, no record
    _owned_by_nas(vault)
    monkeypatch.setattr("darsay.transfer.is_network_filesystem", lambda path: True)
    hashed = _track_hashes(monkeypatch)

    # The plan says where the hashing happens and raises no network warning.
    assert main(["--vault", str(vault), "assemble", str(source), "-n"]) == 0
    plan = capsys.readouterr().out
    assert "already at dest" in plan and "hashed on root@nas" in plan
    assert "dest is on a network mount" not in plan

    logs: list[str] = []
    assemble_partials([source], vault, progress=logs.append)
    text = "\n".join(str(item) for item in logs)
    assert _hashed_under(hashed, dest / "model") == []
    assert "Hashing" in text and "already at the destination" in text
    assert "on root@nas" in text
    assert "warning: the destination is on a network mount" not in text
