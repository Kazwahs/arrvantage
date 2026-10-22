"""
Logging: quiet by default, failures only, and never a secret.

arrVantage once logged nothing at all, so a user could only report "HTTP 401"
with no way to see what had been sent where. Every call to a service now goes
through one wrapper (requests' Session.send): failures are warnings, successes
are DEBUG, and a request is only described by method, host and path.
"""
import logging
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

HEADERS = lambda csrf: {"X-CSRF-Token": csrf}  # noqa: E731

SECRET = "SUPERSECRETKEY123"


def messages(caplog, level=logging.DEBUG):
    return [r.getMessage() for r in caplog.records if r.name == "arrvantage" and r.levelno >= level]


class TestLogLevel:
    def test_defaults_to_warning(self, app_module):
        assert app_module.resolve_log_level(None) == logging.WARNING
        assert app_module.resolve_log_level("") == logging.WARNING

    def test_reads_names_in_any_case(self, app_module):
        assert app_module.resolve_log_level("debug") == logging.DEBUG
        assert app_module.resolve_log_level(" Info ") == logging.INFO
        assert app_module.resolve_log_level("ERROR") == logging.ERROR

    def test_a_typo_means_warning_not_silence_or_flood(self, app_module):
        assert app_module.resolve_log_level("verbose") == logging.WARNING
        assert app_module.resolve_log_level("NOTSET") == logging.WARNING

    def test_the_environment_variable_is_honoured(self, app_module, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "debug")
        app_module.configure_logging()
        assert app_module.log.level == logging.DEBUG
        monkeypatch.delenv("LOG_LEVEL")
        app_module.configure_logging()
        assert app_module.log.level == logging.WARNING

    def test_configuring_twice_adds_one_handler(self, app_module):
        app_module.configure_logging()
        app_module.configure_logging()
        assert len([h for h in app_module.log.handlers if getattr(h, "_arrvantage", False)]) == 1


class TestScrubbing:
    def test_query_strings_are_removed(self, app_module):
        text = "Max retries exceeded with url: /api?apikey=SECRET&mode=queue (Caused by X)"
        scrubbed = app_module.scrub_log_text(text)
        assert "SECRET" not in scrubbed
        assert "/api?[removed]" in scrubbed
        assert "(Caused by X)" in scrubbed

    def test_credentials_in_a_url_are_removed(self, app_module):
        scrubbed = app_module.scrub_log_text("failed http://admin:hunter2@host:8080/x")
        assert "hunter2" not in scrubbed and "admin" not in scrubbed
        assert "host:8080/x" in scrubbed

    def test_ordinary_question_marks_survive(self, app_module):
        assert app_module.scrub_log_text("Is it down? Yes") == "Is it down? Yes"

    def test_long_text_is_cut(self, app_module):
        assert len(app_module.scrub_log_text("x" * 5000)) < 400

    def test_a_request_is_described_by_host_and_path_only(self, app_module):
        what = app_module.describe_request("GET", "http://user:pw@jf:8096/Items/Latest?ApiKey=SECRET#x")
        assert what == "GET jf:8096/Items/Latest"


