# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
