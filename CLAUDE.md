# arrVantage — Project Context

A unified web dashboard for a self-hosted media automation stack (Radarr/Sonarr/Lidarr/Chaptarr, Plex/Jellyfin/Emby, Prowlarr, qBittorrent/SABnzbd, Overseerr/Jellyseerr, Tdarr). Single Flask app, single container, no database — built collaboratively with Claude over an extended project. This file exists so Claude Code starts with real, hard-won context instead of rediscovering it.

## Architecture

- **`app.py`** — the entire backend, single file, Flask + gunicorn.
- **No database.** All config (instances, users, API keys) lives in one `config.json`, path controlled by `CONFIG_DIR` env var. Loaded once into module-level globals (`CONFIG`, `APPS`, `USERS`, etc.) at import time, mutated in place when Settings is saved.
- **Single gunicorn worker, deliberately.** This was a real bug fix, not an oversight — `CONFIG`/`USERS` are per-process in-memory state. With 2+ workers, completing the setup wizard on one worker left other workers still thinking no users existed, causing an infinite `/setup` ↔ `/login` redirect loop on fresh installs. Do not raise `--workers` above 1 without solving that properly first (e.g. moving shared state to the file/a real store).
- **CSRF via one global `before_request` hook**, not per-route decorators — covers every POST automatically. Frontend's global `fetch()` override attaches the token as `X-CSRF-Token` header for JS calls; traditional forms use a hidden field.
- **`/health` is the only endpoint safe for Docker/TrueNAS healthchecks.** `/login` and `/setup` both redirect depending on setup state, so neither is a safe target — this was a real production bug on the TrueNAS catalog listing before `/health` was added.
- **The main page is only a shell.** `index()` does no network calls - it renders the sidebar and Home from config. Each instance's panels (library, request, queue, history, system) come from `GET /fragment/instance/<name>` (`templates/partials/instance_panels.html`), fetched by `ensureInstanceLoaded()` the first time an instance is opened. Don't put per-instance data back into `index.html`; it used to ship every library in the page (multiple MB) and fetch everything serially before the first byte.
- **Independent external calls run concurrently** via `run_concurrently()` (shared `FETCH_POOL`, per-batch timeout, never raises - a failed call becomes `(False, "error text")`). Use it for any new endpoint that needs several upstream calls.
- **Home leads with `/api/attention`.** `summarize_attention()` (pure, unit-tested) merges the same warning across apps by health `source`, collapses a disk several apps share into one item, flags stalled/failed queue items and fully unreachable instances, and ranks critical before warning. The sidebar dot only appears for critical items or stalled downloads - never for a plain health warning. Results are cached ~30s (`?refresh=1` bypasses).
- **Home is a stack of keyed panels users can rearrange.** `#home-panels` holds one `.home-panel[data-panel=KEY]` per entry in `HOME_PANELS` (attention, libraries, mediaservers, indexers, transcoders); `loadHomeStats()` and `renderAttention()` only fill each panel's body, never replace the panels, so a user's order survives refreshes. The layout (`{"order": [...], "hidden": [...]}`) is stored per user as `home_layout` on their record in `config.json`, embedded in the page as JSON, and saved through `POST /api/home/layout` (`{"reset": true}` clears it). Everything passes through `normalize_home_layout()`, so unknown/duplicate keys are dropped and a panel added later appears at the end. `parse_users()` carries `home_layout` forward - Settings rebuilds every user from the form and would otherwise wipe it. To add a Home panel: add its key to `HOME_PANELS`, a `<section class="home-panel" data-panel data-label>` in `index.html`, and fill its body from JS.
- **Short in-process caches** (`_ATTENTION_CACHE`, `_LIBRARY_CACHE`) are per-process state like `CONFIG` - fine with the single worker, but another reason not to raise `--workers`. Library errors are never cached.
- Every category (instances, downloaders, indexers, media servers, transcoders) follows the same shape: a list in `config.json`, a `get_X(name)` lookup helper, a `fetch_X_status()` function, Settings UI support via the generic `parse_indexed_entries()` form parser.

## Working conventions from this project (please keep following these)

