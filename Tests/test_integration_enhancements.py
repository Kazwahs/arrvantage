"""
Tests for the second round of API-integration enhancements: Overseerr/
Jellyseerr status translation, Prowlarr's own health endpoint, SABnzbd's
queue-wide stats, qBittorrent's seed/peer counts + global transfer info,
and transcode/direct-play status for Plex/Jellyfin/Tautulli now-playing.
All response shapes are taken from each project's own confirmed source/
API reference (see the comments in app.py next to each function), not
guessed.
"""

FAKE_REQUESTER = {"url": "http://overseerr.test:5055", "credential": "fake-key"}
FAKE_PROWLARR = {"name": "Prowlarr", "url": "http://prowlarr.test:9696", "api_key": "fake-key"}
FAKE_SABNZBD = {"url": "http://sabnzbd.test:8080", "api_key": "fake-key"}
FAKE_QBT = {"url": "http://qbt.test:8080"}
FAKE_PLEX = {"url": "http://plex.test:32400", "credential": "fake-token"}
FAKE_JELLYFIN = {"url": "http://jellyfin.test:8096", "credential": "fake-token"}


class TestSeerrRequestStatus:
    def test_translates_numeric_status_and_media_status(self, app_module, requests_mock):
        requests_mock.get(
            "http://overseerr.test:5055/api/v1/request",
            json={"results": [{
                "status": 2,  # Approved
                "media": {"mediaType": "movie", "tmdbId": 100, "status": 4},  # Partially Available
                "requestedBy": {"displayName": "Zak"},
                "createdAt": "2026-09-01T12:00:00.000Z",
                "is4k": False,
            }]},
        )
        requests_mock.get(
            "http://overseerr.test:5055/api/v1/movie/100",
            json={"title": "Some Movie", "posterPath": "/abc.jpg"},
        )

        result = app_module.fetch_seerr_requests(FAKE_REQUESTER, "movie")

        item = result["request_items"][0]
        assert item["status"] == "Approved"
        assert item["status_class"] == "success"
        assert item["media_status"] == "Partially Available"
        assert item["requested_date"] == "2026-09-01"
        assert item["is4k"] is False

    def test_declined_status_maps_to_error_class(self, app_module, requests_mock):
        requests_mock.get(
            "http://overseerr.test:5055/api/v1/request",
            json={"results": [{
                "status": 3, "media": {"mediaType": "tv", "tmdbId": 1, "status": 1},
                "requestedBy": {}, "createdAt": "",
            }]},
        )
        requests_mock.get("http://overseerr.test:5055/api/v1/tv/1", json={"name": "Some Show"})

        result = app_module.fetch_seerr_requests(FAKE_REQUESTER, "tv")

        assert result["request_items"][0]["status"] == "Declined"
        assert result["request_items"][0]["status_class"] == "error"

    def test_unrecognized_status_code_falls_back_gracefully(self, app_module, requests_mock):
        # Should never raise just because Overseerr adds a new status
        # value in some future version.
        requests_mock.get(
            "http://overseerr.test:5055/api/v1/request",
            json={"results": [{
                "status": 99, "media": {"mediaType": "movie", "tmdbId": 1, "status": 99},
                "requestedBy": {}, "createdAt": "",
            }]},
        )
        requests_mock.get("http://overseerr.test:5055/api/v1/movie/1", json={"title": "X"})

        result = app_module.fetch_seerr_requests(FAKE_REQUESTER, "movie")

        assert result["request_items"][0]["status"] == "Unknown"
        assert result["request_items"][0]["status_class"] == "notice"


