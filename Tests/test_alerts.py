"""
Push alerts: who gets them, what they say, how they are sent, and that the
destinations (a topic, a token, a webhook address) are treated like API keys.

Delivery is tested against a real HTTP server on this machine rather than
requests_mock, because requests_mock replaces Session.send - the very place the
request logger (and so the "never log an address" rule) hooks in.
"""
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from werkzeug.security import generate_password_hash

SECRET_TOPIC = "arrv-secret-topic-91"
SECRET_TOKEN = "tk_supersecrettoken123"
SECRET_PATH = "api/webhook/SUPERSECRETHOOKID"
NTFY = {"server": "https://ntfy.example", "topic": SECRET_TOPIC, "token": SECRET_TOKEN}


def event(kind="opened", title="Radarr can't be reached", severity="critical", outage=True, key="unreachable:Radarr",
          services=("Radarr",), since="2026-11-01T10:00:00Z", ended="2026-11-01T10:07:00Z"):
    incident = {"key": key, "title": title, "detail": "Connection refused", "severity": severity, "kind": "unreachable",
                "services": list(services), "since": since, "outage": outage, "announced": True, "seen": 2, "missing": 0}
    out = {"type": kind, "incident": incident}
    if kind == "cleared":
        out["ended"] = ended
    return out


class TestNormalizing:
    def test_nothing_set_up_is_none(self, app_module):
        for raw in (None, {}, [], "x", {"level": "all"}, {"ntfy": {"topic": ""}}, {"webhook": {"webhook_url": ""}}):
            assert app_module.normalize_notify(raw) is None

    def test_a_topic_is_enough_and_the_server_defaults(self, app_module):
        assert app_module.normalize_notify({"ntfy": {"topic": "abc_DEF-1"}}) == {
            "level": "critical", "ntfy": {"server": "https://ntfy.sh", "topic": "abc_DEF-1"}}

    def test_a_bad_topic_is_dropped(self, app_module):
        for topic in ("has space", "slash/topic", "a" * 65, "emoji☃", 5, None):
            assert app_module.normalize_notify({"ntfy": {"topic": topic}}) is None

    def test_addresses_must_be_web_addresses(self, app_module):
        assert app_module.normalize_notify({"webhook": {"webhook_url": "ftp://x/y"}}) is None
        assert app_module.normalize_notify({"webhook": {"webhook_url": "javascript:alert(1)"}}) is None
        assert app_module.normalize_notify({"webhook": {"webhook_url": "http://"}}) is None
        bad_server = app_module.normalize_notify({"ntfy": {"topic": "t", "server": "file:///etc/passwd"}})
        assert bad_server["ntfy"]["server"] == "https://ntfy.sh"

    def test_an_unknown_level_means_critical(self, app_module):
        assert app_module.normalize_notify({"level": "shout", "webhook": {"webhook_url": "https://h/x"}})["level"] == "critical"
        assert app_module.normalize_notify({"level": "all", "webhook": {"webhook_url": "https://h/x"}})["level"] == "all"

    def test_the_view_for_the_browser_has_no_secrets(self, app_module):
        notify = app_module.normalize_notify({"ntfy": NTFY, "webhook": {"webhook_url": f"https://hooks.lan:8123/{SECRET_PATH}"}})
        text = json.dumps(app_module.notify_view(notify))
        for secret in (SECRET_TOPIC, SECRET_TOKEN, "SUPERSECRETHOOKID"):
            assert secret not in text
        view = app_module.notify_view(notify)
        assert view["ntfy"]["topic_set"] and view["ntfy"]["token_set"]
        assert view["webhook"] == {"set": True, "host": "hooks.lan:8123"}
        assert app_module.notify_view(None)["ntfy"]["topic_set"] is False


