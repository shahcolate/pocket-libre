"""WiFi endpoint discovery and HTTP transfer, against a local stub server."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pocket_libre.wifi import (
    build_url,
    discover_endpoint,
    download_file,
    is_host_reachable,
    probe_url,
)

MP3_BODY = b"\xff\xf3\x48\xc4" + b"audio-payload" * 400


def _make_server(routes: dict[str, tuple[int, str, bytes]]):
    """Serve a fixed routing table on an ephemeral port."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            status, ctype, body = routes.get(self.path, (404, "text/plain", b"nope"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def server():
    srv = _make_server({
        "/20260328001919.mp3": (200, "audio/mpeg", MP3_BODY),
        "/index.html": (200, "text/html", b"<html>hi</html>"),
        "/empty": (200, "text/plain", b""),
    })
    yield srv
    srv.shutdown()


@pytest.fixture
def port(server):
    return server.server_address[1]


# ── URL building ────────────────────────────────


def test_build_url_substitutes_filename():
    url = build_url("/{filename}", "192.168.4.1", 80, "2026-03-28", "20260328001919")
    assert url == "http://192.168.4.1/20260328001919.mp3"


def test_build_url_substitutes_date_and_timestamp():
    url = build_url(
        "/sd/{date}/{timestamp}.mp3", "192.168.4.1", 80, "2026-03-28", "20260328001919"
    )
    assert url == "http://192.168.4.1/sd/2026-03-28/20260328001919.mp3"


def test_build_url_omits_default_port():
    assert ":80" not in build_url("/{filename}", "h", 80, "d", "t")


def test_build_url_includes_nonstandard_port():
    assert "h:8080" in build_url("/{filename}", "h", 8080, "d", "t")


# ── Reachability ────────────────────────────────


def test_reachable_port_detected(port):
    assert is_host_reachable("127.0.0.1", port, timeout=2.0)


def test_closed_port_not_reachable():
    # Port 1 is reserved and will not be listening.
    assert not is_host_reachable("127.0.0.1", 1, timeout=0.5)


# ── Probing ─────────────────────────────────────


def test_probe_identifies_mp3(port):
    probe = probe_url(f"http://127.0.0.1:{port}/20260328001919.mp3")
    assert probe.status == 200
    assert probe.looks_like_mp3
    assert probe.promising


def test_probe_html_is_not_mp3(port):
    probe = probe_url(f"http://127.0.0.1:{port}/index.html")
    assert probe.status == 200
    assert not probe.looks_like_mp3


def test_probe_404_is_not_promising(port):
    probe = probe_url(f"http://127.0.0.1:{port}/missing")
    assert probe.status == 404
    assert not probe.promising


def test_probe_empty_body_is_not_promising(port):
    assert not probe_url(f"http://127.0.0.1:{port}/empty").promising


def test_probe_unreachable_records_error():
    probe = probe_url("http://127.0.0.1:1/whatever", timeout=0.5)
    assert probe.error
    assert not probe.promising


# ── Discovery ───────────────────────────────────


def test_discovery_finds_the_mp3_endpoint(port):
    report = discover_endpoint(
        date="2026-03-28", timestamp="20260328001919",
        hosts=["127.0.0.1"], ports=[port],
    )
    assert report.endpoint == f"http://127.0.0.1:{port}/20260328001919.mp3"


def test_discovery_reports_reachable_hosts(port):
    report = discover_endpoint(
        "2026-03-28", "20260328001919", hosts=["127.0.0.1"], ports=[port]
    )
    assert "127.0.0.1" in report.reachable_hosts


def test_discovery_returns_report_when_nothing_listens():
    report = discover_endpoint(
        "2026-03-28", "20260328001919", hosts=["127.0.0.1"], ports=[1]
    )
    assert report.endpoint is None
    assert report.reachable_hosts == []


def test_discovery_finds_nothing_for_unknown_recording(port):
    """A recording the server doesn't have must not yield a false positive."""
    report = discover_endpoint(
        "2026-01-01", "99999999999999", hosts=["127.0.0.1"], ports=[port]
    )
    assert report.endpoint is None


# ── Download ────────────────────────────────────


def test_download_writes_file(tmp_path, port):
    out = tmp_path / "rec.mp3"
    written = download_file(f"http://127.0.0.1:{port}/20260328001919.mp3", out)
    assert written == len(MP3_BODY)
    assert out.read_bytes() == MP3_BODY


def test_download_reports_progress(tmp_path, port):
    seen = []
    download_file(
        f"http://127.0.0.1:{port}/20260328001919.mp3",
        tmp_path / "rec.mp3",
        progress_callback=lambda cur, tot: seen.append(cur),
    )
    assert seen and seen[-1] == len(MP3_BODY)


def test_download_failure_leaves_no_file(tmp_path):
    out = tmp_path / "rec.mp3"
    assert download_file("http://127.0.0.1:1/nope", out, timeout=0.5) == 0
    assert not out.exists()


def test_download_leaves_no_partial_file(tmp_path):
    out = tmp_path / "rec.mp3"
    download_file("http://127.0.0.1:1/nope", out, timeout=0.5)
    assert list(tmp_path.iterdir()) == []


def test_short_transfer_is_rejected(tmp_path, port):
    """A truncated download must not leave a file later runs treat as complete."""
    out = tmp_path / "rec.mp3"
    written = download_file(
        f"http://127.0.0.1:{port}/20260328001919.mp3", out,
        expected_size=len(MP3_BODY) * 10,
    )
    assert written == 0
    assert not out.exists()


def test_download_creates_parent_directories(tmp_path, port):
    out = tmp_path / "a" / "b" / "rec.mp3"
    assert download_file(f"http://127.0.0.1:{port}/20260328001919.mp3", out) > 0
    assert out.exists()
