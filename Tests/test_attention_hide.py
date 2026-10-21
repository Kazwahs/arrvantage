"""
Hiding warnings on Home: which items can be hidden, what "hidden until it
clears" means, and the per-user storage behind it.
"""
import json

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731


def ok_signals(**overrides):
    base = {"queue": {"queue_items": []}, "health": {"health_items": []}, "diskspace": {"diskspace_items": []}}
    base.update(overrides)
    return base


def sample_summary(app_module, healthy=True):
    stalled = {"queue_items": [{"title": "Show.S01E01", "status": "warning", "status_messages": ["No files found"]}]}
    health = {"health_items": [{"type": "warning", "source": "IndexerStatusCheck", "message": "Indexers unavailable"}]}
    low = {"diskspace_items": [{"path": "/tv", "total_human": "10 TB", "free_human": "900 GB", "percent_free": 9}]}
    critical_disk = {"diskspace_items": [{"path": "/movies", "total_human": "10 TB", "free_human": "100 GB", "percent_free": 1}]}
    signals = {
        "Sonarr": ok_signals(queue=stalled, health=health, diskspace=low),
        "Radarr": ok_signals(health=health, diskspace=critical_disk),
    }
    return app_module.summarize_attention(signals)


class TestHideKeys:
    def test_warnings_are_hideable_and_criticals_are_not(self, app_module):
        by_id = {i["id"]: i for i in sample_summary(app_module)["items"]}
        assert by_id["queue:Sonarr"]["hideable"] and by_id["queue:Sonarr"]["hide_key"] == "queue:Sonarr"
        health = next(i for i in by_id.values() if i["kind"] == "health")
        assert health["hideable"] and health["hide_key"] == "health:IndexerStatusCheck"
        disks = [i for i in by_id.values() if i["kind"] == "disk"]
        assert {d["severity"]: d["hideable"] for d in disks} == {"warning": True, "critical": False}

    def test_a_disk_warning_is_keyed_on_the_volume_not_the_reading(self, app_module):
        item = next(i for i in sample_summary(app_module)["items"] if i["kind"] == "disk" and i["severity"] == "warning")
        assert item["hide_key"] == "disk:/tv"

    def test_unreachable_is_never_hideable(self, app_module):
        err = {"error": "boom"}
        data = app_module.summarize_attention({"Sonarr": {"queue": err, "health": err, "diskspace": err}})
        item = data["items"][0]
        assert item["kind"] == "unreachable" and item["hideable"] is False

    def test_complete_is_false_when_any_check_failed(self, app_module):
        assert sample_summary(app_module)["complete"] is True
        data = app_module.summarize_attention({"Sonarr": ok_signals(health={"error": "timeout"})})
        assert data["complete"] is False


class TestApplyHidden:
    def test_hidden_warnings_move_to_their_own_list(self, app_module):
        data = sample_summary(app_module)
        result = app_module.apply_hidden_attention(data, ["health:IndexerStatusCheck"])
        assert all(i["hide_key"] != "health:IndexerStatusCheck" for i in result["items"])
        assert [i["hide_key"] for i in result["hidden_items"]] == ["health:IndexerStatusCheck"]
        assert result["summary"]["total"] == len(result["items"]) == data["summary"]["total"] - 1

    def test_a_critical_item_cannot_be_hidden_even_if_its_key_is_listed(self, app_module):
        data = sample_summary(app_module)
        critical = next(i for i in data["items"] if i["severity"] == "critical")
        result = app_module.apply_hidden_attention(data, [critical["hide_key"] or "disk:/movies", "disk:/movies"])
        assert any(i["severity"] == "critical" for i in result["items"])
        assert result["hidden_items"] == []

    def test_the_shared_cached_summary_is_not_modified(self, app_module):
        data = sample_summary(app_module)
        before = json.dumps(data, sort_keys=True)
        app_module.apply_hidden_attention(data, ["queue:Sonarr", "health:IndexerStatusCheck"])
        assert json.dumps(data, sort_keys=True) == before

    def test_hiding_a_stalled_download_warning_clears_that_apps_sidebar_dot(self, app_module):
        data = sample_summary(app_module)
        assert data["per_instance"]["Sonarr"]["dot"] == "warning"
        result = app_module.apply_hidden_attention(data, ["queue:Sonarr"])
        assert result["per_instance"]["Sonarr"]["dot"] is None
        assert data["per_instance"]["Sonarr"]["dot"] == "warning"

    def test_a_critical_dot_survives_hiding_that_apps_warnings(self, app_module):
        data = sample_summary(app_module)
        result = app_module.apply_hidden_attention(data, ["health:IndexerStatusCheck", "disk:/tv"])
        assert result["per_instance"]["Radarr"]["dot"] == data["per_instance"]["Radarr"]["dot"]


