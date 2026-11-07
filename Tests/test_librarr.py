"""
Librarr as an instance kind: its Queue, History, System and Library tabs, the
Home libraries card, the details window, Retry, the move from Downloaders in
old configs, and manga search + send.

The sample bodies are shaped like answers captured from a real Librarr 1.5.0
server (the download job, activity event, library file and search result fields).
"""
import threading

import requests

BASE = "http://librarr.test:5050"
HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731

HEALTH = {"status": "ok", "version": "1.5.0", "library_items": 134, "sources_enabled": 13, "uptime_seconds": 78256,
          "sources": ["annas", "prowlarr", "mangadex"]}
DOWNLOADS = {"downloads": [
    {"source": "mangadex", "title": "One Piece", "status": "error", "job_id": "fc92aacb",
     "error": "no download link found in HTML response", "max_retries": 2},
]}
ACTIVITY = {"events": [
    {"id": 15, "event_type": "torrent_import", "title": "Attack on Titan (2012-2021) (Digital)", "detail": "Imported manga from torrent",
     "job_id": "de8da1", "timestamp": "2026-11-06T21:48:03-08:00"},
    {"id": 14, "event_type": "download_start", "title": "Attack on Titan", "detail": "", "job_id": "", "timestamp": "2026-11-06T21:43:50-08:00"},
    {"id": 8, "event_type": "nzb_import", "title": "Naruto Vol.1", "detail": "", "job_id": "1742", "timestamp": "2026-11-06T14:53:55-08:00"},
    {"id": 11, "event_type": "login", "title": "zak", "detail": "User logged in", "job_id": "", "timestamp": "2026-11-06T21:36:17-08:00"},
    {"id": 12, "event_type": "settings_changed", "title": "", "detail": "x", "job_id": "", "timestamp": "2026-11-06T21:37:17-08:00"},
], "limit": 50, "offset": 0, "total": 15}
LIBRARY_PAGE_1 = {"items": [
    {"id": 162, "title": "Claymore", "author": "Norihiro Yagi", "file_format": "cbz", "file_path": "/manga/Claymore/Claymore v02.cbz",
     "file_size": 234935119, "added_at": "2026-11-07T12:36:29-08:00", "source": "torrent", "media_type": "manga", "original_path": "/downloads/c"},
    {"id": 161, "title": "Claymore", "author": "Norihiro Yagi", "file_format": "cbz", "file_path": "/manga/Claymore/Claymore v01.cbz",
     "file_size": 1500, "added_at": "2026-11-06T09:00:00-08:00", "source": "torrent", "media_type": "manga"},
    {"id": 170, "title": "Akira", "file_format": "epub", "file_path": "C:\\manga\\Akira.epub", "file_size": 2 * 1024 ** 3,
     "added_at": "2026-11-01T00:00:00-08:00", "source": "usenet", "media_type": "manga"},
], "page": 1, "pages": 2, "total": 4}
LIBRARY_PAGE_2 = {"items": [
    {"id": 171, "title": "claymore ", "file_format": "cbz", "file_path": "/manga/Claymore/Claymore v03.cbz", "file_size": 10,
     "added_at": "2026-11-02T00:00:00-08:00", "source": "torrent", "media_type": "manga"},
], "page": 2, "pages": 2, "total": 4}
SEARCH = {
    "results": [
        {"source": "mangadex", "title": "Naruto", "size_human": "700 chapters (completed)", "score": 72, "in_library": False,
         "media_type": "manga", "source_id": "mangadex-6b1e", "url": "https://mangadex.test/title/6b1e", "cover_url": "https://covers.test/x.jpg"},
        {"source": "prowlarr_manga", "title": "Naruto v01-v72", "indexer": "1337x", "size_human": "17.4 GB", "seeders": 26,
         "leechers": 2, "score": 75, "in_library": False, "download_protocol": "torrent",
         "download_url": "http://prowlarr.test/dl/1?apikey=INDEXER-SECRET", "guid": "https://x.test/g"},
        {"source": "nyaa_manga", "title": "Boruto", "indexer": "Nyaa", "size_human": "1.9 GiB", "seeders": 4, "score": 70,
         "in_library": True, "library_title": "Boruto (owned)", "info_hash": "abc", "media_type": "manga"},
    ],
    "sources": [{"name": "mangadex", "label": "MangaDex", "download_type": "direct"}],
    "search_time_ms": 30007, "metadata": {"title": "Naruto 33"},
}


