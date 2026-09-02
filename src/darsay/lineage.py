"""Lineage: family, generation, member, variants — read from a work's name.

A publisher's name for a model is the one lineage declaration every repo
carries: ``Qwen3.8-2.4T-A95B`` says *Qwen*, generation *3.8*, the
*2.4T-A95B* member. This module reads that grammar and nothing else; it
never opens a file and never guesses beyond the tokens. Every result is
labeled ``read_from: "name"`` wherever it is recorded, and the same grammar
runs on darsay.io (``website/src/lib/lineage.ts``) against the same
fixture table, so a board and the CLI agree on every family.

The grammar, in order, over the name split on ``-`` and ``_``:

1. **Family** is the run of leading alphabetic tokens. A token that glues
   letters to a version (``Qwen3.8``, ``esm3``) splits: the letters join
   the family, the number is the generation.
2. **Generation** is the first version-shaped token after the family:
   ``3.8``, ``K3``, ``V3.2``, ``H3`` — up to two prefix letters, digits
   and dots, and at most one trailing letter that is not a size unit
   (``4.5V`` is generation ``4.5``, member ``V``).
3. **Size** tokens (``27B``, ``2.4T``, ``A95B``, ``8x7B``) give parameter
   counts and stay in the member.
4. **Variants** are a closed set (``base``, ``instruct``, ``chat``,
   ``thinking``, ``abliterated``, ``uncensored``, ``distill``) and
   **formats** another (``gguf``, ``fp8``, ``awq`` …); both leave the
   member.
5. **Member** is whatever remains after the generation, joined by ``-``;
   empty for a family's flagship (``Kimi-K3``).

Parent edges — finetune, adapter, merge, quantized — are not read from
names. They come from upstream declarations (``providers``) and are
recorded beside the name-derived facts, each with its provenance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

READ_FROM = "name"

# Generation: up to two prefix letters, a dotted number, an optional single
# trailing letter that is not a size unit (GLM-4.5V → 4.5, member V).
_GENERATION_RE = re.compile(
    r"^(?P<prefix>[A-Za-z]{0,2})(?P<number>\d+(?:\.\d+)*)(?P<tail>[A-Za-z]?)$"
)
# Letters glued to a version: Qwen3.8, esm3, gpt2, Llama3.
_GLUED_RE = re.compile(
    r"^(?P<letters>[A-Za-z]+)(?P<number>\d+(?:\.\d+)*)(?P<tail>[A-Za-z]?)$"
)
_SIZE_RE = re.compile(r"^(?P<n>\d+(?:\.\d+)?)(?P<unit>[KkMmBbTt])$")
_ACTIVE_RE = re.compile(r"^[Aa](?P<n>\d+(?:\.\d+)?)(?P<unit>[MmBbTt])$")
_EXPERTS_RE = re.compile(
    r"^(?P<experts>\d+)[xX](?P<n>\d+(?:\.\d+)?)(?P<unit>[MmBbTt])$"
)
_UNIT = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
_SIZE_UNITS = frozenset("kmbt")

# Closed vocabularies. Keys are the normalized word; values match tokens.
_VARIANTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruct", re.compile(r"^(instruct|it|sft)$", re.IGNORECASE)),
    ("chat", re.compile(r"^chat$", re.IGNORECASE)),
    ("thinking", re.compile(r"^(thinking|reasoning|reasoner|think)$", re.IGNORECASE)),
    (
        "abliterated",
        re.compile(r"^(abliterat\w*|obliterat\w*|heretic|ablated)$", re.IGNORECASE),
    ),
    ("uncensored", re.compile(r"^uncensored$", re.IGNORECASE)),
    ("distill", re.compile(r"^distill\w*$", re.IGNORECASE)),
)
# ``Base`` capitalised, or ``base`` / ``pt`` right after a size token, is a
# pretrained-only release. Bare ``-base`` is a size tier (bert-base) and is
# left in the member.
_BASE_CAPITAL = re.compile(r"^Base$")
_BASE_AFTER_SIZE = re.compile(r"^(base|pt)$", re.IGNORECASE)
# Lowercase size tiers end a family (``bert-base-uncased`` is family ``bert``);
# a capitalised tier is part of a line's name (``Mistral-Small-3.2``).
_TIER_WORDS = frozenset(
    {"nano", "micro", "tiny", "mini", "small", "base", "medium", "large", "xl", "xxl"}
)
_FORMATS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gguf", re.compile(r"^gguf$", re.IGNORECASE)),
    ("fp8", re.compile(r"^fp8$", re.IGNORECASE)),
    ("nvfp4", re.compile(r"^nvfp4$", re.IGNORECASE)),
    ("mxfp4", re.compile(r"^mxfp4$", re.IGNORECASE)),
    ("fp4", re.compile(r"^fp4$", re.IGNORECASE)),
    ("awq", re.compile(r"^awq$", re.IGNORECASE)),
    ("gptq", re.compile(r"^gptq$", re.IGNORECASE)),
    ("int4", re.compile(r"^(int4|4bit|4-bit|w4a16)$", re.IGNORECASE)),
    ("int8", re.compile(r"^(int8|8bit|8-bit|w8a8|w8a16)$", re.IGNORECASE)),
    ("bnb", re.compile(r"^(bnb|bitsandbytes)$", re.IGNORECASE)),
    ("mlx", re.compile(r"^mlx$", re.IGNORECASE)),
    ("exl2", re.compile(r"^exl2$", re.IGNORECASE)),
    ("exl3", re.compile(r"^exl3$", re.IGNORECASE)),
    ("onnx", re.compile(r"^onnx$", re.IGNORECASE)),
    ("safetensors", re.compile(r"^safetensors$", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Lineage:
    """What a name says. ``None`` where the name says nothing."""

    family: str | None
    generation: str | None
    member: str | None
    variants: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    size_total: float | None = None
    size_active: float | None = None
    tokens: tuple[str, ...] = field(default=(), compare=False)

    @property
    def family_key(self) -> str | None:
        """Case-folded family, so ``qwen3.8-max`` and ``Qwen3.8-27B`` meet."""
        return self.family.casefold() if self.family else None

    @property
    def generation_key(self) -> tuple:
        """Sort key for generations within a family: numbers first."""
        return generation_sort_key(self.generation)

    def as_dict(self) -> dict:
        return {
            "family": self.family,
            "generation": self.generation,
            "member": self.member,
            "variants": list(self.variants),
            "formats": list(self.formats),
            "size": (
                {"total": self.size_total, "active": self.size_active}
                if self.size_total is not None or self.size_active is not None
                else None
            ),
            "read_from": READ_FROM,
        }


def _count(n: str, unit: str) -> float:
    return float(n) * _UNIT[unit.lower()]


def generation_sort_key(generation: str | None) -> tuple:
    """``K2.5`` → (2, 5); ``V3.2`` → (3, 2); ``None`` sorts first."""
    if not generation:
        return (-1,)
    m = _GENERATION_RE.match(generation)
    if not m:
        return (-1, generation)
    return tuple(int(part) for part in m.group("number").split("."))


def _variant_of(token: str, after_size: bool) -> str | None:
    if _BASE_CAPITAL.match(token) or (after_size and _BASE_AFTER_SIZE.match(token)):
        return "base"
    for name, pattern in _VARIANTS:
        if pattern.match(token):
            return name
    return None


def _format_of(token: str) -> str | None:
    for name, pattern in _FORMATS:
        if pattern.match(token):
            return name
    return None


def _is_generation(token: str) -> re.Match[str] | None:
    m = _GENERATION_RE.match(token)
    if not m:
        return None
    if m.group("tail") and m.group("tail").lower() in _SIZE_UNITS:
        return None  # 27B, 2.4T are sizes
    if m.group("prefix").lower() == "a" and not m.group("tail"):
        return None  # A95B-style actives never reach here, but guard the shape
    return m


def parse_name(name: str) -> Lineage:
    """Read family, generation, member, variants, formats, and size from a name.

    ``name`` is the repo name after ``owner/`` (or the last path segment of a
    home URL). The result is a fact about the *name*, recorded as such.
    """
    raw = (name or "").strip().strip("/")
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    tokens = tuple(t for t in re.split(r"[-_\s]+", raw) if t)
    if not tokens:
        return Lineage(None, None, None, tokens=tokens)

    family: list[str] = []
    generation: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if (
            token.isalpha()
            and _variant_of(token, False) is None
            and _format_of(token) is None
        ):
            if family and token in _TIER_WORDS:
                break
            family.append(token)
            i += 1
            continue
        glued = _GLUED_RE.match(token)
        if (
            glued
            and glued.group("tail").lower() not in _SIZE_UNITS
            and not _SIZE_RE.match(token)
        ):
            # Qwen3.8 → Qwen + 3.8. The whole letters part joins the family
            # unless it is a one- or two-letter generation prefix (K3, V3.2)
            # after a family already exists.
            letters = glued.group("letters")
            if len(letters) <= 2 and family:
                generation = token
            else:
                family.append(letters)
                generation = glued.group("number") + glued.group("tail")
            i += 1
            break
        if _is_generation(token) and not _SIZE_RE.match(token):
            generation = token
            i += 1
            break
        break
    rest = list(tokens[i:])

    # A generation with a trailing letter (4.5V) hands the letter to the member.
    member_tokens: list[str] = []
    if generation:
        m = _GENERATION_RE.match(generation)
        if m and m.group("tail") and m.group("tail").lower() not in _SIZE_UNITS:
            generation = m.group("prefix") + m.group("number")
            member_tokens.append(m.group("tail"))

    variants: list[str] = []
    formats: list[str] = []
    size_total: float | None = None
    size_active: float | None = None
    after_size = False
    for token in rest:
        experts = _EXPERTS_RE.match(token)
        size = _SIZE_RE.match(token)
        active = _ACTIVE_RE.match(token)
        if experts:
            per = _count(experts.group("n"), experts.group("unit"))
            if size_total is None:
                size_total = per * int(experts.group("experts"))
                size_active = per
            member_tokens.append(token)
            after_size = True
            continue
        if size:
            if size_total is None:
                size_total = _count(size.group("n"), size.group("unit"))
            member_tokens.append(token)
            after_size = True
            continue
        if active:
            if size_active is None:
                size_active = _count(active.group("n"), active.group("unit"))
            member_tokens.append(token)
            after_size = True
            continue
        variant = _variant_of(token, after_size)
        if variant:
            if variant not in variants:
                variants.append(variant)
            after_size = False
            continue
        fmt = _format_of(token)
        if fmt:
            if fmt not in formats:
                formats.append(fmt)
            after_size = False
            continue
        member_tokens.append(token)
        after_size = False

    return Lineage(
        family="-".join(family) if family else None,
        generation=generation or None,
        member="-".join(member_tokens) if member_tokens else None,
        variants=tuple(variants),
        formats=tuple(formats),
        size_total=size_total,
        size_active=size_active,
        tokens=tokens,
    )


def name_of_source(source: str) -> str:
    """The work's name from a source ref or home URL: the last path segment."""
    s = (source or "").strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if ":" in s and "/" not in s.split(":", 1)[0]:
        s = s.split(":", 1)[1]
    return s.rsplit("/", 1)[-1] if s else ""


