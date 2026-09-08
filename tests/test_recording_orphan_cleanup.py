"""Historical staging cleanup and cache-deletion containment regressions."""

import asyncio
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def media():
    return importlib.import_module("custom_components.xsense.recordings_media")


@pytest.fixture
def runtime(media, tmp_path, monkeypatch):
    entries = {"a": SimpleNamespace(entry_id="a", options={}, data={})}
    managers = {"a": SimpleNamespace(_cache={"cameras": [{"serial": "A"}]})}
    hass = SimpleNamespace(
        data={media.DOMAIN: {"_recording_indexes": managers}},
        config_entries=SimpleNamespace(
            async_entries=lambda _: list(entries.values()),
            async_get_entry=entries.get,
        ),
    )

    async def execute(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = execute
    monkeypatch.setattr(media, "_recording_media_root", lambda *_: tmp_path)
    monkeypatch.setattr(
        media, "_recording_media_root_from_value", lambda value: Path(value or tmp_path)
    )
    # Both modification and change times must be older than the orphan threshold.
    now = media.time() + 2 * media.HLS_STAGING_ORPHAN_MIN_AGE_SECONDS
    monkeypatch.setattr(media, "time", lambda: now)
    return hass, entries, managers, tmp_path, now


def staging(root, name="A_1_2"):
    path = root / "hls" / f".{name}.staging"
    path.mkdir(parents=True)
    (path / "segment.ts").write_bytes(b"partial")
    return path


@pytest.mark.asyncio
async def test_maintenance_cleans_old_owned_stage_without_touching_retained_clip(
    media, runtime, monkeypatch
):
    hass, _, _, root, _ = runtime
    stage = staging(root)
    retained = root / "hls/A_1_2"
    retained.mkdir()
    (retained / "segment.ts").write_bytes(b"retained")
    monkeypatch.setattr(media, "_recording_cache_retained", lambda *_: True)
    monkeypatch.setattr(media, "_cache_policy_for_root", lambda *_: (7, 2048))
    result = await media.async_prune_recording_caches(hass, entry_id="a")
    assert result["deleted_items"] == 1
    assert result["deleted_bytes"] == len(b"partial")
    assert not stage.exists()
    assert (retained / "segment.ts").read_bytes() == b"retained"


@pytest.mark.asyncio
@pytest.mark.parametrize("recent", ["directory", "segment", "clock_not_advanced"])
async def test_recent_stage_is_preserved(media, runtime, monkeypatch, recent):
    hass, _, _, root, now = runtime
    stage = staging(root)
    if recent == "clock_not_advanced":
        monkeypatch.setattr(
            media, "time", lambda: now - 2 * media.HLS_STAGING_ORPHAN_MIN_AGE_SECONDS
        )
    else:
        path = stage if recent == "directory" else stage / "segment.ts"
        os.utime(path, (now, now))
    result = await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert result["deleted_items"] == 0
    assert stage.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["B_1_2", "A_01_2", "A_0_2", "A_3_2", "A_1_2_backup", "A_notes"]
)
async def test_unknown_or_noncanonical_stage_names_are_preserved(media, runtime, name):
    hass, _, _, root, _ = runtime
    stage = staging(root, name)
    await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert stage.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_serial", [None, "A", "A_other", "A/other"])
async def test_shared_unknown_or_overlapping_owners_are_preserved(
    media, runtime, other_serial
):
    hass, entries, managers, root, _ = runtime
    stage = staging(root)
    entries["b"] = SimpleNamespace(entry_id="b", options={}, data={})
    if other_serial:
        managers["b"] = SimpleNamespace(_cache={"cameras": [{"serial": other_serial}]})
    await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert stage.exists()


@pytest.mark.asyncio
async def test_colliding_serials_within_entry_are_preserved(media, runtime):
    hass, _, managers, root, _ = runtime
    stage = staging(root, "A_one_1_2")
    managers["a"]._cache["cameras"] = [{"serial": "A/one"}, {"serial": "A_one"}]
    await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert stage.exists()


