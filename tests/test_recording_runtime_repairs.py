"""Behavioral regressions for the recording runtime audit."""

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def media():
    return importlib.import_module("custom_components.xsense.recordings_media")


def shared_hass(media, tmp_path, monkeypatch):
    entries = {
        key: SimpleNamespace(entry_id=key, options={}, data={}) for key in ("a", "b")
    }
    hass = SimpleNamespace(
        data={
            media.DOMAIN: {
                "_recording_indexes": {
                    key: SimpleNamespace(
                        _cache={
                            "cameras": [
                                {
                                    "serial": key.upper(),
                                    "clips": [
                                        {
                                            "serial": key.upper(),
                                            "entry_id": key,
                                            "start": 1,
                                            "end": 2,
                                        }
                                    ],
                                }
                            ]
                        }
                    )
                    for key in entries
                }
            }
        },
        config_entries=SimpleNamespace(
            async_entries=lambda domain: list(entries.values()),
            async_get_entry=entries.get,
        ),
    )

    async def execute(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = execute
    monkeypatch.setattr(media, "_recording_media_root", lambda *_: tmp_path)
    monkeypatch.setattr(media, "_recording_media_root_from_value", lambda _: tmp_path)
    monkeypatch.setattr(media, "_cache_policy_for_root", lambda *_: (7, 2048))
    monkeypatch.setattr(
        media, "_recording_cache_retained", lambda _, entry: entry == "a"
    )
    (tmp_path / "videos").mkdir()
    for serial in ("A", "B"):
        path = tmp_path / "videos" / f"{serial}_1_2.mp4"
        path.write_bytes(b"recording")
        os.utime(path, (media.time() - 3600, media.time() - 3600))
    return hass


@pytest.mark.asyncio
async def test_playback_policy_does_not_prune_other_entry(media, tmp_path, monkeypatch):
    hass = shared_hass(media, tmp_path, monkeypatch)
    await media.async_prune_recording_caches(hass, entry_id="b")
    assert (tmp_path / "videos/A_1_2.mp4").exists()
    assert not (tmp_path / "videos/B_1_2.mp4").exists()


@pytest.mark.asyncio
async def test_global_prune_respects_each_entry_policy(media, tmp_path, monkeypatch):
    hass = shared_hass(media, tmp_path, monkeypatch)
    await media.async_prune_recording_caches(hass)
    assert (tmp_path / "videos/A_1_2.mp4").exists()
    assert not (tmp_path / "videos/B_1_2.mp4").exists()


@pytest.mark.asyncio
async def test_entry_clear_leaves_other_entry_files(media, tmp_path, monkeypatch):
    hass = shared_hass(media, tmp_path, monkeypatch)
    await media.async_clear_recording_caches(hass, entry_id="b")
    assert (tmp_path / "videos/A_1_2.mp4").exists()
    assert not (tmp_path / "videos/B_1_2.mp4").exists()


@pytest.mark.asyncio
async def test_shared_camera_is_not_deleted_by_entry_policy(
    media, tmp_path, monkeypatch
):
    hass = shared_hass(media, tmp_path, monkeypatch)
    hass.data[media.DOMAIN]["_recording_indexes"]["b"]._cache["cameras"] = [
        {"serial": "A"}
    ]
    await media.async_prune_recording_caches(hass, entry_id="b")
    await media.async_delete_recording_cache(
        hass, {"entry_id": "b", "serial": "A", "start": 1, "end": 2}
    )
    assert (tmp_path / "videos/A_1_2.mp4").exists()


@pytest.mark.parametrize(
    "value",
    [
        "/media/../config",
        "/media/a/../../config",
        "/media/a\x00b",
        "relative",
        "/media-other/cache",
    ],
)
def test_runtime_rejects_invalid_media_roots(media, value):
    assert media._recording_media_root_from_value(value) == Path(
        media.DEFAULT_RECORDING_MEDIA_STORAGE_PATH
    )


@pytest.mark.parametrize(
    "value", [None, "/media/xsense_recordings", "/media/../config"]
)
def test_runtime_rejects_escaping_default_fallback(media, monkeypatch, value):
    resolve = Path.resolve

    def resolve_default(path, *args, **kwargs):
        if path == Path(media.DEFAULT_RECORDING_MEDIA_STORAGE_PATH):
            return Path("/config/escaped-recordings")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_default)
    with pytest.raises(ValueError, match="escapes /media"):
        media._recording_media_root_from_value(value)


