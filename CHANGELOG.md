# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-09-19

Acts on a firmware 1.8 field report
([#4](https://github.com/shahcolate/pocket-libre/issues/4)) from
[@jmillerhyetech](https://github.com/jmillerhyetech), which found that the
WiFi transfer path was built on an assumption that was never true and could
leave a device needing a physical power-cycle.

### Added

- A **firmware compatibility table** in the README. Everything except WiFi
  transfer works on both 1.3.3 and 1.8; WiFi transfer works on neither.
- A **troubleshooting section**, including how to recover a device that has
  stopped responding over BLE after a WiFi command.
- `wifi-discover` reports when a sweep was **incomplete** rather than passing
  it off as a clean negative.

### Fixed

- **The WiFi handshake used the wrong order on firmware 1.8.** The file is now
  staged *before* the access point is raised, and the credentials are shown
  before the AP goes up so the join can happen while status polling runs — the
  AP window is only seconds wide. The documented order never broadcast an SSID
  on 1.8 and tore down BLE a few seconds later, leaving the device recoverable
  only by a physical power-cycle.
- **`wifi-discover` probed for an HTTP server that does not exist.** The device
  serves files over a framed socket protocol using a `RANGE` verb, not HTTP.
  The command is now a port sweep that reports what is actually listening.
- **`wifi-discover` reported home routers as device hits.** `192.168.1.1` and
  `10.0.0.1` were in the default candidate list, so running the command off the
  device network confidently returned a router login page. Probing is now
  gated on holding an address inside the device's `192.168.200.0/24`, and the
  colliding hosts are gone.
- **The default AP host was wrong.** It is `192.168.200.1`, not `192.168.4.1`.
- **Recording durations and sizes were ~4x wrong.** The trailing field of a
  `MCU&F` listing row is a duration in **seconds**, not a size in kilobytes.
  The two code paths that read it disagreed with each other; both are fixed,
  and size is now derived from duration at 32 kbps.

### Changed

- **`wifi-transfer` now requires `--url` and refuses to run without one.**
  Raising the access point can strand the device, and no endpoint is known, so
  there is nothing to gain by staging a transfer that cannot complete. It also
  warns and asks for confirmation before touching the device.
- Session-key capture instructions now lead with `adb logcat`, which needs no
  packet capture. The old bug-report route does not work: its snoop log
  truncates ACL packets to 15 bytes.
- PROTOCOL.md now marks the WiFi transfer section as partially decoded, gives
  both the 1.3.3 and 1.8 sequences, and states plainly that the old
  "serves over HTTP at 192.168.4.1" claim was inference from a BLE-only
  capture rather than an observation.
- SECURITY.md documents that the session key is an account identifier which
  the vendor app writes to the Android system log and sends to a third-party
  analytics endpoint, and that its first 8 characters are the device's WiFi AP
  password.

### Breaking

- `Recording.size_kb` is now `Recording.duration_s`, with a new
  `estimated_bytes` property. The web API's recording rows expose `duration_s`
  and `estimated_bytes` in place of `size_kb`.
- `wifi.discover_endpoint()` is replaced by `wifi.diagnose()` and
  `wifi.scan_ports()`.

## [1.0.0] — 2026-08-05

First stable release. Everything in the core workflow — capture, transcribe,
diarize, summarize — is implemented and covered by tests.

### Security

All of the below were found in a pre-release audit of `0.1.0`. See
[SECURITY.md](SECURITY.md) for details and the project's threat model.

- Fixed a path traversal in the web API. `date` and `timestamp` path parameters
  were interpolated into filesystem paths; since path parameters are
  percent-decoded after routing, `%2e%2e` arrived as `..` and escaped the output
  directory — affecting both the read endpoints and the download write path.
- Fixed stored XSS in the web UI. Summaries, analyses, and chat responses were
  rendered as raw HTML, so markup in a transcript (reachable via prompt
  injection in recorded audio) executed as script.
- Fixed API keys and session keys shorter than nine characters being returned in
  cleartext from `/api/config`.
- Config file is now written mode `600`; it holds API keys and the device
  session key.
- Recording identifiers received from the device are now validated before being
  used as path components.

### Added

- `pocket-libre watch` — background auto-sync. Polls for the device and pulls
  new recordings whenever it comes in range, backing off to five-minute checks
  while it is away. Optional `--process` transcribes and summarizes as it goes.
- `pocket-libre wifi-transfer` — WiFi bulk transfer, replacing the removed stub.
  Drives the full BLE handshake, prompts you to join the device AP, and streams
  the file over HTTP. See "Known limitations" below.
- `pocket-libre wifi-discover` — probes the device's access point across
  candidate hosts, ports, and path templates to locate its file-serving HTTP
  endpoint, and prints a report suitable for filing as an issue.
- `PocketCommander.wifi_trigger()` and `wifi_enable()`, so callers can read
  WiFi credentials between the two steps — the order the vendor app uses.
  `wifi_start()` still bundles both for callers that don't need to.
- Test suite: 144 tests covering protocol parsing, config resolution, path
  safety, secret masking, transcript and diarization logic, WiFi discovery and
  transfer, and the watch loop. Runs in ~6 seconds with no device, no network,
  and no model downloads.
- GitHub Actions CI: lint plus tests on Python 3.10 through 3.13, and a
  packaging job that builds and validates the distribution.
- `CONTRIBUTING.md`, `SECURITY.md`, and issue templates.

### Changed

- **Cost reporting is now accurate.** Every estimate was computed at Haiku 3
  rates ($0.25/$1.25 per MTok) while the code called Haiku 4.5 ($1/$5), so
  reported costs were roughly 4× too low. Model ID and prices now live in
  `pocket_libre.pricing` and are used everywhere. The README's per-recording
  estimate is corrected from ~$0.001 to ~$0.02.
- `pocket-libre web` warns when bound to a non-loopback address, since the
  interface has no authentication, and opens a reachable URL instead of a
  literal `0.0.0.0` one.
- README rewritten: accurate command list, session-key capture instructions,
  honest cost table, and an explicit note about the unauthenticated web UI.

### Fixed

- `pocket-libre convert` crashed with `AttributeError` on click 8.1.x — inside
  the declared `click>=8.1.0` range — because `click.Choice` was given integer
  values. It failed even when `--bit-depth` was omitted, since click converts
  defaults too.
- Config values containing newlines, tabs, or other control characters produced
  an unparseable TOML file, silently wiping every setting on the next load.
- An interrupted WiFi transfer no longer leaves a truncated `.mp3` that later
  runs would mistake for a completed download.
- Removed a dead `full_text` computation in the sync pipeline and several unused
  imports.

### Known limitations

- **The WiFi HTTP endpoint is unconfirmed.** The BLE half of WiFi transfer is
  fully decoded, but the path the device serves files from has not been verified
  against hardware. `wifi-transfer` probes for it and `wifi-discover` exists to
  pin it down — if you own a Pocket, running it and reporting the result is the
  single most useful contribution available.
- The web interface has no authentication or CSRF protection. It is a
  single-user tool; keep it on loopback.
- Verified against one device: `PKT01`, firmware 1.3.3.

[Unreleased]: https://github.com/shahcolate/pocket-libre/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/shahcolate/pocket-libre/releases/tag/v1.0.0
