#!/usr/bin/env python3
"""Run a structured review with Claude first and a Codex fallback."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterator

import run_claude_review as claude_adapter


CLASSIFIER_VERSION = 1
FALLBACK_FAILED_EXIT = 6
SNAPSHOT_MISMATCH_EXIT = 7
MAX_CODEX_GENERATIONS_PER_GATE = 2
VALID_REVIEW_EXITS = set(claude_adapter.VERDICT_EXIT_CODES.values())
QUOTA_MACHINE_CODES = {"usage_cap_reached", "credit_balance_low"}
PROVIDER_STATE_FIELDS = {
    "schema_version",
    "state_id",
    "active_provider",
    "fallback_trigger",
    "quota_classifier_version",
    "quota_classification",
    "claude_diagnostic_sha256",
    "codex_attempted_gates",
}
GATE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
ANSI_ESCAPE_RE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
QUOTA_MESSAGE_PATTERNS = (
    (
        "OUT_OF_USAGE_CREDITS",
        re.compile(
            r"^You're out of usage credits(?:\.(?: (?:Run /usage-credits|/model)[^\r\n]{0,200})?)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "ORG_OUT_OF_USAGE_CREDITS",
        re.compile(
            r"^Your organization is out of usage credits(?:\. Contact your admin to add more\.)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "ORG_USAGE_CREDIT_CAP_REACHED",
        re.compile(
            r"^Your organization's usage credit cap is reached for this period(?:\. Contact your admin to raise it\.)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "MONTHLY_SPEND_LIMIT_REACHED",
        re.compile(
            r"^You've hit your monthly spend limit(?:\.(?: (?:Run /usage-credits|/model)[^\r\n]{0,200})?)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "WEEKLY_LIMIT_REACHED",
        re.compile(
            r"^You've hit your weekly limit(?:\s*[·•]\s*resets [^\r\n]{1,100})?$",
            re.IGNORECASE,
        ),
    ),
    (
        "USAGE_LIMIT_REACHED",
        re.compile(
            r"^You've hit your (?:[A-Za-z0-9][A-Za-z0-9 -]{0,31} )?usage limit(?:\.(?: (?:Your )?limit resets [^\r\n]{1,100})?)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "CREDIT_BALANCE_LOW",
        re.compile(
            r"^Credit balance (?:is )?too low(?:\. Add funds: https://platform\.claude\.com/settings/billing)?$",
            re.IGNORECASE,
        ),
    ),
    (
        "GROUP_ZERO_USAGE_LIMIT",
        re.compile(
            r"^Your group's usage limit is set to \$0(?:\. Run /usage-credits to ask your admin for a higher limit)?$",
            re.IGNORECASE,
        ),
    ),
)
VALID_QUOTA_CLASSIFICATIONS = {
    *(f"MACHINE_{code.upper()}" for code in QUOTA_MACHINE_CODES),
    *(f"MESSAGE_{identifier}" for identifier, _pattern in QUOTA_MESSAGE_PATTERNS),
}
VALID_FALLBACK_TRIGGERS = {
    "CLAUDE_QUOTA_EXHAUSTED",
    "CLAUDE_REVIEW_FAILED",
    "CLAUDE_REVIEW_INCONCLUSIVE",
    "CLAUDE_REVIEW_INVALID",
}
WEB_FEATURES = (
    "web_search_request",
    "web_search_cached",
    "standalone_web_search",
    "search_tool",
)
CANONICAL_REVIEW_SCHEMA = (
    Path(__file__).resolve().parent.parent / "references" / "review-schema.json"
)


class WrapperError(RuntimeError):
    """Raised when provider selection or wrapper output cannot be trusted."""


class SnapshotError(WrapperError):
    """Raised when the reviewed repository or frozen inputs are not immutable."""


def _review_output_contract() -> str:
    security_categories = ", ".join(sorted(claude_adapter.SECURITY_CATEGORIES))
    return f"""BEGIN GATEWAY OUTPUT CONTRACT
The JSON schema describes field shapes. These semantic invariants are also mandatory:
- Copy reviewed_head_sha, reviewed_base_sha, and reviewed_merge_base_sha exactly.
- For APPROVED, CHANGES_REQUESTED, or BLOCKED, acceptance_criteria_sources must be non-empty.
- If no explicit criterion exists, set a non-empty no_explicit_criteria_reason.
- Every explicit PARTIAL or MISSING criterion must be P1/P2 and link to a matching AC finding.
- For every conclusive verdict, security_categories_checked must contain each of these exactly once:
  {security_categories}