class TestFetchProwlarrHealth:
    def test_maps_health_checks(self, app_module, requests_mock):
        requests_mock.get(
            "http://prowlarr.test:9696/api/v1/health",
            json=[{"source": "ApplicationLongTermStatusCheck", "type": "warning",
                   "message": "Applications unavailable", "wikiUrl": ""}],
        )

        result = app_module.fetch_prowlarr_health(FAKE_PROWLARR)

        assert result["error"] is None
        assert result["health_items"][0]["type"] == "warning"

    def test_connection_error_returns_error_not_exception(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://prowlarr.test:9696/api/v1/health", exc=requests.exceptions.ConnectionError)

        result = app_module.fetch_prowlarr_health(FAKE_PROWLARR)

        assert result["error"] is not None
        assert result["health_items"] == []


class TestFetchSabnzbdQueueSummary:
    def test_extracts_queue_wide_fields(self, app_module, requests_mock):
        requests_mock.get(
            "http://sabnzbd.test:8080/api",
            json={"queue": {
                "paused": True,
                "speed": "1.2 M",
                "timeleft": "0:12:34",
                "have_warnings": "2",
                "diskspace1": "50.00",
                "diskspacetotal1": "500.00",
                "diskspace2": "80.00",
                "diskspacetotal2": "800.00",
                "slots": [],
            }},
        )

        result = app_module.fetch_sabnzbd_queue(FAKE_SABNZBD)

        summary = result["summary"]
        assert summary["paused"] is True
        assert summary["speed"] == "1.2 M"
        assert summary["have_warnings"] == 2
        assert summary["diskspace_temp_free"] == "50.00"
        assert summary["diskspace_complete_free"] == "80.00"

    def test_empty_queue_still_returns_summary(self, app_module, requests_mock):
        # The whole point of this enhancement: a paused/full-disk state
        # must be visible even when there's nothing actively queued.
        requests_mock.get(
            "http://sabnzbd.test:8080/api",
            json={"queue": {"paused": False, "slots": []}},
        )

        result = app_module.fetch_sabnzbd_queue(FAKE_SABNZBD)

        assert result["items"] == []
        assert result["summary"]["paused"] is False


class TestFetchQbittorrentEnhancements:
    def test_torrents_include_seeds_peers_tags(self, app_module, requests_mock):
        requests_mock.get(
            "http://qbt.test:8080/api/v2/torrents/info",
            json=[{
                "hash": "abc", "name": "Some.Movie", "state": "downloading",
                "progress": 0.5, "num_seeds": 12, "num_leechs": 3, "tags": "movies, 1080p",
            }],
        )

        result = app_module.fetch_qbittorrent_torrents(FAKE_QBT)

        t = result["torrents"][0]
        assert t["num_seeds"] == 12
        assert t["num_leechs"] == 3
        assert t["tags"] == "movies, 1080p"

    def test_transfer_info_connection_status(self, app_module, requests_mock):
        requests_mock.get(
            "http://qbt.test:8080/api/v2/transfer/info",
            json={"connection_status": "firewalled", "dl_info_speed": 1000, "up_info_speed": 500},
        )

        result = app_module.fetch_qbittorrent_transfer_info(FAKE_QBT)

        assert result["connection_status"] == "firewalled"
        assert result["dl_speed"] == 1000

    def test_transfer_info_connection_error_returns_error_not_exception(self, app_module, requests_mock):
        import requests
        requests_mock.get("http://qbt.test:8080/api/v2/transfer/info", exc=requests.exceptions.ConnectionError)

        result = app_module.fetch_qbittorrent_transfer_info(FAKE_QBT)

        assert result["error"] is not None
        assert result["connection_status"] == ""


class TestPlexTranscodeStatus:
    def test_direct_play_when_no_transcode_session(self, app_module, requests_mock):
        requests_mock.get(
            "http://plex.test:32400/status/sessions",
            json={"MediaContainer": {"Metadata": [{
                "title": "Movie", "duration": 100, "viewOffset": 10,
                "Media": [{"bitrate": 8000, "videoResolution": "1080"}],
            }]}},
        )

        result = app_module.fetch_plex_now_playing(FAKE_PLEX)

        item = result["items"][0]
        assert item["play_method"] == "Direct Play"
        assert item["bitrate_kbps"] == 8000
        assert item["video_resolution"] == "1080"

    def test_transcoding_when_transcode_session_present(self, app_module, requests_mock):
        requests_mock.get(
            "http://plex.test:32400/status/sessions",
            json={"MediaContainer": {"Metadata": [{
                "title": "Movie", "duration": 100, "viewOffset": 10,
                "Media": [{"bitrate": 8000, "videoResolution": "1080"}],
                "TranscodeSession": {"videoDecision": "transcode", "bitrate": 2000},
            }]}},
        )

        result = app_module.fetch_plex_now_playing(FAKE_PLEX)

        assert result["items"][0]["play_method"] == "Transcoding"
        assert result["items"][0]["bitrate_kbps"] == 2000

    def test_direct_stream_when_video_decision_is_copy(self, app_module, requests_mock):
        requests_mock.get(
            "http://plex.test:32400/status/sessions",
            json={"MediaContainer": {"Metadata": [{
                "title": "Movie", "duration": 100, "viewOffset": 10,
                "TranscodeSession": {"videoDecision": "copy", "bitrate": 8000},
            }]}},
        )

        result = app_module.fetch_plex_now_playing(FAKE_PLEX)

        assert result["items"][0]["play_method"] == "Direct Stream"


class TestJellyfinTranscodeStatus:
    def test_direct_play_passthrough(self, app_module, requests_mock):
        requests_mock.get(
            "http://jellyfin.test:8096/Sessions",
            json=[{
                "NowPlayingItem": {"Name": "Show", "RunTimeTicks": 100},
                "PlayState": {"PositionTicks": 10, "PlayMethod": "DirectPlay"},
            }],
        )

        result = app_module.fetch_jellyfin_now_playing(FAKE_JELLYFIN)

        assert result["items"][0]["play_method"] == "DirectPlay"
        assert result["items"][0]["bitrate_kbps"] == 0

    def test_transcode_includes_bitrate_and_resolution(self, app_module, requests_mock):
        requests_mock.get(
            "http://jellyfin.test:8096/Sessions",
            json=[{
                "NowPlayingItem": {"Name": "Show", "RunTimeTicks": 100},
                "PlayState": {"PositionTicks": 10, "PlayMethod": "Transcode"},
                "TranscodingInfo": {"Bitrate": 4000000, "Width": 1920, "Height": 1080},
            }],
        )

        result = app_module.fetch_jellyfin_now_playing(FAKE_JELLYFIN)

        item = result["items"][0]
        assert item["play_method"] == "Transcoding"
        assert item["bitrate_kbps"] == 4000
        assert item["video_resolution"] == "1920x1080"


class TestTautulliTranscodeStatus:
    def test_translates_transcode_decision(self, app_module, requests_mock):
        requests_mock.get("http://tautulli.test:8181/api/v2", json={"response": {"data": {
            "sessions": [{
                "title": "Movie", "transcode_decision": "transcode",
                "bandwidth": "4000", "stream_video_resolution": "1080",
            }],
        }}})
        app_module.TAUTULLI["url"] = "http://tautulli.test:8181"
        app_module.TAUTULLI["api_key"] = "fake-key"

        result = app_module.fetch_tautulli_now_playing()

        item = result["items"][0]
        assert item["play_method"] == "Transcoding"
        assert item["bitrate_kbps"] == 4000
        assert item["video_resolution"] == "1080"

    def test_direct_play_decision(self, app_module, requests_mock):
        requests_mock.get("http://tautulli.test:8181/api/v2", json={"response": {"data": {
            "sessions": [{"title": "Movie", "transcode_decision": "direct play"}],
        }}})
        app_module.TAUTULLI["url"] = "http://tautulli.test:8181"
        app_module.TAUTULLI["api_key"] = "fake-key"

        result = app_module.fetch_tautulli_now_playing()

        assert result["items"][0]["play_method"] == "Direct Play"
