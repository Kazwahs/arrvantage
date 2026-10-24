"""
Settings > Backup, restore and diagnostics.

Export: optional keys (off by default), never the session key.
Import: validated, previewed first, and the current settings are saved
as a backup file before anything is replaced.
Diagnostics: counts and types only, addresses hidden by default, no secrets.
"""
import json
import logging
import os

from werkzeug.security import generate_password_hash

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731

KEY = "SECRETAPIKEY111"
TOKEN = "SECRETTOKEN222"

CONFIG = {
    "instances": [
        {"name": "Radarr", "kind": "movie", "url": "http://192.168.1.20:7878", "api_key": KEY,
         "color": "#e8a33d", "enabled": True},
        {"name": "Sonarr", "kind": "series", "url": "http://sonarr.lan:8989", "api_key": KEY,
         "color": "#4d8fdd", "enabled": False},
    ],
    "media_servers": [{"name": "Kazzyfin", "type": "jellyfin", "url": "http://192.168.1.99:30015",
                       "credential": TOKEN, "color": "#5b8cff", "enabled": True}],
    "requesters": [], "downloaders": [], "indexers": [], "transcoders": [],
    "tmdb": {"api_key": "TMDBSECRET"}, "tvdb": {"api_key": "", "pin": "1234"},
    "fanart": {"api_key": ""}, "theaudiodb": {"api_key": "123"},
    "tautulli": {"url": "", "api_key": ""}, "tracearr": {"url": "", "api_key": ""},
}


def seed(app_module):
    """Gives the app a populated config and returns the admin's password."""
    app_module.CONFIG.update(json.loads(json.dumps(CONFIG)))
    app_module.apply_config(app_module.CONFIG)


def get(client, url):
    return client.get(url)


class TestExport:
    def test_keys_are_left_out_unless_asked_for(self, app_module, admin_session):
        client, _csrf = admin_session
        seed(app_module)
        body = client.get("/api/config/export").get_data(as_text=True)
        for secret in (KEY, TOKEN, "TMDBSECRET", "password_hash", "1234"):
            assert secret not in body
        data = json.loads(body)
        assert data["includes_secrets"] is False
        assert data["config"]["instances"][0]["name"] == "Radarr"
        assert data["config"]["instances"][0]["color"] == "#e8a33d"

    def test_keys_come_with_the_checkbox(self, app_module, admin_session):
        client, _csrf = admin_session
        seed(app_module)
        data = client.get("/api/config/export?secrets=1").get_json()
        assert data["includes_secrets"] is True
        assert data["config"]["instances"][0]["api_key"] == KEY
        assert data["config"]["media_servers"][0]["credential"] == TOKEN
        assert data["config"]["users"][0]["password_hash"]

    def test_the_session_key_is_never_exported(self, app_module, admin_session):
        client, _csrf = admin_session
        secret = app_module.CONFIG["secret_key"]
        for suffix in ("", "?secrets=1"):
            assert secret not in client.get("/api/config/export" + suffix).get_data(as_text=True)

    def test_it_downloads_as_a_file_that_says_whether_it_has_keys(self, app_module, admin_session):
        client, _csrf = admin_session
        plain = client.get("/api/config/export")
        full = client.get("/api/config/export?secrets=1")
        assert "attachment" in plain.headers["Content-Disposition"]
        assert plain.headers["Content-Disposition"].endswith('-no-keys.json"')
        assert not full.headers["Content-Disposition"].endswith('-no-keys.json"')
        assert plain.headers["Cache-Control"] == "no-store"

    def test_admin_only(self, app_module, client):
        assert client.get("/api/config/export").status_code in (302, 401, 403)
        assert client.get("/api/diagnostics").status_code in (302, 401, 403)


