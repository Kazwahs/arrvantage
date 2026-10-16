"""
Tests for the Home "needs attention" summary, the concurrent fetching it
runs on, and the lazy-loaded instance panels.

The health messages below are copied from a real stack (Prowlarr indexers
failing, reported separately - and worded differently - by every arr app,
plus several apps reporting the same shared disk), because that exact
situation was what made every sidebar dot red.
"""
import threading
import time

import pytest


def _health(*items):
    return {"error": None, "health_items": [
        {"type": t, "source": src, "message": msg, "wiki_url": ""} for t, src, msg in items
    ]}


def _queue(*items):
    return {"error": None, "queue_items": list(items)}


def _disk(*items):
    return {"error": None, "diskspace_items": [
        {"path": p, "label": "", "free_human": f, "total_human": t, "percent_free": pct}
        for p, f, t, pct in items
    ]}


OK_QUEUE = _queue()
OK_HEALTH = _health()
OK_DISK = _disk(("/media", "12.1 TB", "17.5 TB", 69))
DOWN = {"error": "could not connect (http://x:7878)"}


def _signals(**by_name):
    return {
        name: {"queue": q, "health": h, "diskspace": d}
        for name, (q, h, d) in by_name.items()
    }


class TestSummarizeAttention:
    def test_all_clear(self, app_module):
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, OK_HEALTH, OK_DISK), Sonarr=(OK_QUEUE, OK_HEALTH, OK_DISK),
        ))
        assert result["items"] == []
        assert result["summary"] == {"critical": 0, "warning": 0, "total": 0}
        assert result["checked"] == 2
        assert all(s["dot"] is None for s in result["per_instance"].values())

    def test_same_indexer_warning_in_many_apps_becomes_one_item(self, app_module):
        src = "IndexerLongTermStatusCheck"
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, _health(("warning", src, "Indexers unavailable due to failures for more than 6 hours: Nyaa.si (Prowlarr), Torrent Downloads (Prowlarr)")), OK_DISK),
            Sonarr=(OK_QUEUE, _health(("warning", src, "Indexers unavailable due to failures for more than 6 hours: Torrent Downloads (Prowlarr), Internet Archive (Prowlarr)")), OK_DISK),
            Lidarr=(OK_QUEUE, _health(("warning", src, "Indexers unavailable due to failures for more than 6 hours: Internet Archive (Prowlarr)")), OK_DISK),
        ))
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["kind"] == "health"
        assert item["services"] == ["Radarr", "Sonarr", "Lidarr"]
        # names merged across the apps, each listed once
        assert item["title"].count("Nyaa.si (Prowlarr)") == 1
        assert item["title"].count("Torrent Downloads (Prowlarr)") == 1
        assert item["title"].count("Internet Archive (Prowlarr)") == 1

    def test_health_warnings_alone_do_not_light_the_sidebar_dot(self, app_module):
        # Five red dots for one shared cause carried no information, so a
        # plain health warning is listed on Home but doesn't get a dot.
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, _health(("warning", "Check", "Something minor")), OK_DISK),
        ))
        assert result["per_instance"]["Radarr"]["dot"] is None
        assert result["per_instance"]["Radarr"]["health_issues"] == 1
        assert result["summary"]["warning"] == 1

    def test_notices_are_ignored(self, app_module):
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, _health(("notice", "UpdateCheck", "New update is available")), OK_DISK),
        ))
        assert result["items"] == []

    def test_health_error_is_critical_and_dots_the_instance(self, app_module):
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, _health(("error", "DownloadClientCheck", "No download client is available")), OK_DISK),
        ))
        assert result["items"][0]["severity"] == "critical"
        assert result["per_instance"]["Radarr"]["dot"] == "critical"

    def test_shared_disk_reported_by_every_app_is_one_item(self, app_module):
        low = _disk(("/data", "1.0 TB", "14.5 TB", 7))
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, OK_HEALTH, low), Sonarr=(OK_QUEUE, OK_HEALTH, low), Lidarr=(OK_QUEUE, OK_HEALTH, low),
        ))
        disk_items = [i for i in result["items"] if i["kind"] == "disk"]
        assert len(disk_items) == 1
        assert disk_items[0]["services"] == ["Radarr", "Sonarr", "Lidarr"]
        assert "7%" in disk_items[0]["detail"]

    @pytest.mark.parametrize("pct, severity", [(14, "warning"), (7, "warning"), (6, "critical"), (0, "critical")])
    def test_disk_tiers(self, app_module, pct, severity):
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, OK_HEALTH, _disk(("/data", "1 TB", "10 TB", pct))),
        ))
        assert result["items"][0]["severity"] == severity
        assert ("Critically low" in result["items"][0]["title"]) == (severity == "critical")

    @pytest.mark.parametrize("pct", [15, 40, None])
    def test_healthy_or_unknown_disk_is_not_listed(self, app_module, pct):
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, OK_HEALTH, _disk(("/data", "5 TB", "10 TB", pct))),
        ))
        assert result["items"] == []

    def test_stalled_downloads_are_counted_per_instance(self, app_module):
        bad = _queue(
            {"title": "Ashfall.County.S02E04", "status": "warning", "status_messages": ["No files found are eligible for import"]},
            {"title": "Other.Show.S01E01", "status": "failed", "status_messages": []},
            {"title": "Fine.Movie.2026", "status": "downloading", "status_messages": []},
        )
        result = app_module.summarize_attention(_signals(Sonarr=(bad, OK_HEALTH, OK_DISK)))
        item = result["items"][0]
        assert item["kind"] == "queue"
        assert item["title"].startswith("2 downloads stalled or failing")
        assert "No files found" in item["detail"]
        assert result["per_instance"]["Sonarr"]["queue_count"] == 3
        assert result["per_instance"]["Sonarr"]["queue_issues"] == 2
        assert result["per_instance"]["Sonarr"]["dot"] == "warning"

    def test_unreachable_instance_is_one_critical_item_not_three(self, app_module):
        result = app_module.summarize_attention(_signals(
            Radarr=(DOWN, DOWN, DOWN), Sonarr=(OK_QUEUE, OK_HEALTH, OK_DISK),
        ))
        assert [i["kind"] for i in result["items"]] == ["unreachable"]
        assert result["items"][0]["severity"] == "critical"
        assert "could not connect" in result["items"][0]["detail"]
        assert result["per_instance"]["Radarr"]["unreachable"] is True
        assert result["per_instance"]["Radarr"]["dot"] == "critical"

    def test_one_failing_endpoint_does_not_mark_the_instance_unreachable(self, app_module):
        result = app_module.summarize_attention(_signals(Radarr=(OK_QUEUE, DOWN, OK_DISK)))
        assert result["per_instance"]["Radarr"]["unreachable"] is False
        assert result["items"] == []

    def test_items_are_ranked_critical_first_then_by_kind(self, app_module):
        result = app_module.summarize_attention(_signals(
            Radarr=(DOWN, DOWN, DOWN),
            Sonarr=(_queue({"title": "x", "status": "failed", "status_messages": []}),
                    _health(("warning", "Check", "Minor thing")),
                    _disk(("/data", "100 GB", "10 TB", 3))),
        ))
        order = [(i["severity"], i["kind"]) for i in result["items"]]
        assert order == [("critical", "unreachable"), ("critical", "disk"), ("warning", "queue"), ("warning", "health")]


    def test_within_a_kind_the_widest_impact_comes_first(self, app_module):
        shared = "Indexers unavailable due to failures: A (Prowlarr)"
        result = app_module.summarize_attention(_signals(
            Radarr=(OK_QUEUE, _health(("warning", "IndexerStatusCheck", shared)), OK_DISK),
            Sonarr=(OK_QUEUE, _health(("warning", "IndexerStatusCheck", shared)), OK_DISK),
            # alphabetically first title, but affects only one app
            Lidarr=(OK_QUEUE, _health(("warning", "AllowedHostsCheck", "Allowed Hosts is not configured")), OK_DISK),
        ))
        assert [i["services"] for i in result["items"]] == [["Radarr", "Sonarr"], ["Lidarr"]]


