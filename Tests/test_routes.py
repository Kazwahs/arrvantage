"""
Route-level tests - mostly access control (who can reach what), since
that's the highest-value thing to lock in at this layer. Deeper
per-integration behavior belongs in its own test file instead (see
test_prowlarr.py for the pattern).
"""
from werkzeug.security import generate_password_hash


def _create_users(app_module):
    admin = {
        "username": "admin",
        "password_hash": generate_password_hash("adminpass123"),
        "role": "admin",
        "permissions": {"requesting": True, "searching": True},
    }
    limited_user = {
        "username": "limiteduser",
        "password_hash": generate_password_hash("userpass123"),
        "role": "user",
        "permissions": {"requesting": False, "searching": False},
    }
    app_module.CONFIG["users"] = [admin, limited_user]
    app_module.USERS = app_module.CONFIG["users"]
    app_module.save_config(app_module.CONFIG)


def _login_as(client, username, password):
    login_page = client.get("/login")
    csrf_token = _extract_csrf(login_page.data.decode())
    client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
    )
    return csrf_token


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.find(marker) + len(marker)
    end = html.find('"', start)
    return html[start:end]


class TestSettingsAccessControl:
    def test_admin_can_reach_settings(self, app_module, client):
        _create_users(app_module)
        _login_as(client, "admin", "adminpass123")

        response = client.get("/settings")
        assert response.status_code == 200

    def test_non_admin_cannot_reach_settings(self, app_module, client):
        _create_users(app_module)
        _login_as(client, "limiteduser", "userpass123")

        response = client.get("/settings", follow_redirects=False)
        # Should not render the actual settings page for a non-admin -
        # either a redirect or a 403, but never a 200 with real content.
        assert response.status_code != 200

    def test_settings_test_endpoint_open_before_any_users_exist(self, app_module, client):
        # No users created yet - this endpoint must be reachable
        # without login during initial setup, since there's no
        # admin account to log in as until the wizard finishes.
        # A GET first (any page) establishes a session with a real
        # CSRF token - this route takes a JSON body, so the token has
        # to go in the header rather than the body, same as the
        # frontend's own fetch() override does it.
        setup_page = client.get("/setup")
        csrf_token = _extract_csrf(setup_page.data.decode())

        response = client.post(
            "/api/settings/test",
            json={"category": "instance", "kind_or_type": "movie", "url": "", "credential": ""},
            headers={"X-CSRF-Token": csrf_token},
        )
        # Should not be a 401/redirect-to-login - it should actually
        # attempt (and likely fail validation, which is fine and
        # different from being blocked outright).
        assert response.status_code != 401

    def test_settings_test_endpoint_requires_admin_once_users_exist(self, app_module, client):
        _create_users(app_module)
        # No login at all this time - but still need a valid CSRF
        # token to get past that check first, so an admin-required
        # 403 is genuinely what's being tested, not a CSRF 403.
        login_page = client.get("/login")
        csrf_token = _extract_csrf(login_page.data.decode())

        response = client.post(
            "/api/settings/test",
            json={"category": "instance", "kind_or_type": "movie", "url": "", "credential": ""},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 403


class TestHomeRequiresLogin:
    def test_home_redirects_when_logged_out(self, app_module, client):
        _create_users(app_module)
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_home_reachable_when_logged_in(self, app_module, client):
        _create_users(app_module)
        _login_as(client, "admin", "adminpass123")
        response = client.get("/")
        assert response.status_code == 200