class TestImport:
    def _post(self, client, csrf, file_data, confirm=False):
        return client.post("/api/config/import", headers=HEADERS(csrf), json={"file": file_data, "confirm": confirm})

    def test_a_preview_changes_nothing(self, app_module, admin_session):
        client, csrf = admin_session
        seed(app_module)
        before = json.dumps(app_module.CONFIG, sort_keys=True)
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["instances"] = export["config"]["instances"][:1]
        body = self._post(client, csrf, export).get_json()
        assert body["ok"] is True and body["applied"] is False
        assert json.dumps(app_module.CONFIG, sort_keys=True) == before
        instances = next(s for s in body["report"]["sections"] if s["label"] == "Instances")
        assert (instances["current"], instances["incoming"]) == (2, 1)
        assert not [f for f in os.listdir(app_module.CONFIG_DIR) if f.startswith("config.backup-")]

    def test_a_round_trip_restores_everything(self, app_module, admin_session):
        client, csrf = admin_session
        seed(app_module)
        export = client.get("/api/config/export?secrets=1").get_json()
        original = json.loads(json.dumps(app_module.CONFIG))
        app_module.CONFIG["instances"] = []
        app_module.CONFIG["media_servers"] = []
        app_module.apply_config(app_module.CONFIG)
        body = self._post(client, csrf, export, confirm=True).get_json()
        assert body["applied"] is True
        assert app_module.CONFIG["instances"] == original["instances"]
        assert app_module.CONFIG["media_servers"] == original["media_servers"]
        assert app_module.CONFIG["users"] == original["users"]
        assert "Radarr" in app_module.APPS and app_module.APPS["Radarr"]["api_key"] == KEY
        on_disk = json.load(open(app_module.CONFIG_PATH))
        assert on_disk["instances"] == original["instances"]

    def test_the_old_settings_are_backed_up_first(self, app_module, admin_session):
        client, csrf = admin_session
        seed(app_module)
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["instances"] = []
        body = self._post(client, csrf, export, confirm=True).get_json()
        backup = os.path.join(app_module.CONFIG_DIR, body["backup"])
        saved = json.load(open(backup))
        assert [i["name"] for i in saved["instances"]] == ["Radarr", "Sonarr"]
        assert app_module.CONFIG["instances"] == []
        if os.name == "posix":
            assert oct(os.stat(backup).st_mode & 0o777) == "0o600"

    def test_only_the_newest_backups_are_kept(self, app_module, admin_session):
        client, csrf = admin_session
        for n in range(15):
            open(os.path.join(app_module.CONFIG_DIR, f"config.backup-20200101-0000{n:02d}.json"), "w").write("{}")
        export = client.get("/api/config/export?secrets=1").get_json()
        self._post(client, csrf, export, confirm=True)
        backups = [f for f in os.listdir(app_module.CONFIG_DIR) if f.startswith("config.backup-")]
        assert len(backups) == app_module.CONFIG_BACKUPS_KEPT

    def test_a_file_without_keys_keeps_the_keys_already_set_up(self, app_module, admin_session):
        client, csrf = admin_session
        seed(app_module)
        export = client.get("/api/config/export").get_json()  # no keys
        export["config"]["instances"][0]["url"] = "http://192.168.1.50:7878"
        body = self._post(client, csrf, export, confirm=True).get_json()
        assert body["applied"] is True
        radarr = app_module.CONFIG["instances"][0]
        assert radarr["url"] == "http://192.168.1.50:7878"
        assert radarr["api_key"] == KEY
        assert app_module.CONFIG["media_servers"][0]["credential"] == TOKEN
        assert app_module.CONFIG["tmdb"]["api_key"] == "TMDBSECRET"

    def test_a_file_without_keys_never_touches_accounts(self, app_module, admin_session):
        client, csrf = admin_session
        users_before = json.dumps(app_module.CONFIG["users"])
        export = client.get("/api/config/export").get_json()
        body = self._post(client, csrf, export, confirm=True).get_json()
        assert json.dumps(app_module.CONFIG["users"]) == users_before
        assert any("Accounts and passwords are kept" in n for n in body["report"]["notes"])

    def test_the_session_key_survives_an_import(self, app_module, admin_session):
        client, csrf = admin_session
        before = app_module.CONFIG["secret_key"]
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["secret_key"] = "attacker-chosen"
        self._post(client, csrf, export, confirm=True)
        assert app_module.CONFIG["secret_key"] == before

    def test_a_file_that_would_lock_you_out_is_refused(self, app_module, admin_session):
        client, csrf = admin_session
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["users"] = [{"username": "bob", "password_hash": generate_password_hash("x"),
                                      "role": "user", "permissions": {}}]
        before = json.dumps(app_module.CONFIG, sort_keys=True)
        response = self._post(client, csrf, export, confirm=True)
        assert response.status_code == 400
        assert "no administrator" in response.get_json()["errors"][0]
        assert json.dumps(app_module.CONFIG, sort_keys=True) == before

    def test_the_preview_warns_when_you_would_be_signed_out(self, app_module, admin_session):
        client, csrf = admin_session
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["users"] = [{"username": "other", "password_hash": generate_password_hash("x"),
                                      "role": "admin", "permissions": {}}]
        body = self._post(client, csrf, export).get_json()
        assert body["report"]["signs_you_out"] is True

    def test_not_a_config_file(self, app_module, admin_session):
        client, csrf = admin_session
        for junk in ({"hello": "world"}, [1, 2], "text", None):
            response = self._post(client, csrf, junk)
            assert response.status_code == 400
            assert response.get_json()["errors"] == ["This is not an arrVantage config file."]

    def test_bad_entries_are_skipped_not_trusted(self, app_module, admin_session):
        client, csrf = admin_session
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["instances"] = [
            {"name": "ok", "kind": "movie", "url": "http://r:7878", "api_key": "k", "color": "red", "enabled": "yes"},
            {"name": "bad kind", "kind": "nonsense", "url": "http://x"},
            {"name": "bad url", "kind": "movie", "url": "javascript:alert(1)"},
            {"kind": "movie", "url": "http://x"},
            "not even a dict",
        ]
        body = self._post(client, csrf, export, confirm=True).get_json()
        assert [i["name"] for i in app_module.CONFIG["instances"]] == ["ok"]
        ok = app_module.CONFIG["instances"][0]
        assert ok["color"] == "#5b8cff" and ok["enabled"] is True  # bad colour / flag fall back
        assert any("4 entries" in n for n in body["report"]["notes"])

    def test_unknown_fields_are_dropped(self, app_module, admin_session):
        client, csrf = admin_session
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["instances"] = [{"name": "r", "kind": "movie", "url": "http://r", "evil": "<script>"}]
        self._post(client, csrf, export, confirm=True)
        assert "evil" not in app_module.CONFIG["instances"][0]

    def test_a_raw_config_json_is_accepted(self, app_module, admin_session):
        client, csrf = admin_session
        seed(app_module)
        raw = json.loads(json.dumps(app_module.CONFIG))
        raw["instances"] = raw["instances"][:1]
        body = self._post(client, csrf, raw, confirm=True).get_json()
        assert body["applied"] is True
        assert [i["name"] for i in app_module.CONFIG["instances"]] == ["Radarr"]

    def test_a_bad_password_hash_is_not_accepted(self, app_module, admin_session):
        client, csrf = admin_session
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["users"].append({"username": "mallory", "password_hash": "plaintext", "role": "admin"})
        self._post(client, csrf, export, confirm=True)
        assert [u["username"] for u in app_module.CONFIG["users"]] == ["testadmin"]

    def test_per_user_choices_survive_a_round_trip_and_are_validated(self, app_module, admin_session):
        client, csrf = admin_session
        app_module.CONFIG["users"][0].update({"theme": "nord", "accent": "violet",
                                              "hidden_attention": ["queue:a", "garbage"]})
        export = client.get("/api/config/export?secrets=1").get_json()
        export["config"]["users"][0]["theme"] = "not-a-theme"
        self._post(client, csrf, export, confirm=True)
        user = app_module.CONFIG["users"][0]
        assert user["theme"] == app_module.DEFAULT_THEME
        assert user["accent"] == "violet"
        assert user["hidden_attention"] == ["queue:a"]

    def test_a_huge_upload_is_refused(self, app_module, admin_session):
        client, csrf = admin_session
        response = client.post("/api/config/import", headers=HEADERS(csrf),
                               json={"file": {"instances": [], "pad": "x" * 1_100_000}})
        assert response.status_code == 413

    def test_needs_the_csrf_token_and_an_admin(self, app_module, admin_session, client):
        _client, csrf = admin_session
        assert admin_session[0].post("/api/config/import", json={"file": {}}).status_code == 403
        anonymous = app_module.app.test_client()
        assert anonymous.post("/api/config/import", headers=HEADERS(csrf), json={"file": {}}).status_code in (302, 401, 403)

    def test_settings_still_saves_after_the_refactor(self, app_module, admin_session):
        client, csrf = admin_session
        response = client.post("/settings", data={
            "csrf_token": csrf, "instance_name_1": "Radarr", "instance_kind_1": "movie",
            "instance_url_1": "http://r:7878", "instance_api_key_1": "k", "instance_enabled_1": "on",
            "user_username_1": "testadmin", "user_role_1": "admin"})
        assert response.status_code == 302
        assert "Radarr" in app_module.APPS


