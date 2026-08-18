#!/usr/bin/env python3
"""Run Claude Code as a bounded, read-only reviewer and normalize its result."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any


VERDICTS = {"APPROVED", "CHANGES_REQUESTED", "BLOCKED", "INCONCLUSIVE"}
VERDICT_EXIT_CODES = {
    "APPROVED": 0,
    "CHANGES_REQUESTED": 10,
    "BLOCKED": 11,
    "INCONCLUSIVE": 12,
}
SEVERITIES = {"P1", "P2", "P3"}
CATEGORIES = {"CORRECTNESS", "AC", "SECURITY", "TEST"}
AC_STATUSES = {"COVERED", "PARTIAL", "MISSING", "NA"}
AC_SEVERITIES = {"NONE", "P1", "P2", "P3"}
SCOPES = {"DIFF", "ADJACENT"}
SECURITY_CATEGORIES = {
    "INJECTION",
    "XSS_REDIRECTS",
    "AUTHORIZATION_TENANCY",
    "SECRETS_LOGGING",
    "VALIDATION_PATHS_UPLOADS",
    "SSRF_URL_FETCHING",
    "DESERIALIZATION_XXE_PROTOTYPES",
    "CSRF_SESSIONS_TOKENS",
    "CRYPTO_RANDOMNESS_PASSWORDS",
    "RACES_TOCTOU",
    "DEPENDENCIES_LOCKFILES",
}
MAX_PROMPT_BYTES = 9_500_000
TOP_LEVEL_FIELDS = (
    "schema_version",
    "verdict",
    "inconclusive_reason",
    "reviewed_head_sha",
    "reviewed_base_sha",
    "reviewed_merge_base_sha",
    "summary",
    "findings",
    "acceptance_criteria",
    "acceptance_criteria_sources",
    "no_explicit_criteria_reason",
    "security_categories_checked",
    "security_summary",
)
FINDING_FIELDS = {
    "id",
    "category",
    "severity",
    "title",
    "failure_mode",
    "file",
    "line",
    "evidence",
    "minimal_fix",
    "scope",
    "adjacent_justification",
    "broad_or_risky_fix",
    "remediation",
}
CRITERION_FIELDS = {
    "id",
    "criterion",
    "source",
    "explicit",
    "status",
    "evidence",
    "severity",
    "finding_id",
}


class ReviewError(RuntimeError):
    """Raised when Claude output cannot be trusted for gating."""


_SCHEMA_SINGLE_CHILD_KEYWORDS = {
    "additionalItems",
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
}
_SCHEMA_ARRAY_CHILD_KEYWORDS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_MAP_CHILD_KEYWORDS = {
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
}


def _schema_pointer(parts: tuple[str, ...]) -> str:
    if not parts:
        return "#"
    escaped = (part.replace("~", "~0").replace("/", "~1") for part in parts)
    return "#/" + "/".join(escaped)


def _validate_provider_schema(schema: dict[str, Any]) -> None:
    """Reject JSON Schema features unsupported by either review provider."""

    if "$schema" in schema:
        raise ReviewError(
            "Review schema uses unsupported root keyword '$schema' at #/$schema"
        )

    def walk(node: Any, parts: tuple[str, ...]) -> None:
        if not isinstance(node, dict):
            return
        location = _schema_pointer(parts)
        if "uniqueItems" in node:
            raise ReviewError(
                "Review schema uses unsupported keyword 'uniqueItems' at "
                f"{location}/uniqueItems"
            )
        if "const" in node and "type" not in node:
            raise ReviewError(
                f"Review schema const at {location}/const requires an explicit type"
            )

        for keyword in sorted(_SCHEMA_SINGLE_CHILD_KEYWORDS):
            child = node.get(keyword)
            if isinstance(child, dict):
                walk(child, (*parts, keyword))
            elif keyword == "items" and isinstance(child, list):
                for index, item in enumerate(child):
                    walk(item, (*parts, keyword, str(index)))
        for keyword in sorted(_SCHEMA_ARRAY_CHILD_KEYWORDS):
            children = node.get(keyword)
            if isinstance(children, list):
                for index, child in enumerate(children):
                    walk(child, (*parts, keyword, str(index)))
        for keyword in sorted(_SCHEMA_MAP_CHILD_KEYWORDS):
            children = node.get(keyword)
            if isinstance(children, dict):
                for name, child in sorted(children.items()):
                    walk(child, (*parts, keyword, name))
        dependencies = node.get("dependencies")
        if isinstance(dependencies, dict):
            for name, child in sorted(dependencies.items()):
                if isinstance(child, dict):
                    walk(child, (*parts, "dependencies", name))

    walk(schema, ())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_bytes(path: Path | None, value: bytes) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _inconclusive(
    reason: str, head: str = "", base: str = "", merge_base: str = ""
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "verdict": "INCONCLUSIVE",
        "inconclusive_reason": reason,
        "reviewed_head_sha": head,
        "reviewed_base_sha": base,
        "reviewed_merge_base_sha": merge_base,
        "summary": reason,
        "findings": [],
        "acceptance_criteria": [],
        "acceptance_criteria_sources": [],
        "no_explicit_criteria_reason": None,
        "security_categories_checked": [],
        "security_summary": "Security review did not complete reliably.",
    }


def _decode_json_document(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise ReviewError("Claude returned empty stdout")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewError("Claude stdout was not exactly one JSON document") from exc


def _extract_structured_output(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReviewError("Claude JSON envelope is not an object")

    if payload.get("type") == "result":
        if payload.get("is_error") is not False or payload.get("subtype") != "success":
            raise ReviewError(
                f"Claude result reported failure: {payload.get('subtype') or 'unknown'}"
            )
        structured = payload.get("structured_output")
        if isinstance(structured, dict):
            return structured
        raise ReviewError("Claude result omitted structured_output")
    raise ReviewError("Unrecognized Claude JSON envelope")


def _require_string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ReviewError(f"{field} must be a non-empty string")
    return value


def _validate_review(
    review: dict[str, Any],
    expected_head: str = "",
    expected_base: str = "",
    expected_merge_base: str = "",
) -> dict[str, Any]:
    required = set(TOP_LEVEL_FIELDS)
    missing = sorted(required - review.keys())
    if missing:
        raise ReviewError(f"Structured review is missing fields: {', '.join(missing)}")
    unexpected = sorted(review.keys() - required)
    if unexpected:
        raise ReviewError(f"Structured review has unexpected fields: {', '.join(unexpected)}")

    if (
        not isinstance(review["schema_version"], int)
        or isinstance(review["schema_version"], bool)
        or review["schema_version"] != 1
    ):
        raise ReviewError("schema_version must be 1")
    verdict = _require_string(review["verdict"], "verdict")
    if verdict not in VERDICTS:
        raise ReviewError(f"Unknown verdict: {verdict}")

    head = _require_string(review["reviewed_head_sha"], "reviewed_head_sha")
    base = _require_string(review["reviewed_base_sha"], "reviewed_base_sha")
    merge_base = _require_string(
        review["reviewed_merge_base_sha"], "reviewed_merge_base_sha"
    )
    for field, value in (
        ("reviewed_head_sha", head),
        ("reviewed_base_sha", base),
        ("reviewed_merge_base_sha", merge_base),
    ):
        if value and re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
            raise ReviewError(f"{field} must be a full 40-character hexadecimal SHA")
    if expected_head and head.lower() != expected_head.lower():
        raise ReviewError(f"Reviewed head SHA mismatch: expected {expected_head}, got {head or '<empty>'}")
    if expected_base and base.lower() != expected_base.lower():
        raise ReviewError(f"Reviewed base SHA mismatch: expected {expected_base}, got {base or '<empty>'}")
    if expected_merge_base and merge_base.lower() != expected_merge_base.lower():
        raise ReviewError(
            "Reviewed merge-base SHA mismatch: "
            f"expected {expected_merge_base}, got {merge_base or '<empty>'}"
        )

    inconclusive_reason = review["inconclusive_reason"]
    if inconclusive_reason is not None and not isinstance(inconclusive_reason, str):
        raise ReviewError("inconclusive_reason must be a string or null")
    if verdict == "INCONCLUSIVE" and not (inconclusive_reason or "").strip():
        raise ReviewError("INCONCLUSIVE verdict requires inconclusive_reason")
    if verdict != "INCONCLUSIVE" and inconclusive_reason is not None:
        raise ReviewError(f"{verdict} verdict requires a null inconclusive_reason")

    _require_string(review["summary"], "summary")
    _require_string(review["security_summary"], "security_summary")

    findings = review["findings"]
    if not isinstance(findings, list):
        raise ReviewError("findings must be an array")
    blocking_count = 0
    broad_blocking_count = 0
    seen_ids: set[str] = set()
    findings_by_id: dict[str, dict[str, Any]] = {}
    for index, finding in enumerate(findings):
        prefix = f"findings[{index}]"
        if not isinstance(finding, dict):
            raise ReviewError(f"{prefix} must be an object")
        missing_finding = sorted(FINDING_FIELDS - finding.keys())
        unexpected_finding = sorted(finding.keys() - FINDING_FIELDS)
        if missing_finding or unexpected_finding:
            raise ReviewError(
                f"{prefix} fields mismatch; missing={missing_finding}, "
                f"unexpected={unexpected_finding}"
            )
        finding_id = _require_string(finding.get("id"), f"{prefix}.id")
        if finding_id in seen_ids:
            raise ReviewError(f"Duplicate finding id: {finding_id}")
        seen_ids.add(finding_id)
        findings_by_id[finding_id] = finding
        category = _require_string(finding.get("category"), f"{prefix}.category")
        severity = _require_string(finding.get("severity"), f"{prefix}.severity")
        if category not in CATEGORIES:
            raise ReviewError(f"{prefix}.category is invalid: {category}")
        if severity not in SEVERITIES:
            raise ReviewError(f"{prefix}.severity is invalid: {severity}")
        for field in ("title", "failure_mode", "evidence", "minimal_fix"):
            _require_string(finding.get(field), f"{prefix}.{field}")
        file_value = finding.get("file")
        if file_value is not None and not isinstance(file_value, str):
            raise ReviewError(f"{prefix}.file must be a string or null")
        line_value = finding.get("line")
        if line_value is not None and (
            not isinstance(line_value, int) or isinstance(line_value, bool) or line_value < 1
        ):
            raise ReviewError(f"{prefix}.line must be a positive integer or null")
        if isinstance(file_value, str):
            normalized = PurePosixPath(file_value)
            if (
                not file_value.strip()
                or normalized.is_absolute()
                or ".." in normalized.parts
                or str(normalized) in {"", "."}
                or str(normalized) != file_value
                or "\\" in file_value
            ):
                raise ReviewError(f"{prefix}.file must be a normalized repo-relative path")
        scope = _require_string(finding.get("scope"), f"{prefix}.scope")
        if scope not in SCOPES:
            raise ReviewError(f"{prefix}.scope is invalid: {scope}")
        adjacent_justification = finding.get("adjacent_justification")
        if adjacent_justification is not None and not isinstance(adjacent_justification, str):
            raise ReviewError(f"{prefix}.adjacent_justification must be a string or null")
        if scope == "ADJACENT" and not (adjacent_justification or "").strip():
            raise ReviewError(f"{prefix} requires adjacent_justification")
        if (
            scope == "ADJACENT"
            and severity in {"P1", "P2"}
            and category not in {"AC", "SECURITY"}
        ):
            raise ReviewError(
                f"{prefix} only blocking AC/security findings may compel adjacent-file edits"
            )
        if scope == "DIFF" and adjacent_justification is not None:
            raise ReviewError(f"{prefix} DIFF scope requires null adjacent_justification")
        broad_or_risky = finding.get("broad_or_risky_fix")
        if not isinstance(broad_or_risky, bool):
            raise ReviewError(f"{prefix}.broad_or_risky_fix must be boolean")
        remediation = finding.get("remediation")
        if remediation is not None and not isinstance(remediation, str):
            raise ReviewError(f"{prefix}.remediation must be a string or null")
        if broad_or_risky:
            if category not in {"AC", "SECURITY"} or severity not in {"P1", "P2"}:
                raise ReviewError(
                    f"{prefix} broad/risky fixes are only valid for blocking AC/security findings"
                )
            if not (remediation or "").strip():
                raise ReviewError(f"{prefix} broad/risky fix requires remediation")
            broad_blocking_count += 1
        elif remediation is not None:
            raise ReviewError(f"{prefix} non-broad finding requires null remediation")
        if severity in {"P1", "P2"}:
            blocking_count += 1

    criteria = review["acceptance_criteria"]
    if not isinstance(criteria, list):
        raise ReviewError("acceptance_criteria must be an array")
    seen_criterion_ids: set[str] = set()
    for index, criterion in enumerate(criteria):
        prefix = f"acceptance_criteria[{index}]"
        if not isinstance(criterion, dict):
            raise ReviewError(f"{prefix} must be an object")
        missing_criterion = sorted(CRITERION_FIELDS - criterion.keys())
        unexpected_criterion = sorted(criterion.keys() - CRITERION_FIELDS)
        if missing_criterion or unexpected_criterion:
            raise ReviewError(
                f"{prefix} fields mismatch; missing={missing_criterion}, "
                f"unexpected={unexpected_criterion}"
            )
        for field in ("id", "criterion", "source", "evidence"):
            _require_string(criterion.get(field), f"{prefix}.{field}")
        criterion_id = criterion["id"]
        if criterion_id in seen_criterion_ids:
            raise ReviewError(f"Duplicate acceptance criterion id: {criterion_id}")
        seen_criterion_ids.add(criterion_id)
        explicit = criterion.get("explicit")
        if not isinstance(explicit, bool):
            raise ReviewError(f"{prefix}.explicit must be boolean")
        status = _require_string(criterion.get("status"), f"{prefix}.status")
        severity = _require_string(criterion.get("severity"), f"{prefix}.severity")
        if status not in AC_STATUSES:
            raise ReviewError(f"{prefix}.status is invalid: {status}")
        if severity not in AC_SEVERITIES:
            raise ReviewError(f"{prefix}.severity is invalid: {severity}")
        finding_id = criterion.get("finding_id")
        if finding_id is not None and not isinstance(finding_id, str):
            raise ReviewError(f"{prefix}.finding_id must be a string or null")
        if finding_id is not None:
            linked = findings_by_id.get(finding_id)
            if linked is None or linked.get("category") != "AC":
                raise ReviewError(f"{prefix}.finding_id must reference an AC finding")
            if linked.get("severity") != severity:
                raise ReviewError(f"{prefix}.severity must match its linked AC finding")
        if status in {"COVERED", "NA"} and (severity != "NONE" or finding_id is not None):
            raise ReviewError(
                f"{prefix} covered/not-applicable criterion requires NONE severity and no finding"
            )
        if status in {"PARTIAL", "MISSING"} and severity == "NONE":
            raise ReviewError(f"{prefix} partial/missing criterion requires a severity")
        if status in {"PARTIAL", "MISSING"} and severity in {"P1", "P2"} and finding_id is None:
            raise ReviewError(f"{prefix} blocking criterion requires a linked AC finding")
        if explicit and status in {"PARTIAL", "MISSING"}:
            if severity not in {"P1", "P2"} or finding_id is None:
                raise ReviewError(
                    f"{prefix} explicit partial/missing criterion must reference a blocking AC finding"
                )
        if status in {"PARTIAL", "MISSING"} and severity in {"P1", "P2"}:
            blocking_count += 1

    sources = review["acceptance_criteria_sources"]
    if not isinstance(sources, list) or any(
        not isinstance(source, str) or not source.strip() for source in sources
    ):
        raise ReviewError("acceptance_criteria_sources must be an array of non-empty strings")
    if verdict != "INCONCLUSIVE" and not sources:
        raise ReviewError("A conclusive review requires at least one AC source")
    no_explicit_reason = review["no_explicit_criteria_reason"]
    if no_explicit_reason is not None and not isinstance(no_explicit_reason, str):
        raise ReviewError("no_explicit_criteria_reason must be a string or null")
    has_explicit = any(criterion.get("explicit") is True for criterion in criteria)
    if not has_explicit and verdict != "INCONCLUSIVE" and not (no_explicit_reason or "").strip():
        raise ReviewError("No explicit criteria requires no_explicit_criteria_reason")

    security_categories = review["security_categories_checked"]
    if not isinstance(security_categories, list) or any(
        not isinstance(category, str) for category in security_categories
    ):
        raise ReviewError("security_categories_checked must be an array of strings")
    if len(set(security_categories)) != len(security_categories):
        raise ReviewError("security_categories_checked contains duplicates")
    if verdict != "INCONCLUSIVE" and set(security_categories) != SECURITY_CATEGORIES:
        missing_security = sorted(SECURITY_CATEGORIES - set(security_categories))
        unknown_security = sorted(set(security_categories) - SECURITY_CATEGORIES)
        raise ReviewError(
            "security_categories_checked mismatch; "
            f"missing={missing_security}, unknown={unknown_security}"
        )

    if verdict == "APPROVED" and blocking_count:
        raise ReviewError("APPROVED verdict contains blocking P1/P2 items")
    if verdict == "CHANGES_REQUESTED" and (not blocking_count or broad_blocking_count):
        raise ReviewError("CHANGES_REQUESTED requires blockers and no broad/risky AC/security fix")
    if verdict == "BLOCKED" and not broad_blocking_count:
        raise ReviewError("BLOCKED requires a broad/risky blocking AC/security finding")
    if verdict == "APPROVED" and broad_blocking_count:
        raise ReviewError("APPROVED verdict contains a broad/risky blocker")

    # Return only schema fields so metadata in a future CLI envelope cannot
    # silently become part of the durable gate record.
    return {key: review[key] for key in TOP_LEVEL_FIELDS}


def _terminate_process_group(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()


def _check_claude(binary_name: str) -> int:
    binary = shutil.which(binary_name)
    if binary is None:
        print(json.dumps({"ok": False, "error": f"{binary_name} not found on PATH"}))
        return 2
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False, timeout=15
        )
        # Claude Code 2.1.216 can truncate help output when stdout is a pipe,
        # making capability checks fail nondeterministically. A regular file
        # forces the CLI to flush its complete help text before exit.
        with (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as help_stdout,
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as help_stderr,
        ):
            help_result = subprocess.run(
                [binary, "-p", "--help"],
                stdout=help_stdout,
                stderr=help_stderr,
                text=True,
                check=False,
                timeout=15,
            )
            help_stdout.seek(0)
            help_stderr.seek(0)
            help_text = help_stdout.read() + help_stderr.read()
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"ok": False, "binary": binary, "error": str(exc)}, indent=2))
        return 2
    required_flags = {
        "--safe-mode",
        "--permission-mode",
        "--tools",
        "--no-session-persistence",
        "--output-format",
        "--json-schema",
    }
    missing_flags = sorted(flag for flag in required_flags if flag not in help_text)
    ok = version.returncode == 0 and help_result.returncode == 0 and not missing_flags
    print(
        json.dumps(
            {
                "ok": ok,
                "binary": binary,
                "version": version.stdout.strip() or version.stderr.strip(),
                "missing_flags": missing_flags,
            },
            indent=2,
        )
    )
    return 0 if ok else 2


def _self_test() -> int:
    compatible_schema = {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            # Property names are data, not schema keywords.
            "uniqueItems": {"type": "boolean"},
        },
    }
    _validate_provider_schema(compatible_schema)
    incompatible_schemas = (
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"},
        {
            "type": "object",
            "properties": {
                "values": {"type": "array", "uniqueItems": True},
            },
        },
        {"type": "object", "allOf": [{"const": 1}]},
    )
    for incompatible_schema in incompatible_schemas:
        try:
            _validate_provider_schema(incompatible_schema)
        except ReviewError:
            pass
        else:
            raise AssertionError(
                "provider schema preflight accepted an incompatible fixture"
            )

    sample = {
        "schema_version": 1,
        "verdict": "APPROVED",
        "inconclusive_reason": None,
        "reviewed_head_sha": "a" * 40,
        "reviewed_base_sha": "b" * 40,
        "reviewed_merge_base_sha": "c" * 40,
        "summary": "All gates passed.",
        "findings": [],
        "acceptance_criteria": [
            {
                "id": "ac-1",
                "criterion": "The endpoint rejects unauthenticated requests.",
                "source": "Issue #1",
                "explicit": True,
                "status": "COVERED",
                "evidence": "tests/test_auth.py:20",
                "severity": "NONE",
                "finding_id": None,
            }
        ],
        "acceptance_criteria_sources": ["Issue #1"],
        "no_explicit_criteria_reason": None,
        "security_categories_checked": sorted(SECURITY_CATEGORIES),
        "security_summary": "No blocking security issue found.",
    }
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": sample,
    }
    decoded = _decode_json_document(json.dumps(envelope).encode())
    structured = _extract_structured_output(decoded)
    validated = _validate_review(structured, "a" * 40, "b" * 40, "c" * 40)
    if validated["verdict"] != "APPROVED":
        raise AssertionError("self-test verdict mismatch")

    invalid = dict(sample)
    invalid["findings"] = [
        {
            "id": "correctness-1",
            "category": "CORRECTNESS",
            "severity": "P2",
            "title": "Broken path",
            "failure_mode": "A common request raises an exception.",
            "file": "app.py",
            "line": 10,
            "evidence": "A common request raises an exception.",
            "minimal_fix": "Handle the missing value.",
            "scope": "DIFF",
            "adjacent_justification": None,
            "broad_or_risky_fix": False,
            "remediation": None,
        }
    ]
    try:
        _validate_review(invalid)
    except ReviewError:
        pass
    else:
        raise AssertionError("self-test accepted APPROVED with a P2 finding")
    print(json.dumps({"ok": True, "tests": 6}))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Claude as a read-only structured reviewer."
    )
    parser.add_argument("--check", action="store_true", help="Check CLI compatibility only")
    parser.add_argument("--self-test", action="store_true", help="Run offline parser tests")
    parser.add_argument("--prompt", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--error-file", type=Path)
    parser.add_argument("--raw-output", type=Path)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--context-dir", action="append", type=Path, default=[])
    parser.add_argument("--expected-head", default="")
    parser.add_argument("--expected-base", default="")
    parser.add_argument("--expected-merge-base", default="")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--model", default=os.environ.get("CLAUDE_REVIEW_MODEL", "")
    )
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default=os.environ.get("CLAUDE_REVIEW_EFFORT", "high"),
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=os.environ.get("CLAUDE_REVIEW_MAX_BUDGET_USD"),
    )
    parser.add_argument(
        "--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude")
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.check:
        return _check_claude(args.claude_bin)
    if args.self_test:
        return _self_test()

    required_args = {
        "--prompt": args.prompt,
        "--schema": args.schema,
        "--output": args.output,
        "--error-file": args.error_file,
        "--expected-head": args.expected_head,
        "--expected-base": args.expected_base,
        "--expected-merge-base": args.expected_merge_base,
    }
    missing_args = [
        name
        for name, value in required_args.items()
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing_args:
        parser.error(f"required arguments are missing: {', '.join(missing_args)}")

    assert args.prompt is not None
    assert args.schema is not None
    assert args.output is not None
    assert args.error_file is not None

    try:
        prompt = args.prompt.resolve(strict=True)
        schema_path = args.schema.resolve(strict=True)
        cwd = args.cwd.resolve(strict=True)
        if not cwd.is_dir():
            raise ReviewError(f"cwd is not a directory: {cwd}")
        if prompt.stat().st_size > MAX_PROMPT_BYTES:
            raise ReviewError(
                f"Prompt is too large ({prompt.stat().st_size} bytes); pass context by file path"
            )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise ReviewError("Schema root must be a JSON object")
        _validate_provider_schema(schema)
        context_dirs = [path.resolve(strict=True) for path in args.context_dir]
        if any(not path.is_dir() for path in context_dirs):
            raise ReviewError("Every --context-dir must be a directory")
        if args.timeout < 1:
            raise ReviewError("--timeout must be positive")
        if args.max_budget_usd is not None and args.max_budget_usd <= 0:
            raise ReviewError("--max-budget-usd must be positive")
        for name, value in (
            ("--expected-head", args.expected_head),
            ("--expected-base", args.expected_base),
            ("--expected-merge-base", args.expected_merge_base),
        ):
            if re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
                raise ReviewError(f"{name} must be a full 40-character hexadecimal SHA")
        binary = shutil.which(args.claude_bin)
        if binary is None:
            raise ReviewError(f"{args.claude_bin} not found on PATH")
    except (OSError, json.JSONDecodeError, ReviewError) as exc:
        _write_json(
            args.output,
            _inconclusive(
                str(exc), args.expected_head, args.expected_base, args.expected_merge_base
            ),
        )
        _write_bytes(args.error_file, (str(exc) + "\n").encode())
        return 2

    command = [
        binary,
        "-p",
        "--safe-mode",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "Read,Grep,Glob",
        "--no-session-persistence",
        "--input-format",
        "text",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--effort",
        args.effort,
    ]
    if args.model:
        command.extend(("--model", args.model))
    if args.max_budget_usd is not None:
        command.extend(("--max-budget-usd", str(args.max_budget_usd)))
    for context_dir in context_dirs:
        command.extend(("--add-dir", str(context_dir)))

    try:
        with prompt.open("rb") as prompt_stream:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=prompt_stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                stdout, stderr = _terminate_process_group(process)
                _write_bytes(args.raw_output, stdout)
                _write_bytes(args.error_file, stderr)
                _write_json(
                    args.output,
                    _inconclusive(
                        f"Claude review timed out after {args.timeout}s",
                        args.expected_head,
                        args.expected_base,
                        args.expected_merge_base,
                    ),
                )
                return 4
    except OSError as exc:
        _write_json(
            args.output,
            _inconclusive(
                str(exc), args.expected_head, args.expected_base, args.expected_merge_base
            ),
        )
        _write_bytes(args.error_file, (str(exc) + "\n").encode())
        return 2

    _write_bytes(args.raw_output, stdout)
    _write_bytes(args.error_file, stderr)
    if process.returncode != 0:
        reason = f"Claude exited with status {process.returncode}"
        _write_json(
            args.output,
            _inconclusive(
                reason, args.expected_head, args.expected_base, args.expected_merge_base
            ),
        )
        return 3

    try:
        payload = _decode_json_document(stdout)
        structured = _extract_structured_output(payload)
        validated = _validate_review(
            structured,
            args.expected_head,
            args.expected_base,
            args.expected_merge_base,
        )
    except ReviewError as exc:
        _write_json(
            args.output,
            _inconclusive(
                str(exc), args.expected_head, args.expected_base, args.expected_merge_base
            ),
        )
        return 5

    _write_json(args.output, validated)
    return VERDICT_EXIT_CODES[validated["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