def add_librarr(app_module, name="Librarr", enabled=True):
    app_module.CONFIG.setdefault("instances", []).append(
        {"name": name, "kind": "librarr", "url": BASE, "api_key": "k", "color": "#a78bfa", "enabled": enabled})
    app_module.apply_config(app_module.CONFIG)
    return app_module.APPS[name]


def mock_library(requests_mock, *pages):
    requests_mock.get(f"{BASE}/api/library/manga", [{"json": body} for body in pages])


class InlineThread:
    """Runs the target straight away, so a search finishes before the test goes on."""
    def __init__(self, target=None, args=(), kwargs=None, **_):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class TestKindAndMove:
    def test_it_is_an_instance_kind_with_its_own_backend(self, app_module):
        config = add_librarr(app_module)
        assert app_module.KIND_META["librarr"]["label"] == "Manga and books (Librarr)"
        assert config["backend"] == "librarr" and config["seerr_media_type"] is None and config["library_kind"] == "librarr"
        assert "librarr" not in app_module.DOWNLOADER_TYPES

    def test_an_old_downloader_entry_moves_to_instances_with_everything_kept(self, app_module):
        cfg = {"instances": [{"name": "Radarr", "kind": "movie", "url": "http://r", "api_key": "a", "color": "#111111", "enabled": True}],
               "downloaders": [{"name": "Librarr", "type": "librarr", "url": BASE, "api_key": "k", "color": "#a78bfa", "enabled": False},
                               {"name": "Sab", "type": "sabnzbd", "url": "http://s", "api_key": "s", "enabled": True}]}
        assert app_module.migrate_librarr_downloaders(cfg) is True
        assert cfg["downloaders"] == [{"name": "Sab", "type": "sabnzbd", "url": "http://s", "api_key": "s", "enabled": True}]
        assert cfg["instances"][1] == {"name": "Librarr", "kind": "librarr", "url": BASE, "api_key": "k", "color": "#a78bfa", "enabled": False}
        assert app_module.migrate_librarr_downloaders(cfg) is False  # nothing left to move

    def test_a_name_already_used_by_an_instance_gets_a_number(self, app_module):
        cfg = {"instances": [{"name": "Librarr", "kind": "comic", "url": "", "api_key": ""}],
               "downloaders": [{"name": "Librarr", "type": "librarr", "url": BASE, "api_key": "k"}]}
        app_module.migrate_librarr_downloaders(cfg)
        assert [i["name"] for i in cfg["instances"]] == ["Librarr", "Librarr (2)"]

    def test_starting_the_app_on_an_old_config_moves_it_and_saves(self, app_module, tmp_path):
        import importlib
        import json
        import sys
        (tmp_path / "config.json").write_text(json.dumps({
            "instances": [], "downloaders": [{"name": "Librarr", "type": "librarr", "url": BASE, "api_key": "k", "color": "#a78bfa", "enabled": True}],
            "users": [], "secret_key": "x" * 64}))
        sys.modules.pop("app", None)
        fresh = importlib.import_module("app")
        assert "Librarr" in fresh.APPS and fresh.APPS["Librarr"]["backend"] == "librarr" and fresh.DOWNLOADERS == []
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["downloaders"] == [] and saved["instances"][0]["kind"] == "librarr" and saved["instances"][0]["api_key"] == "k"

    def test_importing_a_backup_made_before_the_move(self, app_module):
        old = {"arrvantage_export": 1, "includes_secrets": True, "config": {
            "instances": [], "downloaders": [{"name": "Librarr", "type": "librarr", "url": BASE, "api_key": "k", "enabled": True}]}}
        new_config, report = app_module.plan_import(old, app_module.CONFIG)
        assert report["errors"] == []
        assert [(i["name"], i["kind"], i["api_key"]) for i in new_config["instances"]] == [("Librarr", "librarr", "k")]
        assert new_config["downloaders"] == []
        assert any("Librarr" in note for note in report["notes"])

    def test_a_keyless_backup_keeps_the_key_already_set_up(self, app_module):
        add_librarr(app_module)
        old = {"arrvantage_export": 1, "includes_secrets": False, "config": {
            "instances": [], "downloaders": [{"name": "Librarr", "type": "librarr", "url": BASE, "enabled": True}]}}
        new_config, _ = app_module.plan_import(old, app_module.CONFIG)
        assert new_config["instances"][0]["api_key"] == "k"


