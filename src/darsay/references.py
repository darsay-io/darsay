"""What a source tree names that lives somewhere else.

A serving recipe names the checkpoint it serves and the image it runs in;
training code names its datasets; a README credits the repository an idea
came from. None of that is a fact darsay can prove about the tree beyond
"these strings are in these files" — so that is what is recorded, with
provenance, in three tiers, strongest first:

``declared``
    A standard file whose job is to say it: a dotenv template's
    ``KEY=value``, a compose file's ``image:``, a Dockerfile's ``FROM``, a
    Spaces card's ``models:`` list.
``evidence``
    A literal in code — a quoted string, or the default of a shell
    ``${VAR:-…}`` expansion read as text. Never evaluated.
``mentioned``
    Prose: READMEs, issue templates, a URL in a comment.

Reading stops there. What a program will actually load at run time is
undecidable in general, and darsay does not guess at it. One rule turns
evidence into a lineage edge: exactly one *model* reference at the
evidence tier or better that resolves upstream is the tree's primary
model, recorded as ``relation: references``. Two candidates are recorded
as candidates and the curator chooses.

The scan is offline and deterministic (``scan_references``); resolution
is the one network step (``resolve_references``), takes a lookup the
caller builds from the provider registry, and is capped with the cap
recorded. Nothing here reads a file the caller did not hand it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

TIERS = ("declared", "evidence", "mentioned")
_TIER_RANK = {tier: i for i, tier in enumerate(TIERS)}
KINDS = ("model", "dataset", "image", "code")
_KIND_RANK = {kind: i for i, kind in enumerate(KINDS)}
FOUND_IN_CAP = 10
RESOLVE_LIMIT = 20

ENV_TEMPLATE_NAMES = (".env.sample", ".env.example", ".env.template", ".env")
COMPOSE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)
PROSE_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})
PROSE_NAMES = frozenset(
    {"LICENSE", "LICENCE", "COPYING", "NOTICE", "README", "CHANGELOG", "AUTHORS"}
)
CODE_SUFFIXES = frozenset(
    {
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".py",
        ".pyi",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".jsonc",
        ".cfg",
        ".ini",
        ".conf",
        ".env",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".rs",
        ".go",
        ".rb",
        ".jl",
        ".lua",
        ".pl",
        ".r",
        ".sql",
        ".mk",
        ".nix",
        ".tf",
        ".hcl",
        ".dockerfile",
    }
)
CODE_NAMES = frozenset(
    {"Makefile", "GNUmakefile", "makefile", "justfile", "Justfile", ".gitattributes"}
)

# A GitHub owner segment that is a site section, not an account.
_GITHUB_NOT_OWNERS = frozenset(
    {
        "sponsors",
        "features",
        "orgs",
        "topics",
        "settings",
        "marketplace",
        "apps",
        "login",
        "about",
        "pricing",
        "site",
        "security",
        "contact",
        "explore",
        "new",
        "notifications",
        "search",
        "blog",
        "enterprise",
    }
)
# The first segment of a path, not a publisher.
_PATH_ROOTS = frozenset(
    {
        "usr",
        "etc",
        "var",
        "opt",
        "tmp",
        "dev",
        "proc",
        "sys",
        "bin",
        "sbin",
        "lib",
        "lib64",
        "home",
        "root",
        "mnt",
        "media",
        "srv",
        "run",
        "src",
        "files",
        "docs",
        "doc",
        "tests",
        "test",
        "scripts",
        "logs",
        "log",
        "build",
        "dist",
        "node_modules",
        "vendor",
        "models",
        "model",
        "data",
        "hub",
        "snapshots",
        "cache",
        "app",
        "workspace",
        "code",
        "orig",
        "assets",
        "static",
        "public",
        "config",
        "configs",
        "examples",
        "example",
        "samples",
        "output",
        "outputs",
        "input",
        "inputs",
        "results",
        "checkpoints",
        "weights",
        "images",
        "img",
        "www",
        "v1",
        "api",
    }
)
_PATH_SUFFIXES = (
    ".py",
    ".sh",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".txt",
    ".cfg",
    ".conf",
    ".ini",
    ".log",
    ".orig",
    ".safetensors",
    ".gguf",
    ".bin",
    ".pt",
    ".pth",
    ".onnx",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".rs",
    ".go",
    ".c",
    ".h",
    ".cpp",
    ".so",
    ".tar",
    ".gz",
    ".zip",
    ".csv",
    ".parquet",
    ".jsonl",
    ".lock",
    ".service",
)

_SEG = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,94}[A-Za-z0-9])?"
_HUB_ID = re.compile(rf"(?<![\w./@:\-$])({_SEG})/({_SEG})(?![\w./@:\-])")
_IMAGE = re.compile(
    r"(?<![\w./@:\-$])"
    r"((?:[a-z0-9.-]+(?::\d+)?/)?[a-z0-9._-]+(?:/[a-z0-9._-]+)+)"
    r":([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})(@sha256:[0-9a-f]{64})?"
    r"(?![\w./@:\-])"
)
_IMAGE_ANY = re.compile(
    r"^((?:[a-z0-9.-]+(?::\d+)?/)?[a-z0-9._-]+(?:/[a-z0-9._-]+)*)"
    r"(?::([A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(@sha256:[0-9a-f]{64})?$"
)
_HUB_URL = re.compile(
    r"https?://(?:www\.)?(?:huggingface\.co|hf\.co)/(datasets/)?([\w.-]+)/([\w.-]+)"
)
_GITHUB_URL = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)")
_SHELL_DEFAULT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}")
_DOTENV = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_COMPOSE_IMAGE = re.compile(r"^\s*image:\s*(\S+)")
_DOCKER_FROM = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE
)
_YAML_LIST_ITEM = re.compile(r"^\s*-\s*['\"]?([^'\"\s#]+)")
_QUOTES = "\"'`"
# Comment leaders by file suffix: a comment is prose, whatever file it is in.
_COMMENT_LEADERS = {
    ".js": ("//",),
    ".mjs": ("//",),
    ".cjs": ("//",),
    ".ts": ("//",),
    ".tsx": ("//",),
    ".jsx": ("//",),
    ".rs": ("//",),
    ".go": ("//",),
    ".sql": ("--",),
    ".lua": ("--",),
}
_DOCSTRING_QUOTES = ('"""', "'''")