@pytest.mark.asyncio
async def test_other_entry_data_is_not_collected(media, runtime):
    hass, entries, managers, root, _ = runtime
    own, other = staging(root), staging(root, "B_1_2")
    entries["b"] = SimpleNamespace(entry_id="b", options={}, data={})
    managers["b"] = SimpleNamespace(_cache={"cameras": [{"serial": "B"}]})
    await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert not own.exists()
    assert other.exists()


@pytest.mark.asyncio
async def test_active_writer_discovered_after_scan_is_preserved(media, runtime):
    hass, _, _, root, _ = runtime
    stage = staging(root)
    clip = {"entry_id": "a", "serial": "A", "start": 1, "end": 2, "media_root": root}
    lock = media._recording_cache_lock(hass, clip)

    async def execute(fn, *args):
        result = fn(*args)
        if fn is media._orphan_hls_staging_candidates:
            await lock.acquire()
        return result

    hass.async_add_executor_job = execute
    try:
        result = await media._async_prune_orphan_hls_staging(hass, "a", root)
        assert result["skipped_active"] == 1
        assert stage.exists()
    finally:
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["retained", "proxy"])
async def test_active_playback_preserves_staging(media, runtime, mode):
    hass, _, _, root, _ = runtime
    stage = staging(root)
    token = {"root": root / "hls/A_1_2", "expires": media.monotonic() + 60}
    if mode == "proxy":
        token = {
            "mode": "proxy",
            "cache_key": "A_1_2",
            "media_root": root,
            "expires": media.monotonic() + 60,
        }
    hass.data[media.DOMAIN]["_recording_hls_tokens"] = {"view": token}
    result = await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert result["skipped_active"] == 1
    assert stage.exists()


@pytest.mark.asyncio
async def test_replaced_stage_is_not_deleted_from_stale_scan(media, runtime):
    hass, _, _, root, _ = runtime
    stage = staging(root)

    async def execute(fn, *args):
        result = fn(*args)
        if fn is media._orphan_hls_staging_candidates:
            stage.rename(root / "hls/preserved")
            staging(root)
        return result

    hass.async_add_executor_job = execute
    result = await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert result["deleted_items"] == 0
    assert stage.exists()


@pytest.mark.asyncio
async def test_cancellation_holds_writer_lock_until_delete_worker_finishes(
    media, runtime
):
    hass, _, _, root, _ = runtime
    stage = staging(root)
    clip = {"entry_id": "a", "serial": "A", "start": 1, "end": 2, "media_root": root}
    lock = media._recording_cache_lock(hass, clip)
    started, finish = asyncio.Event(), asyncio.Event()

    async def execute(fn, *args):
        if fn is media._remove_orphan_hls_staging:
            assert lock.locked()
            started.set()
            await finish.wait()
            assert lock.locked()
        return fn(*args)

    hass.async_add_executor_job = execute
    task = asyncio.create_task(media._async_prune_orphan_hls_staging(hass, "a", root))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert lock.locked()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()
    assert not stage.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["stage", "container", "descendant"])
async def test_orphan_cleanup_never_touches_links_or_their_targets(
    media, runtime, kind
):
    hass, _, _, root, _ = runtime
    target = root / "user-data"
    target.mkdir()
    (target / "precious").write_bytes(b"keep")
    if kind == "container":
        (root / "hls").symlink_to(target)
        stage = staging(root)
        link = root / "hls"
    elif kind == "stage":
        (root / "hls").mkdir()
        stage = root / "hls/.A_1_2.staging"
        stage.symlink_to(target)
        link = stage
    else:
        stage = staging(root)
        link = stage / "user-link"
        link.symlink_to(target)
    await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert stage.exists()
    assert link.is_symlink()
    assert (target / "precious").read_bytes() == b"keep"


@pytest.mark.parametrize(
    "container,suffix", [("hls", ""), ("videos", ".mp4"), ("thumbs", ".jpg")]
)
def test_owned_name_symlink_cannot_delete_other_entry(
    media, tmp_path, container, suffix
):
    folder = tmp_path / container
    folder.mkdir()
    target = folder / f"B_1_2{suffix}"
    if container == "hls":
        target.mkdir()
        (target / "segment.ts").write_bytes(b"keep")
    else:
        target.write_bytes(b"keep")
    link = folder / f"A_1_2{suffix}"
    link.symlink_to(target)
    inventory = media._cache_inventory(tmp_path)
    assert "A_1_2" not in inventory
    result = media._delete_media_cache_groups(tmp_path, set(), None, {"A_"})
    assert result["deleted_items"] == 0
    assert target.exists() and link.is_symlink()


