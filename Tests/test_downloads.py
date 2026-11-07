"""
Home > Downloads: a card per download client with its speed and the few
downloads that are actually moving.

qBittorrent: GET /api/v2/torrents/info?filter=downloading and
stalled_downloading, plus /api/v2/transfer/info. SABnzbd: mode=queue&limit.
"""
import requests

QBIT = "http://qbit.test:8080"
SAB = "http://sab.test:8085"


def add_client(app_module, name, kind, url, enabled=True, color="#4d8fdd"):
    app_module.CONFIG.setdefault("downloaders", []).append(
        {"name": name, "type": kind, "url": url, "api_key": "k", "color": color, "enabled": enabled})
    app_module.apply_config(app_module.CONFIG)


def torrent(name="Linux ISO", state="downloading", progress=0.5, dlspeed=1_000_000, eta=600, hash_=None):
    return {"name": name, "state": state, "progress": progress, "dlspeed": dlspeed, "eta": eta, "hash": hash_ or name}


TRANSFER = {"dl_speed": 2_500_000, "up_speed": 300_000, "connection_status": "connected"}


class TestQbittorrentView:
    def test_a_moving_torrent_is_listed_with_progress_speed_and_eta(self, app_module):
        view = app_module.qbt_downloads_view([torrent(progress=0.256, dlspeed=1_500_000, eta=125)], TRANSFER)
        assert view["items"] == [{"name": "Linux ISO", "progress": 25.6, "speed": 1_500_000, "eta": "2 min", "group": "downloading"}]
        assert (view["down_speed"], view["up_speed"], view["connection"], view["state"]) == (2_500_000, 300_000, "connected", "downloading")

    def test_only_download_states_count(self, app_module):
        torrents = [torrent("a", "pausedDL"), torrent("b", "stoppedDL"), torrent("c", "uploading"), torrent("d", "error"),
                    torrent("e", "stalledUP"), torrent("f", "downloading")]
        view = app_module.qbt_downloads_view(torrents, TRANSFER)
        assert [i["name"] for i in view["items"]] == ["f"] and view["total"] == 1

    def test_a_torrent_returned_by_both_filters_counts_once(self, app_module):
        view = app_module.qbt_downloads_view([torrent("x", "stalledDL", hash_="h1"), torrent("x", "stalledDL", hash_="h1")], TRANSFER)
        assert view["total"] == 1 and view["counts"]["stalled"] == 1

    def test_moving_ones_come_first_then_the_fastest(self, app_module):
        torrents = [torrent("queued", "queuedDL", dlspeed=0), torrent("stalled", "stalledDL", dlspeed=0),
                    torrent("slow", dlspeed=10), torrent("fast", dlspeed=9_000)]
        names = [i["name"] for i in app_module.qbt_downloads_view(torrents, TRANSFER)["items"]]
        assert names == ["fast", "slow", "stalled", "queued"]

    def test_the_list_is_capped_but_the_total_is_not(self, app_module):
        torrents = [torrent(f"t{i}", dlspeed=i) for i in range(12)]
        view = app_module.qbt_downloads_view(torrents, TRANSFER)
        assert len(view["items"]) == app_module.DOWNLOADS_ITEM_LIMIT and view["total"] == 12

    def test_no_eta_means_blank(self, app_module):
        view = app_module.qbt_downloads_view([torrent(eta=8640000), torrent("b", eta=-1)], TRANSFER)
        assert [i["eta"] for i in view["items"]] == ["", ""]

    def test_idle_waiting_and_garbage(self, app_module):
        assert app_module.qbt_downloads_view([], {"dl_speed": 0, "up_speed": 0})["state"] == "idle"
        waiting = app_module.qbt_downloads_view([torrent(state="queuedDL", dlspeed=0)], {"dl_speed": 0, "up_speed": 0})
        assert waiting["state"] == "waiting"
        view = app_module.qbt_downloads_view(["x", None, {"state": "downloading", "progress": "nope", "dlspeed": "fast"}], {})
        assert view["items"][0]["progress"] == 0.0 and view["items"][0]["speed"] == 0 and view["down_speed"] == 0


class TestSabnzbdView:
    QUEUE = {"queue": {"status": "Downloading", "paused": False, "kbpersec": "2048.5", "noofslots_total": "3", "slots": [
        {"nzo_id": "a", "filename": "Queued.Job", "status": "Queued", "percentage": "0", "timeleft": "0:00:00"},
        {"nzo_id": "b", "filename": "Moving.Job", "status": "Downloading", "percentage": "42.3", "timeleft": "0:12:30"},
        {"nzo_id": "c", "filename": "Held.Job", "status": "Paused", "percentage": "10", "timeleft": "0:05:00"},
    ]}}

    def test_active_jobs_first_and_paused_ones_are_counted_not_listed(self, app_module):
        view = app_module.sab_downloads_view(self.QUEUE)
        assert [i["name"] for i in view["items"]] == ["Moving.Job", "Queued.Job"]
        assert view["items"][0]["progress"] == 42.3 and view["items"][0]["eta"] == "0:12:30"
        assert view["items"][1]["eta"] == ""
        assert view["counts"] == {"downloading": 1, "paused": 1, "queued": 1, "preparing": 0}
        assert (view["down_speed"], view["total"], view["state"], view["up_speed"]) == (int(2048.5 * 1024), 3, "downloading", None)

    def test_a_paused_queue_says_so(self, app_module):
        view = app_module.sab_downloads_view({"queue": {"paused": True, "kbpersec": "0", "noofslots_total": 2, "slots": []}})
        assert view["paused"] is True and view["state"] == "paused" and view["total"] == 2

    def test_an_empty_queue_is_idle(self, app_module):
        assert app_module.sab_downloads_view({"queue": {"slots": [], "noofslots_total": 0}})["state"] == "idle"

    def test_fetching_and_propagating_jobs_are_preparing(self, app_module):
        view = app_module.sab_downloads_view({"queue": {"slots": [{"filename": "x", "status": "Fetching"}]}})
        assert view["counts"]["preparing"] == 1 and view["total"] == 1

    def test_garbage_does_not_break_it(self, app_module):
        for junk in (None, [], "x", {"queue": "nope"}, {"queue": {"slots": ["a", None], "kbpersec": "fast", "noofslots_total": "?"}}):
            assert app_module.sab_downloads_view(junk)["error"] is None