class TestMessages:
    def test_an_outage_message_says_what_and_why(self, app_module):
        (message,) = app_module.compose_messages([event()])
        assert message["event"] == "opened" and message["title"] == "Radarr can't be reached"
        assert message["message"] == "Connection refused" and message["severity"] == "critical"

    def test_a_recovery_says_how_long_it_lasted(self, app_module):
        (message,) = app_module.compose_messages([event("cleared")])
        assert message["title"] == "Resolved: Radarr can't be reached"
        assert message["message"] == "Back to normal after 7 min."

    def test_a_few_events_are_sent_separately(self, app_module):
        events = [event(key=f"k{i}", title=f"Service {i} can't be reached") for i in range(3)]
        assert len(app_module.compose_messages(events)) == 3

    def test_a_flood_becomes_one_summary(self, app_module):
        events = [event(key=f"k{i}", title=f"Service {i} can't be reached", services=(f"S{i}",)) for i in range(8)]
        (message,) = app_module.compose_messages(events)
        assert message["title"] == "8 new problems" and message["severity"] == "critical"
        assert "Service 0 can't be reached" in message["message"] and "and 2 more" in message["message"]
        assert message["services"] == [f"S{i}" for i in range(8)]

    def test_problems_and_recoveries_are_summarised_separately(self, app_module):
        events = [event(key=f"a{i}") for i in range(4)] + [event("cleared", key=f"b{i}") for i in range(4)]
        assert [m["title"] for m in app_module.compose_messages(events)] == ["4 new problems", "4 problems resolved"]


class TestWhoHearsWhat:
    def test_the_critical_level_hears_outages_only(self, app_module):
        outage = event()
        stalled = event(key="queue:Radarr", title="2 downloads stalled", severity="warning", outage=False)
        assert app_module.events_for_level([outage, stalled], "critical") == [outage]
        assert app_module.events_for_level([outage, stalled], "all") == [outage, stalled]

    def test_a_hidden_warning_stays_quiet(self, app_module):
        stalled = event(key="queue:Radarr", title="2 downloads stalled", severity="warning", outage=False)
        assert app_module.events_for_level([stalled], "all", hidden=["queue:Radarr"]) == []

    def test_a_critical_problem_cannot_be_hidden(self, app_module):
        outage = event(key="unreachable:Radarr")
        assert app_module.events_for_level([outage], "all", hidden=["unreachable:Radarr"]) == [outage]


@pytest.fixture
def receiver():
    """Records every POST it gets and answers with `status` (302 sends it elsewhere)."""
    class Box:
        status = 200
        requests = []

    box = Box()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            box.requests.append({"path": self.path, "headers": dict(self.headers), "json": json.loads(body or b"null")})
            self.send_response(box.status)
            if box.status == 302:
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    box.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    box.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield box
    server.shutdown()
    server.server_close()


def all_logged(caplog):
    return " ".join(r.getMessage() for r in caplog.records)


