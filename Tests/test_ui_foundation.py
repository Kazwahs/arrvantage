"""
Tests for the shared UI foundation: the static stylesheet/font files, the
asset_url cache-busting helper, every page linking the shared stylesheet,
and the few bits of behaviour the dashboard template decides server-side
(the disk-space warning tiers and the initially-hidden A-Z rail).

These are deliberately about structure, not looks - whether a page "looks
right" is checked by eye, but "does every page still load the stylesheet"
and "does a nearly-full disk say so in words" are cheap to pin down.
"""
import re

import pytest


FAKE_RADARR = {
    "url": "http://radarr.test:7878",
    "api_key": "fake-key",
    "library_kind": "movie",
    "enabled": True,
    "color": "#e8a33d",
    "seerr_media_type": None,
}


def _stub_fetchers(app_module, monkeypatch, disk_items):
    """Replaces every network-facing fetch the index route makes, so
    rendering the dashboard needs no HTTP mocking at all."""
    monkeypatch.setattr(app_module, "fetch_queue", lambda n, c: {"error": None, "queue_items": []})
    monkeypatch.setattr(app_module, "fetch_history", lambda n, c: {"error": None, "history_items": []})
    monkeypatch.setattr(app_module, "fetch_library", lambda n, c: {
        "error": None, "library_items": [], "status_options": [], "genre_options": [],
    })
    monkeypatch.setattr(app_module, "fetch_health", lambda n, c: {"error": None, "health_items": []})
    monkeypatch.setattr(app_module, "fetch_diskspace", lambda n, c: {
        "error": None, "diskspace_items": disk_items,
    })
    monkeypatch.setattr(app_module, "fetch_seerr_requests", lambda r, t: {"error": None, "request_items": []})
    monkeypatch.setattr(app_module, "fetch_collections", lambda c, o: [])
    app_module.APPS["Radarr"] = dict(FAKE_RADARR)


def _disk(path, percent_free):
    return {
        "path": path, "label": "", "free_human": "1 TB",
        "total_human": "10 TB", "percent_free": percent_free,
    }


class TestStaticAssets:
    @pytest.mark.parametrize("path, content_type_part", [
        ("/static/css/app.css", "text/css"),
        ("/static/css/dashboard.css", "text/css"),
        ("/static/fonts/Geist-Variable.woff2", "font/woff2"),
        ("/static/fonts/GeistMono-Variable.woff2", "font/woff2"),
        ("/static/fonts/OFL.txt", "text/plain"),
    ])
    def test_file_is_served(self, client, path, content_type_part):
        response = client.get(path)
        assert response.status_code == 200
        assert content_type_part in response.headers["Content-Type"]

    def test_woff2_files_are_real_fonts(self, client):
        # woff2 files start with the magic bytes "wOF2" - catches an
        # accidentally-committed HTML error page or an empty file.
        for name in ("Geist-Variable.woff2", "GeistMono-Variable.woff2"):
            assert client.get(f"/static/fonts/{name}").data[:4] == b"wOF2"

    def test_font_licence_is_bundled(self, client):
        # SIL OFL requires the licence text to travel with the font.
        assert "SIL OPEN FONT LICENSE" in client.get("/static/fonts/OFL.txt").data.decode().upper()

    def test_shared_stylesheet_defines_test_btn(self, client):
        # .test-btn is used by the dashboard, settings and setup. It was
        # once missing from the dashboard entirely, leaving its buttons
        # unstyled - so it lives in the shared file now.
        css = client.get("/static/css/app.css").data.decode()
        assert ".test-btn" in css

    def test_shared_stylesheet_defines_design_tokens(self, client):
        css = client.get("/static/css/app.css").data.decode()
        for token in ("--bg:", "--panel:", "--accent:", "--error:", "--warning:", "--focus:", "--font-sans:", "--error-text:"):
            assert token in css

    def test_font_urls_in_css_resolve(self, client):
        css = client.get("/static/css/app.css").data.decode()
        for rel in re.findall(r'url\("\.\./fonts/([^"]+)"\)', css):
            assert client.get(f"/static/fonts/{rel}").status_code == 200