class TestHealth:
    def test_probe_needs_no_key(self, app_module, requests_mock):
        mock = requests_mock.get(f"{BASE}/health", json=HEALTH)
        result = app_module.librarr_probe({"url": BASE + "/", "api_key": "k"})
        assert result == {"error": None, "version": "1.5.0", "sources_enabled": 13, "library_items": 134}
        assert "X-Api-Key" not in mock.last_request.headers

    def test_failures_are_words_and_retried_once(self, app_module, requests_mock):
        mock = requests_mock.get(f"{BASE}/health", exc=requests.exceptions.ConnectionError("boom http://x/?apikey=SECRET"))
        assert app_module.librarr_probe({"url": BASE}) == {"error": "could not connect"}
        assert mock.call_count == 2
        requests_mock.get(f"{BASE}/health", status_code=404)
        error = app_module.librarr_probe({"url": BASE})["error"]
        assert "404" in error and "librarr.test" not in error

    def test_another_status_or_another_app(self, app_module, requests_mock):
        requests_mock.get(f"{BASE}/health", json={"status": "degraded"})
        assert "degraded" in app_module.librarr_probe({"url": BASE})["error"]
        requests_mock.get(f"{BASE}/health", text="<html>x</html>")
        assert "not like Librarr" in app_module.librarr_probe({"url": BASE})["error"]

    def test_the_system_tab_says_it_is_running_and_warns_when_no_source_is_on(self, app_module, requests_mock):
        config = add_librarr(app_module)
        requests_mock.get(f"{BASE}/health", json=HEALTH)
        result = app_module.fetch_health("Librarr", config)
        assert result["error"] is None
        assert result["health_items"] == [{"type": "notice", "source": "Librarr", "wiki_url": "",
                                           "message": "Librarr 1.5.0 is running, 13 search sources on"}]
        requests_mock.get(f"{BASE}/health", json={**HEALTH, "sources_enabled": 0})
        types = [i["type"] for i in app_module.fetch_health("Librarr", config)["health_items"]]
        assert types == ["notice", "warning"]

    def test_a_down_librarr_is_an_error_not_a_crash(self, app_module, requests_mock):
        config = add_librarr(app_module)
        requests_mock.get(f"{BASE}/health", exc=requests.exceptions.ConnectionError("down"))
        assert app_module.fetch_health("Librarr", config) == {"error": "could not connect", "health_items": []}

    def test_no_disk_space_is_reported(self, app_module):
        config = add_librarr(app_module)
        assert app_module.fetch_diskspace("Librarr", config) == {"error": None, "diskspace_items": []}