def lineage_of_source(source: str) -> Lineage:
    return parse_name(name_of_source(source))


def display_generation(family: str | None, generation: str | None) -> str:
    """``Qwen 3.8``, ``Kimi K3``, or just the family."""
    if family and generation:
        return f"{family} {generation}"
    return family or generation or "—"


def group_by_family(rows: list[dict], *, source_key: str = "source") -> list[dict]:
    """Group rows into families → generations, for a tree view.

    Returns ``[{family, key, publishers, generations: [{generation, rows}]}]``
    sorted by member count (largest family first), generations oldest
    first. Rows with no family land in one trailing ``None`` group.
    """
    families: dict[str | None, dict] = {}
    for row in rows:
        lin = lineage_of_source(row.get(source_key) or "")
        key = lin.family_key
        fam = families.setdefault(
            key,
            {"family": lin.family, "key": key, "rows": [], "publishers": {}},
        )
        fam["rows"].append((lin, row))
        publisher = _publisher_of(row.get(source_key) or "")
        if publisher:
            fam["publishers"][publisher] = fam["publishers"].get(publisher, 0) + 1
    out = []
    for key, fam in families.items():
        by_gen: dict[str | None, list] = {}
        for lin, row in fam["rows"]:
            by_gen.setdefault(lin.generation, []).append((lin, row))
        generations = [
            {
                "generation": gen,
                "rows": [
                    r
                    for _, r in sorted(
                        members, key=lambda p: (p[0].size_total or 0, p[0].member or "")
                    )
                ],
            }
            for gen, members in sorted(
                by_gen.items(), key=lambda kv: generation_sort_key(kv[0])
            )
        ]
        # The family's home publisher: the one with most members on this list.
        home = (
            max(fam["publishers"], key=fam["publishers"].get)
            if fam["publishers"]
            else None
        )
        # Display casing: the most common spelling among members, counting a
        # provider ref's spelling (the publisher's own) over a home URL's,
        # which the web lowercases.
        spellings: dict[str, int] = {}
        for lin, row in fam["rows"]:
            if lin.family:
                weight = 2 if _publisher_of(row.get(source_key) or "") else 1
                spellings[lin.family] = spellings.get(lin.family, 0) + weight
        family_name = max(spellings, key=spellings.get) if spellings else None
        out.append(
            {
                "family": family_name,
                "key": key,
                "home_publisher": home,
                "count": len(fam["rows"]),
                "generations": generations,
            }
        )
    out.sort(key=lambda f: (f["key"] is None, -f["count"], f["key"] or ""))
    return out


def _publisher_of(source: str) -> str | None:
    s = (source or "").strip()
    if "://" in s:
        return None
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.startswith("datasets/"):
        s = s[len("datasets/") :]
    parts = s.split("/")
    return parts[0] if len(parts) >= 2 and parts[0] else None