@dataclass(frozen=True)
class ScanLimits:
    """How much of a tree a scan may read; the record says when it stopped."""

    file_bytes: int
    total_bytes: int
    file_count: int


# The payload is on this disk: read generously.
LOCAL_SCAN = ScanLimits(
    file_bytes=1024 * 1024, total_bytes=16 * 1024**2, file_count=4000
)
# Bounded range reads over the network, before a byte of payload lands.
REMOTE_SCAN = ScanLimits(file_bytes=256 * 1024, total_bytes=2 * 1024**2, file_count=48)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- file classes


def file_class(path: str) -> str | None:
    """``env_template`` / ``compose`` / ``dockerfile`` / ``code`` / ``prose``,
    or ``None`` for a file the scan does not read (binaries, unknown kinds)."""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    suffix = PurePosixPath(name).suffix.lower()
    stem_upper = name.split(".")[0].upper()
    if parts and parts[0] == ".github":
        return "prose" if suffix in PROSE_SUFFIXES or suffix in CODE_SUFFIXES else None
    if name in ENV_TEMPLATE_NAMES:
        return "env_template"
    if name in COMPOSE_NAMES or re.fullmatch(r"docker-compose\..+\.ya?ml", name):
        return "compose"
    if (
        name in ("Dockerfile", "Containerfile")
        or name.startswith("Dockerfile.")
        or suffix == ".dockerfile"
    ):
        return "dockerfile"
    if suffix in PROSE_SUFFIXES or (not suffix and stem_upper in PROSE_NAMES):
        return "prose"
    if suffix in CODE_SUFFIXES or name in CODE_NAMES:
        return "code"
    return None


