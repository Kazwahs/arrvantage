"""
Dispatcharr as a media server type (github.com/Dispatcharr/Dispatcharr). Shapes read on 20 Nov 2026 from the
person's own v0.34.1 (Swagger at /api/swagger/): a key-less GET /api/core/version/ is the up/down check; with a user
API key (X-API-Key) the Channels, Playlists & Guides and Now Watching tabs read the channel summary, groups, M3U
accounts, EPG sources and /proxy/ts/status. Read-only throughout.
"""
from datetime import datetime, timedelta, timezone

import pytest
import requests

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731
BASE = "http://dispatcharr.test:9191"
KEY = "dpk_" + "b" * 40
SERVER = {"name": "IPTV", "type": "dispatcharr", "url": BASE, "credential": "", "enabled": True}
VERSION = f"{BASE}/api/core/version/"
CHANNELS = f"{BASE}/api/channels/channels/summary/"
GROUPS = f"{BASE}/api/channels/groups/"
PLAYLISTS = f"{BASE}/api/m3u/accounts/"
GUIDES = f"{BASE}/api/epg/sources/"
STATUS = f"{BASE}/proxy/ts/status"


def add_server(app_module, **overrides):
    entry = {**SERVER, **overrides}
    app_module.CONFIG.setdefault("media_servers", []).append(entry)
    app_module.apply_config(app_module.CONFIG)
    return entry


def hours_ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


CHANNEL_LIST = [
    {"id": 12, "uuid": "u-12", "name": "News Two", "logo_id": 7, "channel_number": 2.0, "epg_data_id": 1, "channel_group_id": 4},
    {"id": 11, "uuid": "u-11", "name": "Alpha One", "logo_id": None, "channel_number": 1, "epg_data_id": None, "channel_group_id": 4},
    {"id": 13, "uuid": "u-13", "name": "Sport 10.5", "logo_id": 9, "channel_number": 10.5, "epg_data_id": 2, "channel_group_id": 5},
    {"id": 14, "uuid": "u-14", "name": "No number", "logo_id": None, "channel_number": None, "epg_data_id": None, "channel_group_id": None},
]
GROUP_LIST = [
    {"id": 4, "name": "News", "channel_count": 2, "is_custom": False, "m3u_account_count": 1, "m3u_accounts": []},
    {"id": 5, "name": "Sport", "channel_count": 1, "is_custom": False, "m3u_account_count": 1, "m3u_accounts": []},
    {"id": 6, "name": "Empty group", "channel_count": 0, "is_custom": False, "m3u_account_count": 0, "m3u_accounts": []},
]
PLAYLIST_LIST = [
    {"id": 1, "name": "custom", "server_url": None, "file_path": None, "username": None, "password": None,
     "status": "error", "last_message": "Nothing to refresh", "is_active": True, "updated_at": hours_ago(900), "refresh_interval": 0},
    {"id": 2, "name": "Provider A", "server_url": "http://provider.example/get.php", "username": "me", "password": "hunter2",
     "status": "success", "last_message": "Processed 900 streams", "is_active": True, "updated_at": hours_ago(2),
     "refresh_interval": 24, "max_streams": 3},
]
GUIDE_LIST = [
    {"id": 1, "name": "Guide A", "url": "http://guide.example/xmltv.xml", "username": "", "api_key": "secret-guide-key",
     "status": "success", "last_message": "", "is_active": True, "updated_at": hours_ago(5), "refresh_interval": 12,
     "epg_data_count": 120},
]


def mock_dispatcharr(requests_mock):
    requests_mock.get(VERSION, json={"version": "0.34.1", "timestamp": None})
    requests_mock.get(CHANNELS, json=CHANNEL_LIST)
    requests_mock.get(GROUPS, json=GROUP_LIST)
    requests_mock.get(PLAYLISTS, json=PLAYLIST_LIST)
    requests_mock.get(GUIDES, json=GUIDE_LIST)
    requests_mock.get(STATUS, json={"channels": [], "count": 0})


