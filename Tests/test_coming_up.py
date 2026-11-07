"""
Home > Coming Up: upcoming episodes, movie releases, albums and books from the
Sonarr / Radarr / Lidarr / Readarr-type instances, each item carrying its instance's colour.

Calendar endpoints (verified against each app's CalendarController, and for
Readarr its published API client):
GET /api/v3/calendar (Sonarr, Radarr) and /api/v1/calendar (Lidarr, Readarr) with
start, end, unmonitored=false, plus includeSeries / includeArtist / includeAuthor.
"""
from datetime import date, timedelta
import requests

TODAY = date(2026, 10, 24)


def instance(app_module, name, kind, color="#e8a33d", url=None, enabled=True):
    cfg = {
        "name": name, "kind": kind, "url": url or f"http://{name.lower()}.test:1234",
        "api_key": "k", "color": color, "enabled": enabled,
    }
    app_module.CONFIG.setdefault("instances", []).append(cfg)
    app_module.apply_config(app_module.CONFIG)
    return app_module.APPS[name]


def iso(offset, hour=None):
    day = (TODAY + timedelta(days=offset)).isoformat()
    return f"{day}T{hour:02d}:00:00Z" if hour is not None else f"{day}T00:00:00Z"


class TestSonarrEpisodes:
    def test_an_episode_keeps_its_timestamp_and_names_the_series(self, app_module):
        cfg = instance(app_module, "Sonarr", "series", "#4d8fdd")
        records = [{"airDateUtc": iso(1, 2), "seasonNumber": 2, "episodeNumber": 5, "title": "Low Tide",
                    "hasFile": False, "series": {"title": "Harbor Lights"}}]
        [item] = app_module.calendar_items("Sonarr", cfg, records, TODAY)
        assert item == {
            "instance": "Sonarr", "color": "#4d8fdd", "kind": "series", "title": "Harbor Lights",
            "detail": "S02E05 - Low Tide", "when": iso(1, 2), "all_day": False, "has_file": False,
        }

    def test_downloaded_episodes_are_marked(self, app_module):
        cfg = instance(app_module, "Sonarr", "series")
        [item] = app_module.calendar_items("Sonarr", cfg, [
            {"airDateUtc": iso(0, 3), "seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "series": {"title": "X"}}], TODAY)
        assert item["has_file"] is True

    def test_a_date_only_airdate_is_all_day(self, app_module):
        cfg = instance(app_module, "Sonarr", "series")
        [item] = app_module.calendar_items("Sonarr", cfg, [{"airDate": "2026-10-25", "series": {"title": "X"}}], TODAY)
        assert item["all_day"] is True

    def test_missing_pieces_do_not_break_it(self, app_module):
        cfg = instance(app_module, "Sonarr", "series")
        [item] = app_module.calendar_items("Sonarr", cfg, [{"airDateUtc": iso(2, 1)}], TODAY)
        assert item["title"] == "Unknown series" and item["detail"] == ""

    def test_series_title_can_come_from_a_flat_field(self, app_module):
        cfg = instance(app_module, "Sonarr", "series")
        [item] = app_module.calendar_items("Sonarr", cfg, [{"airDateUtc": iso(2, 1), "seriesTitle": "Flat"}], TODAY)
        assert item["title"] == "Flat"


class TestRadarrReleases:
    def test_each_release_date_in_range_is_its_own_item(self, app_module):
        cfg = instance(app_module, "Radarr", "movie")
        records = [{"title": "Cold Harbor", "inCinemas": iso(1), "digitalRelease": iso(20), "physicalRelease": iso(40),
                    "hasFile": False}]
        items = app_module.calendar_items("Radarr", cfg, records, TODAY)
        assert [(i["detail"], i["when"]) for i in items] == [
            ("In cinemas", (TODAY + timedelta(days=1)).isoformat()),
            ("Digital release", (TODAY + timedelta(days=20)).isoformat()),
        ]  # the physical release is beyond the 30-day ceiling
        assert all(i["all_day"] and i["title"] == "Cold Harbor" for i in items)

    def test_dates_far_in_the_past_are_ignored(self, app_module):
        cfg = instance(app_module, "Radarr", "movie")
        assert app_module.calendar_items("Radarr", cfg, [{"title": "Old", "inCinemas": iso(-300)}], TODAY) == []

    def test_a_movie_with_no_dates_gives_nothing(self, app_module):
        cfg = instance(app_module, "Radarr", "movie")
        assert app_module.calendar_items("Radarr", cfg, [{"title": "Undated"}], TODAY) == []


class TestLidarrAlbums:
    def test_an_album_names_its_artist(self, app_module):
        cfg = instance(app_module, "Lidarr", "artist", "#5fd97a")
        [item] = app_module.calendar_items("Lidarr", cfg, [
            {"title": "Static Bloom", "releaseDate": iso(2), "artist": {"artistName": "Hollow Pines"}}], TODAY)
        assert (item["title"], item["detail"], item["color"], item["all_day"]) == ("Static Bloom", "Hollow Pines", "#5fd97a", True)


class TestBookReleases:
    """Readarr / Chaptarr (author kind). Field names follow Readarr's BookResource;
    Chaptarr has not been checked against a live server."""

    def test_a_book_names_its_author_and_series(self, app_module):
        cfg = instance(app_module, "Chaptarr", "author", "#c58bff")
        [item] = app_module.calendar_items("Chaptarr", cfg, [
            {"title": "The Salt Road", "releaseDate": iso(3), "seriesTitle": "Tidewater",
             "author": {"authorName": "Mara Quill"}, "statistics": {"bookFileCount": 0}}], TODAY)
        assert item == {
            "instance": "Chaptarr", "color": "#c58bff", "kind": "author", "title": "The Salt Road",
            "detail": "Mara Quill - Tidewater", "when": (TODAY + timedelta(days=3)).isoformat(),
            "all_day": True, "has_file": False,
        }

    def test_an_owned_book_is_marked(self, app_module):
        cfg = instance(app_module, "Chaptarr", "author")
        [item] = app_module.calendar_items("Chaptarr", cfg, [
            {"title": "A", "releaseDate": iso(1), "statistics": {"bookFileCount": 1}}], TODAY)
        assert item["has_file"] is True

    def test_missing_pieces_do_not_break_it(self, app_module):
        cfg = instance(app_module, "Chaptarr", "author")
        [item] = app_module.calendar_items("Chaptarr", cfg, [
            {"releaseDate": iso(1), "author": None, "statistics": "odd", "seriesTitle": None}], TODAY)
        assert (item["title"], item["detail"], item["has_file"]) == ("Unknown book", "", False)

    def test_undated_and_old_books_give_nothing(self, app_module):
        cfg = instance(app_module, "Chaptarr", "author")
        assert app_module.calendar_items("Chaptarr", cfg, [
            {"title": "Undated"}, {"title": "Old", "releaseDate": iso(-400)}], TODAY) == []

    def test_request_uses_v1_and_asks_for_the_author(self, app_module, requests_mock):
        cfg = instance(app_module, "Chaptarr", "author")
        mock = requests_mock.get("http://chaptarr.test:1234/api/v1/calendar", json=[])
        assert app_module.fetch_calendar("Chaptarr", cfg, TODAY) == {"error": None, "items": []}
        query = mock.last_request.qs
        assert query["includeauthor"] == ["true"] and query["unmonitored"] == ["false"]
        assert mock.last_request.headers["X-Api-Key"] == "k"

    def test_book_managers_are_part_of_coming_up(self, app_module, monkeypatch):
        instance(app_module, "Chaptarr", "author")
        monkeypatch.setattr(app_module, "fetch_calendar", lambda n, c, today=None: {"error": None, "items": []})
        result = app_module.gather_coming_up(7)
        assert [i["name"] for i in result["instances"]] == ["Chaptarr"]


class TestRobustness:
    def test_garbage_records_are_skipped(self, app_module):
        cfg = instance(app_module, "Sonarr", "series")
        assert app_module.calendar_items("Sonarr", cfg, ["x", None, 3, {"airDateUtc": "not a date"}], TODAY) == []
        assert app_module.calendar_items("Sonarr", cfg, None, TODAY) == []

    def test_an_instance_without_a_colour_gets_the_default(self, app_module):
        cfg = instance(app_module, "Radarr", "movie", color="")
        [item] = app_module.calendar_items("Radarr", cfg, [{"title": "A", "inCinemas": iso(1)}], TODAY)
        assert item["color"] == "#5b8cff"


class TestFetchCalendar:
    def test_sonarr_request_shape(self, app_module, requests_mock):
        cfg = instance(app_module, "Sonarr", "series")
        mock = requests_mock.get("http://sonarr.test:1234/api/v3/calendar", json=[])
        result = app_module.fetch_calendar("Sonarr", cfg, TODAY)
        assert result == {"error": None, "items": []}
        query = mock.last_request.qs
        assert query["start"] == [(TODAY - timedelta(days=1)).isoformat()]
        assert query["end"] == [(TODAY + timedelta(days=31)).isoformat()]
        assert query["unmonitored"] == ["false"] and query["includeseries"] == ["true"]
        assert mock.last_request.headers["X-Api-Key"] == "k"

    def test_lidarr_uses_v1_and_asks_for_the_artist(self, app_module, requests_mock):
        cfg = instance(app_module, "Lidarr", "artist")
        mock = requests_mock.get("http://lidarr.test:1234/api/v1/calendar", json=[])
        app_module.fetch_calendar("Lidarr", cfg, TODAY)
        assert mock.last_request.qs["includeartist"] == ["true"]

    def test_radarr_asks_for_nothing_extra(self, app_module, requests_mock):
        cfg = instance(app_module, "Radarr", "movie")
        mock = requests_mock.get("http://radarr.test:1234/api/v3/calendar", json=[])
        app_module.fetch_calendar("Radarr", cfg, TODAY)
        query = mock.last_request.qs
        assert "includeseries" not in query and "includeartist" not in query

    def test_errors_become_short_messages(self, app_module, requests_mock):
        cfg = instance(app_module, "Sonarr", "series")
        requests_mock.get("http://sonarr.test:1234/api/v3/calendar", exc=requests.exceptions.ConnectionError("boom http://x/?apikey=SECRET"))
        assert app_module.fetch_calendar("Sonarr", cfg, TODAY) == {"error": "could not connect", "items": []}
        requests_mock.get("http://sonarr.test:1234/api/v3/calendar", status_code=401)
        assert "HTTP error 401" in app_module.fetch_calendar("Sonarr", cfg, TODAY)["error"]
        requests_mock.get("http://sonarr.test:1234/api/v3/calendar", text="<html>not json</html>")
        assert app_module.fetch_calendar("Sonarr", cfg, TODAY)["error"].startswith("request failed")

    def test_a_non_list_answer_is_treated_as_empty(self, app_module, requests_mock):
        cfg = instance(app_module, "Sonarr", "series")
        requests_mock.get("http://sonarr.test:1234/api/v3/calendar", json={"message": "hi"})
        assert app_module.fetch_calendar("Sonarr", cfg, TODAY) == {"error": None, "items": []}


class TestEndpoint:
    def _fake(self, monkeypatch, app_module, per_instance):
        calls = []

        def fake(name, cfg, today=None):
            calls.append(name)
            return per_instance[name]

        monkeypatch.setattr(app_module, "fetch_calendar", fake)
        return calls

    def item(self, instance, title, when, all_day=True, color="#111111"):
        return {"instance": instance, "color": color, "kind": "movie", "title": title, "detail": "", "when": when,
                "all_day": all_day, "has_file": False}

    def test_no_calendar_instances_means_nothing_to_show(self, app_module, admin_session):
        client, _ = admin_session
        instance(app_module, "Kapowarr", "comic")  # no calendar of its own
        body = client.get("/api/coming-up").get_json()
        assert body["instances"] == [] and body["items"] == [] and body["errors"] == []

    def test_items_from_every_instance_are_merged_and_ordered(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        instance(app_module, "Radarr", "movie", "#e8a33d")
        instance(app_module, "Sonarr", "series", "#4d8fdd")
        instance(app_module, "Disabled", "series", enabled=False)
        today = app_module._utcnow().date().isoformat()
        calls = self._fake(monkeypatch, app_module, {
            "Radarr": {"error": None, "items": [self.item("Radarr", "Zebra", today)]},
            "Sonarr": {"error": None, "items": [self.item("Sonarr", "Alpha", f"{today}T05:00:00Z", all_day=False),
                                                self.item("Sonarr", "Beta", today)]},
        })
        body = client.get("/api/coming-up?days=14").get_json()
        assert sorted(calls) == ["Radarr", "Sonarr"]  # the disabled one is never asked
        assert [i["title"] for i in body["items"]] == ["Beta", "Zebra", "Alpha"]  # all-day first, then by title, then timed
        assert [i["name"] for i in body["instances"]] == ["Radarr", "Sonarr"]
        assert body["days"] == 14 and body["errors"] == []

    def test_a_failing_instance_is_reported_and_the_rest_still_show(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        instance(app_module, "Radarr", "movie")
        instance(app_module, "Lidarr", "artist")
        today = app_module._utcnow().date().isoformat()
        self._fake(monkeypatch, app_module, {
            "Radarr": {"error": None, "items": [self.item("Radarr", "Zebra", today)]},
            "Lidarr": {"error": "could not connect", "items": []},
        })
        body = client.get("/api/coming-up").get_json()
        assert [i["title"] for i in body["items"]] == ["Zebra"]
        assert body["errors"] == [{"instance": "Lidarr", "error": "could not connect"}]

    def test_a_crashing_fetch_does_not_take_the_panel_down(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        instance(app_module, "Radarr", "movie")

        def boom(*_a, **_k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(app_module, "fetch_calendar", boom)
        body = client.get("/api/coming-up").get_json()
        assert body["errors"] == [{"instance": "Radarr", "error": "kaboom"}]

    def test_a_good_answer_is_cached_but_an_error_is_not(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        instance(app_module, "Radarr", "movie")
        answers = [{"error": "could not connect", "items": []}, {"error": None, "items": []}]
        calls = []

        def fake(name, cfg, today=None):
            calls.append(1)
            return answers[min(len(calls) - 1, 1)]

        monkeypatch.setattr(app_module, "fetch_calendar", fake)
        client.get("/api/coming-up")          # error: not cached
        client.get("/api/coming-up")          # good: cached
        client.get("/api/coming-up")          # served from the cache
        assert len(calls) == 2

    def test_changing_the_instances_refreshes_the_answer(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        instance(app_module, "Radarr", "movie")
        calls = self._fake(monkeypatch, app_module, {"Radarr": {"error": None, "items": []},
                                                      "Sonarr": {"error": None, "items": []}})
        client.get("/api/coming-up")
        instance(app_module, "Sonarr", "series")
        client.get("/api/coming-up")
        assert calls.count("Radarr") == 2 and calls.count("Sonarr") == 1

    def test_an_odd_days_value_falls_back_to_a_week(self, app_module, admin_session):
        client, _ = admin_session
        instance(app_module, "Radarr", "movie")
        for junk in ("abc", "3", "999", ""):
            assert client.get(f"/api/coming-up?days={junk}").get_json()["days"] == 7

    def test_requires_a_login_but_not_admin(self, app_module, client):
        assert client.get("/api/coming-up").status_code in (302, 401)

    def test_the_home_page_has_the_panel(self, app_module, admin_session):
        client, _ = admin_session
        html = client.get("/").data.decode()
        assert 'data-panel="comingup"' in html and "Coming Up" in html
