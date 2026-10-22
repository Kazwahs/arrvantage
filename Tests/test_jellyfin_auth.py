"""
Jellyfin 10.12 / 12.0 turned its legacy authorization off by default
(X-Emby-Token and the api_key query parameter) and answers 401
"CustomAuthentication was challenged" to anything but the current form,
Authorization: MediaBrowser Token="...". Jellyfin therefore gets the current
form (plus the legacy one for older servers); Emby only knows the legacy one.
"""
from urllib.parse import parse_qs, urlparse

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731

JELLY = {"name": "jelly", "type": "jellyfin", "url": "http://jf:8096", "credential": "abc123"}
EMBY = {"name": "emby", "type": "emby", "url": "http://em:8096", "credential": "abc123"}


class TestHelpers:
    def test_jellyfin_sends_the_current_authorization_header_and_the_legacy_one(self, app_module):
        headers = app_module.jellyfin_family_headers("jellyfin", "abc123")
        assert headers["Authorization"] == 'MediaBrowser Token="abc123"'
        assert headers["X-Emby-Token"] == "abc123"

    def test_emby_only_gets_its_own_header(self, app_module):
        assert app_module.jellyfin_family_headers("emby", "abc123") == {"X-Emby-Token": "abc123"}

    def test_a_quote_in_a_pasted_token_cannot_break_the_header(self, app_module):
        assert app_module.jellyfin_family_headers("jellyfin", 'ab"c')["Authorization"] == 'MediaBrowser Token="abc"'

    def test_query_parameters_use_the_current_name_for_jellyfin_only(self, app_module):
        assert app_module.jellyfin_family_token_params("jellyfin", "k") == {"api_key": "k", "ApiKey": "k"}
        assert app_module.jellyfin_family_token_params("emby", "k") == {"api_key": "k"}


class TestEveryJellyfinCallAuthenticatesTheCurrentWay:
    def _check(self, mock):
        assert mock.called
        assert mock.last_request.headers["Authorization"] == 'MediaBrowser Token="abc123"'

    def test_now_playing(self, app_module, requests_mock):
        mock = requests_mock.get("http://jf:8096/Sessions", json=[])
        app_module.fetch_jellyfin_now_playing(JELLY)
        self._check(mock)

    def test_recently_added(self, app_module, requests_mock):
        mock = requests_mock.get("http://jf:8096/Items", json={"Items": []})
        app_module.fetch_jellyfin_recently_added(JELLY)
        self._check(mock)

    def test_watch_history(self, app_module, requests_mock):
        mock = requests_mock.get("http://jf:8096/System/ActivityLog/Entries", json={"Items": []})
        app_module.fetch_jellyfin_watch_history(JELLY)
        self._check(mock)

    def test_users(self, app_module, requests_mock):
        mock = requests_mock.get("http://jf:8096/Users", json=[])
        app_module.fetch_jellyfin_users(JELLY)
        self._check(mock)

    def test_library_rescan(self, app_module, requests_mock):
        mock = requests_mock.post("http://jf:8096/Library/Refresh", status_code=204)
        app_module.trigger_jellyfin_library_scan(JELLY)
        self._check(mock)

    def test_deep_link_search(self, app_module, requests_mock):
        mock = requests_mock.get("http://jf:8096/Items", json={"Items": []})
        app_module.find_jellyfin_link(JELLY, "Movie", "Movie")
        self._check(mock)
        assert mock.last_request.qs["apikey"] == ["abc123"] and mock.last_request.qs["api_key"] == ["abc123"]

    def test_emby_calls_keep_the_legacy_header_only(self, app_module, requests_mock):
        mock = requests_mock.get("http://em:8096/Sessions", json=[])
        app_module.fetch_jellyfin_now_playing(EMBY)
        assert "Authorization" not in mock.last_request.headers
        assert mock.last_request.headers["X-Emby-Token"] == "abc123"

    def test_poster_urls_carry_the_token_in_a_form_jellyfin_accepts(self, app_module, requests_mock):
        item = {"NowPlayingItem": {"Name": "x", "Id": "i1"}, "PlayState": {}}
        requests_mock.get("http://jf:8096/Sessions", json=[item])
        url = app_module.fetch_jellyfin_now_playing(JELLY)["items"][0]["poster_url"]
        query = parse_qs(urlparse(url).query)
        assert query["ApiKey"] == ["abc123"] and query["api_key"] == ["abc123"]


class TestSettingsTestButton:
    def _post(self, client, csrf, kind):
        return client.post("/api/settings/test", headers=HEADERS(csrf), json={
            "category": "media_server", "kind_or_type": kind, "url": "http://jf:8096", "credential": "abc123"})

    def test_a_jellyfin_connection_test_uses_the_current_header(self, admin_session, requests_mock):
        client, csrf = admin_session
        mock = requests_mock.get("http://jf:8096/System/Info", json={"Id": "x"})
        assert self._post(client, csrf, "jellyfin").get_json() == {"ok": True}
        assert mock.last_request.headers["Authorization"] == 'MediaBrowser Token="abc123"'

    def test_a_rejected_key_gets_a_plain_explanation_not_an_empty_http_401(self, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get("http://jf:8096/System/Info", status_code=401, text="")
        error = self._post(client, csrf, "jellyfin").get_json()["error"]
        assert error.startswith("HTTP 401") and "API key" in error and "Jellyfin" in error

    def test_other_errors_keep_their_raw_detail(self, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get("http://jf:8096/System/Info", status_code=500, text="boom")
        assert self._post(client, csrf, "jellyfin").get_json()["error"] == "HTTP 500: boom"
