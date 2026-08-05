# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub's private vulnerability reporting](https://github.com/shahcolate/pocket-libre/security/advisories/new)
rather than opening a public issue.

Include what you were running, what happened, and how to reproduce it. Expect an
initial response within a week.

## Threat model

Pocket Libre handles recorded conversations, so it is worth being explicit about
what it does and does not defend against.

**Secrets on disk.** `~/.pocket-libre/config.toml` holds your Anthropic API key,
HuggingFace token, and the device session key in plaintext, written mode `600`.
Anyone who can read your home directory as your user can read them. The session
key also doubles as the device's WiFi AP password.

**The web interface has no authentication.** It binds to `127.0.0.1` by default,
which is the intended configuration. Running `pocket-libre web --host 0.0.0.0`
exposes to the whole network: every transcript and summary, control of your
device over BLE, and your Anthropic API credits. The command prints a warning
when you do this. There is no CSRF protection, so a malicious page in your
browser can trigger side-effecting requests against localhost — treat the web UI
as a single-user tool on a machine you trust.

**Recorded audio is untrusted input.** Transcripts come from Whisper, and
summaries from a language model reading those transcripts. Anything said near
the device — deliberately or not — flows into the web UI. Model output is
escaped before rendering, but treat prompt injection via recorded speech as a
live concern if you display summaries anywhere else.

**The device is untrusted input.** Recording identifiers from BLE responses
become filenames, so they are validated before use. A spoofed or malfunctioning
peer cannot steer writes outside the output directory.

**Not in scope:** a compromised local machine, a malicious Anthropic or
HuggingFace API endpoint, physical access to an unlocked device, or someone in
BLE range with your session key.

## Fixed in 1.0.0

The 1.0.0 release fixed several issues found in a pre-release audit. All were in
unreleased or `0.1.0` code; no advisories were issued.

- **Path traversal in the web API.** `date` and `timestamp` path parameters were
  interpolated into filesystem paths. Because path parameters are
  percent-decoded after routing, `%2e%2e` reached handlers as `..` and escaped
  the output directory — on read endpoints and the download write path alike.
  Components are now validated and containment is re-checked on the resolved
  path.
- **Stored XSS in the web UI.** Summaries, analyses, and chat responses were
  rendered as raw HTML. A transcript containing markup — reachable via prompt
  injection in recorded audio — executed script in the page. Content is now
  escaped before rendering.
- **Short API keys returned in cleartext.** `/api/config` only masked secrets
  longer than eight characters. All secrets are now masked regardless of length.
- **Config file written world-readable.** Now written mode `600`.
- **Unvalidated device filenames.** Recording identifiers from the device were
  used as path components without checking for separators or dot-segments.
