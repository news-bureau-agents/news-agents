-- Private, normalized court import storage. This migration does not expose any
-- object through the public schema or PostgREST roles.
BEGIN;
CREATE SCHEMA IF NOT EXISTS court_data;
REVOKE ALL ON SCHEMA court_data FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA court_data TO service_role;

CREATE TABLE IF NOT EXISTS court_data.imports (
    import_id text PRIMARY KEY CHECK (import_id ~ '^[0-9a-f]{64}$'),
    source_filename text NOT NULL,
    source_sha256 text NOT NULL UNIQUE CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    header_sha256 text NOT NULL CHECK (header_sha256 ~ '^[0-9a-f]{64}$'),
    source_headers jsonb NOT NULL CHECK (jsonb_typeof(source_headers) = 'array'),
    expected_source_rows integer NOT NULL CHECK (expected_source_rows > 0),
    status text NOT NULL DEFAULT 'staging' CHECK (status IN ('staging', 'complete')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK ((status = 'complete') = (completed_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS court_data.source_rows (
    import_id text NOT NULL REFERENCES court_data.imports(import_id),
    source_row_number integer NOT NULL CHECK (source_row_number > 0),
    row_sha256 text NOT NULL CHECK (row_sha256 ~ '^[0-9a-f]{64}$'),
    staged_case_id text NOT NULL CHECK (staged_case_id ~ '^[0-9a-f]{64}$'),
    non_hearing_sha256 text NOT NULL CHECK (non_hearing_sha256 ~ '^[0-9a-f]{64}$'),
    raw_data jsonb NOT NULL CHECK (jsonb_typeof(raw_data) = 'object'),
    normalized_data jsonb NOT NULL CHECK (jsonb_typeof(normalized_data) = 'object'),
    PRIMARY KEY (import_id, source_row_number)
);

CREATE TABLE IF NOT EXISTS court_data.courts (
    court_id text PRIMARY KEY CHECK (court_id ~ '^[0-9a-f]{64}$'),
    court_code text NOT NULL UNIQUE CHECK (btrim(court_code) <> '')
);

CREATE TABLE IF NOT EXISTS court_data.cases (
    case_id text PRIMARY KEY CHECK (case_id ~ '^[0-9a-f]{64}$'),
    court_id text NOT NULL REFERENCES court_data.courts(court_id),
    case_number text NOT NULL CHECK (btrim(case_number) <> ''),
    case_type text,
    file_date date NOT NULL,
    style text,
    cause_of_action text,
    claim_amount numeric,
    status text,
    UNIQUE (court_id, case_number)
);

CREATE TABLE IF NOT EXISTS court_data.parties (
    party_id text PRIMARY KEY CHECK (party_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL REFERENCES court_data.cases(case_id),
    role text NOT NULL CHECK (role IN ('plaintiff', 'defendant')),
    ordinal smallint NOT NULL CHECK (ordinal > 0),
    name text NOT NULL CHECK (btrim(name) <> ''),
    UNIQUE (case_id, role, ordinal)
);

CREATE TABLE IF NOT EXISTS court_data.attorneys (
    attorney_id text PRIMARY KEY CHECK (attorney_id ~ '^[0-9a-f]{64}$'),
    represented_party_id text NOT NULL UNIQUE REFERENCES court_data.parties(party_id),
    name text NOT NULL CHECK (btrim(name) <> '')
);

CREATE TABLE IF NOT EXISTS court_data.addresses (
    address_id text PRIMARY KEY CHECK (address_id ~ '^[0-9a-f]{64}$'),
    party_id text UNIQUE REFERENCES court_data.parties(party_id),
    attorney_id text UNIQUE REFERENCES court_data.attorneys(attorney_id),
    line1 text,
    line2 text,
    city text,
    state text,
    postal_code text,
    CHECK ((party_id IS NOT NULL)::integer + (attorney_id IS NOT NULL)::integer = 1),
    CHECK (num_nonnulls(line1, line2, city, state, postal_code) > 0)
);

CREATE TABLE IF NOT EXISTS court_data.hearings (
    hearing_id text PRIMARY KEY CHECK (hearing_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL REFERENCES court_data.cases(case_id),
    description text NOT NULL CHECK (btrim(description) <> ''),
    hearing_date date NOT NULL,
    hearing_time time without time zone NOT NULL,
    UNIQUE (case_id, description, hearing_date, hearing_time)
);

CREATE TABLE IF NOT EXISTS court_data.dispositions (
    disposition_id text PRIMARY KEY CHECK (disposition_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL REFERENCES court_data.cases(case_id),
    description text NOT NULL CHECK (btrim(description) <> ''),
    disposition_date date NOT NULL,
    UNIQUE (case_id, description, disposition_date)
);

CREATE TABLE IF NOT EXISTS court_data.judgments (
    judgment_id text PRIMARY KEY CHECK (judgment_id ~ '^[0-9a-f]{64}$'),
    case_id text NOT NULL REFERENCES court_data.cases(case_id),
    judgment_text text,
    judgment_date date,
    in_favor_of text,
    against text,
    judgment_amount numeric,
    attorney_fees numeric,
    court_costs numeric,
    pre_judgment_interest_rate numeric,
    post_judgment_interest_rate numeric,
    CHECK (num_nonnulls(judgment_text, judgment_date, in_favor_of, against,
        judgment_amount, attorney_fees, court_costs, pre_judgment_interest_rate,
        post_judgment_interest_rate) > 0)
);

CREATE TABLE IF NOT EXISTS court_data.case_source_rows (
    import_id text NOT NULL,
    source_row_number integer NOT NULL,
    case_id text NOT NULL REFERENCES court_data.cases(case_id),
    PRIMARY KEY (import_id, source_row_number),
    FOREIGN KEY (import_id, source_row_number)
        REFERENCES court_data.source_rows(import_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS court_source_rows_case_idx ON court_data.source_rows(staged_case_id);
CREATE INDEX IF NOT EXISTS court_cases_court_idx ON court_data.cases(court_id);
CREATE INDEX IF NOT EXISTS court_parties_case_idx ON court_data.parties(case_id);
CREATE INDEX IF NOT EXISTS court_hearings_case_idx ON court_data.hearings(case_id);
CREATE INDEX IF NOT EXISTS court_dispositions_case_idx ON court_data.dispositions(case_id);
CREATE INDEX IF NOT EXISTS court_judgments_case_idx ON court_data.judgments(case_id);
CREATE INDEX IF NOT EXISTS court_case_source_case_idx ON court_data.case_source_rows(case_id);

CREATE OR REPLACE FUNCTION court_data.verify_import(p_import_id text)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog, court_data
AS $$
DECLARE
    result jsonb;
    expected_count integer;
    import_status text;
    source_count bigint;
    linked_count bigint;
    fk_violations bigint;
    rls_missing bigint;
    exposed_privileges bigint;
BEGIN
    SELECT expected_source_rows, status INTO expected_count, import_status
    FROM court_data.imports WHERE import_id = p_import_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown court import'; END IF;

    SELECT count(*) INTO source_count FROM court_data.source_rows WHERE import_id = p_import_id;
    SELECT count(*) INTO linked_count FROM court_data.case_source_rows WHERE import_id = p_import_id;
    SELECT count(*) INTO fk_violations
    FROM court_data.case_source_rows csr
    LEFT JOIN court_data.cases c ON c.case_id = csr.case_id
    WHERE csr.import_id = p_import_id AND c.case_id IS NULL;

    SELECT count(*) INTO rls_missing
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'court_data' AND c.relkind IN ('r', 'p') AND NOT c.relrowsecurity;

    SELECT count(*) INTO exposed_privileges
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'court_data' AND c.relkind IN ('r', 'p') AND
      (has_table_privilege('anon', c.oid, 'SELECT,INSERT,UPDATE,DELETE') OR
       has_table_privilege('authenticated', c.oid, 'SELECT,INSERT,UPDATE,DELETE'));

    IF rls_missing <> 0 OR exposed_privileges <> 0 OR
       has_schema_privilege('anon', 'court_data', 'USAGE') OR
       has_schema_privilege('authenticated', 'court_data', 'USAGE') THEN
        RAISE EXCEPTION 'court import isolation verification failed';
    END IF;

    IF import_status = 'complete' AND (source_count <> expected_count OR linked_count <> expected_count OR fk_violations <> 0) THEN
        RAISE EXCEPTION 'completed court import failed verification';
    END IF;

    SELECT jsonb_build_object(
        'status', import_status,
        'source_rows', source_count,
        'unique_cases', count(DISTINCT csr.case_id),
        'courts', count(DISTINCT c.court_id),
        'parties', count(DISTINCT p.party_id),
        'attorneys', count(DISTINCT a.attorney_id),
        'addresses', count(DISTINCT ad.address_id),
        'hearings', count(DISTINCT h.hearing_id),
        'dispositions', count(DISTINCT d.disposition_id),
        'judgments', count(DISTINCT j.judgment_id),
        'foreign_key_violations', fk_violations,
        'rls_tables_missing', rls_missing,
        'public_role_table_privileges', exposed_privileges,
        'public_schema_access',
          has_schema_privilege('anon', 'court_data', 'USAGE') OR
          has_schema_privilege('authenticated', 'court_data', 'USAGE')
    ) INTO result
    FROM court_data.case_source_rows csr
    JOIN court_data.cases c ON c.case_id = csr.case_id
    LEFT JOIN court_data.parties p ON p.case_id = c.case_id
    LEFT JOIN court_data.attorneys a ON a.represented_party_id = p.party_id
    LEFT JOIN court_data.addresses ad ON ad.party_id = p.party_id OR ad.attorney_id = a.attorney_id
    LEFT JOIN court_data.hearings h ON h.case_id = c.case_id
    LEFT JOIN court_data.dispositions d ON d.case_id = c.case_id
    LEFT JOIN court_data.judgments j ON j.case_id = c.case_id
    WHERE csr.import_id = p_import_id;
    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION court_data.prepare_import(p_metadata jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog, court_data
AS $$
DECLARE
    current_import court_data.imports%ROWTYPE;
BEGIN
    IF p_metadata->>'import_id' IS NULL OR p_metadata->>'expected_source_rows' IS NULL THEN
        RAISE EXCEPTION 'invalid import metadata';
    END IF;
    INSERT INTO court_data.imports
      (import_id, source_filename, source_sha256, header_sha256, source_headers, expected_source_rows)
    VALUES
      (p_metadata->>'import_id', p_metadata->>'source_filename', p_metadata->>'source_sha256',
       p_metadata->>'header_sha256', p_metadata->'source_headers',
       (p_metadata->>'expected_source_rows')::integer)
    ON CONFLICT (import_id) DO NOTHING;

    SELECT * INTO current_import FROM court_data.imports
    WHERE import_id = p_metadata->>'import_id' FOR UPDATE;
    IF current_import.source_sha256 IS DISTINCT FROM p_metadata->>'source_sha256'
       OR current_import.header_sha256 IS DISTINCT FROM p_metadata->>'header_sha256'
       OR current_import.source_headers IS DISTINCT FROM p_metadata->'source_headers'
       OR current_import.expected_source_rows IS DISTINCT FROM (p_metadata->>'expected_source_rows')::integer THEN
        RAISE EXCEPTION 'import metadata conflict';
    END IF;
    IF current_import.status = 'complete' THEN
        RETURN court_data.verify_import(current_import.import_id);
    END IF;
    PERFORM court_data.verify_import(current_import.import_id);
    RETURN jsonb_build_object(
        'status', 'staging',
        'source_rows', (SELECT count(*) FROM court_data.source_rows WHERE import_id = current_import.import_id),
        'source_fingerprint', (
          SELECT encode(sha256(convert_to(coalesce(string_agg(
            source_row_number::text || ':' || row_sha256 || ':' || non_hearing_sha256 || ':' || staged_case_id,
            E'\n' ORDER BY source_row_number), ''), 'UTF8')), 'hex')
          FROM court_data.source_rows WHERE import_id = current_import.import_id
        )
    );
END;
$$;

CREATE OR REPLACE FUNCTION court_data.stage_source_rows(p_import_id text, p_rows jsonb)
RETURNS bigint
LANGUAGE plpgsql
SET search_path = pg_catalog, court_data
AS $$
DECLARE
    item jsonb;
    current_status text;
    existing court_data.source_rows%ROWTYPE;
BEGIN
    SELECT status INTO current_status FROM court_data.imports WHERE import_id = p_import_id FOR UPDATE;
    IF NOT FOUND OR current_status <> 'staging' THEN RAISE EXCEPTION 'import is not staging'; END IF;
    IF jsonb_typeof(p_rows) <> 'array' THEN RAISE EXCEPTION 'invalid staged row payload'; END IF;
    PERFORM court_data.verify_import(p_import_id);

    FOR item IN SELECT value FROM jsonb_array_elements(p_rows)
    LOOP
        IF item->>'source_row_number' IS NULL OR
           (SELECT count(*) FROM jsonb_object_keys(item->'raw_data')) <> 70 THEN
            RAISE EXCEPTION 'invalid staged source row';
        END IF;
        INSERT INTO court_data.source_rows
          (import_id, source_row_number, row_sha256, staged_case_id, non_hearing_sha256, raw_data, normalized_data)
        VALUES
          (p_import_id, (item->>'source_row_number')::integer, item->>'row_sha256',
           item->>'staged_case_id', item->>'non_hearing_sha256', item->'raw_data', item->'normalized_data')
        ON CONFLICT (import_id, source_row_number) DO NOTHING;

        SELECT * INTO existing FROM court_data.source_rows
        WHERE import_id = p_import_id AND source_row_number = (item->>'source_row_number')::integer;
        IF existing.row_sha256 IS DISTINCT FROM item->>'row_sha256'
           OR existing.staged_case_id IS DISTINCT FROM item->>'staged_case_id'
           OR existing.non_hearing_sha256 IS DISTINCT FROM item->>'non_hearing_sha256'
           OR existing.raw_data IS DISTINCT FROM item->'raw_data'
           OR existing.normalized_data IS DISTINCT FROM item->'normalized_data' THEN
            RAISE EXCEPTION 'staged source row conflict';
        END IF;
    END LOOP;
    RETURN (SELECT count(*) FROM court_data.source_rows WHERE import_id = p_import_id);
END;
$$;

CREATE OR REPLACE FUNCTION court_data.finalize_import(p_import_id text)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = pg_catalog, court_data
AS $$
DECLARE
    target court_data.imports%ROWTYPE;
    actual_count bigint;
BEGIN
    SELECT * INTO target FROM court_data.imports WHERE import_id = p_import_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'unknown court import'; END IF;
    IF target.status = 'complete' THEN RETURN court_data.verify_import(p_import_id); END IF;

    SELECT count(*) INTO actual_count FROM court_data.source_rows WHERE import_id = p_import_id;
    IF actual_count <> target.expected_source_rows OR
       (SELECT min(source_row_number) FROM court_data.source_rows WHERE import_id = p_import_id) <> 1 OR
       (SELECT max(source_row_number) FROM court_data.source_rows WHERE import_id = p_import_id) <> target.expected_source_rows THEN
        RAISE EXCEPTION 'court import is incomplete';
    END IF;
    IF EXISTS (
        SELECT 1 FROM court_data.source_rows WHERE import_id = p_import_id
        GROUP BY staged_case_id HAVING count(DISTINCT non_hearing_sha256) > 1
    ) THEN RAISE EXCEPTION 'conflicting repeated case data'; END IF;

    INSERT INTO court_data.courts (court_id, court_code)
    SELECT DISTINCT normalized_data->'court'->>'id', normalized_data->'court'->>'court_code'
    FROM court_data.source_rows WHERE import_id = p_import_id
    ON CONFLICT DO NOTHING;

    INSERT INTO court_data.cases
      (case_id, court_id, case_number, case_type, file_date, style, cause_of_action, claim_amount, status)
    SELECT DISTINCT normalized_data->'case'->>'id', normalized_data->'court'->>'id',
      normalized_data->'case'->>'case_number', normalized_data->'case'->>'case_type',
      (normalized_data->'case'->>'file_date')::date, normalized_data->'case'->>'style',
      normalized_data->'case'->>'cause_of_action', (normalized_data->'case'->>'claim_amount')::numeric,
      normalized_data->'case'->>'status'
    FROM court_data.source_rows WHERE import_id = p_import_id
    ON CONFLICT DO NOTHING;

    IF EXISTS (
      SELECT 1 FROM court_data.source_rows sr JOIN court_data.cases c ON c.case_id = sr.staged_case_id
      WHERE sr.import_id = p_import_id AND (
        c.court_id IS DISTINCT FROM sr.normalized_data->'court'->>'id' OR
        c.case_number IS DISTINCT FROM sr.normalized_data->'case'->>'case_number' OR
        c.case_type IS DISTINCT FROM sr.normalized_data->'case'->>'case_type' OR
        c.file_date IS DISTINCT FROM (sr.normalized_data->'case'->>'file_date')::date OR
        c.style IS DISTINCT FROM sr.normalized_data->'case'->>'style' OR
        c.cause_of_action IS DISTINCT FROM sr.normalized_data->'case'->>'cause_of_action' OR
        c.claim_amount IS DISTINCT FROM (sr.normalized_data->'case'->>'claim_amount')::numeric OR
        c.status IS DISTINCT FROM sr.normalized_data->'case'->>'status')
    ) THEN RAISE EXCEPTION 'existing case conflicts with import'; END IF;

    INSERT INTO court_data.parties (party_id, case_id, role, ordinal, name)
    SELECT DISTINCT p->>'id', sr.staged_case_id, p->>'role', (p->>'ordinal')::smallint, p->>'name'
    FROM court_data.source_rows sr CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') p
    WHERE sr.import_id = p_import_id ON CONFLICT DO NOTHING;

    IF EXISTS (
      SELECT 1 FROM court_data.source_rows sr
      CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') x
      JOIN court_data.parties p ON p.party_id = x->>'id'
      WHERE sr.import_id = p_import_id AND (p.case_id IS DISTINCT FROM sr.staged_case_id OR
        p.role IS DISTINCT FROM x->>'role' OR p.ordinal IS DISTINCT FROM (x->>'ordinal')::smallint OR
        p.name IS DISTINCT FROM x->>'name')
    ) THEN RAISE EXCEPTION 'existing party conflicts with import'; END IF;

    INSERT INTO court_data.attorneys (attorney_id, represented_party_id, name)
    SELECT DISTINCT p->'attorney'->>'id', p->>'id', p->'attorney'->>'name'
    FROM court_data.source_rows sr CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') p
    WHERE sr.import_id = p_import_id AND jsonb_typeof(p->'attorney') = 'object'
    ON CONFLICT DO NOTHING;

    INSERT INTO court_data.addresses (address_id, party_id, line1, line2, city, state, postal_code)
    SELECT DISTINCT p->'address'->>'id', p->>'id', p->'address'->>'line1', p->'address'->>'line2',
      p->'address'->>'city', p->'address'->>'state', p->'address'->>'postal_code'
    FROM court_data.source_rows sr CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') p
    WHERE sr.import_id = p_import_id AND jsonb_typeof(p->'address') = 'object'
    ON CONFLICT DO NOTHING;

    INSERT INTO court_data.addresses (address_id, attorney_id, line1, line2, city, state, postal_code)
    SELECT DISTINCT p->'attorney'->'address'->>'id', p->'attorney'->>'id',
      p->'attorney'->'address'->>'line1', p->'attorney'->'address'->>'line2',
      p->'attorney'->'address'->>'city', p->'attorney'->'address'->>'state',
      p->'attorney'->'address'->>'postal_code'
    FROM court_data.source_rows sr CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') p
    WHERE sr.import_id = p_import_id AND jsonb_typeof(p->'attorney'->'address') = 'object'
    ON CONFLICT DO NOTHING;

    IF EXISTS (
      SELECT 1 FROM court_data.source_rows sr
      CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') p
      JOIN court_data.attorneys a ON a.attorney_id = p->'attorney'->>'id'
      WHERE sr.import_id = p_import_id AND jsonb_typeof(p->'attorney') = 'object'
        AND (a.represented_party_id IS DISTINCT FROM p->>'id' OR a.name IS DISTINCT FROM p->'attorney'->>'name')
    ) THEN RAISE EXCEPTION 'existing attorney conflicts with import'; END IF;

    IF EXISTS (
      SELECT 1 FROM court_data.source_rows sr
      CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') p
      JOIN court_data.addresses ad ON ad.address_id = p->'address'->>'id'
      WHERE sr.import_id = p_import_id AND jsonb_typeof(p->'address') = 'object'
        AND (ad.party_id IS DISTINCT FROM p->>'id' OR ad.attorney_id IS NOT NULL OR
          ad.line1 IS DISTINCT FROM p->'address'->>'line1' OR ad.line2 IS DISTINCT FROM p->'address'->>'line2' OR
          ad.city IS DISTINCT FROM p->'address'->>'city' OR ad.state IS DISTINCT FROM p->'address'->>'state' OR
          ad.postal_code IS DISTINCT FROM p->'address'->>'postal_code')
    ) THEN RAISE EXCEPTION 'existing party address conflicts with import'; END IF;

    IF EXISTS (
      SELECT 1 FROM court_data.source_rows sr
      CROSS JOIN LATERAL jsonb_array_elements(sr.normalized_data->'parties') p
      JOIN court_data.addresses ad ON ad.address_id = p->'attorney'->'address'->>'id'
      WHERE sr.import_id = p_import_id AND jsonb_typeof(p->'attorney'->'address') = 'object'
        AND (ad.attorney_id IS DISTINCT FROM p->'attorney'->>'id' OR ad.party_id IS NOT NULL OR
          ad.line1 IS DISTINCT FROM p->'attorney'->'address'->>'line1' OR
          ad.line2 IS DISTINCT FROM p->'attorney'->'address'->>'line2' OR
          ad.city IS DISTINCT FROM p->'attorney'->'address'->>'city' OR
          ad.state IS DISTINCT FROM p->'attorney'->'address'->>'state' OR
          ad.postal_code IS DISTINCT FROM p->'attorney'->'address'->>'postal_code')
    ) THEN RAISE EXCEPTION 'existing attorney address conflicts with import'; END IF;

    INSERT INTO court_data.hearings (hearing_id, case_id, description, hearing_date, hearing_time)
    SELECT DISTINCT normalized_data->'hearing'->>'id', staged_case_id,
      normalized_data->'hearing'->>'description', (normalized_data->'hearing'->>'hearing_date')::date,
      (normalized_data->'hearing'->>'hearing_time')::time
    FROM court_data.source_rows
    WHERE import_id = p_import_id AND jsonb_typeof(normalized_data->'hearing') = 'object'
    ON CONFLICT DO NOTHING;

    INSERT INTO court_data.dispositions (disposition_id, case_id, description, disposition_date)
    SELECT DISTINCT normalized_data->'disposition'->>'id', staged_case_id,
      normalized_data->'disposition'->>'description', (normalized_data->'disposition'->>'disposition_date')::date
    FROM court_data.source_rows
    WHERE import_id = p_import_id AND jsonb_typeof(normalized_data->'disposition') = 'object'
    ON CONFLICT DO NOTHING;

    INSERT INTO court_data.judgments
      (judgment_id, case_id, judgment_text, judgment_date, in_favor_of, against, judgment_amount,
       attorney_fees, court_costs, pre_judgment_interest_rate, post_judgment_interest_rate)
    SELECT DISTINCT normalized_data->'judgment'->>'id', staged_case_id,
      normalized_data->'judgment'->>'judgment_text', (normalized_data->'judgment'->>'judgment_date')::date,
      normalized_data->'judgment'->>'in_favor_of', normalized_data->'judgment'->>'against',
      (normalized_data->'judgment'->>'judgment_amount')::numeric,
      (normalized_data->'judgment'->>'attorney_fees')::numeric,
      (normalized_data->'judgment'->>'court_costs')::numeric,
      (normalized_data->'judgment'->>'pre_judgment_interest_rate')::numeric,
      (normalized_data->'judgment'->>'post_judgment_interest_rate')::numeric
    FROM court_data.source_rows
    WHERE import_id = p_import_id AND jsonb_typeof(normalized_data->'judgment') = 'object'
    ON CONFLICT DO NOTHING;

    INSERT INTO court_data.case_source_rows (import_id, source_row_number, case_id)
    SELECT import_id, source_row_number, staged_case_id FROM court_data.source_rows
    WHERE import_id = p_import_id ON CONFLICT DO NOTHING;

    IF (SELECT count(*) FROM court_data.case_source_rows WHERE import_id = p_import_id) <> target.expected_source_rows THEN
        RAISE EXCEPTION 'normalized provenance validation failed';
    END IF;
    UPDATE court_data.imports SET status = 'complete', completed_at = clock_timestamp()
    WHERE import_id = p_import_id;
    RETURN court_data.verify_import(p_import_id);
END;
$$;

ALTER TABLE court_data.imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.source_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.courts ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.parties ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.attorneys ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.addresses ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.hearings ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.dispositions ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.judgments ENABLE ROW LEVEL SECURITY;
ALTER TABLE court_data.case_source_rows ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES IN SCHEMA court_data FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA court_data FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA court_data TO service_role;
GRANT EXECUTE ON FUNCTION court_data.prepare_import(jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION court_data.stage_source_rows(text, jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION court_data.finalize_import(text) TO service_role;
GRANT EXECUTE ON FUNCTION court_data.verify_import(text) TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA court_data REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA court_data REVOKE ALL ON FUNCTIONS FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA court_data GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO service_role;
COMMIT;