- **Verify API endpoints against real specs/responses before implementing — don't guess and ship.** Several integrations were rebuilt after guesses turned out wrong (Prowlarr's real test endpoint needed the actual OpenAPI spec; qBittorrent v5.0 renamed pause/resume to stop/start; Plex's bare API root serves XML, not its web UI). When a new integration comes up, look for the real API docs or a live response before writing the request code.
- **Real logos come from `homarr-labs/dashboard-icons`** (CDN via jsDelivr), not hand-drawn SVGs — check that project's actual catalog before assuming an icon exists in a given format (Tdarr, for example, is PNG-only there, no SVG).
- **Don't assume symmetry between similar-looking integrations.** Jellyfin/Emby share an API and both auto-redirect their bare URL to their web UI; Plex does neither of those — needs its own code paths and a `/web/index.html` suffix for direct links.
- **When something breaks, ask for the actual error/log output before proposing a fix.** This project's real bugs were almost always found this way (CI logs, browser DevTools Network tab, container logs) rather than guessed at.

## UI conventions

- **Shared styling lives in `static/css/`, not in templates.** `app.css` is linked by every page (login, setup, settings, dashboard) and holds only what they genuinely share: the `@font-face` rules, the design tokens (`:root` custom properties), the base reset, the one focus ring, `.sr-only`, the reduced-motion block, and `.test-btn`. `dashboard.css` is everything specific to the main dashboard. Login/setup/settings still keep their own page-specific rules in a `<style>` block.
- **Use the tokens.** Colours, spacing (`--space-*`), type sizes (`--text-*`), radii, z-layers (`--z-modal`, `--z-rail`) and motion (`--dur-fast`, `--ease-out`) are all defined in `app.css`. Don't reintroduce a copy of `:root` in a template, and don't pick a raw z-index number.
- **Link stylesheets with `asset_url('css/app.css')`**, not a bare `/static/...` path. It appends `?v=<file mtime>` so a redeployed stylesheet isn't served stale from browser cache. (The font preload is the one exception - it must match the URL inside the CSS exactly, so it stays unversioned.)
- **No inline `style=` except for values computed at runtime** (a progress bar's width, a per-instance colour, a JS-toggled `display`). Repeated styling becomes a class.
- **Status colours (green/amber/red) are reserved for status** and always come with a word or icon, never colour alone (see the disk-space "Low"/"Critically low" labels).
- **Clickable non-button elements** (cover cards, the sidebar logo) need `role="button"`/`role="link"` plus `tabindex="0"`; one delegated keydown handler in `index.html` makes Enter/Space activate them. Prefer a real `<button>` or `<a>` whenever possible.
- **Fonts are bundled** (`static/fonts/`: Geist and Geist Mono, variable woff2) so the UI looks the same on an offline/LAN-only install. They are SIL OFL 1.1; keep `OFL.txt` next to them.
- Still plain CSS with no build step - keep it that way unless there's a strong reason.

## Known field-name uncertainties (flagged in code comments too)

These were educated guesses at the time and haven't been independently reconfirmed — if something related misbehaves, check the raw JSON response first:
- Radarr cast `coverType` value for headshots (assumed `"headshot"`)
- TVDB character field names (`personName`, etc.)
- Tautulli field names throughout (dormant code path — only used when Tracearr isn't configured)
- Fanart.tv/TheAudioDB field names for Lidarr artist enrichment

## Deployment

- GitHub Actions (`.github/workflows/docker-build.yml`): tests must pass before any image builds. Push to `main` → `:latest` + `:sha-xxxxx`. A published GitHub Release → additionally tags the image with that exact version (e.g. `v1.0.4`), used for pinned deployments like the TrueNAS catalog listing.
- Published to `ghcr.io/kazwahs/arrvantage`.
- **Listed in the TrueNAS SCALE Community app catalog** (`truenas/apps` repo, `ix-dev/community/arrvantage/`). That's a genuinely separate repo/PR with its own file format (Jinja2 templates, `questions.yaml`, etc.) — don't confuse changes needed there with changes to this repo. Version bumps there are picked up automatically by their Renovate bot (daily cron), but still need human review/merge on their end.
- Automated test suite in `tests/` (pytest) — isolates `CONFIG_DIR` to a temp directory per test so nothing touches real config, uses `requests-mock` for external API calls.

## Current state

v1.0.4 is the last confirmed-stable release as of this file being written. Reliability pass completed (qBittorrent v5 endpoint fix, Tdarr logo format, Recently Added deduplication, single-worker fix, media server "open directly" links). TrueNAS catalog submission merged and live.
