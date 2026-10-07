# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

WiFi transfer works on firmware 1.7 and 1.8. It was decoded from the vendor
app's Android HCI snoop log during a real "Quick Transfer", reproduced from a
Linux laptop, and verified byte for byte against BLE downloads. A field report
confirmed it on 1.7.

### Added

- **`wifi-transfer` downloads recordings over WiFi at about 1 MB/s** on
  firmware 1.7 and 1.8 (WiFi firmware V9). It raises the device's access
  point, joins it, requests each recording as a BLE transfer and switches it to
  WiFi with `APP&U&WIFI`, then reads the raw MP3 from TCP
  `192.168.200.1:8475`. One recording with `--date`/`--timestamp`; otherwise
  every recording not yet downloaded (`--since`, `--output-dir`,
  `--overwrite`). The device serves a limited number of transfer connections
  per AP session (two on 1.8, one on 1.7), so the AP is restarted in between.
  A failed file (a reset connection, a full disk, no data) counts as one
  failure, and the next file gets a fresh AP. If the BLE link drops, the run
  stops and still reports what was saved. It refuses other firmware without
  `--force` (and then restarts the AP for every file), and a battery under
  10%.
- `hostwifi`: joins the device's hidden network and switches back afterwards,
  with NetworkManager (Linux), netsh (Windows), or by hand (`--wifi manual`,
  for macOS). It never re-issues a connect while one is in progress, because
  that drops the transfer socket.
- `wifi.WifiSession`, `wifi.receive_file` and `wifi.open_transfer_socket`, the
  transfer as a library.
- `PocketCommander.messages`, `mark()`, `wait_for_message()`, `request()` and
  `send_nowait()`, for replies that arrive on their own schedule (`WIFIS`
  changes, `MCU&U&WIFI`, `MCU&OFF`). Also `start_audio_sink()`: a BLE transfer
  only runs while the audio characteristic is subscribed.
- `protocol.TRANSFER_PORT`, `END_MARKER`, `FILES_PER_AP_SESSION` (by
  firmware), and correctly named WiFi status codes.
- PROTOCOL.md documents the WiFi transfer:
  - the vendor app's exact sequence and the client protocol;
  - the 10-byte end marker and the two-connections-per-AP limit;
  - `MCU&SHUT` when switching with no connection open;
  - BLE transfer details measured on 1.8 (notification sizes, `MCU&OFF`, the
    required audio subscription, about 26–65 KB/s);
  - more commands (`MAC`, `GET&USB`, `WPING`) and the app's connect sequence.
- **USB mass storage control.** `pocket-libre usb on|off|status` switches the
  device's USB drive mode over BLE (`APP&USB&<0|1>`, read back with
  `APP&GET&USB`), decoded from an HCI snoop capture of the vendor app.
- PROTOCOL.md documents recording names of the form `PH` + `YYMMDDHHmmss`
  (e.g. `PH261002211958`), seen alongside the usual `YYYYMMDDHHmmss` names.
  They already worked; a test now keeps the listing parser from dropping them.

- **`delete` removes recordings from the device** with `APP&D&<date>&<timestamp>`.
  It deletes one recording (`--date`/`--timestamp`), or with `--downloaded` every
  recording that has a verified copy in the output directory. It asks first
  unless you pass `--yes`. `sync`, `download-all` and `wifi-transfer` take
  `--delete-after` to do the same once they finish. A copy counts as verified
  only if its size matches, to the byte, the size its download recorded in
  `<date>/.downloads.json`, and the device still lists the recording with the
  duration it had then, so a recording that grew after it was downloaded, or a
  name reused for a new recording, is kept. That duration is checked again in
  the listing made right before each deletion, so a change while the
  confirmation is open is caught too. `download-all`, `sync`,
  `wifi-transfer` and `watch` record a download only when the transfer was
  exactly the size the device announced; a transfer without an announced size
  is tried again. One that still can't be checked is saved but marked
  unverified, and the next run downloads it again. Recordings are written
  through a `.part` file and marked unverified until they are recorded, so a
  crash or full disk part way leaves a copy that is downloaded again. Copies
  without any record, such as ones downloaded by earlier versions, by the web
  UI or by `download`, are kept on the device and not downloaded again. The
  record has the usual file mode, so a `watch` service and a user's `delete`
  can share it. Each deletion is confirmed by a complete listing of the date
  (one that ends in `MCU&LIST&<count>` with that many well-formed entries),
  because the device's `MCU&D` reply carries no status; without one, the
  deletion is reported as unconfirmed. If the device can't be reached for
  deleting, the run says so and how to delete later. Also
  `PocketCommander.delete()`, `PocketCommander.delete_and_list()`,
  `PocketCommander.list_files_complete()`, `commands.download_checked()`,
  `commands.has_complete_copy()`, `commands.needs_download()` and
  `commands.save_recording()`.

