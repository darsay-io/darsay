"""The far side: what the vault's config names, and how a mount hints at it."""

from __future__ import annotations

from pathlib import Path

import pytest

from darsay.config import vault_config_path
from darsay.farside import (
    FAR_SIDE_SH,
    FarSide,
    far_side_for,
    far_side_guess,
    far_side_label,
    hash_where_it_lives,
)
from darsay.hashing import hash_file


def test_far_side_guess_reads_the_host_off_a_mount_source():
    assert far_side_guess("//jeremy@pixel._smb._tcp.local/darsay") == "jeremy@pixel"
    assert far_side_guess("//nas/share") == "nas"
    assert far_side_guess("nas:/export/vault") == "nas"
    assert far_side_guess("root@box:/srv") == "root@box"
    assert far_side_guess("/dev/disk3s1") is None
    assert far_side_guess(None) is None
    assert far_side_guess("") is None


def test_far_side_where_maps_a_local_dir_onto_the_host(tmp_path):
    vault = tmp_path / "vault"
    bundle = vault / "acme--toy" / "aaaaaaaaaaaa"
    (bundle / "model").mkdir(parents=True)
    far = FarSide(ssh="root@nas", path="/volume1/darsay/vault")
    assert far.where(vault, bundle / "model") == (
        "/volume1/darsay/vault/acme--toy/aaaaaaaaaaaa/model"
    )
    assert far.where(vault, vault) == "/volume1/darsay/vault"


def test_far_side_for_reads_the_vault_file_only(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert far_side_for(vault) is None
    assert far_side_for(None) is None
    assert far_side_label(vault) is None

    vault_config_path(vault).write_text(
        '[host]\nssh = "root@nas"\npath = "/volume1/darsay/vault"\n',
        encoding="utf-8",
    )
    assert far_side_for(vault) == FarSide(ssh="root@nas", path="/volume1/darsay/vault")
    assert far_side_label(vault) == "on root@nas"

    # A machine's user file cannot claim a host for every vault.
    user = tmp_path / "user.toml"
    user.write_text('[host]\nssh = "me@laptop"\npath = "/x"\n', encoding="utf-8")
    monkeypatch.setenv("DARSAY_CONFIG", str(user))
    other = tmp_path / "other"
    other.mkdir()
    assert far_side_for(other) is None
    assert "host.ssh describes one vault's disk" in capsys.readouterr().err


def test_far_side_for_refuses_half_a_table(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    vault_config_path(vault).write_text('[host]\nssh = "root@nas"\n', encoding="utf-8")
    with pytest.raises(
        SystemExit, match="needs both ssh and path; host.path is not set"
    ):
        far_side_for(vault)


def test_hash_where_it_lives_here_matches_hash_file(tmp_path):
    vault = tmp_path / "vault"
    payload = vault / "acme--toy" / "aaaaaaaaaaaa" / "model"
    (payload / "sub").mkdir(parents=True)
    (payload / "a.bin").write_bytes(b"alpha")
    (payload / "sub" / "b.bin").write_bytes(b"beta")
    (payload / ".cache" / "huggingface").mkdir(parents=True)
    (payload / ".cache" / "huggingface" / "partial").write_bytes(b"x")

    everything = hash_where_it_lives(vault, payload, progress=lambda *_: None)
    assert set(everything) == {"a.bin", "sub/b.bin"}, "tool caches are not payload"
    assert everything["a.bin"] == {
        "sha256": hash_file(payload / "a.bin", with_blake3=False)["sha256"],
        "size": 5,
    }
    some = hash_where_it_lives(
        vault, payload, ["sub/b.bin", "missing.bin"], progress=lambda *_: None
    )
    assert set(some) == {"sub/b.bin"}, "a file that is not there is absent"


def test_far_side_script_is_posix_and_names_every_tool_it_accepts():
    assert FAR_SIDE_SH.startswith("#!/bin/sh\n")
    for tool in ("sha256sum", "shasum -a 256", "sha256 -q", "openssl dgst -sha256"):
        assert tool in FAR_SIDE_SH
    assert "python" not in FAR_SIDE_SH
    assert Path(".").is_dir()
