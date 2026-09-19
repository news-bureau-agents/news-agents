#!/usr/bin/env python3
"""Validate and import the fixed 70-column court export into court_data.

The parser and dry run are local-only.  --apply invokes only the linked Supabase
SQL query command and writes row data solely to short-lived mode-0600 SQL files.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urlsplit

HEADERS = (
    "Case Number", "JP Court ID", "Case Type", "Case File Date",
    "Style Of Case", "Cause of Action", "Claim Amount", "Case Status",
    "Plaintiff Name", "Plaintiff Addr Line 1", "Plaintiff Addr Line 2",
    "Plaintiff Addr City", "Plaintiff Addr State", "Plaintiff Addr Zip",
    "Plaintiff Atty Name", "Plaintiff Atty Addr 1", "Plaintiff Atty Addr 2",
    "Plaintiff Atty City", "Plaintiff Atty State", "Plaintiff Atty Zip",
    "Defendant Name", "Defendant Addr Line 1", "Defendant Addr Line 2",
    "Defendant Addr City", "Defendant Addr State", "Defendant Addr Zip",
    "Defendant Atty Name", "Defendant Atty Addr 1", "Defendant Atty Addr 2",
    "Defendant Atty City", "Defendant Atty State", "Defendant Atty Zip",
    "Second Plaintiff Name", "Second Plaintiff Addr Line 1",
    "Second Plaintiff Addr Line 2", "Second Plaintiff Addr City",
    "Second Plaintiff Addr State", "Second Plaintiff Addr Zip",
    "Second Plaintiff Atty Name", "Second Plaintiff Atty Addr 1",
    "Second Plaintiff Atty Addr 2", "Second Plaintiff Atty City",
    "Second Plaintiff Atty State", "Second Plaintiff Atty Zip",
    "Second Defendant Name", "Second Defendant Addr Line 1",
    "Second Defendant Addr Line 2", "Second Defendant Addr City",
    "Second Defendant Addr State", "Second Defendant Addr Zip",
    "Second Defendant Atty Name", "Second Defendant Atty Addr 1",
    "Second Defendant Atty Addr 2", "Second Defendant Atty City",
    "Second Defendant Atty State", "Second Defendant Atty Zip",
    "Next Hearing Desc", "Next Hearing Date", "Next Hearing Time",
    "Disposition Desc", "Disposition Date", "Judgment Text", "Judgment Date",
    "Judgment In Favor Of", "Judgment Against", "Judgment Amount",
    "Attorney Fees", "Court Costs", "Pre-Judg Int Rate", "Post-Judg Int Rate",
)

PARTY_SLOTS = (
    ("plaintiff", 1, "Plaintiff"),
    ("defendant", 1, "Defendant"),
    ("plaintiff", 2, "Second Plaintiff"),
    ("defendant", 2, "Second Defendant"),
)

DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}\Z")
TIME_RE = re.compile(r"\d{1,2}:\d{2} [AP]M\Z", re.IGNORECASE)
DECIMAL_RE = re.compile(r"[+-]?\d+(?:\.\d+)?\Z")
PROJECT_REF_RE = re.compile(r"[a-z0-9]{20}\Z")


class ImportValidationError(ValueError):
    """A safe-to-display structural error that never includes field values."""


@dataclass(frozen=True)
class ParsedImport:
    import_id: str
    source_filename: str
    file_sha256: str
    header_sha256: str
    raw_headers: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    summary: dict[str, int | str]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    if not isinstance(value, str):
        value = _canonical_json(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(kind: str, *parts: str) -> str:
    return _sha([kind, *parts])


def _required(value: str, field: str, row_number: int) -> str:
    if not value:
        raise ImportValidationError(f"row {row_number}: required field {field} is blank")
    return value


def _date(value: str, field: str, row_number: int, *, required: bool = False) -> str | None:
    if not value:
        if required:
            _required(value, field, row_number)
        return None
    if not DATE_RE.fullmatch(value):
        raise ImportValidationError(f"row {row_number}: invalid date in {field}")
    try:
        parsed = dt.datetime.strptime(value, "%m/%d/%Y").date()
    except ValueError as exc:
        raise ImportValidationError(f"row {row_number}: invalid date in {field}") from exc
    return parsed.isoformat()


def _time(value: str, field: str, row_number: int) -> str | None:
    if not value:
        return None
    if not TIME_RE.fullmatch(value):
        raise ImportValidationError(f"row {row_number}: invalid time in {field}")
    try:
        parsed = dt.datetime.strptime(value.upper(), "%I:%M %p").time()
    except ValueError as exc:
        raise ImportValidationError(f"row {row_number}: invalid time in {field}") from exc
    return parsed.isoformat()


def _decimal(value: str, field: str, row_number: int) -> str | None:
    if not value:
        return None
    if not DECIMAL_RE.fullmatch(value):
        raise ImportValidationError(f"row {row_number}: invalid decimal in {field}")
    try:
        parsed = decimal.Decimal(value)
    except decimal.InvalidOperation as exc:
        raise ImportValidationError(f"row {row_number}: invalid decimal in {field}") from exc
    if not parsed.is_finite():
        raise ImportValidationError(f"row {row_number}: invalid decimal in {field}")
    return format(parsed, "f")


def _address(values: dict[str, str], prefix: str, attorney: bool) -> dict[str, str | None] | None:
    if attorney:
        fields = (f"{prefix} Atty Addr 1", f"{prefix} Atty Addr 2", f"{prefix} Atty City",
                  f"{prefix} Atty State", f"{prefix} Atty Zip")
    else:
        fields = (f"{prefix} Addr Line 1", f"{prefix} Addr Line 2", f"{prefix} Addr City",
                  f"{prefix} Addr State", f"{prefix} Addr Zip")
    parts = [values[field] for field in fields]
    if not any(parts):
        return None
    return dict(zip(("line1", "line2", "city", "state", "postal_code"), (part or None for part in parts)))


def _normalize_row(values: dict[str, str], raw_values: list[str], row_number: int) -> dict[str, Any]:
    court_code = _required(values["JP Court ID"], "JP Court ID", row_number)
    case_number = _required(values["Case Number"], "Case Number", row_number)
    court_id = _stable_id("court", court_code)
    case_id = _stable_id("case", court_code, case_number)

    parties: list[dict[str, Any]] = []
    for role, ordinal, prefix in PARTY_SLOTS:
        name = values[f"{prefix} Name"]
        party_address = _address(values, prefix, False)
        attorney_name = values[f"{prefix} Atty Name"]
        attorney_address = _address(values, prefix, True)
        if not any((name, party_address, attorney_name, attorney_address)):
            continue
        if not name:
            raise ImportValidationError(f"row {row_number}: {prefix} details exist without a name")
        party_id = _stable_id("party", case_id, role, str(ordinal))
        if party_address:
            party_address["id"] = _stable_id("address", "party", party_id)
        party: dict[str, Any] = {
            "id": party_id, "role": role, "ordinal": ordinal, "name": name,
            "address": party_address,
        }
        if attorney_name or attorney_address:
            if not attorney_name:
                raise ImportValidationError(f"row {row_number}: {prefix} attorney details exist without a name")
            attorney_id = _stable_id("attorney", party_id)
            if attorney_address:
                attorney_address["id"] = _stable_id("address", "attorney", attorney_id)
            party["attorney"] = {
                "id": attorney_id,
                "name": attorney_name,
                "address": attorney_address,
            }
        else:
            party["attorney"] = None
        parties.append(party)

    hearing_values = (values["Next Hearing Desc"], values["Next Hearing Date"], values["Next Hearing Time"])
    hearing = None
    if any(hearing_values):
        if not all(hearing_values):
            raise ImportValidationError(f"row {row_number}: incomplete hearing")
        hearing_date = _date(hearing_values[1], "Next Hearing Date", row_number)
        hearing_time = _time(hearing_values[2], "Next Hearing Time", row_number)
        hearing = {
            "description": hearing_values[0], "hearing_date": hearing_date, "hearing_time": hearing_time,
        }
        hearing["id"] = _stable_id("hearing", case_id, hearing["description"], hearing_date or "", hearing_time or "")

    disposition_values = (values["Disposition Desc"], values["Disposition Date"])
    disposition = None
    if any(disposition_values):
        if not all(disposition_values):
            raise ImportValidationError(f"row {row_number}: incomplete disposition")
        disposition_date = _date(disposition_values[1], "Disposition Date", row_number)
        disposition = {"description": disposition_values[0], "disposition_date": disposition_date}
        disposition["id"] = _stable_id("disposition", case_id, disposition["description"], disposition_date or "")

    judgment_fields = (
        "Judgment Text", "Judgment Date", "Judgment In Favor Of", "Judgment Against",
        "Judgment Amount", "Attorney Fees", "Court Costs", "Pre-Judg Int Rate", "Post-Judg Int Rate",
    )
    judgment = None
    if any(values[field] for field in judgment_fields):
        judgment = {
            "judgment_text": values["Judgment Text"] or None,
            "judgment_date": _date(values["Judgment Date"], "Judgment Date", row_number),
            "in_favor_of": values["Judgment In Favor Of"] or None,
            "against": values["Judgment Against"] or None,
            "judgment_amount": _decimal(values["Judgment Amount"], "Judgment Amount", row_number),
            "attorney_fees": _decimal(values["Attorney Fees"], "Attorney Fees", row_number),
            "court_costs": _decimal(values["Court Costs"], "Court Costs", row_number),
            "pre_judgment_interest_rate": _decimal(values["Pre-Judg Int Rate"], "Pre-Judg Int Rate", row_number),
            "post_judgment_interest_rate": _decimal(values["Post-Judg Int Rate"], "Post-Judg Int Rate", row_number),
        }
        judgment["id"] = _stable_id("judgment", case_id, _canonical_json(judgment))

    case = {
        "id": case_id,
        "case_number": case_number,
        "case_type": values["Case Type"] or None,
        "file_date": _date(values["Case File Date"], "Case File Date", row_number, required=True),
        "style": values["Style Of Case"] or None,
        "cause_of_action": values["Cause of Action"] or None,
        "claim_amount": _decimal(values["Claim Amount"], "Claim Amount", row_number),
        "status": values["Case Status"] or None,
    }
    normalized = {
        "court": {"id": court_id, "court_code": court_code},
        "case": case,
        "parties": parties,
        "hearing": hearing,
        "disposition": disposition,
        "judgment": judgment,
    }
    non_hearing = {key: value for key, value in normalized.items() if key != "hearing"}
    raw_data = dict(zip(HEADERS, raw_values))
    return {
        "source_row_number": row_number - 1,
        "row_sha256": _sha(raw_values),
        "staged_case_id": case_id,
        "non_hearing_sha256": _sha(non_hearing),
        "raw_data": raw_data,
        "normalized_data": normalized,
    }


def parse_source(path: Path) -> ParsedImport:
    # Hash and parse the same immutable snapshot, even if the file is replaced.
    snapshot = path.read_bytes()
    file_sha256 = hashlib.sha256(snapshot).hexdigest()

    rows: list[dict[str, Any]] = []
    with io.StringIO(snapshot.decode("utf-8-sig"), newline="") as source:
        reader = csv.reader(source, dialect="excel", strict=True)
        try:
            raw_headers = next(reader)
        except StopIteration as exc:
            raise ImportValidationError("source has no header row") from exc
        except csv.Error as exc:
            raise ImportValidationError("malformed CSV header") from exc
        normalized_headers = [header.strip() for header in raw_headers]
        if tuple(normalized_headers) != HEADERS:
            raise ImportValidationError("source headers do not exactly match the required 70-column layout")
        try:
            for physical_row_number, raw_values in enumerate(reader, 2):
                if len(raw_values) != len(HEADERS):
                    raise ImportValidationError(
                        f"row {physical_row_number}: expected {len(HEADERS)} columns, found {len(raw_values)}"
                    )
                values = dict(zip(HEADERS, (value.strip() for value in raw_values)))
                rows.append(_normalize_row(values, raw_values, physical_row_number))
        except csv.Error as exc:
            raise ImportValidationError(f"malformed CSV near row {reader.line_num}") from exc

    if not rows:
        raise ImportValidationError("source has no data rows")

    case_hashes: dict[str, str] = {}
    for row in rows:
        prior = case_hashes.setdefault(row["staged_case_id"], row["non_hearing_sha256"])
        if prior != row["non_hearing_sha256"]:
            raise ImportValidationError(
                f"conflicting non-hearing fields for repeated case near source row {row['source_row_number']}"
            )

    row_hash_counts = Counter(row["row_sha256"] for row in rows)
    unique_case_data: dict[str, dict[str, Any]] = {}
    unique_judgments: dict[str, dict[str, Any]] = {}
    entity_ids: dict[str, set[str]] = {
        name: set() for name in ("courts", "cases", "parties", "attorneys", "addresses", "hearings", "dispositions", "judgments")
    }
    for row in rows:
        data = row["normalized_data"]
        entity_ids["courts"].add(data["court"]["id"])
        entity_ids["cases"].add(data["case"]["id"])
        unique_case_data.setdefault(data["case"]["id"], data["case"])
        if data["judgment"]:
            unique_judgments.setdefault(data["judgment"]["id"], data["judgment"])
        for party in data["parties"]:
            entity_ids["parties"].add(party["id"])
            if party["address"]:
                entity_ids["addresses"].add(_stable_id("address", "party", party["id"]))
            if party["attorney"]:
                entity_ids["attorneys"].add(party["attorney"]["id"])
                if party["attorney"]["address"]:
                    entity_ids["addresses"].add(_stable_id("address", "attorney", party["attorney"]["id"]))
        for singular, plural in (("hearing", "hearings"), ("disposition", "dispositions"), ("judgment", "judgments")):
            if data[singular]:
                entity_ids[plural].add(data[singular]["id"])

    claim_values = [decimal.Decimal(case["claim_amount"]) for case in unique_case_data.values()
                    if case["claim_amount"] is not None]
    judgment_values = [decimal.Decimal(item["judgment_amount"]) for item in unique_judgments.values()
                       if item["judgment_amount"] is not None]
    summary: dict[str, int | str] = {
        "status": "validated",
        "source_rows": len(rows),
        "unique_cases": len(entity_ids["cases"]),
        "exact_duplicate_occurrences": sum(count - 1 for count in row_hash_counts.values()),
        "repeated_case_occurrences_with_distinct_hearings": len(entity_ids["hearings"])
        - len({row["staged_case_id"] for row in rows if row["normalized_data"]["hearing"]}),
        "claim_amount_nonnull": len(claim_values),
        "claim_amount_zero": sum(value == 0 for value in claim_values),
        "claim_amount_total": format(sum(claim_values, decimal.Decimal()), "f"),
        "judgment_amount_total": format(sum(judgment_values, decimal.Decimal()), "f"),
    }
    summary.update({name: len(ids) for name, ids in entity_ids.items() if name != "cases"})
    return ParsedImport(
        import_id=file_sha256,
        source_filename=path.name,
        file_sha256=file_sha256,
        header_sha256=_sha(normalized_headers),
        raw_headers=tuple(raw_headers),
        rows=tuple(rows),
        summary=summary,
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _payload_chunks(rows: Iterable[dict[str, Any]], max_bytes: int) -> Iterable[str]:
    chunk: list[str] = []
    # Allow for the stage function call and account for SQL apostrophe escaping.
    base_bytes = 512
    size = base_bytes
    for row in rows:
        encoded = _canonical_json(row)
        added = len(encoded.replace("'", "''").encode("utf-8")) + (1 if chunk else 0)
        if added + base_bytes > max_bytes:
            raise ImportValidationError("one source row exceeds the configured SQL chunk limit")
        if chunk and size + added > max_bytes:
            yield "[" + ",".join(chunk) + "]"
            chunk = []
            size = base_bytes
        chunk.append(encoded)
        size += added
    if chunk:
        yield "[" + ",".join(chunk) + "]"


def _run_sql(sql: str, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
             workdir: Path | None = None) -> Any:
    temp_path: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix="court-import-", suffix=".sql")
        temp_path = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("SET standard_conforming_strings = on;\n")
            handle.write(sql)
        completed = runner(
            ["supabase", "db", "query", "--linked", "--file", str(temp_path), "-o", "json"]
            + (["--workdir", str(workdir)] if workdir else []),
            text=True, capture_output=True, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("Supabase SQL query failed; output was suppressed because it may contain court data")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Supabase SQL query returned an unreadable aggregate response") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _result_value(output: Any) -> dict[str, Any]:
    # Supabase CLI wraps PostgreSQL rows as {"rows": [...]}; accepting a bare
    # list as well keeps the parser compatible with older CLI releases.
    rows = output.get("rows") if isinstance(output, dict) else output
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("Supabase SQL query returned an unexpected aggregate response")
    value = rows[0].get("result")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Supabase SQL query returned an unexpected aggregate response") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Supabase SQL query returned an unexpected aggregate response")
    return value


def _check_linked_project(repo_root: Path, expected_ref: str) -> None:
    if not PROJECT_REF_RE.fullmatch(expected_ref):
        raise ImportValidationError("--project-ref must be a 20-character lowercase Supabase project ref")
    link_path = repo_root / "supabase" / ".temp" / "project-ref"
    try:
        actual = link_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("linked Supabase project metadata is unavailable") from exc
    if actual != expected_ref:
        raise RuntimeError("linked Supabase project does not match the expected --project-ref")


def apply_import(
    parsed: ParsedImport,
    project_ref: str,
    repo_root: Path,
    max_chunk_bytes: int = 450_000,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    _check_linked_project(repo_root, project_ref)
    if not 1_000 <= max_chunk_bytes < 500_000:
        raise ImportValidationError("--chunk-bytes must be between 1000 and 499999")

    # Every query resolves --linked from this private per-run directory, not
    # mutable repository metadata. Concurrent `supabase link` cannot retarget it.
    with tempfile.TemporaryDirectory(prefix="court-target-") as directory:
        pinned_root = Path(directory)
        metadata_dir = pinned_root / "supabase" / ".temp"
        metadata_dir.mkdir(parents=True)
        metadata_dir.joinpath("project-ref").write_text(project_ref, encoding="utf-8")
        pooler_path = repo_root / "supabase" / ".temp" / "pooler-url"
        if pooler_path.exists():
            pooler_url = pooler_path.read_text(encoding="utf-8").strip()
            parsed_url = urlsplit(pooler_url)
            if (parsed_url.scheme not in {"postgres", "postgresql"}
                    or unquote(parsed_url.username or "") != f"postgres.{project_ref}"
                    or not (parsed_url.hostname or "").endswith(".pooler.supabase.com")
                    or parsed_url.password is not None):
                raise RuntimeError("pooler metadata does not match the expected project")
            metadata_dir.joinpath("pooler-url").write_text(pooler_url, encoding="utf-8")
        return _apply_import_pinned(parsed, pinned_root, max_chunk_bytes, runner)


def _apply_import_pinned(parsed: ParsedImport, pinned_root: Path, max_chunk_bytes: int,
                         runner: Callable[..., subprocess.CompletedProcess[str]]) -> dict[str, Any]:
    metadata = {
        "import_id": parsed.import_id,
        "source_filename": parsed.source_filename,
        "source_sha256": parsed.file_sha256,
        "header_sha256": parsed.header_sha256,
        "source_headers": list(parsed.raw_headers),
        "expected_source_rows": len(parsed.rows),
    }
    prepare_sql = (
        "SELECT court_data.prepare_import("
        + _sql_literal(_canonical_json(metadata))
        + "::jsonb) AS result;\n"
    )
    prepared = _result_value(_run_sql(prepare_sql, runner, pinned_root))
    if prepared.get("status") == "complete":
        return prepared
    if prepared.get("status") != "staging":
        raise RuntimeError("remote import is not in a resumable staging state")

    staged_count = prepared.get("source_rows", 0)
    if not isinstance(staged_count, int) or not 0 <= staged_count <= len(parsed.rows):
        raise RuntimeError("remote staging count is invalid")
    if staged_count:
        fingerprint = hashlib.sha256("\n".join(
            f"{row['source_row_number']}:{row['row_sha256']}:{row['non_hearing_sha256']}:{row['staged_case_id']}"
            for row in parsed.rows[:staged_count]
        ).encode("utf-8")).hexdigest()
        if prepared.get("source_fingerprint") != fingerprint:
            raise RuntimeError("remote staged rows do not match the expected source prefix")

    for payload in _payload_chunks(parsed.rows[staged_count:], max_chunk_bytes):
        sql = (
            "SELECT jsonb_build_object('staged_rows', court_data.stage_source_rows("
            + _sql_literal(parsed.import_id) + ","
            + _sql_literal(payload) + "::jsonb)) AS result;\n"
        )
        _result_value(_run_sql(sql, runner, pinned_root))

    final_sql = (
        "SET statement_timeout = '10min';\nSELECT court_data.finalize_import("
        + _sql_literal(parsed.import_id) + ") AS result;\n"
    )
    return _result_value(_run_sql(final_sql, runner, pinned_root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true", help="validate locally and print aggregate counts only")
    action.add_argument("--apply", action="store_true", help="stage and finalize through linked Supabase SQL queries")
    parser.add_argument("--project-ref", help="required expected linked project ref for --apply")
    parser.add_argument("--chunk-bytes", type=int, default=450_000, help="maximum generated staging SQL size (<500000)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        parsed = parse_source(args.source)
        if args.dry_run:
            if args.project_ref:
                raise ImportValidationError("--project-ref is only valid with --apply")
            result = parsed.summary
        else:
            if not args.project_ref:
                raise ImportValidationError("--project-ref is required with --apply")
            repo_root = Path(__file__).resolve().parents[1]
            result = apply_import(parsed, args.project_ref, repo_root, args.chunk_bytes)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ImportValidationError, RuntimeError, OSError) as exc:
        print(f"court import failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
