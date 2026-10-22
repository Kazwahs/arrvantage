"""
Needs attention beyond the arr apps: downloaders, indexer services, media
servers and transcoders - what counts as a problem, how severe it is, what
can be hidden, and how the probes are wired up.
"""
HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731

OK_SIGNALS = {"Radarr": {"queue": {"queue_items": []}, "health": {"health_items": []}, "diskspace": {"diskspace_items": []}}}


def summarize(app_module, **services):
    return app_module.summarize_attention(OK_SIGNALS, services)


def indexer(name, healthy=True, enabled=True):
    return {"name": name, "healthy": healthy, "enabled": enabled}


class TestUnreachableServices:
    def test_a_down_downloader_is_critical_and_cannot_be_hidden(self, app_module):
        data = summarize(app_module, downloaders={"qBittorrent": {"error": "connection refused"}})
        item = data["items"][0]
        assert (item["severity"], item["kind"], item["hideable"], item["hide_key"]) == ("critical", "unreachable", False, None)
        assert item["service_type"] == "downloader" and item["services"] == ["qBittorrent"]
        assert item["detail"] == "connection refused"

    def test_down_indexer_service_and_media_server_are_critical(self, app_module):
        data = summarize(app_module, indexers={"Prowlarr": {"error": "x"}}, media_servers={"Plex": {"error": "y"}})
        assert {(i["service_type"], i["severity"]) for i in data["items"]} == {("indexer", "critical"), ("mediaserver", "critical")}

    def test_a_down_transcoder_is_only_a_hideable_warning(self, app_module):
        # Tdarr is often switched off on purpose.
        item = summarize(app_module, transcoders={"Tdarr": {"error": "timed out"}})["items"][0]
        assert (item["severity"], item["hideable"], item["hide_key"]) == ("warning", True, "service:transcoder:Tdarr:down")

    def test_a_failed_probe_makes_the_check_incomplete(self, app_module):
        # so a hidden warning isn't forgotten just because the service was briefly down
        assert summarize(app_module, indexers={"Prowlarr": {"error": "x"}})["complete"] is False
        assert summarize(app_module, indexers={"Prowlarr": {"error": None, "indexers": []}})["complete"] is True

    def test_same_name_in_two_categories_does_not_collide(self, app_module):
        data = summarize(app_module, downloaders={"Main": {"error": "a"}}, media_servers={"Main": {"error": "b"}})
        assert len({i["id"] for i in data["items"]}) == 2


class TestFailingIndexers:
    def test_some_failing_indexers_are_not_raised_here(self, app_module):
        # The arr apps already report these by name; Home's Indexers panel lists them too.
        result = {"error": None, "indexers": [indexer("Nyaa.si", False), indexer("1337x")]}
        assert summarize(app_module, indexers={"Prowlarr": result})["items"] == []

    def test_every_enabled_indexer_failing_is_critical_and_named(self, app_module):
        result = {"error": None, "indexers": [indexer("A", False), indexer("B", False), indexer("Off", True, enabled=False)]}
        item = summarize(app_module, indexers={"Prowlarr": result})["items"][0]
        assert (item["severity"], item["hideable"], item["hide_key"]) == ("critical", False, None)
        assert item["title"] == "Every indexer is failing in Prowlarr" and item["detail"] == "A, B"

    def test_a_disabled_indexer_is_a_choice_not_a_fault(self, app_module):
        result = {"error": None, "indexers": [indexer("Old", False, enabled=False), indexer("Good")]}
        assert summarize(app_module, indexers={"Prowlarr": result})["items"] == []

    def test_no_indexers_at_all_is_not_a_failure(self, app_module):
        assert summarize(app_module, indexers={"Prowlarr": {"error": None, "indexers": []}})["items"] == []

    def test_a_long_list_of_names_is_trimmed(self, app_module):
        result = {"error": None, "indexers": [indexer(f"Idx{i}", False) for i in range(9)]}
        detail = summarize(app_module, indexers={"Prowlarr": result})["items"][0]["detail"]
        assert detail.endswith("and 3 more") and "Idx5" in detail and "Idx6" not in detail


