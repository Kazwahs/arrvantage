"""
Version number, the background monitor and the service history.

The history is built by a pure function (track_incidents) that is fed what
Needs attention found at each check, so almost everything here needs no
network and no thread. The monitor thread itself is never started by the tests
(conftest sets ARRVANTAGE_NO_MONITOR); monitor_once() is called directly.
"""
import json
import os
import re
import stat
import threading

import pytest

T0 = "2026-11-01T10:00:00Z"
T1 = "2026-11-01T10:01:00Z"
T2 = "2026-11-01T10:02:00Z"
T3 = "2026-11-01T10:03:00Z"
T4 = "2026-11-01T10:04:00Z"


def item(key="unreachable:Radarr", title="Radarr can't be reached", kind="unreachable", severity="critical",
         services=("Radarr",), hide_key=None, detail="Connection refused"):
    return {"id": key, "title": title, "detail": detail, "severity": severity, "kind": kind,
            "services": list(services), "hide_key": hide_key}


def check(*items, complete=True):
    return {"items": list(items), "complete": complete}


def run(app_module, state, checks):
    """Feeds a list of (time, data) through track_incidents; returns (state, all events)."""
    events = []
    for when, data in checks:
        state, new = app_module.track_incidents(state, data, when)
        events.extend(new)
    return state, events


class TestVersion:
    def test_the_version_is_a_plain_release_number(self, app_module):
        assert re.fullmatch(r"\d+\.\d+\.\d+", app_module.APP_VERSION)

    def test_the_environment_can_name_the_version(self, app_module):
        assert app_module.resolve_app_version("v2.0.1") == "2.0.1"
        assert app_module.resolve_app_version("2.0.1") == "2.0.1"
        assert app_module.resolve_app_version("2.0.1-rc.1") == "2.0.1-rc.1"

    def test_anything_else_is_ignored(self, app_module):
        for odd in ("", "latest", "sha-abc1234", "1.2", '1.2.3"><script>', "x" * 200):
            assert app_module.resolve_app_version(odd) == app_module.APP_VERSION

    def test_the_main_page_carries_the_version_for_the_about_panel(self, app_module, admin_session):
        client, _csrf = admin_session
        page = client.get("/").data.decode()
        assert app_module.APP_VERSION in page

    def test_diagnostics_names_the_version(self, app_module, admin_session):
        client, _csrf = admin_session
        text = client.get("/api/diagnostics").get_json()["text"]
        assert f"Version: {app_module.APP_VERSION} (build " in text


class TestMonitorInterval:
    def test_default_when_unset_or_nonsense(self, app_module):
        for raw in (None, "", "soon", "1.5"):
            assert app_module.resolve_monitor_interval(raw) == app_module.MONITOR_DEFAULT_INTERVAL
        assert app_module.resolve_monitor_interval("-5") == app_module.MONITOR_DEFAULT_INTERVAL

    def test_zero_turns_it_off(self, app_module):
        assert app_module.resolve_monitor_interval("0") == 0

    def test_it_is_kept_in_a_sensible_range(self, app_module):
        assert app_module.resolve_monitor_interval("5") == app_module.MONITOR_MIN_INTERVAL
        assert app_module.resolve_monitor_interval("120") == 120
        assert app_module.resolve_monitor_interval("999999") == app_module.MONITOR_MAX_INTERVAL

    def test_off_means_no_thread(self, app_module, monkeypatch):
        monkeypatch.setenv("MONITOR_INTERVAL", "0")
        assert app_module.start_monitor() is False
        assert app_module.monitor_status()["enabled"] is False

    def test_it_starts_once(self, app_module, monkeypatch):
        gate = threading.Event()
        monkeypatch.setattr(app_module, "_monitor_loop", lambda interval: gate.wait(5))
        monkeypatch.setenv("MONITOR_INTERVAL", "60")
        try:
            assert app_module.start_monitor() is True
            assert app_module.start_monitor() is False
            assert app_module.monitor_status() ["interval"] == 60
        finally:
            gate.set()


