"""
Shared pytest fixtures.

The app module loads/writes config.json at import time (see CONFIG_DIR
near the top of app.py), so importing it naively in a test run would
create a stray config.json in whatever directory pytest runs from.
This redirects CONFIG_DIR to a fresh temp directory *before* app.py is
ever imported, so every test run starts from a clean, empty config and
never touches real project files.
"""
import os
import sys
import importlib

import pytest


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    """
    Imports (or re-imports) the app module with CONFIG_DIR pointed at
    a fresh temp directory, so each test gets an isolated, empty
    config rather than sharing state with other tests or the real
    project directory.
    """
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    # If app.py was already imported by an earlier test, its
    # module-level CONFIG/APPS/etc were built against a different
    # CONFIG_DIR - force a genuine re-import so this test gets fresh
    # state built against *this* test's temp directory.
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    return module


@pytest.fixture
def client(app_module):
    """A Flask test client, with Flask's own error catching disabled
    so a bug in a route surfaces as a real exception in the test
    output instead of a generic 500 response."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client


@pytest.fixture
def admin_session(app_module, client):
    """
    Creates one admin user directly (bypassing the setup wizard, since
    we're testing routes that assume an account already exists) and
    returns a logged-in test client plus the CSRF token that client's
    session now holds - most POST routes need both to succeed.
    """
    from werkzeug.security import generate_password_hash

    admin_user = {
        "username": "testadmin",
        "password_hash": generate_password_hash("testpassword123"),
        "role": "admin",
        "permissions": {"requesting": True, "searching": True},
    }
    app_module.CONFIG["users"] = [admin_user]
    app_module.USERS = app_module.CONFIG["users"]
    app_module.save_config(app_module.CONFIG)

    # A GET first, to establish a session and receive a CSRF token,
    # exactly like a real browser would before submitting the login form.
    login_page = client.get("/login")
    csrf_token = _extract_csrf_token(login_page.data.decode())

    response = client.post(
        "/login",
        data={"username": "testadmin", "password": "testpassword123", "csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert response.status_code == 302, "login should redirect to / on success"

    # The token is tied to the session, not the page - the same value
    # from login.html's hidden input remains valid for every
    # subsequent POST in this session, including on index.html (which
    # embeds it as a JS variable instead of a hidden input, so it
    # can't be re-extracted the same way anyway).
    return client, csrf_token


def _extract_csrf_token(html: str) -> str:
    """Pulls the CSRF token out of a rendered page's hidden input or
    meta-equivalent - all of this app's forms embed it the same way."""
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    if start == -1:
        raise AssertionError("no csrf_token field found in page - fixture assumption may be stale")
    start += len(marker)
    end = html.find('"', start)
    return html[start:end]
