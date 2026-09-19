# Oasis login

Live test site: **https://oasis.carlunpen.com**. Cloudflare manages a DNS-only A record; Caddy on DigitalOcean terminates HTTPS and renews its certificate. Gunicorn listens only on loopback. No Cloudflare zone-wide settings were changed.

A small Flask application with an Oasis-themed login page. Existing users sign in directly from the browser to Supabase Auth with email and password. The resulting access token exists only in a JavaScript variable: it is not written to local/session storage or a cookie, and a reload or **Sign out** clears it.

The protected `GET /api/me` endpoint does not decode or locally trust JWTs. It sends the bearer token to Supabase `GET /auth/v1/user`, which performs online validation for current and legacy tokens.

## Configuration

Copy the example and provide the public project values:

```sh
cp .env.example .env
```

- `SUPABASE_URL`: project URL, such as `https://example.supabase.co`
- `SUPABASE_PUBLISHABLE_KEY`: Supabase publishable key (a legacy public `anon` key also works)

Both values are intentionally public and are returned by `GET /api/config` so the browser can call Supabase. **Do not use a `service_role`, secret API key, database password, or JWT signing secret.** This application requires Row Level Security and other Supabase authorization policies to be configured appropriately for the public key.

The server rejects non-HTTPS Supabase URLs and refuses to publish secret/service-role or unrecognized key formats. Legacy key role inspection is only a configuration safety check; user authentication always uses Supabase online validation.

The application does not automatically load `.env`; source it into the process environment or use the systemd `EnvironmentFile` shown below.

## Local setup

Python 3.11 or newer is recommended.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
set -a; . ./.env; set +a
python server.py
```

Open <http://127.0.0.1:8000>. The built-in server binds only to localhost and explicitly disables debug mode.

## Endpoints

- `GET /` — login page
- `GET /api/config` — browser-safe public Supabase configuration
- `GET /api/me` — protected profile; requires `Authorization: Bearer <access-token>`
- `GET /healthz` — local process liveness, with no upstream dependency
- `GET /readyz` — bounded live check of Supabase Auth at `/auth/v1/health`

Authentication failures return `401`; connectivity, malformed upstream responses, and unexpected upstream statuses return a generic `503`. Responses have `Cache-Control: no-store`, a restrictive Content Security Policy, and other browser security headers. Credentials and tokens are never explicitly logged by application code.

## Tests

The suite mocks all Supabase calls:

```sh
python -m unittest discover -s tests -v
```

## Production deployment

Install the application and virtual environment under `/opt/oasis`, create an unprivileged `oasis` user, and place public configuration in `/etc/oasis.env`:

```text
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_PUBLISHABLE_KEY=your-publishable-or-anon-key
```

Protect the environment file even though these values are public configuration:

```sh
sudo chown root:root /etc/oasis.env
sudo chmod 0600 /etc/oasis.env
sudo cp deploy/oasis.service /etc/systemd/system/oasis.service
sudo systemctl daemon-reload
sudo systemctl enable --now oasis
```

The sample unit runs Gunicorn as the non-root `oasis` account and binds to `127.0.0.1:8000`:

```sh
gunicorn --bind 127.0.0.1:8000 --workers 2 --threads 4 --timeout 30 --access-logfile - --error-logfile - server:app
```

Use `/opt/oasis/.venv` for the production virtual environment and install `requirements.txt` there. Keep application files owned by root and readable by the `oasis` group; the service must not be able to modify its own code. systemd reads the root-only environment file before dropping privileges.

`deploy/Caddyfile` supplies the TLS reverse proxy. Install Caddy, point the hostname at the VPS, allow inbound ports 80/443, then validate and reload:

```sh
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy forwards the original scheme; Gunicorn trusts forwarded HTTPS only from its default loopback trusted proxy, enabling Flask HSTS. Do not expose port 8000 or enable Flask debug mode. Probe `/healthz` for liveness and `/readyz` for readiness.

## First account and scope

In the `news-agents` Supabase project, open **Authentication → Users → Add user** and create an email/password user (confirm it for testing). Your Supabase dashboard account is not automatically an app user. There is no public signup, password reset, or persistent/refresh session UI in this minimal demo. Sign out clears local memory; it does not globally revoke other sessions.

This connects DigitalOcean to Supabase **Auth**. No application database tables or court records have been imported.

## Live browser verification

`tests/live_smoke.cjs` is opt-in: it creates one randomly named, confirmed test user without sending email, tests HTTPS and actual browser login, and deletes that exact user in `finally`. It uses the already-authenticated Supabase CLI; privileged keys remain in the test process only and are not deployed. Only signed-out screenshots are saved under `artifacts/`.

```sh
SUPABASE_PROJECT_REF=your-project-ref \
PLAYWRIGHT_MODULE=/absolute/path/to/playwright \
CHROME_EXECUTABLE=/usr/bin/google-chrome \
node tests/live_smoke.cjs
```

Verified: 18 mocked backend tests locally and on the VPS; HTTPS certificate validation and redirect; HSTS; Supabase readiness; real wrong-password rejection, browser sign-in and protected profile; missing/invalid bearer rejection; logout/reload clearing; memory-only tokens; desktop/mobile layout. The temporary test account was deleted. A later full-browser rerun was blocked before user creation by HTTP 500 from Supabase's management API while retrieving test keys. Final public checks still returned 200 for `/`, `/healthz`, and `/readyz`, and 401 for unauthenticated `/api/me`; the final form POST fallback is covered by the unit suite.
