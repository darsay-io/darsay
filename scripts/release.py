#!/usr/bin/env python3
"""Prepare a darsay release: bump, verify, commit, tag.

Releasing is pushing the tag. This script does everything before that, and
refuses to leave a half-finished release behind: every check runs before the
first file is written, and the tag is only cut once the full gate passes.

Two kinds of pre-release edit exist, and the script treats them
differently. Anything derivable from a source literal — the version, the
docs landing table's version rows, the changelog heading and date — it
writes, because a hand-typed copy can only drift. Anything authored —
release notes, docs that describe a flag — it confirms and refuses on,
because it cannot write prose: notes must already live under
``## [Unreleased]`` or ``## [X.Y.Z]``, the user docs must not describe a
flag the CLI does not ship, every ``archive`` flag must be mentioned
somewhere a user reads, and every relative link in those docs must name a
file that is here.

    python scripts/release.py 0.8.1
    python scripts/release.py 0.8.1 --dry-run     # report only, write nothing
    python scripts/release.py 0.8.1 --push        # also push the branch
    python scripts/release.py 0.8.1 --prepare-only  # caller owns commit + tag
    python scripts/release.py 0.8.1 --check       # verify prepared source
    python scripts/release.py 0.8.1 --prepare-only --metadata-only

Stdlib only, like the bundle verifier: a release must not depend on the
optional extras it is packaging.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Release validation imports the prepared source in child Python processes.
# Keep those read-only checks from leaving bytecode in an isolated source view
# or an operator's checkout.
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "src" / "darsay" / "__init__.py"
CATALOG = ROOT / "src" / "darsay" / "catalog.py"
EXPORT = ROOT / "src" / "darsay" / "export.py"
CHANGELOG = ROOT / "CHANGELOG.md"
DOCS_INDEX = ROOT / "docs" / "README.md"

RELEASE_BRANCH = "main"
REMOTE = "origin"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# The one literal the build, the tag, and `darsay --version` all read.
VERSION_LINE = re.compile(r'(?m)^__version__ = "([^"]+)"$')

# The docs landing table's "Current" rows: each is a claim about one source
# literal, so each is written from it, never typed.
DOCS_ROWS = (
    ("Tool", "__version__", INIT),
    ("Manifest schema", "SCHEMA_VERSION", INIT),
    ("Catalog schema", "CATALOG_SCHEMA_VERSION", CATALOG),
    ("MVB format", "MVB_FORMAT_VERSION", EXPORT),
)

# User-facing docs that describe the CLI. CLAUDE.md asks that they never
# document an unshipped flag as live; the archive flags — the operator
# surface — must each be findable in at least one of them.
CLI_DOCS = (
    "README.md",
    "docs/GETTING-STARTED.md",
    "docs/CODE.md",
    "docs/CONCEPTS.md",
    "docs/CATALOGS.md",
    "docs/COLLECTIONS.md",
    "docs/DOCTOR.md",
    "docs/INCREMENTAL.md",
    "docs/FAQ.md",
    "examples/README.md",
)

# The pages deliberately outside those two checks, each with its reason.
# Both checks read a page as "every darsay flag named here is live", so a
# page that names a flag in order to say it does not exist fails on its best
# sentences: SOURCES.md's "Do not add a --provider flag", DATASETS.md's "no
# --type flag", QUANTIZATION.md's labelled `hydrate --quantize` proposal.
# Those are not a threshold to tune, which is why this list is not a glob.
#
# It is total instead. check_docs_pages_classified refuses a page that is in
# neither list, so a new docs page is classified by whoever writes it rather
# than by the release that discovers the omission — DOCTOR.md, CATALOGS.md,
# and NORTH-STAR.md each went unclassified for weeks, and nothing said so.
UNCHECKED_DOCS = {
    "docs/DATASETS.md": "names --type to say no such flag is added",
    "docs/DESIGN.md": "weighs flags not taken (--xet)",
    "docs/DISTRIBUTION.md": "pipx and pip command lines, not the CLI's",
    "docs/HYDRATION.md": "runner-script flags, not the CLI's",
    "docs/MANIFEST.md": "schema reference; no command lines",
    "docs/MVB-FORMAT.md": "schema reference; no command lines",
    "docs/NORTH-STAR.md": "vocabulary and principles, not commands",
    "docs/QUANTIZATION.md": "documents the hydrate --quantize proposal",
    "docs/README.md": "the landing table and its version rows",
    "docs/SOURCES.md": "names --provider to say no such flag is added",
    "docs/TESTING.md": "ruff and pytest command lines, not the CLI's",
}

# Files whose relative links must resolve. ``docs/*.md`` is globbed, never
# listed: darsay.io publishes every one of them, so a page added today is
# checked today rather than at the release that discovers it.
LINKED_DOCS = ("README.md", "CONTRIBUTING.md", "examples/README.md")
MD_LINK = re.compile(r"\]\(([^)]+)\)")
# The page nav and the logo are HTML. darsay.io strips those blocks, so only
# this side ever checks them -- and GitHub is where they are read.
HTML_LINK = re.compile(r'(?:href|src)="([^"]+)"')
URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)

FLAG_TOKEN = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
# A flag belongs to the last program named before it on its line; only
# darsay's are checked, so `rsync's --link-dest` in prose stays rsync's.
PROGRAM = re.compile(r"\b(darsay|rsync|pytest|pipx|uvx?|git|wget|curl|tar|hf)\b")


class Abort(SystemExit):
    """A refusal with a human-readable reason."""

    def __init__(self, message: str) -> None:
        super().__init__(f"release: {message}")


def run(
    *args: str, capture: bool = False, cwd: Path = ROOT
) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=capture, check=False)


def git(*args: str) -> str:
    proc = run("git", *args, capture=True)
    if proc.returncode != 0:
        raise Abort(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def read_literal(path: Path, name: str) -> str:
    """The quoted value of ``NAME = "..."`` at the top level of a source file."""
    pattern = re.compile(rf'(?m)^{re.escape(name)} = "([^"]+)"$')
    match = pattern.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise Abort(f"no {name} literal in {path.relative_to(ROOT)}")
    return match.group(1)


def read_current_version() -> str:
    return read_literal(INIT, "__version__")


def docs_row(label: str) -> re.Pattern:
    return re.compile(rf"(?m)^(\| {re.escape(label)} \| \*\*)([^*]+)(\*\* \|)$")


def parse(version: str) -> tuple[int, int, int]:
    return tuple(int(p) for p in version.split("."))  # type: ignore[return-value]


# --- preflight ------------------------------------------------------------


def check_repo_state(tag: str, *, allow_branch: bool) -> None:
    """Everything about the working tree and the remote that must hold."""
    if git("rev-parse", "--is-inside-work-tree") != "true":
        raise Abort("not a git repository")

    if git("status", "--porcelain"):
        raise Abort("working tree is dirty; commit or stash first")

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != RELEASE_BRANCH and not allow_branch:
        raise Abort(
            f"on {branch!r}, expected {RELEASE_BRANCH!r} (--allow-branch to override)"
        )

    if git("tag", "--list", tag):
        raise Abort(
            f"tag {tag} already exists locally; delete it or pick a new version"
        )

    # A tag that already exists on the remote is the failure that is most
    # expensive to discover late -- the release workflow will have already run.
    proc = run("git", "ls-remote", "--tags", REMOTE, f"refs/tags/{tag}", capture=True)
    if proc.returncode != 0:
        print(f"  ! could not reach {REMOTE} to check tags; skipping that check")
        return
    if proc.stdout.strip():
        sha = proc.stdout.split()[0][:12]
        raise Abort(
            f"tag {tag} already exists on {REMOTE} (at {sha}).\n"
            f"        If that tag was pushed by mistake, remove it first:\n"
            f"          git push {REMOTE} :refs/tags/{tag}"
        )

    proc = run("git", "fetch", "--quiet", REMOTE, RELEASE_BRANCH, capture=True)
    if proc.returncode != 0:
        print(f"  ! could not fetch {REMOTE}/{RELEASE_BRANCH}; skipping sync check")
        return
    behind = git("rev-list", "--count", f"HEAD..{REMOTE}/{RELEASE_BRANCH}")
    if behind != "0":
        raise Abort(f"{behind} commit(s) behind {REMOTE}/{RELEASE_BRANCH}; pull first")


_HEADING = re.compile(r"(?m)^## .+")
_UNRELEASED = re.compile(r"(?m)^## \[Unreleased\][ \t]*$")


def _section_body(text: str, heading: re.Match) -> str:
    rest = text[heading.end() :]
    nxt = _HEADING.search(rest)
    return (rest[: nxt.start()] if nxt else rest).strip()


def _has_notes(body: str) -> bool:
    return any(line.lstrip().startswith("- ") for line in body.splitlines())


def prepare_changelog(text: str, version: str, today: str) -> tuple[str, str]:
    """Stamp a date, promote ``[Unreleased]``, leave a fresh Unreleased stub.

    Notes stay authored. This only rewrites headings. Returns
    ``(new_text, log_line)``.
    """
    unreleased = _UNRELEASED.search(text)
    target = re.compile(rf"(?m)^## \[{re.escape(version)}\](?: - \S+)?[ \t]*$")
    versioned = target.search(text)
    u_notes = _has_notes(_section_body(text, unreleased)) if unreleased else False

    if u_notes and versioned:
        raise Abort(
            f"CHANGELOG.md has notes under [Unreleased] and [{version}]; "
            "move them to one section"
        )

    dated = f"## [{version}] - {today}"
    stub = "## [Unreleased]\n\n"

    if u_notes:
        assert unreleased is not None
        updated = (
            text[: unreleased.start()] + stub + dated + "\n" + text[unreleased.end() :]
        )
        return updated, f"{dated} (from [Unreleased])"

    if versioned:
        if not _has_notes(_section_body(text, versioned)):
            raise Abort(f"CHANGELOG.md section for {version} has no notes")
        updated = target.sub(dated, text, count=1)
        if _UNRELEASED.search(updated) is None:
            stamped = re.search(rf"(?m)^## \[{re.escape(version)}\] - ", updated)
            assert stamped is not None
            updated = updated[: stamped.start()] + stub + updated[stamped.start() :]
        return updated, dated

    raise Abort(
        f"CHANGELOG.md has no '## [Unreleased]' or '## [{version}]' section.\n"
        f"        Write the release notes before cutting the release."
    )


def check_changelog(version: str, today: str) -> str:
    """Confirm notes exist; return the heading the write step will produce."""
    _, log_line = prepare_changelog(
        CHANGELOG.read_text(encoding="utf-8"), version, today
    )
    return log_line


def check_prepared_changelog(version: str) -> None:
    """Verify frozen release notes without re-deriving their release date."""
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = re.compile(
        rf"(?m)^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})[ \t]*$"
    ).search(text)
    if heading is None:
        raise Abort(f"CHANGELOG.md is not prepared for {version}")
    try:
        dt.date.fromisoformat(heading.group(1))
    except ValueError as exc:
        raise Abort(f"CHANGELOG.md has an invalid date for {version}") from exc
    if not _has_notes(_section_body(text, heading)):
        raise Abort(f"CHANGELOG.md section for {version} has no notes")
    if _UNRELEASED.search(text) is None:
        raise Abort("CHANGELOG.md has no fresh [Unreleased] section")


def check_docs_table() -> None:
    """Every derived row must have a source literal and a table row to land in."""
    text = DOCS_INDEX.read_text(encoding="utf-8")
    for label, name, path in DOCS_ROWS:
        read_literal(path, name)
        if docs_row(label).search(text) is None:
            raise Abort(
                f"{DOCS_INDEX.relative_to(ROOT)} has no '| {label} | **…** |' row"
            )


def check_docs_versions_current() -> None:
    """Every derived docs row must equal the source literal it describes."""
    text = DOCS_INDEX.read_text(encoding="utf-8")
    for label, name, path in DOCS_ROWS:
        expected = read_literal(path, name)
        match = docs_row(label).search(text)
        if match is None:
            raise Abort(
                f"{DOCS_INDEX.relative_to(ROOT)} has no '| {label} | **…** |' row"
            )
        if match.group(2) != expected:
            raise Abort(
                f"{DOCS_INDEX.relative_to(ROOT)} says {label} is {match.group(2)}, "
                f"expected {expected}"
            )


def published_docs() -> set[str]:
    """Every page darsay.io publishes, as repo-relative paths."""
    return {"README.md", "examples/README.md"} | {
        str(path.relative_to(ROOT)) for path in (ROOT / "docs").glob("*.md")
    }


def check_docs_pages_classified() -> None:
    """Every published page is flag-checked or exempt on purpose, never neither.

    Runs before ``check_docs_flags``, so a page listed but deleted is a
    refusal with its name rather than a traceback from the reader.
    """
    published = published_docs()
    checked, exempt = set(CLI_DOCS), set(UNCHECKED_DOCS)
    if both := sorted(checked & exempt):
        raise Abort(f"docs are in CLI_DOCS and UNCHECKED_DOCS: {' '.join(both)}")
    if gone := sorted((checked | exempt) - published):
        raise Abort(f"docs listed by the flag checks but not here: {' '.join(gone)}")
    if new := sorted(published - checked - exempt):
        raise Abort(
            "docs pages nobody classified:\n        "
            + "\n        ".join(new)
            + "\n        Add each to CLI_DOCS if a user reads about flags there,"
            "\n        or to UNCHECKED_DOCS with the reason it is exempt."
        )


def check_docs_flags() -> None:
    """The user docs and the CLI must agree on which flags exist.

    Confirm, never write: a flag the docs describe but the CLI lacks needs a
    decision, and an archive flag no doc mentions needs a sentence.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from darsay.cli import flags_by_command  # stdlib-only module

    by_command = flags_by_command()
    shipped = set().union(*by_command.values())
    mentioned: set[str] = set()
    unshipped: list[str] = []
    for rel in CLI_DOCS:
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, 1):
            for match in FLAG_TOKEN.finditer(line):
                owners = PROGRAM.findall(line[: match.start()])
                if owners and owners[-1] != "darsay":
                    continue
                flag = match.group(0)
                mentioned.add(flag)
                if flag not in shipped:
                    unshipped.append(f"{flag}  {rel}:{number}")
    if unshipped:
        raise Abort(
            "docs describe flags the CLI does not ship:\n        "
            + "\n        ".join(unshipped)
        )
    undocumented = sorted(by_command["archive"] - mentioned - {"--help"})
    if undocumented:
        raise Abort(
            f"archive flags no user doc mentions: {' '.join(undocumented)}\n"
            f"        (looked in {', '.join(CLI_DOCS)})"
        )