class TestSending:
    def test_ntfy_gets_one_json_post_to_the_server_root(self, app_module, receiver):
        (message,) = app_module.compose_messages([event()])
        ok, detail = app_module.send_ntfy({"server": receiver.url + "/", "topic": "mytopic", "token": SECRET_TOKEN}, message)
        assert ok and detail == "HTTP 200"
        (got,) = receiver.requests
        assert got["path"] == "/"
        assert got["headers"]["Authorization"] == f"Bearer {SECRET_TOKEN}"
        assert got["json"] == {"topic": "mytopic", "title": "Radarr can't be reached", "message": "Connection refused",
                               "priority": 4, "tags": ["rotating_light"]}

    def test_ntfy_without_a_token_sends_no_authorization(self, app_module, receiver):
        (message,) = app_module.compose_messages([event("cleared")])
        app_module.send_ntfy({"server": receiver.url, "topic": "t"}, message)
        got = receiver.requests[0]
        assert "Authorization" not in got["headers"]
        assert got["json"]["priority"] == 3 and got["json"]["tags"] == ["white_check_mark"]

    def test_a_warning_is_a_normal_priority(self, app_module, receiver):
        (message,) = app_module.compose_messages([event(severity="warning", outage=False)])
        app_module.send_ntfy({"server": receiver.url, "topic": "t"}, message)
        assert receiver.requests[0]["json"]["priority"] == 3 and receiver.requests[0]["json"]["tags"] == ["warning"]

    def test_a_title_with_any_characters_goes_through(self, app_module, receiver):
        # ntfy's plain-header form can't carry these; the JSON form can.
        (message,) = app_module.compose_messages([event(title="Plex – café ☃ can't be reached")])
        ok, _ = app_module.send_ntfy({"server": receiver.url, "topic": "t"}, message)
        assert ok and receiver.requests[0]["json"]["title"] == "Plex – café ☃ can't be reached"

    def test_the_webhook_gets_the_documented_fields(self, app_module, receiver):
        (message,) = app_module.compose_messages([event()])
        ok, _ = app_module.send_webhook({"webhook_url": f"{receiver.url}/{SECRET_PATH}"}, message)
        assert ok
        (got,) = receiver.requests
        assert got["path"] == "/" + SECRET_PATH
        assert got["json"] == {
            "app": "arrVantage", "version": app_module.APP_VERSION, "event": "opened",
            "title": "Radarr can't be reached", "message": "Connection refused", "severity": "critical",
            "kind": "unreachable", "services": ["Radarr"], "since": "2026-11-01T10:00:00Z", "ended": None}

    def test_failures_are_described_in_words(self, app_module, receiver):
        (message,) = app_module.compose_messages([event()])
        send = lambda: app_module.send_ntfy({"server": receiver.url, "topic": "t"}, message)  # noqa: E731
        receiver.status = 401
        assert send() == (False, "HTTP 401 - the server refused it (check the access token)")
        receiver.status = 500
        assert send() == (False, "HTTP 500")
        receiver.status = 302
        ok, detail = send()
        assert not ok and "redirected" in detail
        assert len(receiver.requests) == 3        # the redirect was not followed

    def test_a_server_that_is_not_there(self, app_module):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (message,) = app_module.compose_messages([event()])
        assert app_module.send_ntfy({"server": f"http://127.0.0.1:{port}", "topic": "t"}, message) == (False, "could not connect")

    def test_an_address_is_never_logged(self, app_module, receiver, caplog):
        (message,) = app_module.compose_messages([event()])
        receiver.status = 500
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            app_module.send_webhook({"webhook_url": f"{receiver.url}/{SECRET_PATH}?key=QUERYSECRET"}, message)
            app_module.send_message({"webhook": {"webhook_url": f"{receiver.url}/{SECRET_PATH}"}}, message)
        text = all_logged(caplog)
        assert "SUPERSECRETHOOKID" not in text and "QUERYSECRET" not in text
        assert "[path hidden]" in text and "Could not send an alert by webhook: HTTP 500" in text

    def test_an_unreachable_address_is_never_logged_either(self, app_module, caplog):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        (message,) = app_module.compose_messages([event()])
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            app_module.send_message({"webhook": {"webhook_url": f"http://127.0.0.1:{port}/{SECRET_PATH}"}}, message)
        assert "SUPERSECRETHOOKID" not in all_logged(caplog)

    def test_ordinary_requests_are_still_logged_with_their_path(self, app_module, receiver, caplog):
        # The path is only hidden for the length of an alert; the flag must not stick.
        import requests
        (message,) = app_module.compose_messages([event()])
        app_module.send_webhook({"webhook_url": f"{receiver.url}/x"}, message)
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            requests.post(f"{receiver.url}/visible/path", json={})
        assert "/visible/path" in all_logged(caplog)


def add_admin(app_module, name, notify=None, role="admin", hidden=None):
    user = {"username": name, "password_hash": generate_password_hash("adminpass12345"), "role": role,
            "permissions": {"requesting": True, "searching": True}}
    if notify:
        user["notify"] = notify
    if hidden:
        user["hidden_attention"] = hidden
    app_module.USERS.append(user)
    return user