- APPROVED permits no P1/P2 blocker.
- CHANGES_REQUESTED requires at least one P1/P2 blocker and no broad/risky AC or security repair.
- BLOCKED requires a broad/risky P1/P2 AC or security repair with remediation.
- INCONCLUSIVE requires a non-empty inconclusive_reason; use it when required checks cannot complete.
- Finding file paths must be normalized repository-relative paths, never absolute paths.
Before returning, self-check the final object against every rule above. Output one final object only.
END GATEWAY OUTPUT CONTRACT"""


@dataclass
class Attempt:
    provider: str
    exit_code: int
    timed_out: bool
    stdout: bytes
    stderr: bytes
    result: bytes = b""
    prompt_sha256: str | None = None

    def trace_record(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "stdout_base64": base64.b64encode(self.stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(self.stderr).decode("ascii"),
            "result_base64": base64.b64encode(self.result).decode("ascii"),
            "prompt_sha256": self.prompt_sha256,
        }


@dataclass(frozen=True)
class GitSnapshot:
    top_level: Path
    git_dir: Path
    git_common_dir: Path
    head: str
    base: str
    merge_base: str
    worktree_fingerprint: str


@dataclass(frozen=True)
class FrozenInputs:
    prompt: Path
    schema: Path
    context_dirs: tuple[Path, ...]
    context_hashes: tuple[dict[str, Any], ...]
    tree_fingerprint: str


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_private_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_private_json(path: Path, value: Any) -> bytes:
    encoded = _json_bytes(value)
    _write_private_bytes(path, encoded)
    return encoded


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WrapperError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WrapperError(f"{label} must contain a JSON object")
    return value


def _resolve_destination(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.parent.resolve() / expanded.name


def _safe_regular_bytes(path: Path, label: str) -> bytes:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise SnapshotError(f"{label} must be a regular file without symlinks: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise SnapshotError(f"{label} changed while it was opened: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_mode,
    ) != (
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
        opened.st_mode,
    ):
        raise SnapshotError(f"{label} changed while it was read: {path}")
    return b"".join(chunks)


def _update_tree_hash(hasher: Any, root: Path, current: Path) -> None:
    current_stat = current.lstat()
    if stat.S_ISLNK(current_stat.st_mode):
        raise SnapshotError(f"frozen context contains a symbolic link: {current}")
    if not stat.S_ISDIR(current_stat.st_mode):
        raise SnapshotError(f"frozen context root is not a directory: {current}")
    relative_dir = (
        current.relative_to(root).as_posix().encode("utf-8", "surrogateescape")
    )
    hasher.update(b"D\x00" + relative_dir + b"\x00")
    with os.scandir(current) as iterator:
        entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
    for entry in entries:
        path = current / entry.name
        entry_stat = entry.stat(follow_symlinks=False)
        relative = path.relative_to(root).as_posix().encode("utf-8", "surrogateescape")
        if stat.S_ISLNK(entry_stat.st_mode):
            raise SnapshotError(f"frozen context contains a symbolic link: {path}")
        if stat.S_ISDIR(entry_stat.st_mode):
            _update_tree_hash(hasher, root, path)
        elif stat.S_ISREG(entry_stat.st_mode):
            content = _safe_regular_bytes(path, "context file")
            hasher.update(
                b"F\x00" + relative + b"\x00" + _sha256(content).encode() + b"\x00"
            )
        else:
            raise SnapshotError(f"frozen context contains a special file: {path}")


def _tree_fingerprint(root: Path) -> str:
    hasher = hashlib.sha256()
    _update_tree_hash(hasher, root, root)
    return hasher.hexdigest()


def _set_frozen_modes(root: Path, *, read_only: bool) -> None:
    directory_mode = 0o500 if read_only else 0o700
    file_mode = 0o400 if read_only else 0o600
    if not read_only:
        os.chmod(root, directory_mode)
    for current, directories, files in os.walk(root, topdown=not read_only):
        current_path = Path(current)
        for filename in files:
            os.chmod(current_path / filename, file_mode, follow_symlinks=False)
        for directory in directories:
            os.chmod(current_path / directory, directory_mode, follow_symlinks=False)
        os.chmod(current_path, directory_mode, follow_symlinks=False)


def _copy_context_tree(source: Path, destination: Path) -> str:
    before = _tree_fingerprint(source)

    def copy_directory(source_dir: Path, destination_dir: Path) -> None:
        destination_dir.mkdir(mode=0o700)
        with os.scandir(source_dir) as iterator:
            entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            source_path = source_dir / entry.name
            destination_path = destination_dir / entry.name
            entry_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry_stat.st_mode):
                raise SnapshotError(f"context contains a symbolic link: {source_path}")
            if stat.S_ISDIR(entry_stat.st_mode):
                copy_directory(source_path, destination_path)
            elif stat.S_ISREG(entry_stat.st_mode):
                _write_private_bytes(
                    destination_path,
                    _safe_regular_bytes(source_path, "context file"),
                )
            else:
                raise SnapshotError(f"context contains a special file: {source_path}")

    copy_directory(source, destination)
    after = _tree_fingerprint(source)
    frozen = _tree_fingerprint(destination)
    if before != after or before != frozen:
        raise SnapshotError(f"context changed while it was frozen: {source}")
    return frozen


def _freeze_inputs(args: argparse.Namespace, frozen_root: Path) -> FrozenInputs:
    prompt_bytes = _safe_regular_bytes(args.prompt, "review prompt")
    schema_bytes = _safe_regular_bytes(args.schema, "review schema")
    try:
        prompt_text = prompt_bytes.decode("utf-8")
        schema_value = json.loads(schema_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"review prompt/schema could not be frozen: {exc}") from exc
    if not isinstance(schema_value, dict):
        raise SnapshotError("review schema root must be an object")
    try:
        claude_adapter._validate_provider_schema(schema_value)
    except claude_adapter.ReviewError as exc:
        raise SnapshotError(str(exc)) from exc

    if any(frozen_root.iterdir()):
        raise SnapshotError("frozen input root was not empty")
    os.chmod(frozen_root, 0o700)
    frozen_context_root = frozen_root / "contexts"
    frozen_context_root.mkdir(mode=0o700)
    context_paths: list[Path] = []
    context_hashes: list[dict[str, Any]] = []
    replacements: list[tuple[str, str]] = []
    for index, source in enumerate(args.context_dir):
        destination = frozen_context_root / str(index)
        content_hash = _copy_context_tree(source, destination)
        context_paths.append(destination)
        context_hashes.append(
            {
                "source": str(source),
                "content_sha256": content_hash,
            }
        )
        replacements.append((str(source), str(destination)))
    for source, destination in sorted(
        replacements, key=lambda item: len(item[0]), reverse=True
    ):
        prompt_text = prompt_text.replace(source, destination)
    if context_paths:
        prompt_text += "\n\nFrozen reviewer context directories (read-only):\n"
        prompt_text += "".join(f"- {path}\n" for path in context_paths)
    prompt_text += f"\n\n{_review_output_contract()}\n"

    frozen_prompt = frozen_root / "prompt.txt"
    frozen_schema = frozen_root / "review-schema.json"
    rewritten_prompt = prompt_text.encode("utf-8")
    if len(rewritten_prompt) > claude_adapter.MAX_PROMPT_BYTES:
        raise SnapshotError("rewritten frozen prompt exceeds the adapter size limit")
    _write_private_bytes(frozen_prompt, rewritten_prompt)
    _write_private_bytes(frozen_schema, schema_bytes)
    fingerprint = _tree_fingerprint(frozen_root)
    _set_frozen_modes(frozen_root, read_only=True)
    return FrozenInputs(
        prompt=frozen_prompt,
        schema=frozen_schema,
        context_dirs=tuple(context_paths),
        context_hashes=tuple(context_hashes),
        tree_fingerprint=fingerprint,
    )


def _assert_frozen_inputs(frozen_root: Path, frozen: FrozenInputs) -> None:
    current = _tree_fingerprint(frozen_root)
    if current != frozen.tree_fingerprint:
        raise SnapshotError("frozen review inputs changed during provider execution")


def _git_environment() -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ATTR_NOSYSTEM": "1",
    }
    return environment


def _run_git(cwd: Path, *arguments: str, timeout: int = 60) -> bytes:
    git = shutil.which("git")
    if git is None:
        raise SnapshotError("git was not found on PATH")
    command = [
        git,
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "submodule.recurse=false",
        "-c",
        "color.ui=false",
        "-C",
        str(cwd),
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnapshotError(f"safe Git inspection failed: {exc}") from exc
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise SnapshotError(
            f"safe Git inspection failed for {arguments[0]}: {diagnostic or result.returncode}"
        )
    return result.stdout


def _git_identity(
    cwd: Path, expected_head: str, expected_base: str, expected_merge_base: str
) -> tuple[Path, Path, Path, str, str]:
    inside = _run_git(cwd, "rev-parse", "--is-inside-work-tree").decode().strip()
    if inside != "true":
        raise SnapshotError("review cwd is not inside a Git worktree")
    top_level = Path(
        _run_git(cwd, "rev-parse", "--show-toplevel").decode().strip()
    ).resolve()
    if top_level != cwd:
        raise SnapshotError(f"review cwd must be the worktree root: {top_level}")
    git_dir = Path(
        _run_git(cwd, "rev-parse", "--path-format=absolute", "--git-dir")
        .decode()
        .strip()
    ).resolve()
    git_common_dir = Path(
        _run_git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        .decode()
        .strip()
    ).resolve()
    for label, commit in (
        ("expected head", expected_head),
        ("expected base", expected_base),
        ("expected merge base", expected_merge_base),
    ):
        _run_git(cwd, "cat-file", "-e", f"{commit}^{{commit}}")
        if not commit:
            raise SnapshotError(f"{label} is empty")
    actual_head = (
        _run_git(cwd, "rev-parse", "--verify", "HEAD^{commit}").decode().strip().lower()
    )
    if actual_head != expected_head.lower():
        raise SnapshotError(
            f"review HEAD mismatch: expected {expected_head}, found {actual_head}"
        )
    merge_bases = {
        line.strip().lower()
        for line in _run_git(cwd, "merge-base", "--all", expected_head, expected_base)
        .decode()
        .splitlines()
        if line.strip()
    }
    if merge_bases != {expected_merge_base.lower()}:
        raise SnapshotError(
            "computed merge base mismatch: "
            f"expected {expected_merge_base}, found {sorted(merge_bases)}"
        )
    return top_level, git_dir, git_common_dir, actual_head, expected_merge_base.lower()


def _validate_index_visibility(raw: bytes) -> None:
    for record in (entry for entry in raw.split(b"\x00") if entry):
        if len(record) < 3 or record[1:2] != b" " or record[:1] != b"H":
            tag = record[:1].decode("ascii", errors="replace") or "?"
            path = os.fsdecode(record[2:]) if len(record) > 2 else "<unknown>"
            raise SnapshotError(
                "tracked files must not use assume-unchanged, skip-worktree, "
                f"or another nonstandard index state: tag={tag!r} path={path!r}"
            )


def _tracked_worktree_fingerprint(
    cwd: Path, stage: bytes, tracked: bytes, visibility: bytes
) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"INDEX\x00" + stage + b"\x00")
    hasher.update(b"VISIBILITY\x00" + visibility + b"\x00")
    for raw_path in sorted(path for path in tracked.split(b"\x00") if path):
        relative_text = os.fsdecode(raw_path)
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError(
                f"Git returned an unsafe tracked path: {relative_text!r}"
            )
        path = cwd / relative
        entry_stat = path.lstat()
        hasher.update(b"PATH\x00" + raw_path + b"\x00")
        if stat.S_ISREG(entry_stat.st_mode):
            content = _safe_regular_bytes(path, "tracked worktree file")
            hasher.update(b"FILE\x00" + _sha256(content).encode() + b"\x00")
        elif stat.S_ISLNK(entry_stat.st_mode):
            target = os.readlink(path).encode("utf-8", "surrogateescape")
            hasher.update(b"SYMLINK\x00" + target + b"\x00")
        elif stat.S_ISDIR(entry_stat.st_mode):
            hasher.update(b"GITLINK\x00")
        else:
            raise SnapshotError(f"tracked path is a special file: {relative_text}")
    return hasher.hexdigest()


def _capture_git_snapshot(args: argparse.Namespace) -> GitSnapshot:
    first = _git_identity(
        args.cwd,
        args.expected_head,
        args.expected_base,
        args.expected_merge_base,
    )
    status = _run_git(
        args.cwd,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status:
        raise SnapshotError(
            "review cwd must be clean, including staged and untracked files"
        )
    stage = _run_git(args.cwd, "ls-files", "--stage", "-z")
    tracked = _run_git(args.cwd, "ls-files", "--cached", "-z")
    visibility = _run_git(args.cwd, "ls-files", "-v", "-z")
    _validate_index_visibility(visibility)
    fingerprint = _tracked_worktree_fingerprint(args.cwd, stage, tracked, visibility)
    final_status = _run_git(
        args.cwd,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    final_visibility = _run_git(args.cwd, "ls-files", "-v", "-z")
    _validate_index_visibility(final_visibility)
    second = _git_identity(
        args.cwd,
        args.expected_head,
        args.expected_base,
        args.expected_merge_base,
    )
    if final_status or visibility != final_visibility or first != second:
        raise SnapshotError("Git state changed while its review snapshot was captured")
    top_level, git_dir, git_common_dir, head, merge_base = first
    return GitSnapshot(
        top_level=top_level,
        git_dir=git_dir,
        git_common_dir=git_common_dir,
        head=head,
        base=args.expected_base.lower(),
        merge_base=merge_base,
        worktree_fingerprint=fingerprint,
    )


def _assert_git_snapshot(args: argparse.Namespace, expected: GitSnapshot) -> None:
    current = _capture_git_snapshot(args)
    if current != expected:
        raise SnapshotError("Git review snapshot changed during provider execution")


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_private_artifact_paths(
    args: argparse.Namespace, snapshot: GitSnapshot
) -> None:
    safe_git_namespace = snapshot.git_common_dir / "gh-workflow-suite"
    for path in (
        args.output,
        args.error_file,
        args.provider_state,
        args.metadata_output,
        args.trace_output,
    ):
        if _is_beneath(path, snapshot.git_common_dir) and not _is_beneath(
            path, safe_git_namespace
        ):
            raise SnapshotError(
                "private review artifact inside Git metadata must be beneath "
                f"{safe_git_namespace}: {path}"
            )
        if _is_beneath(path, snapshot.top_level) and not _is_beneath(
            path, safe_git_namespace
        ):
            raise SnapshotError(
                f"private review artifact must be outside the worktree: {path}"
            )


def _preflight_private_artifact_paths(args: argparse.Namespace) -> None:
    """Reject unsafe artifact destinations before creating provider state or outputs."""
    top_level, git_dir, git_common_dir, head, merge_base = _git_identity(
        args.cwd,
        args.expected_head,
        args.expected_base,
        args.expected_merge_base,
    )
    _validate_private_artifact_paths(
        args,
        GitSnapshot(
            top_level=top_level,
            git_dir=git_dir,
            git_common_dir=git_common_dir,
            head=head,
            base=args.expected_base.lower(),
            merge_base=merge_base,
            worktree_fingerprint="",
        ),
    )


def _new_provider_state() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "state_id": secrets.token_hex(16),
        "active_provider": "claude",
        "fallback_trigger": None,
        "quota_classifier_version": CLASSIFIER_VERSION,
        "quota_classification": None,
        "claude_diagnostic_sha256": None,
        "codex_attempted_gates": {},
    }


def _validate_provider_state(state: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(PROVIDER_STATE_FIELDS - state.keys())
    unexpected = sorted(state.keys() - PROVIDER_STATE_FIELDS)
    if missing or unexpected:
        raise WrapperError(
            f"provider state fields mismatch; missing={missing}, unexpected={unexpected}"
        )
    if (
        not isinstance(state["schema_version"], int)
        or isinstance(state["schema_version"], bool)
        or state["schema_version"] != 2
    ):
        raise WrapperError("provider state schema_version must be 2")
    state_id = state["state_id"]
    if not isinstance(state_id, str) or re.fullmatch(r"[0-9a-f]{32}", state_id) is None:
        raise WrapperError("provider state state_id is invalid")
    active = state["active_provider"]
    if active not in {"claude", "codex"}:
        raise WrapperError("provider state active_provider must be claude or codex")
    if (
        not isinstance(state["quota_classifier_version"], int)
        or isinstance(state["quota_classifier_version"], bool)
        or state["quota_classifier_version"] != CLASSIFIER_VERSION
    ):
        raise WrapperError("provider state quota classifier version is incompatible")
    trigger = state["fallback_trigger"]
    classification = state["quota_classification"]
    diagnostic_hash = state["claude_diagnostic_sha256"]
    codex_attempted_gates = state["codex_attempted_gates"]
    if not isinstance(codex_attempted_gates, dict):
        raise WrapperError("provider state codex_attempted_gates must be an object")
    for gate_id, attempt_id in codex_attempted_gates.items():
        if not isinstance(gate_id, str) or GATE_ID_RE.fullmatch(gate_id) is None:
            raise WrapperError("provider state contains an invalid Codex gate ID")
        if (
            not isinstance(attempt_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None
        ):
            raise WrapperError("provider state contains an invalid Codex attempt ID")
    if active == "claude" and (
        trigger is not None
        or classification is not None
        or diagnostic_hash is not None
        or codex_attempted_gates
    ):
        raise WrapperError("Claude provider state cannot contain fallback metadata")
    if active == "codex":
        if trigger not in VALID_FALLBACK_TRIGGERS:
            raise WrapperError("Codex provider state lacks a valid fallback trigger")
        if trigger == "CLAUDE_QUOTA_EXHAUSTED":
            if classification not in VALID_QUOTA_CLASSIFICATIONS:
                raise WrapperError("Codex provider state lacks a quota classification")
        elif classification is not None:
            raise WrapperError(
                "Non-quota Codex provider state cannot contain a quota classification"
            )
        if (
            not isinstance(diagnostic_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", diagnostic_hash) is None
        ):
            raise WrapperError("Codex provider state lacks a diagnostic hash")
    return state


@contextmanager
def _locked_provider_state(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "rb+") as lock_stream:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        if path.exists():
            if path.is_symlink():
                raise WrapperError("provider state must not be a symbolic link")
            state = _validate_provider_state(_read_json_object(path, "provider state"))
        else:
            state = _new_provider_state()
            _write_private_json(path, state)
        yield state


def _normalized_diagnostic_lines(raw: bytes) -> list[str]:
    without_ansi = ANSI_ESCAPE_RE.sub(b"", raw[:131_072])
    text = without_ansi.decode("utf-8", errors="replace").replace("\r", "\n")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _error_envelope_signal(raw: bytes) -> tuple[str | None, list[str]]:
    try:
        payload = claude_adapter._decode_json_document(raw)
    except claude_adapter.ReviewError:
        return None, []
    if not isinstance(payload, dict) or payload.get("type") != "result":
        return None, []
    if payload.get("is_error") is not True:
        return None, []

    code_values: list[Any] = [payload.get("code"), payload.get("error_code")]
    message_values: list[Any] = [payload.get("message"), payload.get("result")]
    error = payload.get("error")
    if isinstance(error, dict):
        code_values.extend((error.get("code"), error.get("type")))
        message_values.append(error.get("message"))
    for value in code_values:
        if isinstance(value, str) and value.casefold() in QUOTA_MACHINE_CODES:
            return value.casefold(), []
    messages = [value for value in message_values if isinstance(value, str)]
    return None, messages


def _json_line_machine_code(lines: list[str]) -> str | None:
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        candidates: list[Any] = [payload.get("code"), payload.get("error_code")]
        error = payload.get("error")
        if isinstance(error, dict):
            candidates.extend((error.get("code"), error.get("type")))
        for candidate in candidates:
            if (
                isinstance(candidate, str)
                and candidate.casefold() in QUOTA_MACHINE_CODES
            ):
                return candidate.casefold()
    return None


def _classify_quota(
    adapter_exit: int, raw_stdout: bytes, raw_stderr: bytes
) -> tuple[str | None, str]:
    diagnostic_hash = _sha256(raw_stdout + b"\x00" + raw_stderr)
    if adapter_exit not in {3, 5}:
        return None, diagnostic_hash

    envelope_code, envelope_messages = _error_envelope_signal(raw_stdout)
    if envelope_code is not None:
        return f"MACHINE_{envelope_code.upper()}", diagnostic_hash

    stderr_lines = _normalized_diagnostic_lines(raw_stderr)
    stderr_code = _json_line_machine_code(stderr_lines)
    if stderr_code is not None:
        return f"MACHINE_{stderr_code.upper()}", diagnostic_hash

    candidates = stderr_lines + [
        line
        for message in envelope_messages
        for line in _normalized_diagnostic_lines(message.encode("utf-8"))
    ]
    for line in candidates:
        for identifier, pattern in QUOTA_MESSAGE_PATTERNS:
            if pattern.fullmatch(line):
                return f"MESSAGE_{identifier}", diagnostic_hash
    return None, diagnostic_hash


def _combined_diagnostics(attempts: list[Attempt]) -> bytes:
    chunks: list[bytes] = []
    for attempt in attempts:
        if attempt.stderr:
            chunks.extend(
                (f"[{attempt.provider}]\n".encode(), attempt.stderr.rstrip(), b"\n")
            )
    return b"".join(chunks)


def _minimal_codex_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "CODEX_HOME",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
    }
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    environment.setdefault("TERM", "dumb")
    return environment


def _codex_command(
    binary: str,
    cwd: Path,
    schema: Path,
    result_path: Path,
    shell_home: Path,
    model: str,
) -> list[str]:
    shell_path = os.environ.get("PATH", "/usr/bin:/bin")
    command = [
        binary,
        "-a",
        "never",
        "exec",
        "--strict-config",
        "-s",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-c",
        "project_doc_max_bytes=0",
        "-c",
        "mcp_servers={}",
        "-c",
        'web_search="disabled"',
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        f"shell_environment_policy.set.PATH={json.dumps(shell_path)}",
        "-c",
        f"shell_environment_policy.set.HOME={json.dumps(str(shell_home))}",
    ]
    for feature in WEB_FEATURES:
        command.extend(("--disable", feature))
    command.extend(
        (
            "--color",
            "never",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(result_path),
            "-C",
            str(cwd),
        )
    )
    if model:
        command.extend(("--model", model))
    command.append("-")
    return command


def _run_codex(
    *,
    binary_name: str,
    cwd: Path,
    prompt: Path,
    schema: Path,
    result_path: Path,
    model: str,
    timeout: int,
) -> Attempt:
    prompt_sha256 = _sha256(_safe_regular_bytes(prompt, "Codex review prompt"))
    binary = shutil.which(binary_name)
    if binary is None:
        return Attempt(
            "codex",
            127,
            False,
            b"",
            f"{binary_name} not found on PATH\n".encode(),
            prompt_sha256=prompt_sha256,
        )
    try:
        shell_home = result_path.parent / f"{result_path.stem}-shell-home"
        shell_home.mkdir(mode=0o700)
        command = _codex_command(binary, cwd, schema, result_path, shell_home, model)
        with prompt.open("rb") as prompt_stream:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=_minimal_codex_environment(),
                stdin=prompt_stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                stdout, stderr = claude_adapter._terminate_process_group(process)
                timed_out = True
    except OSError as exc:
        return Attempt(
            "codex",
            127,
            False,
            b"",
            (str(exc) + "\n").encode(),
            prompt_sha256=prompt_sha256,
        )
    try:
        result = result_path.read_bytes() if result_path.is_file() else b""
    except OSError as exc:
        stderr += (f"\nCould not read Codex result: {exc}\n").encode()
        result = b""
    return Attempt(
        "codex",
        process.returncode,
        timed_out,
        stdout,
        stderr,
        result,
        prompt_sha256,
    )


def _write_codex_repair_prompt(
    original_prompt: Path, destination: Path, validation_error: str
) -> Path:
    original = _safe_regular_bytes(original_prompt, "original Codex review prompt")
    diagnostic = validation_error.strip()[:2_000]
    repair = (
        b"\n\nBEGIN STRUCTURED OUTPUT REPAIR\n"
        b"Previous generation was rejected by the gateway validator.\n"
        + f"Validation error: {diagnostic}\n".encode("utf-8", errors="replace")
        + b"Re-review the same immutable evidence where needed, then generate a new final object. "
        b"Correct the validation error and obey the gateway output contract. Do not explain the "
        b"repair and do not emit a progress object. If required checks cannot honestly be completed, "
        b"return INCONCLUSIVE with a concrete reason.\n"
        b"END STRUCTURED OUTPUT REPAIR\n"
    )
    combined = original + repair
    if len(combined) > claude_adapter.MAX_PROMPT_BYTES:
        raise SnapshotError("Codex repair prompt exceeds the adapter size limit")
    _write_private_bytes(destination, combined)
    return destination


def _claude_command(
    args: argparse.Namespace, raw_path: Path, error_path: Path, output: Path
) -> list[str]:
    adapter = Path(claude_adapter.__file__).resolve()
    command = [
        sys.executable,
        str(adapter),
        "--prompt",
        str(args.prompt),
        "--schema",
        str(args.schema),
        "--output",
        str(output),
        "--error-file",
        str(error_path),
        "--raw-output",
        str(raw_path),
        "--cwd",
        str(args.cwd),
        "--expected-head",
        args.expected_head,
        "--expected-base",
        args.expected_base,
        "--expected-merge-base",
        args.expected_merge_base,
        "--timeout",
        str(args.timeout),
        "--effort",
        args.effort,
        "--claude-bin",
        args.claude_bin,
    ]
    if args.model:
        command.extend(("--model", args.model))
    if args.max_budget_usd is not None:
        command.extend(("--max-budget-usd", str(args.max_budget_usd)))
    for context_dir in args.context_dir:
        command.extend(("--context-dir", str(context_dir)))
    return command


def _run_claude(
    args: argparse.Namespace, raw_path: Path, error_path: Path, output: Path
) -> Attempt:
    command = _claude_command(args, raw_path, error_path, output)
    completed = subprocess.run(
        command,
        cwd=args.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        start_new_session=True,
    )
    raw = raw_path.read_bytes() if raw_path.is_file() else b""
    provider_error = error_path.read_bytes() if error_path.is_file() else b""
    stderr = provider_error + completed.stderr
    result = output.read_bytes() if output.is_file() else b""
    prompt_sha256 = _sha256(_safe_regular_bytes(args.prompt, "Claude review prompt"))
    return Attempt(
        "claude",
        completed.returncode,
        completed.returncode == 4,
        raw,
        stderr,
        result,
        prompt_sha256,
    )


def _validated_review(raw: bytes, args: argparse.Namespace) -> dict[str, Any]:
    decoded = claude_adapter._decode_json_document(raw)
    if not isinstance(decoded, dict):
        raise claude_adapter.ReviewError("Review output is not a JSON object")
    return claude_adapter._validate_review(
        decoded,
        args.expected_head,
        args.expected_base,
        args.expected_merge_base,
    )


def _provider_version(binary_name: str) -> str:
    binary = shutil.which(binary_name)
    if binary is None:
        return "unavailable"
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=_minimal_codex_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return (result.stdout.strip() or result.stderr.strip() or "unknown")[:300]


def _finish(
    *,
    args: argparse.Namespace,
    review: dict[str, Any],
    exit_code: int,
    provider: str,
    provider_state: dict[str, Any],
    attempts: list[Attempt],
    classification: str | None,
    selected_from_state: bool,
) -> int:
    review_bytes = _write_private_json(args.output, review)
    trace = {
        "schema_version": 2,
        "gate_id": args.gate_id,
        "provider_state_id": provider_state["state_id"],
        "codex_attempt_id": provider_state["codex_attempted_gates"].get(args.gate_id),
        "attempts": [attempt.trace_record() for attempt in attempts],
    }
    trace_bytes = _write_private_json(args.trace_output, trace)
    diagnostics = _combined_diagnostics(attempts)
    _write_private_bytes(args.error_file, diagnostics)
    state_bytes = _json_bytes(provider_state)
    prompt_bytes = args.prompt.read_bytes()
    schema_bytes = args.schema.read_bytes()
    metadata = {
        "schema_version": 1,
        "gate_id": args.gate_id,
        "review_provider": provider,
        "provider_binary": shutil.which(
            args.codex_bin if provider == "codex_fallback" else args.claude_bin
        ),
        "provider_version": _provider_version(
            args.codex_bin if provider == "codex_fallback" else args.claude_bin
        ),
        "provider_state_id": provider_state["state_id"],
        "codex_attempt_id": provider_state["codex_attempted_gates"].get(args.gate_id),
        "provider_selected_from_state": selected_from_state,
        "fallback_used": provider == "codex_fallback",
        "fallback_trigger": provider_state["fallback_trigger"],
        "quota_classifier_version": CLASSIFIER_VERSION,
        "quota_classification": provider_state["quota_classification"],
        "claude_diagnostic_sha256": provider_state["claude_diagnostic_sha256"],
        "independent_vendor_review": provider == "claude",
        "attempt_count": len(attempts),
        "exit_code": exit_code,
        "verdict": review["verdict"],
        "reviewed_head_sha": args.expected_head,
        "reviewed_base_sha": args.expected_base,
        "reviewed_merge_base_sha": args.expected_merge_base,
        "review_sha256": _sha256(review_bytes),
        "prompt_sha256": _sha256(prompt_bytes),
        "schema_sha256": _sha256(schema_bytes),
        "frozen_inputs_sha256": getattr(args, "frozen_inputs_sha256", None),
        "frozen_contexts": getattr(args, "frozen_context_hashes", []),
        "provider_state_sha256": _sha256(state_bytes),
        "trace_sha256": _sha256(trace_bytes),
        "diagnostics_sha256": _sha256(diagnostics),
    }
    _write_private_json(args.metadata_output, metadata)
    return exit_code


def _inconclusive(args: argparse.Namespace, reason: str) -> dict[str, Any]:
    return claude_adapter._inconclusive(
        reason,
        args.expected_head,
        args.expected_base,
        args.expected_merge_base,
    )


def _check(claude_bin: str, codex_bin: str) -> int:
    try:
        canonical_schema = json.loads(
            CANONICAL_REVIEW_SCHEMA.read_text(encoding="utf-8")
        )
        if not isinstance(canonical_schema, dict):
            raise claude_adapter.ReviewError(
                "Canonical review schema root must be a JSON object"
            )
        claude_adapter._validate_provider_schema(canonical_schema)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        claude_adapter.ReviewError,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "review_schema": {
                        "ok": False,
                        "path": str(CANONICAL_REVIEW_SCHEMA),
                        "error": str(exc),
                    },
                },
                indent=2,
            )
        )
        return 2
    schema_details = {"ok": True, "path": str(CANONICAL_REVIEW_SCHEMA)}

    claude_output = io.StringIO()
    with redirect_stdout(claude_output):
        claude_status = claude_adapter._check_claude(claude_bin)
    try:
        claude_details = json.loads(claude_output.getvalue())
    except json.JSONDecodeError:
        claude_details = {"ok": False, "error": "Claude check returned malformed JSON"}

    codex_path = shutil.which(codex_bin)
    codex_details: dict[str, Any]
    codex_status = 2
    if codex_path is None:
        codex_details = {"ok": False, "error": f"{codex_bin} not found on PATH"}
    else:
        try:
            version = subprocess.run(
                [codex_path, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                env=_minimal_codex_environment(),
            )
            root_help = subprocess.run(
                [codex_path, "--help"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                env=_minimal_codex_environment(),
            )
            exec_help = subprocess.run(
                [codex_path, "exec", "--help"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
                env=_minimal_codex_environment(),
            )
            help_text = (
                root_help.stdout
                + root_help.stderr
                + exec_help.stdout
                + exec_help.stderr
            )
            required = {
                "--ask-for-approval",
                "--sandbox",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--strict-config",
                "--output-schema",
                "--output-last-message",
                "--disable",
            }
            missing = sorted(flag for flag in required if flag not in help_text)
            ok = (
                version.returncode == 0
                and root_help.returncode == 0
                and exec_help.returncode == 0
                and not missing
            )
            codex_status = 0 if ok else 2
            codex_details = {
                "ok": ok,
                "binary": codex_path,
                "version": version.stdout.strip() or version.stderr.strip(),
                "missing_flags": missing,
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            codex_details = {"ok": False, "binary": codex_path, "error": str(exc)}
    print(
        json.dumps(
            {
                "ok": claude_status == 0 and codex_status == 0,
                "review_schema": schema_details,
                "claude": claude_details,
                "codex": codex_details,
            },
            indent=2,
        )
    )
    return 0 if claude_status == 0 and codex_status == 0 else 2


def _self_test() -> int:
    try:
        canonical_schema = json.loads(
            CANONICAL_REVIEW_SCHEMA.read_text(encoding="utf-8")
        )
        if not isinstance(canonical_schema, dict):
            raise AssertionError("canonical review schema root is not an object")
        claude_adapter._validate_provider_schema(canonical_schema)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        claude_adapter.ReviewError,
    ) as exc:
        raise AssertionError(
            f"canonical review schema failed provider preflight: {exc}"
        ) from exc
    with redirect_stdout(io.StringIO()):
        if claude_adapter._self_test() != 0:
            raise AssertionError("Claude adapter self-test failed")
    positives = (
        (
            5,
            _json_bytes(
                {
                    "type": "result",
                    "is_error": True,
                    "error": {"code": "usage_cap_reached"},
                }
            ),
            b"",
        ),
        (3, b"", b"You're out of usage credits\n"),
        (3, b"", b"You've hit your monthly spend limit.\n"),
        (
            3,
            _json_bytes(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": 429,
                    "result": "You've hit your weekly limit · resets Jul 18 at 12am (America/Campo_Grande)",
                }
            ),
            b"",
        ),
        (3, b"", b"Credit balance is too low\n"),
    )
    for exit_code, stdout, stderr in positives:
        classification, _ = _classify_quota(exit_code, stdout, stderr)
        if classification is None:
            raise AssertionError(
                f"quota classifier rejected positive fixture: {stderr!r}"
            )
    negatives = (
        (3, b"", b"Server is temporarily limiting requests (not your usage limit)\n"),
        (3, b"", b"Request rejected (429)\n"),
        (4, b"", b"You're out of usage credits\n"),
        (3, b"", b"Disk quota exceeded\n"),
        (
            5,
            _json_bytes(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "You're out of usage credits",
                }
            ),
            b"",
        ),
    )
    for exit_code, stdout, stderr in negatives:
        classification, _ = _classify_quota(exit_code, stdout, stderr)
        if classification is not None:
            raise AssertionError(
                f"quota classifier accepted negative fixture: {stderr!r}"
            )
    command = _codex_command(
        "codex",
        Path("/repo"),
        Path("/schema"),
        Path("/out"),
        Path("/private-home"),
        "",
    )
    required_fragments = {
        "never",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "project_doc_max_bytes=0",
        "mcp_servers={}",
        'web_search="disabled"',
        'shell_environment_policy.inherit="none"',
    }
    if not required_fragments.issubset(command):
        raise AssertionError("Codex command lost a confinement option")
    artifact_snapshot = GitSnapshot(
        top_level=Path("/repo"),
        git_dir=Path("/repo/.git"),
        git_common_dir=Path("/repo/.git"),
        head="a" * 40,
        base="a" * 40,
        merge_base="a" * 40,
        worktree_fingerprint="test",
    )
    artifact_args = argparse.Namespace(
        output=Path("/repo/.git/HEAD"),
        error_file=Path("/private/error"),
        provider_state=Path("/private/state"),
        metadata_output=Path("/private/metadata"),
        trace_output=Path("/private/trace"),
    )
    try:
        _validate_private_artifact_paths(artifact_args, artifact_snapshot)
    except SnapshotError:
        pass
    else:
        raise AssertionError("artifact validation accepted a Git control file")
    artifact_args.output = Path("/repo/.git/gh-workflow-suite/run/review.json")
    _validate_private_artifact_paths(artifact_args, artifact_snapshot)
    _validate_index_visibility(b"H normal.py\x00H linked-submodule\x00")
    for hidden_fixture in (b"h hidden.py\x00", b"S sparse.py\x00"):
        try:
            _validate_index_visibility(hidden_fixture)
        except SnapshotError:
            pass
        else:
            raise AssertionError("index visibility validation accepted a hidden file")
    provider_state = _new_provider_state()
    _validate_provider_state(provider_state)
    provider_state.update(
        {
            "active_provider": "codex",
            "fallback_trigger": "CLAUDE_QUOTA_EXHAUSTED",
            "quota_classification": "MACHINE_USAGE_CAP_REACHED",
            "claude_diagnostic_sha256": "d" * 64,
            "codex_attempted_gates": {"full-review-01": "e" * 32},
        }
    )
    _validate_provider_state(provider_state)
    generic_provider_state = _new_provider_state()
    generic_provider_state.update(
        {
            "active_provider": "codex",
            "fallback_trigger": "CLAUDE_REVIEW_FAILED",
            "claude_diagnostic_sha256": "d" * 64,
        }
    )
    _validate_provider_state(generic_provider_state)
    provider_state["codex_attempted_gates"] = {"invalid gate": "e" * 32}
    try:
        _validate_provider_state(provider_state)
    except WrapperError:
        pass
    else:
        raise AssertionError("provider state accepted an invalid gate ID")
    with (
        tempfile.TemporaryDirectory(prefix="review-self-source-") as source_name,
        tempfile.TemporaryDirectory(prefix="review-self-frozen-") as frozen_name,
    ):
        source_root = Path(source_name)
        context = source_root / "context"
        context.mkdir()
        evidence = context / "evidence.txt"
        evidence.write_text("evidence\n", encoding="utf-8")
        prompt = source_root / "prompt.txt"
        prompt.write_text(f"Read {evidence}\n", encoding="utf-8")
        schema = source_root / "schema.json"
        schema.write_text("{}\n", encoding="utf-8")
        frozen_root = Path(frozen_name)
        frozen_args = argparse.Namespace(
            prompt=prompt,
            schema=schema,
            context_dir=[context],
        )
        frozen = _freeze_inputs(frozen_args, frozen_root)
        try:
            rewritten = _safe_regular_bytes(frozen.prompt, "self-test prompt").decode()
            if (
                str(context) in rewritten
                or str(frozen.context_dirs[0]) not in rewritten
            ):
                raise AssertionError("frozen prompt did not rewrite its context path")
            if "BEGIN GATEWAY OUTPUT CONTRACT" not in rewritten:
                raise AssertionError(
                    "frozen prompt omitted the semantic output contract"
                )
            repair_path = source_root / "repair-prompt.txt"
            _write_codex_repair_prompt(
                frozen.prompt,
                repair_path,
                "security_categories_checked mismatch",
            )
            repair_text = repair_path.read_text(encoding="utf-8")
            if (
                "BEGIN STRUCTURED OUTPUT REPAIR" not in repair_text
                or "security_categories_checked mismatch" not in repair_text
                or "BEGIN GATEWAY OUTPUT CONTRACT" not in repair_text
            ):
                raise AssertionError("Codex repair prompt lost contract or diagnostic")
            if stat.S_IMODE(frozen.prompt.stat().st_mode) != 0o400:
                raise AssertionError("frozen prompt was not made read-only")
            _assert_frozen_inputs(frozen_root, frozen)
        finally:
            _set_frozen_modes(frozen_root, read_only=False)
        bad_link = context / "bad-link"
        bad_link.symlink_to(evidence)
        try:
            _tree_fingerprint(context)
        except SnapshotError:
            pass
        else:
            raise AssertionError("context fingerprint accepted a symbolic link")
    print(json.dumps({"ok": True, "tests": 28}))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Claude review with a sticky Codex fallback."
    )
    parser.add_argument("--check", action="store_true", help="Check both reviewer CLIs")
    parser.add_argument(
        "--self-test", action="store_true", help="Run offline wrapper tests"
    )
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--error-file", type=Path)
    parser.add_argument("--provider-state", type=Path)
    parser.add_argument("--metadata-output", type=Path)
    parser.add_argument("--trace-output", type=Path)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--context-dir", action="append", type=Path, default=[])
    parser.add_argument("--gate-id", default="")
    parser.add_argument("--expected-head", default="")
    parser.add_argument("--expected-base", default="")
    parser.add_argument("--expected-merge-base", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--model", default=os.environ.get("CLAUDE_REVIEW_MODEL", ""))
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default=os.environ.get("CLAUDE_REVIEW_EFFORT", "medium"),
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=os.environ.get("CLAUDE_REVIEW_MAX_BUDGET_USD"),
    )
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"))
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", "codex"))
    parser.add_argument(
        "--codex-model", default=os.environ.get("CODEX_REVIEW_MODEL", "")
    )
    return parser


def _resolve_args(args: argparse.Namespace) -> None:
    required = {
        "--prompt": args.prompt,
        "--schema": args.schema,
        "--output": args.output,
        "--error-file": args.error_file,
        "--provider-state": args.provider_state,
        "--metadata-output": args.metadata_output,
        "--trace-output": args.trace_output,
        "--gate-id": args.gate_id,
        "--expected-head": args.expected_head,
        "--expected-base": args.expected_base,
        "--expected-merge-base": args.expected_merge_base,
    }
    missing = [
        name
        for name, value in required.items()
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        raise WrapperError(f"required arguments are missing: {', '.join(missing)}")
    args.prompt = _resolve_destination(args.prompt)
    args.schema = _resolve_destination(args.schema)
    args.cwd = args.cwd.resolve(strict=True)
    args.context_dir = [_resolve_destination(path) for path in args.context_dir]
    args.output = _resolve_destination(args.output)
    args.error_file = _resolve_destination(args.error_file)
    args.provider_state = _resolve_destination(args.provider_state)
    args.metadata_output = _resolve_destination(args.metadata_output)
    args.trace_output = _resolve_destination(args.trace_output)
    if not args.cwd.is_dir():
        raise WrapperError(f"cwd is not a directory: {args.cwd}")
    for path in args.context_dir:
        context_stat = path.lstat()
        if stat.S_ISLNK(context_stat.st_mode) or not stat.S_ISDIR(context_stat.st_mode):
            raise WrapperError(f"Every --context-dir must be a real directory: {path}")
    for index, left in enumerate(args.context_dir):
        for right in args.context_dir[index + 1 :]:
            if _is_beneath(left, right) or _is_beneath(right, left):
                raise WrapperError("--context-dir roots must not overlap")
    if args.prompt.lstat().st_size > claude_adapter.MAX_PROMPT_BYTES:
        raise WrapperError("review prompt exceeds the adapter size limit")
    if args.timeout < 1:
        raise WrapperError("--timeout must be positive")
    if args.max_budget_usd is not None and args.max_budget_usd <= 0:
        raise WrapperError("--max-budget-usd must be positive")
    if GATE_ID_RE.fullmatch(args.gate_id) is None:
        raise WrapperError(
            "--gate-id must be 1-128 characters using letters, digits, . _ : or -"
        )
    try:
        schema = json.loads(args.schema.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WrapperError(f"schema is invalid JSON: {exc}") from exc
    if not isinstance(schema, dict):
        raise WrapperError("schema root must be an object")
    try:
        claude_adapter._validate_provider_schema(schema)
    except claude_adapter.ReviewError as exc:
        raise WrapperError(str(exc)) from exc
    for name, value in (
        ("--expected-head", args.expected_head),
        ("--expected-base", args.expected_base),
        ("--expected-merge-base", args.expected_merge_base),
    ):
        if re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise WrapperError(f"{name} must be a full 40-character hexadecimal SHA")
    output_paths = {
        args.output,
        args.error_file,
        args.provider_state,
        args.metadata_output,
        args.trace_output,
    }
    if len(output_paths) != 5:
        raise WrapperError(
            "output, diagnostics, state, metadata, and trace paths must be distinct"
        )
    if args.prompt in output_paths or args.schema in output_paths:
        raise WrapperError(
            "output paths must not overwrite the prompt or review schema"
        )
    for context in args.context_dir:
        if _is_beneath(args.prompt, context) or _is_beneath(args.schema, context):
            raise WrapperError(
                "prompt and schema must be outside reviewer context directories"
            )
        if any(_is_beneath(path, context) for path in output_paths):
            raise WrapperError(
                "private review artifacts and provider state must be outside "
                "reviewer context directories"
            )


def _frozen_review_args(
    args: argparse.Namespace, frozen: FrozenInputs
) -> argparse.Namespace:
    reviewed = argparse.Namespace(**vars(args))
    reviewed.prompt = frozen.prompt
    reviewed.schema = frozen.schema
    reviewed.context_dir = list(frozen.context_dirs)
    reviewed.frozen_inputs_sha256 = frozen.tree_fingerprint
    reviewed.frozen_context_hashes = list(frozen.context_hashes)
    return reviewed


def _validate_scratch_roots(
    frozen_root: Path, attempt_root: Path, context_dirs: list[Path]
) -> None:
    if _is_beneath(frozen_root, attempt_root) or _is_beneath(attempt_root, frozen_root):
        raise SnapshotError(
            "frozen inputs and provider attempt scratch must be separate"
        )
    for context in context_dirs:
        if (
            _is_beneath(frozen_root, context)
            or _is_beneath(context, frozen_root)
            or _is_beneath(attempt_root, context)
            or _is_beneath(context, attempt_root)
        ):
            raise SnapshotError(
                f"system scratch directory is nested under a reviewer context: {context}"
            )


@contextmanager
def _provider_scratch(
    prefix: str, frozen_root: Path, context_dirs: list[Path]
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=prefix) as temporary_name:
        root = Path(temporary_name).resolve()
        os.chmod(root, 0o700)
        _validate_scratch_roots(frozen_root, root, context_dirs)
        yield root


def _assert_review_snapshot(
    args: argparse.Namespace,
    git_snapshot: GitSnapshot,
    frozen_root: Path,
    frozen: FrozenInputs,
) -> None:
    _assert_git_snapshot(args, git_snapshot)
    _assert_frozen_inputs(frozen_root, frozen)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.check:
        return _check(args.claude_bin, args.codex_bin)
    if args.self_test:
        return _self_test()
    try:
        _resolve_args(args)
    except (OSError, WrapperError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        _preflight_private_artifact_paths(args)
    except (OSError, SnapshotError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    attempts: list[Attempt] = []
    classification: str | None = None
    selected_from_state = False
    try:
        with _locked_provider_state(args.provider_state) as provider_state:
            selected_from_state = provider_state["active_provider"] == "codex"
            if selected_from_state:
                classification = provider_state["quota_classification"]
            with tempfile.TemporaryDirectory(prefix="gh-review-frozen-") as frozen_name:
                frozen_root = Path(frozen_name).resolve()
                os.chmod(frozen_root, 0o700)
                frozen: FrozenInputs | None = None
                try:
                    frozen = _freeze_inputs(args, frozen_root)
                    review_args = _frozen_review_args(args, frozen)
                    try:
                        git_snapshot = _capture_git_snapshot(args)
                    except SnapshotError as exc:
                        provider = "codex_fallback" if selected_from_state else "claude"
                        return _finish(
                            args=review_args,
                            review=_inconclusive(review_args, str(exc)),
                            exit_code=SNAPSHOT_MISMATCH_EXIT,
                            provider=provider,
                            provider_state=provider_state,
                            attempts=attempts,
                            classification=classification,
                            selected_from_state=selected_from_state,
                        )
                    try:
                        _validate_private_artifact_paths(args, git_snapshot)
                    except SnapshotError as exc:
                        print(f"error: {exc}", file=sys.stderr)
                        return 2

                    def reject_snapshot_change(
                        exc: SnapshotError, provider: str
                    ) -> int:
                        return _finish(
                            args=review_args,
                            review=_inconclusive(review_args, str(exc)),
                            exit_code=SNAPSHOT_MISMATCH_EXIT,
                            provider=provider,
                            provider_state=provider_state,
                            attempts=attempts,
                            classification=classification,
                            selected_from_state=selected_from_state,
                        )

                    if provider_state["active_provider"] == "claude":
                        with _provider_scratch(
                            "gh-review-claude-", frozen_root, args.context_dir
                        ) as claude_scratch:
                            claude_attempt = _run_claude(
                                review_args,
                                claude_scratch / "claude-raw.json",
                                claude_scratch / "claude-stderr.log",
                                claude_scratch / "claude-review.json",
                            )
                        attempts.append(claude_attempt)
                        try:
                            _assert_review_snapshot(
                                args, git_snapshot, frozen_root, frozen
                            )
                        except SnapshotError as exc:
                            return reject_snapshot_change(exc, "claude")
                        fallback_trigger: str | None = None
                        diagnostic_hash: str | None = None
                        if claude_attempt.exit_code in VALID_REVIEW_EXITS:
                            try:
                                review = _validated_review(
                                    claude_attempt.result, review_args
                                )
                            except claude_adapter.ReviewError as exc:
                                fallback_trigger = "CLAUDE_REVIEW_INVALID"
                                diagnostic_hash = _sha256(
                                    claude_attempt.stdout
                                    + b"\x00"
                                    + claude_attempt.stderr
                                    + b"\x00"
                                    + str(exc).encode("utf-8", errors="replace")
                                )
                            else:
                                try:
                                    _assert_review_snapshot(
                                        args, git_snapshot, frozen_root, frozen
                                    )
                                except SnapshotError as exc:
                                    return reject_snapshot_change(exc, "claude")
                                if review["verdict"] != "INCONCLUSIVE":
                                    return _finish(
                                        args=review_args,
                                        review=review,
                                        exit_code=claude_adapter.VERDICT_EXIT_CODES[
                                            review["verdict"]
                                        ],
                                        provider="claude",
                                        provider_state=provider_state,
                                        attempts=attempts,
                                        classification=None,
                                        selected_from_state=False,
                                    )
                                fallback_trigger = "CLAUDE_REVIEW_INCONCLUSIVE"
                                diagnostic_hash = _sha256(
                                    claude_attempt.stdout
                                    + b"\x00"
                                    + claude_attempt.stderr
                                )
                        else:
                            classification, diagnostic_hash = _classify_quota(
                                claude_attempt.exit_code,
                                claude_attempt.stdout,
                                claude_attempt.stderr,
                            )
                            fallback_trigger = (
                                "CLAUDE_QUOTA_EXHAUSTED"
                                if classification is not None
                                else "CLAUDE_REVIEW_FAILED"
                            )
                        try:
                            _assert_review_snapshot(
                                args, git_snapshot, frozen_root, frozen
                            )
                        except SnapshotError as exc:
                            return reject_snapshot_change(exc, "claude")
                        if fallback_trigger is None or diagnostic_hash is None:
                            raise WrapperError(
                                "Claude fallback decision lacked durable provenance"
                            )
                        provider_state.update(
                            {
                                "active_provider": "codex",
                                "fallback_trigger": fallback_trigger,
                                "quota_classification": classification,
                                "claude_diagnostic_sha256": diagnostic_hash,
                            }
                        )
                        _write_private_json(args.provider_state, provider_state)

                    prior_codex_attempt = provider_state["codex_attempted_gates"].get(
                        args.gate_id
                    )
                    if prior_codex_attempt is not None:
                        review = _inconclusive(
                            review_args,
                            "Codex fallback was already consumed for gate "
                            f"{args.gate_id!r}; refusing a retry",
                        )
                        return _finish(
                            args=review_args,
                            review=review,
                            exit_code=FALLBACK_FAILED_EXIT,
                            provider="codex_fallback",
                            provider_state=provider_state,
                            attempts=attempts,
                            classification=classification,
                            selected_from_state=selected_from_state,
                        )
                    provider_state["codex_attempted_gates"][args.gate_id] = (
                        secrets.token_hex(16)
                    )
                    _write_private_json(args.provider_state, provider_state)

                    reason = "Codex fallback did not complete"
                    with _provider_scratch(
                        "gh-review-codex-", frozen_root, args.context_dir
                    ) as codex_scratch:
                        codex_prompt = review_args.prompt
                        for generation in range(MAX_CODEX_GENERATIONS_PER_GATE):
                            codex_attempt = _run_codex(
                                binary_name=args.codex_bin,
                                cwd=args.cwd,
                                prompt=codex_prompt,
                                schema=review_args.schema,
                                result_path=(
                                    codex_scratch
                                    / f"codex-review-{generation + 1}.json"
                                ),
                                model=args.codex_model,
                                timeout=args.timeout,
                            )
                            attempts.append(codex_attempt)
                            try:
                                _assert_review_snapshot(
                                    args, git_snapshot, frozen_root, frozen
                                )
                            except SnapshotError as exc:
                                return reject_snapshot_change(exc, "codex_fallback")
                            if codex_attempt.timed_out:
                                reason = (
                                    f"Codex fallback timed out after {args.timeout}s"
                                )
                                break
                            if codex_attempt.exit_code != 0:
                                reason = (
                                    "Codex fallback exited with status "
                                    f"{codex_attempt.exit_code}"
                                )
                                break
                            if not codex_attempt.result:
                                reason = "Codex fallback returned no final review"
                                break
                            try:
                                review = _validated_review(
                                    codex_attempt.result, review_args
                                )
                            except claude_adapter.ReviewError as exc:
                                reason = f"Codex fallback output was invalid: {exc}"
                                if generation + 1 >= MAX_CODEX_GENERATIONS_PER_GATE:
                                    break
                                codex_prompt = _write_codex_repair_prompt(
                                    review_args.prompt,
                                    codex_scratch / "codex-repair-prompt.txt",
                                    str(exc),
                                )
                                continue
                            try:
                                _assert_review_snapshot(
                                    args, git_snapshot, frozen_root, frozen
                                )
                            except SnapshotError as exc:
                                return reject_snapshot_change(exc, "codex_fallback")
                            return _finish(
                                args=review_args,
                                review=review,
                                exit_code=claude_adapter.VERDICT_EXIT_CODES[
                                    review["verdict"]
                                ],
                                provider="codex_fallback",
                                provider_state=provider_state,
                                attempts=attempts,
                                classification=classification,
                                selected_from_state=selected_from_state,
                            )
                    review = _inconclusive(
                        review_args,
                        f"{reason} after Claude did not produce a usable review verdict",
                    )
                    return _finish(
                        args=review_args,
                        review=review,
                        exit_code=FALLBACK_FAILED_EXIT,
                        provider="codex_fallback",
                        provider_state=provider_state,
                        attempts=attempts,
                        classification=classification,
                        selected_from_state=selected_from_state,
                    )
                finally:
                    if frozen is not None and frozen_root.exists():
                        _set_frozen_modes(frozen_root, read_only=False)
    except (OSError, WrapperError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