def link_docs() -> list[Path]:
    """Every file whose relative links the gate resolves."""
    return [ROOT / name for name in LINKED_DOCS] + sorted((ROOT / "docs").glob("*.md"))


def link_targets(line: str) -> list[str]:
    """Markdown and HTML link targets on one line, in the order written."""
    return [m.group(1) for m in MD_LINK.finditer(line)] + [
        m.group(1) for m in HTML_LINK.finditer(line)
    ]


def check_docs_links() -> None:
    """Every relative link in the user docs must name a file that is here.

    darsay.io publishes ``docs/*.md`` and ``examples/README.md``, and its
    transform refuses a link it cannot resolve -- which used to mean a docs
    change failed hours after the release, in the other repository. This is
    that same rule, one repo earlier and before the tag exists. Confirm,
    never write: a broken link needs a decision about where it meant to go.
    """
    broken: list[str] = []
    for path in link_docs():
        base = path.parent
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for target in link_targets(line):
                if URL_SCHEME.match(target) or target.startswith(("#", "/")):
                    continue
                relative = target.split("#", 1)[0]
                if not relative:
                    continue
                resolved = Path(os.path.normpath(base / relative))
                where = f"{path.relative_to(ROOT)}:{number}"
                if not resolved.is_relative_to(ROOT):
                    broken.append(f"{target}  {where} (leaves the repository)")
                elif not resolved.exists():
                    broken.append(f"{target}  {where}")
    if broken:
        raise Abort(
            "docs link to files that are not here:\n        "
            + "\n        ".join(broken)
        )