def _priority(path: str) -> tuple:
    kind = file_class(path)
    depth = path.count("/")
    is_root_readme = path.lower() == "readme.md"
    group = {
        "env_template": 0,
        "compose": 0,
        "dockerfile": 0,
        "code": 2 if depth else 1,
        "prose": 3,
    }[kind]
    if is_root_readme:
        group = 0
    return (group, depth, path)


def select_scan_files(
    entries: list[tuple[str, int | None]], limits: ScanLimits
) -> tuple[list[str], dict]:
    """Which files to read, in priority order, within ``limits``.

    Declarations and the root README first, then root-level code, nested
    code, prose. Returns the paths and what was skipped and why, so the
    record can say the scan was partial.
    """
    readable = [(p, s) for p, s in entries if file_class(p) is not None]
    skipped = {
        "not_text": len(entries) - len(readable),
        "too_large": 0,
        "over_budget": 0,
    }
    readable.sort(key=lambda item: _priority(item[0]))
    chosen: list[str] = []
    budget = limits.total_bytes
    for path, size in readable:
        size = int(size or 0)
        if size > limits.file_bytes:
            skipped["too_large"] += 1
            continue
        if len(chosen) >= limits.file_count or size > budget:
            skipped["over_budget"] += 1
            continue
        chosen.append(path)
        budget -= size
    return chosen, skipped


def is_text(data: bytes) -> bool:
    return b"\x00" not in data[:8192]


# ------------------------------------------------------------- classification


_TRAILING_PUNCTUATION = ".,;:!?)]}>'\""


def _segment(text: str) -> str:
    """A URL path segment without the sentence punctuation that followed it."""
    return text.rstrip(_TRAILING_PUNCTUATION)


def _hub_ref(owner: str, name: str, *, dataset: bool = False) -> str:
    owner, name = _segment(owner), _segment(name)
    return f"huggingface:{'datasets/' if dataset else ''}{owner}/{name}"


def _plausible_hub_id(owner: str, name: str) -> bool:
    if owner.lower() in _PATH_ROOTS or "--" in owner or "--" in name:
        return False
    low_name, low_owner = name.lower(), owner.lower()
    if low_name.endswith(_PATH_SUFFIXES) or low_owner.endswith(_PATH_SUFFIXES):
        return False
    if owner.isdigit() or name.isdigit():
        return False
    return True


def _strong_shape(name: str) -> bool:
    """In code, a name with a digit, hyphen, underscore, or dot reads as a
    repo id; ``and/or`` and ``input/output`` do not."""
    return any(ch.isdigit() or ch in "-_." for ch in name)


def _prose_shape(owner: str, name: str) -> bool:
    """In prose the bar is higher: the owner reads as an account (a
    lowercase letter somewhere) and the name carries a hyphen, underscore,
    or dot — ``NVFP4/FP8`` and ``bf16/fp32`` are not repositories."""
    return any(ch.islower() for ch in owner) and any(ch in "-_." for ch in name)


def _wrapped(line: str, start: int, end: int) -> bool:
    before = line[start - 1] if start > 0 else ""
    after = line[end] if end < len(line) else ""
    return before in _QUOTES and after == before


def classify_value(value: str) -> tuple[str, str] | None:
    """``(kind, ref)`` for one whole value: an image reference, a Hub id,
    or a Hub / GitHub URL. ``None`` when it is none of those."""
    value = value.strip().strip(_QUOTES).strip()
    if not value:
        return None
    m = _HUB_URL.match(value)
    if m:
        return ("dataset" if m.group(1) else "model"), _hub_ref(
            m.group(2), m.group(3), dataset=bool(m.group(1))
        )
    m = _GITHUB_URL.match(value)
    if m and m.group(1).lower() not in _GITHUB_NOT_OWNERS:
        return (
            "code",
            f"github:{m.group(1)}/{_segment(m.group(2)).removesuffix('.git')}",
        )
    m = _IMAGE_ANY.match(value)
    if m and (m.group(2) or m.group(3)):
        return "image", f"oci:{value}"
    m = _HUB_ID.fullmatch(value)
    if m and _plausible_hub_id(m.group(1), m.group(2)):
        return "model", _hub_ref(m.group(1), m.group(2))
    return None


