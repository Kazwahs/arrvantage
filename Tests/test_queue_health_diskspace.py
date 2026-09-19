"""
Tests for the queue/health/diskspace enhancements - shaped against
Radarr's real QueueResource/HealthResource/DiskSpaceResource fields
(confirmed by reading Radarr's current C# source directly, not
guessed), covering both the pure-logic helpers and the network-facing
fetch_* functions.
"""

FAKE_CONFIG = {
    "url": "http://radarr.test:7878",
    "api_key": "fake-key-123",
    "queue_endpoint": "/api/v3/queue",
    "history_endpoint": "/api/v3/history",
    "health_endpoint": "/api/v3/health",
    "diskspace_endpoint": "/api/v3/diskspace",
}


class TestGetQueueProgressPct:
    def test_in_progress_download(self, app_module):
        record = {"size": 1000, "sizeleft": 250}
        assert app_module.get_queue_progress_pct(record) == 75

    def test_size_missing_returns_none(self, app_module):
        # Radarr doesn't always know the size right away (e.g. still
        # resolving the release) - must not raise a ZeroDivisionError.
        record = {"sizeleft": 250}
        assert app_module.get_queue_progress_pct(record) is None

    def test_sizeleft_missing_returns_none(self, app_module):
        record = {"size": 1000}
        assert app_module.get_queue_progress_pct(record) is None

    def test_zero_size_returns_none(self, app_module):
        record = {"size": 0, "sizeleft": 0}
        assert app_module.get_queue_progress_pct(record) is None

    def test_fully_downloaded(self, app_module):
        record = {"size": 1000, "sizeleft": 0}
        assert app_module.get_queue_progress_pct(record) == 100


class TestGetQueueStatusMessages:
    def test_no_messages(self, app_module):
        assert app_module.get_queue_status_messages({}) == []

    def test_flattens_nested_status_messages(self, app_module):
        record = {
            "statusMessages": [
                {"title": "movie.mkv", "messages": ["Sample", "Unpacking"]},
                {"title": "movie2.mkv", "messages": ["Missing from data"]},
            ]
        }
        assert app_module.get_queue_status_messages(record) == [
            "Sample", "Unpacking", "Missing from data",
        ]

    def test_includes_top_level_error_message(self, app_module):
        record = {"errorMessage": "Download client returned an error"}
        assert app_module.get_queue_status_messages(record) == [
            "Download client returned an error",
        ]

    def test_combines_both(self, app_module):
        record = {
            "statusMessages": [{"title": "x", "messages": ["Sample"]}],
            "errorMessage": "Qbittorrent unreachable",
        }
        assert app_module.get_queue_status_messages(record) == ["Sample", "Qbittorrent unreachable"]