class TestDelivery:
    def test_each_administrator_hears_on_their_own_level_at_their_own_address(self, app_module, receiver):
        add_admin(app_module, "quiet", {"level": "critical", "webhook": {"webhook_url": receiver.url + "/quiet"}})
        add_admin(app_module, "chatty", {"level": "all", "webhook": {"webhook_url": receiver.url + "/chatty"}})
        stalled = event(key="queue:Radarr", title="2 downloads stalled", severity="warning", outage=False)
        app_module.deliver_events([event(), stalled])
        seen = sorted((r["path"], r["json"]["title"]) for r in receiver.requests)
        assert seen == [("/chatty", "2 downloads stalled"), ("/chatty", "Radarr can't be reached"),
                        ("/quiet", "Radarr can't be reached")]

    def test_a_regular_account_never_triggers_a_request(self, app_module, receiver):
        # Only administrators may point the server at an address, so a stray
        # "notify" on any other account (a hand-edited file) is ignored.
        add_admin(app_module, "viewer", {"webhook": {"webhook_url": receiver.url + "/x"}}, role="user")
        app_module.deliver_events([event()])
        assert receiver.requests == []

    def test_an_account_without_alerts_costs_nothing(self, app_module, receiver):
        add_admin(app_module, "plain")
        app_module.deliver_events([event()])
        assert receiver.requests == []

    def test_a_broken_destination_does_not_stop_the_next(self, app_module, receiver):
        add_admin(app_module, "broken", {"webhook": {"webhook_url": "http://127.0.0.1:1/dead"}})
        add_admin(app_module, "fine", {"webhook": {"webhook_url": receiver.url + "/fine"}})
        app_module.deliver_events([event()])
        assert [r["path"] for r in receiver.requests] == ["/fine"]

    def test_a_check_sends_the_alert_when_the_problem_is_confirmed(self, app_module, receiver, monkeypatch):
        add_admin(app_module, "ops", {"webhook": {"webhook_url": receiver.url + "/ops"}})
        down = {"items": [{"id": "unreachable:Radarr", "title": "Radarr can't be reached", "detail": "refused",
                           "severity": "critical", "kind": "unreachable", "services": ["Radarr"], "hide_key": None}],
                "complete": True}
        queue = [down, down, {"items": [], "complete": True}, {"items": [], "complete": True}]
        monkeypatch.setattr(app_module, "gather_attention", lambda force=False: queue.pop(0))
        sent = []
        for _ in range(4):
            app_module.monitor_once()
            sent.append(len(receiver.requests))
        assert sent == [0, 1, 1, 2]
        assert [r["json"]["event"] for r in receiver.requests] == ["opened", "cleared"]


