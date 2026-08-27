#!/usr/bin/env python3
"""Prepare a darsay release: bump, verify, commit, tag.

Releasing is pushing the tag. This script does everything before that, and
refuses to leave a half-finished release behind: every check runs before the
first file is written, and the tag is only cut once the full gate passes.

    python scripts/release.py 0.8.1
    python scripts/release.py 0.8.1 --dry-run     # report only, write nothing
    python scripts/release.py 0.8.1 --push        # also push the branch

Stdlib only, like the bundle verifier: a release must not depend on the
optional extras it is packaging.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "src" / "darsay" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
DOCS_INDEX = ROOT / "docs" / "README.md"

RELEASE_BRANCH = "main"
REMOTE = "origin"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# The one literal the build, the tag, and `darsay --version` all read.
VERSION_LINE = re.compile(r'(?m)^__version__ = "([^"]+)"$')
# The docs landing table's "Current" row for the tool.
DOCS_TOOL_ROW = re.compile(r"(?m)^(\| Tool \| \*\*)([^*]+)(\*\* \|)$")


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


def read_current_version() -> str:
    match = VERSION_LINE.search(INIT.read_text(encoding="utf-8"))
    if match is None:
        raise Abort(f"no __version__ literal in {INIT.relative_to(ROOT)}")
    return match.group(1)


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


def check_changelog(version: str) -> str:
    """The changelog is written by hand. Confirm it, never generate it."""
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = re.compile(rf"(?m)^## \[{re.escape(version)}\](?: - (\S+))?\s*$")
    match = heading.search(text)
    if match is None:
        raise Abort(
            f"CHANGELOG.md has no '## [{version}]' section.\n"
            f"        Write the release notes before cutting the release."
        )
    body = text[match.end() :].split("\n## ")[0].strip()
    if not body:
        raise Abort(f"CHANGELOG.md section for {version} is empty")
    return match.group(0)


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


def write_docs_version(version: str) -> bool:
    """Keep the docs landing table honest. Cosmetic, but it is a claim."""
    text = DOCS_INDEX.read_text(encoding="utf-8")
    updated, count = DOCS_TOOL_ROW.subn(rf"\g<1>{version}\g<3>", text, count=1)
    if count:
        DOCS_INDEX.write_text(updated, encoding="utf-8")
    return bool(count)


def write_changelog_date(version: str, today: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    pattern = re.compile(rf"(?m)^## \[{re.escape(version)}\].*$")
    CHANGELOG.write_text(
        pattern.sub(f"## [{version}] - {today}", text, count=1), encoding="utf-8"
    )


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


# --- main -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Prepare a release: bump, verify, commit, tag. Push to release.",
    )
    parser.add_argument("version", help="version to release, e.g. 0.8.1")
    parser.add_argument(
        "--dry-run", action="store_true", help="run every check, write nothing"
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="skip the sdist/wheel gate (faster; CI still runs it)",
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

    # Subprocess output is unbuffered; keep ours interleaved correctly when
    # this is piped to a log rather than a terminal.
    sys.stdout.reconfigure(line_buffering=True)

    version = args.version.removeprefix("v")
    if not SEMVER.match(version):
        raise Abort(f"{args.version!r} is not a X.Y.Z version")
    tag = f"v{version}"

    current = read_current_version()
    if parse(version) <= parse(current):
        raise Abort(f"{version} does not come after the current {current}")

    print(f"darsay {current} -> {version}\n")

    print("checking:")
    check_tooling(args.skip_build)
    check_repo_state(tag, allow_branch=args.allow_branch)
    heading = check_changelog(version)
    print(f"  - changelog: {heading.strip()}")
    print("  - repo clean, tag free\n")

    today = dt.date.today().isoformat()

    if args.dry_run:
        print("dry run: would update version, docs, changelog date; then:")
        print(f"  commit  release: {version}")
        print(f"  tag     {tag}")
        return 0

    print("writing:")
    write_version(version)
    print(f"  - {INIT.relative_to(ROOT)}")
    if write_docs_version(version):
        print(f"  - {DOCS_INDEX.relative_to(ROOT)}")
    write_changelog_date(version, today)
    print(f"  - {CHANGELOG.relative_to(ROOT)} (dated {today})\n")

    print("verifying:")
    try:
        run_gate(version, args.skip_build)
    except SystemExit:
        # Leave the tree exactly as it was; a failed gate must cost nothing.
        run("git", "checkout", "--", str(INIT), str(CHANGELOG), str(DOCS_INDEX))
        print("\n  reverted the version bump; nothing was committed")
        raise
    print()

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
