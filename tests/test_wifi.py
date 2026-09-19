"""WiFi AP diagnostics, subnet guards, and the HTTP escape hatch.

Note what these tests can and cannot show. The port-scan and subnet-guard
tests exercise real logic. The HTTP tests run against a local stub server,
and no Pocket firmware has been observed serving files over HTTP — they
cover the `--url` escape hatch, not the device. Passing them says nothing
about whether WiFi transfer works on hardware.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pocket_libre.wifi import (
    AP_SUBNET_PREFIX,
    CANDIDATE_HOSTS,
    DEFAULT_HOST,
    build_url,
    diagnose,
    download_file,
    is_host_reachable,
    on_ap_subnet,
    scan_ports,
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


# ── Host defaults ───────────────────────────────


def test_default_host_is_the_device_gateway():
    """Firmware 1.8 DHCP hands out 192.168.200.1, not the ESP32 default."""
    assert DEFAULT_HOST == "192.168.200.1"


def test_candidate_hosts_exclude_common_home_gateways():
    """Regression: probing these reported people's own routers as hits.

    See https://github.com/shahcolate/pocket-libre/issues/4 section 8.
    """
    assert "192.168.1.1" not in CANDIDATE_HOSTS
    assert "10.0.0.1" not in CANDIDATE_HOSTS


# ── Port scanning ───────────────────────────────


def test_scan_finds_a_listening_port(port):
    result = scan_ports("127.0.0.1", ports=[port], timeout=2.0)
    assert result.open_ports == [port]
    assert result.scanned == 1


def test_scan_reports_nothing_when_closed():
    result = scan_ports("127.0.0.1", ports=[1], timeout=0.5)
    assert result.open_ports == []
    assert result.scanned == 1


def test_scan_returns_sorted_ports(port):
    result = scan_ports("127.0.0.1", ports=[port, 1], timeout=1.0)
    assert result.open_ports == sorted(result.open_ports)


# ── Subnet guard ────────────────────────────────


def test_on_ap_subnet_true_inside_the_device_range(monkeypatch):
    monkeypatch.setattr(
        "pocket_libre.wifi.local_address_for", lambda host=DEFAULT_HOST: "192.168.200.2"
    )
    joined, address = on_ap_subnet()
    assert joined
    assert address == "192.168.200.2"


def test_on_ap_subnet_false_on_a_home_lan(monkeypatch):
    """A routable 192.168.200.1 through double-NAT is not the device."""
    monkeypatch.setattr(
        "pocket_libre.wifi.local_address_for", lambda host=DEFAULT_HOST: "192.168.1.14"
    )
    joined, address = on_ap_subnet()
    assert not joined
    assert address == "192.168.1.14"


def test_on_ap_subnet_false_with_no_route(monkeypatch):
    monkeypatch.setattr(
        "pocket_libre.wifi.local_address_for", lambda host=DEFAULT_HOST: None
    )
    joined, address = on_ap_subnet()
    assert not joined
    assert address is None


def test_ap_subnet_prefix_matches_default_host():
    assert DEFAULT_HOST.startswith(AP_SUBNET_PREFIX)


# ── Diagnose ────────────────────────────────────


def test_diagnose_refuses_to_sweep_off_subnet(monkeypatch, port):
    """The guard that stops us probing a home router."""
    monkeypatch.setattr(
        "pocket_libre.wifi.local_address_for", lambda host=DEFAULT_HOST: "192.168.1.14"
    )
    report = diagnose(host="127.0.0.1", ports=[port])
    assert report.scan is None
    assert not report.on_ap_subnet


def test_diagnose_sweeps_when_forced(monkeypatch, port):
    monkeypatch.setattr(
        "pocket_libre.wifi.local_address_for", lambda host=DEFAULT_HOST: "192.168.1.14"
    )
    report = diagnose(host="127.0.0.1", ports=[port], require_ap_subnet=False)
    assert report.scan is not None
    assert report.scan.open_ports == [port]


def test_diagnose_sweeps_when_on_subnet(monkeypatch, port):
    monkeypatch.setattr(
        "pocket_libre.wifi.local_address_for", lambda host=DEFAULT_HOST: "192.168.200.2"
    )
    report = diagnose(host="127.0.0.1", ports=[port])
    assert report.on_ap_subnet
    assert report.scan.open_ports == [port]


def test_diagnose_reports_empty_sweep_without_claiming_failure(monkeypatch):
    """A confirmed negative is a result, not an error."""
    monkeypatch.setattr(
        "pocket_libre.wifi.local_address_for", lambda host=DEFAULT_HOST: "192.168.200.2"
    )
    report = diagnose(host="127.0.0.1", ports=[1])
    assert report.scan is not None
    assert report.scan.open_ports == []
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


# ── Bare-path templates ─────────────────────────


def test_bare_path_template_resolves_against_default_host():
    """A path-only template must not produce a hostless `http:///...` URL."""
    from pocket_libre.wifi import DEFAULT_HOST

    url = build_url("/{filename}", DEFAULT_HOST, 80, "2026-03-28", "20260328001919")
    assert url.startswith(f"http://{DEFAULT_HOST}/")
    assert "http:///" not in url