class TestArrIndexerWarningsAreNotRepeated:
    HEALTH = {"health_items": [{"type": "warning", "source": "IndexerStatusCheck",
                                "message": "Indexers unavailable due to failures: A (Prowlarr), B (Prowlarr)"}]}

    def _signals(self, health):
        return {"Radarr": {"queue": {"queue_items": []}, "health": health, "diskspace": {"diskspace_items": []}}}

    def test_repeated_when_the_indexer_service_already_says_everything_is_failing(self, app_module):
        down = {"error": None, "indexers": [indexer("A", False), indexer("B", False)]}
        data = app_module.summarize_attention(self._signals(self.HEALTH), {"indexers": {"Prowlarr": down}})
        assert [i["id"] for i in data["items"]] == ["service:indexers:Prowlarr"]

    def test_kept_when_it_names_something_else(self, app_module):
        down = {"error": None, "indexers": [indexer("A", False), indexer("Z", False)]}
        data = app_module.summarize_attention(self._signals(self.HEALTH), {"indexers": {"Prowlarr": down}})
        assert {i["kind"] for i in data["items"]} == {"service", "health"}

    def test_kept_when_prowlarr_reports_no_problem(self, app_module):
        fine = {"error": None, "indexers": [indexer("A"), indexer("B")]}
        data = app_module.summarize_attention(self._signals(self.HEALTH), {"indexers": {"Prowlarr": fine}})
        assert [i["kind"] for i in data["items"]] == ["health"]


class TestDownloaderConnection:
    def test_a_firewalled_client_is_no_longer_a_warning(self, app_module):
        for status in ("firewalled", "disconnected", "connected"):
            assert summarize(app_module, downloaders={"qb": {"error": None, "connection_status": status}})["items"] == []

    def test_a_client_that_does_not_answer_is_still_critical(self, app_module):
        assert summarize(app_module, downloaders={"qb": {"error": "refused"}})["items"][0]["severity"] == "critical"


class TestSummaryShape:
    def test_services_count_toward_checked(self, app_module):
        data = summarize(app_module, downloaders={"qb": {"error": None}}, transcoders={"Tdarr": {"error": None}})
        assert data["checked"] == 3

    def test_a_down_transcoder_ranks_after_a_stalled_download_and_before_health(self, app_module):
        health = {"health_items": [{"type": "warning", "source": "S", "message": "m"}]}
        stalled = {"queue_items": [{"title": "t", "status": "failed"}]}
        signals = {"Radarr": {"queue": stalled, "health": health, "diskspace": {"diskspace_items": []}}}
        data = app_module.summarize_attention(signals, {"transcoders": {"Tdarr": {"error": "off"}}})
        assert [i["kind"] for i in data["items"]] == ["queue", "service", "health"]

    def test_arr_only_callers_still_work(self, app_module):
        assert app_module.summarize_attention(OK_SIGNALS)["checked"] == 1


class TestHidingServiceWarnings:
    def test_service_keys_are_valid_hide_keys(self, app_module):
        assert app_module.valid_hide_key("service:transcoder:Tdarr:down")
        assert not app_module.valid_hide_key("transcoder:Tdarr")

    def test_a_hidden_transcoder_warning_moves_to_the_hidden_list(self, app_module):
        data = summarize(app_module, transcoders={"Tdarr": {"error": "off"}})
        shown = app_module.apply_hidden_attention(data, ["service:transcoder:Tdarr:down"])
        assert shown["items"] == [] and len(shown["hidden_items"]) == 1

    def test_hide_endpoint_accepts_a_service_key(self, app_module, admin_session):
        client, csrf = admin_session
        response = client.post("/api/attention/hide", headers=HEADERS(csrf),
                               json={"key": "service:transcoder:Tdarr:down", "hidden": True})
        assert response.get_json()["hidden"] == ["service:transcoder:Tdarr:down"]


