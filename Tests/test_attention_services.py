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
    def test_failing_indexers_are_named(self, app_module):
        result = {"error": None, "indexers": [indexer("Nyaa.si", False), indexer("1337x"), indexer("Torrent Downloads", False)]}
        item = summarize(app_module, indexers={"Prowlarr": result})["items"][0]
        assert item["title"] == "2 of 3 indexers failing in Prowlarr"
        assert item["detail"] == "Nyaa.si, Torrent Downloads"
        assert (item["severity"], item["hideable"], item["hide_key"]) == ("warning", True, "service:indexers:Prowlarr")

    def test_a_disabled_indexer_is_a_choice_not_a_fault(self, app_module):
        result = {"error": None, "indexers": [indexer("Old", False, enabled=False), indexer("Good")]}
        assert summarize(app_module, indexers={"Prowlarr": result})["items"] == []

    def test_every_enabled_indexer_failing_is_critical(self, app_module):
        result = {"error": None, "indexers": [indexer("A", False), indexer("B", False), indexer("Off", True, enabled=False)]}
        item = summarize(app_module, indexers={"Prowlarr": result})["items"][0]
        assert (item["severity"], item["hideable"]) == ("critical", False)
        assert item["title"] == "Every indexer is failing in Prowlarr"

    def test_a_long_list_of_names_is_trimmed(self, app_module):
        result = {"error": None, "indexers": [indexer(f"Idx{i}", False) for i in range(9)] + [indexer("Fine")]}
        detail = summarize(app_module, indexers={"Prowlarr": result})["items"][0]["detail"]
        assert detail.endswith("and 3 more") and "Idx5" in detail and "Idx6" not in detail


class TestDownloaderConnection:
    def test_firewalled_qbittorrent_is_a_hideable_warning(self, app_module):
        item = summarize(app_module, downloaders={"qBittorrent": {"error": None, "connection_status": "firewalled"}})["items"][0]
        assert item["title"] == "qBittorrent is firewalled" and item["severity"] == "warning"
        assert item["hide_key"] == "service:downloader:qBittorrent:connection" and item["hideable"]

    def test_disconnected_is_reported_and_connected_is_not(self, app_module):
        gone = summarize(app_module, downloaders={"qb": {"error": None, "connection_status": "disconnected"}})["items"]
        fine = summarize(app_module, downloaders={"qb": {"error": None, "connection_status": "connected"}})["items"]
        assert [i["title"] for i in gone] == ["qb is disconnected"] and fine == []

    def test_sabnzbd_has_no_connection_status_and_is_fine(self, app_module):
        assert summarize(app_module, downloaders={"Sab": {"error": None}})["items"] == []


class TestSummaryShape:
    def test_services_count_toward_checked(self, app_module):
        data = summarize(app_module, downloaders={"qb": {"error": None}}, transcoders={"Tdarr": {"error": None}})
        assert data["checked"] == 3

    def test_service_warnings_rank_after_queue_and_before_health(self, app_module):
        health = {"health_items": [{"type": "warning", "source": "S", "message": "m"}]}
        stalled = {"queue_items": [{"title": "t", "status": "failed"}]}
        signals = {"Radarr": {"queue": stalled, "health": health, "diskspace": {"diskspace_items": []}}}
        data = app_module.summarize_attention(signals, {"downloaders": {"qb": {"error": None, "connection_status": "firewalled"}}})
        assert [i["kind"] for i in data["items"]] == ["queue", "service", "health"]

    def test_arr_only_callers_still_work(self, app_module):
        assert app_module.summarize_attention(OK_SIGNALS)["checked"] == 1


class TestHidingServiceWarnings:
    def test_service_keys_are_valid_hide_keys(self, app_module):
        assert app_module.valid_hide_key("service:indexers:Prowlarr")
        assert not app_module.valid_hide_key("indexers:Prowlarr")

    def test_a_hidden_indexer_warning_moves_to_the_hidden_list(self, app_module):
        result = {"error": None, "indexers": [indexer("A", False), indexer("B")]}
        data = summarize(app_module, indexers={"Prowlarr": result})
        shown = app_module.apply_hidden_attention(data, ["service:indexers:Prowlarr"])
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
        monkeypatch.setattr(app_module, "fetch_plex_now_playing", lambda s: {"error": None, "items": []})
        monkeypatch.setattr(app_module, "fetch_jellyfin_now_playing", lambda s: {"error": "HTTP 401", "items": []})
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
        assert titles == ["1 of 2 indexers failing in Prowlarr",
                          "Jelly can't be reached", "Tdarr can't be reached", "qBittorrent is firewalled"]
        assert data["checked"] == 6  # the disabled Plex entry is skipped
        assert data["complete"] is False

    def test_the_media_server_probe_matches_each_server_type(self, app_module, monkeypatch):
        calls = []
        monkeypatch.setattr(app_module, "fetch_plex_now_playing", lambda s: calls.append("plex") or {"error": None})
        monkeypatch.setattr(app_module, "fetch_jellyfin_now_playing", lambda s: calls.append("jf") or {"error": None})
        monkeypatch.setattr(app_module, "fetch_komga_libraries", lambda s: calls.append("komga") or {"error": None})
        for kind in ("plex", "jellyfin", "emby", "komga"):
            assert app_module.probe_media_server({"type": kind}) == {"error": None}
        assert calls == ["plex", "jf", "jf", "komga"]

    def test_sabnzbd_probe_reports_only_reachability(self, app_module, monkeypatch):
        monkeypatch.setattr(app_module, "fetch_sabnzbd_queue", lambda d: {"error": "nope", "items": [], "summary": {}})
        assert app_module.probe_downloader({"type": "sabnzbd"}) == {"error": "nope"}

    def test_home_stats_names_the_failing_indexers(self, app_module, monkeypatch):
        self._install(app_module, monkeypatch)
        monkeypatch.setattr(app_module, "fetch_prowlarr_indexers", lambda s: {
            "error": None, "indexers": [indexer("A", False), indexer("B"), indexer("Old", False, enabled=False)]})
        stats = app_module.fetch_home_stats()
        assert stats["indexers"][0]["unhealthy_names"] == ["A"]
