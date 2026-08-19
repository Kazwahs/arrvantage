"""
Tests for authentication, CSRF protection, and login rate limiting.

Flask's test client sets the same REMOTE_ADDR for every request by
default, which conveniently means repeated requests from one test
client accumulate against the same rate-limit bucket - exactly what's
needed to test the lockout behavior.
"""
from werkzeug.security import generate_password_hash


def _create_admin(app_module):
    admin_user = {
        "username": "testadmin",
        "password_hash": generate_password_hash("testpassword123"),
        "role": "admin",
        "permissions": {"requesting": True, "searching": True},
    }
    app_module.CONFIG["users"] = [admin_user]
    app_module.USERS = app_module.CONFIG["users"]
    app_module.save_config(app_module.CONFIG)


class TestLogin:
    def test_no_users_redirects_to_setup(self, app_module, client):
        # A brand-new instance with no accounts yet should route
        # visitors to the setup wizard, not a login form for an
        # account that doesn't exist.
        response = client.get("/login", follow_redirects=False)
        assert response.status_code == 302
        assert "/setup" in response.headers["Location"]

    def test_correct_credentials_logs_in(self, app_module, client):
        _create_admin(app_module)
        login_page = client.get("/login")
        csrf_token = _extract_csrf(login_page.data.decode())

        response = client.post(
            "/login",
            data={"username": "testadmin", "password": "testpassword123", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_wrong_password_rejected(self, app_module, client):
        _create_admin(app_module)
        login_page = client.get("/login")
        csrf_token = _extract_csrf(login_page.data.decode())

        response = client.post(
            "/login",
            data={"username": "testadmin", "password": "wrongpassword", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 200  # re-renders the login form with an error, no redirect
        assert b"Incorrect username or password" in response.data

    def test_protected_route_redirects_when_not_logged_in(self, app_module, client):
        _create_admin(app_module)
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


class TestCsrfProtection:
    def test_post_without_csrf_token_rejected(self, app_module, client):
        _create_admin(app_module)
        # Deliberately omit csrf_token entirely - the login POST
        # should be rejected before it even gets to checking the
        # password.
        response = client.post(
            "/login",
            data={"username": "testadmin", "password": "testpassword123"},
        )
        assert response.status_code == 403

    def test_post_with_wrong_csrf_token_rejected(self, app_module, client):
        _create_admin(app_module)
        client.get("/login")  # establishes a session with a real token

        response = client.post(
            "/login",
            data={"username": "testadmin", "password": "testpassword123", "csrf_token": "not-the-real-token"},
        )
        assert response.status_code == 403

    def test_post_with_correct_csrf_token_accepted(self, app_module, client):
        _create_admin(app_module)
        login_page = client.get("/login")
        csrf_token = _extract_csrf(login_page.data.decode())

        response = client.post(
            "/login",
            data={"username": "testadmin", "password": "testpassword123", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 302  # got past CSRF and authenticated successfully


class TestLoginRateLimiting:
    def test_lockout_after_max_attempts(self, app_module, client):
        _create_admin(app_module)
        login_page = client.get("/login")
        csrf_token = _extract_csrf(login_page.data.decode())

        # Exhaust the attempt budget with wrong passwords.
        for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
            client.post(
                "/login",
                data={"username": "testadmin", "password": "wrongpassword", "csrf_token": csrf_token},
            )

        # The next attempt - even with the CORRECT password - should
        # now be locked out rather than actually checking credentials.
        response = client.post(
            "/login",
            data={"username": "testadmin", "password": "testpassword123", "csrf_token": csrf_token},
        )
        assert b"Too many failed attempts" in response.data

    def test_successful_login_not_blocked_before_limit(self, app_module, client):
        _create_admin(app_module)
        login_page = client.get("/login")
        csrf_token = _extract_csrf(login_page.data.decode())

        # A couple of failures, well under the limit, shouldn't block
        # a subsequent correct login.
        for _ in range(2):
            client.post(
                "/login",
                data={"username": "testadmin", "password": "wrongpassword", "csrf_token": csrf_token},
            )

        response = client.post(
            "/login",
            data={"username": "testadmin", "password": "testpassword123", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 302


def _extract_csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.find(marker) + len(marker)
    end = html.find('"', start)
    return html[start:end]