@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError])
def test_runtime_rejects_unresolvable_default_fallback(media, monkeypatch, error):
    resolve = Path.resolve

    def resolve_default(path, *args, **kwargs):
        if path == Path(media.DEFAULT_RECORDING_MEDIA_STORAGE_PATH):
            raise error("invalid default")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_default)
    with pytest.raises(ValueError, match="could not be validated"):
        media._recording_media_root_from_value("/media/../config")


def test_runtime_supports_symlinked_media_root_but_not_escaping_child(
    media, tmp_path, monkeypatch
):
    backing = tmp_path / "backing"
    backing.mkdir()
    (backing / "escape").symlink_to(tmp_path)
    resolve = Path.resolve

    def mapped_resolve(path, *args, **kwargs):
        if path.is_relative_to(Path("/media")):
            return resolve(backing / path.relative_to("/media"), *args, **kwargs)
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", mapped_resolve)
    assert media._recording_media_root_from_value("/media/cache") == Path(
        "/media/cache"
    )
    assert media._recording_media_root_from_value("/media/escape/cache") == Path(
        media.DEFAULT_RECORDING_MEDIA_STORAGE_PATH
    )


@pytest.mark.parametrize(
    "serial_a,serial_b",
    [("cam", "cam_other"), ("cam_other", "cam"), ("cam/one", "cam_one")],
)
def test_overlapping_camera_prefixes_are_not_owned(
    media, tmp_path, monkeypatch, serial_a, serial_b
):
    hass = shared_hass(media, tmp_path, monkeypatch)
    managers = hass.data[media.DOMAIN]["_recording_indexes"]
    managers["a"]._cache["cameras"] = [{"serial": serial_a}]
    managers["b"]._cache["cameras"] = [{"serial": serial_b}]
    assert media._entry_cache_prefixes(hass, "a") == set()
    assert media._entry_cache_prefixes(hass, "b") == set()


def test_history_replaces_stale_event_url_and_root(media):
    hass = SimpleNamespace(data={})
    event = {
        "entry_id": "a",
        "serial": "A",
        "start": 1,
        "end": 2,
        "source": "video_url",
        "playback_url": "https://old/clip",
        "media_root": "/media/old",
    }
    fresh = {**event, "playback_url": "https://fresh/clip", "media_root": "/media/new"}
    media._remember_event_recording_clip(hass, event)
    result = media._merge_event_recording_clips(
        hass, [{"entry_id": "a", "serial": "A", "clips": [fresh]}]
    )
    assert result[0]["clips"] == [fresh]
    newer = {**fresh, "playback_url": "https://newer/clip"}
    result = media._merge_event_recording_clips(
        hass, [{"entry_id": "a", "serial": "A", "clips": [newer]}]
    )
    assert result[0]["clips"] == [newer]


@pytest.mark.asyncio
async def test_release_is_token_owned_and_preserves_second_viewer(
    media, tmp_path, monkeypatch
):
    hass = shared_hass(media, tmp_path, monkeypatch)
    proxy = {
        "mode": "proxy",
        "entry_id": "b",
        "cache_key": "B_1_2",
        "media_root": tmp_path,
        "expires": media.monotonic() + 100,
    }
    tokens = hass.data[media.DOMAIN]["_recording_hls_tokens"] = {
        "one": dict(proxy),
        "two": dict(proxy),
    }
    await media.async_release_recording_playback(
        hass, entry_id="a", serial="B", start=1, end=2, token="one"
    )
    assert set(tokens) == {"one", "two"}
    await media.async_release_recording_playback(
        hass, entry_id="b", serial="B", start=1, end=2
    )
    assert set(tokens) == {"one", "two"}
    await media.async_release_recording_playback(
        hass, entry_id="b", serial="B", start=1, end=2, token="one"
    )
    assert set(tokens) == {"two"}
    assert (tmp_path / "videos/B_1_2.mp4").exists()
    await media.async_release_recording_playback(
        hass, entry_id="b", serial="B", start=1, end=2, token="two"
    )
    assert not tokens
    assert not (tmp_path / "videos/B_1_2.mp4").exists()


