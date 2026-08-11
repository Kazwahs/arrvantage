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

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from urllib.parse import quote
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import json
import os
import re
import secrets
import requests

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
# CONFIG
# ---------------------------------------------------------------------------

# Built-in sidebar icons, bundled as static files rather than fetched from
# each instance - avoids depending on that instance's auth settings, and
# keeps the app self-contained for Docker.
INSTANCE_ICONS = {
    "movie": "/static/icons/movie.svg",
    "series": "/static/icons/series.svg",
    "artist": "/static/icons/artist.svg",
    "author": "/static/icons/author.svg",
}

# Structural facts about each *kind* of arr app - which endpoints it uses,
# what kind of library it holds, which Overseerr media type it maps to.
# Any number of named instances can share one of these kinds.
KIND_META = {
    "movie": {
        "queue_endpoint": "/api/v3/queue",
        "history_endpoint": "/api/v3/history",
        "library_endpoint": "/api/v3/movie",
        "seerr_media_type": "movie",
        "label": "Movies (Radarr-type)",
    },
    "series": {
        "queue_endpoint": "/api/v3/queue",
        "history_endpoint": "/api/v3/history",
        "library_endpoint": "/api/v3/series",
        "seerr_media_type": "tv",
        "label": "TV (Sonarr-type)",
    },
    "artist": {
        "queue_endpoint": "/api/v1/queue",
        "history_endpoint": "/api/v1/history",
        "library_endpoint": "/api/v1/artist",
        "seerr_media_type": None,
        "label": "Music (Lidarr-type)",
    },
    "author": {
        "queue_endpoint": "/api/v1/queue",
        "history_endpoint": "/api/v1/history",
        "library_endpoint": "/api/v1/author",
        "seerr_media_type": None,
        "label": "Books (Readarr-type)",
    },
}

# Media server types we can actually talk to correctly. Jellyfin and Emby
# share the same API (Jellyfin was forked from Emby), so one integration
# covers both.
MEDIA_SERVER_TYPES = {
    "plex": "Plex",
    "jellyfin": "Jellyfin",
    "emby": "Emby",
}

# Requester types we can actually talk to correctly. Jellyseerr is an
# API-compatible fork of Overseerr, so one integration covers both.
REQUESTER_TYPES = {
    "overseerr": "Overseerr / Jellyseerr",
}

