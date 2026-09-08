"""Network and cancellation boundaries in native signal-relay cleanup."""

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["os", "timeout", "client"])
async def test_ticket_network_failure_clears_pending_and_reports_frontend(error_name):
    from aiohttp import ClientConnectionError
    from homeassistant.components.camera.webrtc import WebRTCError

    camera = importlib.import_module("custom_components.xsense.camera")
    device = SimpleNamespace(
        type="SSC0A",
        data={"streamProtocol": "webrtc", "supportWebrtc": True},
        entity_id="camera-test",
        sn="SSC0ATEST",
        name="Camera",
        online=True,
    )
    error = {
        "os": OSError("network failed"),
        "timeout": TimeoutError("timed out"),
        "client": ClientConnectionError("connection failed"),
    }[error_name]
    coordinator = SimpleNamespace(
        data={"stations": {device.entity_id: device}, "devices": {}},
        xsense=SimpleNamespace(get_camera_webrtc_ticket=AsyncMock(side_effect=error)),
        async_add_listener=lambda *args: lambda: None,
    )
    entity = camera.XSenseWebRTCCameraEntity(
        coordinator, device, camera.CAMERA_DESCRIPTION
    )
    entity.hass = SimpleNamespace(async_add_import_executor_job=AsyncMock())
    messages = []
    await entity.async_handle_async_webrtc_offer("v=0", "failed", messages.append)
    assert not entity._pending_webrtc_candidates
    assert not entity._webrtc_sessions
    assert len(messages) == 1
    assert isinstance(messages[0], WebRTCError)
    assert messages[0].code == "xsense_webrtc_ticket_failed"
    entity.hass.async_add_import_executor_job.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "error", "normal"])
async def test_signal_close_always_cancels_and_joins_owned_tasks(outcome):
    signal = importlib.import_module(
        "custom_components.xsense.python_xsense.webrtc_signal"
    )
    session = object.__new__(signal.XSenseWebRTCSignalSession)
    session._answer = asyncio.get_running_loop().create_future()
    entered, finish = asyncio.Event(), asyncio.Event()
    cleaned = []

    async def owned_task(name):
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.append(name)

    async def close_socket():
        entered.set()
        await finish.wait()
        if outcome == "error":
            raise OSError("close failed")

    session._ws = SimpleNamespace(closed=False, close=close_socket)
    read_task = session._read_task = asyncio.create_task(owned_task("read"))
    reconnect_task = session._reconnect_task = asyncio.create_task(
        owned_task("reconnect")
    )
    await asyncio.sleep(0)
    close_task = asyncio.create_task(session.close())
    await entered.wait()
    if outcome == "cancel":
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task
    else:
        finish.set()
        await close_task
    assert read_task.done() and read_task.cancelled()
    assert reconnect_task.done() and reconnect_task.cancelled()
    assert sorted(cleaned) == ["read", "reconnect"]
    assert session._read_task is session._reconnect_task is session._ws is None
    assert session._answer.cancelled()
