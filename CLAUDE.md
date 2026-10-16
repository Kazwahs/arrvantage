# arrVantage — Project Context

A unified web dashboard for a self-hosted media automation stack (Radarr/Sonarr/Lidarr/Chaptarr, Plex/Jellyfin/Emby, Prowlarr, qBittorrent/SABnzbd, Overseerr/Jellyseerr, Tdarr). Single Flask app, single container, no database — built collaboratively with Claude over an extended project. This file exists so Claude Code starts with real, hard-won context instead of rediscovering it.

## Architecture

- **`app.py`** — the entire backend, single file, Flask + gunicorn.
- **No database.** All config (instances, users, API keys) lives in one `config.json`, path controlled by `CONFIG_DIR` env var. Loaded once into module-level globals (`CONFIG`, `APPS`, `USERS`, etc.) at import time, mutated in place when Settings is saved.
- **Single gunicorn worker, deliberately.** This was a real bug fix, not an oversight — `CONFIG`/`USERS` are per-process in-memory state. With 2+ workers, completing the setup wizard on one worker left other workers still thinking no users existed, causing an infinite `/setup` ↔ `/login` redirect loop on fresh installs. Do not raise `--workers` above 1 without solving that properly first (e.g. moving shared state to the file/a real store).
- **CSRF via one global `before_request` hook**, not per-route decorators — covers every POST automatically. Frontend's global `fetch()` override attaches the token as `X-CSRF-Token` header for JS calls; traditional forms use a hidden field.
- **`/health` is the only endpoint safe for Docker/TrueNAS healthchecks.** `/login` and `/setup` both redirect depending on setup state, so neither is a safe target — this was a real production bug on the TrueNAS catalog listing before `/health` was added.
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
