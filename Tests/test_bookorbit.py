"""
BookOrbit as a media server type: a health check (GET /api/v1/health, which
answers {"status": "ok"} and needs no sign-in) plus an Open link. Nothing
else is built because BookOrbit has no documented API key or REST reference.
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