class TestQueueAndHistory:
    def test_a_failed_job_is_a_failed_queue_row_with_its_reason_and_retry_id(self, app_module):
        [item] = app_module.librarr_queue_items(DOWNLOADS)
        assert item["status"] == "failed" and item["job_id"] == "fc92aacb" and item["progress_pct"] is None
        assert item["status_messages"] == ["no download link found in HTML response"] and item["indexer"] == "mangadex"
        assert item["time_left"] == "Failed"

    def test_finished_jobs_are_dropped_and_statuses_read_by_meaning(self, app_module):
        jobs = {"downloads": [
            {"title": "a", "status": "completed"}, {"title": "b", "status": "imported"},
            {"title": "c", "status": "downloading", "progress": 41.5}, {"title": "d", "status": "queued"},
            {"title": "e", "status": "importing"}, {"title": "f", "status": "something new"},
        ]}
        items = app_module.librarr_queue_items(jobs)
        assert {i["title"]: i["status"] for i in items} == {"c": "downloading", "e": "preparing", "f": "downloading", "d": "queued"}
        assert next(i for i in items if i["title"] == "c")["progress_pct"] == 42
        assert [i["status"] for i in items] == ["downloading", "downloading", "preparing", "queued"]

    def test_an_unusable_job_id_cannot_be_retried(self, app_module):
        [item] = app_module.librarr_queue_items({"downloads": [{"title": "x", "status": "error", "job_id": "../../etc"}]})
        assert item["job_id"] == "" and item["status_messages"] == ["Download failed"]

    def test_garbage_answers(self, app_module):
        for junk in (None, [], "x", {"downloads": "no"}, {"downloads": [None, "a", 3]}):
            assert app_module.librarr_queue_items(junk) == []
            assert app_module.librarr_history_items(junk) == []

    def test_the_queue_call_sends_the_key_and_a_wrong_key_is_a_few_words(self, app_module, requests_mock):
        config = add_librarr(app_module)
        mock = requests_mock.get(f"{BASE}/api/downloads", json=DOWNLOADS)
        assert app_module.fetch_queue("Librarr", config)["queue_items"][0]["title"] == "One Piece"
        assert mock.last_request.headers["X-Api-Key"] == "k"
        requests_mock.get(f"{BASE}/api/downloads", status_code=401)
        result = app_module.fetch_queue("Librarr", config)
        assert "check the API key" in result["error"] and "librarr.test" not in result["error"] and result["queue_items"] == []

    def test_history_is_newest_first_with_plain_labels_and_no_sign_in_events(self, app_module):
        items = app_module.librarr_history_items(ACTIVITY)
        assert [(i["title"], i["event"], i["date"]) for i in items] == [
            ("Attack on Titan (2012-2021) (Digital)", "Imported", "2026-11-06"),
            ("Attack on Titan", "Started", "2026-11-06"),
            ("Naruto Vol.1", "Imported", "2026-11-06"),
        ]

    def test_an_unknown_event_type_is_shown_in_words(self, app_module):
        [item] = app_module.librarr_history_items({"events": [{"id": 1, "event_type": "file_removed", "title": "X", "timestamp": "nope"}]})
        assert item["event"] == "File removed" and item["date"] == ""

    def test_the_history_call(self, app_module, requests_mock):
        config = add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/activity", json=ACTIVITY)
        assert len(app_module.fetch_history("Librarr", config)["history_items"]) == 3
        requests_mock.get(f"{BASE}/api/activity", status_code=500)
        assert app_module.fetch_history("Librarr", config)["error"] == "HTTP 500"


class TestLibrary:
    def test_files_become_one_card_per_title(self, app_module):
        cards = app_module.librarr_group_files(LIBRARY_PAGE_1["items"] + LIBRARY_PAGE_2["items"])
        assert [c["title"] for c in cards] == ["Akira", "Claymore"]
        akira, claymore = cards
        assert claymore["file_count"] == 3 and claymore["item_id"] == 161 and claymore["detail"] == "3 files · cbz"
        assert claymore["size_bytes"] == 234935119 + 1500 + 10 and claymore["date_added"] == "2026-11-07T12:36:29-08:00"
        assert claymore["status"] == "Downloaded" and claymore["cover_url"] == ""
        assert [f["name"] for f in claymore["librarr_files"]] == ["Claymore v01.cbz", "Claymore v02.cbz", "Claymore v03.cbz"]
        assert akira["librarr_files"][0]["name"] == "Akira.epub" and akira["librarr_files"][0]["size_human"] == "2.0 GB"
        assert claymore["librarr_files"][0]["size_human"] == "1 KB"

    def test_no_file_path_reaches_the_cards(self, app_module):
        text = str(app_module.librarr_group_files(LIBRARY_PAGE_1["items"]))
        assert "/manga/" not in text and "C:\\manga" not in text and "/downloads" not in text

    def test_every_page_is_read(self, app_module, requests_mock):
        config = add_librarr(app_module)
        mock_library(requests_mock, LIBRARY_PAGE_1, LIBRARY_PAGE_2)
        library = app_module.fetch_library("Librarr", config)
        assert library["error"] is None and library["complete"] is True and library["file_count"] == 4
        assert [c["title"] for c in library["library_items"]] == ["Akira", "Claymore"]
        assert library["status_options"] == ["Downloaded"] and library["genre_options"] == []
        assert [r.qs["page"] for r in requests_mock.request_history] == [["1"], ["2"]]
        assert requests_mock.request_history[0].headers["X-Api-Key"] == "k"

    def test_a_later_page_failing_shows_what_arrived_and_says_it_is_not_complete(self, app_module, requests_mock):
        config = add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/library/manga", [{"json": LIBRARY_PAGE_1}, {"status_code": 500}])
        library = app_module.fetch_library("Librarr", config)
        assert library["error"] is None and library["complete"] is False and library["file_count"] == 3

    def test_the_page_cap_stops_a_runaway_list(self, app_module, requests_mock, monkeypatch):
        config = add_librarr(app_module)
        monkeypatch.setattr(app_module, "LIBRARR_LIBRARY_MAX_PAGES", 2)
        endless = {"items": [{"id": 1, "title": "x"}], "page": 1, "pages": 99, "total": 99}
        mock = requests_mock.get(f"{BASE}/api/library/manga", json=endless)
        library = app_module.fetch_library("Librarr", config)
        assert mock.call_count == 2 and library["complete"] is False

    def test_the_first_page_failing_is_an_error(self, app_module, requests_mock):
        config = add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/library/manga", status_code=401)
        library = app_module.fetch_library("Librarr", config)
        assert "check the API key" in library["error"] and library["library_items"] == []
        requests_mock.get(f"{BASE}/api/library/manga", text="<html>login</html>")
        assert "unexpected answer" in app_module.fetch_library("Librarr", config)["error"]

    def test_an_empty_library(self, app_module, requests_mock):
        config = add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/library/manga", json={"items": [], "page": 1, "pages": 1, "total": 0})
        library = app_module.fetch_library("Librarr", config)
        assert library["library_items"] == [] and library["status_options"] == [] and library["complete"] is True


