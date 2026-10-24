# arrVantage

> A unified web frontend for your media automation stack — arr apps, indexers, Seerr, media servers, metadata providers, and transcoders, all in one place.

![Build](https://github.com/Kazwahs/arrvantage/actions/workflows/docker-build.yml/badge.svg) ![Latest release](https://img.shields.io/github/v/release/Kazwahs/arrvantage) ![License](https://img.shields.io/github/license/Kazwahs/arrvantage) ![Image](https://img.shields.io/badge/ghcr.io-kazwahs%2Farrvantage-blue)

![arrVantage Home: what is coming up, anything that needs attention, and your libraries](docs/screenshot.png)

## Status

Actively developed — expect rough edges and breaking changes between releases. Pin a specific image tag rather than tracking `latest` if you want stability.

## Features

- Unified dashboard for your \*arr stack (Sonarr, Radarr, Lidarr, Readarr (Tested with Bookshelf and then Chaptarr), Kapowarr)
- **Needs attention** on Home: stalled or failed downloads, unreachable apps and services, health warnings and low disk space, merged so the same problem is shown once. Warnings you do not care about can be hidden; outages cannot
- **Coming Up** on Home: the next 7, 14 or 30 days of episodes (Sonarr), movie releases (Radarr) and albums (Lidarr), colour-coded with each instance's own colour
- Indexer visibility (Prowlarr)
- Request management via Seerr (Overseerr / Jellyseerr / Seerr)
- Media server status (Plex / Jellyfin / Emby / Komga)
- Metadata lookups (TMDB / TVDB / Fanart.tv)
- Transcode status (Tdarr)
- Your own Home: drag the panels, and the cards inside them, into the order you want, and hide the ones you do not use. It is saved per user
- Eleven themes and a choice of accent colours, also per user
- Several users, with an administrator role and separate permissions for requesting and searching
- **Backup and restore** from Settings, and a **Copy diagnostics** button for bug reports (see below)
- One small container, no database: everything lives in a single `config.json`

## Getting Started

**Just want to run it?** Use the quick install below.

**Want to fork it, customize it, or build your own image?** See [SETUP.md](SETUP.md) for the full walkthrough — forking the repo, the automatic build pipeline, deployment on a generic Docker host or TrueNAS specifically, and a complete reference table of where to find every integration's API key.

## Installation

arrVantage is also available as an app in the TrueNAS SCALE community catalog. Everywhere else it is a single Docker container:

```bash
docker run -d \
  --name arrvantage \
  -p 5000:5000 \
  -v /path/to/config:/app/data \
  ghcr.io/kazwahs/arrvantage:latest
```

Or with `docker-compose.yml`:

```yaml
services:
  arrvantage:
    image: ghcr.io/kazwahs/arrvantage:latest
    pull_policy: always
    container_name: arrvantage
    ports:
      - "5000:5000"
    volumes:
      - /path/to/config:/app/data
    environment:
      - CONFIG_DIR=/app/data
      - FORCE_HTTPS_COOKIES=false
      # - LOG_LEVEL=DEBUG   # uncomment while troubleshooting
    restart: unless-stopped
```

Then open `http://<your-server>:5000` and connect your services (API URLs + keys) from the settings page. There is an initial setup wizard that will run for you to set up your admin account.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `CONFIG_DIR` | Where `config.json` (instances, users, API keys) is read from and written to inside the container. Must point at your mounted volume, or settings won't survive a restart. | `/app/data` |
| `FORCE_HTTPS_COOKIES` | Set to `true` once your reverse proxy + SSL certificate are confirmed working end-to-end, to make the login cookie HTTPS-only. Leave `false` for plain local-network HTTP access. | `false` |
| `PORT` | The port arrVantage listens on inside the container. If you change it, change the right-hand side of the port mapping to match. | `5000` |
| `LOG_LEVEL` | How much arrVantage writes to its log: `WARNING` (default), `INFO`, `DEBUG` or `ERROR`. See [Logs](#logs) below. | `WARNING` |

## Logs

arrVantage writes its log to the container's standard output, so there is no log file to find or clean up: use `docker logs arrvantage` (add `-f` to follow it live), or the **Logs** view of the app in TrueNAS.

By default it is quiet and only reports things that need attention: a call to one of your services that failed (the service's host and path, plus the HTTP status or the kind of connection error), a Settings **Test** that failed, a failed sign-in, and a `config.json` that could not be read. A service that stays down is reported once every five minutes rather than every few seconds.

If something misbehaves, set `LOG_LEVEL=DEBUG` and restart the container; every call arrVantage makes to your services is then listed, with the status and how long it took. Please include those lines when you open an issue.

API keys, tokens, passwords and query strings are never written to the log, so the lines are safe to paste into an issue. They do contain the addresses of your servers (for example `192.168.1.20:8096`), so blank those out if you would rather not share them.

## Backup, restore and diagnostics

Everything is under **Settings → Backup, restore and diagnostics** (administrators only).

- **Export settings** downloads your configuration as a JSON file. By default the file leaves out API keys and password hashes, so it is safe to share or keep in a repository. Tick *Include API keys and password hashes* for a full backup, and treat that file like a password.
- **Restore from a file** shows exactly what a file would change before anything happens. Confirming it first saves your current settings next to `config.json` as `config.backup-<date>.json` (the newest ten are kept), then replaces them. A file exported without keys keeps the keys and accounts you already have set up. Your session key is never exported or replaced.
- **Copy diagnostics** puts a summary of your setup on the clipboard for a bug report: the build, the versions of its main libraries, which kinds of services you run (counts only) and the latest warnings. It never contains API keys, passwords, usernames or the names you gave your services, and server addresses are hidden unless you untick the box.

## Why arrVantage?

I run the full \*arr stack, Seerr, a media server, and a transcoder, and every time I needed to check on one I had a browser tab open for each. What started as a batch file that just launched all those tabs at once slowly turned into the idea for a proper unified frontend — one place to see and manage everything instead of tab-hopping between five different UIs.

## A Note on How This Was Built

I'm in my mid-40s, work more than full-time, and have a family — I don't have the spare hours to learn a new stack from scratch right now. So I built arrVantage with Claude's help rather than writing every line by hand. Full transparency on that: the architecture decisions, the debugging, and the ongoing maintenance are mine, but a lot of the code itself came out of that collaboration. It's working well for me, and I'd rather ship something useful than wait until I had time to do it "the traditional way."

It's still evolving — I'm actively using it myself and tweaking it as I go. Feel free to try it out, and open an issue if something breaks or you'd like to see a feature added.

## Supported Integrations

| Category | Supported |
|---|---|
| \*Arr apps | Radarr (movies), Sonarr (TV), Lidarr (music), Chaptarr / Readarr-style book managers, Kapowarr (comics) |
| Indexers | Prowlarr — indexer health status, individual and bulk testing |
| Downloaders | qBittorrent, SABnzbd |
| Requests | Overseerr, Jellyseerr |
| Media servers | Plex, Jellyfin, Emby — Now Playing, Recently Added, Watch History, Users, library rescan. Komga for comics and manga libraries |
| Activity data | Tautulli and/or Tracearr, for richer Now Playing / Watch History / Users than the media servers' own APIs provide alone |
| Metadata | TMDB, TVDB, Fanart.tv, TheAudioDB |
| Transcoding | Tdarr — stats overview, space saved, per-node worker/queue status |

## Roadmap

What exists today, and what is on the list. The unchecked items are ideas, not promises, and the order is not a schedule.

- [x] Needs attention, with hideable warnings
- [x] Themes and accent colours
- [x] A Home you can rearrange
- [x] Coming Up calendar
- [x] Settings backup and restore, and copyable diagnostics
- [ ] Push alerts (ntfy, Discord or a webhook) when something breaks, so you do not have to have the dashboard open
- [ ] A Downloads panel on Home: qBittorrent and SABnzbd speeds and active queue in one place
- [ ] A short history of when each service went down and came back
- [ ] Book releases in Coming Up (Readarr / Chaptarr), once their calendar API has been checked against a real server
- [ ] Support for more book and comic library apps (BookOrbit is on the list)
- [ ] Show the version in the About panel and in diagnostics

## Contributing / Feedback

Issues and feature requests are welcome at [github.com/Kazwahs/arrvantage/issues](https://github.com/Kazwahs/arrvantage/issues) — this is very much a living project.

If something is not working, the most useful things you can include are the output of **Copy diagnostics** (Settings → Backup, restore and diagnostics) and the container's log. arrVantage logs to the container's standard output (`docker logs arrvantage`, or the Logs view of the app in TrueNAS) and only reports problems by default; set `LOG_LEVEL=DEBUG` and reproduce the problem to see every call it makes. API keys and passwords are never written to the log.

Pull requests are welcome too. For anything bigger than a small fix, please open an issue first so we can talk it through.

## License

Licensed under the [GNU General Public License v3.0](LICENSE) — the same license used by Sonarr, Radarr, and Prowlarr. In short: you're free to use, modify, and redistribute arrVantage, but any distributed modified version must also be open source under GPLv3.

## Acknowledgments

Built with the help of [Claude](https://claude.ai).