class TestFetchQueueEnriched:
    def test_full_record_shape(self, app_module, requests_mock):
        requests_mock.get(
            "http://radarr.test:7878/api/v3/queue",
            json={"records": [{
                "title": "Some.Movie.2026.1080p",
                "movie": {"title": "Some Movie"},
                "status": "downloading",
                "timeleft": "00:12:00",
                "size": 4_000_000_000,
                "sizeleft": 1_000_000_000,
                "indexer": "NZBgeek",
                "downloadClient": "SABnzbd",
                "protocol": "usenet",
                "quality": {"quality": {"id": 7, "name": "Bluray-1080p"}},
                "customFormatScore": 25,
                "statusMessages": [],
                "errorMessage": None,
            }]},
        )

        result = app_module.fetch_queue("Radarr", FAKE_CONFIG)

        assert result["error"] is None
        item = result["queue_items"][0]
        assert item["title"] == "Some Movie"
        assert item["progress_pct"] == 75
        assert item["indexer"] == "NZBgeek"
        assert item["download_client"] == "SABnzbd"
        assert item["protocol"] == "usenet"
        assert item["quality"] == "Bluray-1080p"
        assert item["custom_format_score"] == 25
        assert item["status_messages"] == []
        assert item["size_human"] == "3.7 GB"

    def test_stalled_download_surfaces_status_messages(self, app_module, requests_mock):
        requests_mock.get(
            "http://radarr.test:7878/api/v3/queue",
            json={"records": [{
                "title": "Broken.Release",
                "status": "warning",
                "size": 100, "sizeleft": 100,
                "statusMessages": [{"title": "Broken.Release", "messages": ["Sample"]}],
                "errorMessage": None,
            }]},
        )

        result = app_module.fetch_queue("Radarr", FAKE_CONFIG)

        assert result["queue_items"][0]["status_messages"] == ["Sample"]

    def test_connection_error_returns_error_not_exception(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://radarr.test:7878/api/v3/queue", exc=requests.exceptions.ConnectionError)

        result = app_module.fetch_queue("Radarr", FAKE_CONFIG)

        assert result["error"] is not None
        assert result["queue_items"] == []


class TestFetchHealth:
    def test_maps_health_checks(self, app_module, requests_mock):
        requests_mock.get(
            "http://radarr.test:7878/api/v3/health",
            json=[
                {
                    "source": "IndexerRssCheck",
                    "type": "warning",
                    "message": "Indexers unavailable due to failures: NZBgeek",
                    "wikiUrl": "https://wiki.servarr.com/radarr/system#indexers-are-unavailable",
                },
                {
                    "source": "RootFolderCheck",
                    "type": "error",
                    "message": "Missing root folder: /movies",
                    "wikiUrl": "",
                },
            ],
        )

        result = app_module.fetch_health("Radarr", FAKE_CONFIG)

        assert result["error"] is None
        assert len(result["health_items"]) == 2
        assert result["health_items"][0]["type"] == "warning"
        assert result["health_items"][1]["type"] == "error"
        assert result["health_items"][1]["message"] == "Missing root folder: /movies"

    def test_no_issues_returns_empty_list(self, app_module, requests_mock):
        requests_mock.get("http://radarr.test:7878/api/v3/health", json=[])

        result = app_module.fetch_health("Radarr", FAKE_CONFIG)

        assert result["health_items"] == []

    def test_http_error_returns_error_not_exception(self, app_module, requests_mock):
        requests_mock.get("http://radarr.test:7878/api/v3/health", status_code=401)

        result = app_module.fetch_health("Radarr", FAKE_CONFIG)

        assert result["error"] is not None
        assert result["health_items"] == []


class TestFetchDiskspace:
    def test_computes_percent_free(self, app_module, requests_mock):
        requests_mock.get(
            "http://radarr.test:7878/api/v3/diskspace",
            json=[{
                "path": "/movies",
                "label": "",
                "freeSpace": 50 * (1024 ** 3),
                "totalSpace": 500 * (1024 ** 3),
            }],
        )

        result = app_module.fetch_diskspace("Radarr", FAKE_CONFIG)

        assert result["error"] is None
        item = result["diskspace_items"][0]
        assert item["path"] == "/movies"
        assert item["percent_free"] == 10
        assert item["free_human"] == "50.0 GB"
        assert item["total_human"] == "500.0 GB"

    def test_zero_total_space_does_not_raise(self, app_module, requests_mock):
        requests_mock.get(
            "http://radarr.test:7878/api/v3/diskspace",
            json=[{"path": "/unmounted", "freeSpace": 0, "totalSpace": 0}],
        )

        result = app_module.fetch_diskspace("Radarr", FAKE_CONFIG)

        assert result["diskspace_items"][0]["percent_free"] is None

    def test_connection_error_returns_error_not_exception(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://radarr.test:7878/api/v3/diskspace", exc=requests.exceptions.ConnectionError)

        result = app_module.fetch_diskspace("Radarr", FAKE_CONFIG)

        assert result["error"] is not None
        assert result["diskspace_items"] == []
