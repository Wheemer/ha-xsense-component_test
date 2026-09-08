"""Path-stable shared-root ownership and existing cleanup controls."""

import importlib
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    media = importlib.import_module("custom_components.xsense.recordings_media")
    ledger = importlib.import_module("custom_components.xsense.cache_ownership")
    entries = {
        key: SimpleNamespace(entry_id=key, options={}, data={})
        for key in ("one", "two")
    }
    hass = SimpleNamespace(
        data={media.DOMAIN: {"_recording_indexes": {}}},
        config_entries=SimpleNamespace(
            async_entries=lambda _: list(entries.values()),
            async_get_entry=entries.get,
        ),
    )

    async def execute(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = execute
    monkeypatch.setattr(media, "_recording_media_root", lambda *_: tmp_path)
    monkeypatch.setattr(media, "DEFAULT_RECORDING_MEDIA_STORAGE_PATH", str(tmp_path))
    monkeypatch.setattr(
        media, "_recording_media_root_from_value", lambda value: Path(value or tmp_path)
    )
    monkeypatch.setattr(media, "_cache_policy_for_root", lambda *_: (7, 0))

    def guard(function):
        def guarded(root, *args, **kwargs):
            assert Path(root).resolve().is_relative_to(tmp_path.resolve()), root
            return function(root, *args, **kwargs)

        return guarded

    monkeypatch.setattr(
        media, "_delete_media_cache_groups", guard(media._delete_media_cache_groups)
    )
    monkeypatch.setattr(media, "_cache_inventory", guard(media._cache_inventory))
    monkeypatch.setattr(media, "save_ledger", guard(media.save_ledger))
    monkeypatch.setattr(media, "load_ledger", guard(media.load_ledger))
    monkeypatch.setattr(ledger, "save_ledger", guard(ledger.save_ledger))

    def clip(owner="one", serial="CAM"):
        return {
            "entry_id": owner,
            "serial": serial,
            "start": 1,
            "end": 2,
            "media_root": str(tmp_path),
        }

    def index(owner, serial="CAM", *, saved=False):
        data = {
            "cameras": [
                {"entry_id": owner, "serial": serial, "clips": [clip(owner, serial)]}
            ]
        }
        manager = SimpleNamespace(
            _cache=None if saved else data,
            _store=SimpleNamespace(async_load=AsyncMock(return_value=data)),
            async_clear=AsyncMock(),
        )
        hass.data[media.DOMAIN].setdefault("_recording_indexes", {})[owner] = manager
        return manager

    def artifact(serial="CAM"):
        path = tmp_path / "videos" / f"{media._safe_segment(serial)}_1_2.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"existing recording bytes")
        return path

    def token(owner, name):
        hass.data[media.DOMAIN].setdefault("_recording_hls_tokens", {})[name] = {
            "entry_id": owner,
            "root": tmp_path / "hls/CAM_1_2",
            "cache_key": "CAM_1_2",
            "expires": monotonic() + 1000,
        }

    return SimpleNamespace(
        media=media,
        ledger=ledger,
        hass=hass,
        root=tmp_path,
        entries=entries,
        clip=clip,
        index=index,
        artifact=artifact,
        token=token,
    )


@pytest.mark.asyncio
async def test_shared_release_preserves_other_owner_and_tokens_across_reload(runtime):
    r = runtime
    r.index("one")
    unloaded = r.index("two", saved=True)
    path = r.artifact()
    r.token("one", "first")
    r.token("two", "second")
    result = await r.media.async_delete_camera_recording_cache(
        r.hass, entry_id="one", serial="CAM", suppress_recache=True
    )
    assert (
        result["deleted_items"] == 0
        and path.read_bytes() == b"existing recording bytes"
    )
    assert set(r.hass.data[r.media.DOMAIN]["_recording_hls_tokens"]) == {"second"}
    unloaded._store.async_load.assert_awaited()
    claims = r.ledger.load_ledger(r.root)["clips"]["CAM_1_2"]["owners"]
    assert claims["one"] == {"state": "released", "suppressed": True}
    assert claims["two"] == {"state": "active", "suppressed": False}
    assert r.media._recording_cache_suppressed(r.clip("one"))
    assert not r.media._recording_cache_suppressed(r.clip("two"))
    # No in-memory ledger survives this operation; a fresh disk load retains ownership.
    result = await r.media.async_delete_camera_recording_cache(
        r.hass, entry_id="two", serial="CAM"
    )
    assert result["deleted_items"] == 1 and not path.exists()
    assert not r.hass.data[r.media.DOMAIN]["_recording_hls_tokens"]


@pytest.mark.asyncio
async def test_single_entry_adopts_before_clearing_index(runtime):
    r = runtime
    r.entries.pop("two")
    manager = r.index("one")
    path = r.artifact()

    async def cleared():
        assert not path.exists()
        assert r.ledger.load_ledger(r.root)["clips"]["CAM_1_2"]["serial"] == "CAM"

    manager.async_clear.side_effect = cleared
    result = await r.media.async_clear_recording_caches(r.hass, entry_id="one")
    assert result["deleted_items"] == 1
    manager.async_clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_other_entry_blocks_legacy_claim_but_global_clear_is_available(
    runtime,
):
    r = runtime
    r.index("one")
    path = r.artifact()
    await r.media._async_claim_recording_cache(r.hass, r.clip(), activate=True)
    assert not r.ledger.load_ledger(r.root)["clips"]
    result = await r.media.async_delete_camera_recording_cache(
        r.hass, entry_id="one", serial="CAM"
    )
    assert result["deleted_items"] == 0 and path.exists()
    result = await r.media.async_clear_recording_caches(r.hass)
    assert result["deleted_items"] == 1 and not path.exists()


@pytest.mark.asyncio
async def test_unknown_ordinary_cache_only_existing_global_clear(runtime):
    r = runtime
    r.index("one")
    r.index("two")
    path = r.artifact("unidentified")
    result = await r.media.async_prune_recording_caches(r.hass, entry_id="one")
    assert result["deleted_items"] == 0 and path.exists()
    result = await r.media.async_clear_recording_caches(r.hass)
    assert result["deleted_items"] == 1 and not path.exists()


@pytest.mark.asyncio
async def test_sanitized_collision_is_not_entry_owned(runtime):
    r = runtime
    r.index("one", "CAM/A")
    r.index("two", "CAM_A")
    path = r.artifact("CAM/A")
    result = await r.media.async_delete_camera_recording_cache(
        r.hass, entry_id="one", serial="CAM/A"
    )
    assert result["deleted_items"] == 0 and path.exists()
    assert r.ledger.load_ledger(r.root)["clips"]["CAM_A_1_2"]["conflict"]


@pytest.mark.asyncio
async def test_manual_allow_only_reactivates_requesting_owner(runtime):
    r = runtime
    r.index("one")
    r.index("two")
    r.artifact()
    await r.media.async_clear_recording_caches(r.hass, suppress_recache=True)
    await r.media.async_allow_recording_cache(r.hass, r.clip("one"))
    assert not r.media._recording_cache_suppressed(r.clip("one"))
    assert r.media._recording_cache_suppressed(r.clip("two"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["corrupt", "write"])
async def test_metadata_failure_preserves_files_claims_and_tokens(
    runtime, monkeypatch, failure
):
    r = runtime
    r.index("one")
    r.index("two")
    path = r.artifact()
    await r.media._async_claim_recording_cache(r.hass, r.clip(), activate=True)
    ledger_path = r.root / r.ledger.LEDGER_NAME
    if failure == "corrupt":
        ledger_path.write_text("not valid json")
    else:

        def fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(r.ledger.os, "replace", fail)
    before = ledger_path.read_bytes()
    r.token("one", "first")
    with pytest.raises(r.ledger.OwnershipError):
        await r.media.async_delete_camera_recording_cache(
            r.hass, entry_id="one", serial="CAM"
        )
    assert path.exists() and ledger_path.read_bytes() == before
    assert "first" in r.hass.data[r.media.DOMAIN]["_recording_hls_tokens"]


@pytest.mark.asyncio
async def test_shared_retention_releases_one_owner_without_deleting_other(runtime):
    r = runtime
    r.index("one")
    r.index("two")
    path = r.artifact()
    first = await r.media.async_prune_recording_caches(r.hass, entry_id="one")
    assert first["deleted_items"] == 0 and path.exists()
    second = await r.media.async_prune_recording_caches(r.hass, entry_id="two")
    assert second["deleted_items"] == 1 and not path.exists()


@pytest.mark.asyncio
async def test_known_noncamera_entry_does_not_block_legacy_adoption(runtime):
    r = runtime
    r.index("one")
    r.hass.data[r.media.DOMAIN]["two"] = SimpleNamespace(
        last_update_success=True, data={"stations": {}, "devices": {}}
    )
    path = r.artifact()
    await r.media._async_claim_recording_cache(r.hass, r.clip(), activate=True)
    assert (
        r.ledger.load_ledger(r.root)["clips"]["CAM_1_2"]["owners"]["one"]["state"]
        == "active"
    )
    result = await r.media.async_delete_camera_recording_cache(
        r.hass, entry_id="one", serial="CAM"
    )
    assert result["deleted_items"] == 1 and not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", ["corrupt", "missing_other_entry"])
async def test_housekeeping_metadata_does_not_block_cached_playback(
    runtime, monkeypatch, metadata
):
    r = runtime
    r.index("one")
    path = r.artifact()
    if metadata == "corrupt":
        r.index("two")
        (r.root / r.ledger.LEDGER_NAME).write_text("corrupt")
    source = object.__new__(r.media.XSenseRecordingsMediaSource)
    source.hass = r.hass
    monkeypatch.setattr(
        source, "_async_cached_media_ready", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        source,
        "_async_cached_direct_playback_url_unlocked",
        AsyncMock(return_value=str(path)),
    )
    monkeypatch.setattr(
        r.media, "async_schedule_temporary_recording_cleanup", lambda *_: None
    )
    assert await r.media.async_recording_cache_suppressed(r.hass, r.clip()) == (
        metadata == "corrupt"
    )
    await r.media.async_allow_recording_cache(r.hass, r.clip())
    result = await source._async_cached_direct_playback_url(
        r.clip(), "https://example/recording.mp4"
    )
    assert result == str(path) and path.read_bytes() == b"existing recording bytes"
    if metadata == "corrupt":
        with pytest.raises(r.ledger.OwnershipError):
            await r.media.async_prune_recording_caches(r.hass, entry_id="one")
    else:
        assert (await r.media.async_prune_recording_caches(r.hass, entry_id="one"))[
            "deleted_items"
        ] == 0
    assert path.exists()


@pytest.mark.asyncio
async def test_expired_released_bookkeeping_is_trimmed_but_suppression_stays(runtime):
    r = runtime
    r.index("one")
    r.index("two")
    r.artifact()
    await r.media.async_clear_recording_caches(r.hass)
    await r.media.async_prune_recording_caches(r.hass, entry_id="one")
    assert not r.ledger.load_ledger(r.root)["clips"]
    r.index("one")
    r.index("two")
    r.artifact()
    await r.media.async_clear_recording_caches(r.hass, suppress_recache=True)
    await r.media.async_prune_recording_caches(r.hass, entry_id="one")
    assert r.media._recording_cache_suppressed(r.clip())


@pytest.mark.asyncio
async def test_corrupt_deletion_preferences_do_not_restart_background_downloads(runtime, monkeypatch):
    r = runtime
    r.index("one")
    r.index("two")
    (r.root / r.ledger.LEDGER_NAME).write_text("corrupt")
    monkeypatch.setattr(r.media, "async_refresh_recording_indexes", AsyncMock(return_value=[{}]))
    monkeypatch.setattr(r.media, "_recording_cache_candidates", lambda _: [r.clip()])
    monkeypatch.setattr(r.media, "_recording_cache_retained", lambda *_: True)
    thumbnails = AsyncMock()
    videos = AsyncMock()
    monkeypatch.setattr(r.media.XSenseRecordingsMediaSource, "_async_cache_thumbnail", thumbnails)
    monkeypatch.setattr(r.media.XSenseRecordingsMediaSource, "_async_cached_playback_url", videos)
    result = await r.media.async_cache_recording_media(r.hass, entry_id="one")
    assert result == {"downloaded": 0, "thumbnails": 0, "skipped": 1, "failed": 0}
    thumbnails.assert_not_awaited()
    videos.assert_not_awaited()
    assert (r.root / r.ledger.LEDGER_NAME).read_text() == "corrupt"


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery", ["initialized", "failed", "saved_empty"])
async def test_empty_coordinator_requires_verified_discovery(runtime, discovery):
    r = runtime
    r.index("one")
    path = r.artifact()
    if discovery == "saved_empty":
        manager = r.index("two", saved=True)
        manager._store.async_load.return_value = {"cameras": []}
    else:
        r.hass.data[r.media.DOMAIN]["two"] = SimpleNamespace(
            last_update_success=discovery == "initialized",
            data={} if discovery == "initialized" else {"stations": {}, "devices": {}},
        )
    result = await r.media.async_delete_camera_recording_cache(
        r.hass, entry_id="one", serial="CAM"
    )
    assert result["deleted_items"] == (1 if discovery == "saved_empty" else 0)
    assert path.exists() == (discovery != "saved_empty")


@pytest.mark.asyncio
async def test_unknown_entry_blocks_removal_of_previously_released_bytes(runtime):
    r = runtime
    r.index("one")
    path = r.artifact()
    ledger = {
        "version": 1,
        "clips": {
            "CAM_1_2": {
                "serial": "CAM",
                "owners": {"one": {"state": "released", "suppressed": False}},
            }
        },
    }
    r.ledger.save_ledger(r.root, ledger)
    result = await r.media.async_prune_recording_caches(r.hass, entry_id="one")
    assert result["deleted_items"] == 0 and path.exists()
