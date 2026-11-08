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
        reply = b"{}"
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
            self.send_header("Content-Length", str(len(box.reply)))
            self.end_headers()
            self.wfile.write(box.reply)

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
        assert "Administrators with alerts: 1 (ntfy: 1, discord: 0, telegram: 0, email: 0, webhook: 1, everything level: 1)" in text
        assert "Background check: every 60 s" in text
        for secret in (SECRET_TOPIC, SECRET_TOKEN, "SUPERSECRETHOOKID", "hooks.lan", "ntfy.example"):
            assert secret not in text


# --- Discord -------------------------------------------------------------------

DISCORD_ID = "1234567890123456789"
DISCORD_TOKEN = "AbCdEf-0123456789_SECRETDISCORDTOKEN-abcdefghijklmnopqrstuvwxyz0123"
DISCORD_URL = f"https://discord.com/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}"


class TestDiscordAddress:
    def test_real_looking_addresses_are_accepted_in_their_plain_form(self, app_module):
        clean = app_module._clean_discord_url
        assert clean(DISCORD_URL) == DISCORD_URL
        assert clean(f"  {DISCORD_URL}/  ") == DISCORD_URL                     # spaces and a trailing slash
        assert clean(f"https://DISCORD.com/api/v10/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}") == \
            f"https://discord.com/api/v10/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}"
        assert clean(f"https://ptb.discord.com/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}").startswith("https://ptb.discord.com/")
        assert clean(f"https://discordapp.com/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}")
        assert clean(DISCORD_URL + "?thread_id=987654321") == DISCORD_URL + "?thread_id=987654321"

    def test_anything_that_is_not_a_discord_webhook_is_refused(self, app_module):
        clean = app_module._clean_discord_url
        for bad in (
            None, "", 5, "discord.com/api/webhooks/1/2",
            f"http://discord.com/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}",                 # not https
            f"https://discord.com.evil.example/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}",    # another host
            f"https://evil.example/discord.com/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}",
            f"https://discord.com@evil.example/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}",   # user name trick
            f"https://user:pw@discord.com/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}",
            f"https://discord.com:8443/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}",           # another port
            "https://discord.com/api/webhooks/abc/" + DISCORD_TOKEN,                          # id not a number
            f"https://discord.com/api/webhooks/{DISCORD_ID}/short",                           # token too short
            f"https://discord.com/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}/extra",
            f"https://discord.com/channels/{DISCORD_ID}/{DISCORD_TOKEN}",
            DISCORD_URL + "?wait=true&x=1", DISCORD_URL + "#frag", DISCORD_URL + "?thread_id=abc",
        ):
            assert clean(bad) == "", bad

    def test_it_is_part_of_an_accounts_alerts(self, app_module):
        assert app_module.normalize_notify({"discord": {"webhook_url": DISCORD_URL}}) == \
            {"level": "critical", "discord": {"webhook_url": DISCORD_URL}}
        assert app_module.normalize_notify({"discord": {"webhook_url": "https://example.com/x"}}) is None
        notify = app_module.normalize_notify({"ntfy": NTFY, "discord": {"webhook_url": DISCORD_URL}})
        assert set(notify) == {"level", "ntfy", "discord"}

    def test_the_browser_is_only_told_whether_it_is_set(self, app_module):
        view = app_module.notify_view(app_module.normalize_notify({"discord": {"webhook_url": DISCORD_URL}}))
        assert view["discord"] == {"set": True, "host": "discord.com"}
        assert DISCORD_TOKEN not in json.dumps(view) and DISCORD_ID not in json.dumps(view)
        assert app_module.notify_view(None)["discord"] == {"set": False, "host": ""}


class TestDiscordSending:
    def send(self, app_module, receiver, ev=None, path="/hook"):
        (message,) = app_module.compose_messages([ev or event()])
        return app_module.send_discord({"webhook_url": receiver.url + path}, message)

    def test_a_critical_problem_is_a_red_embed_with_the_severity_in_words(self, app_module, receiver):
        ok, detail = self.send(app_module, receiver)
        assert ok and detail == "HTTP 200"
        (got,) = receiver.requests
        assert got["path"] == "/hook"
        body = got["json"]
        assert body["username"] == "arrVantage" and body["allowed_mentions"] == {"parse": []}
        (embed,) = body["embeds"]
        assert embed["title"] == "Radarr can't be reached" and embed["description"] == "Connection refused"
        assert embed["color"] == app_module.DISCORD_COLORS["critical"] and embed["footer"] == {"text": "arrVantage · Critical"}
        assert embed["timestamp"] == "2026-11-01T10:00:00Z"
        assert embed["fields"] == [{"name": "Services", "value": "Radarr", "inline": False}]

    def test_a_warning_a_recovery_and_a_test(self, app_module, receiver):
        self.send(app_module, receiver, event(severity="warning", outage=False))
        self.send(app_module, receiver, event("cleared"))
        app_module.send_discord({"webhook_url": receiver.url + "/t"}, {
            "event": "test", "title": "arrVantage test alert", "message": "hi", "severity": "warning", "kind": "test",
            "services": [], "since": "2026-11-01T10:00:00Z", "ended": None, "key": ""})
        warning, cleared, test = (r["json"]["embeds"][0] for r in receiver.requests)
        assert (warning["color"], warning["footer"]["text"]) == (app_module.DISCORD_COLORS["warning"], "arrVantage · Warning")
        assert (cleared["color"], cleared["footer"]["text"]) == (app_module.DISCORD_COLORS["cleared"], "arrVantage · Resolved")
        assert cleared["timestamp"] == "2026-11-01T10:07:00Z" and cleared["title"].startswith("Resolved: ")
        assert (test["color"], test["footer"]["text"]) == (app_module.DISCORD_COLORS["test"], "arrVantage · Test")
        assert "fields" not in test

    def test_a_service_named_everyone_cannot_ping_anyone(self, app_module, receiver):
        self.send(app_module, receiver, event(title="@everyone Radarr is down", services=("@everyone",)))
        body = receiver.requests[0]["json"]
        assert body["allowed_mentions"] == {"parse": []} and "@everyone" in body["embeds"][0]["title"]

    def test_long_text_is_cut_to_what_discord_accepts(self, app_module, receiver):
        (message,) = app_module.compose_messages([event(title="T" * 500)])
        message["message"] = "M" * 5000
        message["services"] = ["S" * 400] * 10
        app_module.send_discord({"webhook_url": receiver.url + "/x"}, message)
        embed = receiver.requests[0]["json"]["embeds"][0]
        assert len(embed["title"]) <= 256 and len(embed["description"]) <= 2000 and len(embed["fields"][0]["value"]) <= 1000

    def test_a_summary_message_lists_the_services(self, app_module, receiver):
        events = [event(key=f"unreachable:S{i}", title=f"S{i} can't be reached", services=(f"S{i}",)) for i in range(5)]
        (message,) = app_module.compose_messages(events)
        app_module.send_discord({"webhook_url": receiver.url + "/x"}, message)
        embed = receiver.requests[0]["json"]["embeds"][0]
        assert embed["title"] == "5 new problems" and embed["fields"][0]["value"] == "S0, S1, S2, S3, S4"

    def test_failures_use_discords_own_words(self, app_module, receiver):
        for status, expected in ((404, "does not exist"), (401, "check the webhook address"), (403, "check the webhook address"),
                                 (429, "rate limiting"), (400, "did not accept"), (500, None)):
            receiver.status = status
            ok, detail = self.send(app_module, receiver)
            assert ok is False and detail.startswith(f"HTTP {status}")
            if expected:
                assert expected in detail
        assert "access token" not in detail

    def test_discord_replying_204_is_a_success(self, app_module, receiver):
        receiver.status = 204
        assert self.send(app_module, receiver) == (True, "HTTP 204")

    def test_the_address_is_never_logged(self, app_module, receiver, caplog):
        receiver.status = 500
        (message,) = app_module.compose_messages([event()])
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            app_module.send_message({"discord": {"webhook_url": f"{receiver.url}/api/webhooks/{DISCORD_ID}/{DISCORD_TOKEN}"}}, message)
        text = all_logged(caplog)
        assert DISCORD_TOKEN not in text and DISCORD_ID not in text
        assert "Could not send an alert by discord: HTTP 500" in text

    def test_each_destination_an_admin_has_gets_the_message(self, app_module, receiver):
        (message,) = app_module.compose_messages([event()])
        results = app_module.send_message({"ntfy": {"server": receiver.url, "topic": "t"},
                                          "discord": {"webhook_url": receiver.url + "/d"},
                                          "webhook": {"webhook_url": receiver.url + "/w"}}, message)
        assert [(channel, ok) for channel, ok, _ in results] == [("ntfy", True), ("discord", True), ("webhook", True)]
        assert sorted(r["path"] for r in receiver.requests) == ["/", "/d", "/w"]

    def test_an_administrator_with_only_discord_gets_alerts_on_their_level(self, app_module, receiver, monkeypatch):
        add_admin(app_module, "gamer", {"level": "critical", "discord": {"webhook_url": DISCORD_URL}})
        # the real address is https://discord.com/...; send to the local receiver instead
        monkeypatch.setattr(app_module, "send_discord",
                            lambda settings, message: app_module._post_json(receiver.url + "/d", {"title": message["title"]}))
        app_module.deliver_events([event()])
        assert [r["json"] for r in receiver.requests] == [{"title": "Radarr can't be reached"}]


