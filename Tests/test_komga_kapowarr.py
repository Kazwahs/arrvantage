"""
Tests for the two newest integrations: Kapowarr (a comics-focused *arr
app added as a new "comic" instance kind) and Komga (a comics/manga
server added as a new media-server type).

Kapowarr's shapes are confirmed directly against its own source
(frontend/api.py, backend/internals/status.py, backend/implementations/
root_folders.py, backend/implementations/volumes.py) since it has no
published OpenAPI spec. Komga's shapes are confirmed against its real
OpenAPI spec (komga.org/docs/openapi, and the raw spec at
github.com/gotson/komga/blob/master/komga/docs/openapi.json).
"""

FAKE_KAPOWARR = {
    "url": "http://kapowarr.test:5656",
    "api_key": "fake-key",
    "backend": "kapowarr",
    "library_kind": "comic",
}
FAKE_KOMGA = {"url": "http://komga.test:25600", "credential": "fake-key"}


class TestKapowarrQueue:
    def test_maps_queue_entries(self, app_module, requests_mock):
        requests_mock.get(
            "http://kapowarr.test:5656/api/activity/queue",
            json={"error": None, "result": [
                {"id": 1, "title": "Batman #1", "state": "downloading",
                 "progress": 42.5, "size": 12_000_000_000, "source_name": "GetComics"},
                {"id": 2, "title": "Broken Issue", "state": "failed", "size": -1, "progress": 0},
            ]},
        )

        result = app_module.fetch_kapowarr_queue("Kapowarr", FAKE_KAPOWARR)

        assert result["error"] is None
        ok, failed = result["queue_items"]
        assert ok["title"] == "Batman #1"
        assert ok["progress_pct"] == 42
        assert ok["indexer"] == "GetComics"
        assert ok["size_human"] == "11.2 GB"
        assert failed["status_messages"] == ["Download failed"]

    def test_invalid_api_key_returns_error_not_exception(self, app_module, requests_mock):
        # Confirmed via Kapowarr's own auth() decorator: a bad api_key
        # comes back as a real 401, not a 200 with an "error" field.
        requests_mock.get("http://kapowarr.test:5656/api/activity/queue", status_code=401,
                           json={"error": "ApiKeyInvalid", "result": None})

        result = app_module.fetch_kapowarr_queue("Kapowarr", FAKE_KAPOWARR)

        assert result["error"] is not None
        assert result["queue_items"] == []

    def test_connection_error_returns_error_not_exception(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://kapowarr.test:5656/api/activity/queue", exc=requests.exceptions.ConnectionError)

        result = app_module.fetch_kapowarr_queue("Kapowarr", FAKE_KAPOWARR)

        assert result["error"] is not None
        assert result["queue_items"] == []


class TestKapowarrHistory:
    def test_maps_history_entries(self, app_module, requests_mock):
        requests_mock.get(
            "http://kapowarr.test:5656/api/activity/history",
            json={"error": None, "result": [
                {"file_title": "Batman #1", "downloaded_at": 1690000000, "success": True},
                {"file_title": None, "web_title": "Bad One", "downloaded_at": "2024-01-02T03:04:05Z", "success": False},
            ]},
        )

        result = app_module.fetch_kapowarr_history("Kapowarr", FAKE_KAPOWARR)

        assert result["error"] is None
        assert result["history_items"][0]["title"] == "Batman #1"
        assert result["history_items"][0]["event"] == "Downloaded"
        assert result["history_items"][0]["date"] == "2023-07-22"
        assert result["history_items"][1]["title"] == "Bad One"
        assert result["history_items"][1]["event"] == "Failed"
        assert result["history_items"][1]["date"] == "2024-01-02"


class TestKapowarrHealth:
    def test_maps_known_status_types(self, app_module, requests_mock):
        requests_mock.get(
            "http://kapowarr.test:5656/api/system/status",
            json={"error": None, "result": [
                {"type": "root_folder_full", "display_subtypes": ["/comics"]},
                {"type": "cv_rate_limit", "display_subtypes": []},
            ]},
        )

        result = app_module.fetch_kapowarr_health("Kapowarr", FAKE_KAPOWARR)

        assert result["error"] is None
        assert result["health_items"][0]["type"] == "error"
        assert "/comics" in result["health_items"][0]["message"]
        assert result["health_items"][1]["type"] == "warning"

    def test_unrecognized_status_type_falls_back_gracefully(self, app_module, requests_mock):
        # Only 5 StatusType values exist today - must not raise if a
        # future Kapowarr version adds a new one.
        requests_mock.get(
            "http://kapowarr.test:5656/api/system/status",
            json={"error": None, "result": [{"type": "some_future_type", "display_subtypes": []}]},
        )

        result = app_module.fetch_kapowarr_health("Kapowarr", FAKE_KAPOWARR)

        assert result["health_items"][0]["type"] == "notice"

    def test_no_issues_returns_empty_list(self, app_module, requests_mock):
        requests_mock.get("http://kapowarr.test:5656/api/system/status", json={"error": None, "result": []})

        result = app_module.fetch_kapowarr_health("Kapowarr", FAKE_KAPOWARR)

        assert result["health_items"] == []


class TestKapowarrDiskspace:
    def test_computes_percent_free(self, app_module, requests_mock):
        requests_mock.get(
            "http://kapowarr.test:5656/api/rootfolder",
            json={"error": None, "result": [
                {"id": 1, "folder": "/comics",
                 "size": {"total": 500 * 1024 ** 3, "used": 450 * 1024 ** 3, "free": 50 * 1024 ** 3}},
            ]},
        )

        result = app_module.fetch_kapowarr_diskspace("Kapowarr", FAKE_KAPOWARR)

        item = result["diskspace_items"][0]
        assert item["path"] == "/comics"
        assert item["percent_free"] == 10

    def test_null_size_does_not_raise(self, app_module, requests_mock):
        # Confirmed in root_folders.py: size is None when Kapowarr
        # couldn't read disk usage for that folder.
        requests_mock.get(
            "http://kapowarr.test:5656/api/rootfolder",
            json={"error": None, "result": [{"id": 1, "folder": "/missing", "size": None}]},
        )

        result = app_module.fetch_kapowarr_diskspace("Kapowarr", FAKE_KAPOWARR)

        assert result["diskspace_items"][0]["percent_free"] is None


class TestKapowarrLibrary:
    def test_maps_volumes_to_library_items(self, app_module, requests_mock):
        requests_mock.get(
            "http://kapowarr.test:5656/api/volumes",
            json={"error": None, "result": [
                {"id": 1, "title": "Batman", "year": 2020, "publisher": "DC",
                 "issue_count": 10, "issues_downloaded": 5, "total_size": 123_456_789},
                {"id": 2, "title": "Empty Volume", "year": 2021, "publisher": "DC",
                 "issue_count": 0, "issues_downloaded": 0, "total_size": 0},
            ]},
        )

        result = app_module.fetch_kapowarr_library("Kapowarr", FAKE_KAPOWARR)

        item = result["library_items"][0]
        assert item["title"] == "Batman (2020)"
        assert item["status"] == "Partial"
        assert item["episode_have"] == 5
        assert item["episode_total"] == 10
        assert item["cover_url"] == "http://kapowarr.test:5656/api/volumes/1/cover?api_key=fake-key"
        assert result["library_items"][1]["status"] == "Unknown"
        assert result["status_options"] == ["Partial", "Unknown"]

    def test_connection_error_returns_error_not_exception(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://kapowarr.test:5656/api/volumes", exc=requests.exceptions.ConnectionError)

        result = app_module.fetch_kapowarr_library("Kapowarr", FAKE_KAPOWARR)

        assert result["error"] is not None
        assert result["library_items"] == []


class TestKapowarrDispatch:
    """
    fetch_queue/fetch_history/fetch_health/fetch_diskspace/fetch_library
    all dispatch to the fetch_kapowarr_* versions when a config's
    "backend" is "kapowarr" - this is what lets index() and the other
    call sites use them with zero changes.
    """
    def test_fetch_queue_dispatches_to_kapowarr(self, app_module, requests_mock):
        requests_mock.get("http://kapowarr.test:5656/api/activity/queue", json={"error": None, "result": []})
        result = app_module.fetch_queue("Kapowarr", FAKE_KAPOWARR)
        assert result == {"error": None, "queue_items": []}

    def test_fetch_library_dispatches_to_kapowarr(self, app_module, requests_mock):
        requests_mock.get("http://kapowarr.test:5656/api/volumes", json={"error": None, "result": []})
        result = app_module.fetch_library("Kapowarr", FAKE_KAPOWARR)
        assert result["library_items"] == []

    def test_servarr_instance_does_not_dispatch_to_kapowarr(self, app_module, requests_mock):
        # A normal Radarr-type config has no "backend" key at all -
        # config.get("backend") must default safely rather than raising.
        radarr_config = {
            "url": "http://radarr.test:7878", "api_key": "k",
            "queue_endpoint": "/api/v3/queue", "library_kind": "movie",
        }
        requests_mock.get("http://radarr.test:7878/api/v3/queue", json={"records": []})
        result = app_module.fetch_queue("Radarr", radarr_config)
        assert result == {"error": None, "queue_items": []}


class TestKomgaLibraries:
    def test_lists_libraries(self, app_module, requests_mock):
        requests_mock.get(
            "http://komga.test:25600/api/v1/libraries",
            json=[{"id": "L1", "name": "Comics"}, {"id": "L2", "name": "Manga"}],
        )

        result = app_module.fetch_komga_libraries(FAKE_KOMGA)

        assert result["error"] is None
        assert result["libraries"] == [{"id": "L1", "name": "Comics"}, {"id": "L2", "name": "Manga"}]

    def test_connection_error_returns_error_not_exception(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://komga.test:25600/api/v1/libraries", exc=requests.exceptions.ConnectionError)

        result = app_module.fetch_komga_libraries(FAKE_KOMGA)

        assert result["error"] is not None
        assert result["libraries"] == []


class TestKomgaSeries:
    def test_maps_series_page(self, app_module, requests_mock):
        requests_mock.get(
            "http://komga.test:25600/api/v1/series",
            json={"content": [{
                "id": "s1", "name": "Batman", "booksCount": 10, "booksReadCount": 3,
                "booksUnreadCount": 7, "booksInProgressCount": 1,
                "metadata": {"title": "Batman", "publisher": "DC", "status": "ONGOING"},
            }], "number": 0, "totalPages": 1, "totalElements": 1},
        )

        result = app_module.fetch_komga_series(FAKE_KOMGA, "Komga1")

        item = result["items"][0]
        assert item["title"] == "Batman"
        assert item["publisher"] == "DC"
        assert item["status"] == "Ongoing"
        assert item["cover_url"] == "/api/komga/cover/Komga1/series/s1"
        assert result["total_pages"] == 1


class TestKomgaSeriesDetail:
    def test_combines_series_metadata_and_books(self, app_module, requests_mock):
        requests_mock.get(
            "http://komga.test:25600/api/v1/series/s1",
            json={"id": "s1", "name": "Batman", "booksCount": 2, "booksReadCount": 1,
                  "metadata": {"title": "Batman", "summary": "The Dark Knight", "publisher": "DC",
                               "status": "ONGOING", "genres": ["Superhero"]}},
        )
        requests_mock.get(
            "http://komga.test:25600/api/v1/series/s1/books",
            json={"content": [
                {"id": "b1", "name": "Batman 001", "number": 1, "size": "12 MiB",
                 "metadata": {"title": "Batman 001", "number": "1"},
                 "media": {"pagesCount": 22}, "readProgress": {"completed": True, "page": 22}},
                {"id": "b2", "name": "Batman 002", "number": 2, "size": "13 MiB",
                 "metadata": {"title": "Batman 002", "number": "2"},
                 "media": {"pagesCount": 24}, "readProgress": {"completed": False, "page": 5}},
                {"id": "b3", "name": "Batman 003", "number": 3, "size": "10 MiB",
                 "metadata": {"title": "Batman 003", "number": "3"},
                 "media": {"pagesCount": 20}, "readProgress": None},
            ]},
        )

        result = app_module.fetch_komga_series_detail(FAKE_KOMGA, "Komga1", "s1")

        assert result["series"]["title"] == "Batman"
        assert result["series"]["summary"] == "The Dark Knight"
        assert result["series"]["open_url"] == "http://komga.test:25600/series/s1"
        read, in_progress, unread = result["books"]
        assert read["read"] is True
        assert in_progress["in_progress"] is True
        assert in_progress["page"] == 5
        assert unread["read"] is False and unread["in_progress"] is False


class TestKomgaOndeck:
    def test_maps_ondeck_books(self, app_module, requests_mock):
        requests_mock.get(
            "http://komga.test:25600/api/v1/books/ondeck",
            json={"content": [{
                "id": "b2", "seriesId": "s1", "seriesTitle": "Batman",
                "metadata": {"title": "Batman 002", "number": "2"},
                "media": {"pagesCount": 24}, "readProgress": {"completed": False, "page": 5},
            }]},
        )

        result = app_module.fetch_komga_ondeck(FAKE_KOMGA, "Komga1")

        item = result["items"][0]
        assert item["series_title"] == "Batman"
        assert item["page"] == 5
        assert item["open_url"] == "http://komga.test:25600/book/b2/read"


class TestKomgaLatest:
    def test_maps_latest_series(self, app_module, requests_mock):
        requests_mock.get(
            "http://komga.test:25600/api/v1/series/latest",
            json={"content": [{
                "id": "s9", "name": "New Series", "booksCount": 1, "booksReadCount": 0,
                "booksUnreadCount": 1, "booksInProgressCount": 0,
                "metadata": {"title": "New Series", "publisher": "Image", "status": "ONGOING"},
            }]},
        )

        result = app_module.fetch_komga_latest(FAKE_KOMGA, "Komga1")

        assert result["items"][0]["title"] == "New Series"


class TestKomgaCoverProxy:
    def test_proxies_series_cover_with_api_key_header(self, app_module, client):
        import requests_mock as rm
        app_module.CONFIG["media_servers"] = [
            {"name": "Komga1", "type": "komga", "url": "http://komga.test:25600", "credential": "fake-key", "enabled": True}
        ]
        app_module.MEDIA_SERVERS = app_module.CONFIG["media_servers"]
        app_module.CONFIG["users"] = [{
            "username": "testadmin", "password_hash": "x", "role": "admin",
            "permissions": {"requesting": True, "searching": True},
        }]
        app_module.USERS = app_module.CONFIG["users"]

        with client.session_transaction() as sess:
            sess["username"] = "testadmin"

        with rm.Mocker() as m:
            m.get("http://komga.test:25600/api/v1/series/s1/thumbnail", content=b"fake-image-bytes",
                  headers={"Content-Type": "image/jpeg"})
            response = client.get("/api/komga/cover/Komga1/series/s1")

        assert response.status_code == 200
        assert response.data == b"fake-image-bytes"
        assert m.request_history[0].headers["X-API-Key"] == "fake-key"
