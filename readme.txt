# arrVantage
 
> A unified web frontend for your media automation stack — arr apps, indexers, Seerr, media servers, metadata providers, and transcoders, all in one place.
 
<!-- TODO: screenshot or GIF of the dashboard goes here. This is the single highest-value addition — README traffic mostly bounces at "no picture." -->
<!-- ![arrVantage dashboard](docs/screenshot.png) -->
 
<!-- TODO: badges, once you have them — e.g. license, latest image tag, GHCR pulls -->
<!-- ![License](https://img.shields.io/github/license/Kazwahs/arrvantage) ![Docker](https://img.shields.io/badge/ghcr.io-arrvantage-blue) -->
 
## Status
 
Actively developed — expect rough edges and breaking changes between releases. Pin a specific image tag rather than tracking `latest` if you want stability.
 
## Features
 
<!-- TODO: tighten/confirm this list against what actually ships today -->
 
- Unified dashboard for your \*arr stack (Sonarr, Radarr, ... — TODO: list which)
- Indexer visibility (TODO: Prowlarr? Jackett?)
- Request management via Seerr (Overseerr / Jellyseerr — TODO: which)
- Media server status (Plex / Jellyfin / Emby — TODO: which)
- Metadata lookups (TODO: TMDB / TVDB / etc.)
- Transcode status (TODO: Tdarr / Unmanic / etc.)
## Installation
 
<!-- TODO: confirm image name/tag — inferred from the GHCR package page -->
 
```bash
docker run -d \
  --name arrvantage \
  -p 8080:8080 \
  -v /path/to/config:/config \
  ghcr.io/kazwahs/arrvantage:latest
```
 
Or with `docker-compose.yml`:
 
```yaml
services:
  arrvantage:
    image: ghcr.io/kazwahs/arrvantage:latest
    container_name: arrvantage
    ports:
      - "8080:8080"
    volumes:
      - /path/to/config:/config
    environment:
      # TODO: real env vars
      - TZ=America/Los_Angeles
    restart: unless-stopped
```
 
Then open `http://<your-server>:8080` and connect your services (API URLs + keys) from the settings page.
 
## Configuration
 
<!-- TODO: table of env vars / config options -->
 
| Variable | Description | Default |
|---|---|---|
| `TZ` | Timezone | `UTC` |
| ... | ... | ... |
 
## Why arrVantage?
 
I run the full \*arr stack, Seerr, a media server, and a transcoder, and every time I needed to check on one I had a browser tab open for each. What started as a batch file that just launched all those tabs at once slowly turned into the idea for a proper unified frontend — one place to see and manage everything instead of tab-hopping between five different UIs.
 
## A Note on How This Was Built
 
I'm in my mid-40s, work more than full-time, and have a family — I don't have the spare hours to learn a new stack from scratch right now. So I built arrVantage with Claude's help rather than writing every line by hand. Full transparency on that: the architecture decisions, the debugging, and the ongoing maintenance are mine, but a lot of the code itself came out of that collaboration. It's working well for me, and I'd rather ship something useful than wait until I had time to do it "the traditional way."
 
It's still evolving — I'm actively using it myself and tweaking it as I go. Feel free to try it out, and open an issue if something breaks or you'd like to see a feature added.
 
## Supported Integrations
 
<!-- TODO: real table once confirmed -->
 
| Category | Supported |
|---|---|
| \*Arr apps | ... |
| Indexers | ... |
| Requests | ... |
| Media servers | ... |
| Metadata | ... |
| Transcoding | ... |
 
## Roadmap
 
<!-- TODO -->
 
- [ ] ...
## Contributing / Feedback
 
Issues and feature requests are welcome — this is very much a living project. <!-- TODO: link to Issues, note if PRs are wanted -->
 
## License
 
<!-- TODO: pick and state a license (MIT is common for self-hosted tools like this) -->
 
## Acknowledgments
 
Built with the help of [Claude](https://claude.ai).