class TestProbes:
    def _install(self, app_module, monkeypatch, **fakes):
        app_module.APPS.clear()
        app_module.DOWNLOADERS[:] = [{"name": "qBittorrent", "type": "qbittorrent", "url": "http://q", "api_key": "k"}]
        app_module.INDEXERS[:] = [{"name": "Prowlarr", "type": "prowlarr", "url": "http://p", "api_key": "k"}]
        app_module.MEDIA_SERVERS[:] = [
            {"name": "Plex", "type": "plex", "url": "http://plex", "credential": "t"},
            {"name": "Jelly", "type": "jellyfin", "url": "http://jf", "credential": "t"},
            {"name": "Komga", "type": "komga", "url": "http://k", "credential": "t"},
            {"name": "Off", "type": "plex", "url": "http://off", "credential": "t", "enabled": False},
        ]
        app_module.TRANSCODERS[:] = [{"name": "Tdarr", "type": "tdarr", "url": "http://t", "api_key": ""}]
        monkeypatch.setattr(app_module, "fetch_qbittorrent_transfer_info", lambda d: {"error": None, "connection_status": "firewalled"})
        monkeypatch.setattr(app_module, "fetch_prowlarr_indexers", lambda s: {"error": None, "indexers": [indexer("A", False), indexer("B")]})
        monkeypatch.setattr(app_module, "probe_media_server", lambda s: {"error": "connection refused"} if s["name"] == "Jelly" else {"error": None})
        monkeypatch.setattr(app_module, "fetch_komga_libraries", lambda s: {"error": None, "libraries": []})
        monkeypatch.setattr(app_module, "fetch_tdarr_status", lambda s: {"error": "could not connect"})
        for name, fn in fakes.items():
            monkeypatch.setattr(app_module, name, fn)
        app_module._ATTENTION_CACHE.update({"at": 0.0, "data": None})

    def test_every_kind_of_service_is_probed(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        self._install(app_module, monkeypatch)
        data = client.get("/api/attention").get_json()
        titles = sorted(i["title"] for i in data["items"])
        assert titles == ["Jelly can't be reached", "Tdarr can't be reached"]
        assert data["checked"] == 6  # the disabled Plex entry is skipped
        assert data["complete"] is False

    def test_media_server_probe_uses_public_endpoints_without_a_token(self, app_module, requests_mock):
        plex = requests_mock.get("http://plex/identity", json={"MediaContainer": {}})
        jelly = requests_mock.get("http://jf/System/Info/Public", json={"ServerName": "x"})
        emby = requests_mock.get("http://emby/System/Info/Public", json={})
        for kind, url in (("plex", "http://plex"), ("jellyfin", "http://jf"), ("emby", "http://emby")):
            assert app_module.probe_media_server({"type": kind, "url": url + "/", "credential": "wrong"}) == {"error": None}
        for mock in (plex, jelly, emby):
            assert mock.called and "token" not in str(mock.last_request.headers).lower()

    def test_a_server_that_rejects_the_token_is_not_reported_down(self, app_module, requests_mock):
        # the probe never sends the token, so a bad or limited one can't look like an outage
        requests_mock.get("http://jf/System/Info/Public", json={})
        requests_mock.get("http://jf/Sessions", status_code=403)
        assert app_module.probe_media_server({"type": "jellyfin", "url": "http://jf", "credential": "limited"}) == {"error": None}

    def test_a_real_outage_is_reported_after_one_retry(self, app_module, requests_mock):
        import requests
        mock = requests_mock.get("http://jf/System/Info/Public", exc=requests.exceptions.ConnectTimeout)
        assert app_module.probe_media_server({"type": "jellyfin", "url": "http://jf"})["error"]
        assert mock.call_count == 2

    def test_a_blip_that_clears_on_the_retry_is_not_reported(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://jf/System/Info/Public", [{"exc": requests.exceptions.ConnectTimeout}, {"json": {}}])
        assert app_module.probe_media_server({"type": "jellyfin", "url": "http://jf"}) == {"error": None}

    def test_an_http_error_page_counts_as_down(self, app_module, requests_mock):
        requests_mock.get("http://plex/identity", status_code=502)
        assert "502" in app_module.probe_media_server({"type": "plex", "url": "http://plex"})["error"]

    def test_komga_is_checked_through_its_library_list(self, app_module, monkeypatch):
        monkeypatch.setattr(app_module, "fetch_komga_libraries", lambda s: {"error": "HTTP error 401", "libraries": []})
        assert app_module.probe_media_server({"type": "komga"}) == {"error": "HTTP error 401"}

    def test_sabnzbd_probe_reports_only_reachability(self, app_module, monkeypatch):
        monkeypatch.setattr(app_module, "fetch_sabnzbd_queue", lambda d: {"error": "nope", "items": [], "summary": {}})
        assert app_module.probe_downloader({"type": "sabnzbd"}) == {"error": "nope"}

    def test_home_stats_names_the_failing_indexers(self, app_module, monkeypatch):
        self._install(app_module, monkeypatch)
        monkeypatch.setattr(app_module, "fetch_prowlarr_indexers", lambda s: {
            "error": None, "indexers": [indexer("A", False), indexer("B"), indexer("Old", False, enabled=False)]})
        stats = app_module.fetch_home_stats()
        assert stats["indexers"][0]["unhealthy_names"] == ["A"]