# In Docker, set CONFIG_DIR to a mounted volume path (e.g. /app/data) so
# config.json - your instances, users, and API keys - survives image
# rebuilds. Left unset, it defaults to sitting next to this file, same
# as running the app directly with `python app.py`.
CONFIG_DIR = os.environ.get("CONFIG_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(CONFIG_DIR, exist_ok=True)  # in case a fresh volume mount is still empty
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

EMPTY_CONFIG = {
    "instances": [], "media_servers": [], "requesters": [], "users": [],
    "tmdb": {"api_key": ""}, "secret_key": "",
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # fall through to writing a fresh empty config below

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
            "seerr_media_type": meta["seerr_media_type"],
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
USERS = CONFIG.get("users", [])

# Flask needs a secret key to sign session cookies. Generate one on first
# run and persist it in config.json so logins survive app restarts.
if not CONFIG.get("secret_key"):
    CONFIG["secret_key"] = secrets.token_hex(32)
    save_config(CONFIG)
app.secret_key = CONFIG["secret_key"]

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
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if not USERS:
        return redirect(url_for("setup"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user(username)

        if user and check_password_hash(user["password_hash"], password):
            session["username"] = username
            return redirect(url_for("index"))
        error = "Incorrect username or password."

    return render_template("login.html", error=error)


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


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def fetch_queue(name: str, config: dict) -> dict:
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
        {"title": get_item_title(record), "time_left": get_time_left(record)}
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
    Prefer "remoteUrl" (points straight at the original source, like
    TMDB - loads with no auth needed) and fall back to the arr
    instance's own copy, with the API key appended as a query param
    since <img> tags can't send custom headers.
    """
    images = record.get("images") or []
    poster = next((img for img in images if img.get("coverType") == "poster"), None)
    if not poster:
        return ""

    if poster.get("remoteUrl"):
        return poster["remoteUrl"]

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
    cover_url = get_cover_url(record, config)

    if kind == "movie":
        title = record.get("title", "(unknown)")
        year = record.get("year", "")
        detail = ""
        title = f"{title} ({year})" if year else title
    elif kind == "series":
        title = record.get("title", "(unknown)")
        stats = record.get("statistics", {})
        detail = f"{stats.get('episodeFileCount', 0)}/{stats.get('episodeCount', 0)} episodes"
    elif kind == "artist":
        title = record.get("artistName", "(unknown)")
        stats = record.get("statistics", {})
        detail = f"{stats.get('trackFileCount', 0)}/{stats.get('trackCount', 0)} tracks"
    elif kind == "author":
        title = record.get("authorName", "(unknown)")
        stats = record.get("statistics", {})
        detail = f"{stats.get('bookFileCount', 0)}/{stats.get('bookCount', 0)} books"
    else:
        title = record.get("title", "(unknown)")
        detail = ""

    return {"title": title, "detail": detail, "status": status, "genre": genre,
            "cover_url": cover_url, "item_id": record.get("id"),
            "tmdb_id": record.get("tmdbId") if kind == "movie" else None,
            "tvdb_id": record.get("tvdbId") if kind == "series" else None}


def fetch_library(name: str, config: dict) -> dict:
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
    library_items.sort(key=lambda item: item["title"].lower())

    status_options = sorted({item["status"] for item in library_items})
    genre_options = sorted({g for item in library_items for g in item["genre"].split(", ") if item["genre"] != "Unknown"})

    return {
        "error": None,
        "library_items": library_items,
        "status_options": status_options,
        "genre_options": genre_options,
    }


def fetch_collections(config: dict, owned_tmdb_ids: set) -> list:
    """
    Radarr-only: fetches Movie Collections (e.g. "The Dark Knight
    Collection") and marks which of each collection's movies are
    already in your library vs missing entirely.

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
                "owned": m.get("tmdbId") in owned_tmdb_ids,
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
    # via a 404 on /api/v3/credit, so this isn't attempted at all.

    return {
        "overview": series.get("overview", "No description available."),
        "rating": get_rating_text(series.get("ratings", {})),
        "seasons": seasons,
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
    response = requests.get(
        f"{base}/api/v1/album", headers=headers,
        params={"artistId": artist_id}, timeout=10,
    )
    response.raise_for_status()
    albums = sorted(response.json(), key=lambda a: a.get("releaseDate", "") or "")
    return {
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
        {"track_number": t.get("trackNumber"), "title": t.get("title", "(untitled)")}
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
            "status": item.get("status", "unknown"),
            "requested_by": (item.get("requestedBy") or {}).get("displayName", "unknown"),
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
        headers = {"X-Emby-Token": server["credential"]}
        params = {
            "searchTerm": title, "IncludeItemTypes": item_type,
            "Recursive": "true", "Limit": 10, "api_key": server["credential"],
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
# Settings
# ---------------------------------------------------------------------------


@app.route("/api/settings/test", methods=["POST"])
def api_settings_test():
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
    category = body.get("category")       # "instance" | "media_server" | "requester" | "tmdb"
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

    if not url:
        return jsonify({"ok": False, "error": "URL is required"})

    try:
        if category == "instance":
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
                headers={"X-Emby-Token": credential}, timeout=8,
            )
        else:
            return jsonify({"ok": False, "error": "unknown settings type"})

        response.raise_for_status()
    except requests.exceptions.HTTPError:
        detail = response.text[:200] if response is not None else "no response body"
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

        users.append({
            "username": username, "password_hash": password_hash,
            "role": role, "permissions": permissions,
        })

    # Guarantee an admin always survives, even if the form somehow
    # didn't include one - avoids ever locking yourself out entirely.
    if not any(u["role"] == "admin" for u in users):
        original_admin = next((u for u in existing_users if u["role"] == "admin"), None)
        if original_admin:
            users.insert(0, original_admin)

    return users


@app.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    global CONFIG, APPS, MEDIA_SERVERS, REQUESTERS, USERS, TMDB

    if request.method == "POST":
        new_config = {
            "instances": dedupe_names(parse_indexed_entries(
                request.form, "instance", ["kind", "url", "api_key", "color", "enabled"])),
            "media_servers": dedupe_names(parse_indexed_entries(
                request.form, "mediaserver", ["type", "url", "credential", "enabled"])),
            "requesters": dedupe_names(parse_indexed_entries(
                request.form, "requester", ["type", "url", "credential", "enabled"])),
            "users": parse_users(request.form, CONFIG.get("users", [])),
            "tmdb": {"api_key": request.form.get("tmdb_api_key", "").strip()},
            "secret_key": CONFIG["secret_key"],  # never edited via the form - carry it forward
        }
        save_config(new_config)

        # Update the live in-memory config so the change applies
        # immediately, without needing to restart the app.
        CONFIG = new_config
        APPS = build_apps(CONFIG)
        MEDIA_SERVERS = CONFIG["media_servers"]
        REQUESTERS = CONFIG["requesters"]
        USERS = CONFIG["users"]
        TMDB = CONFIG["tmdb"]

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
        users=CONFIG.get("users", []),
        tmdb=CONFIG.get("tmdb", {"api_key": ""}),
        kind_options=KIND_META,
        media_server_type_options=MEDIA_SERVER_TYPES,
        requester_type_options=REQUESTER_TYPES,
        saved=request.args.get("saved") == "1",
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

    instances = []
    active_requester = get_active_requester()

    for name, config in APPS.items():
        if not config.get("enabled", True):
            continue  # disabled instance - don't even fetch its data

        # Hide the Request tab if there's no active requester configured
        # OR this account isn't allowed to request.
        effective_seerr_type = (
            config["seerr_media_type"] if (active_requester and perms["requesting"]) else None
        )

        queue = fetch_queue(name, config)
        history = fetch_history(name, config)
        library = fetch_library(name, config)
        seerr = fetch_seerr_requests(active_requester, effective_seerr_type)

        collections = []
        if config["library_kind"] == "movie":
            owned_tmdb_ids = {item["tmdb_id"] for item in library["library_items"] if item.get("tmdb_id")}
            collections = fetch_collections(config, owned_tmdb_ids)

        instances.append({
            "name": name,
            "logo_url": INSTANCE_ICONS.get(config["library_kind"], ""),
            "color": config.get("color", "#5b8cff"),
            "library_kind": config["library_kind"],
            "seerr_media_type": effective_seerr_type,
            "error": queue["error"],
            "queue_items": queue["queue_items"],
            "history_items": history["history_items"],
            "library_items": library["library_items"],
            "library_error": library["error"],
            "status_options": library["status_options"],
            "genre_options": library["genre_options"],
            "request_items": seerr["request_items"],
            "collections": collections,
        })
    return render_template(
        "index.html", instances=instances,
        username=user["username"], is_admin=is_admin,
        can_request=perms["requesting"], can_search=perms["searching"],
    )


if __name__ == "__main__":
    # This only runs for local `python app.py` use - Docker's gunicorn
    # entrypoint imports the app object directly and never reaches here.
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)