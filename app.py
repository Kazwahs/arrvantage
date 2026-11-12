"""
app.py

A web dashboard for your arr stack: per-instance Library, Queue,
History, and Request views.

Instances, media servers, and requesters all live in config.json, not
in this file - manage them via the Settings page in the UI (the gear
icon) rather than editing code. You can add multiple instances of the
same kind (e.g. two Radarr-type servers), multiple media servers
(Plex/Jellyfin/Emby), and multiple requesters (Overseerr/Jellyseerr) -
though only the first enabled requester is used for the Request tab
at a time.
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response
from urllib.parse import quote, urlencode, urlsplit
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta, timezone
import collections
import copy
import email.message
import email.utils
import hashlib
import html
import json
import logging
import os
import platform
import re
import secrets
import smtplib
import socket
import ssl
import sys
import requests
import concurrent.futures
import threading
import time

# The release this code belongs to. Change it when you cut a release, in the
# same commit, so the image, the About panel, Copy diagnostics and the GitHub
# release tag all say the same thing. (ARRVANTAGE_VERSION in the environment
# wins, for a build that wants to stamp its own - the image does not today.)
APP_VERSION = "1.5.0"


def resolve_app_version(raw=None) -> str:
    """APP_VERSION, unless the environment names one: 'v1.2.3' and '1.2.3'
    both read as 1.2.3. Anything that isn't a plain version is ignored, so a
    stray value can never put odd text into the page."""
    text = str(raw if raw is not None else os.environ.get("ARRVANTAGE_VERSION") or "").strip()
    text = text[1:] if text[:1] in ("v", "V") else text
    return text if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]{1,30})?", text) else APP_VERSION


app = Flask(__name__)

# Trusts X-Forwarded-* headers from exactly one reverse proxy hop in
# front of this app - needed so Flask knows the original request was
# HTTPS (for correct redirects and secure cookies) even though the
# proxy talks to this container over plain HTTP internally. Only safe
# because this app is meant to sit behind a reverse proxy, never
# exposed directly.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Set FORCE_HTTPS_COOKIES=true once your reverse proxy + SSL is
# confirmed working, so the browser refuses to ever send the login
# cookie over plain HTTP. Defaults off so the app still works before
# that's set up.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FORCE_HTTPS_COOKIES", "false").lower() == "true"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
# Everything goes to stderr, so it shows up in `docker logs` and in the
# TrueNAS app's Logs view - there is no log file to manage. Quiet by default
# (WARNING: failed calls to your services, failed logins, a config that
# couldn't be read). Set LOG_LEVEL=DEBUG to also see every outbound call.
#
# Never log a secret: API keys, tokens and passwords travel in headers,
# request bodies and query strings, so a request is only ever described by
# method, host and path, and anything that quotes a URL (a requests error
# does) goes through scrub_log_text() first.

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
LOG_REPEAT_SECONDS = 300  # an identical warning is shown once per this long


def resolve_log_level(raw) -> int:
    """LOG_LEVEL=debug|info|warning|error (any case). Anything else, or
    nothing, means WARNING - a typo must not silence or flood the log."""
    level = logging.getLevelName(str(raw or "").strip().upper())
    return level if isinstance(level, int) and level > 0 else logging.WARNING


log = logging.getLogger("arrvantage")

# The last few warnings, kept in memory (never on disk) for the Settings
# "Copy diagnostics" button. They are collected whatever LOG_LEVEL is, so a
# person who turned the log down to ERROR can still send a useful report.
RECENT_WARNING_LIMIT = 25
_recent_warnings: "collections.deque" = collections.deque(maxlen=RECENT_WARNING_LIMIT)
_PROCESS_STARTED = time.time()


class _RecentWarningsHandler(logging.Handler):
    _arrvantage_recent = True

    def emit(self, record) -> None:
        try:
            _recent_warnings.append((record.created, record.levelname, record.getMessage()))
        except Exception:  # noqa: BLE001 - logging must never break a request
            pass


def configure_logging() -> None:
    level = resolve_log_level(os.environ.get("LOG_LEVEL"))
    # The logger itself always lets WARNING through, so the diagnostics
    # collector below sees it; the console handler applies LOG_LEVEL.
    log.setLevel(min(level, logging.WARNING))
    for old in [h for h in log.handlers if getattr(h, "_arrvantage", False) or getattr(h, "_arrvantage_recent", False)]:
        log.removeHandler(old)  # re-running (or re-importing in tests) must not stack handlers
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    console.setLevel(level)
    console._arrvantage = True
    log.addHandler(console)
    recent = _RecentWarningsHandler(level=logging.WARNING)
    log.addHandler(recent)


configure_logging()

_URL_USERINFO = re.compile(r"(://)[^/\s@]+@")
_URL_QUERY = re.compile(r"\?[^\s'\")>]*=[^\s'\")>]*")


def scrub_log_text(value, limit: int = 300) -> str:
    """Text that may quote a URL, made safe to log: query strings (where
    api_key=, ApiKey=, X-Plex-Token= live) and user:password@ are removed."""
    text = _URL_USERINFO.sub(r"\1", str(value))
    text = _URL_QUERY.sub("?[removed]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def describe_request(method: str, url: str) -> str:
    """'GET host:port/path' - no query string, no credentials."""
    try:
        parts = urlsplit(url)
        return f"{method} {parts.netloc.rsplit('@', 1)[-1]}{parts.path}"
    except ValueError:
        return f"{method} (unparseable URL)"


_log_seen: dict = {}
_log_seen_lock = threading.Lock()


def log_problem(key, message: str, *args) -> None:
    """A WARNING, but the same problem is only shown once per
    LOG_REPEAT_SECONDS (the rest drop to DEBUG). A service that is down is
    probed every few seconds, and that must not fill the log."""
    now = time.monotonic()
    with _log_seen_lock:
        last = _log_seen.get(key)
        repeat = last is not None and now - last < LOG_REPEAT_SECONDS
        if not repeat:
            if len(_log_seen) > 500:
                for stale in [k for k, t in _log_seen.items() if now - t >= LOG_REPEAT_SECONDS]:
                    del _log_seen[stale]
            _log_seen[key] = now
    log.log(logging.DEBUG if repeat else logging.WARNING, message, *args)


def _install_request_logging() -> None:
    """Every call to a service goes through requests' Session.send, so
    wrapping it once records them all - failures as warnings, successes
    at DEBUG - without touching the ~130 places that make a request."""
    from requests.sessions import Session

    # If this module is imported twice (the tests do), the new wrapper
    # replaces the old one rather than wrapping it again.
    original = getattr(Session.send, "_arrvantage_original", Session.send)

    def send(self, request, **kwargs):
        started = time.monotonic()
        try:
            response = original(self, request, **kwargs)
        except requests.exceptions.RequestException as err:
            _note_request(request, None, err, started)
            raise
        _note_request(request, response, None, started)
        return response

    send._arrvantage_original = original
    Session.send = send


# Set by the alert senders for the length of one call: a webhook address is
# itself a secret (a Discord or Home Assistant webhook carries its key in the
# path), so while it is set a request is described by host alone and an error
# is reduced to its type, since a requests error quotes the whole address.
_log_ctx = threading.local()


def _note_request(request, response, err, started) -> None:
    try:
        redact = getattr(_log_ctx, "redact_path", False)
        if redact:
            host = urlsplit(request.url).netloc.rsplit("@", 1)[-1]
            what = f"{request.method} {host}/[path hidden]"
        else:
            what = describe_request(request.method, request.url)
        ms = int((time.monotonic() - started) * 1000)
        shape = re.sub(r"\d+", "#", what)
        if err is not None:
            log_problem(("request", shape, err.__class__.__name__), "%s failed: %s: %s (%d ms)",
                        what, err.__class__.__name__, "" if redact else scrub_log_text(err), ms)
        elif response.status_code >= 400:
            log_problem(("request", shape, response.status_code), "%s -> HTTP %s (%d ms)",
                        what, response.status_code, ms)
        else:
            log.debug("%s -> HTTP %s (%d ms)", what, response.status_code, ms)
    except Exception:  # noqa: BLE001 - logging must never break a request
        pass


_install_request_logging()

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Built-in sidebar icons, bundled as static files rather than fetched from
# each instance - avoids depending on that instance's auth settings, and
# keeps the app self-contained for Docker.
# Real branded logos, from the community-maintained dashboard-icons
# project (homarr-labs/dashboard-icons, Apache-2.0) - built specifically
# for exactly this use case (integration icons for self-hosted
# dashboards), served via jsDelivr so updates there show up here
# automatically without needing to re-bundle anything.
DASHBOARD_ICONS_BASE = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg"

INSTANCE_ICONS = {
    "movie": f"{DASHBOARD_ICONS_BASE}/radarr.svg",
    "series": f"{DASHBOARD_ICONS_BASE}/sonarr.svg",
    "artist": f"{DASHBOARD_ICONS_BASE}/lidarr.svg",
    "author": f"{DASHBOARD_ICONS_BASE}/chaptarr.svg",
    "comic": f"{DASHBOARD_ICONS_BASE}/kapowarr.svg",
}

DOWNLOADER_ICONS = {
    "qbittorrent": f"{DASHBOARD_ICONS_BASE}/qbittorrent.svg",
    "sabnzbd": f"{DASHBOARD_ICONS_BASE}/sabnzbd.svg",
}

INDEXER_ICONS = {
    "prowlarr": f"{DASHBOARD_ICONS_BASE}/prowlarr.svg",
}

TRANSCODER_ICONS = {
    "tdarr": "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/tdarr.png",
}

MEDIA_SERVER_ICONS = {
    "plex": f"{DASHBOARD_ICONS_BASE}/plex.svg",
    "jellyfin": f"{DASHBOARD_ICONS_BASE}/jellyfin.svg",
    "emby": f"{DASHBOARD_ICONS_BASE}/emby.svg",
    "komga": f"{DASHBOARD_ICONS_BASE}/komga.svg",
}

# Structural facts about each *kind* of arr app - which endpoints it uses,
# what kind of library it holds, which Overseerr media type it maps to.
# Any number of named instances can share one of these kinds.
KIND_META = {
    "movie": {
        "queue_endpoint": "/api/v3/queue",
        "history_endpoint": "/api/v3/history",
        "library_endpoint": "/api/v3/movie",
        "health_endpoint": "/api/v3/health",
        "diskspace_endpoint": "/api/v3/diskspace",
        "seerr_media_type": "movie",
        "label": "Movies (Radarr-type)",
    },
    "series": {
        "queue_endpoint": "/api/v3/queue",
        "history_endpoint": "/api/v3/history",
        "library_endpoint": "/api/v3/series",
        "health_endpoint": "/api/v3/health",
        "diskspace_endpoint": "/api/v3/diskspace",
        "seerr_media_type": "tv",
        "label": "TV (Sonarr-type)",
    },
    "artist": {
        "queue_endpoint": "/api/v1/queue",
        "history_endpoint": "/api/v1/history",
        "library_endpoint": "/api/v1/artist",
        "health_endpoint": "/api/v1/health",
        "diskspace_endpoint": "/api/v1/diskspace",
        "seerr_media_type": None,
        "label": "Music (Lidarr-type)",
    },
    "author": {
        "queue_endpoint": "/api/v1/queue",
        "history_endpoint": "/api/v1/history",
        "library_endpoint": "/api/v1/author",
        "health_endpoint": "/api/v1/health",
        "diskspace_endpoint": "/api/v1/diskspace",
        "seerr_media_type": None,
        "label": "Books (Readarr-type)",
    },
    # Kapowarr isn't part of the Servarr family the other four kinds
    # share - confirmed directly from its source (frontend/api.py,
    # backend/internals/status.py, backend/implementations/root_folders.py)
    # since it has no published OpenAPI spec. Two real differences that
    # matter here: api_key is a query parameter, not an X-Api-Key header,
    # and every response is wrapped in {"error", "result"} rather than a
    # bare list/records object. The endpoints below are kept for display/
    # reference consistency with the other kinds, but the actual fetch_*
    # functions for this kind dispatch to dedicated fetch_kapowarr_*
    # implementations (see "backend" below) rather than using these paths
    # directly the way the Servarr-family fetchers do.
    "comic": {
        "queue_endpoint": "/api/activity/queue",
        "history_endpoint": "/api/activity/history",
        "library_endpoint": "/api/volumes",
        "health_endpoint": "/api/system/status",
        "diskspace_endpoint": "/api/rootfolder",
        "seerr_media_type": None,  # Overseerr/Jellyseerr have no comics support
        "label": "Comics (Kapowarr-type)",
        "backend": "kapowarr",
    },
    # Librarr (manga / books fetcher) is not a Servarr app either: its key goes
    # in an X-Api-Key header but its endpoints and answers are its own, so the
    # fetch_* functions dispatch to fetch_librarr_* (see "backend"). It has no
    # Overseerr link; manga are searched and sent from its own Search tab.
    "librarr": {
        "queue_endpoint": "/api/downloads",
        "history_endpoint": "/api/activity",
        "library_endpoint": "/api/library/manga",
        "health_endpoint": "/health",
        "diskspace_endpoint": "",
        "seerr_media_type": None,
        "label": "Manga and books (Librarr)",
        "backend": "librarr",
    },
}

# Media server types we can actually talk to correctly. Jellyfin and Emby
# share the same API (Jellyfin was forked from Emby), so one integration
# covers both.
MEDIA_SERVER_TYPES = {
    "plex": "Plex",
    "jellyfin": "Jellyfin",
    "emby": "Emby",
    "komga": "Komga",
    "bookorbit": "BookOrbit",
}

# Friendly message + CSS severity per Kapowarr system/status "type" -
# confirmed against backend/internals/status.py and backend/base/
# definitions.py's StatusType enum (only these 5 values exist there).
# Falls back gracefully for anything unrecognized, same as the Overseerr
# status-code handling above, in case a future Kapowarr version adds one.
KAPOWARR_STATUS_MESSAGES = {
    "cv_rate_limit": ("warning", "ComicVine rate limit reached"),
    "download_service_rate_limit": ("warning", "A download service's daily rate limit was reached"),
    "root_folder_almost_full": ("warning", "Root folder is almost full"),
    "root_folder_full": ("error", "Root folder is full"),
    "cf_challenge_with_no_fs": ("warning", "Cloudflare challenge blocked and no FlareSolverr is configured"),
}

# Requester types we can actually talk to correctly. Jellyseerr is an
# API-compatible fork of Overseerr, so one integration covers both.
REQUESTER_TYPES = {
    "overseerr": "Overseerr / Jellyseerr",
}

# Overseerr/Jellyseerr send these as bare integers, not strings - see
# server/constants/media.ts in Overseerr's own source (MediaRequestStatus
# and MediaStatus enums) for the confirmed numbering. Two separate
# enums: SEERR_REQUEST_STATUS is "did an admin approve this ask", while
# SEERR_MEDIA_STATUS is "how far along is the actual download" - a
# request can be Approved while the underlying media is still Pending.
SEERR_REQUEST_STATUS = {
    1: "Pending Approval",
    2: "Approved",
    3: "Declined",
    4: "Failed",
    5: "Completed",
}
SEERR_MEDIA_STATUS = {
    1: "Unknown",
    2: "Pending",
    3: "Processing",
    4: "Partially Available",
    5: "Available",
    6: "Deleted",
}
# CSS severity class per request status, for the same badge styling
# used by health checks elsewhere in this app.
SEERR_REQUEST_STATUS_CLASS = {
    1: "warning",  # Pending Approval
    2: "success",  # Approved
    3: "error",    # Declined
    4: "error",    # Failed
    5: "success",  # Completed
}

# Download client types. Separate from Instances entirely - not tied to
# any single arr instance. Both use a plain API key, same as everything
# else in this app (qBittorrent v5.2.0+ added stateless API key auth,
# replacing the older username/password login flow).
DOWNLOADER_TYPES = {
    "qbittorrent": "qBittorrent",
    "sabnzbd": "SABnzbd",
}

# Indexer manager types. Only Prowlarr is supported today, but kept as
# a dict (matching DOWNLOADER_TYPES's shape) in case that ever changes.
INDEXER_SERVICE_TYPES = {
    "prowlarr": "Prowlarr",
}

# Transcode automation. Only Tdarr is supported today, but kept as a
# dict for consistency with the other single-option categories.
TRANSCODER_TYPES = {
    "tdarr": "Tdarr",
}

# In Docker, set CONFIG_DIR to a mounted volume path (e.g. /app/data) so
# config.json - your instances, users, and API keys - survives image
# rebuilds. Left unset, it defaults to sitting next to this file, same
# as running the app directly with `python app.py`.
CONFIG_DIR = os.environ.get("CONFIG_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(CONFIG_DIR, exist_ok=True)  # in case a fresh volume mount is still empty
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

EMPTY_CONFIG = {
    "instances": [], "media_servers": [], "requesters": [], "downloaders": [], "indexers": [], "transcoders": [], "users": [],
    "tmdb": {"api_key": ""}, "tvdb": {"api_key": "", "pin": ""},
    "fanart": {"api_key": ""}, "theaudiodb": {"api_key": "123"},
    "tautulli": {"url": "", "api_key": ""}, "tracearr": {"url": "", "api_key": ""}, "secret_key": "",
}


def migrate_librarr_downloaders(cfg: dict) -> bool:
    """
    Librarr was briefly a download-client type (in the development build before this one); it is an instance kind
    now. Moves any such entry from "downloaders" to "instances", keeping its
    name, address, key, colour and on/off switch, and renaming it only if an
    instance already has that name. Returns True when something moved. Safe to
    run again - once moved there is nothing left to move.
    """
    downloaders = cfg.get("downloaders")
    if not isinstance(downloaders, list):
        return False
    moving = [d for d in downloaders if isinstance(d, dict) and d.get("type") == "librarr"]
    if not moving:
        return False
    instances = cfg.get("instances")
    if not isinstance(instances, list):
        instances = cfg["instances"] = []
    taken = {i.get("name") for i in instances if isinstance(i, dict)}
    for old in moving:
        name = old.get("name") if isinstance(old.get("name"), str) and old.get("name") else "Librarr"
        unique, number = name, 1
        while unique in taken:
            number += 1
            unique = f"{name} ({number})"
        taken.add(unique)
        instances.append({
            "name": unique, "kind": "librarr", "url": old.get("url", ""), "api_key": old.get("api_key", ""),
            "color": old.get("color") or "#5b8cff", "enabled": old.get("enabled", True),
        })
    cfg["downloaders"] = [d for d in downloaders if not (isinstance(d, dict) and d.get("type") == "librarr")]
    return True


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict) and migrate_librarr_downloaders(cfg):
                log.info("Moved Librarr from Downloaders to Instances")
                try:
                    save_config(cfg)
                except OSError as err:
                    log.error("Could not save the Librarr move to config.json (%s); it will be redone on next start",
                              err.__class__.__name__)
            return cfg
        except (json.JSONDecodeError, OSError) as err:
            # Falls through to writing a fresh empty config below, so say so.
            log.error("config.json could not be read (%s: %s); starting from an empty config",
                      err.__class__.__name__, scrub_log_text(err))

    save_config(EMPTY_CONFIG)
    return EMPTY_CONFIG


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def dedupe_names(entries: list) -> list:
    """Auto-suffixes duplicate names so two saved entries never collide."""
    seen = {}
    for entry in entries:
        base = entry["name"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            entry["name"] = f"{base} ({seen[base]})"
    return entries


def build_apps(cfg: dict) -> dict:
    apps = {}
    for entry in cfg.get("instances", []):
        kind = entry.get("kind")
        meta = KIND_META.get(kind)
        if not meta or not entry.get("name"):
            continue
        apps[entry["name"]] = {
            "queue_endpoint": meta["queue_endpoint"],
            "history_endpoint": meta["history_endpoint"],
            "library_endpoint": meta["library_endpoint"],
            "health_endpoint": meta["health_endpoint"],
            "diskspace_endpoint": meta["diskspace_endpoint"],
            "seerr_media_type": meta["seerr_media_type"],
            "backend": meta.get("backend", "servarr"),
            "library_kind": kind,
            "url": entry.get("url", ""),
            "api_key": entry.get("api_key", ""),
            "color": entry.get("color") or "#5b8cff",
            "enabled": entry.get("enabled", True),
        }
    return apps


def get_active_requester():
    """
    Only one requester powers the Request tab at a time - the first
    enabled one found. You can still keep several saved, just not all
    active simultaneously (the UI doesn't have a requester picker).
    """
    for entry in REQUESTERS:
        if entry.get("enabled") and entry.get("type") in REQUESTER_TYPES:
            return entry
    return None


CONFIG = load_config()
APPS = build_apps(CONFIG)
MEDIA_SERVERS = CONFIG.get("media_servers", [])
REQUESTERS = CONFIG.get("requesters", [])
TMDB = CONFIG.get("tmdb", {"api_key": ""})
TVDB = CONFIG.get("tvdb", {"api_key": "", "pin": ""})
FANART = CONFIG.get("fanart", {"api_key": ""})
THEAUDIODB = CONFIG.get("theaudiodb", {"api_key": "123"})
TAUTULLI = CONFIG.get("tautulli", {"url": "", "api_key": ""})
TRACEARR = CONFIG.get("tracearr", {"url": "", "api_key": ""})
DOWNLOADERS = CONFIG.get("downloaders", [])
INDEXERS = CONFIG.get("indexers", [])
TRANSCODERS = CONFIG.get("transcoders", [])
USERS = CONFIG.get("users", [])

# Flask needs a secret key to sign session cookies. Generate one on first
# run and persist it in config.json so logins survive app restarts.
if not CONFIG.get("secret_key"):
    CONFIG["secret_key"] = secrets.token_hex(32)
    save_config(CONFIG)
app.secret_key = CONFIG["secret_key"]


def ensure_csrf_token() -> str:
    """Generates a per-session CSRF token the first time it's needed,
    and reuses it for the rest of that session."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


# Selectable colour themes (Appearance panel). The colours themselves live in
# static/css/app.css as [data-theme="..."] token sets; this is just the list,
# its labels, and the browser-chrome colour for each. "auto" picks Paper or
# Midnight from the device's light/dark setting.
DEFAULT_THEME = "midnight"
THEMES = {
    "auto": {"label": "Auto", "description": "Follows your device: Paper in light mode, Midnight in dark mode.",
             "color": "#0d1117", "light_color": "#f7f6f3"},
    "midnight": {"label": "Midnight", "description": "The original dark look.", "color": "#0d1117"},
    "paper": {"label": "Paper", "description": "Light and warm, easy to read in daylight.", "color": "#f7f6f3"},
    "carbon": {"label": "Carbon", "description": "Neutral grays with a single blue and square corners, after IBM's Carbon design system.",
               "color": "#161616"},
    "carbon-light": {"label": "Carbon Light", "description": "The same system in its light form: white panels on a pale gray page.",
                     "color": "#f4f4f4"},
    "nord": {"label": "Nord", "description": "Muted blue-grays from the Nord palette, calm and low-glare.", "color": "#2e3440"},
    "nord-light": {"label": "Nord Light", "description": "The pale Snow Storm half of Nord: cool white and slate with a steel-blue accent.",
                   "color": "#eceff4"},
    "solarized": {"label": "Solarized Light", "description": "Warm cream and slate from the Solarized palette, adjusted so small text stays readable.",
                  "color": "#fdf6e3"},
    "solarized-dark": {"label": "Solarized Dark", "description": "Deep teal-black from the Solarized palette with gentle, low-contrast text.",
                       "color": "#002b36"},
    "telemetry": {"label": "Telemetry", "description": "Square corners, monospaced type and near-black panels, like a control-room terminal.",
                  "color": "#0a0a0a"},
    "contrast": {"label": "High contrast", "description": "Pure black and white with bright borders and a thick focus ring.",
                 "color": "#000000"},
}


def normalize_theme(value) -> str:
    """Any stored or posted theme name that isn't a known one becomes the default."""
    return value if isinstance(value, str) and value in THEMES else DEFAULT_THEME


# Accent colour: a per-user highlight laid over whichever theme is active. Only
# non-status hues are offered - green, amber and red are reserved for status and
# must never double as decoration. "default" means "use the theme's own accent".
# The CSS for each key lives in app.css ([data-accent=...]) and is contrast-checked
# against every theme by Tests/test_themes.py; "swatch" is only the picker's fallback.
DEFAULT_ACCENT = "default"
ACCENTS = {
    "default": {"label": "Theme default"},
    "blue": {"label": "Blue"},
    "cyan": {"label": "Cyan"},
    "violet": {"label": "Violet"},
    "magenta": {"label": "Magenta"},
    "neutral": {"label": "Neutral"},
}


def normalize_accent(value) -> str:
    return value if isinstance(value, str) and value in ACCENTS else DEFAULT_ACCENT


@app.context_processor
def inject_theme():
    """Gives every template the signed-in user's theme (the default on login/setup)."""
    user = current_user()
    key = normalize_theme(user.get("theme") if user else None)
    return {
        "current_theme": key,
        "theme_meta": THEMES[key],
        "themes": THEMES,
        "current_accent": normalize_accent(user.get("accent") if user else None),
        "accents": ACCENTS,
    }


@app.context_processor
def inject_asset_helpers():
    """
    Gives every template asset_url('css/app.css'), which is the normal
    static URL plus ?v=<file mtime>. Browsers cache static files hard;
    this makes a redeployed stylesheet get re-fetched without anyone
    having to clear their cache (the fonts are left unversioned on
    purpose - they never change in place).
    """
    def asset_url(filename):
        try:
            version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            version = 0
        return url_for("static", filename=filename, v=version)
    return {
        "asset_url": asset_url,
        "disk_low_pct": DISK_LOW_PCT,
        "disk_critical_pct": DISK_CRITICAL_PCT,
    }


@app.template_filter("external_url")
def external_url(url) -> str:
    """
    Makes a saved service URL safe to use as a link: one saved without a
    scheme gets http:// (same default the JS openDirectly() always used),
    and anything that isn't http(s) can't become a javascript: link.
    """
    url = (url or "").strip()
    if not url:
        return ""
    return url if re.match(r"^https?://", url, re.IGNORECASE) else f"http://{url}"


@app.before_request
def csrf_protect():
    """
    Every state-changing route in this app is a POST, so this single
    check covers all of them - no per-route decoration needed. Accepts
    the token from either a submitted form field (traditional POSTs
    like Settings/Login/Setup) or an X-CSRF-Token header (the JS
    fetch() calls throughout the dashboard, via the global fetch
    override in index.html).
    """
    if request.method != "POST":
        return
    expected = session.get("csrf_token")
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        return jsonify({"ok": False, "error": "Session expired or invalid - please refresh the page."}), 403

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def get_user(username):
    return next((u for u in USERS if u["username"] == username), None)


def current_user():
    username = session.get("username")
    return get_user(username) if username else None


def user_permissions(user):
    """Admins implicitly have every permission - only secondary accounts
    are actually restricted by their saved checkboxes."""
    if user and user.get("role") == "admin":
        return {"requesting": True, "searching": True}
    return (user or {}).get("permissions", {"requesting": False, "searching": False})


# In-memory only. Safe now that the app runs as a single gunicorn
# worker (see the Dockerfile's CMD for why) - there's only ever one
# process, so this dict is the only copy that exists, with no
# cross-worker inconsistency to worry about.
_login_attempts = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60


def is_login_locked_out(client_ip: str) -> bool:
    now = time.time()
    recent = [t for t in _login_attempts.get(client_ip, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_attempts[client_ip] = recent
    return len(recent) >= LOGIN_MAX_ATTEMPTS


def record_failed_login(client_ip: str) -> None:
    _login_attempts.setdefault(client_ip, []).append(time.time())


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not USERS:
            return redirect(url_for("setup"))
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user or user.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "administrator access required"}), 403
            return redirect(url_for("index"))
        return view(*args, **kwargs)
    return wrapped


def permission_required(perm_name):
    """For API routes - returns JSON 403s rather than redirecting, since
    these are called via fetch(), not by navigating the browser."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                return jsonify({"error": "not logged in"}), 401
            if not user_permissions(user).get(perm_name):
                return jsonify({"error": f"your account doesn't have '{perm_name}' permission"}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.route("/setup", methods=["GET", "POST"])
def setup():
    global CONFIG, APPS, MEDIA_SERVERS, REQUESTERS, USERS
    if USERS:
        return redirect(url_for("login"))  # setup only runs once, before any account exists

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not username or not password:
            error = "Username and password are required."
        elif password != confirm:
            error = "Passwords don't match."
        else:
            new_config = {
                "instances": dedupe_names(parse_indexed_entries(
                    request.form, "instance", ["kind", "url", "api_key", "color", "enabled"])),
                "media_servers": dedupe_names(parse_indexed_entries(
                    request.form, "mediaserver", ["type", "url", "credential", "enabled"])),
                "requesters": dedupe_names(parse_indexed_entries(
                    request.form, "requester", ["type", "url", "credential", "enabled"])),
                "users": [{
                    "username": username,
                    "password_hash": generate_password_hash(password),
                    "role": "admin",
                    "permissions": {"requesting": True, "searching": True},
                }],
                "secret_key": CONFIG["secret_key"],
            }
            save_config(new_config)
            CONFIG = new_config
            APPS = build_apps(CONFIG)
            MEDIA_SERVERS = CONFIG["media_servers"]
            REQUESTERS = CONFIG["requesters"]
            USERS = CONFIG["users"]
            session["username"] = username
            return redirect(url_for("index"))

    return render_template(
        "setup.html", error=error,
        kind_options=KIND_META,
        media_server_type_options=MEDIA_SERVER_TYPES,
        requester_type_options=REQUESTER_TYPES,
        csrf_token=ensure_csrf_token(),
    )


@app.route("/health")
def health():
    """
    A true liveness check - returns 200 unconditionally, regardless of
    whether any user accounts exist yet. Unlike /login or /setup (which
    both redirect depending on setup state - /login to /setup when no
    users exist, /setup to /login once they do), this never redirects,
    so it's the only safe target for a Docker/TrueNAS healthcheck.
    A brand-new instance failing its healthcheck before anyone's even
    completed the setup wizard is exactly the bug this fixes.
    """
    return jsonify({"status": "ok"}), 200


@app.route("/login", methods=["GET", "POST"])
def login():
    if not USERS:
        return redirect(url_for("setup"))

    error = None
    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        if is_login_locked_out(client_ip):
            log_problem(("lockout", client_ip), "Login locked out for %s after too many failed attempts", client_ip)
            error = "Too many failed attempts. Please wait a few minutes and try again."
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = get_user(username)

            if user and check_password_hash(user["password_hash"], password):
                session["username"] = username
                return redirect(url_for("index"))
            record_failed_login(client_ip)
            # The name is only logged when it is a real account: people do
            # sometimes type their password into the username box.
            who = f"user {user['username']!r}" if user else "an unknown user"
            log.warning("Failed login for %s from %s", who, client_ip)
            error = "Incorrect username or password."

    return render_template("login.html", error=error, csrf_token=ensure_csrf_token())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def get_item_title(record: dict) -> str:
    for key in ("movie", "series", "book", "artist", "album"):
        nested = record.get(key)
        if isinstance(nested, dict) and nested.get("title"):
            return nested["title"]
    # History records use "sourceTitle" (the raw release name) instead
    # of "title" - queue records use "title", so check both.
    return record.get("title") or record.get("sourceTitle", "(unknown title)")


def get_time_left(record: dict) -> str:
    if record.get("timeleft"):
        return record["timeleft"]
    return record.get("status", "unknown")


def get_queue_progress_pct(record: dict) -> "int | None":
    """None (rather than 0) when the arr app hasn't reported a size yet,
    so the frontend can tell 'unknown' apart from 'just started'."""
    size = record.get("size") or 0
    sizeleft = record.get("sizeleft")
    if not size or sizeleft is None:
        return None
    done = max(0, size - sizeleft)
    return round((done / size) * 100)


def get_queue_status_messages(record: dict) -> list:
    """Queue items report problems two ways: a top-level errorMessage,
    and/or a list of per-file statusMessages (each with its own list of
    strings) - flatten both into one list of short strings for display."""
    messages = []
    for status in record.get("statusMessages") or []:
        messages.extend(status.get("messages") or [])
    if record.get("errorMessage"):
        messages.append(record["errorMessage"])
    return messages


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def fetch_queue(name: str, config: dict) -> dict:
    if config.get("backend") == "kapowarr":
        return fetch_kapowarr_queue(name, config)
    if config.get("backend") == "librarr":
        return fetch_librarr_queue(name, config)

    url = config["url"].rstrip("/") + config["queue_endpoint"]
    headers = {"X-Api-Key": config["api_key"]}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "queue_items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "queue_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "queue_items": []}

    data = response.json()
    records = data.get("records", data) if isinstance(data, dict) else data

    queue_items = [
        {
            "title": get_item_title(record),
            "time_left": get_time_left(record),
            "status": record.get("status", "unknown"),
            "progress_pct": get_queue_progress_pct(record),
            "status_messages": get_queue_status_messages(record),
            "indexer": record.get("indexer") or "",
            "download_client": record.get("downloadClient") or "",
            "protocol": (record.get("protocol") or "").lower(),
            "quality": ((record.get("quality") or {}).get("quality") or {}).get("name", ""),
            "custom_format_score": record.get("customFormatScore", 0),
            "size_human": format_size_gb(record["size"]) if record.get("size") else "",
        }
        for record in records
    ]
    return {"error": None, "queue_items": queue_items}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def get_history_event(record: dict) -> str:
    event = record.get("eventType", "unknown")
    friendly = {
        "grabbed": "Grabbed",
        "downloadFolderImported": "Imported",
        "downloadFailed": "Failed",
        "episodeFileDeleted": "Deleted",
        "movieFileDeleted": "Deleted",
    }
    return friendly.get(event, event)


def fetch_history(name: str, config: dict) -> dict:
    if config.get("backend") == "kapowarr":
        return fetch_kapowarr_history(name, config)
    if config.get("backend") == "librarr":
        return fetch_librarr_history(name, config)

    url = config["url"].rstrip("/") + config["history_endpoint"]
    headers = {"X-Api-Key": config["api_key"]}
    params = {"pageSize": 25, "sortKey": "date", "sortDirection": "descending"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "history_items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "history_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "history_items": []}

    data = response.json()
    records = data.get("records", data) if isinstance(data, dict) else data

    history_items = [
        {
            "title": get_item_title(record),
            "event": get_history_event(record),
            "date": record.get("date", "")[:10],
        }
        for record in records
    ]
    return {"error": None, "history_items": history_items}


# ---------------------------------------------------------------------------
# Health + disk space - the same "problems" banner and free-space figures
# the arr app's own UI shows, surfaced here instead of making people check
# each app individually.
# ---------------------------------------------------------------------------


def fetch_health(name: str, config: dict) -> dict:
    if config.get("backend") == "kapowarr":
        return fetch_kapowarr_health(name, config)
    if config.get("backend") == "librarr":
        return fetch_librarr_health(name, config)

    url = config["url"].rstrip("/") + config["health_endpoint"]
    headers = {"X-Api-Key": config["api_key"]}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "health_items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "health_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "health_items": []}

    records = response.json() or []

    health_items = [
        {
            "type": (record.get("type") or "notice").lower(),
            "source": record.get("source", ""),
            "message": record.get("message", ""),
            "wiki_url": record.get("wikiUrl", ""),
        }
        for record in records
    ]
    return {"error": None, "health_items": health_items}


def fetch_diskspace(name: str, config: dict) -> dict:
    if config.get("backend") == "kapowarr":
        return fetch_kapowarr_diskspace(name, config)
    if config.get("backend") == "librarr":
        return fetch_librarr_diskspace(name, config)

    url = config["url"].rstrip("/") + config["diskspace_endpoint"]
    headers = {"X-Api-Key": config["api_key"]}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "diskspace_items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "diskspace_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "diskspace_items": []}

    records = response.json() or []

    diskspace_items = []
    for record in records:
        total = record.get("totalSpace") or 0
        free = record.get("freeSpace") or 0
        percent_free = round((free / total) * 100) if total else None
        diskspace_items.append({
            "path": record.get("path", ""),
            "label": record.get("label") or "",
            "free_human": format_size_gb(free),
            "total_human": format_size_gb(total),
            "percent_free": percent_free,
        })
    return {"error": None, "diskspace_items": diskspace_items}


# ---------------------------------------------------------------------------
# Kapowarr - a comics-focused *arr app, but with a meaningfully different
# API shape from the Servarr-family apps above: api_key is a query
# parameter (not an X-Api-Key header), and every response - success or
# failure - is wrapped in {"error": ..., "result": ...}. Confirmed
# directly from Kapowarr's own source (frontend/api.py for routes/auth,
# backend/internals/status.py for the health-check shape,
# backend/implementations/root_folders.py for disk space,
# backend/implementations/volumes.py for the volumes/issues shape) since
# it has no published OpenAPI spec at the time this was written. The
# exact key names inside a queue entry's as_dict() weren't independently
# confirmed beyond the abstract Download class's own property names
# (id, volume_id, issue_id, title, size, state, progress, speed,
# source_name) - if a queue item looks wrong, that's the place to check
# against a live response first.
# ---------------------------------------------------------------------------


def kapowarr_request(config: dict, path: str, params: dict = None):
    """
    Shared GET helper - every Kapowarr endpoint needs api_key as a query
    parameter rather than a header, and wraps its JSON body in
    {"error", "result"} even on some logical failures (an actually
    invalid key comes back as a real 401, which raise_for_status()
    catches upstream instead).
    """
    url = config["url"].rstrip("/") + path
    query = {"api_key": config["api_key"]}
    if params:
        query.update(params)
    response = requests.get(url, params=query, timeout=15)
    response.raise_for_status()
    data = response.json()
    return data.get("result"), data.get("error")


def fetch_kapowarr_queue(name: str, config: dict) -> dict:
    try:
        result, error = kapowarr_request(config, "/api/activity/queue")
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "queue_items": []}
    except requests.exceptions.HTTPError:
        return {"error": "HTTP error - check API key", "queue_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "queue_items": []}
    if error:
        return {"error": error, "queue_items": []}

    queue_items = []
    for record in result or []:
        state = record.get("state", "unknown")
        status_messages = []
        if state == "failed":
            status_messages.append("Download failed")
        elif state == "canceled":
            status_messages.append("Download canceled")

        progress = record.get("progress")
        size = record.get("size", -1)
        queue_items.append({
            "title": record.get("title") or "(unknown title)",
            "time_left": state,
            "status": state,
            "progress_pct": round(progress) if isinstance(progress, (int, float)) else None,
            "status_messages": status_messages,
            "indexer": record.get("source_name") or "",
            "download_client": "",
            "protocol": "",
            "quality": "",
            "custom_format_score": 0,
            "size_human": format_size_gb(size) if size and size > 0 else "",
        })
    return {"error": None, "queue_items": queue_items}


def fetch_kapowarr_history(name: str, config: dict) -> dict:
    try:
        result, error = kapowarr_request(config, "/api/activity/history")
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "history_items": []}
    except requests.exceptions.HTTPError:
        return {"error": "HTTP error - check API key", "history_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "history_items": []}
    if error:
        return {"error": error, "history_items": []}

    history_items = []
    for record in result or []:
        # downloaded_at's exact type (epoch seconds vs ISO string) isn't
        # independently confirmed - handled defensively rather than
        # assumed, same spirit as this project's other flagged
        # uncertain fields (see CLAUDE.md).
        raw_date = record.get("downloaded_at")
        if isinstance(raw_date, (int, float)):
            date = datetime.fromtimestamp(raw_date).strftime("%Y-%m-%d")
        else:
            date = str(raw_date or "")[:10]
        history_items.append({
            "title": record.get("file_title") or record.get("web_title") or "(unknown title)",
            "event": "Downloaded" if record.get("success") else "Failed",
            "date": date,
        })
    return {"error": None, "history_items": history_items}


def fetch_kapowarr_health(name: str, config: dict) -> dict:
    try:
        result, error = kapowarr_request(config, "/api/system/status")
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "health_items": []}
    except requests.exceptions.HTTPError:
        return {"error": "HTTP error - check API key", "health_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "health_items": []}
    if error:
        return {"error": error, "health_items": []}

    health_items = []
    for record in result or []:
        status_type = record.get("type", "")
        severity, message = KAPOWARR_STATUS_MESSAGES.get(
            status_type, ("notice", status_type.replace("_", " ").title() or "Unknown issue")
        )
        subtypes = record.get("display_subtypes") or []
        if subtypes:
            message = f"{message}: {', '.join(subtypes)}"
        health_items.append({"type": severity, "source": "Kapowarr", "message": message, "wiki_url": ""})
    return {"error": None, "health_items": health_items}


def fetch_kapowarr_diskspace(name: str, config: dict) -> dict:
    try:
        result, error = kapowarr_request(config, "/api/rootfolder")
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "diskspace_items": []}
    except requests.exceptions.HTTPError:
        return {"error": "HTTP error - check API key", "diskspace_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "diskspace_items": []}
    if error:
        return {"error": error, "diskspace_items": []}

    diskspace_items = []
    for record in result or []:
        # "size" is null when Kapowarr couldn't read disk usage for that
        # folder (e.g. it doesn't exist yet) - confirmed in
        # root_folders.py's own exception handling.
        size = record.get("size") or {}
        total = size.get("total") or 0
        free = size.get("free") or 0
        percent_free = round((free / total) * 100) if total else None
        diskspace_items.append({
            "path": record.get("folder", ""),
            "label": "",
            "free_human": format_size_gb(free) if total else "",
            "total_human": format_size_gb(total) if total else "",
            "percent_free": percent_free,
        })
    return {"error": None, "diskspace_items": diskspace_items}


def get_kapowarr_cover_url(record: dict, config: dict) -> str:
    volume_id = record.get("id")
    if volume_id is None:
        return ""
    return f"{config['url'].rstrip('/')}/api/volumes/{volume_id}/cover?api_key={config['api_key']}"


def fetch_kapowarr_library(name: str, config: dict) -> dict:
    try:
        result, error = kapowarr_request(config, "/api/volumes", params={"sort": "title"})
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "library_items": [], "status_options": [], "genre_options": []}
    except requests.exceptions.HTTPError:
        return {"error": "HTTP error - check API key", "library_items": [], "status_options": [], "genre_options": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "library_items": [], "status_options": [], "genre_options": []}
    if error:
        return {"error": error, "library_items": [], "status_options": [], "genre_options": []}

    library_items = [get_library_item(record, "comic", config) for record in (result or [])]
    library_items.sort(key=lambda item: item["title"].lower())
    status_options = sorted({item["status"] for item in library_items})
    genre_options = sorted({g for item in library_items for g in item["genre"].split(", ") if item["genre"] != "Unknown"})
    return {"error": None, "library_items": library_items, "status_options": status_options, "genre_options": genre_options}


def fetch_kapowarr_issues(config: dict, volume_id: int) -> dict:
    """
    Volume detail for the item-details modal - GET /api/volumes/<id>
    already embeds the full issue list on the volume payload
    (Volume.get_public_data() in Kapowarr's own volumes.py), so this is
    a single request rather than a separate issues endpoint. Connection
    and HTTP errors are deliberately left to propagate to the caller
    (the /api/details route already catches those the same way for
    every other library kind); only Kapowarr's own soft
    {"error": ...} envelope is handled here.
    """
    result, error = kapowarr_request(config, f"/api/volumes/{volume_id}")
    if error:
        return {"error": error}

    result = result or {}
    issues = [
        {
            "issue_number": issue.get("issue_number") or issue.get("calculated_issue_number") or "?",
            "title": issue.get("title") or "(untitled)",
            "date": issue.get("date") or "",
            "has_file": bool(issue.get("files")),
        }
        for issue in result.get("issues") or []
    ]
    return {
        "overview": result.get("description") or "No description available.",
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


def get_status_category(kind: str, record: dict) -> str:
    """
    Turn raw counts into a small set of clean categories that make
    sense as a filter dropdown (a fraction like '3/10' is a bad filter
    value since it's different for almost every item).
    """
    if kind == "movie":
        return "Downloaded" if record.get("hasFile") else "Missing"

    stats = record.get("statistics", {})
    if kind == "series":
        have, total = stats.get("episodeFileCount", 0), stats.get("episodeCount", 0)
    elif kind == "artist":
        have, total = stats.get("trackFileCount", 0), stats.get("trackCount", 0)
    elif kind == "author":
        have, total = stats.get("bookFileCount", 0), stats.get("bookCount", 0)
    elif kind == "comic":
        have, total = record.get("issues_downloaded", 0), record.get("issue_count", 0)
    else:
        have, total = 0, 0

    if total == 0:
        return "Unknown"
    if have == 0:
        return "Missing"
    if have >= total:
        return "Complete"
    return "Partial"


def get_genre(record: dict) -> str:
    """
    Radarr/Sonarr reliably put genres on the movie/series record itself.
    Lidarr/Readarr often don't expose genre at the artist/author level -
    if this keeps showing "Unknown" for those kinds, that's likely why,
    not a bug - genre may only exist deeper (e.g. per-album).
    """
    genres = record.get("genres") or []
    return ", ".join(genres) if genres else "Unknown"


def get_cover_url(record: dict, config: dict) -> str:
    """
    Pull the poster image URL out of the record's "images" list.
    Prefer "remoteUrl" when it's an actual absolute URL (points
    straight at the original source, like TMDB - loads with no auth
    needed). Some instances (confirmed on Lidarr) put an internal
    container path in "remoteUrl" instead of a real external link, so
    this checks for a real http(s) URL before trusting it, and falls
    back to the arr instance's own copy - with the API key appended as
    a query param, since <img> tags can't send custom headers.
    """
    images = record.get("images") or []
    poster = next((img for img in images if img.get("coverType") == "poster"), None)
    if not poster:
        return ""

    remote_url = poster.get("remoteUrl", "")
    if remote_url.startswith("http://") or remote_url.startswith("https://"):
        return remote_url

    path = poster.get("url", "")
    if not path:
        return ""
    separator = "&" if "?" in path else "?"
    return f"{config['url'].rstrip('/')}{path}{separator}apikey={config['api_key']}"


def get_person_image_url(credit: dict, config: dict) -> str:
    """
    Same idea as get_cover_url, but for a cast member's headshot on a
    credit record. Best-guess coverType value ("headshot") - if actor
    photos never show up, check the raw credit JSON for the real one.
    """
    images = credit.get("images") or []
    photo = next((img for img in images if img.get("coverType") in ("headshot", "poster")), None)
    if not photo:
        return ""

    if photo.get("remoteUrl"):
        return photo["remoteUrl"]

    path = photo.get("url", "")
    if not path:
        return ""
    separator = "&" if "?" in path else "?"
    return f"{config['url'].rstrip('/')}{path}{separator}apikey={config['api_key']}"


def get_library_item(record: dict, kind: str, config: dict) -> dict:
    genre = get_genre(record)
    status = get_status_category(kind, record)
    cover_url = get_kapowarr_cover_url(record, config) if kind == "comic" else get_cover_url(record, config)
    episode_have = episode_total = None
    size_bytes = 0

    if kind == "movie":
        title = record.get("title", "(unknown)")
        year = record.get("year", "")
        detail = ""
        title = f"{title} ({year})" if year else title
        release_date = record.get("inCinemas") or record.get("physicalRelease") or str(year)
        size_bytes = record.get("sizeOnDisk", 0) or 0
    elif kind == "series":
        title = record.get("title", "(unknown)")
        stats = record.get("statistics", {})
        episode_have, episode_total = stats.get("episodeFileCount", 0), stats.get("episodeCount", 0)
        detail = f"{episode_have}/{episode_total} episodes"
        release_date = record.get("firstAired", "")
        size_bytes = stats.get("sizeOnDisk", 0) or 0
    elif kind == "artist":
        title = record.get("artistName", "(unknown)")
        stats = record.get("statistics", {})
        detail = f"{stats.get('trackFileCount', 0)}/{stats.get('trackCount', 0)} tracks"
        # No clean single "release date" concept for an artist as a
        # whole - Release Date sort won't distinguish artists well.
        release_date = ""
        size_bytes = stats.get("sizeOnDisk", 0) or 0
    elif kind == "author":
        title = record.get("authorName", "(unknown)")
        stats = record.get("statistics", {})
        detail = f"{stats.get('bookFileCount', 0)}/{stats.get('bookCount', 0)} books"
        # Same limitation as artists - no single release date for an author.
        release_date = ""
        size_bytes = stats.get("sizeOnDisk", 0) or 0
    elif kind == "comic":
        title = record.get("title", "(unknown)")
        year = record.get("year", "")
        title = f"{title} ({year})" if year else title
        episode_have = record.get("issues_downloaded", 0)
        episode_total = record.get("issue_count", 0)
        detail = f"{episode_have}/{episode_total} issues"
        # No single release date concept for a volume as a whole - same
        # reasoning as artists/authors above.
        release_date = ""
        size_bytes = record.get("total_size") or 0
    else:
        title = record.get("title", "(unknown)")
        detail = ""
        release_date = ""

    return {"title": title, "detail": detail, "status": status, "genre": genre,
            "cover_url": cover_url, "item_id": record.get("id"),
            "tmdb_id": record.get("tmdbId") if kind == "movie" else None,
            "tvdb_id": record.get("tvdbId") if kind == "series" else None,
            "foreign_artist_id": record.get("foreignArtistId") if kind == "artist" else None,
            "release_date": release_date or "", "date_added": record.get("added", "") or "",
            "episode_have": episode_have, "episode_total": episode_total, "size_bytes": size_bytes}


def fetch_library(name: str, config: dict) -> dict:
    if config.get("backend") == "kapowarr":
        return fetch_kapowarr_library(name, config)
    if config.get("backend") == "librarr":
        return fetch_librarr_library(name, config)

    url = config["url"].rstrip("/") + config["library_endpoint"]
    headers = {"X-Api-Key": config["api_key"]}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({config['url']})", "library_items": [],
                "status_options": [], "genre_options": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "library_items": [],
                "status_options": [], "genre_options": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "library_items": [],
                "status_options": [], "genre_options": []}

    data = response.json()
    records = data.get("records", data) if isinstance(data, dict) else data

    library_items = [get_library_item(record, config["library_kind"], config) for record in records]

    # Fanart.tv artist images came back blank in practice, so this is
    # disabled for now - back to using Lidarr's own images only. The
    # Fanart.tv integration itself (settings, key, test button,
    # enrich_artist_covers_with_fanart below) is left in place in case
    # it's useful for something else later.
    library_items.sort(key=lambda item: item["title"].lower())

    status_options = sorted({item["status"] for item in library_items})
    genre_options = sorted({g for item in library_items for g in item["genre"].split(", ") if item["genre"] != "Unknown"})

    return {
        "error": None,
        "library_items": library_items,
        "status_options": status_options,
        "genre_options": genre_options,
    }


def fetch_collections(config: dict, owned_tmdb_lookup: dict) -> list:
    """
    Radarr-only: fetches Movie Collections (e.g. "The Dark Knight
    Collection") and marks which of each collection's movies are
    already in your library vs missing entirely.

    owned_tmdb_lookup maps tmdb_id -> this instance's own item_id for
    every owned movie, so an owned movie found here can open its real
    detail view the same way a normal library card does.

    Field names here are my best inference from Radarr's documented
    v3 API - if collections don't load, or movies/counts look wrong,
    the raw JSON from GET /api/v3/collection is the place to check.
    """
    url = config["url"].rstrip("/") + "/api/v3/collection"
    headers = {"X-Api-Key": config["api_key"]}

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return []

    collections = []
    for entry in response.json():
        movies = [
            {
                "tmdb_id": m.get("tmdbId"),
                "title": m.get("title", "(untitled)"),
                "year": m.get("year", ""),
                "owned": m.get("tmdbId") in owned_tmdb_lookup,
                "item_id": owned_tmdb_lookup.get(m.get("tmdbId")),
                # Uncertain whether Radarr's collection movie entries
                # always include full "images" data - falls back to no
                # poster gracefully if not, same as everywhere else.
                "cover_url": get_cover_url(m, config),
            }
            for m in entry.get("movies", [])
        ]
        movies.sort(key=lambda m: m["year"] or 0)
        owned_count = sum(1 for m in movies if m["owned"])

        collections.append({
            "collection_id": entry.get("id"),
            "title": entry.get("title", "(untitled collection)"),
            "quality_profile_id": entry.get("qualityProfileId"),
            "root_folder_path": entry.get("rootFolderPath"),
            "owned_count": owned_count,
            "total_count": len(movies),
            "movies": movies,
            "cover_url": get_cover_url(entry, config),
        })

    collections.sort(key=lambda c: c["title"].lower())
    return collections


@app.route("/api/collections/add-movie/<instance_name>", methods=["POST"])
@login_required
@permission_required("searching")
def api_add_missing_collection_movie(instance_name):
    """
    Adds a movie Radarr doesn't know about yet (found via a
    collection) and searches for it immediately. Uses the quality
    profile and root folder already configured on that collection, so
    it matches however you'd normally add movies from it in Radarr's
    own UI.
    """
    config = APPS.get(instance_name)
    if not config or config.get("library_kind") != "movie":
        return jsonify({"ok": False, "error": "not a movie instance"}), 404

    body = request.get_json(force=True)
    payload = {
        "title": body.get("title", ""),
        "tmdbId": body.get("tmdb_id"),
        "qualityProfileId": body.get("quality_profile_id"),
        "rootFolderPath": body.get("root_folder_path"),
        "monitored": True,
        "minimumAvailability": "announced",
        "addOptions": {"searchForMovie": True},
    }

    url = config["url"].rstrip("/") + "/api/v3/movie"
    headers = {"X-Api-Key": config["api_key"]}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        detail = response.text[:300] if response is not None else "no response body"
        return jsonify({"ok": False, "error": f"HTTP {response.status_code}: {detail}"}), 502
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Detail views (opened by clicking a cover image)
# ---------------------------------------------------------------------------


def get_rating_text(ratings: dict) -> str:
    """
    Radarr/Sonarr's "ratings" object can contain several sources
    (imdb, tmdb, rottenTomatoes, metacritic) and not all are always
    populated. Show whichever is available, preferring imdb.
    """
    if not ratings:
        return "Not rated"
    for source in ("imdb", "tmdb", "rottenTomatoes", "metacritic"):
        entry = ratings.get(source)
        if entry and entry.get("value"):
            return f"{entry['value']} ({source})"
    return "Not rated"


def fetch_movie_details(config: dict, movie_id: int) -> dict:
    base = config["url"].rstrip("/")
    headers = {"X-Api-Key": config["api_key"]}

    movie_resp = requests.get(f"{base}/api/v3/movie/{movie_id}", headers=headers, timeout=10)
    movie_resp.raise_for_status()
    movie = movie_resp.json()

    cast = []
    cast_available = True
    try:
        credit_resp = requests.get(
            f"{base}/api/v3/credit", headers=headers,
            params={"movieId": movie_id}, timeout=10,
        )
        credit_resp.raise_for_status()
        credits_data = credit_resp.json()
        cast = [
            {
                "name": c.get("personName", "?"), "role": c.get("character", ""),
                # Best-guess field name for the TMDB person id on Radarr's
                # credit resource - if filmography lookups never work,
                # check the raw JSON here for the actual field name.
                "person_tmdb_id": c.get("personTmdbId"),
                "image_url": get_person_image_url(c, config),
            }
            for c in credits_data if c.get("type") == "cast"
        ][:20]
    except requests.exceptions.RequestException:
        # Older Radarr versions, or ones without TMDB credit import enabled,
        # may not have this endpoint - degrade gracefully instead of failing.
        cast_available = False

    return {
        "overview": movie.get("overview", "No description available."),
        "rating": get_rating_text(movie.get("ratings", {})),
        "cast": cast,
        "cast_available": cast_available,
        # Only present when the movie currently has a downloaded file -
        # the Replace button needs this to delete the right file.
        "movie_file_id": (movie.get("movieFile") or {}).get("id"),
    }


def fetch_series_details(config: dict, series_id: int) -> dict:
    base = config["url"].rstrip("/")
    headers = {"X-Api-Key": config["api_key"]}

    series_resp = requests.get(f"{base}/api/v3/series/{series_id}", headers=headers, timeout=10)
    series_resp.raise_for_status()
    series = series_resp.json()

    seasons = [
        {
            "season_number": s.get("seasonNumber"),
            "have": (s.get("statistics") or {}).get("episodeFileCount", 0),
            "total": (s.get("statistics") or {}).get("episodeCount", 0),
        }
        for s in series.get("seasons", [])
    ]

    # Sonarr doesn't expose a cast/credit endpoint the way Radarr does
    # (its metadata pipeline is TVDB-based, not TMDB-based) - confirmed
    # via a 404 on /api/v3/credit. Pull cast from TVDB directly instead,
    # using the series' own tvdbId.
    cast_result = fetch_series_cast(series.get("tvdbId"))

    return {
        "overview": series.get("overview", "No description available."),
        "rating": get_rating_text(series.get("ratings", {})),
        "seasons": seasons,
        "cast": cast_result["cast"],
        "cast_available": cast_result["cast_available"],
    }


def fetch_sonarr_episodes(config: dict, series_id: int, season_number: int) -> list:
    base = config["url"].rstrip("/")
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.get(
        f"{base}/api/v3/episode", headers=headers,
        params={"seriesId": series_id}, timeout=10,
    )
    response.raise_for_status()
    episodes = [e for e in response.json() if e.get("seasonNumber") == season_number]
    episodes.sort(key=lambda e: e.get("episodeNumber", 0))
    return [
        {
            "episode_id": e.get("id"),
            "episode_number": e.get("episodeNumber"),
            "title": e.get("title", "(untitled)"),
            "air_date": e.get("airDate", ""),
            "has_file": e.get("hasFile", False),
        }
        for e in episodes
    ]


def fetch_artist_albums(config: dict, artist_id: int) -> dict:
    base = config["url"].rstrip("/")
    headers = {"X-Api-Key": config["api_key"]}

    artist_response = requests.get(f"{base}/api/v1/artist/{artist_id}", headers=headers, timeout=10)
    artist_response.raise_for_status()
    artist = artist_response.json()

    audiodb_bio = fetch_audiodb_artist_bio(artist.get("foreignArtistId"))
    overview = audiodb_bio or artist.get("overview") or "No description available."

    response = requests.get(
        f"{base}/api/v1/album", headers=headers,
        params={"artistId": artist_id}, timeout=10,
    )
    response.raise_for_status()
    albums = sorted(response.json(), key=lambda a: a.get("releaseDate", "") or "")
    return {
        "overview": overview,
        "albums": [
            {
                "album_id": a.get("id"),
                "title": a.get("title", "(untitled)"),
                "year": (a.get("releaseDate") or "")[:4],
            }
            for a in albums
        ]
    }


def fetch_lidarr_tracks(config: dict, album_id: int) -> list:
    base = config["url"].rstrip("/")
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.get(
        f"{base}/api/v1/track", headers=headers,
        params={"albumId": album_id}, timeout=10,
    )
    response.raise_for_status()
    tracks = sorted(response.json(), key=lambda t: t.get("trackNumber", 0))
    return [
        {
            "track_number": t.get("trackNumber"),
            "title": t.get("title", "(untitled)"),
            "has_file": t.get("hasFile", False),
        }
        for t in tracks
    ]


def fetch_author_books(config: dict, author_id: int) -> dict:
    base = config["url"].rstrip("/")
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.get(
        f"{base}/api/v1/book", headers=headers,
        params={"authorId": author_id}, timeout=10,
    )
    response.raise_for_status()
    books = sorted(response.json(), key=lambda b: b.get("releaseDate", "") or "")
    return {
        "books": [
            {
                "title": b.get("title", "(untitled)"),
                "year": (b.get("releaseDate") or "")[:4],
            }
            for b in books
        ]
    }


@app.route("/api/details/<instance_name>/<int:item_id>")
@login_required
def api_details(instance_name, item_id):
    config = APPS.get(instance_name)
    if not config:
        return jsonify({"error": "unknown instance"}), 404

    try:
        kind = config["library_kind"]
        if kind == "movie":
            return jsonify(fetch_movie_details(config, item_id))
        if kind == "series":
            return jsonify(fetch_series_details(config, item_id))
        if kind == "artist":
            return jsonify(fetch_artist_albums(config, item_id))
        if kind == "author":
            return jsonify(fetch_author_books(config, item_id))
        if kind == "comic":
            return jsonify(fetch_kapowarr_issues(config, item_id))
        if kind == "librarr":
            details = librarr_item_details(instance_name, config, item_id)
            if details is None:
                return jsonify({"error": "that title is no longer in Librarr's library"}), 404
            return jsonify(details)
        return jsonify({"error": "unsupported library kind"}), 400
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502


@app.route("/api/details/sonarr-episodes/<instance_name>/<int:series_id>/<int:season_number>")
@login_required
def api_sonarr_episodes(instance_name, series_id, season_number):
    config = APPS.get(instance_name)
    if not config:
        return jsonify({"error": "unknown instance"}), 404
    try:
        episodes = fetch_sonarr_episodes(config, series_id, season_number)
        return jsonify(episodes)
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502


@app.route("/api/details/lidarr-tracks/<instance_name>/<int:album_id>")
@login_required
def api_lidarr_tracks(instance_name, album_id):
    config = APPS.get(instance_name)
    if not config:
        return jsonify({"error": "unknown instance"}), 404
    try:
        tracks = fetch_lidarr_tracks(config, album_id)
        return jsonify(tracks)
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502


# ---------------------------------------------------------------------------
# Search (automated + interactive)
# ---------------------------------------------------------------------------

# Each app names its "search for this" background job differently, and
# expects a different id field in the command payload.
AUTO_SEARCH_COMMANDS = {
    "movie": lambda item_id: {"name": "MoviesSearch", "movieIds": [item_id]},
    "series": lambda item_id: {"name": "SeriesSearch", "seriesId": item_id},
    "artist": lambda item_id: {"name": "ArtistSearch", "artistId": item_id},
    "author": lambda item_id: {"name": "AuthorSearch", "authorId": item_id},
}

# Interactive release search uses a different id parameter name per kind.
RELEASE_SEARCH_PARAM = {
    "movie": "movieId",
    "series": "seriesId",
    "artist": "artistId",
    "author": "authorId",
}


def api_version(config: dict) -> str:
    return "v3" if "v3" in config["queue_endpoint"] else "v1"


def trigger_auto_search(config: dict, item_id: int) -> None:
    kind = config["library_kind"]
    build_payload = AUTO_SEARCH_COMMANDS.get(kind)
    if not build_payload:
        raise ValueError(f"no auto-search command for kind '{kind}'")

    url = f"{config['url'].rstrip('/')}/api/{api_version(config)}/command"
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.post(url, headers=headers, json=build_payload(item_id), timeout=15)
    response.raise_for_status()


@app.route("/api/search/auto/<instance_name>/<int:item_id>", methods=["POST"])
@login_required
@permission_required("searching")
def api_search_auto(instance_name, item_id):
    config = APPS.get(instance_name)
    if not config:
        return jsonify({"ok": False, "error": "unknown instance"}), 404
    try:
        trigger_auto_search(config, item_id)
        return jsonify({"ok": True})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502


@app.route("/api/movie/replace/<instance_name>/<int:item_id>", methods=["POST"])
@login_required
@permission_required("searching")
def api_replace_movie(instance_name, item_id):
    """Deletes the movie's current file, then searches for a
    replacement - two steps, reported separately so a failure on
    either one is unambiguous about what actually happened."""
    config = APPS.get(instance_name)
    if not config or config.get("library_kind") != "movie":
        return jsonify({"ok": False, "error": "not a movie instance"}), 404

    body = request.get_json(force=True)
    movie_file_id = body.get("movie_file_id")
    base = config["url"].rstrip("/")
    headers = {"X-Api-Key": config["api_key"]}

    if movie_file_id:
        try:
            response = requests.delete(f"{base}/api/v3/moviefile/{movie_file_id}", headers=headers, timeout=15)
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            return jsonify({"ok": False, "error": f"Could not delete existing file: {err}"}), 502

    try:
        trigger_auto_search(config, item_id)
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": f"File deleted, but the new search failed to start: {err}"}), 502

    return jsonify({"ok": True})


def fetch_interactive_releases(config: dict, item_id: int) -> list:
    kind = config["library_kind"]
    param_name = RELEASE_SEARCH_PARAM.get(kind)
    if not param_name:
        raise ValueError(f"no release search param for kind '{kind}'")

    url = f"{config['url'].rstrip('/')}/api/{api_version(config)}/release"
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.get(url, headers=headers, params={param_name: item_id}, timeout=30)
    response.raise_for_status()

    # Field names below are consistent across Radarr/Sonarr/Lidarr/Readarr
    # since they share the same underlying codebase lineage.
    return [
        {
            "guid": r.get("guid"),
            "indexer_id": r.get("indexerId"),
            "title": r.get("title", "(untitled release)"),
            "indexer": r.get("indexer", "?"),
            "size_gb": round((r.get("size") or 0) / (1024 ** 3), 2),
            "seeders": r.get("seeders"),
            "quality": ((r.get("quality") or {}).get("quality") or {}).get("name", "?"),
            "rejected": bool(r.get("rejected", False)),
        }
        for r in response.json()
    ]


@app.route("/api/search/interactive/<instance_name>/<int:item_id>")
@login_required
@permission_required("searching")
def api_search_interactive(instance_name, item_id):
    config = APPS.get(instance_name)
    if not config:
        return jsonify({"error": "unknown instance"}), 404
    try:
        return jsonify(fetch_interactive_releases(config, item_id))
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502


@app.route("/api/search/grab/<instance_name>", methods=["POST"])
@login_required
@permission_required("searching")
def api_search_grab(instance_name):
    config = APPS.get(instance_name)
    if not config:
        return jsonify({"ok": False, "error": "unknown instance"}), 404

    body = request.get_json(force=True)
    url = f"{config['url'].rstrip('/')}/api/{api_version(config)}/release"
    headers = {"X-Api-Key": config["api_key"]}
    payload = {"guid": body.get("guid"), "indexerId": body.get("indexer_id")}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        return jsonify({"ok": True})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502


def trigger_episode_auto_search(config: dict, episode_id: int) -> None:
    """Sonarr treats 'search this one episode' as its own command,
    separate from SeriesSearch (which searches every missing episode
    in the whole show)."""
    url = f"{config['url'].rstrip('/')}/api/v3/command"
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.post(url, headers=headers, json={"name": "EpisodeSearch", "episodeIds": [episode_id]}, timeout=15)
    response.raise_for_status()


@app.route("/api/search/episode-auto/<instance_name>/<int:episode_id>", methods=["POST"])
@login_required
@permission_required("searching")
def api_search_episode_auto(instance_name, episode_id):
    config = APPS.get(instance_name)
    if not config or config.get("library_kind") != "series":
        return jsonify({"ok": False, "error": "not a series instance"}), 404
    try:
        trigger_episode_auto_search(config, episode_id)
        return jsonify({"ok": True})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502


def fetch_episode_releases(config: dict, episode_id: int) -> list:
    url = f"{config['url'].rstrip('/')}/api/v3/release"
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.get(url, headers=headers, params={"episodeId": episode_id}, timeout=30)
    response.raise_for_status()
    return [
        {
            "guid": r.get("guid"),
            "indexer_id": r.get("indexerId"),
            "title": r.get("title", "(untitled release)"),
            "indexer": r.get("indexer", "?"),
            "size_gb": round((r.get("size") or 0) / (1024 ** 3), 2),
            "seeders": r.get("seeders"),
            "quality": ((r.get("quality") or {}).get("quality") or {}).get("name", "?"),
            "rejected": bool(r.get("rejected", False)),
        }
        for r in response.json()
    ]


@app.route("/api/search/episode-interactive/<instance_name>/<int:episode_id>")
@login_required
@permission_required("searching")
def api_search_episode_interactive(instance_name, episode_id):
    config = APPS.get(instance_name)
    if not config or config.get("library_kind") != "series":
        return jsonify({"error": "not a series instance"}), 404
    try:
        return jsonify(fetch_episode_releases(config, episode_id))
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502


def trigger_album_auto_search(config: dict, album_id: int) -> None:
    """
    Lidarr has no per-track search command (music is released as whole
    albums, not individual tracks) - AlbumSearch is the finest
    granularity available, same one used for "search this album" from
    Lidarr's own UI.
    """
    url = f"{config['url'].rstrip('/')}/api/v1/command"
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.post(url, headers=headers, json={"name": "AlbumSearch", "albumIds": [album_id]}, timeout=15)
    response.raise_for_status()


@app.route("/api/search/album-auto/<instance_name>/<int:album_id>", methods=["POST"])
@login_required
@permission_required("searching")
def api_search_album_auto(instance_name, album_id):
    config = APPS.get(instance_name)
    if not config or config.get("library_kind") != "artist":
        return jsonify({"ok": False, "error": "not a Lidarr instance"}), 404
    try:
        trigger_album_auto_search(config, album_id)
        return jsonify({"ok": True})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502


def fetch_album_releases(config: dict, album_id: int) -> list:
    url = f"{config['url'].rstrip('/')}/api/v1/release"
    headers = {"X-Api-Key": config["api_key"]}
    response = requests.get(url, headers=headers, params={"albumId": album_id}, timeout=30)
    response.raise_for_status()
    return [
        {
            "guid": r.get("guid"),
            "indexer_id": r.get("indexerId"),
            "title": r.get("title", "(untitled release)"),
            "indexer": r.get("indexer", "?"),
            "size_gb": round((r.get("size") or 0) / (1024 ** 3), 2),
            "seeders": r.get("seeders"),
            "quality": ((r.get("quality") or {}).get("quality") or {}).get("name", "?"),
            "rejected": bool(r.get("rejected", False)),
        }
        for r in response.json()
    ]


@app.route("/api/search/album-interactive/<instance_name>/<int:album_id>")
@login_required
@permission_required("searching")
def api_search_album_interactive(instance_name, album_id):
    config = APPS.get(instance_name)
    if not config or config.get("library_kind") != "artist":
        return jsonify({"error": "not a Lidarr instance"}), 404
    try:
        return jsonify(fetch_album_releases(config, album_id))
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502


# ---------------------------------------------------------------------------
# Overseerr / Jellyseerr integration
# ---------------------------------------------------------------------------


def seerr_headers(requester: dict) -> dict:
    return {"X-Api-Key": requester["credential"]}


def fetch_seerr_media_info(requester: dict, media_type: str, tmdb_id) -> dict:
    """
    Overseerr's request list only gives back a tmdbId, not a clean title
    or image - this looks up the actual movie/show details (which mirror
    TMDB's schema) to get both. posterPath is a relative path that needs
    TMDB's image CDN prefix to become a real, loadable URL.
    """
    if not tmdb_id:
        return {"title": "(unknown)", "poster_url": ""}

    kind_path = "movie" if media_type == "movie" else "tv"
    url = f"{requester['url'].rstrip('/')}/api/v1/{kind_path}/{tmdb_id}"

    try:
        response = requests.get(url, headers=seerr_headers(requester), timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException:
        return {"title": f"(id: {tmdb_id})", "poster_url": ""}

    title = data.get("title") or data.get("name") or f"(id: {tmdb_id})"
    poster_path = data.get("posterPath")
    poster_url = f"https://image.tmdb.org/t/p/w200{poster_path}" if poster_path else ""
    return {"title": title, "poster_url": poster_url}


def fetch_seerr_requests(requester, media_type) -> dict:
    if requester is None or media_type is None:
        return {"error": None, "request_items": []}

    url = requester["url"].rstrip("/") + "/api/v1/request"
    params = {"filter": "all", "take": 25, "sort": "added"}

    try:
        response = requests.get(url, headers=seerr_headers(requester), params=params, timeout=10)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect to requester ({requester['url']})", "request_items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check requester API key", "request_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "request_items": []}

    data = response.json()
    results = data.get("results", data) if isinstance(data, dict) else data

    request_items = []
    for item in results:
        media = item.get("media", {})
        if media.get("mediaType") != media_type:
            continue
        info = fetch_seerr_media_info(requester, media_type, media.get("tmdbId"))
        request_items.append({
            "title": info["title"],
            "poster_url": info["poster_url"],
            "status": SEERR_REQUEST_STATUS.get(item.get("status"), "Unknown"),
            "status_class": SEERR_REQUEST_STATUS_CLASS.get(item.get("status"), "notice"),
            "media_status": SEERR_MEDIA_STATUS.get(media.get("status"), "Unknown"),
            "is4k": bool(item.get("is4k")),
            "requested_by": (item.get("requestedBy") or {}).get("displayName", "unknown"),
            "requested_date": (item.get("createdAt") or "")[:10],
        })
    return {"error": None, "request_items": request_items}


@app.route("/api/seerr/search")
@login_required
@permission_required("requesting")
def api_seerr_search():
    requester = get_active_requester()
    if not requester:
        return jsonify({"error": "no requester is configured/enabled"}), 400

    query = request.args.get("q", "").strip()
    media_type = request.args.get("media_type", "")
    if not query:
        return jsonify([])

    # requests' params= dict encodes spaces as "+" by default, but
    # Overseerr's validator wants strict percent-encoding ("%20") and
    # rejects "+" as an unencoded reserved character - so build the
    # query string ourselves instead of letting requests do it.
    encoded_query = quote(query, safe="")
    url = f"{requester['url'].rstrip('/')}/api/v1/search?query={encoded_query}"

    try:
        response = requests.get(url, headers=seerr_headers(requester), timeout=10)
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        detail = response.text[:300] if response is not None else "no response body"
        return jsonify({"error": f"HTTP {response.status_code}: {detail}"}), 502
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502

    data = response.json()
    results = data.get("results", [])

    filtered = [
        {
            "tmdb_id": r.get("id"),
            "media_type": r.get("mediaType"),
            "title": r.get("title") or r.get("name") or "(untitled)",
            "year": (r.get("releaseDate") or r.get("firstAirDate") or "")[:4],
            "poster_url": f"https://image.tmdb.org/t/p/w200{r['posterPath']}" if r.get("posterPath") else "",
        }
        for r in results
        if r.get("mediaType") == media_type
    ]
    return jsonify(filtered)


@app.route("/api/seerr/request", methods=["POST"])
@login_required
@permission_required("requesting")
def api_seerr_request():
    requester = get_active_requester()
    if not requester:
        return jsonify({"ok": False, "error": "no requester is configured/enabled"}), 400

    body = request.get_json(force=True)
    media_type = body.get("media_type")
    tmdb_id = body.get("tmdb_id")

    url = requester["url"].rstrip("/") + "/api/v1/request"
    payload = {"mediaType": media_type, "mediaId": tmdb_id}

    # Movies are requested whole, but Overseerr requires TV requests to
    # explicitly say which seasons to grab - "all" requests every season.
    if media_type == "tv":
        payload["seasons"] = "all"

    try:
        response = requests.post(url, headers=seerr_headers(requester), json=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        detail = response.text[:300] if response is not None else "no response body"
        return jsonify({"ok": False, "error": f"HTTP {response.status_code}: {detail}"}), 502
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Plex / Jellyfin / Emby "play" links
# ---------------------------------------------------------------------------

# Maps our library_kind to what each service calls that type of item.
PLAY_LINK_TYPES = {
    "movie": ("movie", "Movie"),
    "series": ("show", "Series"),
    "artist": ("artist", "MusicArtist"),
}


def get_plex_machine_id(server: dict):
    """
    The local Plex web UI deep-link format needs the server's own
    machineIdentifier - this is a small, unauthenticated endpoint just
    for that.
    """
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/identity",
            headers={"Accept": "application/json"}, timeout=5,
        )
        response.raise_for_status()
        return response.json().get("MediaContainer", {}).get("machineIdentifier")
    except requests.exceptions.RequestException:
        return None


def find_plex_link(server: dict, title: str, plex_type: str) -> str:
    try:
        params = {"query": title, "X-Plex-Token": server["credential"]}
        response = requests.get(
            f"{server['url'].rstrip('/')}/search",
            headers={"Accept": "application/json"}, params=params, timeout=8,
        )
        response.raise_for_status()
        items = response.json().get("MediaContainer", {}).get("Metadata", []) or []
    except requests.exceptions.RequestException:
        return ""

    candidates = [i for i in items if i.get("type") == plex_type]
    if not candidates:
        return ""

    # Prefer an exact (case-insensitive) title match over Plex's own
    # search ranking, since a loose match could point at the wrong item.
    exact = next((i for i in candidates if i.get("title", "").lower() == title.lower()), None)
    match = exact or candidates[0]
    rating_key = match.get("ratingKey")
    if not rating_key:
        return ""

    machine_id = get_plex_machine_id(server)
    if not machine_id:
        return ""

    return (
        f"{server['url'].rstrip('/')}/web/index.html#!/server/{machine_id}"
        f"/details?key=%2Flibrary%2Fmetadata%2F{rating_key}"
    )


def jellyfin_family_headers(kind: str, token: str) -> dict:
    """
    Auth headers for Jellyfin and Emby. Jellyfin 10.12/12.0 switched its
    legacy authorization off by default (X-Emby-Token and friends, the api_key
    query parameter), and answers 401 "CustomAuthentication was challenged" to
    anything but the current form: Authorization: MediaBrowser Token="...".
    Anything that isn't Emby gets both so older Jellyfin servers keep working;
    Emby only knows the X-Emby-Token header.
    """
    token = (token or "").replace('"', "")
    headers = {"X-Emby-Token": token}
    if kind != "emby":
        headers["Authorization"] = f'MediaBrowser Token="{token}"'
    return headers


def jellyfin_family_token_params(kind: str, token: str) -> dict:
    """The same token as query parameters, for URLs a browser loads itself
    (poster images can't carry headers). Jellyfin's current name is ApiKey;
    api_key is what older Jellyfin and Emby use."""
    params = {"api_key": token}
    if kind != "emby":
        params["ApiKey"] = token
    return params


def get_jellyfin_server_id(server: dict):
    try:
        response = requests.get(f"{server['url'].rstrip('/')}/System/Info/Public", timeout=5)
        response.raise_for_status()
        return response.json().get("Id")
    except requests.exceptions.RequestException:
        return None


def find_jellyfin_link(server: dict, title: str, item_type: str) -> str:
    """Works for both Jellyfin and Emby - they share the same API."""
    try:
        headers = jellyfin_family_headers(server.get("type"), server["credential"])
        params = {
            "searchTerm": title, "IncludeItemTypes": item_type,
            "Recursive": "true", "Limit": 10,
            **jellyfin_family_token_params(server.get("type"), server["credential"]),
        }
        response = requests.get(
            f"{server['url'].rstrip('/')}/Items",
            headers=headers, params=params, timeout=8,
        )
        response.raise_for_status()
        items = response.json().get("Items", []) or []
    except requests.exceptions.RequestException:
        return ""

    if not items:
        return ""

    exact = next((i for i in items if i.get("Name", "").lower() == title.lower()), None)
    match = exact or items[0]
    item_id = match.get("Id")
    if not item_id:
        return ""

    server_id = get_jellyfin_server_id(server)
    if not server_id:
        return ""

    return f"{server['url'].rstrip('/')}/web/index.html#!/details?id={item_id}&serverId={server_id}"


@app.route("/api/play-links")
@login_required
def api_play_links():
    title = request.args.get("title", "").strip()
    kind = request.args.get("kind", "")

    if not title or kind not in PLAY_LINK_TYPES:
        return jsonify({"links": []})

    # Our own movie card titles include a trailing " (YYYY)" that Radarr
    # itself doesn't use in its title field - strip it before searching
    # so the match isn't thrown off by it.
    clean_title = re.sub(r"\s\(\d{4}\)$", "", title)
    plex_type, jellyfin_type = PLAY_LINK_TYPES[kind]

    links = []
    for server in MEDIA_SERVERS:
        if not server.get("enabled") or not server.get("url"):
            continue
        server_type = server.get("type")
        if server_type == "plex":
            url = find_plex_link(server, clean_title, plex_type)
        elif server_type in ("jellyfin", "emby"):
            url = find_jellyfin_link(server, clean_title, jellyfin_type)
        else:
            continue
        if url:
            links.append({"name": server.get("name", server_type), "url": url, "type": server_type})

    return jsonify({"links": links})


# ---------------------------------------------------------------------------
# TMDB actor filmography (click a cast member's name)
# ---------------------------------------------------------------------------


def tmdb_get(path: str, params: dict = None) -> dict:
    params = dict(params or {})
    params["api_key"] = TMDB["api_key"]
    response = requests.get(f"https://api.themoviedb.org/3{path}", params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_tv_tvdb_id(tmdb_tv_id) -> "int | None":
    """One extra TMDB call to translate a TMDB TV id into a TVDB id,
    since Sonarr identifies shows by TVDB id, not TMDB id."""
    try:
        data = tmdb_get(f"/tv/{tmdb_tv_id}/external_ids")
        return data.get("tvdb_id")
    except requests.exceptions.RequestException:
        return None


@app.route("/api/tmdb-movie-info/<int:tmdb_id>")
@login_required
def api_tmdb_movie_info(tmdb_id):
    """
    For a movie that isn't in Radarr yet (found via a collection) -
    pulls a quick overview/rating straight from TMDB, since we have
    nothing from Radarr itself to show for something it doesn't own.
    """
    if not TMDB.get("api_key"):
        return jsonify({"error": "TMDB API key isn't configured in Settings"}), 400

    try:
        data = tmdb_get(f"/movie/{tmdb_id}")
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502

    rating = f"{data['vote_average']:.1f} / 10 (TMDB)" if data.get("vote_average") else "Not rated"
    return jsonify({
        "overview": data.get("overview") or "No description available.",
        "rating": rating,
    })


@app.route("/api/actor-filmography/<instance_name>")
@login_required
def api_actor_filmography(instance_name):
    if not TMDB.get("api_key"):
        return jsonify({"error": "TMDB API key isn't configured in Settings"}), 400

    config = APPS.get(instance_name)
    if not config:
        return jsonify({"error": "unknown instance"}), 404

    kind = config["library_kind"]
    if kind not in ("movie", "series"):
        return jsonify({"error": "not applicable to this instance"}), 400

    person_id = request.args.get("person_id", "").strip()
    if not person_id:
        return jsonify({"error": "missing person_id"}), 400

    try:
        credits_data = tmdb_get(f"/person/{person_id}/combined_credits")
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502

    media_type = "movie" if kind == "movie" else "tv"
    # De-dupe (the same person can appear twice for a title, e.g. actor
    # and director) and sort by popularity so the most relevant credits
    # are checked first, since TV matching below is capped for cost.
    seen_ids = set()
    credits_list = []
    for c in credits_data.get("cast", []) + credits_data.get("crew", []):
        if c.get("media_type") != media_type or c.get("id") in seen_ids:
            continue
        seen_ids.add(c["id"])
        credits_list.append(c)
    credits_list.sort(key=lambda c: c.get("popularity", 0), reverse=True)

    try:
        library = fetch_library(instance_name, config)
    except requests.exceptions.RequestException as err:
        return jsonify({"error": str(err)}), 502

    matches = []
    if kind == "movie":
        # Map tmdb_id -> this instance's own item_id for each owned movie,
        # so a clicked result can open that exact item's detail modal.
        owned = {item["tmdb_id"]: item["item_id"] for item in library["library_items"] if item.get("tmdb_id")}
        matches = [(c, owned[c["id"]]) for c in credits_list if c.get("id") in owned]
    else:
        owned = {item["tvdb_id"]: item["item_id"] for item in library["library_items"] if item.get("tvdb_id")}
        # Capped to bound the extra per-credit TMDB calls this needs -
        # sorted by popularity above, so the cap rarely matters in practice.
        for c in credits_list[:50]:
            tvdb_id = fetch_tv_tvdb_id(c["id"])
            if tvdb_id in owned:
                matches.append((c, owned[tvdb_id]))

    results = [
        {
            "title": c.get("title") or c.get("name") or "(untitled)",
            "year": (c.get("release_date") or c.get("first_air_date") or "")[:4],
            "item_id": item_id,
            "poster_url": f"https://image.tmdb.org/t/p/w200{c['poster_path']}" if c.get("poster_path") else "",
        }
        for c, item_id in matches
    ]
    return jsonify({"results": results})


# ---------------------------------------------------------------------------
# TVDB (TheTVDB) - Sonarr cast lists. Sonarr's own API doesn't expose
# cast/credit data (confirmed via a 404 earlier), so this pulls it
# directly from TheTVDB instead, using the series' own tvdbId.
# ---------------------------------------------------------------------------


_tvdb_token_cache = {"token": None, "obtained_at": 0.0}


def tvdb_login():
    """
    TVDB uses a login-for-a-token flow rather than a simple query-string
    key - exchange the API key (+ optional subscriber PIN) for a bearer
    token, valid for about a month. Cached in memory for 25 days (a bit
    under the real limit, as a safety margin) instead of re-authenticating
    on every single TVDB call.
    """
    if not TVDB.get("api_key"):
        return None

    cache_age = time.time() - _tvdb_token_cache["obtained_at"]
    if _tvdb_token_cache["token"] and cache_age < 25 * 24 * 60 * 60:
        return _tvdb_token_cache["token"]

    payload = {"apikey": TVDB["api_key"]}
    if TVDB.get("pin"):
        payload["pin"] = TVDB["pin"]

    try:
        response = requests.post("https://api4.thetvdb.com/v4/login", json=payload, timeout=10)
        response.raise_for_status()
        token = response.json().get("data", {}).get("token")
        if token:
            _tvdb_token_cache["token"] = token
            _tvdb_token_cache["obtained_at"] = time.time()
        return token
    except requests.exceptions.RequestException:
        return None


def fetch_series_cast(tvdb_series_id) -> dict:
    """
    TVDB's cast/character data lives on a series' "extended" record.
    Field names for each character entry are a best guess (personName,
    name-as-character-role, image) - if cast looks wrong or empty, the
    raw JSON from this endpoint is the place to check.
    """
    if not tvdb_series_id:
        return {"cast": [], "cast_available": False}

    token = tvdb_login()
    if not token:
        return {"cast": [], "cast_available": False}

    try:
        response = requests.get(
            f"https://api4.thetvdb.com/v4/series/{tvdb_series_id}/extended",
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
    except requests.exceptions.RequestException:
        return {"cast": [], "cast_available": False}

    characters = data.get("characters") or []
    cast = [
        {
            "name": c.get("personName", "?"),
            "role": c.get("name", ""),
            # TVDB people aren't cross-referenced with TMDB's person IDs,
            # so these names aren't clickable for the filmography feature
            # the way movie cast is - that's TMDB-specific.
            "person_tmdb_id": None,
            "image_url": c.get("image") or "",
        }
        for c in characters
        if not c.get("peopleType") or c.get("peopleType") == "Actor"
    ][:20]

    return {"cast": cast, "cast_available": True}


# ---------------------------------------------------------------------------
# Fanart.tv - better artist images for Lidarr than what Lidarr's own
# "images" data reliably provides. Looks up by MusicBrainz ID, which
# Lidarr already carries on every artist record (Lidarr is itself
# MusicBrainz-based under the hood).
# ---------------------------------------------------------------------------


def fetch_fanart_artist_image(mbid) -> str:
    """
    Best-guess field names: Lidarr's MusicBrainz ID field on an artist
    record is assumed to be "foreignArtistId", and the image list key
    in Fanart.tv's response is assumed to be "artistthumb" - check the
    raw JSON here first if images don't show up.
    """
    if not mbid or not FANART.get("api_key"):
        return ""

    try:
        response = requests.get(
            f"https://webservice.fanart.tv/v3/music/{mbid}",
            params={"api_key": FANART["api_key"]}, timeout=6,
        )
        if not response.ok:
            return ""
        data = response.json()
    except requests.exceptions.RequestException:
        return ""

    thumbs = data.get("artistthumb") or []
    if not thumbs:
        return ""
    thumbs.sort(key=lambda t: int(t.get("likes") or 0), reverse=True)
    return thumbs[0].get("url", "")


def enrich_artist_covers_with_fanart(library_items: list) -> None:
    """
    Fills in Fanart.tv images for artists Lidarr has no cover for at
    all - only for those (not as a blanket override), and fired in
    parallel rather than one-by-one. Doing this sequentially inside
    get_library_item was pushing whole page loads for larger libraries
    past gunicorn's worker timeout, since it's one live external call
    per artist. Mutates library_items in place.
    """
    targets = [item for item in library_items if not item["cover_url"] and item.get("foreign_artist_id")]
    if not targets:
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_item = {
            executor.submit(fetch_fanart_artist_image, item["foreign_artist_id"]): item
            for item in targets
        }
        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                url = future.result()
                if url:
                    item["cover_url"] = url
            except Exception:
                pass  # leave that one artist without a cover rather than fail the whole page


def fetch_audiodb_artist_bio(mbid) -> str:
    """
    Lidarr/MusicBrainz has no artist biography data at all - it's a
    structured metadata database, not a source of bio text. TheAudioDB
    does have one, looked up by the same MusicBrainz ID already used
    for Fanart.tv.
    """
    if not mbid or not THEAUDIODB.get("api_key"):
        return ""

    try:
        response = requests.get(
            f"https://www.theaudiodb.com/api/v1/json/{THEAUDIODB['api_key']}/artist-mb.php",
            params={"i": mbid}, timeout=8,
        )
        if not response.ok:
            return ""
        artists = response.json().get("artists") or []
    except requests.exceptions.RequestException:
        return ""

    if not artists:
        return ""
    # "strBiographyEN" doesn't appear to exist on real responses - the
    # actual field, confirmed against live data, is "strBiography".
    return artists[0].get("strBiography") or ""


# ---------------------------------------------------------------------------
# Downloaders (qBittorrent / SABnzbd) - separate from the arr instances
# entirely, since a download client is usually shared across all of them
# rather than tied to just one.
# ---------------------------------------------------------------------------


def qbt_headers(downloader: dict) -> dict:
    """
    qBittorrent v5.2.0+ (Web API v2.14.1+) supports stateless API key
    auth via a Bearer token - generated in qBittorrent's WebUI settings.
    Simpler than the older username/password cookie-login flow, and
    matches how every other service in this app authenticates.
    """
    return {"Authorization": f"Bearer {downloader.get('api_key', '')}"}


def fetch_qbittorrent_torrents(downloader: dict) -> dict:
    try:
        response = requests.get(
            f"{downloader['url'].rstrip('/')}/api/v2/torrents/info",
            headers=qbt_headers(downloader), timeout=15,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "torrents": []}

    torrents = [
        {
            "hash": t.get("hash"),
            "name": t.get("name", "(unknown)"),
            "state": t.get("state", "unknown"),
            "progress_pct": round((t.get("progress") or 0) * 100, 1),
            "dlspeed": t.get("dlspeed", 0),
            "upspeed": t.get("upspeed", 0),
            "eta": t.get("eta", 0),
            "size": t.get("size", 0),
            "ratio": round(t.get("ratio", 0), 2),
            "category": t.get("category", ""),
            "num_seeds": t.get("num_seeds", 0),
            "num_leechs": t.get("num_leechs", 0),
            "tags": t.get("tags", ""),
        }
        for t in response.json()
    ]
    return {"error": None, "torrents": torrents}


def fetch_qbittorrent_transfer_info(downloader: dict) -> dict:
    """
    GET /api/v2/transfer/info - global (not per-torrent) stats, confirmed
    via qBittorrent's own WebUI API reference. connection_status in
    particular ("connected" / "firewalled" / "disconnected") is a real
    health signal: "firewalled" usually means an unforwarded port is
    silently starving every torrent of incoming peers, which nothing in
    the per-torrent list would ever surface on its own.
    """
    try:
        response = requests.get(
            f"{downloader['url'].rstrip('/')}/api/v2/transfer/info",
            headers=qbt_headers(downloader), timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "connection_status": "", "dl_speed": 0, "up_speed": 0}

    return {
        "error": None,
        "connection_status": data.get("connection_status", ""),
        "dl_speed": data.get("dl_info_speed", 0),
        "up_speed": data.get("up_info_speed", 0),
    }


def qbt_torrent_action(downloader: dict, action: str, torrent_hash: str, delete_files: bool = False) -> None:
    base = downloader["url"].rstrip("/")
    headers = qbt_headers(downloader)

    # qBittorrent v5.0 renamed pause/resume to stop/start (confirmed
    # via qBittorrent's own official WebUI API docs) - installs on
    # v4.x still only recognize the old names, so this tries the new
    # endpoint first and falls back to the old one on a 404, covering
    # both generations rather than assuming one.
    if action == "pause":
        endpoint_names = ["stop", "pause"]
        data = {"hashes": torrent_hash}
    elif action == "resume":
        endpoint_names = ["start", "resume"]
        data = {"hashes": torrent_hash}
    elif action == "delete":
        endpoint_names = ["delete"]
        data = {"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()}
    else:
        raise ValueError(f"unknown action: {action}")

    response = None
    for endpoint_name in endpoint_names:
        response = requests.post(f"{base}/api/v2/torrents/{endpoint_name}", headers=headers, data=data, timeout=10)
        if response.status_code != 404:
            break
    response.raise_for_status()


def fetch_sabnzbd_queue(downloader: dict) -> dict:
    base = downloader["url"].rstrip("/")
    try:
        response = requests.get(
            f"{base}/api",
            params={"mode": "queue", "output": "json", "apikey": downloader.get("api_key", "")}, timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": [], "summary": {}}

    if "error" in data:
        return {"error": data["error"], "items": [], "summary": {}}

    queue = data.get("queue", {})

    items = [
        {
            "nzo_id": slot.get("nzo_id"),
            "name": slot.get("filename", "(unknown)"),
            "status": slot.get("status", "unknown"),
            "percentage": slot.get("percentage", "0"),
            "size_mb": slot.get("mb", "0"),
            "size_left_mb": slot.get("mbleft", "0"),
            "time_left": slot.get("timeleft", ""),
            "category": slot.get("cat", ""),
        }
        for slot in queue.get("slots", [])
    ]

    # Queue-wide fields sitting on the same response, confirmed against
    # SABnzbd's own API reference - previously discarded entirely even
    # though they cover things the per-slot data can't (is SABnzbd
    # paused with an empty queue, is a folder nearly out of space).
    summary = {
        "paused": bool(queue.get("paused", False)),
        "speed": queue.get("speed", ""),
        "time_left": queue.get("timeleft", ""),
        "have_warnings": int(queue.get("have_warnings") or 0),
        "diskspace_temp_free": queue.get("diskspace1", ""),
        "diskspace_temp_total": queue.get("diskspacetotal1", ""),
        "diskspace_complete_free": queue.get("diskspace2", ""),
        "diskspace_complete_total": queue.get("diskspacetotal2", ""),
    }
    return {"error": None, "items": items, "summary": summary}


def sabnzbd_action(downloader: dict, mode: str, nzo_id: str) -> None:
    base = downloader["url"].rstrip("/")
    params = {"output": "json", "apikey": downloader.get("api_key", "")}

    if mode == "delete":
        params.update({"mode": "queue", "name": "delete", "value": nzo_id})
    else:
        params.update({"mode": mode, "value": nzo_id})  # "pause" or "resume"

    response = requests.get(f"{base}/api", params=params, timeout=15)
    response.raise_for_status()


def get_downloader(name: str):
    return next((d for d in DOWNLOADERS if d["name"] == name), None)


@app.route("/api/downloader/status/<name>")
@login_required
def api_downloader_status(name):
    downloader = get_downloader(name)
    if not downloader:
        return jsonify({"error": "unknown downloader"}), 404

    if downloader["type"] == "qbittorrent":
        result = fetch_qbittorrent_torrents(downloader)
        transfer = fetch_qbittorrent_transfer_info(downloader)
        return jsonify({
            "type": "qbittorrent", "error": result["error"], "items": result["torrents"],
            "summary": {
                "connection_status": transfer["connection_status"],
                "dl_speed": transfer["dl_speed"],
                "up_speed": transfer["up_speed"],
            },
        })
    elif downloader["type"] == "sabnzbd":
        result = fetch_sabnzbd_queue(downloader)
        return jsonify({"type": "sabnzbd", "error": result["error"], "items": result["items"], "summary": result["summary"]})
    return jsonify({"error": "unknown downloader type"}), 400


@app.route("/api/downloader/action/<name>", methods=["POST"])
@login_required
@permission_required("searching")
def api_downloader_action(name):
    """Reuses the 'searching' permission, since pausing/resuming/removing
    downloads is the same class of action-taking as the search buttons
    elsewhere - not a read-only view."""
    downloader = get_downloader(name)
    if not downloader:
        return jsonify({"ok": False, "error": "unknown downloader"}), 404

    body = request.get_json(force=True)
    action = body.get("action")
    item_id = body.get("item_id")

    try:
        if downloader["type"] == "qbittorrent":
            qbt_torrent_action(downloader, action, item_id, delete_files=bool(body.get("delete_files")))
        elif downloader["type"] == "sabnzbd":
            sabnzbd_action(downloader, action, item_id)
        else:
            return jsonify({"ok": False, "error": "unknown downloader type"}), 400
    except (requests.exceptions.RequestException, ValueError) as err:
        return jsonify({"ok": False, "error": str(err)}), 502

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Home > Downloads: one card per download client with its current speed and
# the few downloads that are actually moving. Verified against each client's
# own API reference:
# - qBittorrent: GET /api/v2/torrents/info takes `filter` (`downloading`,
#   `stalled_downloading` among the allowed values) and returns `state`,
#   `progress`, `dlspeed`, `eta` (8640000 means "never"), `hash`, `name`;
#   GET /api/v2/transfer/info gives the global `dl_info_speed` /
#   `up_info_speed`. The docs do not define what each filter includes, so the
#   states are checked here as well. Only the two download filters are asked
#   for - a seedbox with thousands of finished torrents is never listed.
# - SABnzbd: mode=queue takes `limit`; `queue` has `status`, `paused`,
#   `kbpersec`, `noofslots_total`, and each slot `nzo_id`, `filename`,
#   `status`, `percentage`, `timeleft`.
# The panel polls every 10 s, so the answer is shared for a few seconds.
# ---------------------------------------------------------------------------

DOWNLOADS_ITEM_LIMIT = 5
DOWNLOADS_SAB_FETCH_LIMIT = 30
DOWNLOADS_CACHE_SECONDS = 5
QBT_NO_ETA = 8640000
_QBT_DOWNLOAD_STATES = {
    "downloading": "downloading", "forcedDL": "downloading", "metaDL": "downloading", "forcedMetaDL": "downloading",
    "stalledDL": "stalled", "queuedDL": "queued", "checkingDL": "preparing", "allocating": "preparing",
}
_DOWNLOAD_GROUP_ORDER = {"downloading": 0, "preparing": 1, "stalled": 2, "queued": 3}
_DOWNLOADS_CACHE: dict = {"at": 0.0, "data": None}
_DOWNLOADS_LOCK = threading.Lock()


def short_request_error(err) -> str:
    """A failed call to a download client in a few plain words: no address, no body."""
    if isinstance(err, requests.exceptions.HTTPError) and err.response is not None:
        code = err.response.status_code
        return f"HTTP {code} - check the API key" if code in (401, 403) else f"HTTP {code}"
    if isinstance(err, requests.exceptions.Timeout):
        return "timed out"
    if isinstance(err, requests.exceptions.ConnectionError):
        return "could not connect"
    return "request failed"


def _download_state(paused: bool, downloading: int, speed: float, total: int) -> str:
    if paused:
        return "paused"
    if downloading or speed > 0:
        return "downloading"
    return "waiting" if total else "idle"


def _percent(value) -> float:
    try:
        return max(0.0, min(100.0, round(float(value), 1)))
    except (TypeError, ValueError):
        return 0.0


def qbt_downloads_view(torrents, transfer: dict) -> dict:
    """qBittorrent's answers -> the panel's card. Pure. Paused / stopped / seeding
    / errored torrents are skipped, and a torrent that both filters returned is
    counted once."""
    counts = {"downloading": 0, "stalled": 0, "queued": 0, "preparing": 0}
    seen, items = set(), []
    for torrent in torrents if isinstance(torrents, list) else []:
        if not isinstance(torrent, dict):
            continue
        group = _QBT_DOWNLOAD_STATES.get(torrent.get("state"))
        key = torrent.get("hash") or torrent.get("name")
        if group is None or key in seen:
            continue
        seen.add(key)
        counts[group] += 1
        eta = torrent.get("eta")
        speed = torrent.get("dlspeed")
        items.append({
            "name": str(torrent.get("name") or "(unknown)")[:200],
            "progress": _percent((torrent.get("progress") or 0) * 100),
            "speed": int(speed) if isinstance(speed, (int, float)) and speed > 0 else 0,
            "eta": human_duration(eta) if isinstance(eta, (int, float)) and 0 <= eta < QBT_NO_ETA else "",
            "group": group,
        })
    items.sort(key=lambda i: (_DOWNLOAD_GROUP_ORDER[i["group"]], -i["speed"], -i["progress"]))
    down = max(0, int(transfer.get("dl_speed") or 0))
    return {
        "error": None, "paused": False, "down_speed": down, "up_speed": max(0, int(transfer.get("up_speed") or 0)),
        "connection": transfer.get("connection_status") or "", "counts": counts, "total": len(items),
        "state": _download_state(False, counts["downloading"], down, len(items)),
        "items": items[:DOWNLOADS_ITEM_LIMIT],
    }


def sab_downloads_view(data) -> dict:
    """SABnzbd's mode=queue answer -> the panel's card. Pure. Paused jobs are
    counted but not listed; a paused queue says so."""
    queue = data.get("queue") if isinstance(data, dict) and isinstance(data.get("queue"), dict) else {}
    slots = [s for s in queue.get("slots") or [] if isinstance(s, dict)]
    counts = {"downloading": 0, "paused": 0, "queued": 0, "preparing": 0}
    items = []
    for slot in slots:
        status = str(slot.get("status") or "").strip().lower()
        group = {"downloading": "downloading", "paused": "paused", "queued": "queued"}.get(status, "preparing")
        counts[group] += 1
        if group == "paused":
            continue
        eta = str(slot.get("timeleft") or "")
        items.append({
            "name": str(slot.get("filename") or "(unknown)")[:200], "progress": _percent(slot.get("percentage")),
            "speed": 0, "eta": "" if eta in ("", "0:00:00") else eta, "group": group,
        })
    items.sort(key=lambda i: _DOWNLOAD_GROUP_ORDER[i["group"]])  # stable: SABnzbd's own queue order within a group
    try:
        speed = max(0, int(float(queue.get("kbpersec") or 0) * 1024))
    except (TypeError, ValueError):
        speed = 0
    try:
        total = int(queue.get("noofslots_total"))
    except (TypeError, ValueError):
        total = len(slots)
    paused = bool(queue.get("paused"))
    return {
        "error": None, "paused": paused, "down_speed": speed, "up_speed": None, "connection": "",
        "counts": counts, "total": total, "state": _download_state(paused, counts["downloading"], speed, total),
        "items": items[:DOWNLOADS_ITEM_LIMIT],
    }


def fetch_download_client(downloader: dict) -> dict:
    """One client's card (or {"error": words}). The calls inside run one after
    another on purpose: the clients themselves are already fetched in parallel
    on the shared pool, and a pool task waiting on its own pool can deadlock."""
    base = downloader["url"].rstrip("/")
    try:
        if downloader["type"] == "qbittorrent":
            torrents = []
            for torrent_filter in ("downloading", "stalled_downloading"):
                response = requests.get(f"{base}/api/v2/torrents/info", headers=qbt_headers(downloader),
                                        params={"filter": torrent_filter}, timeout=8)
                response.raise_for_status()
                torrents.extend(response.json())
            response = requests.get(f"{base}/api/v2/transfer/info", headers=qbt_headers(downloader), timeout=8)
            response.raise_for_status()
            info = response.json()
            return qbt_downloads_view(torrents, {
                "dl_speed": info.get("dl_info_speed"), "up_speed": info.get("up_info_speed"),
                "connection_status": info.get("connection_status"),
            })
        if downloader["type"] == "sabnzbd":
            response = requests.get(f"{base}/api", params={
                "mode": "queue", "output": "json", "limit": DOWNLOADS_SAB_FETCH_LIMIT, "apikey": downloader.get("api_key", ""),
            }, timeout=8)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                return {"error": "SABnzbd refused the request - check the API key"}
            return sab_downloads_view(data)
    except requests.exceptions.JSONDecodeError:
        return {"error": "unexpected answer - is this the right address?"}
    except requests.exceptions.RequestException as err:
        return {"error": short_request_error(err)}
    except (ValueError, AttributeError, TypeError):
        return {"error": "unexpected answer - is this the right address?"}
    return {"error": "unknown downloader type"}


def gather_downloads(force: bool = False) -> dict:
    with _DOWNLOADS_LOCK:
        cached = _DOWNLOADS_CACHE["data"]
        if not force and cached is not None and time.monotonic() - _DOWNLOADS_CACHE["at"] < DOWNLOADS_CACHE_SECONDS:
            return cached
    clients = [d for d in DOWNLOADERS if d.get("enabled", True)]
    outcomes = run_concurrently({d["name"]: (lambda d=d: fetch_download_client(d)) for d in clients})
    cards, down, up = [], 0, 0
    for d in clients:
        ok, value = outcomes[d["name"]]
        view = value if ok and isinstance(value, dict) else {"error": "request failed"}
        card = {"name": d["name"], "type": d["type"], "color": d.get("color") or _DEFAULT_COLOR,
                "icon_url": DOWNLOADER_ICONS.get(d["type"], ""), **view}
        if not card.get("error"):
            down += card["down_speed"] or 0
            up += card["up_speed"] or 0
        cards.append(card)
    result = {"clients": cards, "down_speed": down, "up_speed": up}
    with _DOWNLOADS_LOCK:
        _DOWNLOADS_CACHE.update({"at": time.monotonic(), "data": result})
    return result


@app.route("/api/downloads")
@login_required
def api_downloads():
    return jsonify(gather_downloads(force=request.args.get("refresh") == "1"))


# ---------------------------------------------------------------------------
# Librarr - a book / manga / audiobook fetcher, added as an instance kind
# (name, address, API key). Checked against its README and a real server
# (v1.5.0):
# - GET /health needs no sign-in: {"status": "ok", "version", "uptime_seconds",
#   "library_items", "sources_enabled", "sources": [...]}.
# - Everything else takes the API key as an X-Api-Key header (README: "every
#   endpoint accepts" it; not yet tried against a real server).
# - GET /api/downloads -> {"downloads": [{source, title, status, job_id,
#   error, max_retries}]}. Only a failed job has been seen: the other status
#   words and any progress field are not documented, so they are read
#   defensively and unknown progress is shown as unknown, never as 0%.
# - GET /api/activity -> {"events": [{id, event_type, title, detail, job_id,
#   timestamp}], total}; event_type includes download_start, torrent_import
#   and nzb_import, and also sign-in and settings events that are left out.
# - GET /api/library/manga?page=N (also /api/library for ebooks, 50 a page, and
#   /api/library/audiobooks - see LIBRARR_LIBRARY_KINDS) -> {"items": [...], "page", "pages",
#   "total"}, 100 per page for manga. One item is one FILE (a series with ten volumes is
#   ten items sharing a title): {id, title, author, file_format, file_path,
#   file_size, added_at, source, media_type}. The cards group files by title.
# - GET /api/search/manga?q= -> {"results": [...], "sources": [...],
#   "metadata", "search_time_ms"}. It asks about a dozen sources and took
#   ~30 s, which would freeze the single gunicorn worker, so the search runs
#   on its own thread and the page polls for it. A result carries source,
#   title, size_human, seeders, score, in_library and download_url /
#   info_hash...; those links can hold an indexer's own API key, so they
#   stay on the server and the page only gets a token.
# - GET /api/search (ebooks) and GET /api/search/audiobooks answer with the same
#   {"results", "sources", "metadata", "search_time_ms"}. An audiobook result was seen live:
#   {abb_url, in_library, indexer, score, score_breakdown, size_human, source: "audiobook",
#   title}. No ebook result could be seen (Anna's Archive answered 403 on the one server
#   tried, so the answer was empty); the page sends ebooks, audiobooks and manga through the
#   same POST /api/download. Each entry of "sources" has name, label, enabled and
#   health {circuit_open, search_fail_streak, ...}: that is how a search that found nothing can
#   say which sources are failing.
# - POST /api/download takes the fields Librarr's own page sends (title,
#   download_url, abb_url, source, source_id, md5, author, info_hash, magnet,
#   media_type, download_protocol, force) and answers {"success", "job_id"};
#   a book already owned answers 409 {"code": "already_in_library"} unless
#   force is true. (The README's /api/download/torrent is not what its page
#   uses.)
# - POST /api/downloads/jobs/<id>/retry retries a failed job.
# ---------------------------------------------------------------------------

LIBRARR_TIMEOUT = 8
LIBRARR_SEARCH_TIMEOUT = 90
LIBRARR_SEARCH_KEEP_SECONDS = 600
LIBRARR_SEARCH_MAX_KEPT = 20
LIBRARR_SEARCH_MAX_RUNNING = 3
LIBRARR_RESULT_LIMIT = 40
# What Librarr can search for, and where (its own page uses these three addresses). Manga is the
# default; the answers all share one shape, and every kind is sent with the same POST /api/download.
LIBRARR_SEARCH_KINDS = {"manga": "/api/search/manga", "ebook": "/api/search", "audiobook": "/api/search/audiobooks"}
LIBRARR_HISTORY_LIMIT = 25
# What Librarr's own page lists (read from its JS and the real server, 10 Nov 2026): three lists, one
# address each, all answering {"items", "page", "pages", "total", ...} with the same file record.
# The file ids come from one table (an ebook was 310, an audiobook 309, a manga 278), so a card's
# item_id is unique across kinds. The page sizes differ (manga and audiobooks 100, ebooks 50).
LIBRARR_LIBRARY_KINDS = {
    "manga": {"path": "/api/library/manga", "label": "Manga", "one": "Manga"},
    "ebook": {"path": "/api/library", "label": "Ebooks", "one": "Ebook"},
    "audiobook": {"path": "/api/library/audiobooks", "label": "Audiobooks", "one": "Audiobook"},
}
LIBRARR_LIBRARY_MAX_PAGES = 30       # pages per list (100 files a page for manga: up to 3,000 files)
LIBRARR_LIBRARY_BUDGET_SECONDS = 20  # stop asking for pages after this long, all lists together
LIBRARR_HISTORY_HIDDEN = {"login", "logout", "settings_changed"}
LIBRARR_HISTORY_LABELS = {"download_start": "Started", "torrent_import": "Imported", "nzb_import": "Imported"}
LIBRARR_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_LIBRARR_SEARCHES: dict = {}
_LIBRARR_LOCK = threading.Lock()


def librarr_headers(config: dict) -> dict:
    return {"X-Api-Key": config.get("api_key", ""), "Accept": "application/json"}


def librarr_get(config: dict, path: str, params=None, timeout: float = LIBRARR_TIMEOUT):
    response = requests.get(f"{config['url'].rstrip('/')}{path}", headers=librarr_headers(config),
                            params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def librarr_call(config: dict, path: str, params=None, timeout: float = LIBRARR_TIMEOUT):
    """librarr_get that returns (data, None) or (None, "a few plain words")."""
    try:
        return librarr_get(config, path, params, timeout), None
    except requests.exceptions.JSONDecodeError:
        return None, "unexpected answer - is this the right address?"
    except requests.exceptions.RequestException as err:
        return None, short_request_error(err)
    except ValueError:
        return None, "unexpected answer - is this the right address?"


def librarr_probe(config: dict) -> dict:
    """Is Librarr answering? /health needs no key, so a wrong key can never
    make a running server look down. One more try after a refused connection
    or a timeout before it counts."""
    url = f"{config['url'].rstrip('/')}/health"
    error = "could not connect"
    for _ in range(2):
        try:
            response = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            error = f"HTTP {response.status_code} - is this Librarr's address?"
            continue
        except requests.exceptions.Timeout:
            error = "timed out"
            continue
        except requests.exceptions.ConnectionError:
            error = "could not connect"
            continue
        except requests.exceptions.RequestException:
            error = "request failed"
            continue
        try:
            body = response.json()
        except ValueError:
            return {"error": "answered, but not like Librarr - check the address"}
        if not isinstance(body, dict):
            return {"error": "answered, but not like Librarr - check the address"}
        status = str(body.get("status", "")).strip().lower()
        if status != "ok":
            return {"error": f"Librarr reports a problem (status: {scrub_log_text(status or 'unknown', 40)})"}

        def count(key):
            value = body.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return {"error": None, "version": scrub_log_text(str(body.get("version") or ""), 30),
                "sources_enabled": count("sources_enabled"), "library_items": count("library_items")}
    return {"error": error}


def _librarr_group(status) -> str:
    """Librarr's job status word -> failed / preparing / done / queued /
    downloading. Only "error" has been seen on a real server, so the rest is
    matched by meaning; an unknown word counts as downloading (shown, not hidden)."""
    word = str(status or "").strip().lower()
    if any(w in word for w in ("error", "fail")):
        return "failed"
    if any(w in word for w in ("import", "process", "extract", "convert")) and "imported" not in word:
        return "preparing"
    if any(w in word for w in ("complete", "done", "finish", "success", "imported")):
        return "done"
    if any(w in word for w in ("queue", "pending", "wait")):
        return "queued"
    return "downloading"


def _librarr_progress(job: dict):
    """A percentage if the job carries one (0-100), else None = unknown."""
    for key in ("progress", "percent", "percentage"):
        value = job.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
            return round(value)
    return None


LIBRARR_GROUP_LABELS = {"downloading": "Downloading", "preparing": "Preparing", "queued": "Queued", "failed": "Failed"}


def librarr_queue_items(downloads) -> list:
    """Librarr's /api/downloads answer -> queue rows in the shape every
    instance's Queue tab uses. Pure; finished jobs are dropped. A failed job
    has status "failed" and a message, so Needs attention picks it up the way
    it does for Kapowarr."""
    rows = downloads.get("downloads") if isinstance(downloads, dict) else None
    items = []
    for job in rows if isinstance(rows, list) else []:
        if not isinstance(job, dict):
            continue
        group = _librarr_group(job.get("status"))
        if group == "done":
            continue
        job_id = str(job.get("job_id") or "")
        note = scrub_log_text(str(job.get("error") or ""), 120) if group == "failed" else ""
        items.append({
            "title": scrub_log_text(str(job.get("title") or "(unknown)"), 200),
            "time_left": LIBRARR_GROUP_LABELS[group], "status": group, "progress_pct": _librarr_progress(job),
            "status_messages": [note or "Download failed"] if group == "failed" else [],
            "indexer": scrub_log_text(str(job.get("source") or ""), 40), "download_client": "", "protocol": "",
            "quality": "", "custom_format_score": 0, "size_human": "",
            "job_id": job_id if LIBRARR_JOB_ID.match(job_id) else "",
        })
    items.sort(key=lambda i: ["downloading", "preparing", "queued", "failed"].index(i["status"]))
    return items


def fetch_librarr_queue(name: str, config: dict) -> dict:
    data, error = librarr_call(config, "/api/downloads")
    if error:
        return {"error": error, "queue_items": []}
    return {"error": None, "queue_items": librarr_queue_items(data)}


def librarr_history_items(activity) -> list:
    """Librarr's /api/activity answer -> history rows, newest first. Pure.
    Sign-in and settings events are left out; they are not about books."""
    events = activity.get("events") if isinstance(activity, dict) else None
    rows = [e for e in events or [] if isinstance(e, dict) and str(e.get("event_type") or "") not in LIBRARR_HISTORY_HIDDEN]
    rows.sort(key=lambda e: e.get("id") if isinstance(e.get("id"), int) else 0, reverse=True)
    items = []
    for event in rows[:LIBRARR_HISTORY_LIMIT]:
        kind = str(event.get("event_type") or "")
        when = str(event.get("timestamp") or "")
        try:
            datetime.fromisoformat(when)
        except ValueError:
            when = ""
        items.append({
            "title": scrub_log_text(str(event.get("title") or event.get("detail") or "(unknown)"), 200),
            "event": LIBRARR_HISTORY_LABELS.get(kind) or scrub_log_text(kind.replace("_", " ").capitalize() or "Event", 40),
            "date": when[:10],
        })
    return items


def fetch_librarr_history(name: str, config: dict) -> dict:
    data, error = librarr_call(config, "/api/activity", {"limit": LIBRARR_HISTORY_LIMIT * 2})
    if error:
        return {"error": error, "history_items": []}
    return {"error": None, "history_items": librarr_history_items(data)}


def fetch_librarr_health(name: str, config: dict) -> dict:
    """System tab: Librarr's own /health. A server that answers is a notice;
    no search source switched on is a warning, because every search would be
    empty."""
    probe = librarr_probe(config)
    if probe["error"]:
        return {"error": probe["error"], "health_items": []}
    parts = [f"Librarr {probe['version']} is running" if probe.get("version") else "Librarr is running"]
    if probe.get("sources_enabled") is not None:
        parts.append(f"{probe['sources_enabled']} search source{'' if probe['sources_enabled'] == 1 else 's'} on")
    items = [{"type": "notice", "source": "Librarr", "message": ", ".join(parts), "wiki_url": ""}]
    if probe.get("sources_enabled") == 0:
        items.append({"type": "warning", "source": "Librarr", "wiki_url": "",
                      "message": "No search sources are switched on, so searches will find nothing"})
    return {"error": None, "health_items": items}


def fetch_librarr_diskspace(name: str, config: dict) -> dict:
    return {"error": None, "diskspace_items": []}  # Librarr does not report free space


def _librarr_size(size) -> str:
    """A file size in plain words: KB, MB or GB, whichever reads best."""
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        return ""
    if size < 1024 ** 2:
        return f"{max(1, round(size / 1024))} KB"
    return f"{size / (1024 ** 2):.0f} MB" if size < 1024 ** 3 else format_size_gb(size)


def _librarr_file_name(path) -> str:
    return re.split(r"[\\/]", str(path or ""))[-1][:200]


def librarr_group_files(files: list, kind: str = "manga") -> list:
    """Librarr's one-item-per-file library -> one card per title. Pure. A card
    keeps the file names (never full paths) for the details window, and its
    item_id is its lowest file id, which stays put while the title is there.
    `kind` is the list the files came from (manga, ebook or audiobook): it
    becomes the card's type, and the same title in two kinds is two cards."""
    meta = LIBRARR_LIBRARY_KINDS.get(kind, LIBRARR_LIBRARY_KINDS["manga"])
    label, one = meta["label"], meta["one"]
    groups = {}
    for record in files:
        if not isinstance(record, dict) or not isinstance(record.get("id"), int) or isinstance(record.get("id"), bool):
            continue
        title = scrub_log_text(str(record.get("title") or "(untitled)"), 200)
        groups.setdefault(title.strip().casefold(), []).append((title, record))
    cards = []
    for rows in groups.values():
        rows.sort(key=lambda r: (_librarr_file_name(r[1].get("file_path")).casefold(), r[1]["id"]))
        size = sum(r["file_size"] for _, r in rows if isinstance(r.get("file_size"), int) and not isinstance(r.get("file_size"), bool))
        added = max((str(r.get("added_at") or "") for _, r in rows), default="")
        formats = sorted({str(r.get("file_format") or "").lower() for _, r in rows if r.get("file_format")})
        count = len(rows)
        cards.append({
            "title": rows[0][0], "kind": kind, "kind_label": label,
            "detail": f"{one} · {count} file{'' if count == 1 else 's'}" + (f" · {', '.join(formats)}" if formats else ""),
            "status": "Downloaded", "genre": label, "cover_url": "", "item_id": min(r["id"] for _, r in rows),
            "tmdb_id": None, "tvdb_id": None, "foreign_artist_id": None, "release_date": "", "date_added": added,
            "episode_have": None, "episode_total": None, "size_bytes": size, "file_count": count,
            "librarr_files": [{
                "name": scrub_log_text(_librarr_file_name(r.get("file_path")) or title, 200),
                "format": scrub_log_text(str(r.get("file_format") or ""), 12).lower(),
                "size_human": _librarr_size(r.get("file_size")),
                "added": str(r.get("added_at") or "")[:10], "source": scrub_log_text(str(r.get("source") or ""), 40),
            } for title, r in rows],
        })
    cards.sort(key=lambda c: c["title"].lower())
    return cards


def fetch_librarr_library(name: str, config: dict) -> dict:
    """All three of Librarr's lists (manga, ebooks, audiobooks), one after another (this already
    runs on the shared pool, and a pool task waiting on its own pool can deadlock). A list that
    cannot be read is named in `unavailable` and the others still show; only when none can be
    read is it an error."""
    empty = {"library_items": [], "status_options": [], "genre_options": []}
    cards, file_count, complete, started = [], 0, True, time.monotonic()
    unavailable, first_error, any_read = [], None, False
    for kind, meta in LIBRARR_LIBRARY_KINDS.items():
        if time.monotonic() - started > LIBRARR_LIBRARY_BUDGET_SECONDS:
            unavailable.append(meta["label"])  # out of time: not asked at all
            complete = False
            continue
        files, kind_complete, read_first = [], False, False
        for page in range(1, LIBRARR_LIBRARY_MAX_PAGES + 1):
            data, error = librarr_call(config, meta["path"], {"page": page}, timeout=15)
            rows = data.get("items") if isinstance(data, dict) else None
            if error or not isinstance(rows, list):
                if page == 1:
                    first_error = first_error or error or "unexpected answer - is this the right address?"
                break  # a later page failed: show what came back, marked as not complete
            read_first = True
            files.extend(r for r in rows if isinstance(r, dict))
            pages = data.get("pages")
            if not rows or (isinstance(pages, int) and page >= pages):
                kind_complete = True
                break
            if time.monotonic() - started > LIBRARR_LIBRARY_BUDGET_SECONDS:
                break
        if not read_first:
            unavailable.append(meta["label"])
            complete = False
            continue
        any_read = True
        complete = complete and kind_complete
        file_count += len(files)
        cards.extend(librarr_group_files(files, kind))
    if not any_read:
        return {"error": first_error or "unexpected answer - is this the right address?", **empty}
    order = list(LIBRARR_LIBRARY_KINDS)
    cards.sort(key=lambda c: (c["title"].lower(), order.index(c["kind"])))
    present = {c["kind"] for c in cards}
    return {"error": None, "library_items": cards, "status_options": ["Downloaded"] if cards else [],
            "genre_options": [m["label"] for k, m in LIBRARR_LIBRARY_KINDS.items() if k in present],
            "file_count": file_count, "complete": complete, "unavailable": unavailable}


def librarr_item_details(name: str, config: dict, item_id: int):
    """The details window for one title: its files. None if it is gone."""
    library = fetch_library_cached(name, config)
    if library.get("error"):
        return {"error": library["error"]}
    card = next((c for c in library["library_items"] if c.get("item_id") == item_id), None)
    if card is None:
        return None
    return {"title": card["title"], "kind": LIBRARR_LIBRARY_KINDS[card["kind"]]["one"], "files": card["librarr_files"],
            "file_count": card["file_count"], "total_size": _librarr_size(card["size_bytes"])}


def librarr_retry(config: dict, job_id: str) -> None:
    if not LIBRARR_JOB_ID.match(str(job_id or "")):
        raise ValueError("unknown job")
    response = requests.post(f"{config['url'].rstrip('/')}/api/downloads/jobs/{job_id}/retry",
                             headers=librarr_headers(config), timeout=15)
    response.raise_for_status()


def _librarr_instance(name: str):
    config = APPS.get(name)
    return config if config and config.get("backend") == "librarr" and config.get("enabled", True) else None


@app.route("/api/librarr/retry/<name>/<job_id>", methods=["POST"])
@login_required
@permission_required("searching")
def api_librarr_retry(name, job_id):
    config = _librarr_instance(name)
    if not config:
        return jsonify({"ok": False, "error": "unknown Librarr"}), 404
    try:
        librarr_retry(config, job_id)
    except ValueError as err:
        return jsonify({"ok": False, "error": str(err)}), 400
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": short_request_error(err)}), 502
    return jsonify({"ok": True})


def librarr_search_results(data) -> tuple:
    """Librarr's search answer -> (rows for the page, raw results by token).
    Pure. Best score first. The rows carry no links - only a token, because a
    result's download link can contain an indexer's API key."""
    results = data.get("results") if isinstance(data, dict) else None
    labels = {}
    for source in data.get("sources") or [] if isinstance(data, dict) else []:
        if isinstance(source, dict) and source.get("name"):
            labels[str(source["name"])] = str(source.get("label") or source["name"])

    def number(value):
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    raw_results = [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []
    raw_results.sort(key=lambda r: -(number(r.get("score")) or 0))
    rows, raw = [], {}
    for index, result in enumerate(raw_results[:LIBRARR_RESULT_LIMIT]):
        token = str(index)
        source = str(result.get("source") or "")
        raw[token] = result
        rows.append({
            "token": token, "title": scrub_log_text(str(result.get("title") or "(untitled)"), 200),
            "source": scrub_log_text(str(result.get("indexer") or labels.get(source) or source), 40),
            "size": "" if str(result.get("size_human") or "").strip() == "?" else scrub_log_text(str(result.get("size_human") or ""), 40),  # Librarr writes "?" for an unknown size
            "seeders": number(result.get("seeders")), "score": number(result.get("score")),
            "format": scrub_log_text(str(result.get("format") or ""), 20),
            "in_library": bool(result.get("in_library")),
            "library_title": scrub_log_text(str(result.get("library_title") or ""), 200),
        })
    return rows, raw


def librarr_failing_sources(data) -> list:
    """The labels of the sources Librarr says are failing right now (its circuit is open, or the
    last searches failed in a row) - so an empty answer can say why. Pure; words only, never the
    error text Librarr keeps (it can quote an address)."""
    failing = []
    for source in (data.get("sources") if isinstance(data, dict) else None) or []:
        if not isinstance(source, dict) or source.get("enabled") is False:
            continue
        health = source.get("health") if isinstance(source.get("health"), dict) else {}
        streak = health.get("search_fail_streak")
        if health.get("circuit_open") is True or (isinstance(streak, int) and not isinstance(streak, bool) and streak >= 3):
            label = scrub_log_text(str(source.get("label") or source.get("name") or ""), 40)
            if label and label not in failing:
                failing.append(label)
    return failing[:10]


def librarr_grab_body(result: dict, force: bool) -> dict:
    """The request body Librarr's own page sends for a result."""
    def text(key):
        return str(result.get(key) or "")

    return {
        "title": text("title"), "download_url": text("download_url") or text("url") or text("epub_url"),
        "abb_url": text("abb_url"), "source": text("source"), "source_id": text("source_id"), "md5": text("md5"),
        "author": text("author"), "info_hash": text("info_hash"), "magnet": text("magnet"),
        "media_type": text("media_type"), "download_protocol": text("download_protocol"), "force": bool(force),
    }


def _librarr_prune(now: float) -> None:
    """Forget old searches (caller holds the lock)."""
    for sid in [s for s, e in _LIBRARR_SEARCHES.items() if now - e["started"] > LIBRARR_SEARCH_KEEP_SECONDS and e["state"] != "running"]:
        del _LIBRARR_SEARCHES[sid]
    while len(_LIBRARR_SEARCHES) > LIBRARR_SEARCH_MAX_KEPT:
        oldest = min((s for s, e in _LIBRARR_SEARCHES.items() if e["state"] != "running"),
                     key=lambda s: _LIBRARR_SEARCHES[s]["started"], default=None)
        if oldest is None:
            break
        del _LIBRARR_SEARCHES[oldest]


def _run_librarr_search(sid: str, config: dict, query: str, kind: str = "manga") -> None:
    error, rows, raw, took, failing = None, [], {}, 0, []
    try:
        data = librarr_get(config, LIBRARR_SEARCH_KINDS[kind], {"q": query}, timeout=LIBRARR_SEARCH_TIMEOUT)
        rows, raw = librarr_search_results(data)
        failing = librarr_failing_sources(data)
        took = data.get("search_time_ms") if isinstance(data, dict) and isinstance(data.get("search_time_ms"), (int, float)) else 0
    except requests.exceptions.JSONDecodeError:
        error = "unexpected answer - is this the right address?"
    except requests.exceptions.RequestException as err:
        error = short_request_error(err)
    except (ValueError, AttributeError, TypeError):
        error = "unexpected answer - is this the right address?"
    with _LIBRARR_LOCK:
        entry = _LIBRARR_SEARCHES.get(sid)
        if entry:
            entry.update({"state": "error" if error else "done", "error": error, "rows": rows, "raw": raw, "took_ms": int(took),
                          "failing": failing})


@app.route("/api/librarr/search/<name>", methods=["POST"])
@login_required
@permission_required("searching")
def api_librarr_search_start(name):
    config = _librarr_instance(name)
    if not config:
        return jsonify({"error": "unknown Librarr"}), 404
    body = request.get_json(silent=True) or {}
    query = " ".join(str(body.get("q") or "").split())
    if len(query) < 2 or len(query) > 100:
        return jsonify({"error": "type 2 to 100 characters to search for"}), 400
    kind = body.get("kind", "manga")
    if kind not in LIBRARR_SEARCH_KINDS:
        return jsonify({"error": "choose manga, ebooks or audiobooks"}), 400
    owner = session.get("username")
    now = time.monotonic()
    with _LIBRARR_LOCK:
        _librarr_prune(now)
        for sid, entry in _LIBRARR_SEARCHES.items():  # the same search already running: share it
            if entry["state"] == "running" and entry["client"] == name and entry["owner"] == owner and entry["query"].lower() == query.lower() \
                    and entry["kind"] == kind:
                return jsonify({"id": sid})
        if sum(1 for e in _LIBRARR_SEARCHES.values() if e["state"] == "running") >= LIBRARR_SEARCH_MAX_RUNNING:
            return jsonify({"error": "several searches are already running - try again in a moment"}), 429
        sid = secrets.token_urlsafe(9)
        _LIBRARR_SEARCHES[sid] = {"client": name, "owner": owner, "query": query, "state": "running", "started": now,
                                  "error": None, "rows": [], "raw": {}, "took_ms": 0, "kind": kind, "failing": []}
    threading.Thread(target=_run_librarr_search, args=(sid, dict(config), query, kind), name="arrv-librarr-search", daemon=True).start()
    return jsonify({"id": sid})


def _librarr_search_entry(name: str, sid: str):
    entry = _LIBRARR_SEARCHES.get(sid)
    return entry if entry and entry["client"] == name and entry["owner"] == session.get("username") else None


@app.route("/api/librarr/search/<name>/<sid>")
@login_required
@permission_required("searching")
def api_librarr_search_result(name, sid):
    with _LIBRARR_LOCK:
        entry = _librarr_search_entry(name, sid)
        if not entry:
            return jsonify({"error": "that search has expired - search again"}), 404
        return jsonify({"state": entry["state"], "error": entry["error"], "query": entry["query"],
                        "results": entry["rows"], "took_ms": entry["took_ms"], "kind": entry["kind"],
                        "failing": entry["failing"]})


@app.route("/api/librarr/grab/<name>/<sid>/<token>", methods=["POST"])
@login_required
@permission_required("searching")
def api_librarr_grab(name, sid, token):
    config = _librarr_instance(name)
    if not config:
        return jsonify({"ok": False, "error": "unknown Librarr"}), 404
    with _LIBRARR_LOCK:
        entry = _librarr_search_entry(name, sid)
        result = entry["raw"].get(token) if entry else None
    if result is None:
        return jsonify({"ok": False, "error": "that result has expired - search again"}), 404
    force = bool((request.get_json(silent=True) or {}).get("force"))
    try:
        response = requests.post(f"{config['url'].rstrip('/')}/api/download", headers=librarr_headers(config),
                                 json=librarr_grab_body(result, force), timeout=20)
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        if response.status_code == 409 or answer.get("code") == "already_in_library":
            return jsonify({"ok": False, "already_in_library": True,
                            "library_title": scrub_log_text(str(answer.get("library_title") or result.get("title") or ""), 200)})
        if not response.ok or answer.get("success") is False:
            reason = scrub_log_text(str(answer.get("error") or ""), 160) or short_request_error(
                requests.exceptions.HTTPError(response=response))
            return jsonify({"ok": False, "error": reason}), 502
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": short_request_error(err)}), 502
    with _ATTENTION_LOCK:
        _ATTENTION_CACHE["data"] = None  # a new job can change Needs attention on its next look
    return jsonify({"ok": True, "job_id": scrub_log_text(str(answer.get("job_id") or ""), 80)})


# ---------------------------------------------------------------------------
# Indexers (Prowlarr) - separate from the arr instances entirely, same
# reasoning as Downloaders: Prowlarr is usually one shared service, not
# tied to any single Radarr/Sonarr/Lidarr instance.
# ---------------------------------------------------------------------------


def fetch_prowlarr_indexers(service: dict) -> dict:
    """
    Confirmed via Prowlarr's real OpenAPI spec: GET /api/v1/indexer
    for the base indexer list, cross-referenced against GET
    /api/v1/indexerstatus for health - Prowlarr tracks failing
    indexers separately, only listing ones currently having issues
    there, so absence from that list means healthy.
    """
    base = service["url"].rstrip("/")
    headers = {"X-Api-Key": service["api_key"]}

    try:
        indexers_resp = requests.get(f"{base}/api/v1/indexer", headers=headers, timeout=15)
        indexers_resp.raise_for_status()
        status_resp = requests.get(f"{base}/api/v1/indexerstatus", headers=headers, timeout=15)
        status_resp.raise_for_status()
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "indexers": []}

    # Map indexerId -> full status entry (not just membership), so we
    # can surface *why* something is unhealthy, not just that it is.
    status_by_id = {s.get("indexerId"): s for s in status_resp.json()}

    indexers = []
    for i in indexers_resp.json():
        status = status_by_id.get(i.get("id"))
        reason = ""
        if status:
            # Confirmed via Prowlarr's real OpenAPI spec: IndexerStatus
            # only has disabledTill/mostRecentFailure/initialFailure as
            # timestamps - no free-text message field exists at all,
            # so this is the most specific info actually available.
            if status.get("disabledTill"):
                reason = f"Disabled until {status['disabledTill']}"
            elif status.get("mostRecentFailure"):
                reason = f"Most recent failure: {status['mostRecentFailure']}"
            else:
                reason = "Recent failures detected"
        indexers.append({
            "id": i.get("id"),
            "name": i.get("name", "(unknown)"),
            "enabled": i.get("enable", True),
            "protocol": i.get("protocol", ""),
            "healthy": status is None,
            "unhealthy_reason": reason,
        })
    indexers.sort(key=lambda i: i["name"].lower())
    return {"error": None, "indexers": indexers}


def fetch_prowlarr_health(service: dict) -> dict:
    """
    Prowlarr is itself part of the Servarr family (same underlying
    framework as Radarr/Sonarr), so it exposes the same GET /api/v1/health
    endpoint with an identical HealthResource shape - confirmed by
    reading Prowlarr's own HealthController/HealthResource source. This
    is a *different* signal than fetch_prowlarr_indexers' per-indexer
    failure tracking above: that's "is this specific indexer failing",
    this is "does Prowlarr itself have a problem" (e.g. an unsynced
    application connection, a stale update, a missing download client).
    """
    base = service["url"].rstrip("/")
    headers = {"X-Api-Key": service["api_key"]}

    try:
        response = requests.get(f"{base}/api/v1/health", headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({service['url']})", "health_items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "health_items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "health_items": []}

    records = response.json() or []

    health_items = [
        {
            "type": (record.get("type") or "notice").lower(),
            "source": record.get("source", ""),
            "message": record.get("message", ""),
            "wiki_url": record.get("wikiUrl", ""),
        }
        for record in records
    ]
    return {"error": None, "health_items": health_items}


def test_prowlarr_indexer(service: dict, indexer_id: int) -> str:
    """
    Confirmed via Prowlarr's real OpenAPI spec: testing an indexer
    means GETting its full current definition, then POSTing that
    whole object to a plain /test endpoint (no ID in the URL) -
    matching the arr-family convention of testing by full-body POST
    rather than a /test/{id} sub-resource. An earlier version of this
    guessed the /test/{id} shape and got a 405, since Prowlarr doesn't
    recognize that pattern at all.

    Returns an empty string on success, or a best-effort error detail
    on failure.
    """
    base = service["url"].rstrip("/")
    headers = {"X-Api-Key": service["api_key"]}

    try:
        get_resp = requests.get(f"{base}/api/v1/indexer/{indexer_id}", headers=headers, timeout=15)
        get_resp.raise_for_status()
        indexer_config = get_resp.json()
    except requests.exceptions.RequestException as err:
        return str(err)

    response = requests.post(f"{base}/api/v1/indexer/test", headers=headers, json=indexer_config, timeout=30)

    if response.ok:
        return ""

    try:
        body = response.json()
        if isinstance(body, list) and body:
            # Sonarr/Radarr/Prowlarr-family validation failures are
            # typically a list of {"propertyName", "errorMessage"}.
            return "; ".join(item.get("errorMessage", str(item)) for item in body)
        if isinstance(body, dict) and body.get("message"):
            return body["message"]
        return str(body)[:300]
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"


def test_all_prowlarr_indexers(service: dict) -> str:
    """
    Prowlarr has a native bulk-test endpoint (confirmed via its real
    OpenAPI spec) - much simpler and more efficient than looping
    individual tests, since it runs server-side in one request.
    """
    base = service["url"].rstrip("/")
    headers = {"X-Api-Key": service["api_key"]}
    try:
        response = requests.post(f"{base}/api/v1/indexer/testall", headers=headers, timeout=60)
    except requests.exceptions.RequestException as err:
        return str(err)

    if response.ok:
        return ""

    try:
        body = response.json()
        if isinstance(body, list) and body:
            return "; ".join(item.get("errorMessage", str(item)) for item in body)
        return str(body)[:300]
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"


def get_indexer_service(name: str):
    return next((s for s in INDEXERS if s["name"] == name), None)


@app.route("/api/indexers/status/<name>")
@login_required
def api_indexers_status(name):
    service = get_indexer_service(name)
    if not service:
        return jsonify({"error": "unknown indexer service"}), 404
    result = fetch_prowlarr_indexers(service)
    health = fetch_prowlarr_health(service)
    result["health_items"] = health["health_items"]
    result["health_error"] = health["error"]
    return jsonify(result)


@app.route("/api/indexers/test/<name>/<int:indexer_id>", methods=["POST"])
@login_required
@permission_required("searching")
def api_indexers_test(name, indexer_id):
    service = get_indexer_service(name)
    if not service:
        return jsonify({"ok": False, "error": "unknown indexer service"}), 404
    try:
        error_detail = test_prowlarr_indexer(service, indexer_id)
        if error_detail:
            return jsonify({"ok": False, "error": error_detail})
        return jsonify({"ok": True})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502


@app.route("/api/indexers/test-all/<name>", methods=["POST"])
@login_required
@permission_required("searching")
def api_indexers_test_all(name):
    service = get_indexer_service(name)
    if not service:
        return jsonify({"ok": False, "error": "unknown indexer service"}), 404
    try:
        error_detail = test_all_prowlarr_indexers(service)
        if error_detail:
            return jsonify({"ok": False, "error": error_detail})
        return jsonify({"ok": True})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502


# ---------------------------------------------------------------------------
# Media Server activity views (Now Playing / Recently Added / Watch
# History / Users) - separate from the arr instances entirely, same
# reasoning as Downloaders and Indexers.
#
# Plex and Jellyfin/Emby have meaningfully different APIs, so each tab
# has one function per dialect (Jellyfin and Emby share the same one).
# Two honest limitations, flagged up front:
# - Plex "Users" only shows locally-managed accounts via this local
#   server API - shared/"friend" users live on plex.tv's cloud API,
#   a different credential surface than the server token stored here,
#   so this will likely undercount who actually has access.
# - Jellyfin/Emby have no clean built-in watch-history endpoint the
#   way Plex does - this approximates it via the admin Activity Log,
#   filtered to playback-looking entries, and is less complete.
# ---------------------------------------------------------------------------


def get_media_server_open_url(server: dict) -> str:
    """
    The stored "url" field is the API base, used for every API call
    this app makes - but Plex's bare API root serves raw XML by
    default rather than redirecting to its own web UI (confirmed
    earlier via find_plex_link, which already needed to target
    /web/index.html specifically for deep-links). Jellyfin/Emby don't
    have this problem - their servers redirect the bare root to their
    web UI correctly on their own, so only Plex needs a different path
    here.
    """
    base = server["url"].rstrip("/")
    if server["type"] == "plex":
        return f"{base}/web/index.html"
    return base


def get_media_server(name: str):
    return next((s for s in MEDIA_SERVERS if s["name"] == name), None)


# ---------------------------------------------------------------------------
# BookOrbit - a self-hosted ebook / comic / audiobook library
# (github.com/bookorbit/bookorbit). What is verified, and what is not:
# - GET /api/v1/health answers {"status": "ok"} and needs no sign-in (from the
#   project's own documentation). The up/down check (Needs attention, History)
#   uses only that, so a missing or wrong API key can never make a running
#   BookOrbit look down.
# - API keys arrived in BookOrbit 3.8.0 (docs/API_KEYS.md in its repository): a key
#   starts with `bok_`, is 53 characters, is sent as `Authorization: Bearer <key>`
#   or `X-Api-Key: <key>` on any /api/v1 request (never in the query string), acts as
#   its owner narrowed (Read is the default; a Read key may use GET routes), and
#   is refused with 403 on key management, passwords and sessions. 401 = a wrong,
#   expired or revoked key; more than 30 wrong keys a minute from one address
#   = 429. GET /api/v1/auth/me reports `authenticationMethod: "api_key"`.
# - Read from the person's real BookOrbit (a signed-in browser session, read-only
#   GETs, 12 Nov 2026) and from its type definitions:
#   GET /api/v1/libraries -> a list of {id, name, bookCount, folders (server paths:
#   never passed on), ...many settings};
#   GET /api/v1/libraries/<id>/stats -> {totalBooks, totalSizeBytes,
#   formatCounts: {"epub": 478, "m4b": 25, ...}};
#   GET /api/v1/dashboard/scrollers/recently-added?limit=N -> {"books": [{id,
#   libraryId, title, authors: [..], seriesName, seriesIndex, files: [{id, format,
#   role, sizeBytes}], addedAt, ...}], "total"} (a GET, so a Read key may use it; the
#   POST routes are mostly refused for a Read key); GET /api/v1/books/<id>/thumbnail
#   is the cover and needs the key, so arrVantage proxies it.
#   Not verified: that those GETs accept a *key* (they were read with a login
#   session; the Test button is how that gets confirmed), the other scroller
#   types, and any route to open one book in BookOrbit's own page (so there is
#   no per-book link, only Open BookOrbit).
# ---------------------------------------------------------------------------

BOOKORBIT_HEALTH_PATH = "/api/v1/health"


def fetch_bookorbit_health(server: dict) -> dict:
    """Is BookOrbit answering? One more try after a refused connection or a
    timeout before it counts, like the other media server probes. Failures are
    described in a few plain words (never the address or the response body)."""
    url = f"{server['url'].rstrip('/')}{BOOKORBIT_HEALTH_PATH}"
    error = "could not connect"
    for _ in range(2):
        try:
            response = requests.get(url, headers={"Accept": "application/json"}, timeout=10)
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            error = f"HTTP {response.status_code} - is this BookOrbit's address?"
            continue
        except requests.exceptions.Timeout:
            error = "timed out"
            continue
        except requests.exceptions.ConnectionError:
            error = "could not connect"
            continue
        except requests.exceptions.RequestException:
            error = "request failed"
            continue
        try:
            body = response.json()
        except ValueError:
            return {"error": "answered, but not like BookOrbit - check the address", "status": None}
        status = str(body.get("status", "")).strip().lower() if isinstance(body, dict) else ""
        if status == "ok":
            return {"error": None, "status": "ok"}
        return {"error": f"BookOrbit reports a problem (status: {scrub_log_text(status or 'unknown', 40)})",
                "status": status or None}
    return {"error": error, "status": None}


BOOKORBIT_KEY_PREFIX = "bok_"
BOOKORBIT_MAX_LIBRARIES = 30
BOOKORBIT_RECENT_LIMIT = 24
BOOKORBIT_MAX_FORMATS = 12
BOOKORBIT_COVER_MAX_BYTES = 2 * 1024 * 1024


def bookorbit_headers(server: dict) -> dict:
    # The key goes in a header, never the address (BookOrbit ignores a key in the query string).
    return {"X-Api-Key": server.get("credential", ""), "Accept": "application/json"}


def _bookorbit_words(err) -> str:
    """A failed call to BookOrbit in a few plain words: no address, no body."""
    code = err.response.status_code if isinstance(err, requests.exceptions.HTTPError) and err.response is not None else None
    if code == 401:
        return "BookOrbit refused the API key - check it was copied whole and has not expired or been revoked"
    if code == 403:
        return "BookOrbit does not let this key read that - make the key with Read access"
    if code == 404:
        return "not found - check the address, and that BookOrbit is version 3.8 or newer"
    if code == 429:
        return "BookOrbit is rate limiting this address (too many wrong keys) - wait a minute"
    return short_request_error(err)


def bookorbit_call(server: dict, path: str, params=None, timeout: float = 10):
    """GET <server>/api/v1/... with the key. Returns (data, None) or (None, "a few plain words")."""
    try:
        response = requests.get(f"{server['url'].rstrip('/')}{path}", headers=bookorbit_headers(server),
                                params=params, timeout=timeout)
        response.raise_for_status()
        return response.json(), None
    except requests.exceptions.JSONDecodeError:
        return None, "answered, but not like BookOrbit - check the address"
    except requests.exceptions.RequestException as err:
        return None, _bookorbit_words(err)


def _bookorbit_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def bookorbit_libraries_view(libraries, stats_by_id: dict) -> list:
    """BookOrbit's library list plus each library's stats -> one row per library. Pure.
    Only name, counts, size and formats are kept: a library's `folders` are server paths."""
    rows = []
    for lib in libraries if isinstance(libraries, list) else []:
        if not isinstance(lib, dict) or _bookorbit_int(lib.get("id")) is None:
            continue
        stats = stats_by_id.get(lib["id"])
        stats = stats if isinstance(stats, dict) else {}
        books = _bookorbit_int(stats.get("totalBooks"))
        if books is None:
            books = _bookorbit_int(lib.get("bookCount"))
        size = _bookorbit_int(stats.get("totalSizeBytes"))
        counts = stats.get("formatCounts") if isinstance(stats.get("formatCounts"), dict) else {}
        formats = sorted(((scrub_log_text(str(k), 12).lower(), v) for k, v in counts.items()
                          if _bookorbit_int(v) and str(k).strip()), key=lambda f: (-f[1], f[0]))
        rows.append({
            "id": lib["id"], "name": scrub_log_text(str(lib.get("name") or "(unnamed)"), 100),
            "books": books, "size_bytes": size, "size_display": format_size_gb(size) if size is not None else "",
            "formats": [{"format": f, "count": c} for f, c in formats[:BOOKORBIT_MAX_FORMATS]],
            "details": bool(stats),
        })
    return rows


def fetch_bookorbit_libraries(server: dict) -> dict:
    if not server.get("credential"):
        return {"error": None, "needs_key": True, "libraries": []}
    data, error = bookorbit_call(server, "/api/v1/libraries")
    if error:
        return {"error": error, "libraries": []}
    if not isinstance(data, list):
        return {"error": "answered, but not like BookOrbit's library list - check the address", "libraries": []}
    stats = {}
    for lib in data[:BOOKORBIT_MAX_LIBRARIES]:
        lib_id = lib.get("id") if isinstance(lib, dict) else None
        if _bookorbit_int(lib_id) is None:
            continue
        found, err = bookorbit_call(server, f"/api/v1/libraries/{lib_id}/stats")
        if not err and isinstance(found, dict):
            stats[lib_id] = found
    rows = bookorbit_libraries_view(data[:BOOKORBIT_MAX_LIBRARIES], stats)
    sized = [r["size_bytes"] for r in rows if r["size_bytes"] is not None]
    return {"error": None, "needs_key": False, "libraries": rows,
            "total_books": sum(r["books"] or 0 for r in rows),
            "total_size_display": format_size_gb(sum(sized)) if sized else "",
            "cut_short": len(data) > BOOKORBIT_MAX_LIBRARIES}


def bookorbit_recent_view(data, server_name: str, library_names: dict) -> list:
    """The recently-added scroller's answer -> cards. Pure. No file path and no link: a cover is
    arrVantage's own proxy address (the real one needs the key)."""
    books = data.get("books") if isinstance(data, dict) else None
    cards = []
    for book in books if isinstance(books, list) else []:
        if not isinstance(book, dict) or _bookorbit_int(book.get("id")) is None:
            continue
        authors = [scrub_log_text(str(a), 80) for a in book.get("authors") or [] if isinstance(a, str) and a.strip()]
        series = scrub_log_text(str(book.get("seriesName") or ""), 100)
        index = book.get("seriesIndex")
        if series and isinstance(index, (int, float)) and not isinstance(index, bool):
            series += f" #{int(index) if float(index).is_integer() else index}"
        formats = []
        for f in book.get("files") or []:
            fmt = scrub_log_text(str(f.get("format") or ""), 12).lower() if isinstance(f, dict) else ""
            if fmt and fmt not in formats:
                formats.append(fmt)
        cards.append({
            "id": book["id"], "title": scrub_log_text(str(book.get("title") or "(untitled)"), 200),
            "authors": ", ".join(authors[:3]), "series": series, "formats": formats[:4],
            "library": library_names.get(book.get("libraryId"), ""),
            "added": str(book.get("addedAt") or "")[:10],
            "cover_url": f"/api/bookorbit/cover/{server_name}/{book['id']}" if book.get("hasCover", True) else "",
        })
    return cards


def fetch_bookorbit_recent(server: dict, server_name: str) -> dict:
    if not server.get("credential"):
        return {"error": None, "needs_key": True, "items": []}
    data, error = bookorbit_call(server, "/api/v1/dashboard/scrollers/recently-added", {"limit": BOOKORBIT_RECENT_LIMIT})
    if error:
        return {"error": error, "items": []}
    if not isinstance(data, dict) or not isinstance(data.get("books"), list):
        return {"error": "answered, but not like BookOrbit's recently added list - check the address", "items": []}
    names = {}
    libs, _ = bookorbit_call(server, "/api/v1/libraries")  # only for the names; a failure just leaves them out
    for lib in libs if isinstance(libs, list) else []:
        if isinstance(lib, dict) and _bookorbit_int(lib.get("id")) is not None:
            names[lib["id"]] = scrub_log_text(str(lib.get("name") or ""), 100)
    return {"error": None, "needs_key": False, "items": bookorbit_recent_view(data, server_name, names)}


# ---------------------------------------------------------------------------
# Komga - a comics/manga/book server. Unlike Plex/Jellyfin, there's no
# "now playing" concept (no live streaming sessions to poll), so this
# gets its own tabs entirely (Library, On Deck, Recently Added) rather
# than plugging into the now-playing/recently-added/watch-history/users
# tab set built for the other media server types. Confirmed against
# Komga's own published OpenAPI spec (komga.org/docs/openapi, and the
# raw spec at github.com/gotson/komga/blob/master/komga/docs/openapi.json)
# - auth is a real X-API-Key header, same convention as everything else
# in this app, and every list endpoint returns a standard Spring Page
# object ({"content": [...], "totalPages": ..., ...}).
#
# Cover art needs a server-side proxy (same reasoning as the Tracearr
# poster-proxy above) since Komga's thumbnail endpoints require the
# X-API-Key header, which an <img> tag can't send.
#
# "Open in Komga" links use Komga's web UI's documented route pattern
# (/series/{id}, /book/{id}/read) - this matches Komga's own Vue Router
# structure as commonly referenced in its community guides, but wasn't
# independently confirmed against the webui's router source itself. If
# a link 404s inside Komga's own UI, that's the thing to double-check.
# ---------------------------------------------------------------------------


def komga_headers(server: dict) -> dict:
    return {"X-API-Key": server["credential"]}


def fetch_komga_libraries(server: dict) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/api/v1/libraries",
            headers=komga_headers(server), timeout=10,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({server['url']})", "libraries": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "libraries": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "libraries": []}

    libraries = [{"id": lib.get("id"), "name": lib.get("name", "(unnamed)")} for lib in response.json() or []]
    return {"error": None, "libraries": libraries}


def get_komga_series_summary(record: dict, server_name: str) -> dict:
    metadata = record.get("metadata") or {}
    return {
        "id": record.get("id"),
        "title": metadata.get("title") or record.get("name", "(unknown)"),
        "publisher": metadata.get("publisher", ""),
        "status": (metadata.get("status") or "").title(),
        "books_count": record.get("booksCount", 0),
        "books_read_count": record.get("booksReadCount", 0),
        "books_unread_count": record.get("booksUnreadCount", 0),
        "books_in_progress_count": record.get("booksInProgressCount", 0),
        "cover_url": f"/api/komga/cover/{server_name}/series/{record.get('id')}",
    }


def fetch_komga_series(server: dict, server_name: str, library_id: str = None,
                        search: str = None, page: int = 0, size: int = 24) -> dict:
    params = {"page": page, "size": size, "sort": "metadata.titleSort,asc"}
    if library_id:
        params["library_id"] = library_id
    if search:
        params["search"] = search

    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/api/v1/series",
            headers=komga_headers(server), params=params, timeout=15,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({server['url']})", "items": [], "page": 0, "total_pages": 0}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "items": [], "page": 0, "total_pages": 0}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "items": [], "page": 0, "total_pages": 0}

    data = response.json()
    items = [get_komga_series_summary(r, server_name) for r in data.get("content", [])]
    return {
        "error": None, "items": items,
        "page": data.get("number", 0), "total_pages": data.get("totalPages", 0),
        "total_elements": data.get("totalElements", 0),
    }


def fetch_komga_series_detail(server: dict, server_name: str, series_id: str) -> dict:
    try:
        series_resp = requests.get(
            f"{server['url'].rstrip('/')}/api/v1/series/{series_id}",
            headers=komga_headers(server), timeout=15,
        )
        series_resp.raise_for_status()
        books_resp = requests.get(
            f"{server['url'].rstrip('/')}/api/v1/series/{series_id}/books",
            headers=komga_headers(server),
            params={"size": 500, "sort": "metadata.numberSort,asc"}, timeout=15,
        )
        books_resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({server['url']})", "series": None, "books": []}
    except requests.exceptions.HTTPError:
        return {"error": "HTTP error - check API key", "series": None, "books": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "series": None, "books": []}

    record = series_resp.json()
    metadata = record.get("metadata") or {}
    series = {
        "id": record.get("id"),
        "title": metadata.get("title") or record.get("name", "(unknown)"),
        "summary": metadata.get("summary", ""),
        "publisher": metadata.get("publisher", ""),
        "status": (metadata.get("status") or "").title(),
        "genres": metadata.get("genres") or [],
        "books_count": record.get("booksCount", 0),
        "books_read_count": record.get("booksReadCount", 0),
        "cover_url": f"/api/komga/cover/{server_name}/series/{series_id}",
        "open_url": f"{server['url'].rstrip('/')}/series/{series_id}",
    }

    books = []
    for b in books_resp.json().get("content", []):
        b_metadata = b.get("metadata") or {}
        progress = b.get("readProgress") or {}
        media = b.get("media") or {}
        books.append({
            "id": b.get("id"),
            "number": b_metadata.get("number") or b.get("number", ""),
            "title": b_metadata.get("title") or b.get("name", "(unknown)"),
            "read": bool(progress.get("completed")),
            "in_progress": bool(progress) and not progress.get("completed"),
            "page": progress.get("page"),
            "pages_total": media.get("pagesCount"),
            "size": b.get("size", ""),
            "cover_url": f"/api/komga/cover/{server_name}/book/{b.get('id')}",
            "open_url": f"{server['url'].rstrip('/')}/book/{b.get('id')}/read",
        })
    return {"error": None, "series": series, "books": books}


def fetch_komga_ondeck(server: dict, server_name: str, page: int = 0, size: int = 20) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/api/v1/books/ondeck",
            headers=komga_headers(server), params={"page": page, "size": size}, timeout=15,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({server['url']})", "items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "items": []}

    items = []
    for b in response.json().get("content", []):
        b_metadata = b.get("metadata") or {}
        progress = b.get("readProgress") or {}
        media = b.get("media") or {}
        items.append({
            "id": b.get("id"),
            "series_id": b.get("seriesId"),
            "series_title": b.get("seriesTitle", ""),
            "title": b_metadata.get("title") or b.get("name", "(unknown)"),
            "number": b_metadata.get("number") or b.get("number", ""),
            "page": progress.get("page"),
            "pages_total": media.get("pagesCount"),
            "cover_url": f"/api/komga/cover/{server_name}/book/{b.get('id')}",
            "open_url": f"{server['url'].rstrip('/')}/book/{b.get('id')}/read",
        })
    return {"error": None, "items": items}


def fetch_komga_latest(server: dict, server_name: str, page: int = 0, size: int = 20) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/api/v1/series/latest",
            headers=komga_headers(server), params={"page": page, "size": size}, timeout=15,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        return {"error": f"could not connect ({server['url']})", "items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "items": []}
    except requests.exceptions.RequestException as err:
        return {"error": f"request failed: {err}", "items": []}

    items = [get_komga_series_summary(r, server_name) for r in response.json().get("content", [])]
    return {"error": None, "items": items}


def fetch_plex_now_playing(server: dict) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/status/sessions",
            headers={"Accept": "application/json"},
            params={"X-Plex-Token": server["credential"]}, timeout=10,
        )
        response.raise_for_status()
        sessions = response.json().get("MediaContainer", {}).get("Metadata", []) or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for s in sessions:
        duration = s.get("duration") or 0
        offset = s.get("viewOffset") or 0
        title = s.get("title", "(unknown)")
        if s.get("grandparentTitle"):
            title = f"{s['grandparentTitle']} - {title}"
        thumb = s.get("thumb") or s.get("grandparentThumb") or ""
        media = (s.get("Media") or [{}])[0]

        # A TranscodeSession is only present at all when Plex is doing
        # some form of transcoding - its absence *is* Direct Play, so
        # this isn't guessable from a single field the way Jellyfin's
        # PlayMethod enum is. videoDecision "copy" means the video
        # stream itself is passed through untouched (Direct Stream) -
        # only "transcode" is genuine CPU/GPU work.
        transcode = s.get("TranscodeSession")
        if transcode:
            play_method = "Transcoding" if transcode.get("videoDecision") == "transcode" else "Direct Stream"
            bitrate_kbps = transcode.get("bitrate")
        else:
            play_method = "Direct Play"
            bitrate_kbps = media.get("bitrate")

        items.append({
            "title": title,
            "poster_url": f"{server['url'].rstrip('/')}{thumb}?X-Plex-Token={server['credential']}" if thumb else "",
            "user": (s.get("User") or {}).get("title", "Unknown"),
            "device": (s.get("Player") or {}).get("title", ""),
            "progress_pct": round((offset / duration) * 100, 1) if duration else 0,
            "state": (s.get("Player") or {}).get("state", ""),
            "play_method": play_method,
            "bitrate_kbps": bitrate_kbps or 0,
            "video_resolution": media.get("videoResolution", ""),
        })
    return {"error": None, "items": items}


def fetch_jellyfin_now_playing(server: dict) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/Sessions",
            headers=jellyfin_family_headers(server.get("type"), server["credential"]), timeout=10,
        )
        response.raise_for_status()
        sessions = response.json() or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for s in sessions:
        now_playing = s.get("NowPlayingItem")
        if not now_playing:
            continue
        play_state = s.get("PlayState") or {}
        position = play_state.get("PositionTicks") or 0
        runtime = now_playing.get("RunTimeTicks") or 0
        title = now_playing.get("Name", "(unknown)")
        if now_playing.get("SeriesName"):
            title = f"{now_playing['SeriesName']} - {title}"
        item_id = now_playing.get("Id")
        # PlayMethod ("DirectPlay"/"DirectStream"/"Transcode") and the
        # sibling TranscodingInfo object are confirmed via Jellyfin's
        # own PlayerStateInfo/TranscodingInfo source models.
        transcoding_info = s.get("TranscodingInfo") or {}
        play_method = play_state.get("PlayMethod", "DirectPlay")
        items.append({
            "title": title,
            "poster_url": f"{server['url'].rstrip('/')}/Items/{item_id}/Images/Primary?{urlencode(jellyfin_family_token_params(server.get('type'), server['credential']))}" if item_id else "",
            "user": s.get("UserName", "Unknown"),
            "device": s.get("DeviceName", ""),
            "progress_pct": round((position / runtime) * 100, 1) if runtime else 0,
            "state": "paused" if play_state.get("IsPaused") else "playing",
            "play_method": "Transcoding" if play_method == "Transcode" else play_method,
            "bitrate_kbps": round((transcoding_info.get("Bitrate") or 0) / 1000),
            "video_resolution": f"{transcoding_info['Width']}x{transcoding_info['Height']}" if transcoding_info.get("Width") else "",
        })
    return {"error": None, "items": items}


def fetch_plex_recently_added(server: dict) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/library/recentlyAdded",
            headers={"Accept": "application/json"},
            params={"X-Plex-Token": server["credential"]}, timeout=10,
        )
        response.raise_for_status()
        entries = response.json().get("MediaContainer", {}).get("Metadata", []) or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for e in entries[:30]:
        title = e.get("title", "(unknown)")
        if e.get("grandparentTitle"):
            title = f"{e['grandparentTitle']} - {title}"
        thumb = e.get("thumb") or e.get("grandparentThumb") or ""
        items.append({
            "title": title,
            "type": e.get("type", ""),
            "poster_url": f"{server['url'].rstrip('/')}{thumb}?X-Plex-Token={server['credential']}" if thumb else "",
        })
    return {"error": None, "items": items}


def fetch_jellyfin_recently_added(server: dict) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/Items",
            headers=jellyfin_family_headers(server.get("type"), server["credential"]),
            params={
                "SortBy": "DateCreated", "SortOrder": "Descending", "Recursive": "true", "Limit": 30,
                "IncludeItemTypes": "Movie,Series,Episode,Audio,MusicAlbum",
            }, timeout=10,
        )
        response.raise_for_status()
        entries = response.json().get("Items", []) or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for e in entries:
        title = e.get("Name", "(unknown)")
        if e.get("SeriesName"):
            title = f"{e['SeriesName']} - {title}"
        item_id = e.get("Id")
        items.append({
            "title": title,
            "type": e.get("Type", ""),
            "poster_url": f"{server['url'].rstrip('/')}/Items/{item_id}/Images/Primary?{urlencode(jellyfin_family_token_params(server.get('type'), server['credential']))}" if item_id else "",
        })
    return {"error": None, "items": items}


def fetch_plex_watch_history(server: dict) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/status/sessions/history/all",
            headers={"Accept": "application/json"},
            params={"X-Plex-Token": server["credential"], "sort": "viewedAt:desc"}, timeout=10,
        )
        response.raise_for_status()
        entries = response.json().get("MediaContainer", {}).get("Metadata", []) or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for e in entries[:30]:
        title = e.get("title", "(unknown)")
        if e.get("grandparentTitle"):
            title = f"{e['grandparentTitle']} - {title}"
        user = e.get("User")
        thumb = e.get("thumb") or e.get("grandparentThumb") or ""
        items.append({
            "title": title,
            "user": user.get("title", "") if isinstance(user, dict) else "",
            "poster_url": f"{server['url'].rstrip('/')}{thumb}?X-Plex-Token={server['credential']}" if thumb else "",
        })
    return {"error": None, "items": items}


def fetch_jellyfin_watch_history(server: dict) -> dict:
    """Approximated via the admin Activity Log - see module note above."""
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/System/ActivityLog/Entries",
            headers=jellyfin_family_headers(server.get("type"), server["credential"]),
            params={"Limit": 50}, timeout=10,
        )
        response.raise_for_status()
        entries = response.json().get("Items", []) or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = [
        {"title": e.get("Name", "(unknown)"), "user": e.get("UserId", ""), "poster_url": ""}
        for e in entries
        if "play" in (e.get("Type") or "").lower() or "play" in (e.get("Name") or "").lower()
    ]
    return {"error": None, "items": items[:30]}


def fetch_plex_users(server: dict) -> dict:
    """Local-server accounts only - see module note above about shared users."""
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/accounts",
            headers={"Accept": "application/json"},
            params={"X-Plex-Token": server["credential"]}, timeout=10,
        )
        response.raise_for_status()
        accounts = response.json().get("MediaContainer", {}).get("Account", []) or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = [{"name": a.get("name") or a.get("title", "(unknown)")} for a in accounts]
    return {"error": None, "items": items}


def fetch_jellyfin_users(server: dict) -> dict:
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}/Users",
            headers=jellyfin_family_headers(server.get("type"), server["credential"]), timeout=10,
        )
        response.raise_for_status()
        users = response.json() or []
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = [{"name": u.get("Name", "(unknown)")} for u in users]
    return {"error": None, "items": items}


def tautulli_configured() -> bool:
    return bool(TAUTULLI.get("url") and TAUTULLI.get("api_key"))


def tautulli_request(cmd: str, params: dict = None) -> dict:
    params = dict(params or {})
    params["apikey"] = TAUTULLI["api_key"]
    params["cmd"] = cmd
    response = requests.get(f"{TAUTULLI['url'].rstrip('/')}/api/v2", params=params, timeout=10)
    response.raise_for_status()
    return response.json().get("response", {}).get("data", {}) or {}


TAUTULLI_TRANSCODE_DECISION = {
    "direct play": "Direct Play",
    "copy": "Direct Stream",
    "transcode": "Transcoding",
}


def fetch_tautulli_now_playing() -> dict:
    """
    Same item shape as fetch_plex_now_playing, so the frontend needs no
    changes at all - only where the data comes from. Best-guess field
    names throughout for the base fields (friendly_name/player/etc,
    long since proven correct in production) - check the raw JSON here
    first if something looks off. The transcode-related fields below
    (transcode_decision, quality_profile, bandwidth) are confirmed
    against Tautulli's own published API reference, not guessed.
    Poster path uses Tautulli's own image proxy convention.
    """
    try:
        data = tautulli_request("get_activity")
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for s in (data.get("sessions") or []):
        title = s.get("title", "(unknown)")
        if s.get("grandparent_title"):
            title = f"{s['grandparent_title']} - {title}"
        thumb = s.get("thumb") or s.get("grandparent_thumb") or ""
        decision = (s.get("transcode_decision") or "").lower()
        items.append({
            "title": title,
            "poster_url": f"{TAUTULLI['url'].rstrip('/')}/pms_image_proxy?img={thumb}&apikey={TAUTULLI['api_key']}" if thumb else "",
            "user": s.get("friendly_name", "Unknown"),
            "device": s.get("player", ""),
            "progress_pct": round(float(s.get("progress_percent") or 0), 1),
            "state": s.get("state", ""),
            "play_method": TAUTULLI_TRANSCODE_DECISION.get(decision, decision.title() or "Unknown"),
            "bitrate_kbps": int(s.get("bandwidth") or 0),
            "video_resolution": s.get("stream_video_resolution") or s.get("video_resolution", ""),
        })
    return {"error": None, "items": items}


def fetch_tautulli_watch_history() -> dict:
    """Pulls from Tautulli's own persistent history database, rather
    than Plex's own limited session-history endpoint."""
    try:
        data = tautulli_request("get_history", {"length": 30})
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for e in (data.get("data") or []):
        title = e.get("title", "(unknown)")
        if e.get("grandparent_title"):
            title = f"{e['grandparent_title']} - {title}"
        thumb = e.get("thumb") or e.get("grandparent_thumb") or ""
        items.append({
            "title": title,
            "user": e.get("friendly_name", ""),
            "poster_url": f"{TAUTULLI['url'].rstrip('/')}/pms_image_proxy?img={thumb}&apikey={TAUTULLI['api_key']}" if thumb else "",
        })
    return {"error": None, "items": items}


def fetch_tautulli_users() -> dict:
    """
    Tracks users from observed playback activity server-side, so this
    should include shared/'friend' users too - unlike the direct Plex
    /accounts endpoint, which only sees locally-managed accounts.
    """
    try:
        data = tautulli_request("get_users_table", {"length": 100})
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = [{"name": u.get("friendly_name", "(unknown)")} for u in (data.get("data") or [])]
    return {"error": None, "items": items}


def tracearr_configured() -> bool:
    return bool(TRACEARR.get("url") and TRACEARR.get("api_key"))


def tracearr_request(path: str, params: dict = None) -> dict:
    headers = {"Authorization": f"Bearer {TRACEARR['api_key']}"}
    response = requests.get(
        f"{TRACEARR['url'].rstrip('/')}/api/v2/public{path}",
        headers=headers, params=params or {}, timeout=10,
    )
    response.raise_for_status()
    return response.json()


@app.route("/api/mediaserver/poster-proxy")
@login_required
def api_mediaserver_poster_proxy():
    """
    Tracearr's poster_url is documented as a 'proxied' URL, which
    likely means it requires the same Bearer auth as the rest of its
    API - <img> tags can't send custom headers, so this fetches the
    image server-side (where we can attach the token) and streams the
    bytes back instead.
    """
    target_url = request.args.get("url", "")
    if not target_url or not tracearr_configured():
        return "", 404

    # poster_url might be a relative path rather than a full URL -
    # resolve it against the configured Tracearr base if so.
    if not target_url.startswith("http://") and not target_url.startswith("https://"):
        target_url = f"{TRACEARR['url'].rstrip('/')}/{target_url.lstrip('/')}"

    if not target_url.startswith(TRACEARR["url"].rstrip("/")):
        return "", 403  # only ever proxy our own configured Tracearr instance

    try:
        response = requests.get(
            target_url, headers={"Authorization": f"Bearer {TRACEARR['api_key']}"}, timeout=10,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return "", 502

    return Response(response.content, mimetype=response.headers.get("Content-Type", "image/jpeg"))


def fetch_tracearr_now_playing(server_name: str) -> dict:
    """
    Tracearr covers Plex, Jellyfin, and Emby from one unified API, so
    (unlike Tautulli) this applies to every media server type. Filtered
    to the specific server clicked in the sidebar by matching
    server_name, since Tracearr's own data spans all your servers at
    once. Field names here are confirmed against a real response.
    """
    try:
        data = tracearr_request("/streams")
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for s in data.get("data") or []:
        if (s.get("server_name") or "").lower() != server_name.lower():
            continue
        title = s.get("media_title", "(unknown)")
        if s.get("show_title"):
            title = f"{s['show_title']} - {title}"
        duration = s.get("duration_ms") or 0
        progress = s.get("progress_ms") or 0
        wrapped_poster = f"/api/mediaserver/poster-proxy?url={quote(s['poster_url'], safe='')}" if s.get("poster_url") else ""
        items.append({
            "title": title,
            "poster_url": wrapped_poster,
            "user": s.get("username") or "Unknown",
            "device": s.get("device") or s.get("player", ""),
            "progress_pct": round((progress / duration) * 100, 1) if duration else 0,
            "state": s.get("state", ""),
            "media_type": s.get("media_type", ""),
            "tmdb_id": s.get("tmdb_id"),
            "tvdb_id": s.get("tvdb_id"),
            "imdb_id": s.get("imdb_id"),
        })
    return {"error": None, "items": items}


def fetch_tracearr_watch_history(server_name: str) -> dict:
    try:
        data = tracearr_request("/history", {"pageSize": 30})
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for e in data.get("data") or []:
        if (e.get("server_name") or "").lower() != server_name.lower():
            continue
        title = e.get("media_title", "(unknown)")
        if e.get("show_title"):
            title = f"{e['show_title']} - {title}"
        user = e.get("user") or {}
        items.append({
            "title": title,
            "user": user.get("username") or "",
            "poster_url": f"/api/mediaserver/poster-proxy?url={quote(e['poster_url'], safe='')}" if e.get("poster_url") else "",
            "media_type": e.get("media_type", ""),
            "tmdb_id": e.get("tmdb_id"),
            "tvdb_id": e.get("tvdb_id"),
            "imdb_id": e.get("imdb_id"),
        })
    return {"error": None, "items": items}


def fetch_tracearr_users(server_type: str) -> dict:
    """
    Filtered by matching server_type (plex/jellyfin/emby) on each
    identity's accounts, since Tracearr's schema has no per-server-name
    field to filter by the way streams/history do. If you run more
    than one server of the same type, this can't fully separate them -
    only the platform type is distinguishable this way.
    """
    try:
        data = tracearr_request("/users", {"pageSize": 100})
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    items = []
    for u in (data.get("data") or []):
        matching_account = next(
            (a for a in (u.get("accounts") or []) if a.get("server_type") == server_type), None,
        )
        if not matching_account:
            continue
        items.append({"name": matching_account.get("username") or u.get("username", "(unknown)")})
    return {"error": None, "items": items}


def resolve_tracearr_show_media(episode_entry: dict) -> dict:
    """
    Episodes carry no show name directly - resolves the show's own
    record via Tracearr's /media/{ref} lookup: episode's own tvdb_id ->
    its canonical media record (which has show_media_id) -> that
    show's own record (which has the title and the show's own
    tvdb_id). Returns {} on any failure, so the caller can fall back
    gracefully rather than show a broken group.
    """
    tvdb_id = episode_entry.get("tvdb_id")
    if not tvdb_id:
        return {}
    try:
        episode_media = tracearr_request(f"/media/episode:tvdb:{tvdb_id}")
        show_media_id = episode_media.get("show_media_id")
        if not show_media_id:
            return {}
        return tracearr_request(f"/media/{show_media_id}")
    except requests.exceptions.RequestException:
        return {}


def fetch_tvdb_series_poster(tvdb_id) -> str:
    """
    Reuses the same TVDB login/integration built for Sonarr cast
    lookups. Best-guess field name ("image") for the series' main
    artwork on the extended series record - check the raw JSON here
    if posters don't show up.
    """
    if not tvdb_id or not TVDB.get("api_key"):
        return ""
    token = tvdb_login()
    if not token:
        return ""
    try:
        response = requests.get(
            f"https://api4.thetvdb.com/v4/series/{tvdb_id}/extended",
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
        )
        response.raise_for_status()
        return response.json().get("data", {}).get("image") or ""
    except requests.exceptions.RequestException:
        return ""


def fetch_tracearr_recently_added() -> dict:
    """
    Not filtered per-server either - a title's availability can span
    multiple servers at once in Tracearr's unified library view.

    Tracearr's Recently Added schema has no poster_url field at all
    (confirmed from its OpenAPI spec - only Streams and History carry
    one). Reuses this app's existing TMDB integration to fetch a real
    poster for movies specifically, using tmdb_id. Scoped to movies
    only - an episode's tmdb_id refers to the episode itself, not the
    show, so fetching a "show" poster from it would return the wrong
    (or no) image.

    Episodes are grouped by show (via grandparent_rating_key, shared
    by every episode of the same show) rather than listed individually,
    with the show's own name and poster resolved through a couple of
    extra Tracearr + TVDB lookups per unique show - see
    resolve_tracearr_show_media and fetch_tvdb_series_poster.
    """
    try:
        data = tracearr_request("/recently-added", {"pageSize": 30})
    except requests.exceptions.RequestException as err:
        return {"error": str(err), "items": []}

    entries = data.get("data") or []

    # A file that gets replaced (e.g. by Tdarr transcoding it after the
    # initial import) can show up as a second, separate "recently
    # added" event for what's really the same piece of content -
    # deduplicate by the canonical media_id (stable across a transcode,
    # unlike the server-specific rating_key/id) so it's only shown
    # once, keeping whichever entry was added most recently.
    deduped_by_media = {}
    for e in entries:
        key = e.get("media_id") or e.get("id")
        existing = deduped_by_media.get(key)
        if not existing or (e.get("added_at") or "") > (existing.get("added_at") or ""):
            deduped_by_media[key] = e
    entries = list(deduped_by_media.values())

    show_groups = {}
    standalone = []
    for e in entries:
        gpk = e.get("grandparent_rating_key")
        if e.get("media_type") == "episode" and gpk:
            show_groups.setdefault((e.get("server_id"), gpk), []).append(e)
        else:
            standalone.append(e)

    items = []
    for e in standalone:
        poster_url = ""
        if e.get("media_type") == "movie" and e.get("tmdb_id") and TMDB.get("api_key"):
            try:
                movie = tmdb_get(f"/movie/{e['tmdb_id']}")
                if movie.get("poster_path"):
                    poster_url = f"https://image.tmdb.org/t/p/w200{movie['poster_path']}"
            except requests.exceptions.RequestException:
                pass
        items.append({
            "title": e.get("title", "(unknown)"),
            "type": e.get("media_type", ""),
            "media_type": e.get("media_type", ""),
            "tmdb_id": e.get("tmdb_id"),
            "tvdb_id": e.get("tvdb_id"),
            "imdb_id": e.get("imdb_id"),
            "poster_url": poster_url,
        })

    for episodes in show_groups.values():
        show_media = resolve_tracearr_show_media(episodes[0])
        show_title = show_media.get("title") or episodes[0].get("title", "(unknown show)")
        count = len(episodes)
        items.append({
            "title": show_title,
            "type": f"{count} new episode{'s' if count != 1 else ''}",
            "media_type": "show",
            "tmdb_id": None,
            "tvdb_id": None,
            "imdb_id": None,
            "poster_url": fetch_tvdb_series_poster(show_media.get("tvdb_id")),
        })
    return {"error": None, "items": items}


@app.route("/api/mediaserver/find-item")
@login_required
def api_mediaserver_find_item():
    """
    Cross-references an external ID (from Tracearr's data, which is
    the only media-server source with these IDs wired up so far)
    against your configured Radarr/Sonarr libraries, so a media-server
    activity card can open the real detail modal. Only works for
    titles that are actually in one of your own arr instances - a
    title only in Plex/Jellyfin and never added to Radarr/Sonarr won't
    resolve, which is expected, not a bug.
    """
    tmdb_id = request.args.get("tmdb_id", type=int)
    tvdb_id = request.args.get("tvdb_id", type=int)
    media_type = request.args.get("media_type", "")

    target_kind = "series" if media_type in ("episode", "show", "season") else "movie"
    if not ((target_kind == "movie" and tmdb_id) or (target_kind == "series" and tvdb_id)):
        return jsonify({"found": False})

    for name, config in APPS.items():
        if config["library_kind"] != target_kind:
            continue
        try:
            library = fetch_library(name, config)
        except requests.exceptions.RequestException:
            continue
        for item in library["library_items"]:
            if target_kind == "movie" and item.get("tmdb_id") == tmdb_id:
                return jsonify({"found": True, "instance": name, "item_id": item["item_id"]})
            if target_kind == "series" and item.get("tvdb_id") == tvdb_id:
                return jsonify({"found": True, "instance": name, "item_id": item["item_id"]})

    return jsonify({"found": False})


def _dispatch_media_server_fetch(name, plex_fn, jellyfin_fn):
    server = get_media_server(name)
    if not server:
        return jsonify({"error": "unknown media server"}), 404
    if server["type"] not in ("plex", "jellyfin", "emby"):
        return jsonify({"error": "not available for this kind of server"})
    result = plex_fn(server) if server["type"] == "plex" else jellyfin_fn(server)
    return jsonify(result)


@app.route("/api/bookorbit/health/<name>")
@login_required
def api_bookorbit_health(name):
    server = get_media_server(name)
    if not server or server["type"] != "bookorbit":
        return jsonify({"error": "unknown BookOrbit server"}), 404
    return jsonify({**fetch_bookorbit_health(server), "open_url": get_media_server_open_url(server),
                    "has_key": bool(server.get("credential"))})


def _bookorbit_server(name: str):
    server = get_media_server(name)
    return server if server and server["type"] == "bookorbit" else None


@app.route("/api/bookorbit/libraries/<name>")
@login_required
def api_bookorbit_libraries(name):
    server = _bookorbit_server(name)
    if not server:
        return jsonify({"error": "unknown BookOrbit server"}), 404
    return jsonify(fetch_bookorbit_libraries(server))


@app.route("/api/bookorbit/recent/<name>")
@login_required
def api_bookorbit_recent(name):
    server = _bookorbit_server(name)
    if not server:
        return jsonify({"error": "unknown BookOrbit server"}), 404
    return jsonify(fetch_bookorbit_recent(server, name))


@app.route("/api/bookorbit/cover/<name>/<int:book_id>")
@login_required
def api_bookorbit_cover(name, book_id):
    server = _bookorbit_server(name)
    if not server or not server.get("credential"):
        return "", 404
    try:
        response = requests.get(f"{server['url'].rstrip('/')}/api/v1/books/{book_id}/thumbnail",
                                headers=bookorbit_headers(server), timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return "", 502
    kind = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if not kind.startswith("image/") or kind == "image/svg+xml" or len(response.content) > BOOKORBIT_COVER_MAX_BYTES:
        return "", 502
    reply = Response(response.content, mimetype=kind)
    reply.headers["Cache-Control"] = "private, max-age=3600"
    return reply


@app.route("/api/mediaserver/now-playing/<name>")
@login_required
def api_mediaserver_now_playing(name):
    server = get_media_server(name)
    if not server:
        return jsonify({"error": "unknown media server"}), 404
    if tracearr_configured():
        return jsonify(fetch_tracearr_now_playing(name))
    if server["type"] == "plex" and tautulli_configured():
        return jsonify(fetch_tautulli_now_playing())
    return _dispatch_media_server_fetch(name, fetch_plex_now_playing, fetch_jellyfin_now_playing)


@app.route("/api/mediaserver/recently-added/<name>")
@login_required
def api_mediaserver_recently_added(name):
    server = get_media_server(name)
    if not server:
        return jsonify({"error": "unknown media server"}), 404
    if tracearr_configured():
        return jsonify(fetch_tracearr_recently_added())
    return _dispatch_media_server_fetch(name, fetch_plex_recently_added, fetch_jellyfin_recently_added)


@app.route("/api/mediaserver/watch-history/<name>")
@login_required
def api_mediaserver_watch_history(name):
    server = get_media_server(name)
    if not server:
        return jsonify({"error": "unknown media server"}), 404
    if tracearr_configured():
        return jsonify(fetch_tracearr_watch_history(name))
    if server["type"] == "plex" and tautulli_configured():
        return jsonify(fetch_tautulli_watch_history())
    return _dispatch_media_server_fetch(name, fetch_plex_watch_history, fetch_jellyfin_watch_history)


@app.route("/api/mediaserver/users/<name>")
@login_required
def api_mediaserver_users(name):
    server = get_media_server(name)
    if not server:
        return jsonify({"error": "unknown media server"}), 404
    if tracearr_configured():
        return jsonify(fetch_tracearr_users(server["type"]))
    if server["type"] == "plex" and tautulli_configured():
        return jsonify(fetch_tautulli_users())
    return _dispatch_media_server_fetch(name, fetch_plex_users, fetch_jellyfin_users)


def get_komga_server(name: str):
    server = get_media_server(name)
    return server if server and server["type"] == "komga" else None


@app.route("/api/komga/libraries/<name>")
@login_required
def api_komga_libraries(name):
    server = get_komga_server(name)
    if not server:
        return jsonify({"error": "unknown Komga server"}), 404
    return jsonify(fetch_komga_libraries(server))


@app.route("/api/komga/series/<name>")
@login_required
def api_komga_series(name):
    server = get_komga_server(name)
    if not server:
        return jsonify({"error": "unknown Komga server"}), 404
    return jsonify(fetch_komga_series(
        server, name,
        library_id=request.args.get("library_id") or None,
        search=request.args.get("search") or None,
        page=request.args.get("page", 0, type=int),
    ))


@app.route("/api/komga/series/<name>/<series_id>")
@login_required
def api_komga_series_detail(name, series_id):
    server = get_komga_server(name)
    if not server:
        return jsonify({"error": "unknown Komga server"}), 404
    return jsonify(fetch_komga_series_detail(server, name, series_id))


@app.route("/api/komga/ondeck/<name>")
@login_required
def api_komga_ondeck(name):
    server = get_komga_server(name)
    if not server:
        return jsonify({"error": "unknown Komga server"}), 404
    return jsonify(fetch_komga_ondeck(server, name, page=request.args.get("page", 0, type=int)))


@app.route("/api/komga/latest/<name>")
@login_required
def api_komga_latest(name):
    server = get_komga_server(name)
    if not server:
        return jsonify({"error": "unknown Komga server"}), 404
    return jsonify(fetch_komga_latest(server, name, page=request.args.get("page", 0, type=int)))


@app.route("/api/komga/cover/<name>/<kind>/<item_id>")
@login_required
def api_komga_cover(name, kind, item_id):
    server = get_komga_server(name)
    if not server or kind not in ("series", "book"):
        return "", 404

    path = f"/api/v1/series/{item_id}/thumbnail" if kind == "series" else f"/api/v1/books/{item_id}/thumbnail"
    try:
        response = requests.get(
            f"{server['url'].rstrip('/')}{path}",
            headers=komga_headers(server), timeout=10,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return "", 502

    return Response(response.content, mimetype=response.headers.get("Content-Type", "image/jpeg"))


# ---------------------------------------------------------------------------
# Tdarr - transcode automation stats. Separate from the arr instances
# entirely, same reasoning as Downloaders/Indexers. Field names here
# are confirmed against real responses from a live Tdarr instance.
# ---------------------------------------------------------------------------


def get_transcoder(name: str):
    return next((t for t in TRANSCODERS if t["name"] == name), None)


def fetch_tdarr_status(server: dict) -> dict:
    headers = {"x-api-key": server["api_key"]} if server.get("api_key") else {}
    base = server["url"].rstrip("/")

    try:
        status_resp = requests.get(f"{base}/api/v2/status", headers=headers, timeout=10)
        status_resp.raise_for_status()
        status = status_resp.json()
    except requests.exceptions.RequestException as err:
        return {"error": str(err)}

    try:
        pies_resp = requests.post(
            f"{base}/api/v2/stats/get-pies", headers=headers,
            json={"data": {"libraryId": ""}}, timeout=15,
        )
        pies_resp.raise_for_status()
        pie_stats = pies_resp.json().get("pieStats", {})
    except requests.exceptions.RequestException:
        pie_stats = {}

    try:
        nodes_resp = requests.get(f"{base}/api/v2/get-nodes", headers=headers, timeout=10)
        nodes_resp.raise_for_status()
        nodes_raw = nodes_resp.json()
    except requests.exceptions.RequestException:
        nodes_raw = {}

    transcode_status = {s["name"]: s["value"] for s in (pie_stats.get("status", {}).get("transcode") or [])}
    healthcheck_status = {s["name"]: s["value"] for s in (pie_stats.get("status", {}).get("healthcheck") or [])}

    nodes = []
    for node_id, node in (nodes_raw or {}).items():
        os_stats = (node.get("resStats") or {}).get("os") or {}
        nodes.append({
            "name": node.get("nodeName", node_id),
            "cpu_percent": os_stats.get("cpuPerc", ""),
            "mem_used_gb": os_stats.get("memUsedGB", ""),
            "mem_total_gb": os_stats.get("memTotalGB", ""),
            "worker_limits": node.get("workerLimits", {}),
            "queue_lengths": node.get("queueLengths", {}),
            "active_workers": len(node.get("workers") or {}),
            "paused": bool(node.get("nodePaused", False)),
        })

    return {
        "error": None,
        "version": status.get("version", ""),
        "uptime_seconds": status.get("uptime", 0),
        "total_files": pie_stats.get("totalFiles", 0),
        "total_transcodes": pie_stats.get("totalTranscodeCount", 0),
        "total_health_checks": pie_stats.get("totalHealthCheckCount", 0),
        "space_saved_gb": round(pie_stats.get("sizeDiff", 0) or 0, 1),
        "transcode_success": transcode_status.get("Transcode success", 0),
        "transcode_error": transcode_status.get("Transcode error", 0),
        "transcode_not_required": transcode_status.get("Not required", 0),
        "healthcheck_success": healthcheck_status.get("Success", 0),
        "healthcheck_error": healthcheck_status.get("Error", 0),
        "nodes": nodes,
    }


@app.route("/api/tdarr/status/<name>")
@login_required
def api_tdarr_status(name):
    server = get_transcoder(name)
    if not server:
        return jsonify({"error": "unknown transcoder"}), 404
    return jsonify(fetch_tdarr_status(server))


# ---------------------------------------------------------------------------
# Home - a summary landing page pulling from every configured category.
# Reuses each category's existing fetch function rather than duplicating
# API logic, at the cost of a few extra live calls on every Home load -
# the same on-demand tradeoff already used throughout this app.
# ---------------------------------------------------------------------------


def format_size_gb(size_bytes: int) -> str:
    gb = (size_bytes or 0) / (1024 ** 3)
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb:.1f} GB"


def fetch_home_stats() -> dict:
    # Every external call here (one library per instance, Prowlarr, Tdarr)
    # is independent of the others, so they all run at once. One slow or
    # unreachable service now costs its own timeout, not everyone's.
    jobs = {}
    for name, config in APPS.items():
        jobs[("library", name)] = lambda name=name, config=config: fetch_library_cached(name, config)
    for service in INDEXERS:
        if service.get("enabled", True):
            jobs[("indexer", service["name"])] = lambda service=service: fetch_prowlarr_indexers(service)
    for server in TRANSCODERS:
        if server.get("enabled", True):
            jobs[("transcoder", server["name"])] = lambda server=server: fetch_tdarr_status(server)
    results = run_concurrently(jobs)

    instance_stats = []
    for name, config in APPS.items():
        ok, library = results[("library", name)]
        if not ok:
            continue  # a failed request just leaves this instance out of Home
        if library.get("error"):
            instance_stats.append({"name": name, "kind": config["library_kind"], "error": library["error"]})
            continue
        items = library["library_items"]
        size_bytes = sum(i.get("size_bytes") or 0 for i in items)

        if config["library_kind"] == "series":
            # Episode-level counts rather than show-level - a show
            # marked "Partial" hides how much is actually missing, so
            # this sums the real episode have/total across every show.
            have = sum(i.get("episode_have") or 0 for i in items)
            total = sum(i.get("episode_total") or 0 for i in items)
            instance_stats.append({
                "name": name,
                "kind": config["library_kind"],
                "icon_url": INSTANCE_ICONS.get(config["library_kind"], ""),
                "total": total,
                "downloaded": have,
                "missing": total - have,
                "unit": "episodes",
                "size_display": format_size_gb(size_bytes),
                "error": None,
            })
        elif config["library_kind"] == "librarr":
            # Librarr lists files, not titles, and only what it imported itself,
            # so the card says titles and files and has no "missing". The size
            # is left out when the list was cut short: a partial total would
            # read as the real one.
            files = library.get("file_count") or 0
            complete = library.get("complete", True)
            instance_stats.append({
                "name": name,
                "kind": "librarr",
                "icon_url": INSTANCE_ICONS.get("librarr", ""),
                "total": len(items),
                "downloaded": len(items),
                "missing": 0,
                "unit": "titles",
                "detail_text": f"{files:,} file{'' if files == 1 else 's'}" + (
                    "" if complete else f" ({', '.join(library['unavailable']).lower()} not read)" if library.get("unavailable") else " (list cut short)"),
                "size_display": format_size_gb(size_bytes) if complete else "",
                "error": None,
            })
        else:
            # "Complete"/"Downloaded" count as downloaded; everything else
            # (Missing, Partial, Unknown) counts as missing - Partial items
            # are genuinely short something, so lumping them in here keeps
            # the two numbers adding up to the total rather than leaving a
            # third, unexplained bucket.
            downloaded = sum(1 for i in items if i["status"] in ("Downloaded", "Complete"))
            instance_stats.append({
                "name": name,
                "kind": config["library_kind"],
                "icon_url": INSTANCE_ICONS.get(config["library_kind"], ""),
                "total": len(items),
                "downloaded": downloaded,
                "missing": len(items) - downloaded,
                "unit": "items",
                "size_display": format_size_gb(size_bytes),
                "error": None,
            })

    media_server_stats = [
        {
            "name": s["name"], "type": s["type"], "url": s["url"],
            "open_url": get_media_server_open_url(s),
            "icon_url": MEDIA_SERVER_ICONS.get(s["type"], ""),
        }
        for s in MEDIA_SERVERS if s.get("enabled", True)
    ]

    indexer_service_stats = []
    for service in INDEXERS:
        if not service.get("enabled", True):
            continue
        ok, result = results[("indexer", service["name"])]
        if not ok:
            result = {"error": result}
        if result.get("error"):
            indexer_service_stats.append({
                "name": service["name"], "type": service["type"], "error": result["error"],
                "icon_url": INDEXER_ICONS.get(service["type"], ""),
            })
            continue
        healthy = sum(1 for idx in result["indexers"] if idx["healthy"])
        # Only indexers that are switched on count as failing - a disabled
        # one is a choice, not a fault.
        unhealthy_names = [
            idx["name"] for idx in result["indexers"]
            if idx["enabled"] and not idx["healthy"]
        ]
        indexer_service_stats.append({
            "unhealthy_names": unhealthy_names,
            "name": service["name"],
            "type": INDEXER_SERVICE_TYPES.get(service["type"], service["type"]),
            "icon_url": INDEXER_ICONS.get(service["type"], ""),
            "total": len(result["indexers"]),
            "healthy": healthy,
            "unhealthy": len(result["indexers"]) - healthy,
            "error": None,
        })

    transcoder_stats = []
    for server in TRANSCODERS:
        if not server.get("enabled", True):
            continue
        ok, status = results[("transcoder", server["name"])]
        if not ok or status.get("error"):
            continue
        transcoder_stats.append({
            "name": server["name"],
            "icon_url": TRANSCODER_ICONS.get(server["type"], ""),
            "total_transcodes": status["total_transcodes"],
            "space_saved_gb": status["space_saved_gb"],
        })

    return {
        "instances": instance_stats,
        "media_servers": media_server_stats,
        "indexers": indexer_service_stats,
        "transcoders": transcoder_stats,
    }


# ---------------------------------------------------------------------------
# Concurrent fetching, short-lived caches, and "needs attention"
#
# The dashboard used to fetch every instance's queue, history, library,
# health, disk space and requests - one call after another - before it
# would send a single byte, and then fetch every library a second time
# for Home. These helpers are what replaced that: independent calls run
# at once, the heavy per-instance data loads only when an instance is
# opened, and Home is driven by a small summary instead.
# ---------------------------------------------------------------------------

DISK_LOW_PCT = 15       # under this much free space: "Low"
DISK_CRITICAL_PCT = 7   # under this much free space: "Critically low"

FETCH_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="arrv-fetch")
FETCH_TIMEOUT_SECONDS = 25  # overall ceiling for one batch of parallel calls


def run_concurrently(jobs: dict, timeout: float = FETCH_TIMEOUT_SECONDS) -> dict:
    """
    Runs {key: zero-arg callable} at once on the shared pool and returns
    {key: (ok, value)}. ok is False when the call raised or didn't finish
    in time, and value is then a short error string - so one bad service
    can never take the whole batch (or the page) down with it.
    """
    futures = {key: FETCH_POOL.submit(fn) for key, fn in jobs.items()}
    deadline = time.monotonic() + timeout
    results = {}
    for key, future in futures.items():
        try:
            results[key] = (True, future.result(timeout=max(0.0, deadline - time.monotonic())))
        except concurrent.futures.TimeoutError:
            future.cancel()
            log_problem(("job", str(key), "timeout"), "Background call %s timed out after %ss", key, timeout)
            results[key] = (False, "timed out")
        except Exception as err:  # noqa: BLE001 - a failed call must not escape
            log_problem(("job", str(key), err.__class__.__name__), "Background call %s failed: %s: %s",
                        key, err.__class__.__name__, scrub_log_text(err))
            results[key] = (False, str(err) or err.__class__.__name__)
    return results


# A library can be large (Sonarr's is thousands of shows), and Home only
# needs its totals, so a library fetched in the last minute is reused
# rather than downloaded again. Errors are never cached.
LIBRARY_CACHE_SECONDS = 60
_LIBRARY_CACHE: dict = {}
_LIBRARY_CACHE_LOCK = threading.Lock()


def _library_cache_key(name: str, config: dict) -> tuple:
    return (name, config.get("url"), config.get("library_kind"))


def store_library_in_cache(name: str, config: dict, library: dict) -> None:
    if library.get("error"):
        return
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE[_library_cache_key(name, config)] = (time.monotonic(), library)


def fetch_library_cached(name: str, config: dict, max_age: float = LIBRARY_CACHE_SECONDS) -> dict:
    key = _library_cache_key(name, config)
    with _LIBRARY_CACHE_LOCK:
        entry = _LIBRARY_CACHE.get(key)
    if entry and time.monotonic() - entry[0] <= max_age:
        return entry[1]
    library = fetch_library(name, config)
    store_library_in_cache(name, config, library)
    return library


def instance_seerr_type(config: dict, perms: dict):
    """Which Overseerr/Jellyseerr media type this instance can request,
    or None when there's no active requester or this account can't request."""
    return config["seerr_media_type"] if (get_active_requester() and perms["requesting"]) else None


def build_instance_view(name: str, config: dict, perms: dict) -> dict:
    """
    Everything the dashboard shows for one instance (library, queue,
    history, requests, system). The six underlying calls are independent
    so they run together. This is what the lazy-loaded instance panels
    are rendered from.
    """
    active_requester = get_active_requester()
    seerr_type = instance_seerr_type(config, perms)

    results = run_concurrently({
        "queue": lambda: fetch_queue(name, config),
        "history": lambda: fetch_history(name, config),
        "library": lambda: fetch_library(name, config),
        "health": lambda: fetch_health(name, config),
        "diskspace": lambda: fetch_diskspace(name, config),
        "seerr": lambda: fetch_seerr_requests(active_requester, seerr_type),
    })

    def pick(key, empty):
        ok, value = results[key]
        return value if ok else {"error": value, **empty}

    queue = pick("queue", {"queue_items": []})
    history = pick("history", {"history_items": []})
    library = pick("library", {"library_items": [], "status_options": [], "genre_options": []})
    health = pick("health", {"health_items": []})
    diskspace = pick("diskspace", {"diskspace_items": []})
    seerr = pick("seerr", {"request_items": []})

    store_library_in_cache(name, config, library)

    # Only "warning" and "error" are actual problems worth badging -
    # "notice" is informational (e.g. "you're on a pre-release branch").
    health_issue_count = sum(
        1 for item in health["health_items"] if item["type"] in ("warning", "error")
    )

    collections = []
    if config["library_kind"] == "movie":
        owned_tmdb_lookup = {
            item["tmdb_id"]: item["item_id"]
            for item in library["library_items"] if item.get("tmdb_id")
        }
        try:
            collections = fetch_collections(config, owned_tmdb_lookup)
        except Exception:  # noqa: BLE001 - collections are a nicety, never fatal
            collections = []

    return {
        "name": name,
        "logo_url": INSTANCE_ICONS.get(config["library_kind"], ""),
        "color": config.get("color", "#5b8cff"),
        "library_kind": config["library_kind"],
        "url": config["url"],
        "seerr_media_type": seerr_type,
        "error": queue["error"],
        "queue_items": queue["queue_items"],
        "history_items": history["history_items"],
        "history_error": history["error"],
        "library_items": library["library_items"],
        "library_error": library["error"],
        "library_file_count": library.get("file_count"),
        "library_complete": library.get("complete", True),
        "library_unavailable": library.get("unavailable") or [],
        "status_options": library["status_options"],
        "genre_options": library["genre_options"],
        "request_items": seerr["request_items"],
        "collections": collections,
        "health_items": health["health_items"],
        "health_error": health["error"],
        "health_issue_count": health_issue_count,
        "diskspace_items": diskspace["diskspace_items"],
        "diskspace_error": diskspace["error"],
    }


# --- Needs attention ---------------------------------------------------

ATTENTION_CACHE_SECONDS = 30
_ATTENTION_CACHE: dict = {"at": 0.0, "data": None}
_ATTENTION_LOCK = threading.Lock()

_SEVERITY_RANK = {"critical": 0, "warning": 1}
_KIND_RANK = {"unreachable": 0, "disk": 1, "queue": 2, "service": 3, "health": 4}
_BAD_QUEUE_STATUSES = {"warning", "failed", "error"}


def _health_group_title(source: str, messages: list) -> str:
    """
    Titles one merged health warning. Arr apps word the same underlying
    problem slightly differently (each lists its own failing indexers),
    so for indexer checks the names are merged across apps; anything
    else just uses the first message.
    """
    if "indexer" in source.lower() and all(": " in m for m in messages):
        prefix = messages[0].split(": ", 1)[0]
        names = []
        for message in messages:
            for entry in message.split(": ", 1)[1].split(", "):
                entry = entry.strip()
                if entry and entry not in names:
                    names.append(entry)
        return f"{prefix}: {', '.join(names)}"
    return messages[0]


_SERVICE_LABELS = {
    "downloaders": ("downloader", "Downloader"),
    "indexers": ("indexer", "Indexer service"),
    "media_servers": ("mediaserver", "Media server"),
    "transcoders": ("transcoder", "Transcoder"),
}
_FAILING_NAMES_SHOWN = 6


def _name_list(names: list) -> str:
    shown = names[:_FAILING_NAMES_SHOWN]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _failing_indexer_names(service_items: list) -> set:
    """Names listed by 'every indexer is failing' items (see _summarize_services)."""
    names = set()
    for item in service_items:
        if item["id"].startswith("service:indexers:"):
            names.update(n.strip() for n in item["detail"].split(", ") if n.strip() and not n.startswith("and "))
    return names


_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _is_indexer_failure_of(group: dict, names: set) -> bool:
    """True when an arr app's indexer-failure warning only repeats indexers already in `names`."""
    if "indexer" not in group["source"].lower():
        return False
    listed = []
    for message in group["messages"]:
        if ": " not in message:
            return False
        listed += [_TRAILING_PAREN.sub("", e).strip() for e in message.split(": ", 1)[1].split(", ")]
    return bool(listed) and all(name in names for name in listed)


def _summarize_services(services: dict) -> tuple:
    """
    Turns probe results for downloaders, indexer services, media servers and
    transcoders into attention items. Returns (items, complete, checked).

    Each probe result is a dict with an "error" (string or None), plus, for
    indexer services, "indexers": a list of {"name", "enabled", "healthy"}.
    Only outages are raised: a service that can't be reached is critical,
    except a transcoder, which is only a warning because Tdarr is often
    switched off on purpose. Every enabled indexer failing is critical too.
    Only warnings can be hidden, so everything else always stays in front
    of you.
    """
    items = []
    complete = True
    checked = 0

    for group, (service_type, noun) in _SERVICE_LABELS.items():
        for name, result in (services.get(group) or {}).items():
            checked += 1
            result = result or {}
            error = result.get("error")
            if error:
                complete = False
                soft = group == "transcoders"
                items.append({
                    "id": f"unreachable:{service_type}:{name}",
                    "severity": "warning" if soft else "critical",
                    "kind": "service" if soft else "unreachable",
                    "title": f"{name} can't be reached", "detail": str(error),
                    "services": [name], "service_type": service_type, "view": None,
                    "hideable": soft,
                    "hide_key": f"service:{service_type}:{name}:down" if soft else None,
                })
                continue

            if group == "indexers":
                # Some indexers failing is routine and the arr apps already
                # report it (by name), so only "nothing can search" is raised here.
                enabled = [i for i in result.get("indexers") or [] if i.get("enabled", True)]
                if enabled and all(not i.get("healthy", True) for i in enabled):
                    items.append({
                        "id": f"service:indexers:{name}", "severity": "critical", "kind": "service",
                        "title": f"Every indexer is failing in {name}",
                        "detail": _name_list([i.get("name") or "(unknown)" for i in enabled]),
                        "services": [name], "service_type": service_type, "view": None,
                        "hideable": False, "hide_key": None,
                    })

    return items, complete, checked


def summarize_attention(signals: dict, services: dict = None) -> dict:
    """
    Pure function: turns {instance: {"queue": {...}, "health": {...},
    "diskspace": {...}}} (each the dict its fetch_* returned, or an
    {"error": ...} stand-in) into the grouped, severity-ranked list Home
    shows. Kept free of any I/O so it can be tested directly.

    `services` optionally adds the rest of the stack, each as
    {name: probe_result}: {"downloaders": ..., "indexers": ..., "media_servers":
    ..., "transcoders": ...}. See _summarize_services for what each probe
    result looks like.
    """
    items = []
    per_instance = {}
    health_groups: dict = {}
    disk_groups: dict = {}
    complete = True  # False if any call failed, so a missing warning may only mean "couldn't check"

    for name, sig in signals.items():
        queue = sig.get("queue") or {"error": "no data", "queue_items": []}
        health = sig.get("health") or {"error": "no data", "health_items": []}
        disk = sig.get("diskspace") or {"error": "no data", "diskspace_items": []}
        if queue.get("error") or health.get("error") or disk.get("error"):
            complete = False

        state = {"health_issues": 0, "queue_count": 0, "queue_issues": 0, "unreachable": False, "dot": None}
        per_instance[name] = state

        # Every call failing means the service itself is down or
        # rejecting us - one clear item, instead of three error lines.
        if queue.get("error") and health.get("error") and disk.get("error"):
            state["unreachable"] = True
            state["dot"] = "critical"
            items.append({
                "id": f"unreachable:{name}", "severity": "critical", "kind": "unreachable",
                "title": f"{name} can't be reached", "detail": queue["error"],
                "services": [name], "service_type": "instance", "view": None,
                "hideable": False, "hide_key": None,
            })
            continue

        queue_items = queue.get("queue_items") or []
        state["queue_count"] = len(queue_items)
        bad = [q for q in queue_items
               if (q.get("status") or "").lower() in _BAD_QUEUE_STATUSES or q.get("status_messages")]
        state["queue_issues"] = len(bad)
        if bad:
            first = bad[0]
            reason = (first.get("status_messages") or [""])[0]
            detail = first.get("title") or ""
            if reason:
                detail = f"{detail} - {reason}" if detail else reason
            items.append({
                "id": f"queue:{name}", "severity": "warning", "kind": "queue",
                "title": f"{len(bad)} download{'s' if len(bad) != 1 else ''} stalled or failing in {name}",
                "detail": detail, "services": [name], "view": "queue",
                "hideable": True, "hide_key": f"queue:{name}",
            })
            state["dot"] = state["dot"] or "warning"

        for h in health.get("health_items") or []:
            if h.get("type") not in ("warning", "error"):
                continue
            state["health_issues"] += 1
            key = h.get("source") or h.get("message")
            group = health_groups.setdefault(key, {"source": h.get("source") or "", "messages": [], "services": [], "error": False})
            if h["message"] not in group["messages"]:
                group["messages"].append(h["message"])
            if name not in group["services"]:
                group["services"].append(name)
            if h["type"] == "error":
                group["error"] = True
                state["dot"] = "critical"

        for d in disk.get("diskspace_items") or []:
            pct = d.get("percent_free")
            if pct is None or pct >= DISK_LOW_PCT:
                continue
            critical = pct < DISK_CRITICAL_PCT
            # Several apps usually report the very same disk (a shared
            # volume), so identical readings collapse into one item.
            key = (d.get("total_human"), d.get("free_human"), pct)
            group = disk_groups.setdefault(key, {"d": d, "critical": critical, "services": []})
            if name not in group["services"]:
                group["services"].append(name)
            if critical:
                state["dot"] = "critical"

    service_items, services_complete, services_checked = _summarize_services(services or {})
    items.extend(service_items)
    complete = complete and services_complete

    covered = _failing_indexer_names(service_items)
    for key, group in health_groups.items():
        if covered and _is_indexer_failure_of(group, covered):
            continue  # the indexer service's own item already says this
        items.append({
            "id": f"health:{key}",
            "severity": "critical" if group["error"] else "warning",
            "kind": "health",
            "title": _health_group_title(group["source"], group["messages"]),
            "detail": "", "services": group["services"], "view": "system",
            "hideable": not group["error"], "hide_key": f"health:{key}",
        })

    for key, group in disk_groups.items():
        d = group["d"]
        items.append({
            "id": f"disk:{key[0]}:{key[1]}:{key[2]}",
            "severity": "critical" if group["critical"] else "warning",
            "kind": "disk",
            "title": f"{'Critically low' if group['critical'] else 'Low'} disk space: {d.get('path') or 'unknown path'}",
            "detail": f"{d.get('free_human')} free of {d.get('total_human')} ({d.get('percent_free')}%)",
            "services": group["services"], "view": "system",
            "hideable": not group["critical"],
            # Keyed on the volume rather than the reading, so the warning stays
            # hidden while free space drifts by a few GB.
            "hide_key": f"disk:{d.get('path') or key[0]}",
        })

    # Within a kind, what affects the most services comes first - a warning
    # shared by five apps matters more than a one-app config tip.
    items.sort(key=lambda i: (_SEVERITY_RANK[i["severity"]], _KIND_RANK[i["kind"]], -len(i["services"]), i["title"]))
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checked": len(signals) + services_checked,
        "complete": complete,
        "items": items,
        "summary": {
            "critical": sum(1 for i in items if i["severity"] == "critical"),
            "warning": sum(1 for i in items if i["severity"] == "warning"),
            "total": len(items),
        },
        "per_instance": per_instance,
    }


MAX_HIDDEN_ATTENTION = 100
_HIDE_KEY_PREFIXES = ("queue:", "health:", "disk:", "service:")


def valid_hide_key(key) -> bool:
    return isinstance(key, str) and len(key) <= 300 and key.startswith(_HIDE_KEY_PREFIXES)


def apply_hidden_attention(data: dict, hidden) -> dict:
    """
    Pure function: splits a summarize_attention() result into what a person
    still wants to see and what they have hidden, and recomputes the parts of
    the summary that depend on that. Only warnings can be hidden - a critical
    item or an unreachable service always shows. The shared cached result is
    never modified.
    """
    hidden = set(hidden or [])
    visible, hidden_items = [], []
    for item in data["items"]:
        if item.get("hideable") and item.get("hide_key") in hidden:
            hidden_items.append(item)
        else:
            visible.append(item)

    per_instance = {name: dict(state) for name, state in data["per_instance"].items()}
    for item in hidden_items:
        # A hidden stalled-download warning also stops lighting that app's sidebar dot.
        if item["kind"] == "queue":
            for name in item["services"]:
                if name in per_instance and per_instance[name]["dot"] == "warning":
                    per_instance[name]["dot"] = None

    result = dict(data)
    result.update({
        "items": visible,
        "hidden_items": hidden_items,
        "per_instance": per_instance,
        "summary": {
            "critical": sum(1 for i in visible if i["severity"] == "critical"),
            "warning": sum(1 for i in visible if i["severity"] == "warning"),
            "total": len(visible),
        },
    })
    return result


def probe_downloader(downloader: dict) -> dict:
    """Light call that says whether a download client answers (and, for
    qBittorrent, whether its connection is healthy)."""
    if downloader["type"] == "qbittorrent":
        return fetch_qbittorrent_transfer_info(downloader)
    if downloader["type"] == "sabnzbd":
        return {"error": fetch_sabnzbd_queue(downloader)["error"]}
    return {"error": None}


def probe_media_server(server: dict) -> dict:
    """
    Is the server answering at all? Plex (/identity) and Jellyfin/Emby
    (/System/Info/Public) both have a small endpoint that needs no token, so
    a token without admin rights, or one that is missing a permission, can
    never make a running server look down. A refused connection or a
    timeout gets one more try before it counts.
    """
    if server["type"] == "komga":
        return {"error": fetch_komga_libraries(server)["error"]}
    if server["type"] == "bookorbit":
        return {"error": fetch_bookorbit_health(server)["error"]}
    path = "/identity" if server["type"] == "plex" else "/System/Info/Public"
    last_error = None
    for _ in range(2):
        try:
            response = requests.get(
                f"{server['url'].rstrip('/')}{path}", headers={"Accept": "application/json"}, timeout=10,
            )
            response.raise_for_status()
            return {"error": None}
        except requests.exceptions.RequestException as err:
            last_error = err
    return {"error": str(last_error) or last_error.__class__.__name__}


def gather_attention(force: bool = False) -> dict:
    """Collects the light-weight signals (queue, health, disk space) from
    every enabled instance, plus a reachability check of every enabled
    downloader, indexer service, media server and transcoder, all at once,
    and summarises them. Cached for a few seconds so rapid refreshes don't
    hammer the apps; force skips that."""
    with _ATTENTION_LOCK:
        cached = _ATTENTION_CACHE["data"]
        if not force and cached is not None and time.monotonic() - _ATTENTION_CACHE["at"] < ATTENTION_CACHE_SECONDS:
            return cached

    enabled = {n: c for n, c in APPS.items() if c.get("enabled", True)}
    jobs = {}
    for n, c in enabled.items():
        jobs[(n, "queue")] = lambda n=n, c=c: fetch_queue(n, c)
        jobs[(n, "health")] = lambda n=n, c=c: fetch_health(n, c)
        jobs[(n, "diskspace")] = lambda n=n, c=c: fetch_diskspace(n, c)

    probes = {
        "downloaders": (DOWNLOADERS, probe_downloader),
        "indexers": (INDEXERS, fetch_prowlarr_indexers),
        "media_servers": (MEDIA_SERVERS, probe_media_server),
        "transcoders": (TRANSCODERS, fetch_tdarr_status),
    }
    for group, (entries, probe) in probes.items():
        for entry in entries:
            if entry.get("enabled", True):
                jobs[("svc", group, entry["name"])] = lambda entry=entry, probe=probe: probe(entry)
    results = run_concurrently(jobs)

    signals = {}
    for n in enabled:
        signals[n] = {}
        for kind in ("queue", "health", "diskspace"):
            ok, value = results[(n, kind)]
            signals[n][kind] = value if ok else {"error": value}

    services = {group: {} for group in probes}
    for key, (ok, value) in results.items():
        if key[0] == "svc":
            services[key[1]][key[2]] = value if ok else {"error": value}

    data = summarize_attention(signals, services)
    with _ATTENTION_LOCK:
        _ATTENTION_CACHE["data"] = data
        _ATTENTION_CACHE["at"] = time.monotonic()
    return data


@app.route("/api/attention")
@login_required
def api_attention():
    data = gather_attention(force=request.args.get("refresh") == "1")
    user = current_user()
    hidden = list(user.get("hidden_attention") or [])
    if hidden and data.get("complete"):
        # "Hidden until it clears": once a hidden warning is gone from a full,
        # error-free check, forget it, so it shows again if it ever comes back.
        # (An incomplete check proves nothing - an app being briefly
        # unreachable must not un-hide its warnings.)
        live = {i["hide_key"] for i in data["items"] if i.get("hide_key")}
        kept = [k for k in hidden if k in live]
        if kept != hidden:
            if kept:
                user["hidden_attention"] = kept
            else:
                user.pop("hidden_attention", None)
            save_config(CONFIG)
            hidden = kept
    return jsonify(apply_hidden_attention(data, hidden))


@app.route("/api/attention/hide", methods=["POST"])
@login_required
def api_attention_hide():
    """
    Hides or un-hides one warning for the signed-in user:
    {"key": "...", "hidden": true|false}, or {"restore_all": true}.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Expected a JSON object."}), 400
    user = current_user()
    hidden = list(user.get("hidden_attention") or [])

    if payload.get("restore_all"):
        hidden = []
    else:
        key = payload.get("key")
        if not valid_hide_key(key) or not isinstance(payload.get("hidden"), bool):
            return jsonify({"ok": False, "error": "Expected a warning key and hidden true/false."}), 400
        if payload["hidden"]:
            if key not in hidden:
                if len(hidden) >= MAX_HIDDEN_ATTENTION:
                    return jsonify({"ok": False, "error": "Too many hidden warnings - restore some first."}), 400
                hidden.append(key)
        else:
            hidden = [k for k in hidden if k != key]

    if hidden:
        user["hidden_attention"] = hidden
    else:
        user.pop("hidden_attention", None)
    save_config(CONFIG)
    return jsonify({"ok": True, "hidden": hidden})


@app.route("/fragment/instance/<name>")
@login_required
def fragment_instance(name):
    """
    The panels (library, request, queue, history, system) for one
    instance, as an HTML fragment. The dashboard fetches this the first
    time an instance is opened, instead of every instance's data being
    baked into the page up front.
    """
    config = APPS.get(name)
    if not config or not config.get("enabled", True):
        return ("Unknown instance", 404)
    perms = user_permissions(current_user())
    html = render_template(
        "partials/instance_panels.html",
        instance=build_instance_view(name, config, perms),
        can_search=perms["searching"],
    )
    response = app.make_response(html)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/home/stats")
@login_required
def api_home_stats():
    return jsonify(fetch_home_stats())


# ---------------------------------------------------------------------------
# Coming Up (Home): what is about to release or air
# ---------------------------------------------------------------------------
# Sonarr (episodes), Radarr (cinema / digital / physical releases) and Lidarr
# (albums) all answer GET /api/<v>/calendar?start=&end=&unmonitored=false.
# Only monitored items are asked for - the same list their own Calendar page
# shows. Each item carries its instance's colour, so the panel can colour-code
# them the way the sidebar does.

CALENDAR_KINDS = ("movie", "series", "artist", "author")
COMING_UP_DAYS = (7, 14, 30)
COMING_UP_CACHE_SECONDS = 300
_COMING_UP_CACHE: dict = {}
_COMING_UP_LOCK = threading.Lock()

_RADARR_RELEASES = (("inCinemas", "In cinemas"), ("digitalRelease", "Digital release"),
                    ("physicalRelease", "Physical release"))


def _record_date(value):
    """The calendar date in an ISO string ('2026-10-25T00:00:00Z' -> date), or None."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def calendar_items(config_name: str, config: dict, records: list, today) -> list:
    """Turns one app's calendar records into the items the panel shows. Pure
    (no network), so it can be tested with sample responses. A date-only
    release is an all-day item and keeps its date; a timed episode keeps its
    UTC timestamp, which the browser shows on the viewer's own local day."""
    kind = config["library_kind"]
    earliest, latest = today - timedelta(days=1), today + timedelta(days=COMING_UP_DAYS[-1] + 1)
    base = {"instance": config_name, "color": config.get("color") or _DEFAULT_COLOR, "kind": kind}
    items = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if kind == "series":
            when = record.get("airDateUtc") or record.get("airDate")
            day = _record_date(when)
            if day is None or not earliest <= day <= latest:
                continue
            series = record.get("series")
            title = (series.get("title") if isinstance(series, dict) else None) or record.get("seriesTitle") or "Unknown series"
            season, episode = record.get("seasonNumber"), record.get("episodeNumber")
            code = f"S{season:02d}E{episode:02d}" if isinstance(season, int) and isinstance(episode, int) else ""
            detail = " - ".join(part for part in (code, record.get("title") or "") if part)
            items.append({**base, "title": title, "detail": detail, "when": when,
                          "all_day": "T" not in str(when), "has_file": bool(record.get("hasFile"))})
        elif kind == "movie":
            for field, label in _RADARR_RELEASES:
                day = _record_date(record.get(field)) if record.get(field) else None
                if day is None or not earliest <= day <= latest:
                    continue
                items.append({**base, "title": record.get("title") or "Unknown movie", "detail": label,
                              "when": day.isoformat(), "all_day": True, "has_file": bool(record.get("hasFile"))})
        elif kind == "artist":
            day = _record_date(record.get("releaseDate")) if record.get("releaseDate") else None
            if day is None or not earliest <= day <= latest:
                continue
            artist = record.get("artist")
            items.append({**base, "title": record.get("title") or "Unknown album",
                          "detail": (artist.get("artistName") if isinstance(artist, dict) else "") or "",
                          "when": day.isoformat(), "all_day": True, "has_file": False})
        elif kind == "author":
            # Readarr-style BookResource (field names from Readarr's published
            # API client: title, releaseDate, seriesTitle, author.authorName,
            # statistics). Chaptarr is a Readarr-compatible fork; its answers
            # have not been checked against a live server.
            day = _record_date(record.get("releaseDate")) if record.get("releaseDate") else None
            if day is None or not earliest <= day <= latest:
                continue
            author = record.get("author")
            stats = record.get("statistics")
            name = (author.get("authorName") if isinstance(author, dict) else "") or ""
            series = record.get("seriesTitle") or ""
            detail = " - ".join(part for part in (name, series) if isinstance(part, str) and part.strip())
            has_file = isinstance(stats, dict) and (stats.get("bookFileCount") or 0) > 0
            items.append({**base, "title": record.get("title") or "Unknown book",
                          "detail": detail, "when": day.isoformat(), "all_day": True, "has_file": bool(has_file)})
    return items


def fetch_calendar(name: str, config: dict, today=None) -> dict:
    today = today or _utcnow().date()
    params = {
        "start": (today - timedelta(days=1)).isoformat(),
        "end": (today + timedelta(days=COMING_UP_DAYS[-1] + 1)).isoformat(),
        "unmonitored": "false",
    }
    if config["library_kind"] == "series":
        params["includeSeries"] = "true"
    elif config["library_kind"] == "artist":
        params["includeArtist"] = "true"
    elif config["library_kind"] == "author":
        params["includeAuthor"] = "true"
    url = f"{config['url'].rstrip('/')}/api/{api_version(config)}/calendar"
    try:
        response = requests.get(url, headers={"X-Api-Key": config["api_key"]}, params=params, timeout=15)
        response.raise_for_status()
        records = response.json()
    except requests.exceptions.ConnectionError:
        return {"error": "could not connect", "items": []}
    except requests.exceptions.HTTPError:
        return {"error": f"HTTP error {response.status_code} - check API key", "items": []}
    except (requests.exceptions.RequestException, ValueError) as err:
        return {"error": f"request failed: {scrub_log_text(err, 120)}", "items": []}
    return {"error": None, "items": calendar_items(name, config, records if isinstance(records, list) else [], today)}


def gather_coming_up(days: int) -> dict:
    """Every enabled calendar-capable instance at once. Cached for a few
    minutes (release dates barely move); a result with a failed app is not."""
    instances = [(name, cfg) for name, cfg in APPS.items()
                 if cfg.get("enabled", True) and cfg["library_kind"] in CALENDAR_KINDS]
    today = _utcnow().date()
    result = {
        "days": days, "start": today.isoformat(), "end": (today + timedelta(days=days - 1)).isoformat(),
        "instances": [{"name": n, "color": c.get("color") or _DEFAULT_COLOR, "kind": c["library_kind"]} for n, c in instances],
        "items": [], "errors": [],
    }
    if not instances:
        return result
    key = (days, today, tuple((n, c["url"], c["api_key"], c.get("color")) for n, c in instances))
    with _COMING_UP_LOCK:
        cached = _COMING_UP_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < COMING_UP_CACHE_SECONDS:
            return cached[1]
    outcomes = run_concurrently({n: (lambda n=n, c=c: fetch_calendar(n, c, today)) for n, c in instances})
    for name, _cfg in instances:
        ok, value = outcomes[name]
        if not ok:
            result["errors"].append({"instance": name, "error": str(value)})
        elif value["error"]:
            result["errors"].append({"instance": name, "error": value["error"]})
        else:
            result["items"].extend(value["items"])
    result["items"].sort(key=lambda i: (i["when"][:10], i["all_day"] is False, i["when"], i["title"].lower()))
    if not result["errors"]:
        with _COMING_UP_LOCK:
            _COMING_UP_CACHE.clear()  # one live entry is plenty; never grows
            _COMING_UP_CACHE[key] = (time.monotonic(), result)
    return result


@app.route("/api/coming-up")
@login_required
def api_coming_up():
    days = request.args.get("days", type=int)
    return jsonify(gather_coming_up(days if days in COMING_UP_DAYS else COMING_UP_DAYS[0]))


# The Home page is a stack of these panels. Each user can reorder them and
# hide the ones they don't use; that choice is stored on their own user
# record in config.json, so it follows them to every browser and device.
HOME_PANELS = ("attention", "comingup", "downloads", "libraries", "mediaservers", "indexers", "transcoders")
# The panels made of cards a person can put in their own order.
CARD_PANELS = ("libraries", "mediaservers", "indexers", "transcoders")
MAX_CARDS_PER_PANEL = 100
MAX_CARD_NAME_LENGTH = 200


def normalize_home_layout(raw) -> dict:
    """
    Turns whatever was saved (or posted) into a layout that is always safe
    to render: every known panel appears exactly once in `order`, unknown or
    repeated names are dropped, and a panel added in a later release simply
    lands at the end for people who saved a layout before it existed.
    """
    order, wanted_hidden = [], set()
    if isinstance(raw, dict):
        for key in raw.get("order") if isinstance(raw.get("order"), list) else []:
            if isinstance(key, str) and key in HOME_PANELS and key not in order:
                order.append(key)
        for key in raw.get("hidden") if isinstance(raw.get("hidden"), list) else []:
            if isinstance(key, str) and key in HOME_PANELS:
                wanted_hidden.add(key)
    order += [key for key in HOME_PANELS if key not in order]
    layout = {"order": order, "hidden": [key for key in order if key in wanted_hidden]}

    # The order of the cards inside a panel. Names are whatever the person's
    # instances are called, so they aren't checked against the config here (an
    # instance can be renamed or removed later); the page ignores names it
    # doesn't have and puts any card it wasn't told about at the end.
    cards = {}
    raw_cards = raw.get("cards") if isinstance(raw, dict) else None
    if isinstance(raw_cards, dict):
        for key in CARD_PANELS:
            names = []
            for name in raw_cards.get(key) if isinstance(raw_cards.get(key), list) else []:
                if isinstance(name, str) and 0 < len(name) <= MAX_CARD_NAME_LENGTH and name not in names:
                    names.append(name)
                if len(names) >= MAX_CARDS_PER_PANEL:
                    break
            if names:
                cards[key] = names
    if cards:
        layout["cards"] = cards
    return layout


@app.route("/api/theme", methods=["POST"])
@login_required
def api_theme():
    """Saves the signed-in user's colour theme."""
    payload = request.get_json(silent=True)
    theme = payload.get("theme") if isinstance(payload, dict) else None
    if not isinstance(theme, str) or theme not in THEMES:
        return jsonify({"ok": False, "error": "Unknown theme."}), 400
    user = current_user()
    if theme == DEFAULT_THEME:
        user.pop("theme", None)
    else:
        user["theme"] = theme
    save_config(CONFIG)
    return jsonify({"ok": True, "theme": theme})


@app.route("/api/accent", methods=["POST"])
@login_required
def api_accent():
    """Saves the signed-in user's accent colour ("default" clears it)."""
    payload = request.get_json(silent=True)
    accent = payload.get("accent") if isinstance(payload, dict) else None
    if not isinstance(accent, str) or accent not in ACCENTS:
        return jsonify({"ok": False, "error": "Unknown accent."}), 400
    user = current_user()
    if accent == DEFAULT_ACCENT:
        user.pop("accent", None)
    else:
        user["accent"] = accent
    save_config(CONFIG)
    return jsonify({"ok": True, "accent": accent})


@app.route("/api/home/layout", methods=["POST"])
@login_required
def api_home_layout():
    """Saves (or, with {"reset": true}, clears) the signed-in user's Home layout."""
    user = current_user()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Expected a JSON object."}), 400

    if payload.get("reset"):
        user.pop("home_layout", None)
        layout = normalize_home_layout(None)
    else:
        layout = normalize_home_layout(payload)
        user["home_layout"] = layout
    save_config(CONFIG)
    return jsonify({"ok": True, "layout": layout})


@app.route("/api/search/cross-instance")
@login_required
def api_search_cross_instance():
    """
    Searches every configured instance's library at once, rather than
    needing to click into each one separately. Fetches each library
    fresh on every search (same on-demand tradeoff used throughout
    this app) rather than caching, so a very large library set could
    make this noticeably slower than a per-instance search - a
    reasonable cost for how infrequently this gets used compared to
    browsing a single instance.
    """
    query = request.args.get("q", "").strip().lower()
    if len(query) < 2:
        return jsonify({"results": []})

    results = []
    for name, config in APPS.items():
        try:
            library = fetch_library(name, config)
        except requests.exceptions.RequestException:
            continue
        if library.get("error"):
            continue
        for item in library["library_items"]:
            if query in item["title"].lower():
                results.append({
                    "instance": name,
                    "kind": config["library_kind"],
                    "title": item["title"],
                    "cover_url": item["cover_url"],
                    "status": item["status"],
                    "item_id": item["item_id"],
                })

    return jsonify({"results": results[:50]})


def trigger_plex_library_scan(server: dict) -> None:
    """Well-established Plex endpoint for refreshing every library -
    standard across the Plex ecosystem, though not verified against
    this specific instance."""
    response = requests.get(
        f"{server['url'].rstrip('/')}/library/sections/all/refresh",
        params={"X-Plex-Token": server["credential"]}, timeout=15,
    )
    response.raise_for_status()


def trigger_jellyfin_library_scan(server: dict) -> None:
    """Well-established Jellyfin/Emby endpoint for a full library scan -
    standard across the Jellyfin ecosystem, though not verified against
    this specific instance."""
    response = requests.post(
        f"{server['url'].rstrip('/')}/Library/Refresh",
        headers=jellyfin_family_headers(server.get("type"), server["credential"]), timeout=15,
    )
    response.raise_for_status()


@app.route("/api/mediaserver/rescan/<name>", methods=["POST"])
@login_required
def api_mediaserver_rescan(name):
    server = get_media_server(name)
    if not server:
        return jsonify({"ok": False, "error": "unknown media server"}), 404
    if server["type"] not in ("plex", "jellyfin", "emby"):
        return jsonify({"ok": False, "error": "rescanning is not available for this kind of server"}), 400
    try:
        if server["type"] == "plex":
            trigger_plex_library_scan(server)
        else:
            trigger_jellyfin_library_scan(server)
        return jsonify({"ok": True})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)}), 502


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
    """The Test button in Settings. A failure is also written to the log,
    with what was being tested, so a bug report can say more than 'it
    failed' (the service's own message is what the person sees on screen)."""
    result = _run_settings_test()
    try:
        response = result[0] if isinstance(result, tuple) else result
        outcome = response.get_json(silent=True) or {}
        if outcome.get("ok") is False:
            body = request.get_json(silent=True) or {}
            host = urlsplit(str(body.get("url") or "")).netloc.rsplit("@", 1)[-1] or "(no URL)"
            log.warning("Settings test failed: %s/%s at %s: %s", body.get("category"), body.get("kind_or_type"),
                        host, scrub_log_text(outcome.get("error", "")))
    except Exception:  # noqa: BLE001 - logging must never change the answer
        pass
    return result


def _run_settings_test():
    """
    Tests connectivity + credentials for whatever the person has typed
    into a Settings (or first-run wizard) field so far - deliberately
    does NOT read from the saved APPS/MEDIA_SERVERS/REQUESTERS globals,
    since the whole point is to validate values before they're saved.

    Allowed either during initial setup (no accounts exist yet - there's
    nothing to protect before that point) or by a logged-in
    administrator afterward.
    """
    if USERS:
        user = current_user()
        if not user or user.get("role") != "admin":
            return jsonify({"ok": False, "error": "administrator access required"}), 403

    body = request.get_json(force=True)
    category = body.get("category")       # "instance" | "media_server" | "requester" | "tmdb" | "downloader"
    kind_or_type = body.get("kind_or_type")
    url = (body.get("url") or "").strip()
    credential = (body.get("credential") or "").strip()

    if category == "tmdb":
        # TMDB's API endpoint is fixed - there's no URL field for this one.
        try:
            response = requests.get(
                "https://api.themoviedb.org/3/authentication",
                params={"api_key": credential}, timeout=8,
            )
            response.raise_for_status()
        except requests.exceptions.HTTPError:
            detail = response.text[:200] if response is not None else "no response body"
            return jsonify({"ok": False, "error": f"HTTP {response.status_code}: {detail}"})
        except requests.exceptions.RequestException as err:
            return jsonify({"ok": False, "error": str(err)})
        return jsonify({"ok": True})

    if category == "tvdb":
        # TVDB uses a login-for-a-token flow, not a simple query-string
        # key - "testing" it means actually attempting that login.
        pin = (body.get("pin") or "").strip()
        payload = {"apikey": credential}
        if pin:
            payload["pin"] = pin
        try:
            response = requests.post("https://api4.thetvdb.com/v4/login", json=payload, timeout=8)
            response.raise_for_status()
            if not response.json().get("data", {}).get("token"):
                return jsonify({"ok": False, "error": "Login succeeded but no token was returned"})
        except requests.exceptions.HTTPError:
            detail = response.text[:200] if response is not None else "no response body"
            return jsonify({"ok": False, "error": f"HTTP {response.status_code}: {detail}"})
        except requests.exceptions.RequestException as err:
            return jsonify({"ok": False, "error": str(err)})
        return jsonify({"ok": True})

    if category == "fanart":
        # Fanart.tv has no dedicated "check my key" endpoint, so this
        # probes with a deliberately-invalid MusicBrainz ID: a bad key
        # gets 401/403, while a valid key just returns 404 (not found)
        # for a made-up ID - either way, that confirms the key works.
        try:
            response = requests.get(
                "https://webservice.fanart.tv/v3/music/00000000-0000-0000-0000-000000000000",
                params={"api_key": credential}, timeout=8,
            )
            if response.status_code in (401, 403):
                return jsonify({"ok": False, "error": "Invalid API key"})
        except requests.exceptions.RequestException as err:
            return jsonify({"ok": False, "error": str(err)})
        return jsonify({"ok": True})

    if category == "theaudiodb":
        # No dedicated key-check endpoint either - search for a
        # well-known artist and confirm real results come back.
        try:
            response = requests.get(
                f"https://www.theaudiodb.com/api/v1/json/{credential}/search.php",
                params={"s": "Coldplay"}, timeout=8,
            )
            response.raise_for_status()
            if not response.json().get("artists"):
                return jsonify({"ok": False, "error": "Key didn't return any results - it may be invalid"})
        except requests.exceptions.RequestException as err:
            return jsonify({"ok": False, "error": str(err)})
        return jsonify({"ok": True})

    if category == "tautulli":
        if not url:
            return jsonify({"ok": False, "error": "URL is required"})
        try:
            response = requests.get(
                f"{url.rstrip('/')}/api/v2",
                params={"apikey": credential, "cmd": "get_activity"}, timeout=8,
            )
            response.raise_for_status()
            if response.json().get("response", {}).get("result") != "success":
                return jsonify({"ok": False, "error": "Tautulli didn't confirm success - check the API key"})
        except requests.exceptions.RequestException as err:
            return jsonify({"ok": False, "error": str(err)})
        return jsonify({"ok": True})

    if category == "tracearr":
        if not url:
            return jsonify({"ok": False, "error": "URL is required"})
        try:
            response = requests.get(
                f"{url.rstrip('/')}/api/v2/public/users",
                headers={"Authorization": f"Bearer {credential}"},
                params={"pageSize": 1}, timeout=8,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as err:
            return jsonify({"ok": False, "error": str(err)})
        return jsonify({"ok": True})

    if not url:
        return jsonify({"ok": False, "error": "URL is required"})

    try:
        if category == "instance" and kind_or_type == "comic":
            # Kapowarr, unlike the Servarr-family kinds below: api_key is
            # a query parameter, not an X-Api-Key header.
            response = requests.get(
                f"{url.rstrip('/')}/api/system/status",
                params={"api_key": credential}, timeout=8,
            )
        elif category == "instance" and kind_or_type == "librarr":
            # /health needs no key, so it cannot say whether the key works:
            # ask for the download list, which does.
            response = requests.get(
                f"{url.rstrip('/')}/api/downloads",
                headers={"X-Api-Key": credential, "Accept": "application/json"}, timeout=8,
            )
            response.raise_for_status()
            try:
                body = response.json()
                shaped = isinstance(body, dict) and "downloads" in body
            except ValueError:
                shaped = False
            if not shaped:
                return jsonify({"ok": False, "error": "answered, but not like Librarr - check the address and the API key"})
        elif category == "instance":
            meta = KIND_META.get(kind_or_type)
            if not meta:
                return jsonify({"ok": False, "error": "unknown instance kind"})
            version = "v3" if "v3" in meta["queue_endpoint"] else "v1"
            response = requests.get(
                f"{url.rstrip('/')}/api/{version}/system/status",
                headers={"X-Api-Key": credential}, timeout=8,
            )
        elif category == "requester":
            response = requests.get(
                f"{url.rstrip('/')}/api/v1/auth/me",
                headers={"X-Api-Key": credential}, timeout=8,
            )
        elif category == "media_server" and kind_or_type == "plex":
            response = requests.get(
                f"{url.rstrip('/')}/library/sections",
                headers={"Accept": "application/json"},
                params={"X-Plex-Token": credential}, timeout=8,
            )
        elif category == "media_server" and kind_or_type in ("jellyfin", "emby"):
            response = requests.get(
                f"{url.rstrip('/')}/System/Info",
                headers=jellyfin_family_headers(kind_or_type, credential), timeout=8,
            )
        elif category == "media_server" and kind_or_type == "bookorbit":
            # Its health endpoint needs no sign-in, so a good answer there means the
            # address is right. With a key (BookOrbit 3.8 and newer) the key itself is
            # checked too, through /auth/me, which any key may call.
            health = fetch_bookorbit_health({"url": url})
            if health["error"]:
                return jsonify({"ok": False, "error": health["error"]})
            if credential:
                body, error = bookorbit_call({"url": url, "credential": credential}, "/api/v1/auth/me", timeout=8)
                if error:
                    hint = "" if credential.startswith(BOOKORBIT_KEY_PREFIX) else " (BookOrbit keys start with bok_)"
                    return jsonify({"ok": False, "error": error + hint})
                if not isinstance(body, dict):
                    return jsonify({"ok": False, "error": "answered, but not like BookOrbit - check the address"})
            return jsonify({"ok": True})
        elif category == "media_server" and kind_or_type == "komga":
            response = requests.get(
                f"{url.rstrip('/')}/api/v2/users/me",
                headers={"X-API-Key": credential}, timeout=8,
            )
        elif category == "downloader" and kind_or_type == "qbittorrent":
            response = requests.get(
                f"{url.rstrip('/')}/api/v2/app/version",
                headers={"Authorization": f"Bearer {credential}"}, timeout=8,
            )
        elif category == "downloader" and kind_or_type == "sabnzbd":
            response = requests.get(
                f"{url.rstrip('/')}/api",
                params={"mode": "version", "output": "json", "apikey": credential}, timeout=8,
            )
            if response.ok and "error" in response.json():
                return jsonify({"ok": False, "error": response.json()["error"]})
        elif category == "indexerserver" and kind_or_type == "prowlarr":
            response = requests.get(
                f"{url.rstrip('/')}/api/v1/system/status",
                headers={"X-Api-Key": credential}, timeout=8,
            )
        elif category == "transcoder" and kind_or_type == "tdarr":
            response = requests.get(
                f"{url.rstrip('/')}/api/v2/status",
                headers={"x-api-key": credential} if credential else {}, timeout=8,
            )
        else:
            return jsonify({"ok": False, "error": "unknown settings type"})

        response.raise_for_status()
    except requests.exceptions.HTTPError:
        detail = response.text[:200] if response is not None else "no response body"
        if category == "media_server" and kind_or_type in ("jellyfin", "emby") and response.status_code in (401, 403):
            return jsonify({"ok": False, "error": (
                f"HTTP {response.status_code}: the server rejected this API key. Create one in the "
                f"{kind_or_type.capitalize()} dashboard (Dashboard, then API Keys) and paste it here.")})
        return jsonify({"ok": False, "error": f"HTTP {response.status_code}: {detail}"})
    except requests.exceptions.RequestException as err:
        return jsonify({"ok": False, "error": str(err)})

    return jsonify({"ok": True})


def parse_indexed_entries(form, prefix: str, fields: list) -> list:
    """
    Parses dynamically add/removable form rows named like
    f"{prefix}_name_{suffix}", f"{prefix}_url_{suffix}", etc. into a
    list of dicts. Suffixes are arbitrary strings (not necessarily
    sequential), so cards can be added/removed client-side freely
    without the backend needing to know about gaps.

    Order matters here (it's what determines sidebar order for
    instances), so suffixes are collected in first-seen order - form
    submission follows DOM order, which follows however the cards were
    arranged/reordered on the page - rather than an unordered set.
    """
    suffixes = []
    seen = set()
    pattern = re.compile(rf"^{re.escape(prefix)}_name_(.+)$")
    for key in form.keys():
        m = pattern.match(key)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            suffixes.append(m.group(1))

    entries = []
    for suffix in suffixes:
        name = form.get(f"{prefix}_name_{suffix}", "").strip()
        if not name:
            continue
        entry = {"name": name}
        for field in fields:
            if field == "enabled":
                entry["enabled"] = form.get(f"{prefix}_enabled_{suffix}") == "on"
            else:
                entry[field] = form.get(f"{prefix}_{field}_{suffix}", "").strip()
        entries.append(entry)
    return entries


def parse_users(form, existing_users: list) -> list:
    """
    Users need different handling than instances/media servers: a blank
    password field means "keep the current password" rather than "clear
    it", and the admin role always has full permissions regardless of
    what any checkbox says.
    """
    suffixes = []
    seen = set()
    pattern = re.compile(r"^user_username_(.+)$")
    for key in form.keys():
        m = pattern.match(key)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            suffixes.append(m.group(1))

    existing_by_username = {u["username"]: u for u in existing_users}
    users = []
    for suffix in suffixes:
        username = form.get(f"user_username_{suffix}", "").strip()
        if not username:
            continue

        role = form.get(f"user_role_{suffix}", "user")
        password = form.get(f"user_password_{suffix}", "")
        existing = existing_by_username.get(username)

        if password:
            password_hash = generate_password_hash(password)
        elif existing:
            password_hash = existing["password_hash"]
        else:
            continue  # brand-new user with no password set - can't create it yet

        if role == "admin":
            permissions = {"requesting": True, "searching": True}
        else:
            permissions = {
                "requesting": form.get(f"user_perm_requesting_{suffix}") == "on",
                "searching": form.get(f"user_perm_searching_{suffix}") == "on",
            }

        entry = {
            "username": username, "password_hash": password_hash,
            "role": role, "permissions": permissions,
        }
        # Saving Settings rebuilds every user from the form, which has no
        # field for per-user preferences (Home layout, theme, accent, hidden
        # warnings) - carry them forward so they aren't wiped.
        for kept in ("home_layout", "theme", "accent", "hidden_attention", "notify"):
            if existing and existing.get(kept):
                entry[kept] = existing[kept]
        users.append(entry)

    # Guarantee an admin always survives, even if the form somehow
    # didn't include one - avoids ever locking yourself out entirely.
    if not any(u["role"] == "admin" for u in users):
        original_admin = next((u for u in existing_users if u["role"] == "admin"), None)
        if original_admin:
            users.insert(0, original_admin)

    return users


# ---------------------------------------------------------------------------
# Backup, restore and diagnostics (Settings)
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


EXPORT_SECTIONS = ("instances", "media_servers", "requesters", "downloaders", "indexers", "transcoders",
                   "users", "tmdb", "tvdb", "fanart", "theaudiodb", "tautulli", "tracearr")
# What "include API keys" covers. The session signing key (secret_key) is
# never exported or imported: it is not a setting, and replacing it would sign
# everyone out.
EXPORT_SECRET_FIELDS = ("api_key", "credential", "password_hash", "pin",
                        "topic", "token", "webhook_url",  # an account's alert destinations from here on
                        "bot_token", "chat_id", "smtp_user", "smtp_password", "mail_from", "mail_to",
                        "user_key", "app_token")
IMPORT_MAX_BYTES = 1_000_000
CONFIG_BACKUPS_KEPT = 10
_DEFAULT_COLOR = "#5b8cff"
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_PASSWORD_HASH_RE = re.compile(r"^(scrypt|pbkdf2|argon2)[:$]")

# section -> (label, field that holds the type, allowed types, fields kept, secret field)
_ENTRY_SECTIONS = {
    "instances": ("Instances", "kind", KIND_META, ("name", "kind", "url", "api_key", "color", "enabled"), "api_key"),
    "media_servers": ("Media servers", "type", MEDIA_SERVER_TYPES,
                      ("name", "type", "url", "credential", "color", "enabled"), "credential"),
    "requesters": ("Requesters", "type", REQUESTER_TYPES, ("name", "type", "url", "credential", "enabled"), "credential"),
    "downloaders": ("Downloaders", "type", DOWNLOADER_TYPES, ("name", "type", "url", "api_key", "color", "enabled"), "api_key"),
    "indexers": ("Indexer services", "type", INDEXER_SERVICE_TYPES,
                 ("name", "type", "url", "api_key", "color", "enabled"), "api_key"),
    "transcoders": ("Transcoders", "type", TRANSCODER_TYPES, ("name", "type", "url", "api_key", "color", "enabled"), "api_key"),
}
# section -> (label, {field: default}, secret fields)
_SIMPLE_SECTIONS = {
    "tmdb": ("TMDB", {"api_key": ""}, ("api_key",)),
    "tvdb": ("TVDB", {"api_key": "", "pin": ""}, ("api_key", "pin")),
    "fanart": ("Fanart.tv", {"api_key": ""}, ("api_key",)),
    "theaudiodb": ("TheAudioDB", {"api_key": "123"}, ("api_key",)),
    "tautulli": ("Tautulli", {"url": "", "api_key": ""}, ("api_key",)),
    "tracearr": ("Tracearr", {"url": "", "api_key": ""}, ("api_key",)),
}


def apply_config(new_config: dict) -> None:
    """Makes a config the live one - the same in-memory refresh Settings does
    after a save, so a change applies without a restart."""
    global CONFIG, APPS, MEDIA_SERVERS, REQUESTERS, DOWNLOADERS, INDEXERS, TRANSCODERS, USERS, TMDB, TVDB, FANART, THEAUDIODB, TAUTULLI, TRACEARR
    CONFIG = new_config
    APPS = build_apps(CONFIG)
    MEDIA_SERVERS = CONFIG["media_servers"]
    REQUESTERS = CONFIG["requesters"]
    DOWNLOADERS = CONFIG["downloaders"]
    INDEXERS = CONFIG["indexers"]
    TRANSCODERS = CONFIG["transcoders"]
    USERS = CONFIG["users"]
    TMDB = CONFIG["tmdb"]
    TVDB = CONFIG["tvdb"]
    _tvdb_token_cache["token"] = None
    _tvdb_token_cache["obtained_at"] = 0.0
    FANART = CONFIG["fanart"]
    THEAUDIODB = CONFIG["theaudiodb"]
    TAUTULLI = CONFIG["tautulli"]
    TRACEARR = CONFIG["tracearr"]


# --- Export -----------------------------------------------------------------


def _strip_secrets(value):
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in value.items() if k not in EXPORT_SECRET_FIELDS}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


def build_export(cfg: dict, include_secrets: bool) -> dict:
    data = copy.deepcopy({key: cfg.get(key, EMPTY_CONFIG.get(key)) for key in EXPORT_SECTIONS})
    if not include_secrets:
        data = _strip_secrets(data)
    return {
        "arrvantage_export": 1,
        "exported_at": _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "includes_secrets": bool(include_secrets),
        "config": data,
    }


@app.route("/api/config/export")
@login_required
@admin_required
def api_config_export():
    include_secrets = request.args.get("secrets") == "1"
    payload = build_export(CONFIG, include_secrets)
    stamp = _utcnow().strftime("%Y%m%d-%H%M%S")
    name = f"arrvantage-config-{stamp}{'' if include_secrets else '-no-keys'}.json"
    response = Response(json.dumps(payload, indent=2), mimetype="application/json")
    response.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    response.headers["Cache-Control"] = "no-store"
    return response


# --- Import -----------------------------------------------------------------


def _text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _import_entries(section: str, raw, current: list, carry_secrets: bool, notes: list):
    label, type_field, allowed, fields, secret_field = _ENTRY_SECTIONS[section]
    if raw is None:
        return copy.deepcopy(current), False
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list.")
    # A file saved without keys cannot restore them, so keep the key of the
    # entry that is already set up under the same name.
    existing = {(e.get("name"), e.get(type_field)): e for e in current if isinstance(e, dict)}
    entries, skipped = [], 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        name = _text(item.get("name"), 100)
        kind = item.get(type_field)
        url = _text(item.get("url"), 500)
        if not name or kind not in allowed or (url and not url.lower().startswith(("http://", "https://"))):
            skipped += 1
            continue
        entry = {"name": name, type_field: kind, "url": url}
        if "api_key" in fields:
            entry["api_key"] = _text(item.get("api_key"), 500)
        if "credential" in fields:
            entry["credential"] = _text(item.get("credential"), 500)
        if "color" in fields:
            color = item.get("color")
            entry["color"] = color if isinstance(color, str) and _COLOR_RE.match(color) else _DEFAULT_COLOR
        entry["enabled"] = item.get("enabled") if isinstance(item.get("enabled"), bool) else True
        if carry_secrets and not entry.get(secret_field):
            old = existing.get((name, kind))
            if old and old.get(secret_field):
                entry[secret_field] = old[secret_field]
        entries.append(entry)
    if skipped:
        notes.append(f"{label}: {skipped} entr{'y' if skipped == 1 else 'ies'} in the file could not be used and will be skipped.")
    return dedupe_names(entries), True


def _import_simple(section: str, raw, current: dict, carry_secrets: bool):
    _label, defaults, secret_fields = _SIMPLE_SECTIONS[section]
    if not isinstance(raw, dict):
        return copy.deepcopy(current) if isinstance(current, dict) else dict(defaults)
    out = {}
    for field, default in defaults.items():
        value = _text(raw.get(field), 500)
        if carry_secrets and field in secret_fields and not value:
            value = _text((current or {}).get(field), 500)
        out[field] = value or default
    return out


def _import_users(raw, notes: list) -> list:
    if not isinstance(raw, list):
        raise ValueError("Users must be a list.")
    users, skipped = [], 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        username = _text(item.get("username"), 64)
        password_hash = item.get("password_hash")
        role = item.get("role")
        if (not username or role not in ("admin", "user") or not isinstance(password_hash, str)
                or not _PASSWORD_HASH_RE.match(password_hash) or any(u["username"] == username for u in users)):
            skipped += 1
            continue
        perms = item.get("permissions") if isinstance(item.get("permissions"), dict) else {}
        user = {
            "username": username, "password_hash": password_hash, "role": role,
            "permissions": ({"requesting": True, "searching": True} if role == "admin" else
                            {"requesting": perms.get("requesting") is True, "searching": perms.get("searching") is True}),
        }
        if item.get("theme"):
            user["theme"] = normalize_theme(item.get("theme"))
        if item.get("accent"):
            user["accent"] = normalize_accent(item.get("accent"))
        if isinstance(item.get("home_layout"), dict):
            user["home_layout"] = normalize_home_layout(item["home_layout"])
        if isinstance(item.get("hidden_attention"), list):
            user["hidden_attention"] = [k for k in item["hidden_attention"] if valid_hide_key(k)][:MAX_HIDDEN_ATTENTION]
        notify = normalize_notify(item.get("notify")) if role == "admin" else None
        if notify:
            user["notify"] = notify
        users.append(user)
    if skipped:
        notes.append(f"Users: {skipped} account{'' if skipped == 1 else 's'} in the file could not be used and will be skipped.")
    return users


def plan_import(file_data, current: dict):
    """
    Turns an uploaded export (or a raw config.json) into the config that would
    replace the current one, without touching anything. Returns
    (new_config or None, report). A file saved without API keys keeps the keys
    and accounts already set up; the session key is always kept.
    """
    report = {"errors": [], "notes": [], "sections": [], "includes_secrets": True, "signs_you_out": False}
    if not isinstance(file_data, dict):
        report["errors"].append("This is not an arrVantage config file.")
        return None, report
    if "arrvantage_export" in file_data:
        incoming = file_data.get("config")
        includes_secrets = file_data.get("includes_secrets") is True
    elif any(key in file_data for key in EXPORT_SECTIONS):
        incoming, includes_secrets = file_data, True  # a raw config.json
    else:
        report["errors"].append("This is not an arrVantage config file.")
        return None, report
    if not isinstance(incoming, dict):
        report["errors"].append("This is not an arrVantage config file.")
        return None, report
    report["includes_secrets"] = includes_secrets
    carry_secrets = not includes_secrets
    if isinstance(incoming.get("instances"), list):
        incoming = copy.deepcopy(incoming)  # an older file kept Librarr under Downloaders
        if migrate_librarr_downloaders(incoming):
            report["notes"].append("Librarr was a download client in this file; it will be added as an instance instead.")

    new_config = {"secret_key": current.get("secret_key", "")}
    try:
        for section, (label, *_rest) in _ENTRY_SECTIONS.items():
            entries, present = _import_entries(section, incoming.get(section), current.get(section, []),
                                               carry_secrets, report["notes"])
            new_config[section] = entries
            report["sections"].append({
                "label": label, "current": len(current.get(section, [])), "incoming": len(entries) if present else None,
                "names": [e["name"] for e in entries],
            })
        for section in _SIMPLE_SECTIONS:
            new_config[section] = _import_simple(section, incoming.get(section), current.get(section), carry_secrets)

        raw_users = incoming.get("users")
        if includes_secrets and raw_users is not None:
            users = _import_users(raw_users, report["notes"])
            if not any(u["role"] == "admin" for u in users):
                report["errors"].append("The file has no administrator account that can be restored, so importing it "
                                        "would lock you out. Nothing was changed.")
                return None, report
            new_config["users"] = users
            report["sections"].append({"label": "Users", "current": len(current.get("users", [])),
                                       "incoming": len(users), "names": [u["username"] for u in users]})
            report["signs_you_out"] = not any(u["username"] == session.get("username") for u in users)
        else:
            new_config["users"] = copy.deepcopy(current.get("users", []))
            report["notes"].append("Accounts and passwords are kept as they are"
                                   + ("" if includes_secrets else " (the file was saved without keys and password hashes)") + ".")
    except ValueError as err:
        report["errors"].append(str(err))
        return None, report
    return new_config, report


def backup_config() -> str:
    """Saves the current settings next to config.json before an import
    replaces them, keeping the newest few. Returns the file name."""
    name = f"config.backup-{_utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    path = os.path.join(CONFIG_DIR, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # it holds API keys
    with os.fdopen(fd, "w") as handle:
        json.dump(CONFIG, handle, indent=2)
    backups = sorted(f for f in os.listdir(CONFIG_DIR) if re.fullmatch(r"config\.backup-\d{8}-\d{6}\.json", f))
    for stale in backups[:-CONFIG_BACKUPS_KEPT]:
        try:
            os.remove(os.path.join(CONFIG_DIR, stale))
        except OSError:
            pass
    return name


@app.route("/api/config/import", methods=["POST"])
@login_required
@admin_required
def api_config_import():
    """Two calls: without `confirm` it only reports what would change; with
    `confirm: true` it backs up the current settings and applies the file."""
    raw = request.get_data(cache=True)
    if len(raw) > IMPORT_MAX_BYTES:
        return jsonify({"ok": False, "errors": ["That file is too large to be an arrVantage config."]}), 413
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "file" not in payload:
        return jsonify({"ok": False, "errors": ["Expected the file's contents."]}), 400
    new_config, report = plan_import(payload["file"], CONFIG)
    if report["errors"]:
        return jsonify({"ok": False, "errors": report["errors"], "report": report}), 400
    if payload.get("confirm") is not True:
        return jsonify({"ok": True, "applied": False, "report": report})

    backup = backup_config()
    save_config(new_config)
    apply_config(new_config)
    with _ATTENTION_LOCK:
        _ATTENTION_CACHE["data"] = None
    with _LIBRARY_CACHE_LOCK:
        _LIBRARY_CACHE.clear()
    log.info("Config imported by %r; the previous settings are saved as %s", session.get("username"), backup)
    return jsonify({"ok": True, "applied": True, "backup": backup, "report": report})


# --- Diagnostics ------------------------------------------------------------


def build_fingerprint() -> str:
    """A short hash of app.py: the app has no version number of its own, and
    this tells two builds apart when someone reports a problem."""
    try:
        with open(os.path.abspath(__file__), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:10]
    except OSError:
        return "unknown"


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:  # noqa: BLE001
        return "unknown"


_KIND_ALIAS = {"movie": "radarr", "series": "sonarr", "artist": "lidarr", "author": "readarr", "comic": "kapowarr", "librarr": "librarr"}


def _service_aliases() -> list:
    """(address, hostname, label) for every configured service, where the label
    is the service type plus a number ('jellyfin-1') - what stands in for an
    address when the person asks for it to be hidden."""
    aliases, counts = [], {}

    def add(label: str, url: str):
        parts = urlsplit(url or "")
        if not parts.netloc:
            return
        counts[label] = counts.get(label, 0) + 1
        netloc = parts.netloc.rsplit("@", 1)[-1]
        aliases.append((netloc, parts.hostname or "", f"{label}-{counts[label]}"))

    for entry in CONFIG.get("instances", []):
        add(_KIND_ALIAS.get(entry.get("kind"), "arr"), entry.get("url"))
    for section in ("media_servers", "requesters", "downloaders", "indexers", "transcoders"):
        for entry in CONFIG.get(section, []):
            add(str(entry.get("type") or section), entry.get("url"))
    for section in ("tautulli", "tracearr"):
        add(section, (CONFIG.get(section) or {}).get("url"))
    return aliases


def hide_addresses(text: str) -> str:
    for netloc, host, label in sorted(_service_aliases(), key=lambda a: -len(a[0])):
        text = text.replace(netloc, label)
        if host:
            text = re.sub(rf"(?<![\w.-]){re.escape(host)}(?![\w.-])", label, text)
    return re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "x.x.x.x", text)


def _count_line(label: str, entries: list) -> str:
    enabled = sum(1 for e in entries if e.get("enabled", True))
    return f"  {label}: {len(entries)} ({enabled} enabled)"


def build_diagnostics(hide: bool = True) -> str:
    """A block of plain text for pasting into a bug report. It describes the
    setup by type and count only - no names, URLs, keys or usernames."""
    now = _utcnow()
    lines = [
        "arrVantage diagnostics",
        f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"Version: {resolve_app_version()} (build {build_fingerprint()})",
        f"Running for: {int((time.time() - _PROCESS_STARTED) // 60)} min",
        f"Python {platform.python_version()}, Flask {_package_version('flask')}, "
        f"requests {_package_version('requests')}, gunicorn {_package_version('gunicorn')}",
        f"Log level: {logging.getLevelName(resolve_log_level(os.environ.get('LOG_LEVEL')))}",
        f"HTTPS-only cookies: {'on' if app.config.get('SESSION_COOKIE_SECURE') else 'off'}",
        "",
        "Set up:",
    ]
    by_kind = {}
    for entry in CONFIG.get("instances", []):
        by_kind.setdefault((KIND_META.get(entry.get("kind")) or {}).get("label", str(entry.get("kind"))), []).append(entry)
    for label, entries in sorted(by_kind.items()):
        lines.append(_count_line(label, entries))
    for section, labels in (("media_servers", MEDIA_SERVER_TYPES), ("requesters", REQUESTER_TYPES),
                            ("downloaders", DOWNLOADER_TYPES), ("indexers", INDEXER_SERVICE_TYPES),
                            ("transcoders", TRANSCODER_TYPES)):
        grouped = {}
        for entry in CONFIG.get(section, []):
            grouped.setdefault(labels.get(entry.get("type"), str(entry.get("type"))), []).append(entry)
        for label, entries in sorted(grouped.items()):
            lines.append(_count_line(label, entries))
    for section, label in (("tautulli", "Tautulli"), ("tracearr", "Tracearr")):
        if (CONFIG.get(section) or {}).get("url"):
            lines.append(f"  {label}: configured")
    lines.append(f"  Users: {len(CONFIG.get('users', []))}")
    if not any(by_kind.values()) and len(lines) <= 9:
        lines.append("  (nothing configured yet)")

    cached = _ATTENTION_CACHE["data"]
    lines += ["", "Last Needs attention check:"]
    if cached:
        summary = cached.get("summary") or {}
        lines.append(f"  {summary.get('critical', 0)} critical, {summary.get('warning', 0)} warning"
                     f"{'' if cached.get('complete', True) else ' (incomplete: some checks failed)'}")
    else:
        lines.append("  not run yet")

    # Counts only - no names, no addresses, no topics.
    status = monitor_status()
    with _HISTORY_LOCK:
        history_state = _history_state()
        open_count = sum(1 for i in history_state["open"].values() if i.get("announced"))
        recorded = len(history_state["history"])
    lines += ["", "Monitor and alerts:"]
    lines.append(f"  Background check: every {status['interval']} s" if status["enabled"] else "  Background check: off")
    if status["last_check"]:
        lines.append(f"  Last background check: {status['last_check']}")
    lines.append(f"  Problems open now: {open_count}; outages in the history: {recorded}")
    admins = [normalize_notify(u.get("notify")) for u in CONFIG.get("users", []) if u.get("role") == "admin"]
    admins = [n for n in admins if n]
    lines.append(f"  Administrators with alerts: {len(admins)} "
                 f"(" + ", ".join(f"{channel}: {sum(1 for n in admins if n.get(channel))}" for channel in NOTIFY_CHANNELS)
                 + f", everything level: {sum(1 for n in admins if n['level'] == 'all')})")

    lines += ["", f"Recent warnings (newest last, up to {RECENT_WARNING_LIMIT}):"]
    recent = list(_recent_warnings)
    if not recent:
        lines.append("  none since start")
    for created, level, message in recent:
        stamp = datetime.fromtimestamp(created, timezone.utc).strftime("%H:%M:%S")
        lines.append(f"  {stamp} {level} {scrub_log_text(message, 400)}")

    text = "\n".join(lines)
    return hide_addresses(text) if hide else text


@app.route("/api/diagnostics")
@login_required
@admin_required
def api_diagnostics():
    hide = request.args.get("hide_addresses", "1") != "0"
    return jsonify({"ok": True, "text": build_diagnostics(hide)})


# ---------------------------------------------------------------------------
# Monitor, service history and alerts
# ---------------------------------------------------------------------------
#
# Needs attention used to be checked only while somebody had the dashboard
# open. A small background thread now runs the same check every
# MONITOR_INTERVAL seconds, remembers when a problem started and ended (the
# service history), and tells the administrators who asked to hear about it
# (ntfy, Discord, Telegram, email, Slack, Pushover, Gotify or a webhook). It is a thread of the one gunicorn worker, like the
# other in-process state; MONITOR_INTERVAL=0 switches it off.

MONITOR_DEFAULT_INTERVAL = 60
MONITOR_MIN_INTERVAL = 30
MONITOR_MAX_INTERVAL = 3600
MONITOR_FIRST_DELAY = 15       # seconds after start before the first check
MONITOR_CONFIRM_CHECKS = 2     # checks in a row before a problem or a recovery counts
HISTORY_KEEP_DAYS = 90
HISTORY_MAX_ENTRIES = 500
HISTORY_API_LIMIT = 100
HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
NOTIFY_TIMEOUT_SECONDS = 10
NOTIFY_MAX_SEPARATE = 3        # more events than this in one check become one summary message
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
NOTIFY_LEVELS = {
    "critical": "Outages and critical problems only",
    "all": "Everything, warnings included",
}
_NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
# Where an administrator can be alerted, in the order a message is sent to them.
NOTIFY_CHANNELS = ("ntfy", "discord", "telegram", "email", "slack", "pushover", "gotify", "webhook")
# Discord webhook addresses look like https://discord.com/api/webhooks/<id>/<token>
# (ptb. and canary. are Discord's own test builds; discordapp.com is the old name).
# Checking the shape means a typo is caught on save, and this destination can only
# ever send to Discord.
_DISCORD_HOSTS = ("discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com")
_DISCORD_PATH_RE = re.compile(r"^/api(?:/v\d{1,2})?/webhooks/\d{5,25}/[A-Za-z0-9._-]{20,120}$")
_DISCORD_QUERY_RE = re.compile(r"^thread_id=\d{1,25}$")
DISCORD_COLORS = {"critical": 0xE5484D, "warning": 0xF5A524, "cleared": 0x30A46C, "test": 0x3E63DD}
# Telegram: a bot token is "<bot id>:<secret>"; the chat is a number (negative for
# groups and channels) or a public channel's @name. The API address is fixed in the
# code, so that box can only ever send to Telegram.
TELEGRAM_API = "https://api.telegram.org"
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")
_TELEGRAM_CHAT_RE = re.compile(r"^(?:-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$")
TELEGRAM_MAX_CHATS = 10
# Slack: an incoming webhook is https://hooks.slack.com/services/<team>/<app>/<secret> (Slack's
# "Sending messages using incoming webhooks" page). Checking the shape means a typo is caught on
# save and this destination can only ever send to Slack. (GovSlack uses another domain, whose
# exact host name could not be confirmed, so it is not accepted - use the generic webhook there.)
_SLACK_HOSTS = ("hooks.slack.com",)
_SLACK_PATH_RE = re.compile(r"^/services/[A-Z0-9]{5,30}/[A-Z0-9]{5,30}/[A-Za-z0-9]{10,60}$")
# Pushover (pushover.net/api): POST https://api.pushover.net/1/messages.json with the app's token
# and the person's user key, each 30 letters and digits. The address is fixed in the code.
PUSHOVER_API = "https://api.pushover.net/1/messages.json"
_PUSHOVER_KEY_RE = re.compile(r"^[A-Za-z0-9]{30}$")
# Gotify (gotify.net/docs/pushmsg): a server of the person's own; POST <server>/message with the
# application token in the X-Gotify-Key header.
_GOTIFY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.~-]{1,100}$")
# Email: the mail server is typed in by the administrator (like a webhook address).
# security -> (label, usual port)
SMTP_SECURITY = {
    "starttls": ("STARTTLS (usually port 587)", 587),
    "ssl": ("SSL/TLS (usually port 465)", 465),
    "none": ("None, unencrypted (port 25; only for a mail server on your own network)", 25),
}
MAIL_MAX_RECIPIENTS = 5
_MAIL_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_MAIL_ADDRESS_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,250}[A-Za-z0-9])?$")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def resolve_monitor_interval(raw) -> int:
    """Seconds between background checks. Nothing or nonsense means the default;
    0 turns the monitor off; otherwise it is kept between 30 s and an hour (the
    services are being asked every time, so a tight loop would be unkind)."""
    text = str(raw if raw is not None else "").strip()
    try:
        value = int(text)
    except ValueError:
        return MONITOR_DEFAULT_INTERVAL
    if value == 0:
        return 0
    if value < 0:
        return MONITOR_DEFAULT_INTERVAL
    return max(MONITOR_MIN_INTERVAL, min(value, MONITOR_MAX_INTERVAL))


def _iso_now() -> str:
    return time.strftime(_TIME_FORMAT, time.gmtime())


def _parse_time(value):
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def human_duration(seconds) -> str:
    """'45 s', '4 min', '2 h 5 min', '3 d 2 h' - how long something lasted."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def _between(start, end) -> str:
    a, b = _parse_time(start), _parse_time(end)
    return human_duration((b - a).total_seconds()) if a and b else "a while"


# --- Tracking problems over time (pure) ---------------------------------------


def incident_key(item: dict) -> str:
    """What makes two sightings 'the same problem'. A warning that can be hidden
    already has a stable key for exactly this (a disk's key ignores how much is
    free); everything else keeps one id."""
    return str(item.get("hide_key") or item.get("id") or "")


def is_outage(item: dict) -> bool:
    """A service that is down or a critical problem - what the history lists,
    and all that the 'outages only' alert level sends."""
    return item.get("kind") in ("unreachable", "service") or item.get("severity") == "critical"


def empty_history() -> dict:
    return {"version": 1, "open": {}, "history": []}


def _ensure_history_entry(history: list, incident: dict) -> None:
    if any(entry["key"] == incident["key"] and entry["end"] is None for entry in history):
        return
    history.append({
        "key": incident["key"], "title": incident["title"], "detail": incident["detail"],
        "severity": incident["severity"], "kind": incident["kind"],
        "services": list(incident["services"]), "start": incident["since"], "end": None,
    })


def track_incidents(state: dict, data: dict, now: str, confirm: int = MONITOR_CONFIRM_CHECKS):
    """
    Pure. `state` is what the last check left behind, `data` a
    summarize_attention() result, `now` an ISO time. Returns (new_state, events).

    A problem is only announced once it has been seen `confirm` checks in a row,
    and only called over once it has been absent that many times, so one slow
    answer neither raises an alarm nor ends an outage. A check where something
    failed to answer (complete is False) cannot prove a *warning* has cleared -
    its call may be the very thing that failed - so only a service that is
    itself being probed (unreachable / service items) can end then.
    """
    state = copy.deepcopy(state)
    open_incidents, history = state["open"], state["history"]
    complete = bool(data.get("complete"))
    present = {}
    for item in data.get("items") or []:
        key = incident_key(item)
        if key:
            present.setdefault(key, item)

    events = []
    for key, item in present.items():
        incident = open_incidents.get(key)
        if incident is None:
            incident = open_incidents[key] = {"key": key, "since": now, "seen": 0, "missing": 0, "announced": False}
        incident.update({
            "title": str(item.get("title") or ""), "detail": str(item.get("detail") or ""),
            "severity": item.get("severity") or "warning", "kind": item.get("kind") or "",
            "services": [str(s) for s in item.get("services") or []], "outage": is_outage(item),
        })
        incident["seen"] = min(incident["seen"] + 1, confirm)  # capped, so a long outage doesn't rewrite the file every check
        incident["missing"] = 0
        if not incident["announced"] and incident["seen"] >= confirm:
            incident["announced"] = True
            events.append({"type": "opened", "incident": copy.deepcopy(incident)})
        if incident["announced"] and incident["outage"]:
            _ensure_history_entry(history, incident)  # also catches a warning that became critical

    for key in list(open_incidents):
        if key in present:
            continue
        incident = open_incidents[key]
        if not (complete or incident.get("kind") in ("unreachable", "service")):
            continue
        if not incident["announced"]:
            del open_incidents[key]  # a blip that never counted
            continue
        incident["missing"] += 1
        if incident["missing"] >= confirm:
            del open_incidents[key]
            events.append({"type": "cleared", "incident": copy.deepcopy(incident), "ended": now})
            for entry in reversed(history):
                if entry["key"] == key and entry["end"] is None:
                    entry["end"] = now
                    break
    return state, events


def prune_history(history: list, now: str, keep_days: int = HISTORY_KEEP_DAYS, limit: int = HISTORY_MAX_ENTRIES) -> list:
    stamp = _parse_time(now) or datetime.now(timezone.utc)
    cutoff = (stamp - timedelta(days=keep_days)).strftime(_TIME_FORMAT)
    kept = [entry for entry in history if entry["end"] is None or entry["end"] >= cutoff]
    return kept[-limit:]


def _clean_history_state(raw) -> dict:
    """history.json is read back from disk, so trust nothing in it."""
    state = empty_history()
    if not isinstance(raw, dict):
        return state

    def text(value, limit=300):
        return value[:limit] if isinstance(value, str) else ""

    def when(value):
        return value if _parse_time(value) else None

    for key, inc in (raw.get("open") if isinstance(raw.get("open"), dict) else {}).items():
        if isinstance(key, str) and isinstance(inc, dict) and when(inc.get("since")):
            state["open"][key[:300]] = {
                "key": key[:300], "since": inc["since"], "seen": MONITOR_CONFIRM_CHECKS if inc.get("announced") is True else 0,
                "missing": 0, "announced": inc.get("announced") is True, "title": text(inc.get("title")),
                "detail": text(inc.get("detail")), "severity": "critical" if inc.get("severity") == "critical" else "warning",
                "kind": text(inc.get("kind"), 40), "outage": inc.get("outage") is True,
                "services": [text(s, 100) for s in (inc.get("services") if isinstance(inc.get("services"), list) else [])][:20],
            }
    for entry in (raw.get("history") if isinstance(raw.get("history"), list) else []):
        if isinstance(entry, dict) and when(entry.get("start")) and isinstance(entry.get("key"), str):
            end = when(entry.get("end"))
            if end is None and entry["key"] not in state["open"]:
                end = entry["start"]  # an outage nothing remembers ending: don't show it as still going
            state["history"].append({
                "key": entry["key"][:300], "title": text(entry.get("title")), "detail": text(entry.get("detail")),
                "severity": "critical" if entry.get("severity") == "critical" else "warning",
                "kind": text(entry.get("kind"), 40),
                "services": [text(s, 100) for s in (entry.get("services") if isinstance(entry.get("services"), list) else [])][:20],
                "start": entry["start"], "end": end,
            })
    return state


# --- Storing it ---------------------------------------------------------------

_HISTORY_LOCK = threading.Lock()
_HISTORY = {"state": None}
_MONITOR = {"thread": None, "interval": None, "last_check": None, "checks": 0}


def load_history() -> dict:
    try:
        with open(HISTORY_PATH, "r") as handle:
            return _clean_history_state(json.load(handle))
    except FileNotFoundError:
        return empty_history()
    except (OSError, ValueError) as err:
        log.warning("history.json could not be read (%s); the service history starts again", err.__class__.__name__)
        return empty_history()


def save_history(state: dict) -> None:
    """Written to a temporary file and moved into place, so a restart in the
    middle of a write cannot leave half a file behind."""
    tmp = HISTORY_PATH + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(state, handle)
        os.replace(tmp, HISTORY_PATH)
    except OSError as err:
        log_problem(("history-save", err.__class__.__name__),
                    "The service history could not be saved (%s: %s)", err.__class__.__name__, scrub_log_text(err))


def _history_state() -> dict:
    """The live history, read from disk the first time. Callers hold _HISTORY_LOCK."""
    if _HISTORY["state"] is None:
        _HISTORY["state"] = load_history()
    return _HISTORY["state"]


# --- Alert destinations (per administrator) ----------------------------------


def _clean_url(value, limit: int = 500) -> str:
    text = value.strip()[:limit] if isinstance(value, str) else ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    return text if parts.scheme in ("http", "https") and parts.netloc else ""


def _clean_discord_url(value) -> str:
    """A Discord webhook address in its plain form, or "" when it is not one.
    Only https, only Discord's own hosts, only the webhook path (and a forum
    thread_id if present); a user name, port or any other query is refused."""
    text = _clean_url(value, 300)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return ""
    path = parts.path.rstrip("/")
    if (parts.scheme != "https" or (parts.hostname or "").lower() not in _DISCORD_HOSTS or port not in (None, 443)
            or "@" in parts.netloc or not _DISCORD_PATH_RE.match(path)
            or (parts.query and not _DISCORD_QUERY_RE.match(parts.query)) or parts.fragment):
        return ""
    return f"https://{parts.hostname.lower()}{path}" + (f"?{parts.query}" if parts.query else "")


def _clean_slack_url(value) -> str:
    """A Slack incoming-webhook address in its plain form, or "" when it is not one:
    https, Slack's own host, the /services/<team>/<app>/<secret> path, nothing else."""
    text = _clean_url(value, 300)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return ""
    path = parts.path.rstrip("/")
    if (parts.scheme != "https" or (parts.hostname or "").lower() not in _SLACK_HOSTS or port not in (None, 443)
            or "@" in parts.netloc or not _SLACK_PATH_RE.match(path) or parts.query or parts.fragment):
        return ""
    return f"https://{parts.hostname.lower()}{path}"


def _clean_pushover_key(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _PUSHOVER_KEY_RE.match(text) else ""


def normalize_pushover(raw):
    if not isinstance(raw, dict):
        return None
    user, token = _clean_pushover_key(raw.get("user_key")), _clean_pushover_key(raw.get("app_token"))
    return {"user_key": user, "app_token": token} if user and token else None


def _clean_gotify_server(value) -> str:
    """The address of a Gotify server, without a trailing slash. A path is allowed
    (Gotify behind a reverse proxy at /gotify); a login in the address, a query or a
    fragment are not."""
    text = _clean_url(value, 300)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return ""
    if "@" in parts.netloc or parts.query or parts.fragment:
        return ""
    return text.rstrip("/")


def _clean_gotify_token(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _GOTIFY_TOKEN_RE.match(text) else ""


def normalize_gotify(raw):
    if not isinstance(raw, dict):
        return None
    server, token = _clean_gotify_server(raw.get("server")), _clean_gotify_token(raw.get("token"))
    return {"server": server, "token": token} if server and token else None


def _clean_telegram_token(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if _TELEGRAM_TOKEN_RE.match(text) else ""


def _clean_telegram_chat(value) -> str:
    """A chat id as text ("123456789", "-1001234567890") or a channel's "@name"."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    text = value.strip() if isinstance(value, str) else ""
    return text if _TELEGRAM_CHAT_RE.match(text) else ""


def _clean_mail_address(value) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text) <= 254 and _MAIL_ADDRESS_RE.match(text) else ""


def split_mail_recipients(value) -> tuple:
    """(valid addresses, anything that was not one). Takes a list or text with
    commas, semicolons or spaces between addresses; repeats are dropped."""
    if isinstance(value, str):
        pieces = re.split(r"[,;\s]+", value.strip())
    elif isinstance(value, list):
        pieces = [p.strip() for p in value if isinstance(p, str)]
    else:
        pieces = []
    good, bad = [], []
    for piece in pieces:
        if not piece:
            continue
        address = _clean_mail_address(piece)
        if not address:
            bad.append(piece[:60])
        elif address.lower() not in [g.lower() for g in good]:
            good.append(address)
    return good, bad


def _clean_mail_host(value) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    return text if _MAIL_HOST_RE.match(text) else ""


def _clean_port(value, default: int) -> int:
    try:
        port = int(str(value).strip()) if value not in (None, "") else default
    except ValueError:
        return default
    return port if 1 <= port <= 65535 else default


def normalize_telegram(raw):
    if not isinstance(raw, dict):
        return None
    token, chat = _clean_telegram_token(raw.get("bot_token")), _clean_telegram_chat(raw.get("chat_id"))
    return {"bot_token": token, "chat_id": chat} if token and chat else None


def normalize_email(raw):
    """
    {"host", "port", "security": starttls|ssl|none, "mail_from", "mail_to": [...],
     "smtp_user"?, "smtp_password"?}  - or None when it is not usable (no server,
    no sender or no recipient). The login is optional (a mail server on your own
    network often wants none).
    """
    if not isinstance(raw, dict):
        return None
    host = _clean_mail_host(raw.get("host"))
    security = raw.get("security") if raw.get("security") in SMTP_SECURITY else "starttls"
    user = raw.get("smtp_user").strip()[:200] if isinstance(raw.get("smtp_user"), str) else ""
    password = raw.get("smtp_password").strip()[:200] if isinstance(raw.get("smtp_password"), str) else ""
    sender = _clean_mail_address(raw.get("mail_from")) or _clean_mail_address(user)
    recipients = split_mail_recipients(raw.get("mail_to"))[0][:MAIL_MAX_RECIPIENTS]
    if not (host and sender and recipients):
        return None
    out = {"host": host, "port": _clean_port(raw.get("port"), SMTP_SECURITY[security][1]), "security": security,
           "mail_from": sender, "mail_to": recipients}
    if user:
        out["smtp_user"] = user
        if password:
            out["smtp_password"] = password
    return out


def normalize_notify(raw):
    """
    The clean form of an account's alert settings, or None when no destination
    is set up:
        {"level": "critical" | "all",
         "ntfy":     {"server", "topic", "token"?},
         "discord":  {"webhook_url"},
         "telegram": {"bot_token", "chat_id"},
         "email":    {"host", "port", "security", "mail_from", "mail_to", "smtp_user"?, "smtp_password"?},
         "slack":    {"webhook_url"},
         "pushover": {"user_key", "app_token"},
         "gotify":   {"server", "token"},
         "webhook":  {"webhook_url"}}
    Anything unusable is dropped rather than raised, because this also reads
    config.json and uploaded files.
    """
    if not isinstance(raw, dict):
        return None
    out = {"level": raw.get("level") if raw.get("level") in NOTIFY_LEVELS else "critical"}
    ntfy = raw.get("ntfy")
    if isinstance(ntfy, dict):
        topic = ntfy.get("topic").strip() if isinstance(ntfy.get("topic"), str) else ""
        if _NTFY_TOPIC_RE.match(topic):
            entry = {"server": _clean_url(ntfy.get("server"), 300) or DEFAULT_NTFY_SERVER, "topic": topic}
            token = ntfy.get("token").strip()[:200] if isinstance(ntfy.get("token"), str) else ""
            if token:
                entry["token"] = token
            out["ntfy"] = entry
    discord = raw.get("discord")
    if isinstance(discord, dict):
        url = _clean_discord_url(discord.get("webhook_url"))
        if url:
            out["discord"] = {"webhook_url": url}
    telegram = normalize_telegram(raw.get("telegram"))
    if telegram:
        out["telegram"] = telegram
    mail = normalize_email(raw.get("email"))
    if mail:
        out["email"] = mail
    slack = raw.get("slack")
    if isinstance(slack, dict):
        url = _clean_slack_url(slack.get("webhook_url"))
        if url:
            out["slack"] = {"webhook_url": url}
    pushover = normalize_pushover(raw.get("pushover"))
    if pushover:
        out["pushover"] = pushover
    gotify = normalize_gotify(raw.get("gotify"))
    if gotify:
        out["gotify"] = gotify
    webhook = raw.get("webhook")
    if isinstance(webhook, dict):
        url = _clean_url(webhook.get("webhook_url"))
        if url:
            out["webhook"] = {"webhook_url": url}
    return out if any(channel in out for channel in NOTIFY_CHANNELS) else None


def notify_view(notify) -> dict:
    """What the browser may be told about an account's alert settings: whether
    each secret is set, never the secret."""
    notify = notify or {}
    ntfy, webhook, discord = notify.get("ntfy"), notify.get("webhook"), notify.get("discord")
    mail = notify.get("email")
    return {
        "level": notify.get("level", "critical"),
        "telegram": {"set": bool(notify.get("telegram"))},
        # The server, sender and recipients are not secrets (the browser needs them to
        # show the form); the login and password are, so only whether they are set.
        "email": {"set": True, "host": mail["host"], "port": mail["port"], "security": mail["security"],
                  "mail_from": mail["mail_from"], "mail_to": ", ".join(mail["mail_to"]),
                  "user_set": bool(mail.get("smtp_user")), "password_set": bool(mail.get("smtp_password"))} if mail else
                 {"set": False, "host": "", "port": SMTP_SECURITY["starttls"][1], "security": "starttls",
                  "mail_from": "", "mail_to": "", "user_set": False, "password_set": False},
        "security_options": [{"key": key, "label": label} for key, (label, _port) in SMTP_SECURITY.items()],  # a list: JSON objects get sorted
        "default_ports": {key: port for key, (_label, port) in SMTP_SECURITY.items()},
        "ntfy": {"server": ntfy["server"], "topic_set": True, "token_set": bool(ntfy.get("token"))} if ntfy else
                {"server": DEFAULT_NTFY_SERVER, "topic_set": False, "token_set": False},
        "discord": {"set": True, "host": urlsplit(discord["webhook_url"]).hostname or "discord.com"} if discord else
                   {"set": False, "host": ""},
        "slack": {"set": bool(notify.get("slack"))},
        "pushover": {"set": bool(notify.get("pushover"))},
        # A Gotify server is the person's own address and is not a secret (the token is, and is not sent).
        "gotify": {"set": True, "server": notify["gotify"]["server"]} if notify.get("gotify") else
                  {"set": False, "server": ""},
        "webhook": {"set": True, "host": urlsplit(webhook["webhook_url"]).netloc.rsplit("@", 1)[-1]} if webhook else
                   {"set": False, "host": ""},
    }


# --- Writing the messages (pure) ------------------------------------------------


def _message_for(event: dict) -> dict:
    incident = event["incident"]
    base = {
        "severity": incident["severity"], "kind": incident["kind"], "services": incident["services"],
        "since": incident["since"], "key": incident["key"],
    }
    if event["type"] == "opened":
        return {**base, "event": "opened", "title": incident["title"][:200],
                "message": (incident["detail"] or "Needs attention.")[:1000], "ended": None}
    return {**base, "event": "cleared", "title": f"Resolved: {incident['title']}"[:200],
            "message": f"Back to normal after {_between(incident['since'], event['ended'])}.", "ended": event["ended"]}


def _summary_message(kind: str, events: list) -> dict:
    titles = [e["incident"]["title"] for e in events]
    shown = "; ".join(titles[:6]) + (f"; and {len(titles) - 6} more" if len(titles) > 6 else "")
    services = []
    for e in events:
        for name in e["incident"]["services"]:
            if name not in services:
                services.append(name)
    opened = kind == "opened"
    return {
        "event": kind, "key": "", "kind": "summary", "services": services[:20], "since": min(e["incident"]["since"] for e in events),
        "ended": None if opened else max(e["ended"] for e in events),
        "severity": "critical" if opened and any(e["incident"]["severity"] == "critical" for e in events) else "warning",
        "title": f"{len(events)} new problems" if opened else f"{len(events)} problems resolved",
        "message": shown[:1000],
    }


def compose_messages(events: list, max_separate: int = NOTIFY_MAX_SEPARATE) -> list:
    """One message per event - unless a lot happen in one check (a switch or
    the router going down takes the whole stack with it), when they become one
    summary, so a person's phone isn't buried."""
    messages = []
    for kind in ("opened", "cleared"):
        group = [e for e in events if e["type"] == kind]
        if len(group) > max_separate:
            messages.append(_summary_message(kind, group))
        else:
            messages.extend(_message_for(e) for e in group)
    return messages


def events_for_level(events: list, level: str, hidden=()) -> list:
    """What an account on this alert level should hear. A warning the person
    has hidden on Home stays quiet here too; a critical one cannot be hidden."""
    hidden = set(hidden or [])
    keep = []
    for event in events:
        incident = event["incident"]
        if level != "all" and not incident.get("outage"):
            continue
        if incident["key"] in hidden and incident["severity"] != "critical":
            continue
        keep.append(event)
    return keep


# --- Sending ---------------------------------------------------------------------


def _post_json_reply(url: str, body: dict, headers=None):
    """POSTs JSON. Returns (ok, words, reply) where `words` is safe to show and
    to log - never the address (it may hold a secret) and never the reply - and
    `reply` is the response (None if there was none), for a caller that needs
    to read it."""
    _log_ctx.redact_path = True
    try:
        response = requests.post(url, json=body, headers=headers or {}, timeout=NOTIFY_TIMEOUT_SECONDS,
                                 allow_redirects=False)
    except requests.exceptions.Timeout:
        return False, "timed out", None
    except requests.exceptions.SSLError:
        return False, "the certificate was not accepted", None
    except requests.exceptions.ConnectionError:
        return False, "could not connect", None
    except requests.exceptions.RequestException as err:
        return False, err.__class__.__name__, None
    finally:
        _log_ctx.redact_path = False
    code = response.status_code
    if 300 <= code < 400:
        return False, f"redirected (HTTP {code}) - use the address it redirects to", response
    if code in (401, 403):
        return False, f"HTTP {code} - the server refused it (check the access token)", response
    if code >= 400:
        return False, f"HTTP {code}", response
    return True, f"HTTP {code}", response


def _post_json(url: str, body: dict, headers=None):
    ok, detail, _reply = _post_json_reply(url, body, headers)
    return ok, detail


def send_ntfy(settings: dict, message: dict):
    """ntfy's JSON publish: one POST to the server's root with the topic in the body."""
    opened = message["event"] == "opened"
    if message["event"] == "test":
        priority, tags = 3, ["bell"]
    elif not opened:
        priority, tags = 3, ["white_check_mark"]
    elif message["severity"] == "critical":
        priority, tags = 4, ["rotating_light"]
    else:
        priority, tags = 3, ["warning"]
    body = {"topic": settings["topic"], "title": message["title"], "message": message["message"],
            "priority": priority, "tags": tags}
    headers = {"Authorization": f"Bearer {settings['token']}"} if settings.get("token") else {}
    return _post_json(settings["server"].rstrip("/") + "/", body, headers)


def send_discord(settings: dict, message: dict):
    """A Discord webhook message: one embed, coloured by severity, with the
    severity also written in its footer (colour is never the only signal).
    `allowed_mentions` is empty so a service called "@everyone" can never ping
    anyone. Discord answers 204 with no body."""
    event = message["event"]
    if event == "test":
        color, label = DISCORD_COLORS["test"], "Test"
    elif event == "cleared":
        color, label = DISCORD_COLORS["cleared"], "Resolved"
    elif message["severity"] == "critical":
        color, label = DISCORD_COLORS["critical"], "Critical"
    else:
        color, label = DISCORD_COLORS["warning"], "Warning"
    embed = {"title": message["title"][:256], "description": message["message"][:2000], "color": color,
             "footer": {"text": f"arrVantage \u00b7 {label}"}}
    stamp = message["ended"] if event == "cleared" and message.get("ended") else message.get("since")
    if stamp:
        embed["timestamp"] = stamp
    if message.get("services"):
        embed["fields"] = [{"name": "Services", "value": ", ".join(message["services"])[:1000], "inline": False}]
    body = {"username": "arrVantage", "allowed_mentions": {"parse": []}, "embeds": [embed]}
    ok, detail = _post_json(settings["webhook_url"], body)
    if not ok:  # _post_json's wording suits an access token; Discord's causes differ
        if detail.startswith("HTTP 404"):
            detail = "HTTP 404 - Discord says this webhook does not exist (it may have been deleted)"
        elif detail.startswith(("HTTP 401", "HTTP 403")):
            detail = detail.split(" - ")[0] + " - Discord refused it (check the webhook address)"
        elif detail == "HTTP 429":
            detail = "HTTP 429 - Discord is rate limiting this webhook"
        elif detail == "HTTP 400":
            detail = "HTTP 400 - Discord did not accept the message"
    return ok, detail


def _severity_label(message: dict) -> str:
    """The same word every destination uses: Test, Resolved, Critical or Warning."""
    if message["event"] == "test":
        return "Test"
    if message["event"] == "cleared":
        return "Resolved"
    return "Critical" if message["severity"] == "critical" else "Warning"


def _telegram_words(detail: str) -> str:
    """_post_json's wording suits an access token; Telegram's causes differ."""
    code = detail.split(" - ")[0]
    return {
        "HTTP 400": "HTTP 400 - Telegram could not send it (check the chat ID, and that you have sent your bot a message "
                    "or added it to the group or channel)",
        "HTTP 401": "HTTP 401 - Telegram says the bot token is wrong",
        "HTTP 403": "HTTP 403 - the bot was blocked, or removed from that chat",
        "HTTP 404": "HTTP 404 - Telegram does not know this bot token (check it for typing mistakes)",
        "HTTP 409": "HTTP 409 - this bot has a webhook set, which stops it being used this way",
        "HTTP 429": "HTTP 429 - Telegram is rate limiting this bot",
    }.get(code, detail)


def send_telegram(settings: dict, message: dict):
    """A Telegram bot message (sendMessage): the title in bold, then the text, then
    the severity and services in italics. HTML mode with everything escaped, so a
    service name containing < or & is shown as written. The bot token sits in the
    address's path, which _post_json keeps out of the logs."""
    lines = [f"<b>{html.escape(message['title'][:300], quote=False)}</b>", html.escape(message["message"][:2500], quote=False)]
    tail = _severity_label(message)
    if message.get("services"):
        tail += " · " + ", ".join(message["services"])[:600]
    lines += ["", f"<i>{html.escape(tail, quote=False)}</i>"]
    body = {"chat_id": settings["chat_id"], "text": "\n".join(lines), "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True}}
    ok, detail = _post_json(f"{TELEGRAM_API}/bot{settings['bot_token']}/sendMessage", body)
    return ok, (detail if ok else _telegram_words(detail))


def find_telegram_chats(token: str):
    """Asks Telegram which chats have written to this bot lately (getUpdates), so
    the person can pick one instead of digging a chat id out of a web page.
    Returns (True, [{"id", "label"}]) or (False, words)."""
    ok, detail, reply = _post_json_reply(f"{TELEGRAM_API}/bot{token}/getUpdates", {"limit": 100})
    if not ok:
        return False, _telegram_words(detail)
    try:
        updates = reply.json().get("result") or []
    except ValueError:
        return False, "Telegram's answer could not be read"
    seen, chats = set(), []
    for update in reversed(updates if isinstance(updates, list) else []):  # newest first
        if not isinstance(update, dict):
            continue
        for key in ("message", "edited_message", "channel_post", "edited_channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat") if isinstance(update.get(key), dict) else None
            if not isinstance(chat, dict) or not isinstance(chat.get("id"), int) or isinstance(chat.get("id"), bool):
                continue
            if chat["id"] in seen:
                continue
            seen.add(chat["id"])
            kind = chat.get("type") if chat.get("type") in ("private", "group", "supergroup", "channel") else "chat"
            if kind == "private":
                name = " ".join(str(chat.get(p)) for p in ("first_name", "last_name") if chat.get(p)) or str(chat.get("username") or "")
            else:
                name = str(chat.get("title") or chat.get("username") or "")
            label = (name[:60] or "Unnamed") + f" ({'private chat' if kind == 'private' else kind})"
            chats.append({"id": str(chat["id"]), "label": label})
            if len(chats) >= TELEGRAM_MAX_CHATS:
                return True, chats
    return True, chats


def _one_line(text: str, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def build_email(settings: dict, message: dict) -> email.message.EmailMessage:
    """The alert as a plain-text email. Pure, so it is easy to check."""
    label, title = _severity_label(message), _one_line(message["title"], 200)
    if message["event"] == "test":
        subject = title
    elif message["event"] == "cleared":
        subject = f"[arrVantage] {title}"
    else:
        subject = f"[arrVantage] {label}: {title}"
    body = [message["title"][:300], "", message["message"][:3000], "", f"Severity: {label}"]
    if message.get("services"):
        body.append("Services: " + ", ".join(message["services"])[:1000])
    if message.get("since"):
        body.append(f"Started: {message['since']} (UTC)")
    if message.get("ended"):
        body.append(f"Ended: {message['ended']} (UTC)")
    body += ["", "--", "Sent by arrVantage, because you set up email alerts in Settings."]
    mail = email.message.EmailMessage()
    mail["Subject"] = subject
    mail["From"] = settings["mail_from"]
    mail["To"] = ", ".join(settings["mail_to"])
    mail["Date"] = email.utils.formatdate(localtime=False)
    mail["Message-ID"] = email.utils.make_msgid(domain=settings["mail_from"].rsplit("@", 1)[-1])
    mail["Auto-Submitted"] = "auto-generated"
    mail.set_content("\n".join(body))
    return mail


def _smtp_words(err: Exception, stage: str = "") -> str:
    """What went wrong with a mail server, in words that carry no address,
    login or server reply. `stage` says which step it was on, so "closed the
    connection" points at something: connecting, saying hello, starting
    encryption, logging in, or sending the message."""
    at = f" (while {stage})" if stage else ""
    if isinstance(err, smtplib.SMTPAuthenticationError):
        return "the mail server refused the login (check the user name and password; many providers need an app password)"
    if isinstance(err, smtplib.SMTPRecipientsRefused):
        return "the mail server refused the recipient address"
    if isinstance(err, smtplib.SMTPSenderRefused):
        return "the mail server refused the From address (it often must belong to the account you log in with)"
    if isinstance(err, smtplib.SMTPNotSupportedError):
        return "the mail server does not offer the security or login you chose (try another security option or port)"
    if isinstance(err, smtplib.SMTPServerDisconnected):
        if stage == "logging in":
            return "the mail server hung up while logging in (usually the login or app password was refused: check both)"
        return f"the mail server closed the connection{at} (check the security option and port)"
    if isinstance(err, smtplib.SMTPResponseException):
        return f"the mail server answered {err.smtp_code}{at}"
    if isinstance(err, smtplib.SMTPException):
        return f"{err.__class__.__name__}{at}"
    if isinstance(err, ssl.SSLCertVerificationError):
        return "the mail server's certificate was not accepted"
    if isinstance(err, ssl.SSLError):
        return f"could not set up a secure connection{at} (check the security option and port)"
    if isinstance(err, (TimeoutError, socket.timeout)):
        return f"timed out{at}"
    if isinstance(err, socket.gaierror):
        return "could not find the mail server (check its name)"
    if isinstance(err, UnicodeError):
        return "the user name or password has characters the mail login cannot use"
    return f"could not connect{at}"


def _smtp_login_once(server, user: str, password: str) -> None:
    """Logs in with ONE method. smtplib's own login() goes on to the next method
    after a refusal, and Gmail hangs up on that second attempt - which turned a
    plain "wrong password" (535) into "the mail server closed the connection"."""
    server.user, server.password = user, password
    server.ehlo_or_helo_if_needed()
    if not server.has_extn("auth"):
        raise smtplib.SMTPNotSupportedError("SMTP AUTH extension not supported by server.")
    offered = server.esmtp_features["auth"].split()
    for method in ("CRAM-MD5", "PLAIN", "LOGIN"):
        if method in offered:
            server.auth(method, getattr(server, "auth_" + method.lower().replace("-", "_")), initial_response_ok=True)
            return
    raise smtplib.SMTPException("No suitable authentication method found.")


def send_email(settings: dict, message: dict):
    mail = build_email(settings, message)
    context = ssl.create_default_context()
    stage = "connecting"
    try:
        local_name = socket.gethostname() or "localhost"  # not getfqdn(): it can stall on DNS
        if settings["security"] == "ssl":
            server = smtplib.SMTP_SSL(settings["host"], settings["port"], local_hostname=local_name,
                                      timeout=NOTIFY_TIMEOUT_SECONDS, context=context)
        else:
            server = smtplib.SMTP(settings["host"], settings["port"], local_hostname=local_name,
                                  timeout=NOTIFY_TIMEOUT_SECONDS)
        with server:
            stage = "saying hello"
            server.ehlo()
            if settings["security"] == "starttls":
                stage = "starting encryption"
                server.starttls(context=context)
                server.ehlo()
            if settings.get("smtp_user"):
                stage = "logging in"
                _smtp_login_once(server, settings["smtp_user"], settings.get("smtp_password", ""))
            stage = "sending the message"
            server.send_message(mail, from_addr=settings["mail_from"], to_addrs=settings["mail_to"])
    except (OSError, UnicodeError) as err:  # smtplib's errors and sockets' are OSErrors
        return False, _smtp_words(err, stage)
    return True, "sent"


def _tail_line(message: dict, limit: int = 600) -> str:
    """'Critical · Radarr, Sonarr' - the severity in words (never colour alone) and the services."""
    tail = _severity_label(message)
    if message.get("services"):
        tail += " · " + ", ".join(message["services"])[:limit]
    return tail


def _slack_escape(text: str) -> str:
    """Slack reads & < > as markup (<!channel> is how a message pings everyone), so they are
    written as entities and a service named '@channel' or '<!here>' cannot ping anyone."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_words(detail: str, reply) -> str:
    """Slack answers an error with a short code as plain text. Only a code from a fixed
    list is used (anything else in a reply is never shown or logged)."""
    code = detail.split(" - ")[0]
    body = (reply.text or "").strip()[:60] if reply is not None else ""
    if body in ("channel_not_found", "channel_is_archived", "posting_to_general_channel_denied"):
        return f"{code} - Slack says the channel is gone, archived or not allowed ({body})"
    if body in ("no_service", "no_service_id", "no_team", "no_active_hooks", "team_disabled") or code == "HTTP 404":
        return f"{code} - Slack says this webhook does not exist or is switched off (check the address)"
    if body in ("invalid_payload", "no_text") or code == "HTTP 400":
        return f"{code} - Slack did not accept the message"
    if body in ("action_prohibited", "invalid_token") or code in ("HTTP 401", "HTTP 403"):
        return f"{code} - Slack refused it (a workspace admin may have restricted this webhook)"
    if code == "HTTP 429":
        return "HTTP 429 - Slack is rate limiting this webhook"
    return detail


def send_slack(settings: dict, message: dict):
    """A Slack incoming-webhook message: one `text` field in Slack's mrkdwn, the title in
    bold, then the text, then the severity and services in italics. Everything is escaped
    first. Slack answers 200 with the text `ok`."""
    text = "\n".join([f"*{_slack_escape(message['title'][:300])}*", _slack_escape(message["message"][:2500]),
                      f"_{_slack_escape(_tail_line(message))}_"])
    ok, detail, reply = _post_json_reply(settings["webhook_url"], {"text": text})
    return ok, (detail if ok else _slack_words(detail, reply))


def _pushover_words(detail: str, reply) -> str:
    """Pushover's 4xx answers carry an `errors` list; it is only read to tell the two
    commonest mistakes apart, never shown."""
    code = detail.split(" - ")[0]
    errors = ""
    if reply is not None:
        try:
            listed = reply.json().get("errors")
            errors = " ".join(str(e) for e in listed).lower() if isinstance(listed, list) else ""
        except (ValueError, AttributeError):
            pass
    if code == "HTTP 429":
        return "HTTP 429 - this Pushover app has reached its monthly message limit"
    if code.startswith("HTTP 4") and "token" in errors:
        return f"{code} - Pushover says the application token is wrong"
    if code.startswith("HTTP 4") and "user" in errors:
        return f"{code} - Pushover says the user key is wrong, or that user has no active device"
    if code.startswith("HTTP 4"):
        return f"{code} - Pushover did not accept it (check the user key and application token)"
    return detail


def send_pushover(settings: dict, message: dict):
    """A Pushover message: title, text (with the severity and services on the last line) and a
    priority - Critical is high (1, which can get through quiet hours), a warning normal (0), a
    recovery low (-1, no sound). Priority 2 is never used (it repeats until acknowledged and
    needs more fields). The time shown is when the problem began (or ended)."""
    event = message["event"]
    priority = 0 if event == "test" else -1 if event == "cleared" else 1 if message["severity"] == "critical" else 0
    tail = _tail_line(message, 300)
    text = message["message"][: max(0, 1024 - len(tail) - 1)] + "\n" + tail
    body = {"token": settings["app_token"], "user": settings["user_key"], "title": message["title"][:250],
            "message": text[:1024], "priority": priority}
    stamp = _parse_time(message["ended"] if event == "cleared" and message.get("ended") else message.get("since"))
    if stamp:
        body["timestamp"] = int(stamp.timestamp())
    ok, detail, reply = _post_json_reply(PUSHOVER_API, body)
    if ok:  # Pushover answers 200 with {"status": 1}; anything else is a failure
        try:
            ok = reply.json().get("status") == 1
        except (ValueError, AttributeError):
            ok = False
        if not ok:
            return False, "Pushover did not confirm the message"
    return ok, (detail if ok else _pushover_words(detail, reply))


def _gotify_words(detail: str) -> str:
    code = detail.split(" - ")[0]
    return {
        "HTTP 400": "HTTP 400 - Gotify did not accept the message",
        "HTTP 401": "HTTP 401 - Gotify says the application token is wrong",
        "HTTP 403": "HTTP 403 - Gotify refused it (check the application token)",
        "HTTP 404": "HTTP 404 - nothing answered at that address (check the server address, and any path it needs)",
    }.get(code, detail)


def send_gotify(settings: dict, message: dict):
    """A Gotify push (POST <server>/message, JSON, the application token in X-Gotify-Key):
    title, the text with the severity and services on a last line, a priority (Gotify's own
    guide could not be read here; its examples use 2, 5 and 10 and a higher number is louder,
    so Critical is 8, a warning or test 5, a recovery 3) and `text/plain` so a service name
    is never read as markdown."""
    event = message["event"]
    priority = 5 if event == "test" else 3 if event == "cleared" else 8 if message["severity"] == "critical" else 5
    text = message["message"][:2500] + "\n\n" + _tail_line(message)
    body = {"title": message["title"][:200], "message": text, "priority": priority,
            "extras": {"client::display": {"contentType": "text/plain"}}}
    ok, detail = _post_json(f"{settings['server']}/message", body, {"X-Gotify-Key": settings["token"]})
    return ok, (detail if ok else _gotify_words(detail))


def send_webhook(settings: dict, message: dict):
    body = {
        "app": "arrVantage", "version": resolve_app_version(), "event": message["event"],
        "title": message["title"], "message": message["message"], "severity": message["severity"],
        "kind": message["kind"], "services": message["services"], "since": message["since"], "ended": message["ended"],
    }
    return _post_json(settings["webhook_url"], body)


def send_message(notify: dict, message: dict) -> list:
    """Sends one message to every destination an account has. Returns
    [(channel, ok, detail)]; a failure is logged once in a while, without the address."""
    results = []
    senders = {"ntfy": send_ntfy, "discord": send_discord, "telegram": send_telegram, "email": send_email,
               "slack": send_slack, "pushover": send_pushover, "gotify": send_gotify, "webhook": send_webhook}
    for channel in NOTIFY_CHANNELS:
        sender = senders[channel]
        if notify.get(channel):
            ok, detail = sender(notify[channel], message)
            if not ok:
                log_problem(("alert", channel, detail), "Could not send an alert by %s: %s", channel, detail)
            results.append((channel, ok, detail))
    return results


def deliver_events(events: list) -> None:
    for user in list(USERS):
        notify = normalize_notify(user.get("notify")) if user.get("role") == "admin" else None
        if not notify:
            continue
        mine = events_for_level(events, notify["level"], user.get("hidden_attention"))
        for message in compose_messages(mine):
            send_message(notify, message)


# --- The check itself --------------------------------------------------------------


def monitor_once(deliver: bool = True) -> list:
    """One background check: run Needs attention, update the history, send the
    alerts that are due. Returns the events (for tests)."""
    data = gather_attention()
    now = _iso_now()
    with _HISTORY_LOCK:
        before = _history_state()
        after, events = track_incidents(before, data, now)
        after["history"] = prune_history(after["history"], now)
        _HISTORY["state"] = after
        if after != before:
            save_history(after)
    _MONITOR["last_check"] = now
    _MONITOR["checks"] += 1
    if events and deliver:
        deliver_events(events)
    return events


def _monitor_loop(interval: int) -> None:
    time.sleep(MONITOR_FIRST_DELAY)
    while True:
        try:
            monitor_once()
        except Exception as err:  # noqa: BLE001 - the monitor must outlive any one bad check
            log_problem(("monitor", err.__class__.__name__), "A background check failed: %s: %s",
                        err.__class__.__name__, scrub_log_text(err))
        time.sleep(interval)


def start_monitor() -> bool:
    """Starts the background thread (once). False when it is switched off or already running."""
    interval = resolve_monitor_interval(os.environ.get("MONITOR_INTERVAL"))
    _MONITOR["interval"] = interval
    thread = _MONITOR["thread"]
    if interval == 0 or (thread is not None and thread.is_alive()):
        return False
    thread = threading.Thread(target=_monitor_loop, args=(interval,), name="arrv-monitor", daemon=True)
    _MONITOR["thread"] = thread
    thread.start()
    return True


def monitor_status() -> dict:
    interval = _MONITOR["interval"]
    if interval is None:
        interval = resolve_monitor_interval(os.environ.get("MONITOR_INTERVAL"))
    return {"enabled": interval > 0, "interval": interval, "last_check": _MONITOR["last_check"], "checks": _MONITOR["checks"]}


@app.route("/api/history")
@login_required
def api_history():
    """The recorded outages, newest first; `end: null` means still going."""
    with _HISTORY_LOCK:
        entries = copy.deepcopy(_history_state()["history"])
    entries.reverse()
    return jsonify({"ok": True, "now": _iso_now(), "monitor": monitor_status(), "history": entries[:HISTORY_API_LIMIT]})


# --- Alert settings (administrators only; each sets their own) -----------------


@app.route("/api/notifications")
@login_required
@admin_required
def api_notifications():
    return jsonify({"ok": True, "levels": NOTIFY_LEVELS, "notify": notify_view(normalize_notify(current_user().get("notify"))),
                    "monitor": monitor_status()})


@app.route("/api/notifications", methods=["POST"])
@login_required
@admin_required
def api_notifications_save():
    """
    {"level": ..., "ntfy": {"server", "topic", "token", "remove"}, "discord": {"webhook_url", "remove"},
     "telegram": {"bot_token", "chat_id", "remove"},
     "email": {"host", "port", "security", "smtp_user", "smtp_password", "clear_login", "mail_from", "mail_to", "remove"},
     "slack": {"webhook_url", "remove"}, "pushover": {"user_key", "app_token", "remove"},
     "gotify": {"server", "token", "remove"},
     "webhook": {"webhook_url", "remove"}}
    A secret left blank keeps what is saved (the browser is never sent them);
    "remove": true clears a destination.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "errors": ["Expected a JSON object."]}), 400
    user = current_user()
    current = normalize_notify(user.get("notify")) or {}
    errors = []

    level = payload.get("level", current.get("level", "critical"))
    if level not in NOTIFY_LEVELS:
        errors.append("Choose what to be alerted about.")

    new = {"level": level if level in NOTIFY_LEVELS else "critical"}

    ntfy_in = payload.get("ntfy") if isinstance(payload.get("ntfy"), dict) else {}
    old = current.get("ntfy")
    if ntfy_in.get("remove") is True:
        pass
    else:
        server_in = ntfy_in.get("server").strip() if isinstance(ntfy_in.get("server"), str) else ""
        topic_in = ntfy_in.get("topic").strip() if isinstance(ntfy_in.get("topic"), str) else ""
        token_in = ntfy_in.get("token").strip() if isinstance(ntfy_in.get("token"), str) else ""
        if server_in and not _clean_url(server_in, 300):
            errors.append("The ntfy server address must start with http:// or https://.")
        if topic_in and not _NTFY_TOPIC_RE.match(topic_in):
            errors.append("An ntfy topic can only use letters, numbers, - and _ (up to 64 characters).")
        topic = topic_in or (old or {}).get("topic", "")
        if topic and not errors:
            entry = {"server": _clean_url(server_in, 300) or (old or {}).get("server") or DEFAULT_NTFY_SERVER, "topic": topic}
            token = token_in or ("" if ntfy_in.get("clear_token") is True else (old or {}).get("token", ""))
            if token:
                entry["token"] = token[:200]
            new["ntfy"] = entry
        elif token_in and not topic:
            errors.append("Enter the ntfy topic as well as the access token.")

    discord_in = payload.get("discord") if isinstance(payload.get("discord"), dict) else {}
    if discord_in.get("remove") is not True:
        url_in = discord_in.get("webhook_url").strip() if isinstance(discord_in.get("webhook_url"), str) else ""
        if url_in and not _clean_discord_url(url_in):
            errors.append("The Discord webhook address must look like https://discord.com/api/webhooks/... "
                          "(in Discord: channel settings, Integrations, Webhooks, Copy Webhook URL).")
        elif url_in:
            new["discord"] = {"webhook_url": _clean_discord_url(url_in)}
        elif current.get("discord"):
            new["discord"] = current["discord"]

    telegram_in = payload.get("telegram") if isinstance(payload.get("telegram"), dict) else {}
    if telegram_in.get("remove") is not True:
        token_in = telegram_in.get("bot_token").strip() if isinstance(telegram_in.get("bot_token"), str) else ""
        chat_in = telegram_in.get("chat_id").strip() if isinstance(telegram_in.get("chat_id"), str) else ""
        old_tg = current.get("telegram") or {}
        if token_in and not _clean_telegram_token(token_in):
            errors.append("That does not look like a Telegram bot token. It looks like 123456789:AA... "
                          "(BotFather sends it when you create the bot).")
        if chat_in and not _clean_telegram_chat(chat_in):
            errors.append("The Telegram chat ID is a number such as 123456789 (a group's starts with -), "
                          "or a public channel's @name. Use Find my chat to get it.")
        token, chat = token_in or old_tg.get("bot_token", ""), chat_in or old_tg.get("chat_id", "")
        if (token_in or chat_in) and not (token and chat):
            errors.append("Telegram needs both the bot token and the chat ID.")
        elif token and chat and not errors:
            new["telegram"] = {"bot_token": token, "chat_id": chat}
        elif old_tg and not (token_in or chat_in):
            new["telegram"] = old_tg

    mail_in = payload.get("email") if isinstance(payload.get("email"), dict) else {}
    if mail_in.get("remove") is not True:
        old_mail = current.get("email") or {}
        text = lambda key: mail_in.get(key).strip() if isinstance(mail_in.get(key), str) else ""  # noqa: E731
        touched = any(text(k) for k in ("host", "smtp_user", "smtp_password", "mail_from", "mail_to"))
        if touched or old_mail:
            host = text("host").lower() or old_mail.get("host", "")
            security = mail_in.get("security") if mail_in.get("security") in SMTP_SECURITY else old_mail.get("security", "starttls")
            port_text = str(mail_in.get("port")).strip() if mail_in.get("port") not in (None, "") else ""
            login = text("smtp_user") or ("" if mail_in.get("clear_login") is True else old_mail.get("smtp_user", ""))
            password = text("smtp_password") or ("" if mail_in.get("clear_login") is True else old_mail.get("smtp_password", ""))
            sender = text("mail_from") or old_mail.get("mail_from", "")
            recipients_text = mail_in.get("mail_to") if "mail_to" in mail_in else old_mail.get("mail_to", [])
            good, bad = split_mail_recipients(recipients_text)
            mail_errors = []
            if not _clean_mail_host(host):
                mail_errors.append("Enter the mail server's name, such as smtp.gmail.com (no http:// and no port).")
            if port_text and not (port_text.isdigit() and 1 <= int(port_text) <= 65535):
                mail_errors.append("The mail server port is a number from 1 to 65535.")
            if sender and not _clean_mail_address(sender):
                mail_errors.append("The From address is not a valid email address.")
            if not sender and not _clean_mail_address(login):
                mail_errors.append("Enter the From address (the login is not an email address, so it cannot be used).")
            if bad:
                mail_errors.append("Not an email address: " + ", ".join(bad[:3]) + ".")
            if not good and not bad:
                mail_errors.append("Enter the address to send alerts to.")
            if len(good) > MAIL_MAX_RECIPIENTS:
                mail_errors.append(f"Use at most {MAIL_MAX_RECIPIENTS} recipient addresses.")
            if password and not login:
                mail_errors.append("Enter the login name as well as the password.")
            if mail_errors:
                errors.extend(mail_errors)
            else:
                candidate = {"host": host, "port": port_text or SMTP_SECURITY[security][1], "security": security,
                             "mail_from": sender, "mail_to": good, "smtp_user": login, "smtp_password": password}
                new["email"] = normalize_email(candidate)

    slack_in = payload.get("slack") if isinstance(payload.get("slack"), dict) else {}
    if slack_in.get("remove") is not True:
        url_in = slack_in.get("webhook_url").strip() if isinstance(slack_in.get("webhook_url"), str) else ""
        if url_in and not _clean_slack_url(url_in):
            errors.append("The Slack webhook address must look like https://hooks.slack.com/services/... "
                          "(in Slack: create an app with Incoming Webhooks, add a webhook to a channel, copy its URL).")
        elif url_in:
            new["slack"] = {"webhook_url": _clean_slack_url(url_in)}
        elif current.get("slack"):
            new["slack"] = current["slack"]

    pushover_in = payload.get("pushover") if isinstance(payload.get("pushover"), dict) else {}
    if pushover_in.get("remove") is not True:
        user_in = pushover_in.get("user_key").strip() if isinstance(pushover_in.get("user_key"), str) else ""
        app_in = pushover_in.get("app_token").strip() if isinstance(pushover_in.get("app_token"), str) else ""
        old_po = current.get("pushover") or {}
        if user_in and not _clean_pushover_key(user_in):
            errors.append("The Pushover user key is 30 letters and numbers (shown on the Pushover home page).")
        if app_in and not _clean_pushover_key(app_in):
            errors.append("The Pushover application token is 30 letters and numbers "
                          "(create an application on pushover.net to get one).")
        user_key, app_token = user_in or old_po.get("user_key", ""), app_in or old_po.get("app_token", "")
        if (user_in or app_in) and not (user_key and app_token):
            errors.append("Pushover needs both the user key and the application token.")
        elif user_key and app_token and not errors:
            new["pushover"] = {"user_key": user_key, "app_token": app_token}
        elif old_po and not (user_in or app_in):
            new["pushover"] = old_po

    gotify_in = payload.get("gotify") if isinstance(payload.get("gotify"), dict) else {}
    if gotify_in.get("remove") is not True:
        server_in = gotify_in.get("server").strip() if isinstance(gotify_in.get("server"), str) else ""
        token_in = gotify_in.get("token").strip() if isinstance(gotify_in.get("token"), str) else ""
        old_go = current.get("gotify") or {}
        if server_in and not _clean_gotify_server(server_in):
            errors.append("The Gotify server address must start with http:// or https:// and have no login, "
                          "? or # in it, for example https://gotify.example.com.")
        if token_in and not _clean_gotify_token(token_in):
            errors.append("That does not look like a Gotify application token (letters, numbers, - _ . ~).")
        server, token = _clean_gotify_server(server_in) or old_go.get("server", ""), token_in or old_go.get("token", "")
        if (server_in or token_in) and not (server and token):
            errors.append("Gotify needs both the server address and an application token.")
        elif server and token and not errors:
            new["gotify"] = {"server": server, "token": token}
        elif old_go and not (server_in or token_in):
            new["gotify"] = old_go

    hook_in = payload.get("webhook") if isinstance(payload.get("webhook"), dict) else {}
    if hook_in.get("remove") is not True:
        url_in = hook_in.get("webhook_url").strip() if isinstance(hook_in.get("webhook_url"), str) else ""
        if url_in and not _clean_url(url_in):
            errors.append("The webhook address must start with http:// or https://.")
        elif url_in:
            new["webhook"] = {"webhook_url": _clean_url(url_in)}
        elif current.get("webhook"):
            new["webhook"] = current["webhook"]

    if errors:
        return jsonify({"ok": False, "errors": errors}), 400
    cleaned = normalize_notify(new)
    if cleaned:
        user["notify"] = cleaned
    else:
        user.pop("notify", None)
    save_config(CONFIG)
    return jsonify({"ok": True, "notify": notify_view(cleaned)})


@app.route("/api/notifications/test", methods=["POST"])
@login_required
@admin_required
def api_notifications_test():
    """Sends a test message to the signed-in administrator's saved destinations."""
    notify = normalize_notify(current_user().get("notify"))
    if not notify:
        return jsonify({"ok": False, "error": "Save a destination first, then send the test."}), 400
    message = {"event": "test", "title": "arrVantage test alert", "message": "If you can read this, alerts reach you.",
               "severity": "warning", "kind": "test", "services": [], "since": _iso_now(), "ended": None, "key": ""}
    results = [{"channel": channel, "ok": ok, "detail": detail} for channel, ok, detail in send_message(notify, message)]
    mail = notify.get("email")
    for result in results:  # which mail server and mode the test really used (none of it is secret)
        if result["channel"] == "email" and mail:
            result["via"] = f"{mail['host']}:{mail['port']}, {mail['security']}"
    return jsonify({"ok": all(r["ok"] for r in results), "results": results})


@app.route("/api/notifications/telegram-chats", methods=["POST"])
@login_required
@admin_required
def api_notifications_telegram_chats():
    """Lists the chats that have written to a Telegram bot, so the person can pick
    one. {"bot_token"}; blank uses the saved one (the browser is never sent it)."""
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    typed = payload.get("bot_token").strip() if isinstance(payload.get("bot_token"), str) else ""
    saved = (normalize_notify(current_user().get("notify")) or {}).get("telegram") or {}
    token = _clean_telegram_token(typed) if typed else saved.get("bot_token", "")
    if not token:
        return jsonify({"ok": False, "error": "Enter the bot token first." if not typed else
                        "That does not look like a Telegram bot token."}), 400
    ok, result = find_telegram_chats(token)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "chats": result})


@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    if request.method == "POST":
        new_config = {
            "instances": dedupe_names(parse_indexed_entries(
                request.form, "instance", ["kind", "url", "api_key", "color", "enabled"])),
            "media_servers": dedupe_names(parse_indexed_entries(
                request.form, "mediaserver", ["type", "url", "credential", "color", "enabled"])),
            "requesters": dedupe_names(parse_indexed_entries(
                request.form, "requester", ["type", "url", "credential", "enabled"])),
            "downloaders": dedupe_names(parse_indexed_entries(
                request.form, "downloader", ["type", "url", "api_key", "color", "enabled"])),
            "indexers": dedupe_names(parse_indexed_entries(
                request.form, "indexerserver", ["type", "url", "api_key", "color", "enabled"])),
            "transcoders": dedupe_names(parse_indexed_entries(
                request.form, "transcoder", ["type", "url", "api_key", "color", "enabled"])),
            "users": parse_users(request.form, CONFIG.get("users", [])),
            "tmdb": {"api_key": request.form.get("tmdb_api_key", "").strip()},
            "tvdb": {
                "api_key": request.form.get("tvdb_api_key", "").strip(),
                "pin": request.form.get("tvdb_pin", "").strip(),
            },
            "fanart": {"api_key": request.form.get("fanart_api_key", "").strip()},
            "theaudiodb": {"api_key": request.form.get("theaudiodb_api_key", "123").strip() or "123"},
            "tautulli": {
                "url": request.form.get("tautulli_url", "").strip(),
                "api_key": request.form.get("tautulli_api_key", "").strip(),
            },
            "tracearr": {
                "url": request.form.get("tracearr_url", "").strip(),
                "api_key": request.form.get("tracearr_api_key", "").strip(),
            },
            "secret_key": CONFIG["secret_key"],  # never edited via the form - carry it forward
        }
        save_config(new_config)

        # Update the live in-memory config so the change applies
        # immediately, without needing to restart the app.
        apply_config(new_config)

        # Only the admin can reach this route, so keep their session
        # pointed at their account even if they just renamed themselves.
        admin_account = next((u for u in USERS if u["role"] == "admin"), None)
        if admin_account:
            session["username"] = admin_account["username"]

        return redirect(url_for("settings", saved="1"))

    return render_template(
        "settings.html",
        instances=CONFIG.get("instances", []),
        media_servers=CONFIG.get("media_servers", []),
        requesters=CONFIG.get("requesters", []),
        downloaders=CONFIG.get("downloaders", []),
        indexers=CONFIG.get("indexers", []),
        transcoders=CONFIG.get("transcoders", []),
        # An account's alert destinations are secrets and are edited in their
        # own box (Alerts), never through this page's data.
        users=[{k: v for k, v in u.items() if k != "notify"} for u in CONFIG.get("users", [])],
        tmdb=CONFIG.get("tmdb", {"api_key": ""}),
        tvdb=CONFIG.get("tvdb", {"api_key": "", "pin": ""}),
        fanart=CONFIG.get("fanart", {"api_key": ""}),
        theaudiodb=CONFIG.get("theaudiodb", {"api_key": "123"}),
        tautulli=CONFIG.get("tautulli", {"url": "", "api_key": ""}),
        tracearr=CONFIG.get("tracearr", {"url": "", "api_key": ""}),
        kind_options=KIND_META,
        media_server_type_options=MEDIA_SERVER_TYPES,
        requester_type_options=REQUESTER_TYPES,
        downloader_type_options=DOWNLOADER_TYPES,
        indexer_type_options=INDEXER_SERVICE_TYPES,
        transcoder_type_options=TRANSCODER_TYPES,
        saved=request.args.get("saved") == "1",
        csrf_token=ensure_csrf_token(),
    )


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------


@app.route("/")
@login_required
def index():
    user = current_user()
    perms = user_permissions(user)
    is_admin = user.get("role") == "admin"

    # Only what the sidebar needs, all of it straight from config - no
    # network calls. Each instance's panels are fetched on demand from
    # /fragment/instance/<name>, and the health/queue badges are filled in
    # from /api/attention once the page is already on screen.
    instances = [
        {
            "name": name,
            "logo_url": INSTANCE_ICONS.get(config["library_kind"], ""),
            "color": config.get("color", "#5b8cff"),
            "library_kind": config["library_kind"],
            "url": config["url"],
            "seerr_media_type": instance_seerr_type(config, perms),
        }
        for name, config in APPS.items()
        if config.get("enabled", True)
    ]

    downloaders_for_template = [
        {
            "name": d["name"],
            "type": d["type"],
            "url": d["url"],
            "color": d.get("color", "#5b8cff"),
            "logo_url": DOWNLOADER_ICONS.get(d["type"], ""),
        }
        for d in DOWNLOADERS if d.get("enabled", True)
    ]

    indexers_for_template = [
        {
            "name": i["name"],
            "type": i["type"],
            "url": i["url"],
            "color": i.get("color", "#5b8cff"),
            "logo_url": INDEXER_ICONS.get(i["type"], ""),
        }
        for i in INDEXERS if i.get("enabled", True)
    ]

    transcoders_for_template = [
        {
            "name": t["name"],
            "type": t["type"],
            "url": t["url"],
            "color": t.get("color", "#5b8cff"),
            "logo_url": TRANSCODER_ICONS.get(t["type"], ""),
        }
        for t in TRANSCODERS if t.get("enabled", True)
    ]

    media_servers_for_template = [
        {
            "name": s["name"],
            "type": s["type"],
            "url": s["url"],
            "open_url": get_media_server_open_url(s),
            "color": s.get("color", "#5b8cff"),
            "logo_url": MEDIA_SERVER_ICONS.get(s["type"], ""),
        }
        for s in MEDIA_SERVERS if s.get("enabled", True)
    ]

    return render_template(
        "index.html", instances=instances, downloaders=downloaders_for_template,
        indexer_services=indexers_for_template, media_server_list=media_servers_for_template,
        transcoders=transcoders_for_template,
        username=user["username"], is_admin=is_admin,
        can_request=perms["requesting"], can_search=perms["searching"],
        csrf_token=ensure_csrf_token(),
        home_layout=normalize_home_layout(user.get("home_layout")),
        app_version=resolve_app_version(),
    )


# gunicorn imports this module, so that is where the background monitor starts
# (the tests set ARRVANTAGE_NO_MONITOR so they never get a thread of their own).
if __name__ != "__main__" and os.environ.get("ARRVANTAGE_NO_MONITOR") != "1":
    start_monitor()

if __name__ == "__main__":
    # This only runs for local `python app.py` use - Docker's gunicorn
    # entrypoint imports the app object directly and never reaches here.
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    # Flask's reloader runs this file twice; only the process that serves
    # requests (WERKZEUG_RUN_MAIN) gets the monitor, or alerts would double.
    if not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_monitor()
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)