"""
BookOrbit as a media server type: a health check (GET /api/v1/health, which
answers {"status": "ok"} and needs no sign-in) plus an Open link. Nothing
else was built until BookOrbit 3.8 added API keys (docs/API_KEYS.md): with a key the
Libraries and Recently Added tabs read GET /api/v1/libraries, /libraries/<id>/stats and
/dashboard/scrollers/recently-added (shapes read from the person's real server, 12 Nov 2026).
"""
import requests

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731
SERVER = {"name": "Books", "type": "bookorbit", "url": "http://bookorbit.test:3000", "credential": "", "enabled": True}
HEALTH = "http://bookorbit.test:3000/api/v1/health"


def add_server(app_module, **overrides):
    entry = {**SERVER, **overrides}
    app_module.CONFIG.setdefault("media_servers", []).append(entry)
    app_module.apply_config(app_module.CONFIG)
    return entry


class TestHealth:
    def test_ok_means_healthy(self, app_module, requests_mock):
        mock = requests_mock.get(HEALTH, json={"status": "ok"})
        assert app_module.fetch_bookorbit_health(SERVER) == {"error": None, "status": "ok"}
        assert "Authorization" not in mock.last_request.headers  # no sign-in is needed or sent

    def test_a_trailing_slash_in_the_address_is_fine(self, app_module, requests_mock):
        requests_mock.get(HEALTH, json={"status": "ok"})
        assert app_module.fetch_bookorbit_health({**SERVER, "url": SERVER["url"] + "/"})["error"] is None

    def test_another_status_is_a_problem(self, app_module, requests_mock):
        requests_mock.get(HEALTH, json={"status": "degraded"})
        result = app_module.fetch_bookorbit_health(SERVER)
        assert "degraded" in result["error"]

    def test_an_http_error_names_the_code_and_not_the_address(self, app_module, requests_mock):
        requests_mock.get(HEALTH, status_code=404)
        error = app_module.fetch_bookorbit_health(SERVER)["error"]
        assert "404" in error and "bookorbit.test" not in error

    def test_something_that_is_not_bookorbit(self, app_module, requests_mock):
        requests_mock.get(HEALTH, text="<html>some other app</html>")
        assert "not like BookOrbit" in app_module.fetch_bookorbit_health(SERVER)["error"]
        requests_mock.get(HEALTH, json=["ok"])
        assert app_module.fetch_bookorbit_health(SERVER)["error"]

    def test_connection_failures_are_words_and_retried_once(self, app_module, requests_mock):
        mock = requests_mock.get(HEALTH, exc=requests.exceptions.ConnectionError("boom http://x/?apikey=SECRET"))
        assert app_module.fetch_bookorbit_health(SERVER) == {"error": "could not connect", "status": None}
        assert mock.call_count == 2
        requests_mock.get(HEALTH, exc=requests.exceptions.ConnectTimeout("slow"))
        assert app_module.fetch_bookorbit_health(SERVER)["error"] == "timed out"

    def test_one_failure_followed_by_success_counts_as_up(self, app_module, requests_mock):
        requests_mock.get(HEALTH, [{"exc": requests.exceptions.ConnectionError("blip")}, {"json": {"status": "ok"}}])
        assert app_module.fetch_bookorbit_health(SERVER)["error"] is None


