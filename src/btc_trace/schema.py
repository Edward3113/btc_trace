"""Check reports against the published JSON Schemas.

Each report kind has a schema that is the contract for its format: the HTML page and
anyone else reading a report can rely on it. Trace reports follow
``schemas/trace-report.schema.json``; exposure reports carry ``"report_kind":
"exposure"`` and follow ``schemas/exposure-report.schema.json``. Tests check that every
report the tool produces matches its schema, so the code and the schemas cannot drift
apart unnoticed.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator

REPORT_VERSION = "1.0"
EXPOSURE_REPORT_VERSION = "1.0"
UTXO_REPORT_VERSION = "1.1"
SCHEMAS = {
    "trace": ("schemas/trace-report.schema.json", REPORT_VERSION),
    "exposure": ("schemas/exposure-report.schema.json", EXPOSURE_REPORT_VERSION),
    "utxo-set": ("schemas/utxo-set-report.schema.json", UTXO_REPORT_VERSION),
}


def report_kind(report: Any) -> str:
    """The report's kind ('exposure', 'utxo-set'); trace reports carry none."""
    if isinstance(report, dict):
        return str(report.get("report_kind", "trace"))
    return "trace"


@cache
def load_schema(kind: str = "trace") -> dict[str, Any]:
    resource, _ = SCHEMAS[kind]
    return json.loads(files("btc_trace").joinpath(resource).read_text())


@cache
def _validator(kind: str) -> Draft202012Validator:
    schema = load_schema(kind)
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
    kind = report_kind(report)
    if kind not in SCHEMAS:
        return [f"report_kind {kind!r} is not one this btc_trace knows"]
    version = SCHEMAS[kind][1]
    if isinstance(report, dict) and "report_version" not in report:
        return [
            "report has no report_version: it was made by an older btc_trace. "
            f"Run `btc-trace {kind}` again to produce a current report."
        ]
    if isinstance(report, dict) and report.get("report_version") != version:
        return [
            f"report_version {report.get('report_version')!r} is not supported; "
            f"this btc_trace reads {kind} reports of version {version}."
        ]
    errors = sorted(_validator(kind).iter_errors(report), key=lambda e: list(e.absolute_path))
    problems = [f"{_where(e.absolute_path)}: {e.message}" for e in errors[:limit]]
    if len(errors) > limit:
        problems.append(f"... and {len(errors) - limit} more")
    return problems