class TestRunConcurrently:
    def test_runs_jobs_in_parallel(self, app_module):
        def slow():
            time.sleep(0.3)
            return "done"
        started = time.monotonic()
        results = app_module.run_concurrently({i: slow for i in range(8)})
        elapsed = time.monotonic() - started
        assert all(ok and value == "done" for ok, value in results.values())
        assert elapsed < 1.2, f"8 x 0.3s calls took {elapsed:.2f}s - they are not running at once"

    def test_a_failing_job_does_not_sink_the_others(self, app_module):
        def boom():
            raise RuntimeError("kaput")
        results = app_module.run_concurrently({"bad": boom, "good": lambda: 42})
        assert results["good"] == (True, 42)
        assert results["bad"] == (False, "kaput")

    def test_a_hung_job_is_reported_as_timed_out(self, app_module):
        gate = threading.Event()
        results = app_module.run_concurrently({"hung": lambda: gate.wait(5), "fast": lambda: 1}, timeout=0.3)
        gate.set()
        assert results["fast"] == (True, 1)
        assert results["hung"] == (False, "timed out")


FAKE_INSTANCE = {
    "url": "http://radarr.test:7878", "api_key": "k", "library_kind": "movie",
    "enabled": True, "color": "#e8a33d", "seerr_media_type": None,
}