class TestDiagnostics:
    def _text(self, client, hide=True):
        return client.get(f"/api/diagnostics?hide_addresses={1 if hide else 0}").get_json()["text"]

    def test_it_never_contains_a_secret_or_a_name(self, app_module, admin_session):
        client, _csrf = admin_session
        seed(app_module)
        for hide in (True, False):
            text = self._text(client, hide)
            for secret in (KEY, TOKEN, "TMDBSECRET", "1234", "testpassword123", "pbkdf2", "scrypt",
                           app_module.CONFIG["secret_key"], "Kazzyfin", "testadmin"):
                assert secret not in text

    def test_it_describes_the_setup_by_type_and_count(self, app_module, admin_session):
        client, _csrf = admin_session
        seed(app_module)
        text = self._text(client)
        assert "Movies (Radarr-type): 1 (1 enabled)" in text
        assert "TV (Sonarr-type): 1 (0 enabled)" in text
        assert "Jellyfin: 1 (1 enabled)" in text
        assert "Users: 1" in text
        assert "Build fingerprint:" in text

    def test_addresses_are_hidden_by_default(self, app_module, admin_session):
        client, _csrf = admin_session
        seed(app_module)
        app_module.log.warning("GET 192.168.1.99:30015/Sessions -> HTTP 401 (12 ms)")
        app_module.log.warning("GET sonarr.lan:8989/api/v3/health failed: ConnectionError: host='sonarr.lan', port=8989")
        hidden = self._text(client, True)
        assert "192.168.1.99" not in hidden and "sonarr.lan" not in hidden
        assert "jellyfin-1/Sessions -> HTTP 401" in hidden
        assert "sonarr-1" in hidden
        shown = self._text(client, False)
        assert "192.168.1.99:30015/Sessions" in shown

    def test_an_unconfigured_address_is_still_masked(self, app_module, admin_session):
        client, _csrf = admin_session
        app_module.log.warning("GET 10.0.0.7:80/x -> HTTP 500")
        assert "10.0.0.7" not in self._text(client, True)

    def test_recent_warnings_are_listed_even_at_log_level_error(self, app_module, admin_session, monkeypatch):
        client, _csrf = admin_session
        monkeypatch.setenv("LOG_LEVEL", "error")
        app_module.configure_logging()
        app_module.log.warning("something went sideways")
        assert "WARNING something went sideways" in self._text(client)

    def test_debug_lines_are_not_collected(self, app_module, admin_session, monkeypatch):
        client, _csrf = admin_session
        monkeypatch.setenv("LOG_LEVEL", "debug")
        app_module.configure_logging()
        app_module.log.debug("chatty detail")
        assert "chatty detail" not in self._text(client)

    def test_a_secret_in_a_warning_is_scrubbed_again(self, app_module, admin_session):
        client, _csrf = admin_session
        app_module.log.warning("odd failure at http://x/api?apikey=%s", "LEAKYLEAKY")
        assert "LEAKYLEAKY" not in self._text(client)

    def test_the_list_is_capped(self, app_module, admin_session):
        client, _csrf = admin_session
        for n in range(app_module.RECENT_WARNING_LIMIT + 10):
            app_module.log.warning("problem number %d", n)
        text = self._text(client)
        assert "problem number 0" not in text and f"problem number {app_module.RECENT_WARNING_LIMIT + 9}" in text

    def test_the_last_attention_check_is_summarised(self, app_module, admin_session):
        client, _csrf = admin_session
        app_module._ATTENTION_CACHE["data"] = {"summary": {"critical": 2, "warning": 3}, "complete": False}
        assert "2 critical, 3 warning (incomplete" in self._text(client)

    def test_the_handler_is_not_stacked_by_reconfiguring(self, app_module):
        for _ in range(3):
            app_module.configure_logging()
        handlers = [h for h in app_module.log.handlers if getattr(h, "_arrvantage_recent", False)]
        assert len(handlers) == 1
        assert app_module.log.isEnabledFor(logging.WARNING)