@pytest.fixture(autouse=True)
def _fresh_summary_cache(app_module):
    app_module._DISPATCHARR_SUMMARY.clear()
    yield
    app_module._DISPATCHARR_SUMMARY.clear()


class TestHealth:
    def test_a_version_answer_means_up_and_no_key_is_sent(self, app_module, requests_mock):
        mock = requests_mock.get(VERSION, json={"version": "0.34.1", "timestamp": None})
        result = app_module.fetch_dispatcharr_health({**SERVER, "credential": KEY})
        assert result == {"error": None, "version": "0.34.1"}
        assert "X-Api-Key" not in mock.last_request.headers

    def test_trailing_slash_is_fine(self, app_module, requests_mock):
        requests_mock.get(VERSION, json={"version": "0.34.1"})
        assert app_module.fetch_dispatcharr_health({**SERVER, "url": BASE + "/"})["error"] is None

    def test_something_that_is_not_dispatcharr(self, app_module, requests_mock):
        for kwargs in ({"text": "<html>another app</html>"}, {"json": ["x"]}, {"json": {"version": ""}}, {"json": {}}):
            requests_mock.get(VERSION, **kwargs)
            assert "not like Dispatcharr" in app_module.fetch_dispatcharr_health(SERVER)["error"]

    def test_http_errors_name_the_code_and_not_the_address(self, app_module, requests_mock):
        requests_mock.get(VERSION, status_code=404)
        error = app_module.fetch_dispatcharr_health(SERVER)["error"]
        assert "404" in error and "dispatcharr.test" not in error

    def test_connection_failures_are_words_and_retried_once(self, app_module, requests_mock):
        mock = requests_mock.get(VERSION, exc=requests.exceptions.ConnectionError("boom http://x/?token=SECRET"))
        assert app_module.fetch_dispatcharr_health(SERVER) == {"error": "could not connect", "version": ""}
        assert mock.call_count == 2
        requests_mock.get(VERSION, exc=requests.exceptions.ConnectTimeout("slow"))
        assert app_module.fetch_dispatcharr_health(SERVER)["error"] == "timed out"

    def test_one_failure_then_success_counts_as_up(self, app_module, requests_mock):
        requests_mock.get(VERSION, [{"exc": requests.exceptions.ConnectionError("blip")}, {"json": {"version": "0.34.1"}}])
        assert app_module.fetch_dispatcharr_health(SERVER)["error"] is None