class TestAssetUrl:
    def test_asset_url_adds_version_query(self, app_module):
        with app_module.app.test_request_context("/"):
            url = app_module.app.jinja_env.globals.get("asset_url")
            # The helper is injected by a context processor rather than
            # registered as a global, so go through the processor.
            ctx = {}
            for fn in app_module.app.template_context_processors[None]:
                ctx.update(fn())
            url = ctx["asset_url"]("css/app.css")
        assert url.startswith("/static/css/app.css?v=")
        assert re.search(r"\?v=\d+$", url)

    def test_asset_url_for_missing_file_does_not_raise(self, app_module):
        with app_module.app.test_request_context("/"):
            ctx = {}
            for fn in app_module.app.template_context_processors[None]:
                ctx.update(fn())
            assert ctx["asset_url"]("css/nope.css").endswith("?v=0")


class TestPagesLinkSharedStylesheet:
    def test_login_page(self, app_module, client):
        # /login redirects to the wizard until an account exists.
        app_module.CONFIG["users"] = [{"username": "u", "password_hash": "x", "role": "admin", "permissions": {}}]
        app_module.USERS = app_module.CONFIG["users"]
        html = client.get("/login").data.decode()
        assert "/static/css/app.css?v=" in html
        assert "fonts/Geist-Variable.woff2" in html

    def test_setup_page(self, client):
        # No users exist on a fresh app, so /setup renders instead of redirecting.
        html = client.get("/setup").data.decode()
        assert "/static/css/app.css?v=" in html

    def test_settings_page(self, admin_session):
        client, _ = admin_session
        html = client.get("/settings").data.decode()
        assert "/static/css/app.css?v=" in html

    def test_dashboard_page_links_both_stylesheets(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        _stub_fetchers(app_module, monkeypatch, [])
        html = client.get("/").data.decode()
        assert "/static/css/app.css?v=" in html
        assert "/static/css/dashboard.css?v=" in html

    def test_pages_no_longer_duplicate_the_token_block(self, admin_session):
        # The :root colour block used to be copy-pasted into every page.
        client, _ = admin_session
        html = client.get("/settings").data.decode()
        assert "--panel: #151b23" not in html


class TestDashboardMarkup:
    def test_alpha_rail_starts_hidden(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        _stub_fetchers(app_module, monkeypatch, [])
        html = client.get("/").data.decode()
        assert re.search(r'<div class="alpha-index hidden" id="alpha-index"', html)

    def test_dashboard_has_landmarks(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        _stub_fetchers(app_module, monkeypatch, [])
        html = client.get("/").data.decode()
        assert "<main" in html
        assert "<nav" in html


class TestDiskSpaceTiers:
    def _render(self, app_module, admin_session, monkeypatch, items):
        client, _ = admin_session
        _stub_fetchers(app_module, monkeypatch, items)
        # Instance panels (incl. disk space) are lazy-loaded fragments now,
        # not part of the main page.
        return client.get("/fragment/instance/Radarr").data.decode()

    def test_healthy_disk_has_no_warning(self, app_module, admin_session, monkeypatch):
        html = self._render(app_module, admin_session, monkeypatch, [_disk("/mnt/media", 40)])
        assert "Critically low" not in html
        assert "Low &middot;" not in html
        assert 'class="free"' in html

    def test_low_disk_says_low_in_words(self, app_module, admin_session, monkeypatch):
        html = self._render(app_module, admin_session, monkeypatch, [_disk("/mnt/media", 12)])
        assert "Low &middot;" in html
        assert "is-low" in html
        assert "(12%)" in html
        assert "Critically low" not in html

    def test_critical_disk_says_critically_low(self, app_module, admin_session, monkeypatch):
        html = self._render(app_module, admin_session, monkeypatch, [_disk("/mnt/media", 4)])
        assert "Critically low &middot;" in html
        assert "is-critical" in html

    @pytest.mark.parametrize("percent, expected", [
        (15, ""), (14, "low"), (7, "low"), (6, "critical"), (0, "critical"),
    ])
    def test_tier_boundaries(self, app_module, admin_session, monkeypatch, percent, expected):
        html = self._render(app_module, admin_session, monkeypatch, [_disk("/mnt/media", percent)])
        assert ('class="free is-low"' in html) == (expected == "low")
        assert ('class="free is-critical"' in html) == (expected == "critical")

    def test_unknown_percent_does_not_raise_or_warn(self, app_module, admin_session, monkeypatch):
        html = self._render(app_module, admin_session, monkeypatch, [_disk("/mnt/media", None)])
        assert "Low &middot;" not in html
        assert "progressbar" not in html

    def test_bar_is_exposed_as_progressbar(self, app_module, admin_session, monkeypatch):
        html = self._render(app_module, admin_session, monkeypatch, [_disk("/mnt/media", 12)])
        assert 'role="progressbar"' in html
        assert 'aria-valuenow="88"' in html
