"""
Librarr as a download-client type: health (no key), the Home card from
/api/downloads + /api/activity, retry, and manga search + send.

The sample bodies are shaped like answers captured from a real Librarr 1.5.0
server (the download job and activity event fields, the search result fields).
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
], "limit": 50, "offset": 0, "total": 15}
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
    app_module.CONFIG.setdefault("downloaders", []).append(
        {"name": name, "type": "librarr", "url": BASE, "api_key": "k", "color": "#a78bfa", "enabled": enabled})
    app_module.apply_config(app_module.CONFIG)
    return app_module.get_downloader(name)


class InlineThread:
    """Runs the target straight away, so a search finishes before the test goes on."""
    def __init__(self, target=None, args=(), kwargs=None, **_):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class TestHealth:
    def test_ok_needs_no_key(self, app_module, requests_mock):
        mock = requests_mock.get(f"{BASE}/health", json=HEALTH)
        result = app_module.fetch_librarr_health({"url": BASE + "/", "api_key": "k"})
        assert result == {"error": None, "version": "1.5.0", "sources_enabled": 13, "library_items": 134}
        assert "X-Api-Key" not in mock.last_request.headers

    def test_failures_are_words_and_retried_once(self, app_module, requests_mock):
        mock = requests_mock.get(f"{BASE}/health", exc=requests.exceptions.ConnectionError("boom http://x/?apikey=SECRET"))
        assert app_module.fetch_librarr_health({"url": BASE}) == {"error": "could not connect"}
        assert mock.call_count == 2
        requests_mock.get(f"{BASE}/health", status_code=404)
        error = app_module.fetch_librarr_health({"url": BASE})["error"]
        assert "404" in error and "librarr.test" not in error

    def test_another_status_or_another_app(self, app_module, requests_mock):
        requests_mock.get(f"{BASE}/health", json={"status": "degraded"})
        assert "degraded" in app_module.fetch_librarr_health({"url": BASE})["error"]
        requests_mock.get(f"{BASE}/health", text="<html>x</html>")
        assert "not like Librarr" in app_module.fetch_librarr_health({"url": BASE})["error"]

    def test_the_attention_probe_uses_health_and_a_down_librarr_is_critical(self, app_module, requests_mock):
        requests_mock.get(f"{BASE}/health", exc=requests.exceptions.ConnectionError("down"))
        probe = app_module.probe_downloader({"type": "librarr", "url": BASE, "api_key": "k"})
        assert probe == {"error": "could not connect"}


class TestViews:
    def test_a_failed_job_is_listed_with_its_reason_and_a_retry_id(self, app_module):
        [item] = app_module.librarr_jobs(DOWNLOADS)
        assert item["group"] == "failed" and item["job_id"] == "fc92aacb" and item["progress"] is None
        assert item["note"] == "no download link found in HTML response" and item["source"] == "mangadex"

    def test_finished_jobs_are_dropped_and_statuses_read_by_meaning(self, app_module):
        jobs = {"downloads": [
            {"title": "a", "status": "completed"}, {"title": "b", "status": "imported"},
            {"title": "c", "status": "downloading", "progress": 41.5}, {"title": "d", "status": "queued"},
            {"title": "e", "status": "importing"}, {"title": "f", "status": "something new"},
        ]}
        groups = {i["name"]: i["group"] for i in app_module.librarr_jobs(jobs)}
        assert groups == {"c": "downloading", "e": "preparing", "f": "downloading", "d": "queued"}
        assert next(i for i in app_module.librarr_jobs(jobs) if i["name"] == "c")["progress"] == 41.5

    def test_an_unusable_job_id_cannot_be_retried(self, app_module):
        [item] = app_module.librarr_jobs({"downloads": [{"title": "x", "status": "error", "job_id": "../../etc"}]})
        assert item["job_id"] == ""

    def test_recent_imports_are_the_newest_import_events_only(self, app_module):
        recent = app_module.librarr_recent_imports(ACTIVITY)
        assert [r["title"] for r in recent] == ["Attack on Titan (2012-2021) (Digital)", "Naruto Vol.1"]
        assert recent[0]["when"] == "2026-11-06T21:48:03-08:00"

    def test_the_card_has_no_speed_and_counts_failures(self, app_module):
        card = app_module.librarr_downloads_view(DOWNLOADS, ACTIVITY)
        assert card["down_speed"] is None and card["up_speed"] is None and card["error"] is None
        assert card["counts"] == {"downloading": 0, "preparing": 0, "queued": 0, "failed": 1}
        assert card["state"] == "idle" and card["total"] == 1 and len(card["recent"]) == 2  # a failed job is not "waiting"
        queued = app_module.librarr_downloads_view({"downloads": [{"title": "q", "status": "queued"}]})
        assert queued["state"] == "waiting"

    def test_idle_and_garbage(self, app_module):
        assert app_module.librarr_downloads_view({"downloads": []})["state"] == "idle"
        for junk in (None, [], "x", {"downloads": "no"}, {"downloads": [None, "a", 3]}):
            assert app_module.librarr_downloads_view(junk, junk)["error"] is None


class TestHomePanel:
    def test_the_card_comes_from_downloads_and_activity_with_the_key(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        down = requests_mock.get(f"{BASE}/api/downloads", json=DOWNLOADS)
        requests_mock.get(f"{BASE}/api/activity", json=ACTIVITY)
        body = client.get("/api/downloads?refresh=1").get_json()
        [card] = body["clients"]
        assert card["type"] == "librarr" and card["counts"]["failed"] == 1 and card["recent"][0]["title"].startswith("Attack")
        assert down.last_request.headers["X-Api-Key"] == "k"
        assert (body["down_speed"], body["up_speed"]) == (0, 0)  # Librarr reports no speed

    def test_a_wrong_key_is_a_few_words(self, app_module, requests_mock):
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/downloads", status_code=401)
        result = app_module.fetch_download_client(app_module.get_downloader("Librarr"))
        assert "check the API key" in result["error"] and "librarr.test" not in result["error"]

    def test_activity_failing_does_not_hide_the_queue(self, app_module, requests_mock):
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/downloads", json=DOWNLOADS)
        requests_mock.get(f"{BASE}/api/activity", status_code=500)
        card = app_module.fetch_download_client(app_module.get_downloader("Librarr"))
        assert card["error"] is None and card["recent"] == [] and card["total"] == 1


class TestDetailAndRetry:
    def test_the_downloader_view_has_jobs_recent_imports_and_a_summary(self, app_module, admin_session, requests_mock):
        client, _ = admin_session
        add_librarr(app_module)
        requests_mock.get(f"{BASE}/api/downloads", json=DOWNLOADS)
        requests_mock.get(f"{BASE}/api/activity", json=ACTIVITY)
        requests_mock.get(f"{BASE}/health", json=HEALTH)
        body = client.get("/api/downloader/status/Librarr").get_json()
        assert body["type"] == "librarr" and body["error"] is None
        assert body["items"][0]["job_id"] == "fc92aacb" and len(body["recent"]) == 2
        assert body["summary"] == {"version": "1.5.0", "sources_enabled": 13, "library_items": 134, "failed": 1}

    def test_retry_posts_to_the_job(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        add_librarr(app_module)
        mock = requests_mock.post(f"{BASE}/api/downloads/jobs/fc92aacb/retry", json={"success": True})
        response = client.post("/api/downloader/action/Librarr", json={"action": "retry", "item_id": "fc92aacb"}, headers=HEADERS(csrf))
        assert response.get_json() == {"ok": True} and mock.last_request.headers["X-Api-Key"] == "k"

    def test_other_actions_and_odd_ids_are_refused(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        add_librarr(app_module)
        for payload in ({"action": "delete", "item_id": "abc"}, {"action": "retry", "item_id": "../x"}, {"action": "retry", "item_id": ""}):
            response = client.post("/api/downloader/action/Librarr", json=payload, headers=HEADERS(csrf))
            assert response.status_code == 502
        assert not requests_mock.called


class TestSettingsTest:
    BODY = {"category": "downloader", "kind_or_type": "librarr", "url": BASE, "credential": "k"}

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

    def test_only_librarr_clients_and_only_the_searcher(self, app_module, admin_session, requests_mock, monkeypatch):
        client, csrf = admin_session
        add_librarr(app_module)
        app_module.CONFIG["downloaders"].append({"name": "Sab", "type": "sabnzbd", "url": "http://sab.test", "api_key": "k", "enabled": True})
        app_module.apply_config(app_module.CONFIG)
        assert client.post("/api/librarr/search/Sab", json={"q": "naruto"}, headers=HEADERS(csrf)).status_code == 404
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
