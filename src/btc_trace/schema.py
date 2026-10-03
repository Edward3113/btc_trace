"""Check trace reports against the published JSON Schema.

The schema (``schemas/trace-report.schema.json``) is the contract for the report
format: the HTML page and anyone else reading a report can rely on it. Tests check
that every report the tracer produces matches it, so the code and the schema cannot
drift apart unnoticed.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

REPORT_VERSION = "1.0"
SCHEMA_RESOURCE = "schemas/trace-report.schema.json"


@cache
def load_schema() -> dict[str, Any]:
    return json.loads(files("btc_trace").joinpath(SCHEMA_RESOURCE).read_text())


@cache
def _validator() -> Draft202012Validator:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _where(path: Any) -> str:
    parts = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)
    return "report" + parts


def validate_report(report: Any, limit: int = 10) -> list[str]:
    """Readable problems with a report; an empty list means it is valid.

    Reports from before versioning get one clear message instead of a list of
    missing fields.
    """
    if isinstance(report, dict) and "report_version" not in report:
        return [
            "report has no report_version: it was made by an older btc_trace. "
            "Run `btc-trace trace` again to produce a current report."
        ]
    if isinstance(report, dict) and report.get("report_version") != REPORT_VERSION:
        return [
            f"report_version {report.get('report_version')!r} is not supported; "
            f"this btc_trace reads version {REPORT_VERSION}."
        ]
    errors = sorted(_validator().iter_errors(report), key=lambda e: list(e.absolute_path))
    problems = [f"{_where(e.absolute_path)}: {e.message}" for e in errors[:limit]]
    if len(errors) > limit:
        problems.append(f"... and {len(errors) - limit} more")
    return problems
