from __future__ import annotations

import darsay.identity as identity
from darsay.identity import machine_name, same_machine


def test_machine_name_strips_domain_and_honors_override(monkeypatch):
    monkeypatch.delenv("DARSAY_MACHINE_ID", raising=False)
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "Foo.local")
    assert machine_name() == "Foo"
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "plainbox")
    assert machine_name() == "plainbox"
    monkeypatch.setenv("DARSAY_MACHINE_ID", "pinned-id")
    assert machine_name() == "pinned-id"


def test_same_machine_ignores_domain_and_case():
    # the flapping macOS case: same box, different suffixes
    assert same_machine("Foo.local", "Foo.lan")
    assert same_machine("Foo.local", "foo")
    assert same_machine("FOO", "foo")
    # different machines, and an empty record, are not the same
    assert not same_machine("foo", "bar")
    assert not same_machine(None, "foo")
    assert not same_machine("", "foo")
