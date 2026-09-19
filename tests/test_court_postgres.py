"""Opt-in PostgreSQL checks; COURT_TEST_CONTAINER must name a disposable DB.

Uses synthetic records unless COURT_TEST_SOURCE is explicitly set. No row data
or SQL errors are printed. Run with an isolated PostgreSQL 17 container whose
roles include anon, authenticated, service_role. Never target a shared database.
"""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_court_import as fixtures

court_import = fixtures.court_import


@unittest.skipUnless(os.environ.get('COURT_TEST_CONTAINER'), 'isolated PostgreSQL container not configured')
class CourtPostgresTests(unittest.TestCase):
    def run_db(self, sql):
        return subprocess.run(
            ['docker', 'exec', '-i', os.environ['COURT_TEST_CONTAINER'],
             'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'postgres'],
            input=sql, text=True, capture_output=True,
        )

    def runner(self, command, **kwargs):
        sql = Path(command[command.index('--file') + 1]).read_text()
        result = self.run_db(sql)
        if result.returncode:
            return subprocess.CompletedProcess(command, result.returncode, '', 'database error suppressed')
        return subprocess.CompletedProcess(command, 0, json.dumps({'rows': [{'result': json.loads(result.stdout)}]}), '')

    def test_migration_import_idempotency_and_fail_closed_isolation(self):
        root = Path(__file__).resolve().parents[1]
        migration = (root / 'supabase/migrations/20260919000000_create_private_court_data.sql').read_text()
        self.assertEqual(self.run_db(migration).returncode, 0, 'migration failed (details suppressed)')
        helper = fixtures.CourtImportTests()
        first = helper.make_row(**{'Next Hearing Desc': 'Synthetic hearing', 'Next Hearing Date': '09/20/2026', 'Next Hearing Time': '09:00 AM'})
        second = dict(first, **{'Next Hearing Date': '09/21/2026'})
        path = helper.write_source([first, first, second])
        self.addCleanup(helper.doCleanups)
        source = Path(os.environ['COURT_TEST_SOURCE']) if os.environ.get('COURT_TEST_SOURCE') else path
        parsed = court_import.parse_source(source)
        # This runner ignores linked remote targets and can only exec the named container.
        result = court_import._apply_import_pinned(parsed, root, 450_000, self.runner)
        for key in ('source_rows', 'unique_cases', 'courts', 'parties', 'attorneys', 'addresses', 'hearings', 'dispositions', 'judgments'):
            self.assertEqual(result[key], parsed.summary[key], key)
        for key in ('foreign_key_violations', 'rls_tables_missing', 'public_role_table_privileges'):
            self.assertEqual(result[key], 0, key)
        self.assertFalse(result['public_schema_access'])
        renamed = dataclasses.replace(parsed, source_filename='synthetic-renamed-export.txt')
        self.assertEqual(court_import._apply_import_pinned(renamed, root, 450_000, self.runner), result)
        for change in (
            'ALTER TABLE court_data.cases DISABLE ROW LEVEL SECURITY;',
            'GRANT SELECT ON court_data.cases TO anon;',
            'GRANT USAGE ON SCHEMA court_data TO authenticated;',
        ):
            sql = 'BEGIN; ' + change + ' SELECT court_data.verify_import(' + court_import._sql_literal(parsed.import_id) + '); ROLLBACK;'
            failure = self.run_db(sql)
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn('isolation verification failed', failure.stderr)
        for role in ('anon', 'authenticated'):
            denied = self.run_db(f'BEGIN; SET LOCAL ROLE {role}; SELECT count(*) FROM court_data.cases; ROLLBACK;')
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn('permission denied', denied.stderr)
        final = self.run_db('SELECT court_data.verify_import(' + court_import._sql_literal(parsed.import_id) + ');')
        self.assertEqual(final.returncode, 0)
        print('PostgreSQL import verified (aggregate counts, renamed retry, privacy failures, public-role denial).')
