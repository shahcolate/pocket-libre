"""Background watch loop: presence polling, backoff, and error tolerance."""

import pytest

from pocket_libre.watch import (
    MAX_BACKOFF_SECONDS,
    next_backoff,
    watch_loop,
    watch_many,
)


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


# ── Several recorders at once ───────────────────


def _target(name, address, counts, calls=None):
    """A WatchTarget whose sync returns the next value from `counts`."""
    from pocket_libre.watch import WatchTarget

    queue = list(counts)

    async def sync_once():
        if calls is not None:
            calls.append(name)
        return queue.pop(0) if queue else 0

    return WatchTarget(name=name, address=address, sync_once=sync_once)


@pytest.mark.asyncio
async def test_watch_many_syncs_each_present_device(recorder):
    _slept, sleep = recorder
    calls: list[str] = []
    targets = [
        _target("hers", "AA:01", [2], calls),
        _target("mine", "AA:02", [1], calls),
    ]

    async def present(_address):
        return True

    stats = await watch_many(targets, poll_interval=60.0, presence_check=present,
                             max_iterations=1, sleep=sleep)
    assert calls == ["hers", "mine"]
    assert stats["hers"].recordings_synced == 2
    assert stats["mine"].recordings_synced == 1


@pytest.mark.asyncio
async def test_watch_many_never_syncs_two_devices_at_once(recorder):
    """One BLE connection per device and one adapter: strictly sequential."""
    _slept, sleep = recorder
    in_flight = 0
    overlaps = 0

    from pocket_libre.watch import WatchTarget

    async def sync_once():
        nonlocal in_flight, overlaps
        in_flight += 1
        if in_flight > 1:
            overlaps += 1
        import asyncio as _asyncio
        await _asyncio.sleep(0)
        in_flight -= 1
        return 1

    targets = [
        WatchTarget("hers", "AA:01", sync_once),
        WatchTarget("mine", "AA:02", sync_once),
    ]

    async def present(_address):
        return True

    await watch_many(targets, poll_interval=60.0, presence_check=present,
                     max_iterations=3, sleep=sleep)
    assert overlaps == 0


@pytest.mark.asyncio
async def test_absent_device_does_not_slow_the_present_one(recorder):
    """Each target backs off on its own."""
    _slept, sleep = recorder
    calls: list[str] = []
    targets = [
        _target("here", "AA:01", [1, 1, 1, 1, 1], calls),
        _target("away", "AA:02", [], calls),
    ]

    async def present(address):
        return address == "AA:01"

    stats = await watch_many(targets, poll_interval=60.0, presence_check=present,
                             max_iterations=6, sleep=sleep)
    # The present device is scanned every interval; the absent one doubles its
    # wait each miss, so it is scanned far less often.
    assert stats["here"].scans > stats["away"].scans
    assert calls and set(calls) == {"here"}


@pytest.mark.asyncio
async def test_watch_many_survives_a_failing_sync(recorder):
    _slept, sleep = recorder

    from pocket_libre.watch import WatchTarget

    async def boom():
        raise RuntimeError("BLE dropped")

    targets = [WatchTarget("hers", "AA:01", boom)]

    async def present(_address):
        return True

    stats = await watch_many(targets, poll_interval=60.0, presence_check=present,
                             max_iterations=2, sleep=sleep)
    assert stats["hers"].failures == 2
    assert stats["hers"].recordings_synced == 0


@pytest.mark.asyncio
async def test_watch_many_with_no_targets_returns_immediately(recorder):
    _slept, sleep = recorder
    assert await watch_many([], sleep=sleep) == {}
