# Private court-data import

Court records are stored only in the `court_data` schema. The migration revokes schema/table/function access from `PUBLIC`, `anon`, and `authenticated`, enables RLS without policies on every table, and explicitly grants server access to `service_role`. It does not change the existing public application schema.

## Safety properties

- The importer requires the exact normalized 70-column header and rejects malformed row widths.
- Values are trimmed for normalized records. Dates, times, and decimals are parsed strictly; blank amounts remain `NULL`. Case numbers and postal codes remain text.
- Every physical source occurrence is retained in `source_rows`, including exact duplicates. The original field values are retained in JSONB alongside normalized staging JSON and SHA-256 identities.
- Parties are case-scoped. Attorneys reference the exact represented party; people are never merged by name. Hearings, dispositions, and judgments are separate records.
- A repeated court/case key with differing non-hearing data fails validation rather than overwriting data.
- Hashing and parsing use one immutable in-memory byte snapshot. Content identity is SHA-256, independent of the download filename.
- Apply is resumable: an ordered fingerprint verifies the staged source prefix before skipping it. A mismatched/noncontiguous prefix is rejected. Finalization is one database transaction and marks an import complete only after row/provenance and privacy checks. A verified complete import is a no-op, including a byte-identical renamed download.
- Privacy verification raises an error if RLS is disabled or public roles gain schema/table access. Checks run before staging and on completion/retry.
- PII-bearing SQL exists only in mode-0600 temporary files, which are removed in `finally`. CLI stdout/stderr are captured and never echoed on errors. The CLI responses contain aggregate results only.

The migration is intentionally **not** applied by the importer. Review and apply it separately:

```sh
supabase db query --linked \
  --file supabase/migrations/20260919000000_create_private_court_data.sql \
  -o json
```

## Validate locally

Use the existing environment; no dependency installation is needed:

```sh
.venv/bin/python scripts/import_court_data.py \
  '/home/carlunpen/Downloads/CasesFiled-09012026To09182026 (1).txt' \
  --dry-run
```

Dry-run performs no network or CLI operation and prints aggregate counts only.

## Apply after the migration

Confirm the expected linked project ref out of band, then run:

```sh
.venv/bin/python scripts/import_court_data.py \
  '/home/carlunpen/Downloads/CasesFiled-09012026To09182026 (1).txt' \
  --apply --project-ref YOUR_20_CHARACTER_REF
```

`--apply` checks `supabase/.temp/project-ref` against the explicit expected ref. It then creates a private per-run workdir containing that fixed ref and validated project-specific pooler metadata, passing it explicitly to every CLI call. Concurrent repository relinking cannot retarget later batches. Run `supabase link --project-ref ...` first to establish IPv4 pooler metadata when direct IPv6 access is unavailable.

It stages bounded SQL chunks below 500 KB and raises finalization's statement timeout to ten minutes. Allow at least 15 minutes for a fresh 10,729-row import through this CLI; per-call login overhead can be substantial. After interruption, rerun the same command: the verified staged prefix is skipped. Ordinary exceptions clean up temporary SQL; a force-killed process may leave a mode-0600 `court-import-*.sql` file in the OS temp directory, which the operator must remove after confirming ownership. Do not redirect or preserve PII-bearing SQL. Successful output is only aggregate entity, provenance, RLS, foreign-key, and access-check counts.

After manually applying and verifying this migration, record it once in migration history:

```sh
supabase migration repair 20260919000000 --status applied --linked
supabase migration list --linked
```

This is a conservative initial snapshot importer. Future exports with changed case/party/address attributes are rejected rather than silently overwriting history; a versioned update policy must be added before using it as a recurring court-record sync.

Run synthetic tests with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```