@pytest.mark.parametrize(
    "container,suffix", [("hls", ""), ("videos", ".mp4"), ("thumbs", ".jpg")]
)
def test_symlink_containers_are_never_inventoried(media, tmp_path, container, suffix):
    target = tmp_path / "elsewhere"
    target.mkdir()
    artifact = target / f"A_1_2{suffix}"
    artifact.mkdir() if container == "hls" else artifact.write_bytes(b"keep")
    (tmp_path / container).symlink_to(target)
    assert media._cache_inventory(tmp_path) == {}
    media._delete_media_cache_groups(tmp_path, set(), None, None)
    assert artifact.exists()
    assert (tmp_path / container).is_symlink()


def test_hls_tree_containing_user_link_is_skipped(media, tmp_path):
    artifact = tmp_path / "hls/A_1_2"
    artifact.mkdir(parents=True)
    target = tmp_path / "precious"
    target.write_bytes(b"keep")
    (artifact / "segment.ts").symlink_to(target)
    assert media._cache_inventory(tmp_path) == {}
    media._delete_media_cache_groups(tmp_path, set(), None, None)
    assert (artifact / "segment.ts").is_symlink()
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("redirect", ["artifact", "container", "root"])
def test_deletion_revalidates_links_added_after_inventory(media, tmp_path, redirect):
    backing = tmp_path / "backing"
    artifact = backing / "videos/A_1_2.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"original")
    root = tmp_path / "cache"
    root.symlink_to(backing)
    group = media._cache_inventory(root)["A_1_2"]
    elsewhere = tmp_path / "elsewhere"
    other = elsewhere / "videos/A_1_2.mp4"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other entry")
    if redirect == "artifact":
        artifact.rename(artifact.with_suffix(".preserved"))
        artifact.symlink_to(other)
    elif redirect == "container":
        artifact.parent.rename(backing / "preserved")
        (backing / "videos").symlink_to(other.parent)
    else:
        root.unlink()
        root.symlink_to(elsewhere)
    assert media._remove_cache_group(root, group) is None
    assert other.read_bytes() == b"other entry"


def test_valid_symlinked_storage_root_still_allows_ordinary_cleanup(media, tmp_path):
    backing = tmp_path / "backing"
    artifact = backing / "videos/A_1_2.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"cache")
    root = tmp_path / "cache"
    root.symlink_to(backing)
    result = media._delete_media_cache_groups(root, set(), {"A_1_2"}, None)
    assert result["deleted_items"] == 1
    assert not artifact.exists()
    assert root.is_symlink()


@pytest.mark.asyncio
async def test_owner_removed_during_discovery_is_preserved(media, runtime):
    hass, _, managers, root, _ = runtime
    stage = staging(root)

    async def execute(fn, *args):
        result = fn(*args)
        if fn is media._orphan_hls_staging_candidates:
            managers.clear()
        return result

    hass.async_add_executor_job = execute
    await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert stage.exists()


@pytest.mark.parametrize("staged", [False, True])
def test_container_redirected_during_delete_is_not_followed(
    media, tmp_path, monkeypatch, staged
):
    root = tmp_path / "cache"
    root.mkdir()
    path = staging(root) if staged else root / "hls/A_1_2"
    if not staged:
        path.mkdir(parents=True)
        (path / "segment.ts").write_bytes(b"keep")
    target = tmp_path / "user-data"
    target.mkdir()
    other = target / path.name
    other.mkdir()
    (other / "precious").write_bytes(b"untouched")
    if staged:
        cutoff = media.time() + 10
        fingerprint = media._orphan_hls_staging_info(path, cutoff)[0]
    else:
        group = media._cache_inventory(root)["A_1_2"]
    original_open = os.open
    redirected = False

    def redirect_before_open(name, flags, *args, **kwargs):
        nonlocal redirected
        if not redirected:
            redirected = True
            (root / "hls").rename(root / "preserved")
            (root / "hls").symlink_to(target)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", redirect_before_open)
    if staged:
        assert media._remove_orphan_hls_staging(path, cutoff, fingerprint) is None
    else:
        assert media._remove_cache_group(root, group) is None
    assert (other / "precious").read_bytes() == b"untouched"
    assert (root / "hls").is_symlink()


