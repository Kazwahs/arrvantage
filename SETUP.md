# Setting up arrVantage

This walks through getting your own copy of arrVantage running, from forking the repo to configuring your first integration. It assumes basic comfort with Docker and a self-hosted environment (TrueNAS, Unraid, a bare Docker host — the container itself doesn't care which).

## What you'll need

- A GitHub account (for hosting your own copy and building the image automatically)
- A Docker host with persistent storage available for a small config file
- API keys/credentials for whichever services you want to connect (see the reference table near the end — you don't need all of them, just the ones you actually run)

## 1. Fork the repository

Fork [arrVantage](https://github.com/Kazwahs/arrvantage) into your own GitHub account. You'll deploy from your own fork, not the original — that way the automatic build pipeline publishes *your* image, and you're free to make your own tweaks without touching the upstream repo.

## 2. Let the build pipeline run once

arrVantage builds itself automatically — there's nothing to configure here beyond making sure it actually ran.

1. Any push to your fork's `main` branch triggers a GitHub Action (`.github/workflows/docker-build.yml`) that builds a Docker image and publishes it to GitHub Container Registry (ghcr.io).
2. On your fork's GitHub page, click the **Actions** tab. You should see "Build and Publish Docker Image" listed. If it hasn't run yet, push any small change (even just editing this file) to trigger it.
3. Once it finishes with a green checkmark, go to your GitHub profile's **Packages** tab (or check your fork's sidebar, which usually shows a Packages link once one exists). You'll see a new `arrvantage` package.
4. Open that package's settings and change its visibility to **Public**. This only affects the compiled image — your source code stays exactly as private or public as your fork already is. Making the image public means your Docker host can pull it without needing any registry credentials, which is by far the simplest path.

## 3. Deploy the container

You need a small persistent volume for `config.json` (your instances, users, and API keys — this is the only thing that needs to survive a redeploy) and one open port.

### Generic Docker Compose

```yaml
services:
  arrvantage:
    image: ghcr.io/<your-github-username>/arrvantage:latest
    pull_policy: always
    container_name: arrvantage
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      - ./arrvantage-data:/app/data
    environment:
      - CONFIG_DIR=/app/data
      - FORCE_HTTPS_COOKIES=false
```

Replace `<your-github-username>` with your actual GitHub username (lowercase). `pull_policy: always` is what makes future updates reliable — every redeploy checks the registry for the newest image instead of silently reusing whatever's cached locally.

### TrueNAS SCALE (Custom App)

1. Create a dataset for persistent storage first, e.g. `/mnt/yourpool/apps/arrvantage-data`.
2. In TrueNAS, go to **Apps → Discover Apps → Custom App**, and switch to YAML/Compose mode.
3. Use the same Compose block as above, but point the volume at your real dataset path and pick whatever host port fits your setup:

```yaml
    volumes:
      - /mnt/yourpool/apps/arrvantage-data:/app/data
```

4. Deploy. First boot creates `config.json` in that directory automatically.

**One thing worth knowing up front:** the container runs as a non-root user (UID 1000) for security. If you ever see a `PermissionError` writing to `config.json` — usually only after recreating the data directory from scratch — fix it with:

```
chown -R 1000:1000 /mnt/yourpool/apps/arrvantage-data
```

### A note on HTTPS

If you're putting arrVantage behind a reverse proxy with a real SSL certificate (recommended if you're exposing it beyond your own network), set `FORCE_HTTPS_COOKIES=true` once you've confirmed the proxy + certificate are working end-to-end. This makes the login cookie HTTPS-only. Leave it `false` for plain local-network HTTP access.

## 4. Updating arrVantage going forward

This is the whole reason the build pipeline exists — updates should never require a manual rebuild:

1. Make your changes (or pull upstream changes into your fork) and push to `main`.
2. GitHub Actions builds and publishes a new `:latest` image automatically — takes a couple of minutes, no action needed from you.
3. Redeploy the app in TrueNAS (or run `docker compose up -d` / re-pull on a generic host). `pull_policy: always` guarantees it picks up the new image every time.

## 5. First-run setup

Visit your arrVantage instance in a browser. Since no accounts exist yet, you'll land on a setup wizard:

1. Create your admin account (username + password).
2. Add your first *arr instance (Radarr, Sonarr, Lidarr, or a book-manager like Chaptarr) — you can add the rest later from Settings.
3. Finish the wizard, and you're in.

Everything else — media servers, downloaders, indexers, transcoders, request services, metadata providers — gets added afterward from **Settings** (the gear icon, admin-only), using the same pattern: give it a name, its URL, and its credential, then hit Test before Save.

## 6. Where to find each service's credentials

| Service | Where to find it |
|---|---|
| Radarr / Sonarr / Lidarr / Chaptarr | Settings → General → Security → API Key |
| Plex | Requires an `X-Plex-Token` — the community-standard way is signing into plex.tv and inspecting a request in your browser's network tab; search "how to find Plex token" for an up-to-date walkthrough, since Plex doesn't expose this directly in its own UI |
| Jellyfin / Emby | Dashboard → API Keys → generate a new key |
| Overseerr / Jellyseerr | Settings → General → API Key |
| qBittorrent | No API key — use your normal Web UI username and password |
| SABnzbd | Config → General → API Key |
| Prowlarr | Settings → General → Security → API Key |
| Tautulli | Settings → Web Interface → API Key |
| Tracearr | Settings → General → generate a Public API key (format `trr_pub_...`) |
| Tdarr | No auth by default on most installs — leave the field blank unless you've enabled it yourself |
| TMDB | themoviedb.org account → Settings → API → API Key |
| TVDB | thetvdb.com → Dashboard → API Keys (v4 key, plus an optional subscriber PIN if you have one) |
| Fanart.tv | fanart.tv account → API section |
| TheAudioDB | Works out of the box with the shared public test key already filled in — only needed if you have your own key |

None of these are required to get started — add only what you actually run, and expand later from Settings whenever you connect something new.

## Troubleshooting

- **"Internal Server Error" saving Settings** — almost always the non-root-user permission issue described in step 3. Check container logs for a `PermissionError` on `config.json`.
- **A redeploy doesn't seem to include your latest change** — confirm the GitHub Action actually finished (green checkmark) before redeploying; `pull_policy: always` only helps if a newer image genuinely exists to pull.
- **A specific integration behaves oddly** — check the container logs first. Several integrations in this project were built against best-informed guesses at undocumented API shapes and later corrected once real data was available; if something looks wrong, it's usually a fixable field-name mismatch, not a fundamental problem.