class TestDiscordSettingsApi:
    def post(self, client, csrf, path, body):
        return client.post(path, json=body, headers={"X-CSRF-Token": csrf})

    def test_saving_and_clearing(self, app_module, admin_session):
        client, csrf = admin_session
        reply = self.post(client, csrf, "/api/notifications", {"discord": {"webhook_url": DISCORD_URL}}).get_json()
        assert reply["ok"] and reply["notify"]["discord"] == {"set": True, "host": "discord.com"}
        assert DISCORD_TOKEN not in json.dumps(reply)
        assert app_module.get_user("testadmin")["notify"]["discord"]["webhook_url"] == DISCORD_URL
        # a blank box keeps what is saved
        self.post(client, csrf, "/api/notifications", {"level": "all", "discord": {"webhook_url": ""}})
        saved = app_module.get_user("testadmin")["notify"]
        assert saved["discord"]["webhook_url"] == DISCORD_URL and saved["level"] == "all"
        # remove clears it, and with nothing else left the whole setting goes
        reply = self.post(client, csrf, "/api/notifications", {"discord": {"remove": True}}).get_json()
        assert reply["ok"] and reply["notify"]["discord"]["set"] is False
        assert "notify" not in app_module.get_user("testadmin")

    def test_a_wrong_address_is_explained_and_nothing_is_saved(self, app_module, admin_session):
        client, csrf = admin_session
        for bad in ("https://example.com/hook", "not a url", "http://discord.com/api/webhooks/1/2"):
            reply = self.post(client, csrf, "/api/notifications", {"discord": {"webhook_url": bad}})
            assert reply.status_code == 400 and "Discord webhook address" in " ".join(reply.get_json()["errors"])
        assert "notify" not in app_module.get_user("testadmin")

    def test_the_get_never_returns_the_address(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"discord": {"webhook_url": DISCORD_URL}})
        text = client.get("/api/notifications").get_data(as_text=True)
        assert DISCORD_TOKEN not in text and DISCORD_ID not in text and '"discord"' in text

    def test_the_test_button_sends_to_discord(self, app_module, admin_session, receiver, monkeypatch):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"discord": {"webhook_url": DISCORD_URL}})
        real = app_module.send_discord
        monkeypatch.setattr(app_module, "send_discord", lambda settings, message: real({"webhook_url": receiver.url + "/d"}, message))
        reply = self.post(client, csrf, "/api/notifications/test", {}).get_json()
        assert reply == {"ok": True, "results": [{"channel": "discord", "ok": True, "detail": "HTTP 200"}]}
        assert receiver.requests[0]["json"]["embeds"][0]["footer"]["text"] == "arrVantage · Test"

    def test_the_address_is_a_secret_everywhere_else(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"discord": {"webhook_url": DISCORD_URL}})
        assert DISCORD_TOKEN not in client.get("/settings").get_data(as_text=True)
        assert DISCORD_TOKEN not in client.get("/api/config/export").get_data(as_text=True)
        assert DISCORD_TOKEN in client.get("/api/config/export?secrets=1").get_data(as_text=True)
        assert DISCORD_TOKEN not in client.get("/api/diagnostics?hide_addresses=0").get_data(as_text=True)
        text = client.get("/api/diagnostics?hide_addresses=0").get_json()["text"]
        assert "discord: 1" in text

    def test_a_keyless_import_keeps_it_and_a_full_backup_restores_it(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"discord": {"webhook_url": DISCORD_URL}})
        plain = json.loads(client.get("/api/config/export").get_data(as_text=True))
        assert client.post("/api/config/import", json={"file": plain, "confirm": True}, headers={"X-CSRF-Token": csrf}).get_json()["ok"]
        assert app_module.get_user("testadmin")["notify"]["discord"]["webhook_url"] == DISCORD_URL
        full = json.loads(client.get("/api/config/export?secrets=1").get_data(as_text=True))
        self.post(client, csrf, "/api/notifications", {"discord": {"remove": True}})
        assert "notify" not in app_module.get_user("testadmin")
        client.post("/api/config/import", json={"file": full, "confirm": True}, headers={"X-CSRF-Token": csrf})
        assert app_module.get_user("testadmin")["notify"]["discord"]["webhook_url"] == DISCORD_URL

    def test_a_file_cannot_point_an_account_at_a_non_discord_address(self, app_module, admin_session):
        client, csrf = admin_session
        full = json.loads(client.get("/api/config/export?secrets=1").get_data(as_text=True))
        full["config"]["users"][0]["notify"] = {"discord": {"webhook_url": "https://example.com/x"}}
        client.post("/api/config/import", json={"file": full, "confirm": True}, headers={"X-CSRF-Token": csrf})
        assert "notify" not in app_module.get_user("testadmin")

    def test_the_settings_page_has_the_discord_box(self, app_module, admin_session):
        client, _ = admin_session
        html = client.get("/settings").get_data(as_text=True)
        assert 'id="alerts-discord-url"' in html and 'id="alerts-discord-remove"' in html