class TestTrackingProblems:
    def test_one_sighting_is_not_enough(self, app_module):
        state, events = run(app_module, app_module.empty_history(), [(T0, check(item()))])
        assert events == []
        assert app_module.empty_history()["open"] == {}   # the argument is never changed in place
        assert state["history"] == []

    def test_two_in_a_row_open_the_problem(self, app_module):
        state, events = run(app_module, app_module.empty_history(), [(T0, check(item())), (T1, check(item()))])
        assert [e["type"] for e in events] == ["opened"]
        assert events[0]["incident"]["title"] == "Radarr can't be reached"
        assert events[0]["incident"]["since"] == T0           # it began at the first sighting
        assert state["history"] == [{
            "key": "unreachable:Radarr", "title": "Radarr can't be reached", "detail": "Connection refused",
            "severity": "critical", "kind": "unreachable", "services": ["Radarr"], "start": T0, "end": None}]

    def test_it_is_announced_only_once(self, app_module):
        _state, events = run(app_module, app_module.empty_history(), [(t, check(item())) for t in (T0, T1, T2, T3)])
        assert [e["type"] for e in events] == ["opened"]

    def test_two_clear_checks_end_it(self, app_module):
        state, events = run(app_module, app_module.empty_history(),
                            [(T0, check(item())), (T1, check(item())), (T2, check()), (T3, check())])
        assert [e["type"] for e in events] == ["opened", "cleared"]
        assert events[1]["ended"] == T3
        assert state["history"][0]["end"] == T3
        assert state["open"] == {}

    def test_one_clear_check_does_not(self, app_module):
        state, events = run(app_module, app_module.empty_history(),
                            [(T0, check(item())), (T1, check(item())), (T2, check()), (T3, check(item()))])
        assert [e["type"] for e in events] == ["opened"]       # it flickered; nobody is told it recovered
        assert state["history"][0]["end"] is None

    def test_a_blip_that_never_counted_leaves_no_trace(self, app_module):
        state, events = run(app_module, app_module.empty_history(), [(T0, check(item())), (T1, check())])
        assert events == [] and state == app_module.empty_history()

    def test_a_warning_is_announced_but_is_not_history(self, app_module):
        stalled = item("queue:Radarr", "2 downloads stalled in Radarr", kind="queue", severity="warning", hide_key="queue:Radarr")
        state, events = run(app_module, app_module.empty_history(), [(T0, check(stalled)), (T1, check(stalled))])
        assert [e["type"] for e in events] == ["opened"]
        assert events[0]["incident"]["outage"] is False
        assert state["history"] == []

    def test_a_service_warning_counts_as_an_outage(self, app_module):
        # Tdarr being down is only a warning, but it is still a service that went down.
        tdarr = item("service:transcoders:Tdarr", "Tdarr can't be reached", kind="service", severity="warning",
                     hide_key="service:transcoders:Tdarr")
        state, _events = run(app_module, app_module.empty_history(), [(T0, check(tdarr)), (T1, check(tdarr))])
        assert [h["title"] for h in state["history"]] == ["Tdarr can't be reached"]

    def test_a_warning_that_turns_critical_joins_the_history(self, app_module):
        warn = item("disk:/data", "Low disk space: /data", kind="disk", severity="warning", hide_key="disk:/data")
        crit = item("disk:/data:1:2:3", "Critically low disk space: /data", kind="disk", severity="critical", hide_key="disk:/data")
        state, _ = run(app_module, app_module.empty_history(),
                       [(T0, check(warn)), (T1, check(warn)), (T2, check(crit))])
        assert [(h["severity"], h["end"]) for h in state["history"]] == [("critical", None)]
        assert state["history"][0]["start"] == T0   # the disk was already a problem; it just got worse

    def test_a_changing_reading_is_the_same_problem(self, app_module):
        a = item("disk:/data:1TB:50GB:5", "Critically low disk space: /data", kind="disk", severity="critical",
                 hide_key="disk:/data", detail="50 GB free")
        b = dict(a, id="disk:/data:1TB:48GB:4", detail="48 GB free")
        state, events = run(app_module, app_module.empty_history(), [(T0, check(a)), (T1, check(b)), (T2, check(a))])
        assert [e["type"] for e in events] == ["opened"]
        assert len(state["open"]) == 1

    def test_an_incomplete_check_cannot_clear_a_warning(self, app_module):
        # The health call may be the very thing that failed, so a missing warning proves nothing.
        health = item("health:Indexers", "Indexers unavailable", kind="health", severity="warning", hide_key="health:Indexers")
        state, events = run(app_module, app_module.empty_history(), [
            (T0, check(health)), (T1, check(health)),
            (T2, check(complete=False)), (T3, check(complete=False)), (T4, check(complete=False))])
        assert [e["type"] for e in events] == ["opened"]
        assert "health:Indexers" in state["open"]

    def test_an_incomplete_check_can_still_clear_an_outage(self, app_module):
        # A service that answers is gone from the list because it answered, whatever else failed.
        state, events = run(app_module, app_module.empty_history(), [
            (T0, check(item())), (T1, check(item())),
            (T2, check(complete=False)), (T3, check(complete=False))])
        assert [e["type"] for e in events] == ["opened", "cleared"]

    def test_two_problems_are_tracked_separately(self, app_module):
        a, b = item(), item("service:downloaders:qBit", "qBit can't be reached", kind="service", services=("qBit",))
        state, events = run(app_module, app_module.empty_history(),
                            [(T0, check(a, b)), (T1, check(a, b)), (T2, check(b)), (T3, check(b))])
        assert sorted(e["incident"]["title"] for e in events if e["type"] == "cleared") == ["Radarr can't be reached"]
        assert list(state["open"]) == ["service:downloaders:qBit"]