def check_tooling(skip_build: bool) -> None:
    missing = [m for m in ("ruff", "pytest") if not have_module(m)]
    if not skip_build:
        missing += [m for m in ("build", "twine") if not have_module(m)]
    if missing:
        raise Abort(
            f"missing tooling: {', '.join(missing)}\n"
            f'        pip install -e ".[dev,release]"'
        )


def have_module(name: str) -> bool:
    return run(sys.executable, "-c", f"import {name}", capture=True).returncode == 0


# --- writes ---------------------------------------------------------------


def write_version(version: str) -> None:
    text = INIT.read_text(encoding="utf-8")
    INIT.write_text(
        VERSION_LINE.sub(f'__version__ = "{version}"', text, count=1), encoding="utf-8"
    )


def write_docs_versions() -> list[str]:
    """Rewrite the docs landing table from the source literals it claims.

    Runs after ``write_version`` so the Tool row reads the new version.
    Returns the rows that changed, for the log.
    """
    text = DOCS_INDEX.read_text(encoding="utf-8")
    changed = []
    for label, name, path in DOCS_ROWS:
        value = read_literal(path, name)
        updated = docs_row(label).sub(rf"\g<1>{value}\g<3>", text, count=1)
        if updated != text:
            changed.append(f"{label} {value}")
            text = updated
    if changed:
        DOCS_INDEX.write_text(text, encoding="utf-8")
    return changed


