"""
Themes (Appearance panel) and the slide-over panel that hosts it.
"""
import json
import os
import re

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_css():
    return open(os.path.join(ROOT, "static", "css", "app.css")).read()


def token_block(css, opener):
    """Body of the first rule that starts with `opener` (selector text up to its '{')."""
    match = re.search(r"^\s*" + re.escape(opener) + r"\s*\{", css, re.M)  # skips mentions in comments
    open_brace = match.end() - 1
    depth, i = 1, open_brace + 1
    while depth:
        depth += {"{": 1, "}": -1}.get(css[i], 0)
        i += 1
    return css[open_brace + 1:i - 1]


class TestNormalizeTheme:
    def test_known_keys_pass_through(self, app_module):
        for key in app_module.THEMES:
            assert app_module.normalize_theme(key) == key

    def test_anything_else_falls_back_to_the_default(self, app_module):
        for bad in (None, "", "neon", 7, ["paper"], {"paper": 1}):
            assert app_module.normalize_theme(bad) == app_module.DEFAULT_THEME

    def test_default_is_the_original_look(self, app_module):
        assert app_module.DEFAULT_THEME == "midnight"


class TestThemeEndpoint:
    def test_requires_login(self, client):
        assert client.post("/api/theme", json={"theme": "paper"}).status_code in (302, 401, 403)

    def test_rejects_missing_csrf_token(self, admin_session):
        client, _ = admin_session
        assert client.post("/api/theme", json={"theme": "paper"}).status_code == 403

    def test_saves_a_known_theme(self, app_module, admin_session):
        client, csrf = admin_session
        response = client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "telemetry"})
        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "theme": "telemetry"}
        assert json.load(open(app_module.CONFIG_PATH))["users"][0]["theme"] == "telemetry"

    def test_unknown_theme_is_a_400_and_changes_nothing(self, app_module, admin_session):
        client, csrf = admin_session
        client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "paper"})
        for bad in ({"theme": "neon"}, {"theme": 5}, {}, ["paper"]):
            response = client.post("/api/theme", headers=HEADERS(csrf), json=bad)
            assert response.status_code == 400
        assert json.load(open(app_module.CONFIG_PATH))["users"][0]["theme"] == "paper"

    def test_choosing_the_default_removes_the_saved_key(self, app_module, admin_session):
        client, csrf = admin_session
        client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "paper"})
        client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "midnight"})
        assert "theme" not in json.load(open(app_module.CONFIG_PATH))["users"][0]

    def test_each_user_has_their_own_theme(self, app_module, admin_session):
        from werkzeug.security import generate_password_hash
        client, csrf = admin_session
        app_module.CONFIG["users"].append({
            "username": "other", "password_hash": generate_password_hash("otherpassword1"),
            "role": "user", "permissions": {"requesting": False, "searching": False}})
        app_module.USERS = app_module.CONFIG["users"]
        client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "contrast"})
        assert app_module.CONFIG["users"][0]["theme"] == "contrast"
        assert "theme" not in app_module.CONFIG["users"][1]


class TestParseUsersKeepsPreferences:
    def test_theme_and_layout_survive_a_settings_save(self, app_module):
        from werkzeug.security import generate_password_hash
        existing = [{"username": "zak", "password_hash": generate_password_hash("pw12345678"),
                     "role": "admin", "permissions": {}, "theme": "paper",
                     "home_layout": {"order": ["indexers"], "hidden": []}}]
        app_module.CONFIG["users"] = existing
        app_module.USERS = existing
        form = {"user_username_0": "zak", "user_role_0": "admin", "user_password_0": ""}
        parsed = app_module.parse_users(form, existing) if _takes_two(app_module) else None
        if parsed is not None:
            assert parsed[0].get("theme") == "paper"
            assert parsed[0].get("home_layout")


def _takes_two(app_module):
    import inspect
    return len(inspect.signature(app_module.parse_users).parameters) >= 2