class TestFetching:
    def test_qbittorrent_asks_for_the_two_download_filters_with_its_token(self, app_module, requests_mock):
        add_client(app_module, "Qbit", "qbittorrent", QBIT)
        info = requests_mock.get(f"{QBIT}/api/v2/torrents/info", json=[torrent()])
        requests_mock.get(f"{QBIT}/api/v2/transfer/info", json={"dl_info_speed": 5, "up_info_speed": 6, "connection_status": "connected"})
        card = app_module.fetch_download_client(app_module.get_downloader("Qbit"))
        assert [r.qs["filter"] for r in info.request_history] == [["downloading"], ["stalled_downloading"]]
        assert info.last_request.headers["Authorization"] == "Bearer k"
        assert card["down_speed"] == 5 and card["up_speed"] == 6 and card["total"] == 1  # the same torrent from both calls

    def test_sabnzbd_limits_the_queue(self, app_module, requests_mock):
        add_client(app_module, "Sab", "sabnzbd", SAB)
        mock = requests_mock.get(f"{SAB}/api", json=TestSabnzbdView.QUEUE)
        card = app_module.fetch_download_client(app_module.get_downloader("Sab"))
        qs = mock.last_request.qs
        assert qs["mode"] == ["queue"] and qs["limit"] == [str(app_module.DOWNLOADS_SAB_FETCH_LIMIT)] and qs["apikey"] == ["k"]
        assert card["error"] is None and card["total"] == 3

    def test_failures_are_a_few_words_never_an_address(self, app_module, requests_mock):
        add_client(app_module, "Qbit", "qbittorrent", QBIT)
        client = app_module.get_downloader("Qbit")
        requests_mock.get(f"{QBIT}/api/v2/torrents/info", exc=requests.exceptions.ConnectionError("boom http://qbit.test:8080/?apikey=SECRET"))
        assert app_module.fetch_download_client(client) == {"error": "could not connect"}
        requests_mock.get(f"{QBIT}/api/v2/torrents/info", exc=requests.exceptions.ReadTimeout("slow"))
        assert app_module.fetch_download_client(client) == {"error": "timed out"}
        requests_mock.get(f"{QBIT}/api/v2/torrents/info", status_code=403)
        assert "check the API key" in app_module.fetch_download_client(client)["error"]
        requests_mock.get(f"{QBIT}/api/v2/torrents/info", status_code=500)
        assert app_module.fetch_download_client(client) == {"error": "HTTP 500"}
        requests_mock.get(f"{QBIT}/api/v2/torrents/info", text="<html>not json</html>")
        assert "unexpected answer" in app_module.fetch_download_client(client)["error"]

    def test_sabnzbd_api_error_body(self, app_module, requests_mock):
        add_client(app_module, "Sab", "sabnzbd", SAB)
        requests_mock.get(f"{SAB}/api", json={"status": False, "error": "API Key Incorrect"})
        assert "check the API key" in app_module.fetch_download_client(app_module.get_downloader("Sab"))["error"]


class TestEndpoint:
    def _fake(self, monkeypatch, app_module, views):
        calls = []

        def fake(downloader):
            calls.append(downloader["name"])
            return views[downloader["name"]]

        monkeypatch.setattr(app_module, "fetch_download_client", fake)
        return calls

    def test_requires_login(self, client):
        assert client.get("/api/downloads").status_code in (302, 401, 403)

    def test_cards_totals_and_failed_clients(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        add_client(app_module, "Qbit", "qbittorrent", QBIT)
        add_client(app_module, "Sab", "sabnzbd", SAB, color="#e8a33d")
        add_client(app_module, "Off", "sabnzbd", "http://off.test", enabled=False)
        self._fake(monkeypatch, app_module, {
            "Qbit": app_module.qbt_downloads_view([torrent()], TRANSFER),
            "Sab": {"error": "could not connect"},
        })
        body = client.get("/api/downloads").get_json()
        assert [c["name"] for c in body["clients"]] == ["Qbit", "Sab"]
        assert body["clients"][0]["color"] == "#4d8fdd" and body["clients"][1]["error"] == "could not connect"
        assert (body["down_speed"], body["up_speed"]) == (2_500_000, 300_000)  # the failed client adds nothing

    def test_no_clients_gives_an_empty_list(self, app_module, admin_session):
        client, _ = admin_session
        assert client.get("/api/downloads").get_json() == {"clients": [], "down_speed": 0, "up_speed": 0}

    def test_the_answer_is_shared_for_a_few_seconds(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        add_client(app_module, "Sab", "sabnzbd", SAB)
        calls = self._fake(monkeypatch, app_module, {"Sab": app_module.sab_downloads_view(TestSabnzbdView.QUEUE)})
        client.get("/api/downloads")
        client.get("/api/downloads")
        assert calls == ["Sab"]
        client.get("/api/downloads?refresh=1")
        assert calls == ["Sab", "Sab"]
