#!/usr/bin/env python3
"""Install the complete GH workflow skill into the user-global skill root."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys


SKILL_NAME = "gh-workflow-suite"
LEGACY_IMPORT_NAME = "source-command-skills-gh-workflow-suite-skill"
REQUIRED_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/porting-notes.md",
    "references/workflows.md",
    "references/full-review.md",
    "references/issue-pipeline.md",
    "references/drain-issues.md",
    "references/review-schema.json",
    "scripts/install_user_skill.py",
    "scripts/run_claude_review.py",
    "scripts/run_review.py",
)


def _validate_source(source: Path) -> None:
    missing = [relative for relative in REQUIRED_FILES if not (source / relative).is_file()]
    if missing:
        raise RuntimeError(f"Skill source is incomplete: {', '.join(missing)}")


def _backup_path(disabled_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = disabled_root / f"{LEGACY_IMPORT_NAME}.imported-{stamp}"
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = disabled_root / f"{LEGACY_IMPORT_NAME}.imported-{stamp}-{counter}"
        counter += 1
    return candidate


def _same_symlink(destination: Path, source: Path) -> bool:
    return destination.is_symlink() and destination.resolve() == source.resolve()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install gh-workflow-suite under ~/.agents/skills."
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Override the home directory (primarily for isolated testing)",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy the skill instead of symlinking it; copied installs need manual updates",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the operations without changing files"
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source = Path(__file__).resolve().parents[1]
    try:
        _validate_source(source)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    home = args.home.expanduser().resolve()
    skill_root = home / ".agents" / "skills"
    disabled_root = home / ".agents" / "skills-disabled"
    destination = skill_root / SKILL_NAME
    legacy = skill_root / LEGACY_IMPORT_NAME

    if destination.exists() or destination.is_symlink():
        if _same_symlink(destination, source) and not args.copy:
            destination_action = "already-installed"
        else:
            print(
                f"error: destination already exists and was not changed: {destination}",
                file=sys.stderr,
            )
            print("Inspect or move it outside ~/.agents/skills, then rerun.", file=sys.stderr)
            return 3
    else:
        destination_action = "copy" if args.copy else "symlink"

    backup = _backup_path(disabled_root) if legacy.exists() or legacy.is_symlink() else None
    print(f"source:      {source}")
    print(f"destination: {destination}")
    print(f"mode:        {destination_action}")
    if backup is not None:
        print(f"disable old: {legacy} -> {backup}")

    if args.dry_run:
        return 0

    moved_legacy = False
    try:
        skill_root.mkdir(parents=True, exist_ok=True)
        if backup is not None:
            disabled_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(backup))
            moved_legacy = True

        if destination_action == "symlink":
            destination.symlink_to(source, target_is_directory=True)
        elif destination_action == "copy":
            shutil.copytree(source, destination)
    except OSError as exc:
        if destination_action == "copy" and destination.exists() and not destination.is_symlink():
            try:
                shutil.rmtree(destination)
            except OSError:
                print(
                    f"warning: could not remove partial copied skill at {destination}",
                    file=sys.stderr,
                )
        if moved_legacy and backup is not None and not legacy.exists():
            try:
                shutil.move(str(backup), str(legacy))
            except OSError:
                print(f"warning: could not restore imported skill from {backup}", file=sys.stderr)
        print(f"error: install failed: {exc}", file=sys.stderr)
        return 4

    if destination_action == "already-installed":
        print("Already installed. Start a new Codex task if this task has stale skill metadata.")
    else:
        print("Installed. Start a new Codex task or restart Codex, then invoke $gh-workflow-suite.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