@pytest.mark.asyncio
async def test_cleanup_reschedules_while_proxy_is_active(media, tmp_path, monkeypatch):
    hass = shared_hass(media, tmp_path, monkeypatch)
    hass.loop = asyncio.get_running_loop()
    clip = {
        "entry_id": "b",
        "serial": "B",
        "start": 1,
        "end": 2,
        "media_root": tmp_path,
    }
    hass.data[media.DOMAIN]["_recording_hls_tokens"] = {
        "viewer": {
            "mode": "proxy",
            "entry_id": "b",
            "cache_key": "B_1_2",
            "media_root": tmp_path,
            "expires": media.monotonic() + 100,
        }
    }
    callbacks = []

    def call_later(hass, delay, callback):
        callbacks.append(callback)
        return lambda: None

    monkeypatch.setattr(media, "async_call_later", call_later)
    media.async_schedule_temporary_recording_cleanup(hass, clip)
    await callbacks[0]()
    assert len(callbacks) == 2
    assert (tmp_path / "videos/B_1_2.mp4").exists()


def test_retained_token_is_removed_on_entry_unload(media, tmp_path):
    http = importlib.import_module("custom_components.xsense.http")
    hass = SimpleNamespace(data={})
    token = http._create_hls_segment_token(hass, tmp_path, "a")
    other = http._create_hls_segment_token(hass, tmp_path, "b")
    media.async_stop_recording_media_sync(hass, "a")
    assert token not in hass.data[media.DOMAIN]["_recording_hls_tokens"]
    assert other in hass.data[media.DOMAIN]["_recording_hls_tokens"]