def _scan_line(line: str, *, strict: bool) -> list[tuple[str, str, str]]:
    """``(kind, ref, how)`` for every reference in one line of text.

    ``strict`` (prose, unquoted code) requires the repo-id shape; a quoted
    token in code is taken on the quotes alone.
    """
    found: list[tuple[str, str, str]] = []
    masked = line
    for m in _HUB_URL.finditer(line):
        dataset = bool(m.group(1))
        found.append(
            (
                "dataset" if dataset else "model",
                _hub_ref(m.group(2), m.group(3), dataset=dataset),
                "url",
            )
        )
        masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end() :]
    for m in _GITHUB_URL.finditer(line):
        if m.group(1).lower() in _GITHUB_NOT_OWNERS:
            continue
        found.append(
            (
                "code",
                f"github:{m.group(1)}/{_segment(m.group(2)).removesuffix('.git')}",
                "url",
            )
        )
        masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end() :]
    for m in _IMAGE.finditer(masked):
        found.append(("image", f"oci:{_segment(m.group(0))}", "literal"))
        masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end() :]
    for m in _HUB_ID.finditer(masked):
        owner, name = m.group(1), m.group(2)
        if not _plausible_hub_id(owner, name):
            continue
        if not _wrapped(masked, m.start(), m.end()):
            if strict and not _prose_shape(owner, name):
                continue
            if not strict and not _strong_shape(name):
                continue
        found.append(("model", _hub_ref(owner, name), "literal"))
    return found


def _is_comment(line: str, suffix: str) -> bool:
    stripped = line.lstrip()
    leaders = _COMMENT_LEADERS.get(suffix, ("#",))
    return stripped.startswith(leaders)


def _shell_defaults(line: str) -> list[tuple[str, str]]:
    """``(variable, default)`` pairs from ``${VAR:-default}`` expansions,
    innermost included, read as text."""
    out: list[tuple[str, str]] = []
    for m in _SHELL_DEFAULT.finditer(line):
        variable, default = m.group(1), m.group(2)
        if "${" in default:
            out.extend(_shell_defaults(default + "}"))
            continue
        out.append((variable, default))
    return out