def _install(app_module, monkeypatch, names=("Radarr", "Sonarr", "Lidarr"), delay=0.0,
             health=None, disk=None, queue=None):
    for n in names:
        app_module.APPS[n] = dict(FAKE_INSTANCE)

    def slow(value):
        def fn(name, config):
            time.sleep(delay)
            return value
        return fn

    monkeypatch.setattr(app_module, "fetch_queue", slow(queue or OK_QUEUE))
    monkeypatch.setattr(app_module, "fetch_health", slow(health or OK_HEALTH))
    monkeypatch.setattr(app_module, "fetch_diskspace", slow(disk or OK_DISK))
    # the cache is module state shared across tests in one process
    app_module._ATTENTION_CACHE.update({"at": 0.0, "data": None})


class TestAttentionEndpoint:
    def test_requires_login(self, client):
        assert client.get("/api/attention").status_code == 302

    def test_returns_grouped_summary(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        _install(app_module, monkeypatch, health=_health(("warning", "IndexerStatusCheck", "Indexers unavailable due to failures: A (Prowlarr)")))
        data = client.get("/api/attention").get_json()
        assert data["checked"] == 3
        assert len(data["items"]) == 1
        assert data["items"][0]["services"] == ["Radarr", "Sonarr", "Lidarr"]

    def test_fetches_all_instances_at_once(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        _install(app_module, monkeypatch, names=[f"App{i}" for i in range(6)], delay=0.25)
        started = time.monotonic()
        data = client.get("/api/attention").get_json()
        elapsed = time.monotonic() - started
        assert data["checked"] == 6
        # 6 apps x 3 calls x 0.25s would be 4.5s one after another
        assert elapsed < 1.5, f"took {elapsed:.2f}s"

    def test_result_is_cached_briefly_and_refresh_bypasses_it(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        _install(app_module, monkeypatch)
        calls = []
        real = app_module.fetch_queue

        def counting(name, config):
            calls.append(name)
            return real(name, config)

        monkeypatch.setattr(app_module, "fetch_queue", counting)
        client.get("/api/attention")
        client.get("/api/attention")
        assert len(calls) == 3  # second request served from cache
        client.get("/api/attention?refresh=1")
        assert len(calls) == 6

    def test_disabled_instances_are_skipped(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        _install(app_module, monkeypatch)
        app_module.APPS["Sonarr"]["enabled"] = False
        assert client.get("/api/attention").get_json()["checked"] == 2


class TestLazyInstancePanels:
    def _stub_everything(self, app_module, monkeypatch, library_items=None):
        monkeypatch.setattr(app_module, "fetch_queue", lambda n, c: OK_QUEUE)
        monkeypatch.setattr(app_module, "fetch_history", lambda n, c: {"error": None, "history_items": []})
        monkeypatch.setattr(app_module, "fetch_library", lambda n, c: {
            "error": None, "library_items": library_items or [], "status_options": ["Downloaded"], "genre_options": ["Drama"],
        })
        monkeypatch.setattr(app_module, "fetch_health", lambda n, c: OK_HEALTH)
        monkeypatch.setattr(app_module, "fetch_diskspace", lambda n, c: OK_DISK)
        monkeypatch.setattr(app_module, "fetch_seerr_requests", lambda r, t: {"error": None, "request_items": []})
        monkeypatch.setattr(app_module, "fetch_collections", lambda c, o: [{"collection_id": 1, "title": "Saga", "cover_url": "", "owned_count": 1, "total_count": 2}])
        app_module.APPS["Radarr"] = dict(FAKE_INSTANCE)

    def test_main_page_makes_no_instance_calls(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        app_module.APPS["Radarr"] = dict(FAKE_INSTANCE)

        def forbidden(*a, **k):
            raise AssertionError("the main page must not fetch instance data")

        for fn in ("fetch_queue", "fetch_history", "fetch_library", "fetch_health",
                   "fetch_diskspace", "fetch_seerr_requests", "fetch_collections"):
            monkeypatch.setattr(app_module, fn, forbidden)

        response = client.get("/")
        assert response.status_code == 200
        html = response.data.decode()
        assert 'data-instance="Radarr"' in html          # sidebar tab is there
        assert 'data-instance-panels="Radarr"' in html    # empty host for the panels
        assert 'class="panel" data-instance=' not in html  # no baked-in panels

    def test_main_page_stays_small_with_a_huge_library(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        app_module.APPS["Radarr"] = dict(FAKE_INSTANCE)
        # would be ~MBs of markup if it were still baked into the page
        monkeypatch.setattr(app_module, "fetch_library", lambda n, c: pytest.fail("library fetched for main page"))
        assert len(client.get("/").data) < 400_000

    def test_fragment_contains_all_five_panels(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        self._stub_everything(app_module, monkeypatch, library_items=[{
            "item_id": 1, "title": "The Long Harbor", "status": "Downloaded", "genre": "Drama", "cover_url": "",
            "release_date": "2020-01-01", "date_added": "2026-01-01",
        }])
        response = client.get("/fragment/instance/Radarr")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        html = response.data.decode()
        for view in ("library", "request", "queue", "history", "system"):
            assert f'data-instance="Radarr" data-view="{view}"' in html
        assert "The Long Harbor" in html
        assert 'class="collections-data"' in html  # movie instances ship their collections

    def test_fragment_for_unknown_instance_is_404(self, admin_session):
        client, _ = admin_session
        assert client.get("/fragment/instance/Nope").status_code == 404

    def test_fragment_for_disabled_instance_is_404(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        self._stub_everything(app_module, monkeypatch)
        app_module.APPS["Radarr"]["enabled"] = False
        assert client.get("/fragment/instance/Radarr").status_code == 404

    def test_fragment_requires_login(self, client):
        assert client.get("/fragment/instance/Radarr").status_code == 302

    def test_one_failing_call_degrades_just_that_panel(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        self._stub_everything(app_module, monkeypatch)

        def boom(n, c):
            raise RuntimeError("history exploded")

        monkeypatch.setattr(app_module, "fetch_history", boom)
        response = client.get("/fragment/instance/Radarr")
        assert response.status_code == 200
        html = response.data.decode()
        assert "history exploded" in html          # that panel says what went wrong
        assert 'data-view="library"' in html       # the rest still rendered
        assert 'data-view="system"' in html

    def test_fragment_data_is_reused_by_home_stats(self, app_module, admin_session, monkeypatch):
        client, _ = admin_session
        self._stub_everything(app_module, monkeypatch)
        client.get("/fragment/instance/Radarr")

        calls = []
        monkeypatch.setattr(app_module, "fetch_library", lambda n, c: calls.append(n) or {
            "error": None, "library_items": [], "status_options": [], "genre_options": []})
        app_module.fetch_home_stats()
        assert calls == []  # served from the just-fetched library, not downloaded again

    def test_library_errors_are_not_cached(self, app_module, monkeypatch):
        app_module.APPS["Radarr"] = dict(FAKE_INSTANCE)
        results = iter([
            {"error": "could not connect", "library_items": [], "status_options": [], "genre_options": []},
            {"error": None, "library_items": [], "status_options": [], "genre_options": []},
        ])
        monkeypatch.setattr(app_module, "fetch_library", lambda n, c: next(results))
        assert app_module.fetch_library_cached("Radarr", app_module.APPS["Radarr"])["error"]
        assert app_module.fetch_library_cached("Radarr", app_module.APPS["Radarr"])["error"] is None


class TestHomeStatsConcurrency:
    def test_home_stats_fetches_libraries_at_once(self, app_module, monkeypatch):
        for i in range(6):
            app_module.APPS[f"App{i}"] = dict(FAKE_INSTANCE)

        def slow_library(name, config):
            time.sleep(0.25)
            return {"error": None, "library_items": [], "status_options": [], "genre_options": []}

        monkeypatch.setattr(app_module, "fetch_library", slow_library)
        started = time.monotonic()
        stats = app_module.fetch_home_stats()
        assert len(stats["instances"]) == 6
        assert time.monotonic() - started < 1.0