class TestWiring:
    def test_it_is_a_selectable_type(self, app_module):
        assert app_module.MEDIA_SERVER_TYPES["dispatcharr"] == "Dispatcharr"

    def test_down_is_a_critical_outage(self, app_module, requests_mock):
        requests_mock.get(VERSION, exc=requests.exceptions.ConnectionError("down"))
        probe = app_module.probe_media_server(SERVER)
        assert probe == {"error": "could not connect"}
        signals = {"Radarr": {"queue": {"queue_items": []}, "health": {"health_items": []}, "diskspace": {"diskspace_items": []}}}
        [item] = app_module.summarize_attention(signals, {"media_servers": {"IPTV": probe}})["items"]
        assert (item["severity"], item["hideable"]) == ("critical", False)

    def test_a_refused_key_never_makes_a_running_dispatcharr_look_down(self, app_module, requests_mock):
        mock_dispatcharr(requests_mock)
        requests_mock.get(PLAYLISTS, status_code=401)
        requests_mock.get(GUIDES, status_code=401)
        assert app_module.probe_media_server({**SERVER, "credential": KEY}) == {"error": None, "failing_sources": []}

    def test_without_a_key_only_the_address_is_checked(self, app_module, requests_mock):
        mock_dispatcharr(requests_mock)
        assert app_module.probe_media_server(SERVER) == {"error": None}
        assert [r.path for r in requests_mock.request_history] == ["/api/core/version/"]

    def test_the_built_in_custom_playlist_is_never_a_warning(self, app_module, requests_mock):
        mock_dispatcharr(requests_mock)
        assert app_module.probe_media_server({**SERVER, "credential": KEY})["failing_sources"] == []

    def test_a_failing_playlist_is_a_hideable_warning_naming_it(self, app_module, requests_mock):
        mock_dispatcharr(requests_mock)
        broken = [PLAYLIST_LIST[0], {**PLAYLIST_LIST[1], "status": "error", "last_message": "Failed at http://provider.example/x"}]
        requests_mock.get(PLAYLISTS, json=broken)
        probe = app_module.probe_media_server({**SERVER, "credential": KEY})
        assert [(f["name"], f["problem"]) for f in probe["failing_sources"]] == [("Provider A", "error")]
        signals = {"Radarr": {"queue": {"queue_items": []}, "health": {"health_items": []}, "diskspace": {"diskspace_items": []}}}
        items = app_module.summarize_attention(signals, {"media_servers": {"IPTV": probe}})["items"]
        [item] = [i for i in items if i["id"].startswith("service:")]
        assert item["severity"] == "warning" and item["hideable"] is True and item["hide_key"] == "service:mediaserver:IPTV:refresh"
        assert "Provider A" in item["detail"] and "refresh failed" in item["detail"] and "provider.example" not in str(item)

    def test_settings_test_button_without_a_key(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(VERSION, json={"version": "0.34.1"})
        body = {"category": "media_server", "kind_or_type": "dispatcharr", "url": BASE, "credential": ""}
        assert client.post("/api/settings/test", json=body, headers=HEADERS(csrf)).get_json() == {"ok": True}

    def test_settings_test_button_checks_a_typed_key(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(VERSION, json={"version": "0.34.1"})
        mock = requests_mock.get(f"{BASE}/api/accounts/users/me/", json={"id": 1, "username": "zak"})
        body = {"category": "media_server", "kind_or_type": "dispatcharr", "url": BASE, "credential": KEY}
        assert client.post("/api/settings/test", json=body, headers=HEADERS(csrf)).get_json() == {"ok": True}
        assert mock.last_request.headers["X-Api-Key"] == KEY
        requests_mock.get(f"{BASE}/api/accounts/users/me/", status_code=401)
        result = client.post("/api/settings/test", json=body, headers=HEADERS(csrf)).get_json()
        assert result["ok"] is False and "refused the API key" in result["error"] and KEY not in result["error"]

    def test_settings_test_button_reports_a_wrong_address(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(VERSION, status_code=502)
        body = {"category": "media_server", "kind_or_type": "dispatcharr", "url": BASE, "credential": ""}
        result = client.post("/api/settings/test", json=body, headers=HEADERS(csrf)).get_json()
        assert result["ok"] is False and "502" in result["error"]

    def test_it_survives_a_backup_round_trip(self, app_module, admin_session):
        client, _ = admin_session
        add_server(app_module)
        exported = client.get("/api/config/export").get_json()["config"]
        assert [s["type"] for s in exported["media_servers"]] == ["dispatcharr"]


class TestChannelsView:
    def test_sorted_by_number_with_unnumbered_last_and_groups_named(self):
        import app as a
        view = a.dispatcharr_channels_view(CHANNEL_LIST, GROUP_LIST, "IPTV")
        assert [c["name"] for c in view["channels"]] == ["Alpha One", "News Two", "Sport 10.5", "No number"]
        assert [c["number"] for c in view["channels"]] == [1, 2, 10.5, None]
        assert view["channels"][0]["group"] == "News" and view["channels"][3]["group"] == ""
        # groups with no channels are left out of the picker, the rest are alphabetical
        assert view["groups"] == [{"id": 4, "name": "News", "count": 2}, {"id": 5, "name": "Sport", "count": 1}]

    def test_logo_addresses_point_back_at_this_app(self):
        import app as a
        view = a.dispatcharr_channels_view(CHANNEL_LIST, GROUP_LIST, "My IPTV/1")
        assert view["channels"][1]["logo_url"] == "/api/dispatcharr/logo/My%20IPTV%2F1/7"
        assert view["channels"][0]["logo_url"] == ""

    def test_junk_is_skipped(self):
        import app as a
        view = a.dispatcharr_channels_view(
            ["x", None, {"name": "no id"}, {"id": "abc"}, {"id": 1, "name": None, "channel_number": "7", "logo_id": "nope"}],
            [None, "x", {"id": "z"}, {"id": 4, "name": None, "channel_count": "many"}], "IPTV")
        assert [c["name"] for c in view["channels"]] == ["(unnamed)"]
        assert view["channels"][0]["number"] is None and view["channels"][0]["logo_url"] == ""
        assert view["groups"] == []
        assert a.dispatcharr_channels_view(None, None, "IPTV") == {"channels": [], "groups": []}

    def test_no_key_means_nothing_is_asked(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module)
        assert client.get("/api/dispatcharr/channels/IPTV").get_json()["needs_key"] is True
        assert client.get("/api/dispatcharr/sources/IPTV").get_json()["needs_key"] is True
        assert client.get("/api/dispatcharr/watching/IPTV").get_json()["needs_key"] is True
        assert client.get("/api/dispatcharr/summary/IPTV").get_json()["needs_key"] is True
        assert not requests_mock.called

    def test_the_route_returns_channels_and_sends_the_key_only_in_a_header(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        body = client.get("/api/dispatcharr/channels/IPTV").get_json()
        assert body["error"] is None and body["total"] == 4 and body["cut_short"] is False and KEY not in str(body)
        sent = requests_mock.request_history
        assert sent and all(r.headers["X-Api-Key"] == KEY and KEY not in r.url for r in sent)

    def test_group_names_failing_still_shows_channels(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        requests_mock.get(GROUPS, status_code=500)
        body = client.get("/api/dispatcharr/channels/IPTV").get_json()
        assert body["error"] is None and body["total"] == 4 and body["groups"] == []

    def test_very_long_lists_are_cut_and_say_so(self, app_module, admin_session, requests_mock, monkeypatch):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        monkeypatch.setattr(app_module, "DISPATCHARR_MAX_CHANNELS", 2)
        body = client.get("/api/dispatcharr/channels/IPTV").get_json()
        assert body["total"] == 2 and body["cut_short"] is True

    def test_failures_are_words(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        for code, words in ((401, "refused the API key"), (403, "administrator"), (404, "not found"), (500, "HTTP 500")):
            requests_mock.get(CHANNELS, status_code=code)
            error = client.get("/api/dispatcharr/channels/IPTV").get_json()["error"]
            assert words in error and KEY not in error and "dispatcharr.test" not in error
        requests_mock.get(CHANNELS, text="<html>login</html>")
        assert "not like Dispatcharr" in client.get("/api/dispatcharr/channels/IPTV").get_json()["error"]
        requests_mock.get(CHANNELS, json={"not": "a list"})
        assert "not like Dispatcharr" in client.get("/api/dispatcharr/channels/IPTV").get_json()["error"]

    def test_an_unknown_or_other_kind_of_server_is_a_404(self, app_module, admin_session):
        client, _ = admin_session
        app_module.CONFIG.setdefault("media_servers", []).append({**SERVER, "name": "Other", "type": "plex"})
        app_module.apply_config(app_module.CONFIG)
        for name in ("Nope", "Other"):
            assert client.get(f"/api/dispatcharr/channels/{name}").status_code == 404
            assert client.get(f"/api/dispatcharr/logo/{name}/1").status_code == 404

    def test_login_is_required(self, app_module):
        client = app_module.app.test_client()
        assert client.get("/api/dispatcharr/channels/IPTV").status_code in (302, 401)


class TestSources:
    def test_the_custom_playlist_is_manual_and_never_a_problem(self):
        import app as a
        rows = a.dispatcharr_sources_view(PLAYLIST_LIST, GUIDE_LIST)
        custom = rows[0]
        assert custom["manual"] is True and custom["problem"] == "" and custom["status_label"] == "Manual channels"
        assert custom["message"] == ""

    def test_nothing_sensitive_is_passed_on(self):
        import app as a
        text = str(a.dispatcharr_sources_view(PLAYLIST_LIST, GUIDE_LIST))
        for secret in ("hunter2", "secret-guide-key", "provider.example", "guide.example", "get.php", "'me'"):
            assert secret not in text

    def test_addresses_in_a_message_are_hidden(self):
        import app as a
        rows = a.dispatcharr_sources_view([{**PLAYLIST_LIST[1], "status": "error",
                                            "last_message": "Failed to fetch http://provider.example/get.php now"}], [])
        assert "provider.example" not in rows[0]["message"] and "(address hidden)" in rows[0]["message"]

    def test_a_failed_refresh_is_a_problem(self):
        import app as a
        [row] = a.dispatcharr_sources_view([{**PLAYLIST_LIST[1], "status": "error"}], [])
        assert row["problem"] == "error" and row["status_label"] == "Failed"

    def test_an_overdue_refresh_is_stale_but_a_recent_one_is_not(self):
        import app as a
        old = {**PLAYLIST_LIST[1], "refresh_interval": 24, "updated_at": hours_ago(100)}
        fine = {**PLAYLIST_LIST[1], "refresh_interval": 24, "updated_at": hours_ago(30)}
        rows = a.dispatcharr_sources_view([old, fine], [])
        assert [r["problem"] for r in rows] == ["stale", ""]

    def test_no_schedule_means_never_stale(self):
        import app as a
        [row] = a.dispatcharr_sources_view([{**PLAYLIST_LIST[1], "refresh_interval": 0, "updated_at": hours_ago(5000)}], [])
        assert row["problem"] == ""

    def test_a_busy_refresh_is_not_stale(self):
        import app as a
        [row] = a.dispatcharr_sources_view([{**PLAYLIST_LIST[1], "status": "fetching", "updated_at": hours_ago(500)}], [])
        assert row["problem"] == "" and row["status_label"] == "Fetching"

    def test_a_switched_off_source_is_never_a_problem(self):
        import app as a
        [row] = a.dispatcharr_sources_view([{**PLAYLIST_LIST[1], "status": "error", "is_active": False}], [])
        assert row["problem"] == "" and row["active"] is False

    def test_the_dummy_guide_is_manual(self):
        import app as a
        [row] = a.dispatcharr_sources_view([], [{"id": 9, "name": "Dummy", "source_type": "dummy", "status": "error", "is_active": True}])
        assert row["manual"] is True and row["problem"] == ""

    def test_counts_and_ages(self):
        import app as a
        rows = a.dispatcharr_sources_view(PLAYLIST_LIST, GUIDE_LIST)
        provider = [r for r in rows if r["name"] == "Provider A"][0]
        guide = [r for r in rows if r["kind"] == "guide"][0]
        assert provider["streams_allowed"] == 3 and provider["channels_found"] is None and provider["interval_hours"] == 24
        assert 1.9 < provider["age_hours"] < 2.2
        assert guide["channels_found"] == 120 and guide["streams_allowed"] is None

    def test_unknown_status_and_junk(self):
        import app as a
        rows = a.dispatcharr_sources_view(["x", None, {"name": "no id"}, {"id": 3, "name": None, "status": "weird", "updated_at": "garbage", "server_url": "http://x.example/"}], "nope")
        assert len(rows) == 1 and rows[0]["status"] == "unknown" and rows[0]["status_label"] == "Unknown"
        assert rows[0]["age_hours"] is None and rows[0]["problem"] == ""

    def test_the_route(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        body = client.get("/api/dispatcharr/sources/IPTV").get_json()
        assert body["error"] is None and body["problems"] == 0 and body["unavailable"] == []
        assert {r["kind"] for r in body["sources"]} == {"playlist", "guide"} and len(body["sources"]) == 3

    def test_one_list_failing_is_said_and_the_other_still_shows(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        requests_mock.get(GUIDES, status_code=500)
        body = client.get("/api/dispatcharr/sources/IPTV").get_json()
        assert body["unavailable"] == ["guides"] and {r["kind"] for r in body["sources"]} == {"playlist"}

    def test_both_failing_is_an_error(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        requests_mock.get(PLAYLISTS, status_code=401)
        requests_mock.get(GUIDES, status_code=401)
        assert "refused the API key" in client.get("/api/dispatcharr/sources/IPTV").get_json()["error"]


class TestWatching:
    def test_nobody_watching(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        body = client.get("/api/dispatcharr/watching/IPTV").get_json()
        assert body == {"error": None, "needs_key": False, "channels": [], "count": 0}
        assert not [r for r in requests_mock.request_history if r.path.startswith("/api/channels")]  # names are not needed

    def test_entries_are_matched_to_channel_names(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        requests_mock.get(STATUS, json={"channels": [
            {"channel_id": "u-12", "state": "active", "uptime": 3725.0,
             "clients": [{"ip_address": "192.168.1.50", "username": "kid"}, {"ip_address": "192.168.1.51"}]}], "count": 1})
        body = client.get("/api/dispatcharr/watching/IPTV").get_json()
        [channel] = body["channels"]
        assert channel["name"] == "News Two" and channel["viewers"] == 2 and channel["state"] == "active"
        assert channel["uptime"] and body["count"] == 1
        assert "192.168" not in str(body) and "kid" not in str(body)

    def test_a_dict_keyed_by_channel_is_understood(self):
        import app as a
        view = a.dispatcharr_watching_view({"channels": {"u-1": {"client_count": 3, "state": "buffering"}}, "count": 1}, {"u-1": "One"})
        assert view["channels"][0]["name"] == "One" and view["channels"][0]["viewers"] == 3 and view["channels"][0]["state"] == "buffering"

    def test_unknown_shapes_fall_back_to_the_count(self):
        import app as a
        view = a.dispatcharr_watching_view({"channels": ["junk", 5, None], "count": 2}, {})
        assert view == {"channels": [], "count": 2}
        assert a.dispatcharr_watching_view(["not a dict"], {}) == {"channels": [], "count": 0}
        assert a.dispatcharr_watching_view({"channels": [{"weird": 1}]}, {})["channels"][0]["name"] == "(unknown channel)"

    def test_failures(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        requests_mock.get(STATUS, status_code=403)
        assert "administrator" in client.get("/api/dispatcharr/watching/IPTV").get_json()["error"]
        requests_mock.get(STATUS, json=["list"])
        assert "not like Dispatcharr" in client.get("/api/dispatcharr/watching/IPTV").get_json()["error"]


class TestSummary:
    def test_the_numbers_for_home(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        assert client.get("/api/dispatcharr/summary/IPTV").get_json() == {
            "error": None, "needs_key": False, "channels": 4, "problems": 0, "watching": 0}

    def test_problems_are_counted(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        requests_mock.get(GUIDES, json=[{**GUIDE_LIST[0], "status": "error"}])
        assert client.get("/api/dispatcharr/summary/IPTV").get_json()["problems"] == 1

    def test_a_part_that_fails_is_left_out_not_zero(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        requests_mock.get(PLAYLISTS, status_code=500)
        requests_mock.get(GUIDES, status_code=500)
        requests_mock.get(STATUS, status_code=500)
        body = client.get("/api/dispatcharr/summary/IPTV").get_json()
        assert body["channels"] == 4 and body["problems"] is None and body["watching"] is None

    def test_a_good_answer_is_cached_briefly_and_a_failure_is_not(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        mock_dispatcharr(requests_mock)
        client.get("/api/dispatcharr/summary/IPTV")
        calls = requests_mock.call_count
        client.get("/api/dispatcharr/summary/IPTV")
        assert requests_mock.call_count == calls
        app_module._DISPATCHARR_SUMMARY.clear()
        requests_mock.get(CHANNELS, status_code=500)
        assert client.get("/api/dispatcharr/summary/IPTV").get_json()["error"]
        requests_mock.get(CHANNELS, json=CHANNEL_LIST)
        assert client.get("/api/dispatcharr/summary/IPTV").get_json()["error"] is None


class TestLogo:
    def test_a_png_is_passed_through(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        requests_mock.get(f"{BASE}/api/channels/logos/7/cache/", content=b"\x89PNG....", headers={"Content-Type": "image/png"})
        response = client.get("/api/dispatcharr/logo/IPTV/7")
        assert response.status_code == 200 and response.mimetype == "image/png" and response.data.startswith(b"\x89PNG")

    def test_svg_html_and_oversize_are_refused(self, app_module, admin_session, requests_mock, monkeypatch):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        url = f"{BASE}/api/channels/logos/7/cache/"
        requests_mock.get(url, content=b"<svg onload=alert(1)>", headers={"Content-Type": "image/svg+xml"})
        assert client.get("/api/dispatcharr/logo/IPTV/7").status_code == 502
        requests_mock.get(url, content=b"<html>", headers={"Content-Type": "text/html"})
        assert client.get("/api/dispatcharr/logo/IPTV/7").status_code == 502
        monkeypatch.setattr(app_module, "DISPATCHARR_LOGO_MAX_BYTES", 3)
        requests_mock.get(url, content=b"123456", headers={"Content-Type": "image/png"})
        assert client.get("/api/dispatcharr/logo/IPTV/7").status_code == 502

    def test_a_failure_is_a_502(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        requests_mock.get(f"{BASE}/api/channels/logos/7/cache/", status_code=404)
        assert client.get("/api/dispatcharr/logo/IPTV/7").status_code == 502


class TestPage:
    def test_the_page_has_the_tabs_and_ids_the_script_uses(self, app_module, admin_session):
        client, _ = admin_session
        add_server(app_module)
        html = client.get("/").get_data(as_text=True)
        for needle in ('data-mstab="dp-channels"', 'data-mstab="dp-sources"', 'data-mstab="dp-watching"',
                       'id="dp-group-IPTV"', 'id="dp-search-IPTV"', 'id="dp-stats-IPTV"', 'id="dp-pagination-IPTV"',
                       'id="mediaserver-dp-channels-IPTV"', 'id="mediaserver-dp-sources-IPTV"', 'id="mediaserver-dp-watching-IPTV"',
                       "function loadDispatcharrChannels", "function loadDispatcharrSources", "function loadDispatcharrWatching",
                       "function hydrateDispatcharrHome"):
            assert needle in html, needle


# ---- Channel details: a link for a player, and what is on now and next ----
SEARCH = f"{BASE}/api/epg/programs/search/"


def iso(delta_minutes):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def program(title, start_min, end_min, **extra):
    return {"id": abs(hash(title)) % 1000, "title": title, "sub_title": None, "description": None,
            "start_time": iso(start_min), "end_time": iso(end_min), "tvg_id": "x", **extra}


class TestGuideView:
    def test_now_and_next_with_progress(self):
        import app as a
        view = a.dispatcharr_guide_view([program("Later", 60, 90), program("Right now", -30, 30, sub_title="Ep 4",
                                                                           description="What happens?  Everything."),
                                         program("Up next", 30, 60), program("Earlier", -90, -30)])
        assert view["now"]["title"] == "Right now" and view["now"]["subtitle"] == "Ep 4"
        assert view["now"]["percent"] == 50 and view["next"]["title"] == "Up next"
        # a question mark in the blurb survives (scrub_log_text would have cut it as a query string)
        assert view["now"]["description"] == "What happens? Everything."
        assert view["now"]["start"].endswith("Z")

    def test_a_gap_means_no_now_but_still_a_next(self):
        import app as a
        view = a.dispatcharr_guide_view([program("Later", 20, 50)])
        assert view["now"] is None and view["next"]["title"] == "Later"

    def test_nothing_or_junk(self):
        import app as a
        assert a.dispatcharr_guide_view([]) == {"now": None, "next": None}
        assert a.dispatcharr_guide_view(None) == {"now": None, "next": None}
        junk = ["x", None, {"title": "no times"}, {"title": "bad", "start_time": "nope", "end_time": "nope"},
                {"title": "backwards", "start_time": iso(10), "end_time": iso(5)}]
        assert a.dispatcharr_guide_view(junk) == {"now": None, "next": None}

    def test_a_paged_answer_is_understood_and_text_is_cut(self):
        import app as a
        view = a.dispatcharr_guide_view({"results": [program("T" * 400, -5, 5, description="d" * 900)]})
        assert len(view["now"]["title"]) <= 154 and len(view["now"]["description"]) <= 404

    def test_addresses_in_a_blurb_are_dropped(self):
        import app as a
        view = a.dispatcharr_guide_view([program("Show", -5, 5, description="More at http://tracker.example/x now")])
        assert "tracker.example" not in view["now"]["description"]


class TestChannelDetails:
    def mock(self, requests_mock, programs=None, **kw):
        mock_dispatcharr(requests_mock)
        return requests_mock.get(SEARCH, json=programs if programs is not None else [program("Now", -10, 20), program("Next", 20, 50)], **kw)

    def test_the_link_the_guide_and_the_exact_questions_asked(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        search = self.mock(requests_mock)
        body = client.get("/api/dispatcharr/channel/IPTV/12").get_json()
        assert body["error"] is None and body["has_guide"] is True and body["guide_error"] is None
        assert body["stream_url"] == f"{BASE}/proxy/ts/stream/u-12"
        assert body["now"]["title"] == "Now" and body["next"]["title"] == "Next"
        asked = search.last_request.qs
        assert asked["channel_id"] == ["12"] and asked["page_size"] == ["20"] and "end_after" in asked and "start_before" in asked
        assert search.last_request.headers["X-Api-Key"] == KEY
        assert KEY not in str(body)  # the link carries no key

    def test_a_channel_without_guide_data_never_asks_the_guide(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        search = self.mock(requests_mock)
        body = client.get("/api/dispatcharr/channel/IPTV/11").get_json()  # epg_data_id is None
        assert body["has_guide"] is False and body["now"] is None and body["stream_url"].endswith("/u-11")
        assert not search.called

    def test_a_guide_that_cannot_be_read_still_gives_the_link(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        self.mock(requests_mock, status_code=500)
        body = client.get("/api/dispatcharr/channel/IPTV/12").get_json()
        assert body["error"] is None and body["guide_error"] == "HTTP 500" and body["stream_url"]

    def test_a_channel_that_is_gone(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        self.mock(requests_mock)
        assert "no longer in Dispatcharr" in client.get("/api/dispatcharr/channel/IPTV/4242").get_json()["error"]

    def test_a_strange_uuid_gives_no_link(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module, credential=KEY)
        self.mock(requests_mock)
        requests_mock.get(CHANNELS, json=[{**CHANNEL_LIST[0], "uuid": "../../etc?x=1"}])
        assert client.get("/api/dispatcharr/channel/IPTV/12").get_json()["stream_url"] == ""

    def test_no_key_and_failures(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_server(app_module)
        assert client.get("/api/dispatcharr/channel/IPTV/12").get_json()["needs_key"] is True
        assert not requests_mock.called
        app_module.CONFIG["media_servers"][-1]["credential"] = KEY
        app_module.apply_config(app_module.CONFIG)
        requests_mock.get(CHANNELS, status_code=401)
        assert "refused the API key" in client.get("/api/dispatcharr/channel/IPTV/12").get_json()["error"]

    def test_unknown_server_and_login(self, app_module, admin_session):
        client, _ = admin_session
        assert client.get("/api/dispatcharr/channel/Nope/1").status_code == 404
        assert app_module.app.test_client().get("/api/dispatcharr/channel/IPTV/1").status_code in (302, 401)

    def test_the_channel_list_says_which_channels_have_a_guide(self):
        import app as a
        rows = {c["id"]: c["has_guide"] for c in a.dispatcharr_channels_view(CHANNEL_LIST, GROUP_LIST, "IPTV")["channels"]}
        assert rows == {11: False, 12: True, 13: True, 14: False}

    def test_the_page_has_the_panel_code(self, app_module, admin_session):
        client, _ = admin_session
        add_server(app_module)
        html = client.get("/").get_data(as_text=True)
        for needle in ("function openDispatcharrChannel", "function dispatcharrCopy", "data-dp-channel", "dp-copy-btn"):
            assert needle in html, needle