class TestAlertSettingsApi:
    def post(self, client, csrf, path, body):
        return client.post(path, json=body, headers={"X-CSRF-Token": csrf})

    def test_a_regular_account_cannot_use_it(self, app_module, admin_session):
        add_admin(app_module, "viewer", role="user")
        other = app_module.app.test_client()
        token = other.get("/login").data.decode().split('name="csrf_token" value="')[1].split('"')[0]
        other.post("/login", data={"username": "viewer", "password": "adminpass12345", "csrf_token": token})
        assert other.get("/api/notifications").status_code == 403
        assert other.post("/api/notifications", json={}, headers={"X-CSRF-Token": token}).status_code == 403
        assert other.post("/api/notifications/test", json={}, headers={"X-CSRF-Token": token}).status_code == 403

    def test_it_needs_the_csrf_token(self, app_module, admin_session):
        client, _csrf = admin_session
        assert client.post("/api/notifications", json={"level": "all"}).status_code == 403

    def test_saving_and_reading_back_never_returns_the_secrets(self, app_module, admin_session):
        client, csrf = admin_session
        reply = self.post(client, csrf, "/api/notifications", {
            "level": "all", "ntfy": {"server": "https://ntfy.example", "topic": SECRET_TOPIC, "token": SECRET_TOKEN},
            "webhook": {"webhook_url": f"https://hooks.lan/{SECRET_PATH}"}}).get_json()
        assert reply["ok"] and reply["notify"]["level"] == "all"
        again = client.get("/api/notifications")
        for secret in (SECRET_TOPIC, SECRET_TOKEN, "SUPERSECRETHOOKID"):
            assert secret not in again.get_data(as_text=True) and secret not in json.dumps(reply)
        saved = app_module.get_user("testadmin")["notify"]
        assert saved["ntfy"] == NTFY and saved["webhook"]["webhook_url"].endswith(SECRET_PATH)

    def test_a_blank_secret_keeps_what_is_saved(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"ntfy": NTFY, "webhook": {"webhook_url": "https://h/x"}})
        self.post(client, csrf, "/api/notifications", {
            "level": "all", "ntfy": {"server": "https://other.example", "topic": "", "token": ""}, "webhook": {"webhook_url": ""}})
        saved = app_module.get_user("testadmin")["notify"]
        assert saved["level"] == "all"
        assert saved["ntfy"] == {"server": "https://other.example", "topic": SECRET_TOPIC, "token": SECRET_TOKEN}
        assert saved["webhook"] == {"webhook_url": "https://h/x"}

    def test_a_new_value_replaces_the_old(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"ntfy": NTFY})
        self.post(client, csrf, "/api/notifications", {"ntfy": {"topic": "newtopic", "token": "newtoken"}})
        saved = app_module.get_user("testadmin")["notify"]["ntfy"]
        assert (saved["topic"], saved["token"], saved["server"]) == ("newtopic", "newtoken", "https://ntfy.example")

    def test_a_destination_can_be_removed(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"ntfy": NTFY, "webhook": {"webhook_url": "https://h/x"}})
        self.post(client, csrf, "/api/notifications", {"ntfy": {"remove": True}})
        assert "ntfy" not in app_module.get_user("testadmin")["notify"]
        self.post(client, csrf, "/api/notifications", {"webhook": {"remove": True}})
        assert "notify" not in app_module.get_user("testadmin")

    def test_the_token_alone_can_be_cleared(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"ntfy": NTFY})
        self.post(client, csrf, "/api/notifications", {"ntfy": {"clear_token": True}})
        assert "token" not in app_module.get_user("testadmin")["notify"]["ntfy"]

    @pytest.mark.parametrize("body, message", [
        ({"level": "shout"}, "Choose what to be alerted about"),
        ({"ntfy": {"topic": "has space"}}, "letters, numbers"),
        ({"ntfy": {"topic": "ok", "server": "ntfy.sh"}}, "must start with http"),
        ({"ntfy": {"token": "tk_x"}}, "Enter the ntfy topic"),
        ({"webhook": {"webhook_url": "ftp://example/x"}}, "must start with http"),
    ])
    def test_mistakes_are_explained_and_nothing_is_saved(self, app_module, admin_session, body, message):
        client, csrf = admin_session
        reply = self.post(client, csrf, "/api/notifications", body)
        assert reply.status_code == 400 and message in " ".join(reply.get_json()["errors"])
        assert "notify" not in app_module.get_user("testadmin")

    def test_it_is_saved_to_the_config_file(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"ntfy": NTFY})
        on_disk = json.load(open(app_module.CONFIG_PATH))
        assert on_disk["users"][0]["notify"]["ntfy"]["topic"] == SECRET_TOPIC

    def test_the_test_needs_something_saved(self, app_module, admin_session):
        client, csrf = admin_session
        reply = self.post(client, csrf, "/api/notifications/test", {})
        assert reply.status_code == 400 and "Save a destination" in reply.get_json()["error"]

    def test_the_test_alert_reaches_the_saved_address(self, app_module, admin_session, receiver):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"webhook": {"webhook_url": receiver.url + "/hook"}})
        reply = self.post(client, csrf, "/api/notifications/test", {}).get_json()
        assert reply["ok"] and reply["results"] == [{"channel": "webhook", "ok": True, "detail": "HTTP 200"}]
        assert receiver.requests[0]["json"]["event"] == "test"

    def test_a_failed_test_says_why_without_the_address(self, app_module, admin_session, receiver):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"webhook": {"webhook_url": receiver.url + "/" + SECRET_PATH}})
        receiver.status = 404
        reply = self.post(client, csrf, "/api/notifications/test", {})
        assert reply.get_json() == {"ok": False, "results": [{"channel": "webhook", "ok": False, "detail": "HTTP 404"}]}
        assert "SUPERSECRETHOOKID" not in reply.get_data(as_text=True)


