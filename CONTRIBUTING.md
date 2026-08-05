# Contributing

Thanks for helping out. This project exists because people with the hardware
shared what they found.

## What's most useful

1. **The WiFi HTTP endpoint.** The BLE side of WiFi transfer is fully decoded,
   but nobody has confirmed which HTTP path the device serves files from. If you
   own a Pocket: join its WiFi AP and run `pocket-libre wifi-discover`, then
   paste the output into an issue. This unblocks fast transfers for everyone.
2. **Other firmware versions.** Run `pocket-libre explore` and share the output.
   The GATT map in `PROTOCOL.md` comes from a single device on firmware 1.3.3.
3. **Protocol corrections.** Capture with PacketLogger (macOS) or the Android
   HCI snoop log, and tell us where reality differs from `PROTOCOL.md`.

You don't need a device to help — bug fixes, tests, and documentation are all
welcome.

## Development setup

```bash
git clone https://github.com/shahcolate/pocket-libre.git
cd pocket-libre
python -m venv .venv && source .venv/bin/activate

# Full install, including Whisper and PyTorch
pip install -e ".[dev]"

# Or, to skip the ~2GB PyTorch download and just run the tests:
pip install --no-deps -e .
pip install -r requirements-test.txt
```

## Running tests

```bash
pytest              # the whole suite, ~6 seconds
pytest -v           # with test names
pytest tests/test_web.py
```

The suite runs without a Pocket device, without network access, and without
downloading Whisper models. BLE and HTTP are stubbed out. If you add a feature
that needs hardware, factor the logic so the hardware call is an injectable
seam — `watch_loop` in `src/pocket_libre/watch.py` is the pattern to copy.

## Linting

```bash
ruff check src tests
```

CI runs this on every push. `ruff format` is intentionally *not* enforced yet:
adopting it would rewrite most of the tree at once. If you want to take that on,
do it as its own PR that changes nothing else, and add the check to CI.

## Pull requests

- Add a test for behavior you fix or add. Tests that fail before your change and
  pass after are the most useful kind.
- Keep the diff focused — unrelated reformatting makes review harder.
- Update `PROTOCOL.md` if you learn something new about the device.
- Add an entry to `CHANGELOG.md` under "Unreleased".

## Project layout

```
src/pocket_libre/
  cli.py         Command-line entry point (click)
  commands.py    BLE command protocol — APP&/MCU& over GATT
  protocol.py    UUIDs, constants, audio format
  capture.py     Raw BLE notification capture
  transcribe.py  Local Whisper transcription
  diarize.py     Speaker identification (pyannote → Claude → fallback)
  summarize.py   Claude summarization
  analyze.py     Entity extraction, mind maps, chat
  pricing.py     Model ID and token costs — change prices here, once
  watch.py       Background auto-sync loop
  wifi.py        WiFi transfer client and endpoint discovery
  web/app.py     FastAPI backend
  web/static/    Single-page frontend
```

## Security

Found something exploitable? See [SECURITY.md](SECURITY.md). Please don't open a
public issue for it.
