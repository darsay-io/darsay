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