class TestHistoryUpkeep:
    def entry(self, key, start, end):
        return {"key": key, "title": key, "detail": "", "severity": "critical", "kind": "unreachable",
                "services": [], "start": start, "end": end}

    def test_old_closed_outages_are_dropped(self, app_module):
        history = [self.entry("old", "2026-06-01T00:00:00Z", "2026-06-01T01:00:00Z"),
                   self.entry("recent", "2026-10-30T00:00:00Z", "2026-10-30T01:00:00Z")]
        kept = app_module.prune_history(history, "2026-11-01T00:00:00Z")
        assert [e["key"] for e in kept] == ["recent"]

    def test_an_outage_still_going_is_never_dropped(self, app_module):
        history = [self.entry("long", "2026-01-01T00:00:00Z", None)]
        assert app_module.prune_history(history, "2026-11-01T00:00:00Z") == history

    def test_the_list_has_a_ceiling(self, app_module):
        history = [self.entry(f"k{i}", "2026-10-30T00:00:00Z", "2026-10-30T01:00:00Z") for i in range(600)]
        kept = app_module.prune_history(history, "2026-11-01T00:00:00Z")
        assert len(kept) == app_module.HISTORY_MAX_ENTRIES and kept[-1]["key"] == "k599"

    def test_durations_read_in_plain_words(self, app_module):
        assert app_module.human_duration(40) == "40 s"
        assert app_module.human_duration(240) == "4 min"
        assert app_module.human_duration(2 * 3600 + 300) == "2 h 5 min"
        assert app_module.human_duration(3600) == "1 h"
        assert app_module.human_duration(3 * 86400 + 2 * 3600) == "3 d 2 h"

    def test_a_damaged_file_is_not_trusted(self, app_module):
        for raw in (None, [], "text", {"open": [], "history": "x"}, {"open": {"k": 5}, "history": [7, {"key": 1}]}):
            assert app_module._clean_history_state(raw) == app_module.empty_history()

    def test_an_outage_nothing_remembers_ending_is_closed(self, app_module):
        raw = {"open": {}, "history": [{"key": "gone", "title": "t", "start": T0, "end": None}]}
        assert app_module._clean_history_state(raw)["history"][0]["end"] == T0