def write_changelog(version: str, today: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    updated, log_line = prepare_changelog(text, version, today)
    if updated != text:
        CHANGELOG.write_text(updated, encoding="utf-8")
    return log_line


# --- gate -----------------------------------------------------------------


def gate_step(label: str, *cmd: str) -> None:
    """Run one gate quietly; surface the whole log only if it fails."""
    print(f"  - {label}", flush=True)
    proc = run(*cmd, capture=True)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise Abort(f"{label} failed")


def run_gate(version: str, skip_build: bool) -> None:
    """The same checks CI runs, before the commit rather than after."""
    gate_step("ruff check", sys.executable, "-m", "ruff", "check")
    gate_step("ruff format", sys.executable, "-m", "ruff", "format", "--check")
    gate_step(
        "pytest",
        sys.executable,
        "-m",
        "pytest",
        "-m",
        "not e2e",
        "--cov=darsay",
        "--cov-fail-under=73",
        "-q",
    )

    if skip_build:
        print("  - build (skipped)")
        return

    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    gate_step("build", sys.executable, "-m", "build")

    built = sorted(p.name for p in dist.iterdir())
    want = [f"darsay-{version}-py3-none-any.whl", f"darsay-{version}.tar.gz"]
    if missing := [n for n in want if n not in built]:
        raise Abort(f"build produced {built}, expected {missing}")

    gate_step(
        "twine check",
        sys.executable,
        "-m",
        "twine",
        "check",
        *[str(dist / n) for n in want],
    )


def check_prepared_release(
    version: str, _today: str, skip_build: bool, metadata_only: bool = False
) -> None:
    """Verify one already-prepared source tree without changing Git or files."""
    current = read_current_version()
    if current != version:
        raise Abort(f"source version is {current}, expected prepared {version}")

    check_prepared_changelog(version)

    check_docs_versions_current()
    check_docs_pages_classified()
    check_docs_flags()
    check_docs_links()
    if metadata_only:
        print("  - project gate (covered by exact source CI)")
    else:
        run_gate(version, skip_build)


# --- main -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Prepare a release: bump, verify, commit, tag. Push to release.",
    )
    parser.add_argument("version", help="version to release, e.g. 0.8.1")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--dry-run", action="store_true", help="run every check, write nothing"
    )
    modes.add_argument(
        "--prepare-only",
        action="store_true",
        help="write and verify release files; leave commit and tag to the caller",
    )
    modes.add_argument(
        "--check",
        action="store_true",
        help="verify an already-prepared release without changing files or Git",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="skip the sdist/wheel gate (faster; CI still runs it)",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help=(
            "verify only derived release metadata; requires --prepare-only or "
            "--check and leaves the project gate to exact source CI"
        ),
    )
    parser.add_argument(
        "--allow-branch",
        action="store_true",
        help=f"allow releasing from a branch other than {RELEASE_BRANCH}",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help=f"push {RELEASE_BRANCH} when done (never the tag -- that is the release)",
    )
    args = parser.parse_args(argv)
    if args.metadata_only and not (args.prepare_only or args.check):
        parser.error("--metadata-only requires --prepare-only or --check")

    # Subprocess output is unbuffered; keep ours interleaved correctly when
    # this is piped to a log rather than a terminal.
    sys.stdout.reconfigure(line_buffering=True)

    version = args.version.removeprefix("v")
    if not SEMVER.match(version):
        raise Abort(f"{args.version!r} is not a X.Y.Z version")
    tag = f"v{version}"

    current = read_current_version()
    today = dt.date.today().isoformat()
    if args.check:
        print(f"checking prepared darsay {version}:")
        if not args.metadata_only:
            check_tooling(args.skip_build)
        check_prepared_release(
            version, today, args.skip_build, metadata_only=args.metadata_only
        )
        print("prepared release verified")
        return 0

    if parse(version) < parse(current):
        raise Abort(f"{version} does not come after the current {current}")

    if version == current:
        if not args.prepare_only:
            raise Abort(f"{version} does not come after the current {current}")
        print(f"darsay {version} is already prepared\n")
        if not args.metadata_only:
            check_tooling(args.skip_build)
        check_prepared_release(
            version, today, args.skip_build, metadata_only=args.metadata_only
        )
        print(f"prepared {tag}; caller owns the commit and tag")
        return 0

    print(f"darsay {current} -> {version}\n")

    print("checking:")
    if not args.metadata_only:
        check_tooling(args.skip_build)
    if not args.prepare_only:
        check_repo_state(tag, allow_branch=args.allow_branch)
    heading = check_changelog(version, today)
    print(f"  - changelog: {heading}")
    check_docs_table()
    check_docs_pages_classified()
    check_docs_flags()
    check_docs_links()
    print(
        "  - docs: version table rows present, every page classified, "
        "flags match the CLI, links resolve"
    )
    print("  - repo clean, tag free\n")

    if args.dry_run:
        print("dry run: would update version, docs, changelog heading; then:")
        print(f"  commit  release: {version}")
        print(f"  tag     {tag}")
        return 0

    print("writing:")
    write_version(version)
    print(f"  - {INIT.relative_to(ROOT)}")
    if rows := write_docs_versions():
        print(f"  - {DOCS_INDEX.relative_to(ROOT)} ({', '.join(rows)})")
    log_line = write_changelog(version, today)
    print(f"  - {CHANGELOG.relative_to(ROOT)} ({log_line})\n")

    print("verifying:")
    if args.metadata_only:
        print("  - project gate (covered by exact source CI)")
    else:
        try:
            run_gate(version, args.skip_build)
        except SystemExit:
            # Leave the tree exactly as it was; a failed gate must cost nothing.
            run("git", "checkout", "--", str(INIT), str(CHANGELOG), str(DOCS_INDEX))
            print("\n  reverted the version bump; nothing was committed")
            raise
    print()

    if args.prepare_only:
        print(f"prepared {tag}; caller owns the commit and tag")
        return 0

    git("add", str(INIT), str(CHANGELOG), str(DOCS_INDEX))
    git("commit", "-m", f"release: {version}")
    git("tag", "-a", tag, "-m", f"darsay {version}")
    print(f"committed and tagged {tag}\n")

    if args.push:
        print(f"pushing {RELEASE_BRANCH}:")
        if run("git", "push", REMOTE, RELEASE_BRANCH).returncode != 0:
            raise Abort(f"could not push {RELEASE_BRANCH}")
        print()
        print("Release it:")
        print(f"    git push {REMOTE} {tag}")
    else:
        print("Release it:")
        print(f"    git push {REMOTE} {RELEASE_BRANCH}")
        print(f"    git push {REMOTE} {tag}")
    print(f"\nUndo before pushing: git tag -d {tag} && git reset --hard HEAD~1")
    print("Once PyPI has it, this machine too: pipx upgrade darsay")
    return 0


if __name__ == "__main__":
    sys.exit(main())
