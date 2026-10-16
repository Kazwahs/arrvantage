"""
Per-user, arrangeable Home layout: normalising whatever was saved, the
save/reset endpoint, how the main page embeds it, and that saving
Settings doesn't wipe it.
"""
import json
import re

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731

DEFAULT = ["attention", "libraries", "mediaservers", "indexers", "transcoders"]


def embedded_layout(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="home-layout-data">(.*?)</script>', html, re.S)
    assert match, "main page should embed the layout"
    return json.loads(match.group(1))


class TestNormalizeHomeLayout:
    def test_default_when_nothing_saved(self, app_module):
        assert app_module.normalize_home_layout(None) == {"order": DEFAULT, "hidden": []}

    def test_keeps_a_valid_order(self, app_module):
        layout = app_module.normalize_home_layout(
            {"order": ["transcoders", "libraries", "attention", "indexers", "mediaservers"], "hidden": ["indexers"]})
        assert layout["order"] == ["transcoders", "libraries", "attention", "indexers", "mediaservers"]
        assert layout["hidden"] == ["indexers"]

    def test_drops_unknown_and_repeated_names(self, app_module):
        layout = app_module.normalize_home_layout(
            {"order": ["libraries", "bogus", "libraries", 7, None], "hidden": ["nope", "libraries", "libraries"]})
        assert layout["order"] == ["libraries", "attention", "mediaservers", "indexers", "transcoders"]
        assert layout["hidden"] == ["libraries"]

    def test_missing_panels_are_appended_in_default_order(self, app_module):
        layout = app_module.normalize_home_layout({"order": ["indexers"]})
        assert layout["order"] == ["indexers", "attention", "libraries", "mediaservers", "transcoders"]

    def test_garbage_input_falls_back_to_default(self, app_module):
        for junk in ("text", 5, [], {"order": "libraries", "hidden": 3}, {"order": None}):
            assert app_module.normalize_home_layout(junk) == {"order": DEFAULT, "hidden": []}

    def test_hidden_list_follows_display_order(self, app_module):
        layout = app_module.normalize_home_layout(
            {"order": ["indexers", "libraries"], "hidden": ["libraries", "indexers"]})
        assert layout["hidden"] == ["indexers", "libraries"]


class TestHomeLayoutEndpoint:
    def test_requires_login(self, client):
        response = client.post("/api/home/layout", json={"order": DEFAULT})
        assert response.status_code in (302, 401, 403)

    def test_rejects_missing_csrf_token(self, admin_session):
        client, _ = admin_session
        response = client.post("/api/home/layout", json={"order": DEFAULT})
        assert response.status_code == 403

    def test_saves_and_returns_the_normalised_layout(self, app_module, admin_session):
        client, csrf = admin_session
        response = client.post(
            "/api/home/layout", headers=HEADERS(csrf),
            json={"order": ["indexers", "attention", "bogus"], "hidden": ["libraries"]})
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["layout"]["order"] == ["indexers", "attention", "libraries", "mediaservers", "transcoders"]
        assert body["layout"]["hidden"] == ["libraries"]

        saved = json.load(open(app_module.CONFIG_PATH))
        assert saved["users"][0]["home_layout"] == body["layout"]

    def test_rejects_non_object_bodies(self, admin_session):
        client, csrf = admin_session
        response = client.post("/api/home/layout", headers=HEADERS(csrf), json=["libraries"])
        assert response.status_code == 400

    def test_reset_clears_the_saved_layout(self, app_module, admin_session):
        client, csrf = admin_session
        client.post("/api/home/layout", headers=HEADERS(csrf), json={"order": ["transcoders"]})
        response = client.post("/api/home/layout", headers=HEADERS(csrf), json={"reset": True})
        assert response.get_json()["layout"] == {"order": DEFAULT, "hidden": []}
        assert "home_layout" not in json.load(open(app_module.CONFIG_PATH))["users"][0]

    def test_each_user_has_their_own_layout(self, app_module, admin_session):
        from werkzeug.security import generate_password_hash
        client, csrf = admin_session
        app_module.CONFIG["users"].append({
            "username": "guest", "password_hash": generate_password_hash("guestpassword1"),
            "role": "user", "permissions": {"requesting": False, "searching": False},
        })
        app_module.save_config(app_module.CONFIG)
        client.post("/api/home/layout", headers=HEADERS(csrf), json={"order": ["transcoders"], "hidden": ["attention"]})

        other = app_module.app.test_client()
        page = other.get("/login").data.decode()
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        other.post("/login", data={"username": "guest", "password": "guestpassword1", "csrf_token": token})
        assert embedded_layout(other.get("/").data.decode()) == {"order": DEFAULT, "hidden": []}


class TestMainPageLayout:
    def test_default_layout_is_embedded(self, admin_session):
        client, _ = admin_session
        assert embedded_layout(client.get("/").data.decode()) == {"order": DEFAULT, "hidden": []}

    def test_saved_layout_is_embedded_on_the_next_load(self, admin_session):
        client, csrf = admin_session
        client.post("/api/home/layout", headers=HEADERS(csrf),
                    json={"order": ["libraries", "attention"], "hidden": ["transcoders"]})
        layout = embedded_layout(client.get("/").data.decode())
        assert layout["order"][:2] == ["libraries", "attention"]
        assert layout["hidden"] == ["transcoders"]

    def test_every_panel_exists_in_the_markup(self, admin_session):
        client, _ = admin_session
        html = client.get("/").data.decode()
        for key in DEFAULT:
            assert f'data-panel="{key}"' in html


class TestSettingsKeepsLayout:
    def test_parse_users_carries_the_layout_forward(self, app_module):
        from werkzeug.security import generate_password_hash
        layout = {"order": ["indexers"], "hidden": []}
        existing = [{
            "username": "zak", "password_hash": generate_password_hash("secretpass1"),
            "role": "admin", "permissions": {"requesting": True, "searching": True},
            "home_layout": layout,
        }]
        form = {"user_username_0": "zak", "user_role_0": "admin", "user_password_0": ""}
        users = app_module.parse_users(form, existing)
        assert users[0]["home_layout"] == layout

    def test_users_without_a_layout_stay_without_one(self, app_module):
        from werkzeug.security import generate_password_hash
        existing = [{
            "username": "zak", "password_hash": generate_password_hash("secretpass1"),
            "role": "admin", "permissions": {"requesting": True, "searching": True},
        }]
        form = {"user_username_0": "zak", "user_role_0": "admin", "user_password_0": ""}
        assert "home_layout" not in app_module.parse_users(form, existing)[0]