class TestStoringAndRestarting:
    def monitor(self, app_module, monkeypatch, *datas):
        queue = list(datas)
        monkeypatch.setattr(app_module, "gather_attention", lambda force=False: queue.pop(0))
        return [app_module.monitor_once(deliver=False) for _ in datas]

    def test_a_check_writes_the_history_privately(self, app_module, monkeypatch):
        self.monitor(app_module, monkeypatch, check(item()), check(item()))
        path = app_module.HISTORY_PATH
        saved = json.load(open(path))
        assert saved["history"][0]["title"] == "Radarr can't be reached"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert not os.path.exists(path + ".tmp")

    def test_a_quiet_check_does_not_rewrite_the_file(self, app_module, monkeypatch):
        self.monitor(app_module, monkeypatch, check(item()), check(item()), check(item()))
        first = os.stat(app_module.HISTORY_PATH).st_mtime_ns
        self.monitor(app_module, monkeypatch, check(item()), check(item()))
        assert os.stat(app_module.HISTORY_PATH).st_mtime_ns == first

    def test_a_restart_remembers_an_open_outage_and_does_not_announce_it_again(self, app_module, monkeypatch):
        before = self.monitor(app_module, monkeypatch, check(item()), check(item()))
        assert [e["type"] for e in before[1]] == ["opened"]
        app_module._HISTORY["state"] = None          # what a restart does: memory is gone, the file is not
        after = self.monitor(app_module, monkeypatch, check(item()), check(item()))
        assert after == [[], []]
        app_module._HISTORY["state"] = None
        ended = self.monitor(app_module, monkeypatch, check(), check())
        assert [e["type"] for e in ended[1]] == ["cleared"]
        assert app_module.load_history()["history"][0]["end"] is not None

    def test_a_corrupt_file_starts_the_history_again_and_says_so(self, app_module, caplog):
        with open(app_module.HISTORY_PATH, "w") as handle:
            handle.write("{not json")
        with caplog.at_level("WARNING", logger="arrvantage"):
            assert app_module.load_history() == app_module.empty_history()
        assert any("history.json could not be read" in r.getMessage() for r in caplog.records)

    def test_the_checks_are_counted_for_the_status(self, app_module, monkeypatch):
        self.monitor(app_module, monkeypatch, check(), check())
        status = app_module.monitor_status()
        assert status["checks"] == 2 and status["last_check"]


class TestHistoryApi:
    def test_it_needs_a_login(self, app_module, client):
        assert client.get("/api/history").status_code in (302, 401)

    def test_it_lists_newest_first_and_marks_what_is_still_going(self, app_module, admin_session, monkeypatch):
        client, _csrf = admin_session
        radarr, sonarr = item(), item("unreachable:Sonarr", "Sonarr can't be reached", services=("Sonarr",))
        queue = [check(radarr), check(radarr), check(radarr), check(radarr), check(radarr, sonarr), check(radarr, sonarr)]
        monkeypatch.setattr(app_module, "gather_attention", lambda force=False: queue.pop(0))
        for _ in range(6):
            app_module.monitor_once(deliver=False)
        body = client.get("/api/history").get_json()
        assert body["ok"] and body["monitor"]["checks"] == 6
        assert [h["title"] for h in body["history"]] == ["Sonarr can't be reached", "Radarr can't be reached"]
        assert all(h["end"] is None for h in body["history"])

    def test_a_regular_account_may_read_it(self, app_module, admin_session):
        # It lists the same things Needs attention already shows every account.
        from werkzeug.security import generate_password_hash
        client, _csrf = admin_session
        app_module.USERS.append({"username": "viewer", "password_hash": generate_password_hash("viewerpass123"),
                                 "role": "user", "permissions": {"requesting": False, "searching": False}})
        other = app_module.app.test_client()
        page = other.get("/login").data.decode()
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        other.post("/login", data={"username": "viewer", "password": "viewerpass123", "csrf_token": token})
        assert other.get("/api/history").status_code == 200