class TestSecretsElsewhere:
    @pytest.fixture
    def with_alerts(self, app_module, admin_session):
        client, csrf = admin_session
        client.post("/api/notifications", json={"level": "all", "ntfy": NTFY,
                                                "webhook": {"webhook_url": f"https://hooks.lan/{SECRET_PATH}"}},
                    headers={"X-CSRF-Token": csrf})
        return client, csrf

    def test_the_settings_page_is_not_handed_them(self, app_module, with_alerts):
        client, _csrf = with_alerts
        page = client.get("/settings").data.decode()
        for secret in (SECRET_TOPIC, SECRET_TOKEN, "SUPERSECRETHOOKID"):
            assert secret not in page

    def test_saving_settings_keeps_the_alerts(self, app_module, with_alerts):
        # Settings rebuilds every account from its form, which has no alert fields.
        client, csrf = with_alerts
        form = {"csrf_token": csrf, "user_username_a": "testadmin", "user_role_a": "admin"}
        assert client.post("/settings", data=form).status_code == 302
        assert app_module.get_user("testadmin")["notify"]["ntfy"]["topic"] == SECRET_TOPIC

    def test_an_export_without_keys_leaves_them_out(self, app_module, with_alerts):
        client, _csrf = with_alerts
        text = client.get("/api/config/export").get_data(as_text=True)
        for secret in (SECRET_TOPIC, SECRET_TOKEN, "SUPERSECRETHOOKID"):
            assert secret not in text

    def test_an_export_with_keys_is_a_full_backup(self, app_module, with_alerts):
        client, _csrf = with_alerts
        text = client.get("/api/config/export?secrets=1").get_data(as_text=True)
        for secret in (SECRET_TOPIC, SECRET_TOKEN, "SUPERSECRETHOOKID"):
            assert secret in text

    def test_importing_a_file_without_keys_keeps_the_alerts(self, app_module, with_alerts):
        client, csrf = with_alerts
        exported = json.loads(client.get("/api/config/export").get_data(as_text=True))
        reply = client.post("/api/config/import", json={"file": exported, "confirm": True}, headers={"X-CSRF-Token": csrf})
        assert reply.get_json()["ok"]
        assert app_module.get_user("testadmin")["notify"]["ntfy"]["topic"] == SECRET_TOPIC

    def test_a_full_backup_restores_the_alerts(self, app_module, with_alerts):
        client, csrf = with_alerts
        exported = json.loads(client.get("/api/config/export?secrets=1").get_data(as_text=True))
        client.post("/api/notifications", json={"ntfy": {"remove": True}, "webhook": {"remove": True}},
                    headers={"X-CSRF-Token": csrf})
        assert "notify" not in app_module.get_user("testadmin")
        client.post("/api/config/import", json={"file": exported, "confirm": True}, headers={"X-CSRF-Token": csrf})
        restored = app_module.get_user("testadmin")["notify"]
        assert restored["level"] == "all" and restored["ntfy"] == NTFY

    def test_a_file_cannot_give_a_regular_account_alerts(self, app_module, with_alerts):
        client, csrf = with_alerts
        exported = json.loads(client.get("/api/config/export?secrets=1").get_data(as_text=True))
        exported["config"]["users"].append({
            "username": "sneaky", "password_hash": generate_password_hash("sneakypass1234"), "role": "user",
            "permissions": {"requesting": False, "searching": False},
            "notify": {"webhook": {"webhook_url": "http://192.168.1.1/admin"}}})
        client.post("/api/config/import", json={"file": exported, "confirm": True}, headers={"X-CSRF-Token": csrf})
        assert "notify" not in app_module.get_user("sneaky")

    def test_diagnostics_has_counts_and_no_destinations(self, app_module, with_alerts):
        client, _csrf = with_alerts
        text = client.get("/api/diagnostics?hide_addresses=0").get_json()["text"]
        assert "Administrators with alerts: 1 (ntfy: 1, webhook: 1, everything level: 1)" in text
        assert "Background check: every 60 s" in text
        for secret in (SECRET_TOPIC, SECRET_TOKEN, "SUPERSECRETHOOKID", "hooks.lan", "ntfy.example"):
            assert secret not in text