class TestHideEndpoint:
    def test_needs_csrf(self, admin_session):
        client, _ = admin_session
        assert client.post("/api/attention/hide", json={"key": "queue:Sonarr", "hidden": True}).status_code == 403

    def test_hide_and_unhide_are_saved_per_user(self, app_module, admin_session):
        client, csrf = admin_session
        r = client.post("/api/attention/hide", headers=HEADERS(csrf), json={"key": "queue:Sonarr", "hidden": True})
        assert r.get_json() == {"ok": True, "hidden": ["queue:Sonarr"]}
        assert json.load(open(app_module.CONFIG_PATH))["users"][0]["hidden_attention"] == ["queue:Sonarr"]
        client.post("/api/attention/hide", headers=HEADERS(csrf), json={"key": "queue:Sonarr", "hidden": True})
        assert app_module.CONFIG["users"][0]["hidden_attention"] == ["queue:Sonarr"]  # no duplicate
        client.post("/api/attention/hide", headers=HEADERS(csrf), json={"key": "queue:Sonarr", "hidden": False})
        assert "hidden_attention" not in json.load(open(app_module.CONFIG_PATH))["users"][0]

    def test_restore_all_clears_the_list(self, app_module, admin_session):
        client, csrf = admin_session
        for key in ("queue:A", "health:B", "disk:/c"):
            client.post("/api/attention/hide", headers=HEADERS(csrf), json={"key": key, "hidden": True})
        r = client.post("/api/attention/hide", headers=HEADERS(csrf), json={"restore_all": True})
        assert r.get_json()["hidden"] == []
        assert "hidden_attention" not in app_module.CONFIG["users"][0]

    def test_bad_requests_are_refused(self, admin_session):
        client, csrf = admin_session
        for bad in ({"key": "evil", "hidden": True}, {"key": "queue:A"}, {"key": 5, "hidden": True},
                    {"key": "queue:" + "x" * 400, "hidden": True}, ["queue:A"]):
            assert client.post("/api/attention/hide", headers=HEADERS(csrf), json=bad).status_code == 400

    def test_the_list_is_capped(self, app_module, admin_session):
        client, csrf = admin_session
        app_module.CONFIG["users"][0]["hidden_attention"] = [f"queue:{i}" for i in range(app_module.MAX_HIDDEN_ATTENTION)]
        r = client.post("/api/attention/hide", headers=HEADERS(csrf), json={"key": "queue:new", "hidden": True})
        assert r.status_code == 400


class TestAttentionApi:
    def _prime(self, app_module, data):
        app_module._ATTENTION_CACHE["data"] = data
        app_module._ATTENTION_CACHE["at"] = __import__("time").monotonic()

    def test_the_response_splits_visible_and_hidden(self, app_module, admin_session):
        client, _ = admin_session
        app_module.CONFIG["users"][0]["hidden_attention"] = ["health:IndexerStatusCheck"]
        self._prime(app_module, sample_summary(app_module))
        body = client.get("/api/attention").get_json()
        assert [i["hide_key"] for i in body["hidden_items"]] == ["health:IndexerStatusCheck"]
        assert all(i["hide_key"] != "health:IndexerStatusCheck" for i in body["items"])

    def test_a_hidden_warning_that_has_cleared_is_forgotten_after_a_full_check(self, app_module, admin_session):
        client, _ = admin_session
        app_module.CONFIG["users"][0]["hidden_attention"] = ["health:IndexerStatusCheck", "queue:Gone"]
        self._prime(app_module, sample_summary(app_module))
        client.get("/api/attention")
        assert app_module.CONFIG["users"][0]["hidden_attention"] == ["health:IndexerStatusCheck"]

    def test_an_incomplete_check_never_unhides_anything(self, app_module, admin_session):
        client, _ = admin_session
        app_module.CONFIG["users"][0]["hidden_attention"] = ["queue:Gone"]
        data = app_module.summarize_attention({"Sonarr": ok_signals(health={"error": "timeout"})})
        self._prime(app_module, data)
        client.get("/api/attention")
        assert app_module.CONFIG["users"][0]["hidden_attention"] == ["queue:Gone"]

    def test_each_user_sees_their_own_hidden_list(self, app_module, admin_session):
        client, _ = admin_session
        self._prime(app_module, sample_summary(app_module))
        body = client.get("/api/attention").get_json()
        assert body["hidden_items"] == [] and len(body["items"]) == 4