@pytest.mark.asyncio
async def test_cancelled_download_removes_staging(media, tmp_path, monkeypatch):
    source = object.__new__(media.XSenseRecordingsMediaSource)
    source.hass = SimpleNamespace()
    monkeypatch.setattr(media, "_hls_cache_dir", lambda _: tmp_path / "clip")
    entered = asyncio.Event()

    async def download(*args, **kwargs):
        (args[2] / "partial.ts").write_bytes(b"partial")
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(source, "_async_cache_hls_playlist", download)
    task = asyncio.create_task(source._async_cache_hls_clip("https://example/clip", {}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not media._hls_staging_cache_dir(tmp_path / "clip").exists()


@pytest.mark.asyncio
async def test_filesystem_cancellation_waits_for_worker(media):
    source = object.__new__(media.XSenseRecordingsMediaSource)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def executor(fn, *args):
        entered.set()
        await finish.wait()
        return fn(*args)

    source.hass = SimpleNamespace(async_add_executor_job=executor)
    calls = []
    task = asyncio.create_task(source._async_file_job(calls.append, "finished"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["finished"]


def test_nested_hls_repaired_sidecar_uses_root_relative_path(
    media, tmp_path, monkeypatch
):
    variant = tmp_path / "variant"
    variant.mkdir()
    (variant / "segment.ts").write_bytes(b"segment")
    (variant / "fixed.ts").write_bytes(b"fixed")
    playlist = variant / "index.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:2,\nsegment.ts\n#EXT-X-ENDLIST\n")
    profile = {
        "leading_aac": media.HLS_LEADING_AAC_BROKEN,
        "leading_segment": "segment.ts",
        "leading_playback_segment": "fixed.ts",
        "leading_playback_verified": True,
    }
    monkeypatch.setattr(
        media, "_categorize_hls_leading_segment", lambda *a, **k: profile
    )
    media._finalize_hls_playback_profile(
        tmp_path,
        {
            "leading_segment_path": variant / "segment.ts",
            "leading_playlist_path": playlist,
        },
    )
    assert (
        media._read_hls_playback_profile(tmp_path)["leading_playback_segment"]
        == "variant/fixed.ts"
    )
    assert media._hls_playback_profile_ready(tmp_path)


@pytest.mark.asyncio
async def test_paging_fetches_all_records_and_stops_repeated_page():
    module = importlib.import_module(
        "custom_components.xsense.python_xsense.async_xsense"
    )
    client = object.__new__(module.AsyncXSense)
    client.get_camera_library_history = AsyncMock(
        side_effect=[{"list": [{"id": 1}, {"id": 2}]}, {"list": [{"id": 3}]}]
    )
    result = await client._get_camera_library_pages(
        ["A"], 1, 2, house=None, start=0, limit=2
    )
    assert [item["id"] for item in result["list"]] == [1, 2, 3]
    assert [
        call.kwargs["start"]
        for call in client.get_camera_library_history.call_args_list
    ] == [0, 2]
    client.get_camera_library_history = AsyncMock(
        return_value={"list": [{"id": 1}, {"id": 2}]}
    )
    result = await client._get_camera_library_pages(
        ["A"], 1, 2, house=None, start=0, limit=2
    )
    assert len(result["list"]) == 2
    assert client.get_camera_library_history.await_count == 2


@pytest.mark.asyncio
async def test_library_paging_uses_apk_exclusive_end_index():
    module = importlib.import_module(
        "custom_components.xsense.python_xsense.async_xsense"
    )
    client = object.__new__(module.AsyncXSense)
    client.addx_call = AsyncMock(
        side_effect=[
            {"list": [{"id": 1}, {"id": 2}]},
            {"list": [{"id": 3}]},
        ]
    )
    result = await client._get_camera_library_pages(
        ["A"], 1, 2, house=None, start=0, limit=2
    )
    assert len(result["list"]) == 3
    assert [
        (call.kwargs["from"], call.kwargs["to"])
        for call in client.addx_call.call_args_list
    ] == [(0, 2), (2, 4)]


@pytest.mark.asyncio
async def test_signal_close_during_connect_closes_late_socket():
    signal = importlib.import_module(
        "custom_components.xsense.python_xsense.webrtc_signal"
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    ws = SimpleNamespace(close=AsyncMock())

    async def connect(*args, **kwargs):
        entered.set()
        await finish.wait()
        return ws

    ticket = signal.XSenseWebRTCTicket.from_api(
        "CAM",
        {
            "signalServer": "wss://example",
            "groupId": "group",
            "role": "viewer",
            "id": "id",
            "traceId": "trace",
            "sign": "sign",
            "time": 123,
            "expirationTime": 9999999999999,
            "iceServer": [],
        },
    )
    session = signal.XSenseWebRTCSignalSession(
        session=SimpleNamespace(ws_connect=connect),
        ticket=ticket,
        offer_sdp="v=0",
        resolution="HD",
        camera_online=True,
        remote_candidate_callback=lambda _: None,
    )
    task = asyncio.create_task(session.start())
    await entered.wait()
    await session.close()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    ws.close.assert_awaited_once()
    assert session._ws is None
    assert session._read_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_camera_close_or_cancel_during_ticket_never_starts_signal(
    monkeypatch, cancel
):
    camera = importlib.import_module("custom_components.xsense.camera")
    device = SimpleNamespace(
        type="SSC0A",
        data={"streamProtocol": "webrtc", "supportWebrtc": True},
        entity_id="camera-test",
        sn="SSC0ATEST",
        name="Camera",
        online=True,
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def ticket(*args, **kwargs):
        entered.set()
        await finish.wait()
        return {"signalServer": "https://signal.example"}

    coordinator = SimpleNamespace(
        data={"stations": {device.entity_id: device}, "devices": {}},
        xsense=SimpleNamespace(get_camera_webrtc_ticket=ticket),
        async_add_listener=lambda *a: lambda: None,
    )
    entity = camera.XSenseWebRTCCameraEntity(
        coordinator, device, camera.CAMERA_DESCRIPTION
    )
    entity.hass = SimpleNamespace(
        data={},
        async_create_task=asyncio.create_task,
        async_add_import_executor_job=AsyncMock(),
    )
    messages = []
    task = asyncio.create_task(
        entity.async_handle_async_webrtc_offer("v=0", "one", messages.append)
    )
    await entered.wait()
    if cancel:
        task.cancel()
    else:
        entity.close_webrtc_session("one")
        finish.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert not entity._webrtc_sessions
    assert not entity._pending_webrtc_candidates
    assert not messages
    entity.hass.async_add_import_executor_job.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_proxy_preparation_dropped_request_releases_token(
    media, monkeypatch, cancel
):
    from aiohttp import web

    http = importlib.import_module("custom_components.xsense.http")
    hass = SimpleNamespace(data={})
    clip = {
        "entry_id": "a",
        "serial": "A",
        "start": 1,
        "end": 2,
        "playback_url": "https://example/clip.m3u8",
    }
    view = http.XSenseRecordingsPanelPlaybackView(hass)
    monkeypatch.setattr(view, "_clip", AsyncMock(return_value=clip))
    monkeypatch.setattr(http, "_recordings_runtime_available", lambda _: True)
    monkeypatch.setattr(http, "_recording_cache_retained", lambda *_: False)
    monkeypatch.setattr(http, "XSenseRecordingsMediaSource", lambda _: None)
    monkeypatch.setattr(
        http,
        "_async_fetch_proxy_resource",
        AsyncMock(
            return_value=(
                b"#EXTM3U\n",
                "application/vnd.apple.mpegurl",
                clip["playback_url"],
            )
        ),
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def rewrite(*args):
        entered.set()
        await finish.wait()
        return "#EXTM3U\n", {}

    monkeypatch.setattr(http, "_async_rewrite_hls_proxy_playlist", rewrite)
    monkeypatch.setattr(
        http, "async_schedule_temporary_recording_cleanup", lambda *args: None
    )
    request = SimpleNamespace(
        query={"serial": "A"}, transport=SimpleNamespace(is_closing=lambda: False)
    )
    task = asyncio.create_task(view.get(request, "a", "1", "2"))
    await entered.wait()
    assert hass.data[media.DOMAIN]["_recording_hls_tokens"]
    if cancel:
        task.cancel()
    else:
        request.transport = None
        finish.set()
    with pytest.raises(asyncio.CancelledError if cancel else web.HTTPGone):
        await task
    assert not hass.data[media.DOMAIN]["_recording_hls_tokens"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_retained_preparation_dropped_request_releases_token(
    media, tmp_path, monkeypatch, cancel
):
    from aiohttp import web

    http = importlib.import_module("custom_components.xsense.http")
    hass = SimpleNamespace(data={})
    clip = {"entry_id": "a", "serial": "A", "start": 1, "end": 2}
    view = http.XSenseRecordingsPanelPlaybackView(hass)
    monkeypatch.setattr(view, "_clip", AsyncMock(return_value=clip))
    monkeypatch.setattr(http, "_recordings_runtime_available", lambda _: True)
    monkeypatch.setattr(http, "_recording_cache_retained", lambda *_: True)
    monkeypatch.setattr(http, "_recording_media_sync_enabled", lambda *_: False)
    monkeypatch.setattr(
        http, "async_recording_cache_suppressed", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(http, "async_touch_recording_cache", AsyncMock())
    monkeypatch.setattr(
        http, "_hls_playlist_cache_path", lambda _: tmp_path / "A_1_2/index.m3u8"
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def read_playlist(*args):
        entered.set()
        await finish.wait()
        return "#EXTM3U\n"

    source = SimpleNamespace(
        _async_cached_media_ready=AsyncMock(return_value=True),
        _async_cached_media_format=AsyncMock(return_value="hls"),
        _async_cached_playback_url=AsyncMock(return_value="/media/clip.m3u8"),
        _async_hls_ready=AsyncMock(return_value=True),
        _async_file_job=read_playlist,
    )
    monkeypatch.setattr(http, "XSenseRecordingsMediaSource", lambda _: source)
    request = SimpleNamespace(
        query={"serial": "A"}, transport=SimpleNamespace(is_closing=lambda: False)
    )
    task = asyncio.create_task(view.get(request, "a", "1", "2"))
    await entered.wait()
    assert hass.data[media.DOMAIN]["_recording_hls_tokens"]
    if cancel:
        task.cancel()
    else:
        request.transport = SimpleNamespace(is_closing=lambda: True)
        finish.set()
    with pytest.raises(asyncio.CancelledError if cancel else web.HTTPGone):
        await task
    assert not hass.data[media.DOMAIN]["_recording_hls_tokens"]


@pytest.mark.asyncio
async def test_loaded_index_rebinds_old_root_and_hls_path(media, monkeypatch):
    manager = object.__new__(media.XSenseRecordingIndex)
    manager._loaded = False
    manager.hass = SimpleNamespace()
    manager.entry_id = "a"
    clip = {
        "entry_id": "a",
        "serial": "A",
        "start": 1,
        "end": 2,
        "media_root": "/media/old",
        "cached_url": "/media/local/old/videos/A_1_2.mp4",
        "thumbnail_url": "https://example/thumb.jpg",
    }
    manager._store = SimpleNamespace(
        async_load=AsyncMock(return_value={"cameras": [{"clips": [clip]}]})
    )
    monkeypatch.setattr(
        media, "_recording_media_root", lambda *args: Path("/media/new")
    )
    await manager._async_load()
    assert clip["media_root"] == "/media/new"
    assert clip["cached_url"] == "/media/local/new/videos/A_1_2.mp4"
    assert clip["cached_thumbnail_url"] == "/media/local/new/thumbs/A_1_2.jpg"
    assert media._hls_playlist_cache_path(clip) == Path(
        "/media/new/hls/A_1_2/index.m3u8"
    )
