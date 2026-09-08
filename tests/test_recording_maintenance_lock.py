"""Deterministic retained-viewer versus automatic-prune ordering tests."""

import asyncio
import gc
import importlib
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    media = importlib.import_module("custom_components.xsense.recordings_media")
    http = importlib.import_module("custom_components.xsense.http")
    entry = SimpleNamespace(entry_id="entry", options={}, data={})
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_get_entry=lambda _: entry,
            async_entries=lambda _: [entry],
        ),
    )

    async def execute(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = execute
    clip = {
        "entry_id": "entry",
        "serial": "CAM",
        "start": 1,
        "end": 2,
        "media_root": str(tmp_path),
        "source": "video_url",
        "playback_url": "https://example/clip.m3u8",
    }
    hass.data[media.DOMAIN] = {
        "_recording_indexes": {
            "entry": SimpleNamespace(
                _cache={
                    "cameras": [{"entry_id": "entry", "serial": "CAM", "clips": [clip]}]
                }
            )
        }
    }
    for module in (media, http):
        monkeypatch.setattr(
            module,
            "_recording_media_root_from_value",
            lambda value: Path(value or tmp_path),
        )
        monkeypatch.setattr(module, "_recording_cache_retained", lambda *_: True)
    monkeypatch.setattr(media, "_recording_media_root", lambda *_: tmp_path)
    monkeypatch.setattr(media, "_cache_policy_for_root", lambda *_: (7, 0))
    monkeypatch.setattr(http, "_recordings_runtime_available", lambda _: True)
    monkeypatch.setattr(http, "_recording_media_sync_enabled", lambda *_: False)
    monkeypatch.setattr(
        http, "async_recording_cache_suppressed", AsyncMock(return_value=False)
    )
    playlist = tmp_path / "hls/CAM_1_2/index.m3u8"
    segment = playlist.parent / "segment.ts"

    def write_cache():
        playlist.parent.mkdir(parents=True, exist_ok=True)
        playlist.write_text("#EXTM3U\n#EXTINF:2,\nsegment.ts\n#EXT-X-ENDLIST\n")
        segment.write_bytes(b"retained media")

    write_cache()
    prepared = asyncio.Event()

    class Source:
        calls = 0
        refill_prunes = 0

        async def _async_cached_media_ready(self, clip):
            return playlist.exists() and segment.exists()

        async def _async_cached_media_format(self, clip):
            return "hls"

        async def _async_cached_playback_url(self, clip):
            self.calls += 1
            # A cache hit can return while a competing prune already owns rootlock.
            if not playlist.exists():
                assert not media._recording_maintenance_lock(hass, tmp_path).locked()
                async with media._recording_cache_lock(hass, clip):
                    write_cache()
                    self.refill_prunes += 1
                    await media.async_prune_recording_caches(hass, entry_id="entry")
            prepared.set()
            return "/media/local/cache/index.m3u8"

        async def _async_hls_ready(self, clip):
            return playlist.exists() and segment.exists()

        async def _async_mp4_ready(self, path):
            return False

        async def _async_file_job(self, fn, *args):
            return fn(*args)

    source = Source()
    monkeypatch.setattr(http, "XSenseRecordingsMediaSource", lambda _: source)
    monkeypatch.setattr(http, "_hls_playback_fields_for_clip", lambda _: {})
    monkeypatch.setattr(http, "_hls_playback_response_headers", lambda _: {})
    view = http.XSenseRecordingsPanelPlaybackView(hass)
    monkeypatch.setattr(view, "_clip", AsyncMock(return_value=clip))
    request = SimpleNamespace(
        query={"serial": "CAM"}, transport=SimpleNamespace(is_closing=lambda: False)
    )
    return SimpleNamespace(
        media=media,
        http=http,
        hass=hass,
        root=tmp_path,
        clip=clip,
        source=source,
        view=view,
        request=request,
        playlist=playlist,
        segment=segment,
        prepared=prepared,
    )


@pytest.mark.asyncio
async def test_prune_wins_then_viewer_refills_outside_lock(runtime):
    r = runtime
    pruning, finish = asyncio.Event(), asyncio.Event()
    first = True

    async def execute(fn, *args):
        nonlocal first
        if fn is r.media._prune_owned_media_cache and first:
            first = False
            pruning.set()
            await finish.wait()
        return fn(*args)

    r.hass.async_add_executor_job = execute
    prune = asyncio.create_task(
        r.media.async_prune_recording_caches(r.hass, entry_id="entry")
    )
    await pruning.wait()
    viewer = asyncio.create_task(r.view.get(r.request, "entry", "1", "2"))
    await r.prepared.wait()
    assert not viewer.done()
    assert not r.hass.data[r.media.DOMAIN].get("_recording_hls_tokens")
    finish.set()
    result = await asyncio.wait_for(prune, 2)
    response = await asyncio.wait_for(viewer, 2)
    assert result["deleted_items"] == 1
    assert r.source.calls == 2
    assert r.source.refill_prunes == 1  # Nested automatic prune did not deadlock.
    assert response.status == 200
    assert r.playlist.exists() and r.segment.exists()
    token = response.headers["X-XSense-Playback-Token"]
    assert token in r.hass.data[r.media.DOMAIN]["_recording_hls_tokens"]


@pytest.mark.asyncio
async def test_viewer_wins_prune_waits_for_token_publication(runtime):
    r = runtime
    reading, finish = asyncio.Event(), asyncio.Event()

    async def read(fn, *args):
        reading.set()
        await finish.wait()
        return fn(*args)

    r.source._async_file_job = read
    viewer = asyncio.create_task(r.view.get(r.request, "entry", "1", "2"))
    await reading.wait()
    prune = asyncio.create_task(
        r.media.async_prune_recording_caches(r.hass, entry_id="entry")
    )
    await asyncio.sleep(0)
    assert not prune.done()
    finish.set()
    response = await asyncio.wait_for(viewer, 2)
    result = await asyncio.wait_for(prune, 2)
    assert result["deleted_items"] == 0
    assert result["skipped_active"] == 1
    assert response.status == 200 and r.segment.exists()


@pytest.mark.asyncio
async def test_second_viewer_and_prune_do_not_revoke_first_viewer(runtime):
    r = runtime
    first = await r.view.get(r.request, "entry", "1", "2")
    first_token = first.headers["X-XSense-Playback-Token"]
    prune = asyncio.create_task(
        r.media.async_prune_recording_caches(r.hass, entry_id="entry")
    )
    viewer = asyncio.create_task(r.view.get(r.request, "entry", "1", "2"))
    result, second = await asyncio.wait_for(asyncio.gather(prune, viewer), 2)
    second_token = second.headers["X-XSense-Playback-Token"]
    assert first_token != second_token
    assert {first_token, second_token} <= r.hass.data[r.media.DOMAIN][
        "_recording_hls_tokens"
    ].keys()
    assert result["deleted_items"] == 0
    assert r.segment.read_bytes() == b"retained media"


@pytest.mark.asyncio
async def test_cancelled_prune_keeps_mutex_until_worker_finishes(runtime):
    r = runtime
    pruning, finish = asyncio.Event(), asyncio.Event()
    first = True

    async def execute(fn, *args):
        nonlocal first
        if fn is r.media._prune_owned_media_cache and first:
            first = False
            pruning.set()
            await finish.wait()
        return fn(*args)

    r.hass.async_add_executor_job = execute
    prune = asyncio.create_task(
        r.media.async_prune_recording_caches(r.hass, entry_id="entry")
    )
    await pruning.wait()
    prune.cancel()
    await asyncio.sleep(0)
    prune.cancel()
    await asyncio.sleep(0)
    assert r.media._recording_maintenance_lock(r.hass, r.root).locked()
    viewer = asyncio.create_task(r.view.get(r.request, "entry", "1", "2"))
    await r.prepared.wait()
    assert not viewer.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await prune
    response = await asyncio.wait_for(viewer, 2)
    assert response.status == 200 and r.segment.exists()
    assert not r.media._recording_maintenance_lock(r.hass, r.root).locked()


@pytest.mark.asyncio
async def test_cancelled_viewer_releases_mutex_and_owned_token(runtime):
    r = runtime
    reading = asyncio.Event()

    async def read(fn, *args):
        reading.set()
        await asyncio.Future()

    r.source._async_file_job = read
    viewer = asyncio.create_task(r.view.get(r.request, "entry", "1", "2"))
    await reading.wait()
    viewer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await viewer
    assert not r.hass.data[r.media.DOMAIN]["_recording_hls_tokens"]
    result = await asyncio.wait_for(
        r.media.async_prune_recording_caches(r.hass, entry_id="entry"), 2
    )
    assert result["deleted_items"] == 1


def test_resolved_root_aliases_share_maintenance_mutex(runtime):
    r = runtime
    alias = r.root / "alias"
    alias.symlink_to(r.root)
    assert r.media._recording_maintenance_lock(
        r.hass, alias
    ) is r.media._recording_maintenance_lock(r.hass, r.root)


def test_idle_maintenance_roots_do_not_accumulate(runtime):
    r = runtime
    lock = r.media._recording_maintenance_lock(r.hass, r.root)
    locks = r.hass.data[r.media.DOMAIN]["_recording_maintenance_locks"]
    assert isinstance(locks, weakref.WeakValueDictionary)
    reference = weakref.ref(lock)
    assert len(locks) == 1
    del lock
    gc.collect()
    assert reference() is None
    assert len(locks) == 0


def test_existing_maintenance_lock_objects_survive_map_upgrade(runtime):
    r = runtime
    existing = asyncio.Lock()
    r.hass.data.setdefault(r.media.DOMAIN, {})["_recording_maintenance_locks"] = {
        str(r.root.resolve()): existing
    }
    assert r.media._recording_maintenance_lock(r.hass, r.root) is existing
    assert isinstance(
        r.hass.data[r.media.DOMAIN]["_recording_maintenance_locks"],
        weakref.WeakValueDictionary,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_real_file_worker_double_cancel_drains_before_unlock(
    runtime, worker_fails
):
    r = runtime
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finish = threading.Event()
    marker = r.root / "worker-finished"
    source = object.__new__(r.media.XSenseRecordingsMediaSource)
    source.hass = SimpleNamespace(
        async_add_executor_job=lambda fn, *args: loop.run_in_executor(None, fn, *args)
    )

    def worker():
        loop.call_soon_threadsafe(started.set)
        assert finish.wait(5)
        marker.write_text("finished")
        if worker_fails:
            raise OSError("worker failed after cancellation")

    lock = r.media._recording_maintenance_lock(r.hass, r.root)

    async def publish():
        async with lock:
            await source._async_file_job(worker)

    task = asyncio.create_task(publish())
    await started.wait()
    try:
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert lock.locked()
        assert not marker.exists()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert marker.read_text() == "finished"
    assert not lock.locked()