### Changed

- **WiFi status codes were mislabelled.** `MCU&WIFIS&3` is "AP coming up" and
  `2` is "waiting for a client", not the other way round. `1` means a client
  has joined. `WIFI_STATUS_STARTING` is now 3, and `WIFI_STATUS_WAITING_FOR_CLIENT`
  and `WIFI_STATUS_CLIENT_JOINED` were added. `WIFI_STATUS_CONNECTING` is
  gone.
- `wifi-discover` is now a diagnostic for other firmware; it names 8475 when
  it finds it.
- `PocketCommander` splits notifications that carry several `MCU&` replies
  (e.g. `MCU&WIFIOMCU&OFF`).
- PROTOCOL.md corrects earlier guesses: `RANGE` is the BLE byte-range
  download, not the WiFi verb; `APP&WIFI&SWITCH` isn't implemented on 1.8; and
  staging a file before `APP&WIFIO` isn't required.

### Removed

- The HTTP escape hatch: `wifi-transfer --url`, the `wifi.url_template`
  setting, `wifi.download_file()` and `wifi.build_url()`. No firmware serves
  files over HTTP.

### Fixed

- **`wifi-transfer` recovers from two failures seen on firmware 1.8.** When
  the device resets or closes a transfer connection before sending any of the
  file, the file is tried once more on a fresh access point; any other failure
  (part way through, no data, a full disk, a failed join) still fails the file.
  When the device does not answer `APP&WIFIO`, raising the access point is
  tried again with longer pauses (2, 5, 10 s), at the start as well as between
  files; if the third try fails between files, the run stops instead of
  repeating it for every remaining file. In a 12-file field run, these two
  accounted for all 4 failed files.

- **`download-all` counted skipped recordings as downloaded** and exited 0 when
  a download failed. It now reports "N downloaded, M failed" for the files it
  tried and exits 1 if any failed, or if authentication fails. A file it can't
  write (a full disk, permissions) counts as one failure, leaves no partial
  `.mp3`, and the remaining files are still tried.
- `PocketCommander.list_all_recordings()` returns recordings oldest first,
  whatever order the device lists them in, so `download-all`, `wifi-transfer`,
  `sync`, `watch` and the web UI all go in date order. `PH…` names (phone
  calls) sort by the time they carry, among the other recordings of their date
  (`Recording.sort_key`).
- `download-all` and `wifi-transfer` print one line for recordings already
  downloaded ("2 recording(s) to download (5 already downloaded)", or "N
  recording(s) to consider, all already downloaded") instead of one line per
  skipped file. They say "No recordings on the device." for an empty device,
  and "No recordings from <date> on (N on the device)." when `--since` leaves
  nothing.
- **`status` reported a nearly full device as nearly empty, and vice versa.**
  `APP&SPACE` answers with *free* and total storage in **MB**
  (`MCU&SPA&<free>&<total>`), not used and total in KB. A brand-new 64 GB
  device showed `59,619 / 59,634 KB (99% used)`; it now shows
  `15 / 59,634 MB (0% used)`. `get_storage()` still returns
  `(used, total)`, now in MB, and the web API's `storage_used_kb` /
  `storage_total_kb` are now `storage_used_mb` / `storage_total_mb`.
  Reported on firmware 1.7 in
  [#11](https://github.com/shahcolate/pocket-libre/issues/11).
- **`status` and the web UI set the device clock to local time.** The
  vendor app sets it to UTC, so the clock flipped between the two depending on
  which connected last, and recording names (which follow the clock) could be
  hours off. `set_time()` now sends UTC, and takes a `datetime` instead of a
  string. If you have only used pocket-libre so far, your recordings are named
  in local time: from the first time pocket-libre sets the clock after
  upgrading (`status`, or the web UI's status view), new ones are named in UTC,
  so names and date folders jump by your UTC offset at that point. Older
  recordings keep their names. `--since` compares against those folder dates.
- **A BLE disconnect mid-download crashed `download-all`** with `BleakError:
  Service Discovery has not been performed yet`, raised while unsubscribing
  from a link that was already gone. Without the crash it was worse: the
  partial data was saved under the final name, and later runs skipped it as
  "already exists".
- **`download` and `download-all` now retry dropped transfers**, as `sync`
  and `watch` already did, opening a fresh connection for each recording.
  The README already said `download` retried; now it does.
- **A transfer shorter than the size the device announces is treated as
  failed** and retried, instead of being kept. Previously `sync` and
  `watch` only rejected transfers under half the size estimated from
  duration, and `download` checked nothing.

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
