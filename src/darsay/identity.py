"""A stable name for the machine that attested a bundle.

``archive.host`` (and the ``host`` on each verification, replica, move, and
transfer session) records which machine last read a bundle's bytes, so
``migrate`` and ``verify`` can say "this bundle passed here and has not
moved" instead of sending an operator to re-hash a payload nothing touched.

A raw hostname is not stable on a laptop: macOS appends ``.local``, a DHCP
network appends its domain, and both come and go. So the identity is the
first label of the hostname, and ``$DARSAY_MACHINE_ID`` overrides it for a
machine that wants to pin one. Comparison is case-insensitive and ignores
the domain on both sides, so a record written by an older darsay that
stored a full hostname still matches the same machine today.
"""

from __future__ import annotations

import os
import socket


def _short(name: str | None) -> str:
    """The first label of a hostname, without the domain."""
    return (name or "").split(".")[0].strip()


def machine_name() -> str:
    """This machine's stable name for ``archive.host``."""
    override = (os.environ.get("DARSAY_MACHINE_ID") or "").strip()
    if override:
        return override
    hostname = socket.gethostname()
    return _short(hostname) or hostname


def same_machine(recorded: str | None, current: str | None) -> bool:
    """Whether two recorded host names denote the same machine.

    Domain-insensitive and case-insensitive, so ``Foo.local``, ``Foo.lan``,
    and ``foo`` are one machine.
    """
    a, b = _short(recorded).lower(), _short(current).lower()
    return bool(a) and a == b
