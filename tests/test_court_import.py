import csv
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "import_court_data.py"
SPEC = importlib.util.spec_from_file_location("court_import", MODULE_PATH)
court_import = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = court_import
assert SPEC.loader is not None
SPEC.loader.exec_module(court_import)


class CourtImportTests(unittest.TestCase):
    def make_row(self, **overrides):
        row = {header: "" for header in court_import.HEADERS}
        row.update({
            "Case Number": "SYNTH-001",
            "JP Court ID": "SYNTH-COURT",
            "Case Type": "Synthetic civil",
            "Case File Date": "09/01/2026",
            "Style Of Case": "Synthetic Alpha v. Synthetic Beta",
            "Cause of Action": "Synthetic cause",
            "Claim Amount": "125.50",
            "Case Status": "Synthetic open",
            "Plaintiff Name": "Synthetic Alpha",
            "Plaintiff Addr Line 1": "1 Example Way",
            "Plaintiff Addr City": "Exampleville",
            "Plaintiff Addr State": "TX",
            "Plaintiff Addr Zip": "00123",
            "Plaintiff Atty Name": "Synthetic Counsel",
            "Plaintiff Atty Addr 1": "2 Example Way",
            "Plaintiff Atty City": "Exampleville",
            "Plaintiff Atty State": "TX",
            "Plaintiff Atty Zip": "00124",
            "Defendant Name": "Synthetic Beta",
        })
        row.update(overrides)
        return row

    def write_source(self, rows, headers=None):
        temp = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False)
        self.addCleanup(lambda: Path(temp.name).unlink(missing_ok=True))
        headers = list(headers or court_import.HEADERS)
        with temp:
            writer = csv.DictWriter(temp, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return Path(temp.name)

    def test_normalizes_repeated_hearings_and_retains_every_occurrence(self):
        first = self.make_row(**{
            "Next Hearing Desc": "Synthetic setting",
            "Next Hearing Date": "09/20/2026",
            "Next Hearing Time": "9:05 AM",
        })
        second = dict(first)
        second.update({"Next Hearing Desc": "Synthetic trial", "Next Hearing Date": "09/21/2026"})
        parsed = court_import.parse_source(self.write_source([first, first, second]))

        self.assertEqual(parsed.summary["source_rows"], 3)
        self.assertEqual(parsed.summary["unique_cases"], 1)
        self.assertEqual(parsed.summary["exact_duplicate_occurrences"], 1)
        self.assertEqual(parsed.summary["repeated_case_occurrences_with_distinct_hearings"], 1)
        self.assertEqual(parsed.summary["parties"], 2)
        self.assertEqual(parsed.summary["attorneys"], 1)
        self.assertEqual(parsed.summary["addresses"], 2)
        self.assertEqual(parsed.summary["hearings"], 2)
        self.assertEqual(len(parsed.rows), 3)
        normalized = parsed.rows[0]["normalized_data"]
        self.assertEqual(normalized["case"]["claim_amount"], "125.50")
        self.assertEqual(normalized["hearing"]["hearing_time"], "09:05:00")
        self.assertEqual(normalized["parties"][0]["address"]["postal_code"], "00123")

    def test_deterministic_import_and_entity_identifiers(self):
        path = self.write_source([self.make_row()])
        first = court_import.parse_source(path)
        second = court_import.parse_source(path)
        self.assertEqual(first.import_id, second.import_id)
        self.assertEqual(first.rows[0]["normalized_data"], second.rows[0]["normalized_data"])

    def test_hash_and_parse_use_one_immutable_snapshot(self):
        path = self.write_source([self.make_row()])
        original = path.read_bytes()
        with patch.object(Path, "read_bytes", return_value=original) as read_bytes:
            parsed = court_import.parse_source(path)
        read_bytes.assert_called_once_with()
        self.assertEqual(parsed.import_id, court_import.hashlib.sha256(original).hexdigest())
        self.assertEqual(parsed.summary["source_rows"], 1)

    def test_whitespace_only_hearing_difference_is_not_a_distinct_hearing(self):
        first = self.make_row(**{"Next Hearing Desc": "Synthetic hearing", "Next Hearing Date": "09/20/2026", "Next Hearing Time": "09:00 AM"})
        second = dict(first, **{"Next Hearing Desc": " Synthetic hearing "})
        parsed = court_import.parse_source(self.write_source([first, second]))
        self.assertEqual(parsed.summary["hearings"], 1)
        self.assertEqual(parsed.summary["repeated_case_occurrences_with_distinct_hearings"], 0)

    def test_concurrent_repository_relink_does_not_change_pinned_destination(self):
        parsed = court_import.parse_source(self.write_source([self.make_row()]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "supabase" / ".temp"
            metadata.mkdir(parents=True)
            link = metadata / "project-ref"
            link.write_text("a" * 20)
            targets = []

            def runner(command, **kwargs):
                pinned = Path(command[command.index("--workdir") + 1])
                targets.append((pinned / "supabase" / ".temp" / "project-ref").read_text())
                link.write_text("b" * 20)
                sql = Path(command[command.index("--file") + 1]).read_text()
                result = {"status": "complete" if "finalize_import" in sql else "staging"}
                return subprocess.CompletedProcess(command, 0, json.dumps({"rows": [{"result": result}]}), "")

            court_import.apply_import(parsed, "a" * 20, root, runner=runner)
            self.assertGreater(len(targets), 1)
            self.assertEqual(set(targets), {"a" * 20})

    def test_matching_staged_prefix_is_skipped_and_mismatch_rejected(self):
        parsed = court_import.parse_source(self.write_source([self.make_row()]))
        row = parsed.rows[0]
        fingerprint = court_import.hashlib.sha256(f"1:{row['row_sha256']}:{row['non_hearing_sha256']}:{row['staged_case_id']}".encode()).hexdigest()
        sql_calls = []

        def runner(command, **kwargs):
            sql = Path(command[command.index("--file") + 1]).read_text()
            sql_calls.append(sql)
            result = {"status": "staging", "source_rows": 1, "source_fingerprint": fingerprint} if "prepare_import" in sql else {"status": "complete"}
            return subprocess.CompletedProcess(command, 0, json.dumps({"rows": [{"result": result}]}), "")

        result = court_import._apply_import_pinned(parsed, Path("/tmp"), 450000, runner)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(sql_calls), 2)
        self.assertFalse(any("stage_source_rows" in sql for sql in sql_calls))
        fingerprint = "wrong"
        with self.assertRaisesRegex(RuntimeError, "source prefix"):
            court_import._apply_import_pinned(parsed, Path("/tmp"), 450000, runner)

    def test_pooler_for_another_project_is_rejected(self):
        parsed = court_import.parse_source(self.write_source([self.make_row()]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "supabase" / ".temp"
            metadata.mkdir(parents=True)
            metadata.joinpath("project-ref").write_text("a" * 20)
            metadata.joinpath("pooler-url").write_text("postgresql://postgres." + "b" * 20 + "@example.pooler.supabase.com:5432/postgres")
            with self.assertRaisesRegex(RuntimeError, "pooler metadata"):
                court_import.apply_import(parsed, "a" * 20, root)

    def test_rejects_unknown_header(self):
        headers = list(court_import.HEADERS)
        headers[-1] = "Unexpected Column"
        with self.assertRaisesRegex(court_import.ImportValidationError, "headers"):
            court_import.parse_source(self.write_source([self.make_row()], headers))

    def test_rejects_malformed_width(self):
        path = self.write_source([self.make_row()])
        with path.open("a", encoding="utf-8", newline="") as output:
            output.write("too,few,columns\n")
        with self.assertRaisesRegex(court_import.ImportValidationError, "expected 70 columns"):
            court_import.parse_source(path)

    def test_rejects_invalid_date_time_and_decimal(self):
        invalid = (
            ({"Case File Date": "2026-09-01"}, "invalid date"),
            ({"Next Hearing Date": "09/20/2026"}, "incomplete hearing"),
            ({"Next Hearing Desc": "Synthetic setting", "Next Hearing Date": "09/20/2026",
              "Next Hearing Time": "25:00 PM"}, "invalid time"),
            ({"Claim Amount": "$125.50"}, "invalid decimal"),
        )
        for overrides, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(court_import.ImportValidationError, message):
                    court_import.parse_source(self.write_source([self.make_row(**overrides)]))

    def test_rejects_repeated_case_nonhearing_conflict(self):
        changed = self.make_row(**{"Case Status": "Synthetic closed"})
        with self.assertRaisesRegex(court_import.ImportValidationError, "conflicting non-hearing"):
            court_import.parse_source(self.write_source([self.make_row(), changed]))

    def test_sql_temp_file_is_mode_0600_removed_and_output_not_echoed(self):
        observed = {}

        def runner(command, **kwargs):
            path = Path(command[command.index("--file") + 1])
            observed["path"] = path
            observed["mode"] = os.stat(path).st_mode & 0o777
            observed["content"] = path.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, '{"rows":[{"result":{"status":"staging"}}]}', "")

        output = court_import._run_sql("SELECT 'synthetic-sensitive-marker';", runner)
        self.assertEqual(output["rows"][0]["result"]["status"], "staging")
        self.assertEqual(observed["mode"], 0o600)
        self.assertIn("synthetic-sensitive-marker", observed["content"])
        self.assertFalse(observed["path"].exists())

    def test_completed_import_is_verified_then_no_op(self):
        parsed = court_import.parse_source(self.write_source([self.make_row()]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "supabase" / ".temp"
            metadata.mkdir(parents=True)
            metadata.joinpath("project-ref").write_text("a" * 20, encoding="utf-8")
            calls = []

            def runner(command, **kwargs):
                calls.append(command)
                response = {"rows": [{"result": {"status": "complete", "source_rows": 1}}]}
                return subprocess.CompletedProcess(command, 0, json.dumps(response), "")

            result = court_import.apply_import(parsed, "a" * 20, root, runner=runner)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(calls), 1)

    def test_sql_failure_suppresses_cli_output_and_removes_file(self):
        observed = {}

        def runner(command, **kwargs):
            observed["path"] = Path(command[command.index("--file") + 1])
            return subprocess.CompletedProcess(command, 1, "sensitive stdout", "sensitive stderr")

        with self.assertRaisesRegex(RuntimeError, "output was suppressed") as raised:
            court_import._run_sql("SELECT 'synthetic';", runner)
        self.assertNotIn("sensitive", str(raised.exception))
        self.assertFalse(observed["path"].exists())

    def test_chunks_remain_below_configured_bound(self):
        rows = [{"source_row_number": number, "payload": "x" * 300} for number in range(1, 12)]
        chunks = list(court_import._payload_chunks(rows, 1000))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-8")) + 256, 1000)
            self.assertIsInstance(json.loads(chunk), list)

    def test_project_ref_must_match_ignored_link_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "supabase" / ".temp"
            metadata.mkdir(parents=True)
            metadata.joinpath("project-ref").write_text("a" * 20, encoding="utf-8")
            court_import._check_linked_project(root, "a" * 20)
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                court_import._check_linked_project(root, "b" * 20)


if __name__ == "__main__":
    unittest.main()