# ---------------------------------------------------------------------------
# Telegram and email
# ---------------------------------------------------------------------------

import base64
import email as email_lib
import smtplib
import socket
import socketserver
import ssl

TG_TOKEN = "123456789:AAH-secretTelegramBotTokenValue0123456"
TG_CHAT = "-1001234567890"
MAIL_PASSWORD = "s3cretMailPassw0rd"
MAIL_USER = "alerts@example.com"


@pytest.fixture
def telegram(app_module, receiver, monkeypatch):
    """Telegram's address is fixed in the code; point it at the local receiver."""
    monkeypatch.setattr(app_module, "TELEGRAM_API", receiver.url)
    return receiver


@pytest.fixture
def smtp_server():
    """A small mail server on this machine: records what it is given. Offers AUTH PLAIN."""
    class Box:
        messages = []
        logins = []
        reject_login = False
        reject_rcpt = False
        offer_login = False
        greeting = "220 fake ESMTP"

    box = Box()
    box.messages, box.logins = [], []

    class Handler(socketserver.StreamRequestHandler):
        def say(self, text):
            self.wfile.write((text + "\r\n").encode())

        def handle(self):
            self.say(box.greeting)
            if not box.greeting.startswith("220"):
                return
            sender, rcpts = "", []
            for raw in self.rfile:
                line = raw.decode(errors="replace").rstrip("\r\n")
                upper = line.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    self.say("250-fake")
                    self.say("250-AUTH PLAIN LOGIN" if box.offer_login else "250-AUTH PLAIN")
                    self.say("250 8BITMIME")
                elif upper.startswith("AUTH LOGIN"):
                    return  # hangs up, as Gmail does on a second attempt after a refusal
                elif upper.startswith("AUTH PLAIN"):
                    _, user, password = base64.b64decode(line.split()[2]).decode().split("\0")
                    box.logins.append((user, password))
                    self.say("535 5.7.8 bad credentials" if box.reject_login else "235 2.7.0 ok")
                elif upper.startswith("MAIL FROM"):
                    sender = line.split(":", 1)[1].strip().strip("<>").split(">")[0]
                    self.say("250 ok")
                elif upper.startswith("RCPT TO"):
                    if box.reject_rcpt:
                        self.say("550 no such user")
                    else:
                        rcpts.append(line.split(":", 1)[1].strip().strip("<>").split(">")[0])
                        self.say("250 ok")
                elif upper == "DATA":
                    self.say("354 go")
                    data = []
                    for body_line in self.rfile:
                        if body_line in (b".\r\n", b".\n"):
                            break
                        data.append(body_line.decode(errors="replace"))
                    box.messages.append({"from": sender, "to": list(rcpts), "raw": "".join(data)})
                    self.say("250 queued")
                elif upper == "QUIT":
                    self.say("221 bye")
                    return
                else:
                    self.say("250 ok")

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    box.port = server.server_address[1]
    yield box
    server.shutdown()
    server.server_close()


def mail_settings(smtp_server, **extra):
    base = {"host": "127.0.0.1", "port": smtp_server.port, "security": "none", "mail_from": MAIL_USER,
            "mail_to": ["me@example.com"]}
    base.update(extra)
    return base


