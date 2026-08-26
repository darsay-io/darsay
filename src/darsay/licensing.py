"""License identification and rights flags.

The flags table covers common model licenses so a bundle records, up front,
whether commercial use / redistribution / modification are permitted. Anything
not in the table is archived with null flags and marked for manual review —
the tool records what upstream ships and never fabricates license text.
"""

from __future__ import annotations

from pathlib import Path

# spdx_id -> rights summary. Flags are a curator convenience, not legal advice.
LICENSE_INFO = {
    "apache-2.0": {
        "name": "Apache License 2.0",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": True,
        "trademark_terms": "Section 6: no trademark rights granted beyond what is needed to describe origin of the work.",
    },
    "mit": {
        "name": "MIT License",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": None,
    },
    "bsd-3-clause": {
        "name": "BSD 3-Clause License",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": "Clause 3: names of contributors may not be used to endorse derived products.",
    },
    "cc-by-4.0": {
        "name": "Creative Commons Attribution 4.0",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": None,
    },
    "cc-by-nc-4.0": {
        "name": "Creative Commons Attribution-NonCommercial 4.0",
        "commercial_use": False,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": None,
    },
    "cc-by-sa-4.0": {
        "name": "Creative Commons Attribution-ShareAlike 4.0",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": None,
    },
    "gpl-3.0": {
        "name": "GNU General Public License v3.0",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": True,
        "trademark_terms": None,
    },
    "llama2": {
        "name": "Llama 2 Community License",
        "commercial_use": True,  # with the >700M MAU carve-out
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": "Meta trademarks not licensed; 'Llama' naming requirements apply to derivatives.",
    },
    "llama3": {
        "name": "Llama 3 Community License",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": "Meta trademarks not licensed; 'Llama' naming requirements apply to derivatives.",
    },
    "gemma": {
        "name": "Gemma Terms of Use",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": "Google trademarks not licensed.",
    },
    "openrail": {
        "name": "OpenRAIL",
        "commercial_use": True,
        "redistribution": True,
        "modification": True,
        "attribution_required": True,
        "patent_grant": False,
        "trademark_terms": None,
    },
}

LICENSE_FILE_PATTERNS = (
    "LICENSE*",
    "LICENCE*",
    "COPYING*",
    "NOTICE*",
    "license*",
    "notice*",
)


def find_license_files(payload_root: Path) -> list[str]:
    """Return payload-relative POSIX paths of license/notice files shipped upstream."""
    found = set()
    for pattern in LICENSE_FILE_PATTERNS:
        for p in payload_root.glob(pattern):
            if p.is_file():
                found.add(p.relative_to(payload_root).as_posix())
    return sorted(found)


def build_licensing_record(
    spdx_id: str | None, payload_root: Path, gated: bool = False
) -> dict:
    info = LICENSE_INFO.get(spdx_id) if spdx_id else None
    license_files = find_license_files(payload_root)
    notes = []
    if not license_files:
        notes.append(
            "Upstream repository ships no license text file; license identified only by "
            "repo metadata tag. Verify terms at the source before redistribution."
        )
    if info is None and spdx_id is not None:
        notes.append(
            f"License id '{spdx_id}' not in the rights-flags table; review manually."
        )
    if gated:
        notes.append(
            "Upstream repo is gated: the files were obtained under an access agreement "
            "that is not part of the payload (see source.access). Review those terms "
            "before redistributing this bundle, whatever the license flags say."
        )
    return {
        "spdx_id": spdx_id,
        "name": info["name"] if info else None,
        "license_files": [f"{payload_root.name}/{f}" for f in license_files],
        "commercial_use": info["commercial_use"] if info else None,
        "redistribution": info["redistribution"] if info else None,
        "modification": info["modification"] if info else None,
        "attribution_required": info["attribution_required"] if info else None,
        "patent_grant": info["patent_grant"] if info else None,
        "trademark_terms": info["trademark_terms"] if info else None,
        "needs_manual_review": info is None or gated,
        "notes": " ".join(notes) if notes else None,
    }