class TestWiring:
    def test_it_is_a_selectable_media_server_type(self, app_module):
        assert app_module.MEDIA_SERVER_TYPES["bookorbit"] == "BookOrbit"

    def test_the_attention_probe_uses_the_health_check(self, app_module, monkeypatch):
        monkeypatch.setattr(app_module, "fetch_bookorbit_health", lambda s: {"error": "timed out", "status": None})
        assert app_module.probe_media_server(SERVER) == {"error": "timed out"}

    def test_a_down_bookorbit_is_a_critical_outage(self, app_module, requests_mock):
        requests_mock.get(HEALTH, exc=requests.exceptions.ConnectionError("down"))
        probe = app_module.probe_media_server(SERVER)
        signals = {"Radarr": {"queue": {"queue_items": []}, "health": {"health_items": []},
                              "diskspace": {"diskspace_items": []}}}
        [item] = app_module.summarize_attention(signals, {"media_servers": {"Books": probe}})["items"]
        assert (item["severity"], item["service_type"], item["hideable"]) == ("critical", "mediaserver", False)

    def test_settings_test_button_needs_no_token(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(HEALTH, json={"status": "ok"})
        body = {"category": "media_server", "kind_or_type": "bookorbit", "url": SERVER["url"], "credential": ""}
        assert client.post("/api/settings/test", json=body, headers=HEADERS(csrf)).get_json() == {"ok": True}

    def test_settings_test_button_reports_a_wrong_address(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(HEALTH, status_code=502)
        body = {"category": "media_server", "kind_or_type": "bookorbit", "url": SERVER["url"], "credential": ""}
        result = client.post("/api/settings/test", json=body, headers=HEADERS(csrf)).get_json()
        assert result["ok"] is False and "502" in result["error"]


class TestRoutes:
    def test_the_status_route_returns_health_and_the_open_link(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module)
        requests_mock.get(HEALTH, json={"status": "ok"})
        body = client.get("/api/bookorbit/health/Books").get_json()
        assert body["error"] is None and body["open_url"] == "http://bookorbit.test:3000"

    def test_the_status_route_only_serves_bookorbit_servers(self, app_module, admin_session):
        client, _ = admin_session
        add_server(app_module, name="Plexy", type="plex")
        assert client.get("/api/bookorbit/health/Plexy").status_code == 404
        assert client.get("/api/bookorbit/health/Nope").status_code == 404

    def test_plex_style_endpoints_say_so_instead_of_calling_jellyfin(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module)
        for path in ("recently-added", "watch-history", "users"):
            body = client.get(f"/api/mediaserver/{path}/Books").get_json()
            assert body == {"error": "not available for this kind of server"}
        assert not requests_mock.called  # nothing was sent to BookOrbit

    def test_rescan_is_only_for_plex_jellyfin_and_emby(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        add_server(app_module)
        response = client.post("/api/mediaserver/rescan/Books", headers=HEADERS(csrf))
        assert response.status_code == 400 and response.get_json()["ok"] is False
        assert not requests_mock.called

    def test_it_survives_a_backup_round_trip(self, app_module, admin_session):
        client, _ = admin_session
        add_server(app_module)
        exported = client.get("/api/config/export").get_json()["config"]
        assert [s["type"] for s in exported["media_servers"]] == ["bookorbit"]


# What the person's real BookOrbit 3.8 answered (12 Nov 2026), trimmed; `folders` are server paths.
KEY = "bok_" + "a" * 49
BASE = "http://bookorbit.test:3000"
LIBRARIES = [
    {"id": 3, "name": "Books", "bookCount": 695, "organizationMode": "book_per_folder", "accessLevel": None,
     "folders": [{"id": 1, "path": "/books/secret-folder"}]},
    {"id": 5, "name": "Comics", "bookCount": 1693, "folders": [{"id": 2, "path": "/comics/secret-folder"}]},
]
STATS = {
    3: {"totalBooks": 695, "totalSizeBytes": 23704094467,
        "formatCounts": {"cbz": 1, "mobi": 50, "pdf": 92, "azw3": 39, "m4b": 25, "epub": 478, "azw": 3, "mp3": 7}},
    5: {"totalBooks": 1693, "totalSizeBytes": 96383394083, "formatCounts": {"cbz": 1690, "epub": 3}},
}
RECENT = {"books": [
    {"id": 3147, "libraryId": 3, "status": "present", "title": "Threshing Day DA", "seriesId": None, "seriesName": None,
     "seriesIndex": None, "authors": ["Rebecca Yarros"], "files": [{"id": 12728, "format": "m4b", "role": "primary", "sizeBytes": 332952639}],
     "addedAt": "2026-11-11T02:33:43.146Z", "hasCover": True},
    {"id": 3144, "libraryId": 3, "title": "White Gold Wielder", "seriesName": "The Chronicles of Thomas Covenant", "seriesIndex": 6,
     "authors": ["Stephen R. Donaldson"], "files": [{"format": "epub"}, {"format": "epub"}, {"format": "mobi"}],
     "addedAt": "2026-11-08T00:48:54.360Z"},
], "total": 2}


def mock_bookorbit(requests_mock, key_ok=True):
    requests_mock.get(HEALTH, json={"status": "ok"})
    requests_mock.get(f"{BASE}/api/v1/libraries", json=LIBRARIES)
    for lib_id, stats in STATS.items():
        requests_mock.get(f"{BASE}/api/v1/libraries/{lib_id}/stats", json=stats)
    requests_mock.get(f"{BASE}/api/v1/dashboard/scrollers/recently-added", json=RECENT)


class TestApiKey:
    def test_the_libraries_view_has_counts_size_and_formats_and_no_paths(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_bookorbit(requests_mock)
        body = client.get("/api/bookorbit/libraries/Books").get_json()
        assert body["error"] is None and body["needs_key"] is False
        books, comics = body["libraries"]
        assert (books["name"], books["books"], books["size_display"]) == ("Books", 695, "22.1 GB")
        assert books["formats"][0] == {"format": "epub", "count": 478} and {"format": "m4b", "count": 25} in books["formats"]
        assert comics["size_display"] == "89.8 GB" and body["total_books"] == 2388 and body["total_size_display"] == "111.8 GB"
        assert "secret-folder" not in str(body) and KEY not in str(body)

    def test_the_key_is_sent_in_a_header_and_never_in_the_address(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_bookorbit(requests_mock)
        client.get("/api/bookorbit/libraries/Books")
        client.get("/api/bookorbit/recent/Books")
        sent = [r for r in requests_mock.request_history if r.path != "/api/v1/health"]
        assert sent and all(r.headers["X-Api-Key"] == KEY for r in sent)
        assert all(KEY not in r.url for r in requests_mock.request_history)

    def test_without_a_key_nothing_is_asked_of_bookorbit(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module)
        assert client.get("/api/bookorbit/libraries/Books").get_json()["needs_key"] is True
        assert client.get("/api/bookorbit/recent/Books").get_json()["needs_key"] is True
        assert not requests_mock.called

    def test_a_refused_key_is_said_in_words(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        requests_mock.get(f"{BASE}/api/v1/libraries", status_code=401)
        error = client.get("/api/bookorbit/libraries/Books").get_json()["error"]
        assert "refused the API key" in error and KEY not in error and "bookorbit.test" not in error

    def test_other_failures(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        for code, words in ((403, "Read access"), (404, "3.8"), (429, "rate limiting"), (500, "HTTP 500")):
            requests_mock.get(f"{BASE}/api/v1/libraries", status_code=code)
            assert words in client.get("/api/bookorbit/libraries/Books").get_json()["error"]
        requests_mock.get(f"{BASE}/api/v1/libraries", text="<html>login</html>")
        assert "not like BookOrbit" in client.get("/api/bookorbit/libraries/Books").get_json()["error"]
        requests_mock.get(f"{BASE}/api/v1/libraries", json={"not": "a list"})
        assert "not like BookOrbit" in client.get("/api/bookorbit/libraries/Books").get_json()["error"]

    def test_a_library_whose_stats_fail_still_shows_its_count(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_bookorbit(requests_mock)
        requests_mock.get(f"{BASE}/api/v1/libraries/5/stats", status_code=500)
        books, comics = client.get("/api/bookorbit/libraries/Books").get_json()["libraries"]
        assert books["details"] is True and comics["details"] is False
        assert comics["books"] == 1693 and comics["size_display"] == "" and comics["formats"] == []

    def test_defensive_parsing_of_odd_answers(self, app_module):
        rows = app_module.bookorbit_libraries_view(
            [{"id": 1, "name": "A", "bookCount": True}, {"id": "x"}, "junk", {"id": 2}],
            {1: {"totalBooks": -4, "totalSizeBytes": "big", "formatCounts": {"epub": "many", "pdf": 3, "": 2}}})
        assert [r["id"] for r in rows] == [1, 2] and rows[0]["books"] is None and rows[0]["size_display"] == ""
        assert rows[0]["formats"] == [{"format": "pdf", "count": 3}] and rows[1]["name"] == "(unnamed)"

    def test_recently_added_cards(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_bookorbit(requests_mock)
        body = client.get("/api/bookorbit/recent/Books").get_json()
        first, second = body["items"]
        assert first == {"id": 3147, "title": "Threshing Day DA", "authors": "Rebecca Yarros", "series": "", "formats": ["m4b"],
                         "library": "Books", "added": "2026-11-11", "cover_url": "/api/bookorbit/cover/Books/3147"}
        assert second["series"] == "The Chronicles of Thomas Covenant #6" and second["formats"] == ["epub", "mobi"]
        sent = [r for r in requests_mock.request_history if "recently-added" in r.path][0]
        assert sent.qs["limit"] == ["24"]

    def test_recently_added_without_the_library_names_still_works(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_bookorbit(requests_mock)
        requests_mock.get(f"{BASE}/api/v1/libraries", status_code=500)
        assert client.get("/api/bookorbit/recent/Books").get_json()["items"][0]["library"] == ""

    def test_the_routes_only_serve_bookorbit_servers(self, app_module, admin_session):
        client, _ = admin_session
        add_server(app_module, name="Plexy", type="plex", credential="t")
        for path in ("libraries/Plexy", "recent/Plexy", "cover/Plexy/1", "libraries/Nope"):
            assert client.get(f"/api/bookorbit/{path}").status_code == 404


class TestCover:
    def test_the_cover_is_proxied_with_the_key(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock = requests_mock.get(f"{BASE}/api/v1/books/3147/thumbnail", content=b"\x89PNG-bytes", headers={"Content-Type": "image/png"})
        reply = client.get("/api/bookorbit/cover/Books/3147")
        assert reply.status_code == 200 and reply.data == b"\x89PNG-bytes" and reply.mimetype == "image/png"
        assert "private" in reply.headers["Cache-Control"] and mock.last_request.headers["X-Api-Key"] == KEY

    def test_something_that_is_not_a_picture_is_refused(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        for kind in ("text/html", "image/svg+xml", ""):
            requests_mock.get(f"{BASE}/api/v1/books/1/thumbnail", content=b"<script>x</script>", headers={"Content-Type": kind})
            assert client.get("/api/bookorbit/cover/Books/1").status_code == 502

    def test_a_huge_answer_and_an_upstream_failure(self, app_module, admin_session, requests_mock, monkeypatch):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        monkeypatch.setattr(app_module, "BOOKORBIT_COVER_MAX_BYTES", 4)
        requests_mock.get(f"{BASE}/api/v1/books/1/thumbnail", content=b"12345", headers={"Content-Type": "image/png"})
        assert client.get("/api/bookorbit/cover/Books/1").status_code == 502
        requests_mock.get(f"{BASE}/api/v1/books/2/thumbnail", status_code=404)
        assert client.get("/api/bookorbit/cover/Books/2").status_code == 502

    def test_no_key_no_cover_and_the_id_must_be_a_number(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module)
        assert client.get("/api/bookorbit/cover/Books/1").status_code == 404
        add_server(app_module, name="Keyed", credential=KEY)
        assert client.get("/api/bookorbit/cover/Keyed/..%2Fadmin").status_code == 404
        assert not requests_mock.called


class TestKeyTest:
    def _post(self, client, csrf, credential):
        body = {"category": "media_server", "kind_or_type": "bookorbit", "url": BASE, "credential": credential}
        return client.post("/api/settings/test", json=body, headers=HEADERS(csrf)).get_json()

    def test_a_good_key(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(HEALTH, json={"status": "ok"})
        me = requests_mock.get(f"{BASE}/api/v1/auth/me", json={"id": 7, "authenticationMethod": "api_key"})
        assert self._post(client, csrf, KEY) == {"ok": True}
        assert me.last_request.headers["X-Api-Key"] == KEY

    def test_a_wrong_key_says_so_and_hints_at_the_prefix(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(HEALTH, json={"status": "ok"})
        requests_mock.get(f"{BASE}/api/v1/auth/me", status_code=401)
        result = self._post(client, csrf, "wrongkey")
        assert result["ok"] is False and "refused the API key" in result["error"] and "bok_" in result["error"]
        assert "bok_" not in self._post(client, csrf, KEY)["error"]

    def test_no_key_still_only_checks_the_address(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(HEALTH, json={"status": "ok"})
        assert self._post(client, csrf, "") == {"ok": True}
        assert [r.path for r in requests_mock.request_history] == ["/api/v1/health"]


class TestKeyIsASecretAndNeverAffectsUptime:
    def test_the_key_is_left_out_of_a_plain_export(self, app_module, admin_session):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        assert KEY not in str(client.get("/api/config/export").get_json())

    def test_a_bad_key_never_makes_a_running_bookorbit_look_down(self, app_module, requests_mock):
        server = add_server(app_module, credential="bok_wrong")
        requests_mock.get(HEALTH, json={"status": "ok"})
        requests_mock.get(f"{BASE}/api/v1/libraries", status_code=401)
        assert app_module.probe_media_server(server) == {"error": None}
        assert [r.path for r in requests_mock.request_history] == ["/api/v1/health"]

    def test_the_page_renders_the_new_tabs(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        html = client.get("/").get_data(as_text=True)
        assert 'data-mstab="bo-libraries"' in html and 'data-mstab="bo-recent"' in html
        assert "mediaserver-bo-libraries-Books" in html and "mediaserver-bo-recent-Books" in html
        assert KEY not in html