class TestTelegramAndEmailSettingsAreChecked:
    def test_a_telegram_destination_needs_a_real_token_and_chat(self, app_module):
        ok = app_module.normalize_telegram({"bot_token": TG_TOKEN, "chat_id": TG_CHAT})
        assert ok == {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}
        assert app_module.normalize_telegram({"bot_token": TG_TOKEN, "chat_id": "@mychannel"})["chat_id"] == "@mychannel"
        assert app_module.normalize_telegram({"bot_token": TG_TOKEN, "chat_id": 424242424})["chat_id"] == "424242424"
        for bad in ({"bot_token": "nope", "chat_id": TG_CHAT}, {"bot_token": TG_TOKEN, "chat_id": ""},
                    {"bot_token": TG_TOKEN, "chat_id": "12 34"}, {"bot_token": TG_TOKEN, "chat_id": "@x"},
                    {"bot_token": TG_TOKEN, "chat_id": True}, {"bot_token": "", "chat_id": TG_CHAT}, "text", None):
            assert app_module.normalize_telegram(bad) is None

    def test_an_email_destination_needs_a_server_a_sender_and_a_recipient(self, app_module):
        good = app_module.normalize_email({"host": "SMTP.Example.com", "mail_from": "a@example.com",
                                           "mail_to": "me@example.com, you@example.com"})
        assert good == {"host": "smtp.example.com", "port": 587, "security": "starttls", "mail_from": "a@example.com",
                        "mail_to": ["me@example.com", "you@example.com"]}
        for bad in ({"host": "", "mail_from": "a@b.co", "mail_to": "c@d.co"},
                    {"host": "http://smtp.example.com", "mail_from": "a@b.co", "mail_to": "c@d.co"},
                    {"host": "smtp.example.com/x", "mail_from": "a@b.co", "mail_to": "c@d.co"},
                    {"host": "smtp.example.com", "mail_from": "", "mail_to": "c@d.co"},
                    {"host": "smtp.example.com", "mail_from": "a@b.co", "mail_to": ""},
                    {"host": "smtp.example.com", "mail_from": "a@b.co", "mail_to": "not an address"}, None):
            assert app_module.normalize_email(bad) is None

    def test_the_login_can_stand_in_for_the_from_address(self, app_module):
        mail = app_module.normalize_email({"host": "h.example.com", "smtp_user": "me@example.com", "smtp_password": "x",
                                           "mail_to": "me@example.com"})
        assert mail["mail_from"] == "me@example.com" and mail["smtp_user"] == "me@example.com"
        assert app_module.normalize_email({"host": "h.example.com", "smtp_user": "plainlogin", "mail_to": "me@example.com"}) is None

    def test_ports_security_and_recipients_are_kept_in_bounds(self, app_module):
        base = {"host": "h.example.com", "mail_from": "a@b.co"}
        assert app_module.normalize_email({**base, "mail_to": "c@d.co", "security": "ssl"})["port"] == 465
        assert app_module.normalize_email({**base, "mail_to": "c@d.co", "security": "bogus"})["security"] == "starttls"
        assert app_module.normalize_email({**base, "mail_to": "c@d.co", "port": "99999"})["port"] == 587
        assert app_module.normalize_email({**base, "mail_to": "c@d.co", "port": "2525"})["port"] == 2525
        many = ",".join(f"u{i}@d.co" for i in range(9))
        assert len(app_module.normalize_email({**base, "mail_to": many})["mail_to"]) == app_module.MAIL_MAX_RECIPIENTS
        assert app_module.normalize_email({**base, "mail_to": "c@d.co, C@D.co"})["mail_to"] == ["c@d.co"]

    def test_a_password_without_a_login_is_dropped(self, app_module):
        mail = app_module.normalize_email({"host": "h.example.com", "mail_from": "a@b.co", "mail_to": "c@d.co",
                                           "smtp_password": "x"})
        assert "smtp_password" not in mail and "smtp_user" not in mail

    def test_the_browser_view_has_no_secrets(self, app_module):
        notify = app_module.normalize_notify({
            "telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT},
            "email": mail_settings(type("S", (), {"port": 25}), smtp_user="mailuser-secret", smtp_password=MAIL_PASSWORD)})
        text = json.dumps(app_module.notify_view(notify))
        for secret in (TG_TOKEN, TG_CHAT, MAIL_PASSWORD, "mailuser-secret"):
            assert secret not in text
        view = app_module.notify_view(notify)
        assert view["telegram"] == {"set": True}
        assert view["email"]["user_set"] is True and view["email"]["password_set"] is True
        assert view["email"]["host"] == "127.0.0.1" and view["email"]["mail_to"] == "me@example.com"
        # a list, because a JSON object would be sorted and STARTTLS (the usual choice) should come first
        assert [c["key"] for c in view["security_options"]] == ["starttls", "ssl", "none"]

    def test_either_alone_is_a_destination(self, app_module):
        only_telegram = app_module.normalize_notify({"telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}})
        assert list(only_telegram) == ["level", "telegram"]
        only_mail = app_module.normalize_notify({"email": {"host": "h.example.com", "mail_from": "a@b.co", "mail_to": "c@d.co"}})
        assert list(only_mail) == ["level", "email"]


class TestTelegramSending:
    def send(self, app_module, ev=None, chat=TG_CHAT):
        (message,) = app_module.compose_messages([ev or event()])
        return app_module.send_telegram({"bot_token": TG_TOKEN, "chat_id": chat}, message)

    def test_one_post_to_the_bots_send_message_with_the_chat_and_text(self, app_module, telegram):
        ok, detail = self.send(app_module)
        assert ok and detail == "HTTP 200"
        (got,) = telegram.requests
        assert got["path"] == f"/bot{TG_TOKEN}/sendMessage"
        body = got["json"]
        assert body["chat_id"] == TG_CHAT and body["parse_mode"] == "HTML"
        assert body["link_preview_options"] == {"is_disabled": True}
        assert body["text"] == "<b>Radarr can't be reached</b>\nConnection refused\n\n<i>Critical · Radarr</i>"

    def test_the_severity_is_written_in_words_for_every_kind(self, app_module, telegram):
        self.send(app_module, event(severity="warning", outage=False))
        self.send(app_module, event("cleared"))
        app_module.send_telegram({"bot_token": TG_TOKEN, "chat_id": TG_CHAT}, {
            "event": "test", "title": "arrVantage test alert", "message": "hi", "severity": "warning", "kind": "test",
            "services": [], "since": "2026-11-01T10:00:00Z", "ended": None, "key": ""})
        texts = [r["json"]["text"] for r in telegram.requests]
        assert texts[0].endswith("<i>Warning · Radarr</i>")
        assert texts[1].startswith("<b>Resolved: Radarr") and texts[1].endswith("<i>Resolved · Radarr</i>")
        assert texts[2].endswith("<i>Test</i>")

    def test_markup_in_a_service_name_is_shown_as_written(self, app_module, telegram):
        self.send(app_module, event(title="<b>Radarr</b> & co", services=("A<script>",)))
        text = telegram.requests[0]["json"]["text"]
        assert "<b>&lt;b&gt;Radarr&lt;/b&gt; &amp; co</b>" in text and "A&lt;script&gt;" in text
        assert "<script>" not in text

    def test_long_text_stays_inside_telegrams_limit(self, app_module, telegram):
        (message,) = app_module.compose_messages([event(title="T" * 500)])
        message["message"] = "M" * 9000
        message["services"] = ["S" * 400] * 10
        app_module.send_telegram({"bot_token": TG_TOKEN, "chat_id": TG_CHAT}, message)
        assert len(telegram.requests[0]["json"]["text"]) < 4096

    def test_failures_use_telegrams_own_words(self, app_module, telegram):
        for status, expected in ((400, "check the chat ID"), (401, "bot token is wrong"), (403, "blocked, or removed"),
                                 (404, "does not know this bot token"), (409, "webhook"), (429, "rate limiting"), (500, None)):
            telegram.status = status
            ok, detail = self.send(app_module)
            assert ok is False and detail.startswith(f"HTTP {status}")
            if expected:
                assert expected in detail
        assert "access token" not in detail

    def test_the_token_is_never_logged(self, app_module, telegram, caplog):
        telegram.status = 401
        (message,) = app_module.compose_messages([event()])
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            app_module.send_message({"telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}}, message)
        text = all_logged(caplog)
        assert TG_TOKEN not in text and "AAH-secret" not in text
        assert "Could not send an alert by telegram: HTTP 401" in text

    def test_it_can_only_ever_go_to_telegram(self, app_module):
        assert app_module.TELEGRAM_API == "https://api.telegram.org"


UPDATES = {"ok": True, "result": [
    {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 424242424, "first_name": "Zak", "last_name": "S", "type": "private"}, "text": "hi"}},
    {"update_id": 2, "my_chat_member": {"chat": {"id": -1001234567890, "title": "Homelab alerts", "type": "supergroup"}}},
    {"update_id": 3, "channel_post": {"message_id": 3, "chat": {"id": -1009999999999, "title": "News", "username": "newsfeed", "type": "channel"}}},
    {"update_id": 4, "message": {"message_id": 4, "chat": {"id": 424242424, "first_name": "Zak", "type": "private"}, "text": "again"}},
    {"update_id": 5, "callback_query": {"id": "x"}},
    "junk", {"update_id": 6, "message": {"chat": {"id": "not a number"}}},
]}


class TestFindingTheTelegramChat:
    def test_lists_each_chat_once_newest_first_with_readable_names(self, app_module, telegram):
        telegram.reply = json.dumps(UPDATES).encode()
        ok, chats = app_module.find_telegram_chats(TG_TOKEN)
        assert ok
        assert chats == [{"id": "424242424", "label": "Zak (private chat)"},
                         {"id": "-1009999999999", "label": "News (channel)"},
                         {"id": "-1001234567890", "label": "Homelab alerts (supergroup)"}]
        assert telegram.requests[0]["path"] == f"/bot{TG_TOKEN}/getUpdates"

    def test_nothing_yet_is_an_empty_list(self, app_module, telegram):
        telegram.reply = b'{"ok": true, "result": []}'
        assert app_module.find_telegram_chats(TG_TOKEN) == (True, [])

    def test_errors_are_explained_without_the_token(self, app_module, telegram):
        telegram.status = 401
        ok, words = app_module.find_telegram_chats(TG_TOKEN)
        assert ok is False and "bot token is wrong" in words and TG_TOKEN not in words
        telegram.status, telegram.reply = 200, b"not json"
        assert app_module.find_telegram_chats(TG_TOKEN) == (False, "Telegram's answer could not be read")

    def test_it_is_capped(self, app_module, telegram):
        many = {"ok": True, "result": [{"update_id": i, "message": {"chat": {"id": 1000 + i, "type": "private", "first_name": f"P{i}"}}}
                                       for i in range(40)]}
        telegram.reply = json.dumps(many).encode()
        assert len(app_module.find_telegram_chats(TG_TOKEN)[1]) == app_module.TELEGRAM_MAX_CHATS


class TestEmailMessage:
    def build(self, app_module, ev=None, **extra):
        (message,) = app_module.compose_messages([ev or event()])
        settings = app_module.normalize_email({"host": "h.example.com", "mail_from": MAIL_USER,
                                               "mail_to": "me@example.com, you@example.com", **extra})
        return app_module.build_email(settings, message)

    def test_a_problem_has_the_severity_and_title_in_the_subject(self, app_module):
        mail = self.build(app_module)
        assert mail["Subject"] == "[arrVantage] Critical: Radarr can't be reached"
        assert mail["From"] == MAIL_USER and mail["To"] == "me@example.com, you@example.com"
        assert mail["Auto-Submitted"] == "auto-generated"
        assert mail["Message-ID"].endswith("@example.com>")
        body = mail.get_content()
        assert "Connection refused" in body and "Severity: Critical" in body and "Services: Radarr" in body
        assert "Started: 2026-11-01T10:00:00Z (UTC)" in body

    def test_a_warning_a_recovery_and_a_test(self, app_module):
        assert self.build(app_module, event(severity="warning", outage=False))["Subject"].startswith("[arrVantage] Warning: ")
        cleared = self.build(app_module, event("cleared"))
        assert cleared["Subject"] == "[arrVantage] Resolved: Radarr can't be reached"
        assert "Ended: 2026-11-01T10:07:00Z (UTC)" in cleared.get_content()
        settings = app_module.normalize_email({"host": "h.example.com", "mail_from": MAIL_USER, "mail_to": "me@example.com"})
        test = app_module.build_email(settings, {"event": "test", "title": "arrVantage test alert", "message": "hi",
                                                 "severity": "warning", "kind": "test", "services": [], "since": None,
                                                 "ended": None, "key": ""})
        assert test["Subject"] == "arrVantage test alert"

    def test_a_line_break_in_a_title_cannot_add_a_header(self, app_module):
        mail = self.build(app_module, event(title="Radarr down\r\nBcc: someone@evil.example"))
        assert "\n" not in mail["Subject"] and mail["Bcc"] is None
        assert "someone@evil.example" in mail["Subject"]  # kept as text on the one line

    def test_a_summary_lists_the_services(self, app_module):
        events = [event(key=f"unreachable:S{i}", title=f"S{i} can't be reached", services=(f"S{i}",)) for i in range(5)]
        (message,) = app_module.compose_messages(events)
        settings = app_module.normalize_email({"host": "h.example.com", "mail_from": MAIL_USER, "mail_to": "me@example.com"})
        mail = app_module.build_email(settings, message)
        assert mail["Subject"] == "[arrVantage] Critical: 5 new problems" and "Services: S0, S1, S2, S3, S4" in mail.get_content()


class TestEmailSending:
    def test_a_message_reaches_the_mail_server_with_a_login(self, app_module, smtp_server):
        (message,) = app_module.compose_messages([event()])
        ok, detail = app_module.send_email(
            mail_settings(smtp_server, smtp_user=MAIL_USER, smtp_password=MAIL_PASSWORD, mail_to=["me@example.com", "you@example.com"]), message)
        assert (ok, detail) == (True, "sent")
        assert smtp_server.logins == [(MAIL_USER, MAIL_PASSWORD)]
        (got,) = smtp_server.messages
        assert got["from"] == MAIL_USER and got["to"] == ["me@example.com", "you@example.com"]
        parsed = email_lib.message_from_string(got["raw"])
        assert parsed["Subject"] == "[arrVantage] Critical: Radarr can't be reached"
        assert "Connection refused" in parsed.get_payload(decode=True).decode()

    def test_no_login_means_no_login_attempt(self, app_module, smtp_server):
        (message,) = app_module.compose_messages([event()])
        assert app_module.send_email(mail_settings(smtp_server), message)[0] is True
        assert smtp_server.logins == []

    def test_failures_are_explained_in_words(self, app_module, smtp_server):
        (message,) = app_module.compose_messages([event()])
        smtp_server.reject_login = True
        ok, detail = app_module.send_email(mail_settings(smtp_server, smtp_user=MAIL_USER, smtp_password="wrong"), message)
        assert ok is False and "refused the login" in detail and "wrong" not in detail
        smtp_server.reject_login, smtp_server.reject_rcpt = False, True
        ok, detail = app_module.send_email(mail_settings(smtp_server), message)
        assert ok is False and "refused the recipient" in detail and "me@example.com" not in detail
        smtp_server.reject_rcpt, smtp_server.greeting = False, "554 no service"
        ok, detail = app_module.send_email(mail_settings(smtp_server), message)
        assert ok is False and "answered 554" in detail

    def test_a_server_that_is_not_there(self, app_module):
        (message,) = app_module.compose_messages([event()])
        ok, detail = app_module.send_email({"host": "127.0.0.1", "port": 1, "security": "none", "mail_from": MAIL_USER,
                                            "mail_to": ["me@example.com"]}, message)
        assert (ok, detail) == (False, "could not connect (while connecting)")

    def test_a_name_that_does_not_exist(self, app_module):
        (message,) = app_module.compose_messages([event()])
        ok, detail = app_module.send_email({"host": "no-such-host.invalid", "port": 25, "security": "none", "mail_from": MAIL_USER,
                                            "mail_to": ["me@example.com"]}, message)
        assert ok is False and detail in ("could not find the mail server (check its name)", "could not connect")

    def test_tls_on_a_plain_port_says_what_to_check(self, app_module, smtp_server):
        (message,) = app_module.compose_messages([event()])
        ok, detail = app_module.send_email(mail_settings(smtp_server, security="ssl"), message)
        assert ok is False and "secure connection" in detail or "closed the connection" in detail

    def test_starttls_on_a_server_without_it_is_explained(self, app_module, smtp_server):
        (message,) = app_module.compose_messages([event()])
        ok, detail = app_module.send_email(mail_settings(smtp_server, security="starttls"), message)
        assert ok is False and "does not offer the security" in detail

    def test_non_ascii_login_is_explained_not_raised(self, app_module, smtp_server):
        (message,) = app_module.compose_messages([event()])
        ok, detail = app_module.send_email(mail_settings(smtp_server, smtp_user="mailé@example.com", smtp_password="päss"), message)
        assert ok is False and "characters" in detail

    def test_the_connection_steps_for_each_security_option(self, app_module, monkeypatch):
        calls = []

        class Fake:
            def __init__(self, *args, **kwargs):
                calls.append((self.__class__.__name__, args[:2], kwargs.get("timeout"), type(kwargs.get("context")).__name__))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                calls.append("quit")

            def ehlo(self):
                calls.append("ehlo")

            def starttls(self, context=None):
                calls.append(("starttls", type(context).__name__))

            esmtp_features = {"auth": "LOGIN PLAIN"}
            user = password = ""

            def ehlo_or_helo_if_needed(self):
                pass

            def has_extn(self, name):
                return name.lower() in self.esmtp_features

            def auth_plain(self, challenge=None):
                return None

            def auth(self, mechanism, authobject, initial_response_ok=True):
                # one attempt, with PLAIN (smtplib's login() would go on to LOGIN after a refusal)
                calls.append(("login", self.user, self.password))
                calls.append(("method", mechanism))

            def send_message(self, mail, from_addr=None, to_addrs=None):
                calls.append(("send", from_addr, tuple(to_addrs)))

        class FakeSSL(Fake):
            pass

        monkeypatch.setattr(smtplib, "SMTP", Fake)
        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
        (message,) = app_module.compose_messages([event()])
        base = {"host": "smtp.example.com", "mail_from": MAIL_USER, "mail_to": ["me@example.com"],
                "smtp_user": MAIL_USER, "smtp_password": "pw"}
        app_module.send_email({**base, "port": 587, "security": "starttls"}, message)
        assert calls == [("Fake", ("smtp.example.com", 587), app_module.NOTIFY_TIMEOUT_SECONDS, "NoneType"), "ehlo",
                         ("starttls", "SSLContext"), "ehlo", ("login", MAIL_USER, "pw"), ("method", "PLAIN"),
                         ("send", MAIL_USER, ("me@example.com",)), "quit"]
        calls.clear()
        app_module.send_email({**base, "port": 465, "security": "ssl"}, message)
        assert calls[0] == ("FakeSSL", ("smtp.example.com", 465), app_module.NOTIFY_TIMEOUT_SECONDS, "SSLContext")
        assert ("starttls", "SSLContext") not in calls and ("login", MAIL_USER, "pw") in calls
        calls.clear()
        app_module.send_email({**base, "port": 25, "security": "none", "smtp_user": ""}, message)
        assert not any(isinstance(c, tuple) and c[0] in ("starttls", "login") for c in calls)

    def test_the_password_is_never_logged(self, app_module, smtp_server, caplog):
        smtp_server.reject_login = True
        (message,) = app_module.compose_messages([event()])
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            app_module.send_message({"email": mail_settings(smtp_server, smtp_user=MAIL_USER, smtp_password=MAIL_PASSWORD)}, message)
        text = all_logged(caplog)
        assert MAIL_PASSWORD not in text and MAIL_USER not in text
        assert "Could not send an alert by email: the mail server refused the login" in text


class TestDeliveryToEveryDestination:
    def test_each_destination_an_admin_has_gets_the_message(self, app_module, receiver, telegram, smtp_server):
        (message,) = app_module.compose_messages([event()])
        notify = {"ntfy": {"server": receiver.url, "topic": "t"}, "discord": {"webhook_url": receiver.url + "/d"},
                  "telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT},
                  "email": mail_settings(smtp_server), "webhook": {"webhook_url": receiver.url + "/w"}}
        results = app_module.send_message(notify, message)
        assert [(channel, ok) for channel, ok, _ in results] == [
            ("ntfy", True), ("discord", True), ("telegram", True), ("email", True), ("webhook", True)]
        assert len(smtp_server.messages) == 1
        assert sorted(r["path"] for r in receiver.requests) == sorted(["/", "/d", f"/bot{TG_TOKEN}/sendMessage", "/w"])

    def test_a_failing_destination_does_not_stop_the_next(self, app_module, telegram, smtp_server):
        telegram.status = 401
        (message,) = app_module.compose_messages([event()])
        results = app_module.send_message({"telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT},
                                           "email": mail_settings(smtp_server)}, message)
        assert [(c, ok) for c, ok, _ in results] == [("telegram", False), ("email", True)]
        assert len(smtp_server.messages) == 1

    def test_an_admin_with_only_email_hears_alerts_on_their_level(self, app_module, smtp_server):
        add_admin(app_module, "mailer", {"level": "critical", "email": mail_settings(smtp_server)})
        stalled = event(key="queue:Radarr", title="2 downloads stalled", severity="warning", outage=False)
        app_module.deliver_events([event(), stalled])
        assert len(smtp_server.messages) == 1


class TestTelegramAndEmailSettingsApi:
    def post(self, client, csrf, path, body):
        return client.post(path, json=body, headers={"X-CSRF-Token": csrf})

    def mail_body(self, smtp_server=None, **extra):
        body = {"host": "mail.example.com", "port": "587", "security": "starttls", "mail_from": MAIL_USER, "mail_to": "me@example.com"}
        body.update(extra)
        return body

    def test_saving_keeping_and_removing_telegram(self, app_module, admin_session):
        client, csrf = admin_session
        reply = self.post(client, csrf, "/api/notifications", {"telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}}).get_json()
        assert reply["ok"] and reply["notify"]["telegram"] == {"set": True}
        assert TG_TOKEN not in json.dumps(reply) and TG_CHAT not in json.dumps(reply)
        assert app_module.get_user("testadmin")["notify"]["telegram"] == {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}
        # blank keeps both; a new chat alone replaces only the chat
        self.post(client, csrf, "/api/notifications", {"level": "all", "telegram": {"bot_token": "", "chat_id": ""}})
        assert app_module.get_user("testadmin")["notify"]["telegram"] == {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}
        self.post(client, csrf, "/api/notifications", {"telegram": {"chat_id": "424242424"}})
        assert app_module.get_user("testadmin")["notify"]["telegram"] == {"bot_token": TG_TOKEN, "chat_id": "424242424"}
        reply = self.post(client, csrf, "/api/notifications", {"telegram": {"remove": True}}).get_json()
        assert reply["notify"]["telegram"] == {"set": False} and "notify" not in app_module.get_user("testadmin")

    def test_telegram_mistakes_are_explained_and_nothing_is_saved(self, app_module, admin_session):
        client, csrf = admin_session
        for body, text in (({"bot_token": "abc", "chat_id": TG_CHAT}, "bot token"),
                           ({"bot_token": TG_TOKEN, "chat_id": "hello"}, "chat ID"),
                           ({"bot_token": TG_TOKEN}, "both the bot token and the chat ID"),
                           ({"chat_id": TG_CHAT}, "both the bot token and the chat ID")):
            reply = self.post(client, csrf, "/api/notifications", {"telegram": body})
            assert reply.status_code == 400 and text in " ".join(reply.get_json()["errors"])
        assert "notify" not in app_module.get_user("testadmin")

    def test_saving_keeping_and_removing_email(self, app_module, admin_session):
        client, csrf = admin_session
        reply = self.post(client, csrf, "/api/notifications", {"email": self.mail_body(smtp_user=MAIL_USER, smtp_password=MAIL_PASSWORD)}).get_json()
        assert reply["ok"]
        assert reply["notify"]["email"] == {
            "set": True, "host": "mail.example.com", "port": 587, "security": "starttls", "mail_from": MAIL_USER,
            "mail_to": "me@example.com", "user_set": True, "password_set": True}
        assert MAIL_PASSWORD not in json.dumps(reply)
        saved = app_module.get_user("testadmin")["notify"]["email"]
        assert saved["smtp_user"] == MAIL_USER and saved["smtp_password"] == MAIL_PASSWORD
        # saving again with blank login and password keeps both; other fields change
        self.post(client, csrf, "/api/notifications", {"email": self.mail_body(host="other.example.com", mail_to="a@b.co, c@d.co")})
        saved = app_module.get_user("testadmin")["notify"]["email"]
        assert saved["host"] == "other.example.com" and saved["mail_to"] == ["a@b.co", "c@d.co"]
        assert saved["smtp_user"] == MAIL_USER and saved["smtp_password"] == MAIL_PASSWORD
        # only the password replaced
        self.post(client, csrf, "/api/notifications", {"email": self.mail_body(smtp_password="newpassword")})
        assert app_module.get_user("testadmin")["notify"]["email"]["smtp_password"] == "newpassword"
        # the login can be removed on its own
        self.post(client, csrf, "/api/notifications", {"email": self.mail_body(clear_login=True)})
        saved = app_module.get_user("testadmin")["notify"]["email"]
        assert "smtp_user" not in saved and "smtp_password" not in saved
        reply = self.post(client, csrf, "/api/notifications", {"email": {"remove": True}}).get_json()
        assert reply["notify"]["email"]["set"] is False and "notify" not in app_module.get_user("testadmin")

    def test_changing_the_security_without_a_port_uses_its_usual_port(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"email": self.mail_body(port="", security="ssl")})
        assert app_module.get_user("testadmin")["notify"]["email"]["port"] == 465

    def test_email_mistakes_are_explained_and_nothing_is_saved(self, app_module, admin_session):
        client, csrf = admin_session
        cases = (
            (self.mail_body(host="https://mail.example.com"), "mail server's name"),
            (self.mail_body(host=""), "mail server's name"),
            (self.mail_body(port="abc"), "port"),
            (self.mail_body(port="70000"), "port"),
            (self.mail_body(mail_from="nonsense"), "From address"),
            (self.mail_body(mail_from=""), "From address"),
            (self.mail_body(mail_to=""), "address to send alerts to"),
            (self.mail_body(mail_to="me@example.com, nonsense"), "Not an email address: nonsense"),
            (self.mail_body(smtp_password="x"), "login name as well as the password"),
            (self.mail_body(mail_to=",".join(f"u{i}@d.co" for i in range(8))), "at most 5"),
        )
        for body, text in cases:
            reply = self.post(client, csrf, "/api/notifications", {"email": body})
            assert reply.status_code == 400, body
            assert text in " ".join(reply.get_json()["errors"]), (body, reply.get_json())
        assert "notify" not in app_module.get_user("testadmin")

    def test_the_login_can_be_the_from_address(self, app_module, admin_session):
        client, csrf = admin_session
        reply = self.post(client, csrf, "/api/notifications", {"email": self.mail_body(mail_from="", smtp_user="me@example.com", smtp_password="x")})
        assert reply.get_json()["ok"]
        assert app_module.get_user("testadmin")["notify"]["email"]["mail_from"] == "me@example.com"

    def test_the_test_button_sends_to_both(self, app_module, admin_session, telegram, smtp_server):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {
            "telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT},
            "email": self.mail_body(host="127.0.0.1", port=str(smtp_server.port), security="none")})
        reply = self.post(client, csrf, "/api/notifications/test", {}).get_json()
        assert reply == {"ok": True, "results": [{"channel": "telegram", "ok": True, "detail": "HTTP 200"},
                                                 {"channel": "email", "ok": True, "detail": "sent",
                                                  "via": f"127.0.0.1:{smtp_server.port}, none"}]}
        assert telegram.requests[0]["json"]["text"].endswith("<i>Test</i>")
        assert email_lib.message_from_string(smtp_server.messages[0]["raw"])["Subject"] == "arrVantage test alert"

    def test_the_secrets_stay_out_of_every_other_place(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {
            "telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT},
            "email": self.mail_body(smtp_user="mailuser-secret", smtp_password=MAIL_PASSWORD)})
        secrets_ = (TG_TOKEN, TG_CHAT, MAIL_PASSWORD, "mailuser-secret")
        for text in (client.get("/api/notifications").get_data(as_text=True), client.get("/settings").get_data(as_text=True),
                     client.get("/api/config/export").get_data(as_text=True),
                     client.get("/api/diagnostics?hide_addresses=0").get_data(as_text=True)):
            for secret in secrets_:
                assert secret not in text
        full = client.get("/api/config/export?secrets=1").get_data(as_text=True)
        for secret in secrets_:
            assert secret in full
        diagnostics = client.get("/api/diagnostics?hide_addresses=0").get_json()["text"]
        assert "telegram: 1, email: 1" in diagnostics
        # the recipient and sender addresses are personal too
        assert "me@example.com" not in client.get("/api/config/export").get_data(as_text=True)
        assert MAIL_USER not in client.get("/api/config/export").get_data(as_text=True)

    def test_a_keyless_import_keeps_them_and_a_full_backup_restores_them(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {
            "telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}, "email": self.mail_body(smtp_user=MAIL_USER, smtp_password=MAIL_PASSWORD)})
        before = app_module.get_user("testadmin")["notify"]
        plain = json.loads(client.get("/api/config/export").get_data(as_text=True))
        assert client.post("/api/config/import", json={"file": plain, "confirm": True}, headers={"X-CSRF-Token": csrf}).get_json()["ok"]
        assert app_module.get_user("testadmin")["notify"] == before
        full = json.loads(client.get("/api/config/export?secrets=1").get_data(as_text=True))
        self.post(client, csrf, "/api/notifications", {"telegram": {"remove": True}, "email": {"remove": True}})
        assert "notify" not in app_module.get_user("testadmin")
        client.post("/api/config/import", json={"file": full, "confirm": True}, headers={"X-CSRF-Token": csrf})
        assert app_module.get_user("testadmin")["notify"] == before

    def test_saving_settings_keeps_them(self, app_module, admin_session):
        client, csrf = admin_session
        self.post(client, csrf, "/api/notifications", {"telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}, "email": self.mail_body()})
        form = {"csrf_token": csrf, "user_username_a": "testadmin", "user_role_a": "admin"}
        assert client.post("/settings", data=form).status_code == 302
        saved = app_module.get_user("testadmin")["notify"]
        assert saved["telegram"]["bot_token"] == TG_TOKEN and saved["email"]["host"] == "mail.example.com"

    def test_a_regular_account_cannot_use_any_of_it(self, app_module, admin_session):
        add_admin(app_module, "viewer", role="user")
        other = app_module.app.test_client()
        token = other.get("/login").data.decode().split('name="csrf_token" value="')[1].split('"')[0]
        other.post("/login", data={"username": "viewer", "password": "adminpass12345", "csrf_token": token})
        reply = other.post("/api/notifications/telegram-chats", json={"bot_token": TG_TOKEN}, headers={"X-CSRF-Token": token})
        assert reply.status_code == 403

    def test_the_chat_finder_endpoint(self, app_module, admin_session, telegram):
        client, csrf = admin_session
        telegram.reply = json.dumps(UPDATES).encode()
        # nothing typed and nothing saved
        reply = self.post(client, csrf, "/api/notifications/telegram-chats", {})
        assert reply.status_code == 400 and "bot token" in reply.get_json()["error"]
        # a mistyped token is refused before anything is sent
        reply = self.post(client, csrf, "/api/notifications/telegram-chats", {"bot_token": "abc"})
        assert reply.status_code == 400 and telegram.requests == []
        # a typed token
        reply = self.post(client, csrf, "/api/notifications/telegram-chats", {"bot_token": TG_TOKEN}).get_json()
        assert reply["ok"] and reply["chats"][0] == {"id": "424242424", "label": "Zak (private chat)"}
        assert telegram.requests[-1]["path"] == f"/bot{TG_TOKEN}/getUpdates"
        # the saved token is used when the box is blank
        self.post(client, csrf, "/api/notifications", {"telegram": {"bot_token": TG_TOKEN, "chat_id": TG_CHAT}})
        telegram.requests.clear()
        assert self.post(client, csrf, "/api/notifications/telegram-chats", {"bot_token": ""}).get_json()["ok"]
        assert telegram.requests[0]["path"] == f"/bot{TG_TOKEN}/getUpdates"
        # an error from Telegram comes back in words
        telegram.status = 401
        reply = self.post(client, csrf, "/api/notifications/telegram-chats", {"bot_token": TG_TOKEN})
        assert reply.status_code == 400 and "bot token is wrong" in reply.get_json()["error"] and TG_TOKEN not in reply.get_data(as_text=True)

    def test_the_settings_page_has_the_boxes(self, app_module, admin_session):
        client, _ = admin_session
        page = client.get("/settings").get_data(as_text=True)
        for element in ("alerts-telegram-token", "alerts-telegram-chat", "alerts-telegram-find", "alerts-telegram-remove",
                        "alerts-email-host", "alerts-email-security", "alerts-email-port", "alerts-email-user",
                        "alerts-email-password", "alerts-email-from", "alerts-email-to", "alerts-email-remove", "alerts-email-clear-login"):
            assert f'id="{element}"' in page


class TestEmailSaysWhichStepFailed:
    def run(self, app_module, smtp_server, **kw):
        (message,) = app_module.compose_messages([event()])
        return app_module.send_email(mail_settings(smtp_server, **kw), message)

    def test_a_server_that_hangs_up_on_the_greeting_is_a_connecting_problem(self, app_module, smtp_server):
        smtp_server.greeting = "421 go away"
        ok, detail = self.run(app_module, smtp_server)
        assert ok is False and "answered 421 (while connecting)" in detail

    def test_each_step_is_named_in_the_words(self, app_module):
        words = app_module._smtp_words
        gone = smtplib.SMTPServerDisconnected("x")
        assert words(gone, "sending the message") == "the mail server closed the connection (while sending the message) (check the security option and port)"
        assert words(gone, "") == "the mail server closed the connection (check the security option and port)"
        assert "(while starting encryption)" in words(ssl.SSLError("x"), "starting encryption")
        assert words(TimeoutError(), "saying hello") == "timed out (while saying hello)"
        assert words(smtplib.SMTPDataError(554, b"nope"), "sending the message") == "the mail server answered 554 (while sending the message)"
        # the specific causes already say what happened, and carry no server text
        assert "(while" not in words(smtplib.SMTPAuthenticationError(535, b"secret reply"), "logging in")
        assert "secret reply" not in words(smtplib.SMTPAuthenticationError(535, b"secret reply"), "logging in")

    def test_a_real_login_failure_is_still_a_login_refusal(self, app_module, smtp_server):
        smtp_server.reject_login = True
        ok, detail = self.run(app_module, smtp_server, smtp_user=MAIL_USER, smtp_password="wrong")
        assert ok is False and "refused the login" in detail

    def test_a_refused_login_is_not_retried_with_another_method(self, app_module, smtp_server):
        # smtplib's login() would try AUTH LOGIN after PLAIN was refused, and Gmail then hangs up,
        # which used to show as "closed the connection" instead of the real refusal.
        smtp_server.offer_login, smtp_server.reject_login = True, True
        ok, detail = self.run(app_module, smtp_server, smtp_user=MAIL_USER, smtp_password="wrong")
        assert ok is False and "refused the login" in detail
        assert smtp_server.logins == [(MAIL_USER, "wrong")]

    def test_a_server_that_hangs_up_during_login_says_to_check_the_login(self, app_module):
        words = app_module._smtp_words(smtplib.SMTPServerDisconnected("x"), "logging in")
        assert "hung up while logging in" in words and "app password" in words

    def test_a_good_login_still_works_when_login_is_offered(self, app_module, smtp_server):
        smtp_server.offer_login = True
        ok, detail = self.run(app_module, smtp_server, smtp_user=MAIL_USER, smtp_password=MAIL_PASSWORD)
        assert (ok, detail) == (True, "sent") and smtp_server.logins == [(MAIL_USER, MAIL_PASSWORD)]