def _dotenv(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    m = _DOTENV.match(stripped)
    if not m:
        return None
    value = m.group(2).strip()
    if value[:1] in _QUOTES:
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end > 0 else value[1:]
    else:
        words = value.split("#", 1)[0].split()
        value = words[0] if words else ""
    return m.group(1), value


def _spaces_card(text: str) -> list[tuple[int, str, str]]:
    """``(line_no, kind, ref)`` from a README's YAML front matter ``models:``
    and ``datasets:`` lists — the Hugging Face Spaces convention."""
    if not text.startswith("---"):
        return []
    lines = text.splitlines()
    out: list[tuple[int, str, str]] = []
    current: str | None = None
    for no, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            break
        head = line.split(":", 1)[0].strip().lower()
        if line and not line[0].isspace() and ":" in line:
            current = head if head in ("models", "datasets") else None
            continue
        if current is None:
            continue
        m = _YAML_LIST_ITEM.match(line)
        if not m:
            continue
        item = m.group(1)
        if "/" not in item:
            continue
        owner, _, name = item.partition("/")
        if current == "models":
            out.append((no, "model", _hub_ref(owner, name)))
        else:
            out.append((no, "dataset", _hub_ref(owner, name, dataset=True)))
    return out


# -------------------------------------------------------------------- the scan


class _Collector:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.self_ref: str | None = None

    def add(self, kind: str, ref: str, tier: str, declared_by: str, where: str) -> None:
        if self.self_ref and ref.lower() == self.self_ref:
            return
        item = self.items.get(ref)
        if item is None:
            item = {
                "ref": ref,
                "kind": kind,
                "tier": tier,
                "declared_by": declared_by,
                "found_in": [],
                "occurrences": 0,
                "resolved": None,
            }
            self.items[ref] = item
        elif _TIER_RANK[tier] < _TIER_RANK[item["tier"]]:
            item["tier"], item["declared_by"] = tier, declared_by
            if where in item["found_in"]:
                item["found_in"].remove(where)
            item["found_in"].insert(0, where)
            del item["found_in"][FOUND_IN_CAP:]
            item["occurrences"] += 1
            return
        item["occurrences"] += 1
        if where not in item["found_in"] and len(item["found_in"]) < FOUND_IN_CAP:
            item["found_in"].append(where)

    def records(self) -> list[dict]:
        repositories = {
            ref.split(":", 1)[1].lower()
            for ref in self.items
            if ref.startswith("github:")
        }
        items = [
            item
            for ref, item in self.items.items()
            if not (
                ref.startswith("huggingface:")
                and item["tier"] == "mentioned"
                and ref.split(":", 1)[1].lower() in repositories
            )
        ]
        for item in items:
            if item["kind"] in ("model", "dataset", "code"):
                item["revision"] = None
            else:
                item["digest"] = None
        items.sort(key=_order)
        return items


def _order(item: dict) -> tuple:
    """Tier, then what upstream said (resolves, unknown, does not), then
    kind, then name — the order every surface shows references in."""
    resolved = {True: 0, None: 1, False: 2}[item.get("resolved")]
    return (_TIER_RANK[item["tier"]], resolved, _KIND_RANK[item["kind"]], item["ref"])


def _scan_file(path: str, text: str, out: _Collector) -> None:
    kind = file_class(path)
    suffix = PurePosixPath(path).suffix.lower()
    lines = text.splitlines()
    root_readme = path.lower() == "readme.md"
    if root_readme:
        for no, item_kind, ref in _spaces_card(text):
            out.add(item_kind, ref, "declared", "spaces_card", f"{path}:{no}")
    in_docstring = False
    for no, line in enumerate(lines, start=1):
        where = f"{path}:{no}"
        if kind in ("code", "compose", "dockerfile"):
            prose_line = in_docstring or _is_comment(line, suffix)
            if suffix == ".py":
                for quote in _DOCSTRING_QUOTES:
                    if line.count(quote) % 2:
                        in_docstring = not in_docstring
            if prose_line:
                for item_kind, ref, how in _scan_line(line, strict=True):
                    out.add(item_kind, ref, "mentioned", how, where)
                continue
        if kind == "env_template":
            parsed = _dotenv(line)
            if parsed:
                found = classify_value(parsed[1])
                if found:
                    out.add(found[0], found[1], "declared", "env_template", where)
            continue
        if kind == "compose":
            m = _COMPOSE_IMAGE.match(line)
            if m:
                found = classify_value(m.group(1))
                if found:
                    out.add(found[0], found[1], "declared", "compose", where)
                continue
        if kind == "dockerfile":
            m = _DOCKER_FROM.match(line)
            if m:
                image = m.group(1)
                if image.lower() != "scratch" and not _stage_name(image, lines):
                    found = classify_value(image)
                    if found:
                        out.add(found[0], found[1], "declared", "dockerfile", where)
                continue
        if kind in ("code", "compose", "dockerfile"):
            for _variable, default in _shell_defaults(line):
                found = classify_value(default)
                if found:
                    out.add(found[0], found[1], "evidence", "shell_default", where)
            for item_kind, ref, how in _scan_line(line, strict=False):
                out.add(item_kind, ref, "evidence", how, where)
            continue
        for item_kind, ref, how in _scan_line(line, strict=True):
            out.add(item_kind, ref, "mentioned", how, where)


def _stage_name(image: str, lines: list[str]) -> bool:
    """``FROM builder`` names an earlier ``AS builder`` stage, not an image."""
    for line in lines:
        m = _DOCKER_FROM.match(line)
        if m and m.group(2) and m.group(2).lower() == image.lower():
            return True
    return False


def scan_references(
    files: list[tuple[str, bytes]],
    *,
    skipped: dict | None = None,
    truncated: bool = False,
    self_ref: str | None = None,
) -> dict:
    """The ``references`` record from the files the caller read.

    ``files`` are ``(payload-relative path, bytes)``; binary files are
    counted, not read. ``skipped`` and ``truncated`` come from
    ``select_scan_files`` so the record says how far the scan went.
    ``self_ref`` is the tree's own address: a tree linking to itself is
    not a reference.
    """
    out = _Collector()
    out.self_ref = (self_ref or "").lower() or None
    scanned = 0
    bytes_scanned = 0
    binary = 0
    for path, data in files:
        if not is_text(data):
            binary += 1
            continue
        scanned += 1
        bytes_scanned += len(data)
        _scan_file(path, data.decode("utf-8", "replace"), out)
    counts = dict(skipped or {})
    counts["binary"] = counts.get("binary", 0) + binary
    items = out.records()
    return {
        "read_from": "payload",
        "scan": {
            "files_scanned": scanned,
            "bytes_scanned": bytes_scanned,
            "skipped": counts,
            "partial": bool(
                truncated or any(counts.get(k) for k in ("too_large", "over_budget"))
            ),
        },
        "items": items or None,
        "resolved_at": None,
        "query_limit": None,
        "primary_model": _primary_model(items),
    }


# ------------------------------------------------------------------ resolution


def resolve_references(references: dict, exists, *, limit: int = RESOLVE_LIMIT) -> dict:
    """Ask upstream whether each declared or evidence reference exists.

    ``exists(ref) -> bool | None`` is the caller's lookup (built from the
    provider registry). At most ``limit`` lookups, strongest tier first;
    the cap is recorded. Mentions and images are left ``null``: a mention
    is not a claim, and an image has no provider yet.
    """
    items = references.get("items") or []
    lookups = 0
    for item in items:
        if item["kind"] == "image" or item["tier"] == "mentioned":
            continue
        if lookups >= limit:
            break
        lookups += 1
        try:
            item["resolved"] = exists(item["ref"])
        except Exception:
            item["resolved"] = None
    # A name the Hub does not know, which the tree also links as a GitHub
    # repository, is that repository.
    repositories = {
        i["ref"].split(":", 1)[1].lower() for i in items if i["kind"] == "code"
    }
    items = [
        i
        for i in items
        if not (
            i["kind"] == "model"
            and i.get("resolved") is False
            and i["ref"].split(":", 1)[1].lower() in repositories
        )
    ]
    items.sort(key=_order)
    references["items"] = items or None
    references["resolved_at"] = _utc_now()
    references["query_limit"] = limit
    references["primary_model"] = _primary_model(items)
    return references


def _primary_model(items: list[dict]) -> dict:
    """The one model the code names that resolves upstream, or why not."""
    candidates = [
        i
        for i in items
        if i["kind"] == "model" and i["tier"] in ("declared", "evidence")
    ]
    rule = "the one model named in code that resolves upstream"
    if not candidates:
        return {
            "ref": None,
            "rule": rule,
            "candidates": [],
            "reason": "no model named in code",
        }
    resolving = [i for i in candidates if i.get("resolved") is True]
    refs = [i["ref"] for i in candidates]
    if len(resolving) == 1:
        return {
            "ref": resolving[0]["ref"],
            "rule": rule,
            "candidates": refs,
            "reason": None,
        }
    if not resolving and all(i.get("resolved") is None for i in candidates):
        return {
            "ref": None,
            "rule": rule,
            "candidates": refs,
            "reason": "not resolved upstream (offline, or the lookup failed)",
        }
    if not resolving:
        return {
            "ref": None,
            "rule": rule,
            "candidates": refs,
            "reason": "none resolved upstream",
        }
    return {
        "ref": None,
        "rule": rule,
        "candidates": refs,
        "reason": f"{len(resolving)} resolving candidates; the curator chooses",
    }


def primary_edge(references: dict | None) -> dict | None:
    """The ``lineage.parents`` edge for the primary model, or ``None``."""
    primary = (references or {}).get("primary_model") or {}
    ref = primary.get("ref")
    if not ref:
        return None
    item = next((i for i in references.get("items") or [] if i["ref"] == ref), None)
    return {
        "source": ref,
        "relation": "references",
        "declared_by": (item or {}).get("declared_by"),
    }
