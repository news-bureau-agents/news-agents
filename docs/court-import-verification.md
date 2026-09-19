# Court import verification — 2026-09-19

Imported the September 1–18, 2026 court export into the `news-agents` Supabase project, private schema `court_data`. No source records, names, addresses, credentials, or PII-bearing SQL are included in Git.

| Entity | Verified rows |
|---|---:|
| Completed import | 1 |
| Original source occurrences | 10,729 |
| Unique cases | 10,720 |
| Courts | 16 |
| Case-scoped parties | 22,404 |
| Attorney assignments | 6,289 |
| Addresses | 27,545 |
| Distinct hearings | 4,269 |
| Dispositions | 400 |
| Judgments | 62 |

## Integrity

- All source occurrences retained, including two exact duplicate occurrences.
- Seven additional distinct hearing records retained rather than discarded by case deduplication.
- Source byte SHA-256 matches import provenance.
- All case claim amounts match corresponding raw source values; zero mismatches.
- Non-null claim amounts: 8,963; zero amounts: 217; blanks remain NULL.
- Unique-case claim total: $33,807,288.7100.
- Judgment amount total: $172,678.8200.
- Zero reported foreign-key violations; all database constraints validated.
- An interrupted upload was resumed after verifying its ordered staged-prefix fingerprint.
- Reapplying the byte-identical differently named download returned the same completed import and counts, without adding rows.

## Privacy and runtime checks

- All 11 court tables have RLS enabled, with no policies granting public access.
- No court schema/table access for `anon` or `authenticated`.
- Actual SQL queries under both public roles were denied on Supabase.
- `service_role` has explicit server-only privileges; no privileged key was added to the app or browser.
- The existing Flask frontend remains an Auth demo and cannot read court records.

## Tests and migration

- 35 tests passed, including synthetic PostgreSQL integration.
- A full actual-source import passed against isolated PostgreSQL 17 before the remote import.
- Integration tested renamed retries, denial of public-role reads, and fail-closed verification after simulated RLS/grant drift.
- Unit regressions cover one-snapshot hashing/parsing, concurrent relink protection, pooler target mismatch, and verified staging-prefix resume.
- Migration `20260919000000` applied and confirmed in local/remote Supabase migration history.
- The disposable local PostgreSQL container was stopped and removed. The PII-bearing temporary SQL file left by the timed-out upload was removed.

These checks establish import consistency, not source completeness or legal accuracy against the court's live records.
