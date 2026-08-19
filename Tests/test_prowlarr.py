"""
Tests for the Prowlarr integration, using mocked HTTP responses shaped
like the real API responses confirmed earlier in this project (via
Prowlarr's own OpenAPI spec and real test data) - not guessed shapes.
This locks in that confirmed-correct behavior against future
regression, since nothing else would catch a future code change
silently breaking these field-name assumptions again.
"""

FAKE_SERVICE = {"name": "Prowlarr", "type": "prowlarr", "url": "http://prowlarr.test:9696", "api_key": "fake-key-123"}


class TestFetchProwlarrIndexers:
    def test_healthy_indexer_has_no_reason(self, app_module, requests_mock):
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexer",
            json=[{"id": 1, "name": "SomeTracker", "enable": True, "protocol": "torrent"}],
        )
        requests_mock.get("http://prowlarr.test:9696/api/v1/indexerstatus", json=[])

        result = app_module.fetch_prowlarr_indexers(FAKE_SERVICE)

        assert result["error"] is None
        assert len(result["indexers"]) == 1
        indexer = result["indexers"][0]
        assert indexer["healthy"] is True
        assert indexer["unhealthy_reason"] == ""

    def test_unhealthy_indexer_shows_disabled_till_reason(self, app_module, requests_mock):
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexer",
            json=[{"id": 30, "name": "ExtraTorrent.st", "enable": True, "protocol": "torrent"}],
        )
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexerstatus",
            json=[{
                "id": 5, "indexerId": 30,
                "disabledTill": "2026-08-19T01:01:43Z",
                "mostRecentFailure": "2026-08-18T01:01:43Z",
                "initialFailure": "2026-08-17T01:01:43Z",
            }],
        )

        result = app_module.fetch_prowlarr_indexers(FAKE_SERVICE)

        indexer = result["indexers"][0]
        assert indexer["healthy"] is False
        assert "2026-08-19T01:01:43Z" in indexer["unhealthy_reason"]

    def test_unhealthy_falls_back_to_most_recent_failure_when_not_disabled(self, app_module, requests_mock):
        # disabledTill absent (indexer failing but not auto-disabled) -
        # should fall back to the mostRecentFailure timestamp instead.
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexer",
            json=[{"id": 7, "name": "SomeTracker", "enable": True, "protocol": "usenet"}],
        )
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexerstatus",
            json=[{"id": 1, "indexerId": 7, "disabledTill": None, "mostRecentFailure": "2026-08-18T10:00:00Z", "initialFailure": None}],
        )

        result = app_module.fetch_prowlarr_indexers(FAKE_SERVICE)

        assert "2026-08-18T10:00:00Z" in result["indexers"][0]["unhealthy_reason"]

    def test_request_failure_returns_error_not_exception(self, app_module, requests_mock):
        requests_mock.get("http://prowlarr.test:9696/api/v1/indexer", status_code=500)

        result = app_module.fetch_prowlarr_indexers(FAKE_SERVICE)

        assert result["error"] is not None
        assert result["indexers"] == []


class TestTestProwlarrIndexer:
    def test_successful_test_returns_empty_string(self, app_module, requests_mock):
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexer/1",
            json={"id": 1, "name": "SomeTracker", "enable": True},
        )
        requests_mock.post("http://prowlarr.test:9696/api/v1/indexer/test", status_code=200)

        error = app_module.test_prowlarr_indexer(FAKE_SERVICE, 1)

        assert error == ""

    def test_failed_test_returns_error_message(self, app_module, requests_mock):
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexer/1",
            json={"id": 1, "name": "SomeTracker", "enable": True},
        )
        requests_mock.post(
            "http://prowlarr.test:9696/api/v1/indexer/test",
            status_code=400,
            json=[{"propertyName": "ApiKey", "errorMessage": "Unable to connect to indexer"}],
        )

        error = app_module.test_prowlarr_indexer(FAKE_SERVICE, 1)

        assert "Unable to connect to indexer" in error

    def test_uses_full_object_post_not_test_by_id(self, app_module, requests_mock):
        """
        The whole point of this fix (see project history) was that
        testing an indexer requires GETting its full config, then
        POSTing that whole object to a plain /test endpoint - not
        POSTing to /test/{id}. This test would fail if that regressed
        back to the old, incorrect shape.
        """
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/indexer/42",
            json={"id": 42, "name": "SomeTracker", "enable": True},
        )
        test_call = requests_mock.post("http://prowlarr.test:9696/api/v1/indexer/test", status_code=200)

        app_module.test_prowlarr_indexer(FAKE_SERVICE, 42)

        assert test_call.called
        assert test_call.last_request.json()["id"] == 42


class TestTestAllProwlarrIndexers:
    def test_successful_testall(self, app_module, requests_mock):
        requests_mock.post("http://prowlarr.test:9696/api/v1/indexer/testall", status_code=200)
        error = app_module.test_all_prowlarr_indexers(FAKE_SERVICE)
        assert error == ""

    def test_failed_testall_returns_error(self, app_module, requests_mock):
        requests_mock.post("http://prowlarr.test:9696/api/v1/indexer/testall", status_code=500, text="Server Error")
        error = app_module.test_all_prowlarr_indexers(FAKE_SERVICE)
        assert error != ""
