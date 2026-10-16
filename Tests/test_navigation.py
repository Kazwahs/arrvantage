"""
Navigation structure: the sidebar, the shared top-bar tools, and the
small accessibility guarantees that go with them.
"""
import os
import re


def make_page(app_module, admin_session):
    """Configures one of each sidebar category and returns the rendered page."""
    client, _ = admin_session
    cfg = app_module.CONFIG
    cfg["instances"] = [{"name": "Radarr", "kind": "movie", "url": "radarr.lan:7878", "api_key": "k", "color": "#e8a33d"}]
    cfg["downloaders"] = [{"name": "qBittorrent", "type": "qbittorrent", "url": "http://qb.lan", "enabled": True}]
    cfg["media_servers"] = [{"name": "Plex", "type": "plex", "url": "http://plex.lan:32400", "credential": "x", "enabled": True}]
    cfg["indexers"] = [{"name": "Prowlarr", "type": "prowlarr", "url": "http://prowlarr.lan", "api_key": "k", "enabled": True}]
    cfg["transcoders"] = [{"name": "Tdarr", "type": "tdarr", "url": "http://tdarr.lan", "enabled": True}]
    app_module.APPS = app_module.build_apps(cfg)
    app_module.MEDIA_SERVERS = cfg["media_servers"]
    app_module.DOWNLOADERS = cfg["downloaders"]
    app_module.INDEXERS = cfg["indexers"]
    app_module.TRANSCODERS = cfg["transcoders"]
    return client.get("/").data.decode()


class TestExternalUrlFilter:
    def test_adds_http_when_there_is_no_scheme(self, app_module):
        assert app_module.external_url("radarr.lan:7878") == "http://radarr.lan:7878"

    def test_keeps_http_and_https_as_they_are(self, app_module):
        assert app_module.external_url("https://x.example/a") == "https://x.example/a"
        assert app_module.external_url("HTTP://x.example") == "HTTP://x.example"

    def test_a_javascript_url_is_never_left_as_a_javascript_scheme(self, app_module):
        assert not app_module.external_url("javascript:alert(1)").lower().startswith("javascript:")

    def test_blank_and_none_give_an_empty_string(self, app_module):
        assert app_module.external_url("") == ""
        assert app_module.external_url(None) == ""


class TestSidebar:
    def test_open_links_are_real_links_beside_the_tab_not_inside_a_button(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        # A link nested in a button is invalid and unreachable by some assistive tech.
        for button in re.findall(r"<button\b.*?</button>", html, re.S):
            assert "<a " not in button, button[:120]
        assert 'class="tab-open-link" href="http://radarr.lan:7878"' in html
        assert 'aria-label="Open Plex directly"' in html

    def test_every_open_link_opens_in_a_new_tab_safely(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        for tag in re.findall(r'<a class="tab-open-link"[^>]*>', html):
            assert 'target="_blank"' in tag and 'rel="noopener"' in tag

    def test_every_category_gets_a_tab(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        for name in ("Radarr", "qBittorrent", "Plex", "Prowlarr", "Tdarr"):
            assert f'class="nav-item' in html and name in html
        assert html.count('class="nav-item') >= 5

    def test_home_is_a_real_button_with_a_name(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        assert re.search(r'<button type="button" class="sidebar-logo"[^>]*aria-label="Home"', html)
        assert 'class="sidebar-logo" role="button"' not in html

    def test_skip_link_points_at_the_main_landmark(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        assert 'class="skip-link" href="#main-content"' in html
        assert re.search(r'<main class="main" id="main-content" tabindex="-1">', html)


class TestTopbars:
    def test_every_view_shows_who_is_signed_in_and_a_log_out_link(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        topbars = html.count('<div class="topbar">')
        assert topbars == 6  # Home, instances, downloaders, indexers, media servers, transcoders
        assert html.count('class="logout-link"') == topbars
        assert html.count('class="user-name">testadmin<') == topbars

    def test_settings_link_is_admin_only(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        assert html.count('href="/settings"') == html.count('<div class="topbar">')

    def test_search_boxes_have_accessible_names(self, app_module, admin_session):
        html = make_page(app_module, admin_session)
        assert 'id="global-search"' in html and 'aria-label="Search this tab"' in html
        assert 'aria-label="Search across all your libraries"' in html


class TestLibraryFilters:
    def test_filter_controls_are_labelled(self, app_module):
        # The partial is rendered by the lazy fragment route (covered elsewhere);
        # here just check its source gives every control an accessible name.
        path = os.path.join(os.path.dirname(app_module.__file__), "templates", "partials", "instance_panels.html")
        source = open(path).read()
        for sid in ("status-filter-", "genre-filter-", "sort-{{", "view-mode-"):
            tag = re.search(r'<select id="%s[^>]*>' % re.escape(sid), source)
            assert tag and "aria-label=" in tag.group(0), sid
        assert re.search(r'class="sort-dir-btn"[^>]*aria-label=', source, re.S)
