"""Background watch loop: presence polling, backoff, and error tolerance."""

import pytest

from pocket_libre.watch import MAX_BACKOFF_SECONDS, next_backoff, watch_loop


@pytest.fixture
def recorder():
    """Captures the durations the loop sleeps for instead of waiting."""
    slept: list[float] = []

    async def sleep(seconds: float):
        slept.append(seconds)

    return slept, sleep


# ── Backoff ─────────────────────────────────────


def test_backoff_starts_at_base():
    assert next_backoff(0, 60.0) == 60.0


def test_backoff_doubles():
    assert next_backoff(60.0, 60.0) == 120.0


def test_backoff_is_capped():
    assert next_backoff(MAX_BACKOFF_SECONDS, 60.0) == MAX_BACKOFF_SECONDS


def test_backoff_never_exceeds_ceiling():
    current = 60.0
    for _ in range(20):
        current = next_backoff(current, 60.0)
    assert current <= MAX_BACKOFF_SECONDS


# ── Loop behaviour ──────────────────────────────


async def test_syncs_when_device_present(recorder):
    slept, sleep = recorder
    calls = []

    async def sync():
        calls.append(1)
        return 2

    stats = await watch_loop(
        "AA:BB", sync, poll_interval=10.0,
        presence_check=lambda addr: _true(), max_iterations=3, sleep=sleep,
    )
    assert len(calls) == 3
    assert stats.recordings_synced == 6
    assert stats.sync_runs == 3


async def test_does_not_sync_when_absent(recorder):
    slept, sleep = recorder
    calls = []

    async def sync():
        calls.append(1)
        return 0

    stats = await watch_loop(
        "AA:BB", sync, poll_interval=10.0,
        presence_check=lambda addr: _false(), max_iterations=3, sleep=sleep,
    )
    assert calls == []
    assert stats.sync_runs == 0
    assert stats.scans == 3


async def test_backs_off_while_absent(recorder):
    slept, sleep = recorder

    async def sync():
        return 0

    await watch_loop(
        "AA:BB", sync, poll_interval=10.0,
        presence_check=lambda addr: _false(), max_iterations=4, sleep=sleep,
    )
    assert slept == [10.0, 20.0, 40.0, 80.0]


async def test_backoff_resets_after_a_find(recorder):
    slept, sleep = recorder
    presence = iter([False, False, True, False])

    async def check(addr):
        return next(presence)

    async def sync():
        return 1

    await watch_loop(
        "AA:BB", sync, poll_interval=10.0,
        presence_check=check, max_iterations=4, sleep=sleep,
    )
    # Backoff grows while absent, resets on the find, then restarts at base.
    assert slept == [10.0, 20.0, 10.0, 10.0]


async def test_sync_failure_does_not_stop_the_loop(recorder):
    slept, sleep = recorder
    attempts = []

    async def sync():
        attempts.append(1)
        raise RuntimeError("device vanished mid-sync")

    stats = await watch_loop(
        "AA:BB", sync, poll_interval=5.0,
        presence_check=lambda addr: _true(), max_iterations=3, sleep=sleep,
    )
    assert len(attempts) == 3
    assert stats.failures == 3
    assert stats.recordings_synced == 0


async def test_counts_scans_across_mixed_results(recorder):
    slept, sleep = recorder
    presence = iter([True, False, True])

    async def check(addr):
        return next(presence)

    async def sync():
        return 1

    stats = await watch_loop(
        "AA:BB", sync, poll_interval=1.0,
        presence_check=check, max_iterations=3, sleep=sleep,
    )
    assert stats.scans == 3
    assert stats.sync_runs == 2
    assert stats.recordings_synced == 2


async def _true():
    return True


async def _false():
    return False