class TestHomeCard:
    def test_the_libraries_panel_says_titles_and_files(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        mock_library(requests_mock, LIBRARY_PAGE_1, LIBRARY_PAGE_2)
        body = client.get("/api/home/stats").get_json()
        [card] = [i for i in body["instances"] if i["name"] == "Librarr"]
        assert card["unit"] == "titles" and card["total"] == 2 and card["missing"] == 0
        assert card["detail_text"] == "4 files" and card["size_display"]

    def test_a_cut_short_list_has_no_size_and_says_so(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/library/manga", [{"json": LIBRARY_PAGE_1}, {"status_code": 500}])
        [card] = [i for i in client.get("/api/home/stats").get_json()["instances"] if i["name"] == "Librarr"]
        assert card["detail_text"] == "3 files (list cut short)" and card["size_display"] == ""

    def test_it_is_not_on_the_downloads_panel(self, app_module, admin_session):
        client, _ = admin_session
        add_librarr(app_module)
        assert client.get("/api/downloads?refresh=1").get_json()["clients"] == []


class TestPanelsAndDetails:
    def _mock_all(self, requests_mock):
        requests_mock.get(f"{BASE}/api/downloads", json=DOWNLOADS)
        requests_mock.get(f"{BASE}/api/activity", json=ACTIVITY)
        requests_mock.get(f"{BASE}/health", json=HEALTH)
        mock_library(requests_mock, LIBRARY_PAGE_1, LIBRARY_PAGE_2)

    def test_the_instance_panels(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        self._mock_all(requests_mock)
        html = client.get("/fragment/instance/Librarr").get_data(as_text=True)
        assert 'data-view="search"' in html and 'data-librarr-retry="fc92aacb"' in html and "data-librarr-refresh" in html
        assert "Claymore" in html and "3 files" in html and "Disk space" not in html
        assert 'id="genre-filter-Librarr"' not in html and "/manga/" not in html
        assert 'data-queue-count="1"' in html

    def test_a_user_who_cannot_search_gets_no_retry_or_search_form(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        app_module.USERS[0]["permissions"] = {"requesting": True, "searching": False}
        app_module.USERS[0]["role"] = "user"
        self._mock_all(requests_mock)
        html = client.get("/fragment/instance/Librarr").get_data(as_text=True)
        assert "data-librarr-retry" not in html and 'name="q"' not in html

    def test_other_instances_have_no_search_panel(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        app_module.CONFIG["instances"].append({"name": "Radarr", "kind": "movie", "url": "http://r.test", "api_key": "a", "enabled": True})
        app_module.apply_config(app_module.CONFIG)
        for path in ("/api/v3/queue", "/api/v3/history", "/api/v3/movie", "/api/v3/health", "/api/v3/diskspace"):
            requests_mock.get(f"http://r.test{path}", json=[])
        assert 'data-view="search"' not in client.get("/fragment/instance/Radarr").get_data(as_text=True)

    def test_the_details_window_lists_the_files_without_paths(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        self._mock_all(requests_mock)
        body = client.get("/api/details/Librarr/161").get_json()
        assert body["file_count"] == 3 and body["title"] == "Claymore" and body["total_size"] == "224 MB"
        assert body["files"][0] == {"name": "Claymore v01.cbz", "format": "cbz", "size_human": "1 KB", "added": "2026-11-06", "source": "torrent"}
        assert "/manga/" not in str(body)

    def test_a_title_that_is_gone(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        self._mock_all(requests_mock)
        assert client.get("/api/details/Librarr/9999").status_code == 404

    def test_a_librarr_failure_in_details_is_an_error_message(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/library/manga", status_code=500)
        assert client.get("/api/details/Librarr/161").get_json()["error"] == "HTTP 500"

    def test_the_needs_attention_summary_flags_a_failed_job(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        self._mock_all(requests_mock)
        body = client.get("/api/attention?refresh=1").get_json()
        assert body["per_instance"]["Librarr"]["dot"] in ("warning", "critical")


class TestRetry:
    def test_retry_posts_to_the_job(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        add_librarr(app_module)
        mock = requests_mock.post(f"{BASE}/api/downloads/jobs/fc92aacb/retry", json={"success": True})
        response = client.post("/api/librarr/retry/Librarr/fc92aacb", headers=HEADERS(csrf))
        assert response.get_json() == {"ok": True} and mock.last_request.headers["X-Api-Key"] == "k"

    def test_an_odd_id_is_refused_without_calling_librarr(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        add_librarr(app_module)
        assert client.post("/api/librarr/retry/Librarr/..%2Fx", headers=HEADERS(csrf)).status_code in (400, 404)
        assert not requests_mock.called

    def test_only_librarr_instances_and_only_a_signed_in_searcher(self, app_module, admin_session, requests_mock, client):
        c, csrf = admin_session
        add_librarr(app_module)
        app_module.CONFIG["instances"].append({"name": "Radarr", "kind": "movie", "url": "http://r.test", "api_key": "a", "enabled": True})
        app_module.apply_config(app_module.CONFIG)
        assert c.post("/api/librarr/retry/Radarr/abc", headers=HEADERS(csrf)).status_code == 404

    def test_a_librarr_error_is_a_few_words(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        add_librarr(app_module)
        requests_mock.post(f"{BASE}/api/downloads/jobs/abc/retry", exc=requests.exceptions.ConnectionError("boom http://librarr.test/?apikey=SECRET"))
        response = client.post("/api/librarr/retry/Librarr/abc", headers=HEADERS(csrf))
        assert response.status_code == 502 and response.get_json() == {"ok": False, "error": "could not connect"}


class TestSettingsTest:
    BODY = {"category": "instance", "kind_or_type": "librarr", "url": BASE, "credential": "k"}

    def test_a_good_key(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        mock = requests_mock.get(f"{BASE}/api/downloads", json=DOWNLOADS)
        assert client.post("/api/settings/test", json=self.BODY, headers=HEADERS(csrf)).get_json() == {"ok": True}
        assert mock.last_request.headers["X-Api-Key"] == "k"

    def test_a_wrong_key_or_wrong_app(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get(f"{BASE}/api/downloads", status_code=401)
        result = client.post("/api/settings/test", json=self.BODY, headers=HEADERS(csrf)).get_json()
        assert result["ok"] is False and "401" in result["error"]
        requests_mock.get(f"{BASE}/api/downloads", text="<html>login</html>")
        assert "not like Librarr" in client.post("/api/settings/test", json=self.BODY, headers=HEADERS(csrf)).get_json()["error"]

    def test_it_is_offered_under_instances_not_downloaders(self, app_module, admin_session):
        client, _ = admin_session
        html = client.get("/settings").get_data(as_text=True)
        assert "Manga and books (Librarr)" in html


class TestSearchRows:
    def test_best_score_first_and_no_links_reach_the_page(self, app_module):
        rows, raw = app_module.librarr_search_results(SEARCH)
        assert [r["title"] for r in rows] == ["Naruto v01-v72", "Naruto", "Boruto"]
        assert [r["token"] for r in rows] == ["0", "1", "2"] and raw["0"]["download_url"].endswith("INDEXER-SECRET")
        text = str(rows)
        assert "INDEXER-SECRET" not in text and "http" not in text and "covers.test" not in text
        assert rows[0]["source"] == "1337x" and rows[1]["source"] == "MangaDex"
        assert rows[2]["in_library"] is True and rows[2]["library_title"] == "Boruto (owned)"

    def test_garbage_and_the_cap(self, app_module):
        for junk in (None, [], {"results": "x"}, {"results": [None, 3, "a"]}):
            assert app_module.librarr_search_results(junk)[0] == []
        many = {"results": [{"title": f"t{i}", "score": i} for i in range(100)]}
        assert len(app_module.librarr_search_results(many)[0]) == app_module.LIBRARR_RESULT_LIMIT

    def test_the_grab_body_mirrors_what_librarrs_own_page_sends(self, app_module):
        _, raw = app_module.librarr_search_results(SEARCH)
        body = app_module.librarr_grab_body(raw["0"], force=False)
        assert body == {"title": "Naruto v01-v72", "download_url": "http://prowlarr.test/dl/1?apikey=INDEXER-SECRET", "abb_url": "",
                        "source": "prowlarr_manga", "source_id": "", "md5": "", "author": "", "info_hash": "", "magnet": "",
                        "media_type": "", "download_protocol": "torrent", "force": False}
        assert app_module.librarr_grab_body(raw["1"], True)["download_url"] == "https://mangadex.test/title/6b1e"


class TestSearchAndGrab:
    def _start(self, client, csrf, monkeypatch, app_module, q="Naruto"):
        monkeypatch.setattr(app_module.threading, "Thread", InlineThread)
        return client.post("/api/librarr/search/Librarr", json={"q": q}, headers=HEADERS(csrf))

    def test_search_runs_off_the_request_and_the_page_collects_it(self, app_module, admin_session, requests_mock, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        mock = requests_mock.get(f"{BASE}/api/search/manga", json=SEARCH)
        sid = self._start(client, csrf, monkeypatch, app_module).get_json()["id"]
        body = client.get(f"/api/librarr/search/Librarr/{sid}").get_json()
        assert body["state"] == "done" and body["error"] is None and body["took_ms"] == 30007
        assert [r["title"] for r in body["results"]] == ["Naruto v01-v72", "Naruto", "Boruto"]
        assert mock.last_request.qs["q"] == ["naruto"] and mock.last_request.headers["X-Api-Key"] == "k"
        assert "INDEXER-SECRET" not in str(body)

    def test_a_failing_search_is_a_few_words(self, app_module, admin_session, requests_mock, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/search/manga", exc=requests.exceptions.ReadTimeout("slow"))
        sid = self._start(client, csrf, monkeypatch, app_module).get_json()["id"]
        body = client.get(f"/api/librarr/search/Librarr/{sid}").get_json()
        assert body["state"] == "error" and body["error"] == "timed out"

    def test_a_search_in_progress_reports_running_and_is_shared(self, app_module, admin_session, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        started = []
        monkeypatch.setattr(app_module.threading, "Thread", lambda **kw: type("T", (), {"start": lambda s: started.append(kw)})())
        first = client.post("/api/librarr/search/Librarr", json={"q": "Naruto"}, headers=HEADERS(csrf)).get_json()["id"]
        again = client.post("/api/librarr/search/Librarr", json={"q": "naruto "}, headers=HEADERS(csrf)).get_json()["id"]
        assert first == again and len(started) == 1
        assert client.get(f"/api/librarr/search/Librarr/{first}").get_json()["state"] == "running"

    def test_too_many_at_once_and_bad_queries(self, app_module, admin_session, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        monkeypatch.setattr(app_module.threading, "Thread", lambda **kw: type("T", (), {"start": lambda s: None})())
        for q in ("one", "two", "three"):
            assert client.post("/api/librarr/search/Librarr", json={"q": q}, headers=HEADERS(csrf)).status_code == 200
        assert client.post("/api/librarr/search/Librarr", json={"q": "four"}, headers=HEADERS(csrf)).status_code == 429
        for bad in ("", "a", "x" * 101):
            assert client.post("/api/librarr/search/Librarr", json={"q": bad}, headers=HEADERS(csrf)).status_code in (400, 429)

    def test_only_librarr_instances_and_only_the_searcher(self, app_module, admin_session, requests_mock, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        app_module.CONFIG["instances"].append({"name": "Radarr", "kind": "movie", "url": "http://r.test", "api_key": "k", "enabled": True})
        app_module.apply_config(app_module.CONFIG)
        assert client.post("/api/librarr/search/Radarr", json={"q": "naruto"}, headers=HEADERS(csrf)).status_code == 404
        requests_mock.get(f"{BASE}/api/search/manga", json=SEARCH)
        sid = self._start(client, csrf, monkeypatch, app_module).get_json()["id"]
        app_module._LIBRARR_SEARCHES[sid]["owner"] = "someone-else"
        assert client.get(f"/api/librarr/search/Librarr/{sid}").status_code == 404

    def test_sending_a_result_posts_the_real_fields_server_side(self, app_module, admin_session, requests_mock, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/search/manga", json=SEARCH)
        sid = self._start(client, csrf, monkeypatch, app_module).get_json()["id"]
        post = requests_mock.post(f"{BASE}/api/download", json={"success": True, "job_id": "abc123"})
        result = client.post(f"/api/librarr/grab/Librarr/{sid}/0", json={}, headers=HEADERS(csrf)).get_json()
        assert result == {"ok": True, "job_id": "abc123"}
        sent = post.last_request.json()
        assert sent["download_url"].endswith("INDEXER-SECRET") and sent["force"] is False and sent["source"] == "prowlarr_manga"
        assert post.last_request.headers["X-Api-Key"] == "k"

    def test_a_book_already_owned_says_so_and_force_sends_it_anyway(self, app_module, admin_session, requests_mock, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/search/manga", json=SEARCH)
        sid = self._start(client, csrf, monkeypatch, app_module).get_json()["id"]
        post = requests_mock.post(f"{BASE}/api/download", status_code=409,
                                  json={"code": "already_in_library", "library_item_id": 7, "library_title": "Boruto (owned)"})
        result = client.post(f"/api/librarr/grab/Librarr/{sid}/2", json={}, headers=HEADERS(csrf)).get_json()
        assert result == {"ok": False, "already_in_library": True, "library_title": "Boruto (owned)"}
        requests_mock.post(f"{BASE}/api/download", json={"success": True, "job_id": "j2"})
        forced = client.post(f"/api/librarr/grab/Librarr/{sid}/2", json={"force": True}, headers=HEADERS(csrf)).get_json()
        assert forced["ok"] is True and post.call_count == 1

    def test_errors_from_librarr_are_shown_as_words(self, app_module, admin_session, requests_mock, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/search/manga", json=SEARCH)
        sid = self._start(client, csrf, monkeypatch, app_module).get_json()["id"]
        requests_mock.post(f"{BASE}/api/download", status_code=500, json={"success": False, "error": "no download link found"})
        response = client.post(f"/api/librarr/grab/Librarr/{sid}/0", json={}, headers=HEADERS(csrf))
        assert response.status_code == 502 and response.get_json()["error"] == "no download link found"
        requests_mock.post(f"{BASE}/api/download", exc=requests.exceptions.ConnectionError("boom http://librarr.test/?apikey=SECRET"))
        assert client.post(f"/api/librarr/grab/Librarr/{sid}/0", json={}, headers=HEADERS(csrf)).get_json() == {"ok": False, "error": "could not connect"}

    def test_an_expired_or_unknown_result(self, app_module, admin_session):
        client, csrf = admin_session
        add_librarr(app_module)
        assert client.post("/api/librarr/grab/Librarr/nope/0", json={}, headers=HEADERS(csrf)).status_code == 404

    def test_old_searches_are_forgotten(self, app_module):
        with app_module._LIBRARR_LOCK:
            app_module._LIBRARR_SEARCHES["old"] = {"client": "L", "owner": "u", "query": "q", "state": "done",
                                                   "started": 0.0, "error": None, "rows": [], "raw": {}, "took_ms": 0}
            app_module._librarr_prune(app_module.LIBRARR_SEARCH_KEEP_SECONDS + 1)
        assert "old" not in app_module._LIBRARR_SEARCHES

    def test_a_signed_out_visitor_cannot_search(self, client):
        assert client.post("/api/librarr/search/Librarr", json={"q": "naruto"}).status_code in (302, 401, 403)
