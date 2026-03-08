# Flow2API Token Updater v3.3

Flow2API Token Updater is a lightweight multi-profile token refresh tool.
It uses Playwright persistent browser profiles to keep Google Labs login
sessions alive, extracts session tokens when needed, and pushes them to
Flow2API.

This version focuses on three things:

- multi-profile account management
- per-profile Flow2API target override
- a realtime dashboard with charts and recent activity

## Highlights

- Lightweight runtime: VNC/Xvfb/noVNC only starts when login is needed
- Smart sync: profiles are grouped by effective Flow2API URL and token
- Per-profile overrides: each profile can override target URL and token
- Proxy support: each profile can use its own proxy
- Cookie import: recover login state without opening the browser
- Realtime dashboard: SSE-first updates with polling fallback
- Chart ranges: 6h / 24h / 72h / 7d
- Built-in analytics: sync activity, failure reasons, target distribution

## How it works

1. Each account is stored as an isolated Profile.
2. Browser state is persisted in `profiles/`.
3. During sync, the effective target is resolved as:
   - `profile.flow2api_url` or global `FLOW2API_URL`
   - `profile.connection_token_override` or global `CONNECTION_TOKEN`
4. Active profiles are grouped by effective target URL + effective token.
5. Each group calls Flow2API `check-tokens` first.
6. Only profiles that need refresh are synced.
7. If target-side checking fails, the group falls back to force sync.
8. Every sync result is stored as history for the dashboard.

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/genz27/flow2api_tupdater.git
cd flow2api_tupdater
cp .env.example .env
```

At minimum, set these values in `.env`:

- `ADMIN_PASSWORD`
- `FLOW2API_URL`
- `CONNECTION_TOKEN`

### 2. Start the service

```bash
docker compose up -d --build
```

### 3. Access the app

- Admin UI: `http://localhost:8002`
- noVNC: `http://localhost:6080/vnc.html`

> Port `6080` is only relevant when VNC login is enabled and in use.

## Common workflows

### Workflow A: login via VNC

1. Open the admin UI.
2. Configure the global default Flow2API URL and token.
3. Create a Profile.
4. Click `Login` to launch the browser.
5. Finish Google login in noVNC.
6. Click `Close Browser` to persist the session state.
7. Run one manual sync to verify the account works.
8. Let the scheduled job handle later refreshes.

### Workflow B: import cookies

1. Create a Profile.
2. Open the `Cookie` dialog.
3. Paste cookie JSON for the `labs.google` domain.
4. Run `Check Login` or `Sync` to validate the imported session.

### Multi-instance Flow2API setup

If one account should sync to another Flow2API instance:

1. Open that profile's edit dialog.
2. Set `Flow2API URL override`.
3. If the target instance uses a different token, also set
   `Connection Token override`.
4. Save the profile. That profile will now prefer the override values.

## Dashboard

The admin dashboard includes:

- overview metrics
- sync activity chart with time-range switching
- status breakdown and profile ranking
- failure reason aggregation
- target instance distribution
- recent activity feed
- realtime connection state

The frontend prefers SSE updates and automatically falls back to light
polling if the realtime stream is unavailable.

## Persistence

The default `docker-compose.yml` mounts these directories:

- `./data` -> `/app/data`
  - `profiles.db`: profile data and sync history
  - `config.json`: persisted global defaults
- `./profiles` -> `/app/profiles`
  - Playwright persistent browser profile data
- `./logs` -> `/app/logs`
  - runtime logs

## Environment variables

The following options are actively used by the application:

| Variable | Description | Default |
|----------|-------------|---------|
| `ADMIN_PASSWORD` | Admin UI password | empty |
| `API_KEY` | External API key | empty |
| `FLOW2API_URL` | Global default Flow2API URL | `http://host.docker.internal:8000` |
| `CONNECTION_TOKEN` | Global default Flow2API token | empty |
| `REFRESH_INTERVAL` | Scheduled refresh interval in minutes | `60` |
| `SESSION_TTL_MINUTES` | Admin session TTL, `0` means no expiry | `1440` |
| `CONFIG_FILE` | Path for persisted global config | `/app/data/config.json` |
| `API_PORT` | HTTP listen port | `8002` |
| `ENABLE_VNC` | Enable VNC login entry, `1/0` | `1` |
| `VNC_PASSWORD` | Password for noVNC / x11vnc | `flow2api` |

### Configuration precedence

Effective target resolution follows this order:

1. Profile-level `flow2api_url`
2. Global `FLOW2API_URL`

And for the token:

1. Profile-level `connection_token_override`
2. Global `CONNECTION_TOKEN`

## API reference

### Admin UI API

Used by the web dashboard:

- `POST /api/login`
- `POST /api/logout`
- `GET /api/auth/check`
- `GET /api/status`
- `GET /api/dashboard?hours=6|24|72|168`
- `GET /api/dashboard/stream?session_token=...`
- `GET /api/config`
- `POST /api/config`
- `GET /api/profiles`
- `POST /api/profiles`
- `GET /api/profiles/{id}`
- `PUT /api/profiles/{id}`
- `DELETE /api/profiles/{id}`
- `POST /api/profiles/{id}/launch`
- `POST /api/profiles/{id}/close`
- `POST /api/profiles/{id}/check-login`
- `POST /api/profiles/{id}/import-cookies`
- `POST /api/profiles/{id}/extract`
- `POST /api/profiles/{id}/sync`
- `POST /api/sync-all`

### External API

These endpoints require `X-API-Key`:

- `GET /v1/profiles`
- `GET /v1/profiles/{id}/token`
- `POST /v1/profiles/{id}/sync`
- `GET /health`

## Upgrade notes

### Upgrading to v3.3

v3.3 adds:

- profile-level target URL override
- profile-level token override
- sync history storage
- realtime dashboard and SSE stream
- failure reason aggregation
- target instance distribution
- dashboard time-range filters

Recommended upgrade steps:

1. Back up `data/` and `profiles/`.
2. Pull the latest code.
3. Rebuild and restart the container.
4. The app will auto-create new columns and history tables if needed.
5. Re-check global defaults in the admin UI.
6. Re-check profile-level overrides if you use multiple Flow2API targets.

## Troubleshooting

### Sync says: incomplete Flow2API URL or token

The effective target config is incomplete. Check:

- the global default target settings
- the profile-level URL override
- the profile-level token override

A profile that points to another Flow2API instance usually also needs a
matching token override.

### Sync says: failed to extract token

The stored browser session is no longer usable. Try one of these:

- login again through VNC
- import a fresh cookie set
- run `Check Login` before syncing again

### noVNC does not work

Check:

- `ENABLE_VNC=1`
- port `6080` is mapped
- you actually clicked the profile `Login` button

### Changed API_PORT but cannot access the app

If you change the application listen port, make sure you also update the
port mapping in `docker-compose.yml`.

## License

MIT