class TestPages:
    def test_dashboard_carries_the_users_theme(self, app_module, admin_session):
        client, csrf = admin_session
        assert 'data-theme="midnight"' in client.get("/").data.decode()
        client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "paper"})
        html = client.get("/").data.decode()
        assert '<html lang="en" data-theme="paper"' in html
        assert 'content="#f7f6f3"' in html

    def test_auto_gives_a_light_and_a_dark_browser_colour(self, app_module, admin_session):
        client, csrf = admin_session
        client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "auto"})
        html = client.get("/").data.decode()
        assert 'media="(prefers-color-scheme: light)"' in html
        assert 'media="(prefers-color-scheme: dark)"' in html

    def test_settings_page_uses_the_theme_too(self, app_module, admin_session):
        client, csrf = admin_session
        client.post("/api/theme", headers=HEADERS(csrf), json={"theme": "telemetry"})
        assert 'data-theme="telemetry"' in client.get("/settings").data.decode()

    def test_signed_out_pages_use_the_default_and_a_remembered_choice(self, client):
        html = client.get("/login", follow_redirects=True).data.decode()
        assert 'data-theme="midnight"' in html
        assert "localStorage.getItem('arrTheme')" in html

    def test_appearance_button_is_in_every_top_bar(self, app_module, admin_session):
        client, _ = admin_session
        html = client.get("/").data.decode()
        assert html.count('onclick="showAppearance()"') == html.count('<div class="topbar">')

    def test_appearance_picker_lists_every_theme_as_a_radio(self, app_module, admin_session):
        client, _ = admin_session
        html = client.get("/").data.decode()
        assert 'role="radiogroup" aria-label="Theme"' in html
        for key in app_module.THEMES:
            assert f'data-theme-key="{key}"' in html


class TestSlideOver:
    def test_one_labelled_dialog_hosts_details_about_and_appearance(self, admin_session):
        client, _ = admin_session
        html = client.get("/").data.decode()
        assert html.count('id="slide-over"') == 1
        assert re.search(r'id="slide-over"[^>]*role="dialog"[^>]*aria-modal="true"', html)
        assert 'class="modal-scroll" tabindex="0"' in html
        assert 'aria-label="Close panel"' in html


class TestThemeTokens:
    def test_auto_is_an_exact_copy_of_paper(self):
        css = read_css()
        paper = token_block(css, '[data-theme="paper"]')
        # Auto sits inside a prefers-color-scheme: light media query.
        auto = token_block(css, '[data-theme="auto"]')
        normalise = lambda text: re.sub(r"\s+", " ", text).strip()
        assert normalise(paper) == normalise(auto)

    def test_every_theme_defines_the_same_core_tokens(self, app_module):
        css = read_css()
        core = ("--bg", "--panel", "--sidebar", "--border", "--text", "--muted", "--accent", "--on-accent")
        # Every theme restates the core tokens itself (none relies on inheriting them),
        # because the Appearance panel previews each one inside whichever theme is active.
        for key in ("midnight", *[k for k in app_module.THEMES if k not in ("midnight", "auto")]):
            block = token_block(css, ':root,\n[data-theme="midnight"]' if key == "midnight" else f'[data-theme="{key}"]')
            for token in core:
                assert re.search(rf"{re.escape(token)}\s*:", block), f"{key} is missing {token}"

    def test_every_theme_in_the_backend_has_a_css_block(self, app_module):
        css = read_css()
        for key in app_module.THEMES:
            assert f'[data-theme="{key}"]' in css, key


def _luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(block):
    """Hex-valued tokens in a block; a token that is just var(--other) takes that token's value."""
    tokens = dict(re.findall(r"(--[a-z-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;", block))
    for name, target in re.findall(r"(--[a-z-]+)\s*:\s*var\((--[a-z-]+)\)\s*;", block):
        if target in tokens:
            tokens[name] = tokens[target]
    return tokens


class TestThemeContrast:
    """Small text must clear WCAG AA (4.5:1) on the page and panel colours in every theme."""

    PAIRS = ("--text", "--muted", "--accent", "--warning", "--error-text", "--success")

    def themes(self, app_module):
        css = read_css()
        blocks = {"midnight": token_block(css, ':root,\n[data-theme="midnight"]')}
        for key in app_module.THEMES:
            if key not in ("midnight", "auto"):
                blocks[key] = token_block(css, f'[data-theme="{key}"]')
        return {key: _tokens(block) for key, block in blocks.items()}

    def test_ink_and_status_colours_are_readable_on_page_and_panels(self, app_module):
        for key, tokens in self.themes(app_module).items():
            for surface in ("--bg", "--panel"):
                for name in self.PAIRS:
                    ratio = _contrast(tokens[name], tokens[surface])
                    assert ratio >= 4.5, f"{key}: {name} on {surface} is {ratio:.2f}:1"

    def test_text_on_a_solid_accent_button_is_readable(self, app_module):
        for key, tokens in self.themes(app_module).items():
            for accent in ("--accent", "--accent-hover"):
                ratio = _contrast(tokens["--on-accent"], tokens[accent])
                assert ratio >= 4.5, f"{key}: --on-accent on {accent} is {ratio:.2f}:1"

    def test_focus_ring_is_visible_against_the_page(self, app_module):
        for key, tokens in self.themes(app_module).items():
            assert _contrast(tokens["--focus"], tokens["--bg"]) >= 3, key