@pytest.fixture
def local_server():
    """A real HTTP server on this machine. requests_mock swaps out
    Session.send, which is exactly where the logging hooks in, so these
    tests talk over a real socket instead. /status/NNN answers NNN."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            found = re.search(r"/status/(\d+)", self.path)
            self.send_response(int(found.group(1)) if found else 200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestOutboundCalls:
    def test_a_rejected_key_is_logged_without_the_key(self, app_module, local_server, caplog):
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            requests.get(f"http://{local_server}/status/401", params={"ApiKey": SECRET},
                         headers={"X-Emby-Token": SECRET, "Authorization": f'MediaBrowser Token="{SECRET}"'})
        lines = messages(caplog, logging.WARNING)
        assert len(lines) == 1
        assert f"GET {local_server}/status/401 -> HTTP 401" in lines[0]
        assert SECRET not in " ".join(r.getMessage() for r in caplog.records)

    def test_a_connection_error_is_logged_without_the_key(self, app_module, caplog):
        port = closed_port()
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            with pytest.raises(requests.exceptions.ConnectionError):
                requests.get(f"http://127.0.0.1:{port}/api", params={"apikey": SECRET}, timeout=3)
        lines = messages(caplog, logging.WARNING)
        assert len(lines) == 1
        assert f"GET 127.0.0.1:{port}/api failed: ConnectionError" in lines[0]
        assert SECRET not in " ".join(r.getMessage() for r in caplog.records)

    def test_success_is_only_a_debug_line(self, app_module, local_server, caplog):
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            requests.get(f"http://{local_server}/ping")
        assert messages(caplog, logging.INFO) == []
        assert any(f"GET {local_server}/ping -> HTTP 200" in m for m in messages(caplog))

    def test_the_same_failure_is_warned_about_once(self, app_module, local_server, caplog):
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            for _ in range(5):
                requests.get(f"http://{local_server}/status/503")
        assert len(messages(caplog, logging.WARNING)) == 1
        assert len([m for m in messages(caplog) if "HTTP 503" in m]) == 5  # the rest are still at DEBUG

    def test_ids_in_a_path_do_not_defeat_the_repeat_filter(self, app_module, local_server, caplog):
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            for movie_id in (1, 2, 3):
                requests.get(f"http://{local_server}/api/v3/movie/{movie_id}/status/404")
        assert len(messages(caplog, logging.WARNING)) == 1

    def test_a_different_failure_is_still_reported(self, app_module, local_server, caplog):
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            requests.get(f"http://{local_server}/status/503")
            requests.get(f"http://{local_server}/status/401")
        assert len(messages(caplog, logging.WARNING)) == 2

    def test_a_failure_is_reported_again_after_the_quiet_period(self, app_module, local_server, caplog, monkeypatch):
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            requests.get(f"http://{local_server}/status/503")
            monkeypatch.setattr(app_module, "LOG_REPEAT_SECONDS", 0)
            requests.get(f"http://{local_server}/status/503")
        assert len(messages(caplog, logging.WARNING)) == 2

    def test_installing_twice_does_not_log_twice(self, app_module, local_server, caplog):
        app_module._install_request_logging()
        app_module._install_request_logging()
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            requests.get(f"http://{local_server}/status/500")
        assert len(messages(caplog, logging.WARNING)) == 1

    def test_a_logging_fault_never_breaks_the_request(self, app_module, local_server, monkeypatch):
        def broken(*_a, **_k):
            raise RuntimeError("logging exploded")

        monkeypatch.setattr(app_module, "describe_request", broken)
        assert requests.get(f"http://{local_server}/ping").json() == {}

    def test_the_error_still_reaches_the_caller(self, app_module):
        with pytest.raises(requests.exceptions.ConnectionError):
            requests.get(f"http://127.0.0.1:{closed_port()}/x", timeout=3)


class TestSettingsTest:
    def test_a_failed_test_says_what_was_tested(self, app_module, admin_session, requests_mock, caplog):
        client, csrf = admin_session
        requests_mock.get("http://jf:8096/System/Info", status_code=401)
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            response = client.post("/api/settings/test", headers=HEADERS(csrf), json={
                "category": "media_server", "kind_or_type": "jellyfin",
                "url": "http://jf:8096", "credential": SECRET})
        assert response.get_json()["ok"] is False
        lines = messages(caplog, logging.WARNING)
        assert any("Settings test failed: media_server/jellyfin at jf:8096" in line for line in lines)
        assert SECRET not in " ".join(r.getMessage() for r in caplog.records)

    def test_a_good_test_logs_no_warning(self, app_module, admin_session, requests_mock, caplog):
        client, csrf = admin_session
        requests_mock.get("http://jf:8096/System/Info", json={})
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            response = client.post("/api/settings/test", headers=HEADERS(csrf), json={
                "category": "media_server", "kind_or_type": "jellyfin",
                "url": "http://jf:8096", "credential": SECRET})
        assert response.get_json() == {"ok": True}
        assert messages(caplog, logging.WARNING) == []

    def test_the_answer_is_unchanged(self, app_module, admin_session, requests_mock):
        client, csrf = admin_session
        requests_mock.get("http://jf:8096/System/Info", status_code=401)
        body = client.post("/api/settings/test", headers=HEADERS(csrf), json={
            "category": "media_server", "kind_or_type": "jellyfin",
            "url": "http://jf:8096", "credential": SECRET}).get_json()
        assert body["error"].startswith("HTTP 401: the server rejected this API key")

    def test_a_url_with_credentials_is_logged_without_them(self, app_module, admin_session, requests_mock, caplog):
        client, csrf = admin_session
        requests_mock.get(re.compile(r".*"), status_code=500)
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            client.post("/api/settings/test", headers=HEADERS(csrf), json={
                "category": "media_server", "kind_or_type": "jellyfin",
                "url": "http://me:hunter2@jf:8096", "credential": "k"})
        assert any("Settings test failed" in m for m in messages(caplog, logging.WARNING))
        assert "hunter2" not in " ".join(r.getMessage() for r in caplog.records)


class TestLogin:
    def _login(self, client, username, password):
        page = client.get("/login").data.decode()
        marker = 'name="csrf_token" value="'
        start = page.find(marker) + len(marker)
        token = page[start:page.find('"', start)]
        return client.post("/login", data={"username": username, "password": password, "csrf_token": token})

    def test_a_failed_login_for_a_real_user_names_them_but_never_the_password(self, app_module, admin_session, caplog):
        _, _ = admin_session
        other = app_module.app.test_client()
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            self._login(other, "testadmin", "wrong-password-xyz")
        text = " ".join(messages(caplog, logging.WARNING))
        assert "Failed login for user 'testadmin'" in text
        assert "wrong-password-xyz" not in text

    def test_an_unknown_name_is_not_logged_in_case_it_is_a_password(self, app_module, admin_session, caplog):
        other = app_module.app.test_client()
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            self._login(other, "my-actual-password-1", "x")
        text = " ".join(messages(caplog, logging.WARNING))
        assert "Failed login for an unknown user" in text
        assert "my-actual-password-1" not in text


class TestBackgroundCalls:
    def test_a_job_that_raises_is_logged_and_the_batch_survives(self, app_module, caplog):
        def boom():
            raise ValueError(f"bad value at http://x/api?apikey={SECRET}")

        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            results = app_module.run_concurrently({"a": boom, "b": lambda: 5})
        assert results["a"][0] is False
        assert results["b"] == (True, 5)
        lines = messages(caplog, logging.WARNING)
        assert any("Background call a failed: ValueError" in line for line in lines)
        assert SECRET not in " ".join(lines)


class TestConfig:
    def test_an_unreadable_config_is_reported(self, app_module, caplog, tmp_path):
        broken = tmp_path / "config.json"
        broken.write_text("{ not json")
        app_module.CONFIG_PATH = str(broken)
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            app_module.load_config()
        assert any("config.json could not be read" in line for line in messages(caplog, logging.ERROR))

    def test_a_first_run_with_no_config_is_not_an_error(self, app_module, caplog, tmp_path):
        app_module.CONFIG_PATH = str(tmp_path / "fresh" / "config.json")
        (tmp_path / "fresh").mkdir()
        with caplog.at_level(logging.DEBUG, logger="arrvantage"):
            app_module.load_config()
        assert messages(caplog, logging.WARNING) == []