@pytest.mark.asyncio
async def test_waiting_writer_from_another_entry_shares_cleanup_lock(media, runtime):
    hass, _, _, root, _ = runtime
    staging(root)
    clip = {"entry_id": "a", "serial": "A", "start": 1, "end": 2, "media_root": root}
    writer_lock = media._recording_cache_lock(hass, {**clip, "entry_id": "b"})
    assert writer_lock is media._recording_cache_lock(hass, clip)
    deleting, finish, writing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(fn, *args):
        if fn is media._remove_orphan_hls_staging:
            deleting.set()
            await finish.wait()
        return fn(*args)

    async def writer():
        async with writer_lock:
            writing.set()
            return staging(root)

    hass.async_add_executor_job = execute
    cleanup = asyncio.create_task(
        media._async_prune_orphan_hls_staging(hass, "a", root)
    )
    await deleting.wait()
    next_writer = asyncio.create_task(writer())
    cleanup.cancel()
    await asyncio.sleep(0)
    assert not writing.is_set()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    new_stage = await next_writer
    assert writing.is_set()
    assert (new_stage / "segment.ts").read_bytes() == b"partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["retained", "proxy"])
async def test_playback_starting_after_staging_scan_is_preserved(media, runtime, mode):
    hass, _, _, root, _ = runtime
    stage = staging(root)
    token = {"root": root / "hls/A_1_2", "expires": media.monotonic() + 60}
    if mode == "proxy":
        token = {
            "mode": "proxy",
            "cache_key": "A_1_2",
            "media_root": root,
            "expires": media.monotonic() + 60,
        }

    async def execute(fn, *args):
        result = fn(*args)
        if fn is media._orphan_hls_staging_candidates:
            assert result
            hass.data[media.DOMAIN]["_recording_hls_tokens"] = {"new-viewer": token}
        return result

    hass.async_add_executor_job = execute
    result = await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert result["skipped_active"] == 1
    assert stage.exists()
    assert hass.data[media.DOMAIN]["_recording_hls_tokens"]["new-viewer"] is token


@pytest.mark.asyncio
async def test_storage_root_retargeted_after_scan_preserves_both_targets(
    media, runtime, monkeypatch
):
    hass, _, _, root, _ = runtime
    original, other = root / "original", root / "user-data"
    original_stage, other_stage = staging(original), staging(other)
    link = root / "cache-link"
    link.symlink_to(original)
    monkeypatch.setattr(media, "_recording_media_root", lambda *_: link)

    async def execute(fn, *args):
        result = fn(*args)
        if fn is media._orphan_hls_staging_candidates:
            assert result
            link.unlink()
            link.symlink_to(other)
        return result

    hass.async_add_executor_job = execute
    result = await media._async_prune_orphan_hls_staging(hass, "a", link)
    assert result["deleted_items"] == 0
    assert original_stage.exists()
    assert other_stage.exists()
    assert link.is_symlink()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["retained", "proxy"])
async def test_viewer_starting_after_protection_snapshot_does_not_lose_final_media(
    media, runtime, mode
):
    hass, _, _, root, _ = runtime
    stage = staging(root)
    retained = root / "hls/A_1_2"
    retained.mkdir()
    (retained / "segment.ts").write_bytes(b"retained")
    token = {"root": retained, "expires": media.monotonic() + 60}
    if mode == "proxy":
        token = {
            "mode": "proxy",
            "cache_key": "A_1_2",
            "media_root": root,
            "expires": media.monotonic() + 60,
        }

    async def execute(fn, *args):
        if fn is media._remove_orphan_hls_staging:
            hass.data[media.DOMAIN]["_recording_hls_tokens"] = {"new-viewer": token}
        return fn(*args)

    hass.async_add_executor_job = execute
    result = await media._async_prune_orphan_hls_staging(hass, "a", root)
    assert result["deleted_items"] == 1
    assert not stage.exists()
    assert (retained / "segment.ts").read_bytes() == b"retained"
    assert hass.data[media.DOMAIN]["_recording_hls_tokens"]["new-viewer"] is token
