"""Keep history discovery from changing a previously accepted ticket identity."""

from types import SimpleNamespace

import pytest

from custom_components.xsense.python_xsense.async_xsense import AsyncXSense
from custom_components.xsense.python_xsense.exceptions import APIFailure


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_previous", [False, True])
async def test_history_lookup_does_not_replace_ticket_identity(reject_previous):
    client = AsyncXSense()
    home = SimpleNamespace(house_id="home-1", mqtt_region="eu-central-1")
    client.houses = {"home-1": home}
    data = {"addxSerialNumber": "live-id"}
    camera = SimpleNamespace(
        sn="label-id", entity_id="history-id", data=data,
        house=home, set_data=data.update,
    )
    calls = []

    async def addx_call(endpoint, **kwargs):
        assert endpoint == "/device/getWebrtcTicket"
        assert kwargs["_house"] is home
        assert kwargs["verifyDormancyStatus"] is True
        serial = kwargs["serialNumber"]
        calls.append(serial)
        if reject_previous and len(calls) > 1 and serial == "live-id":
            raise APIFailure("error -2002/DEVICE_NO_ACCESS")
        return {"signalServer": "https://signal.example"}

    async def history_pages(serials, *args, **kwargs):
        assert kwargs["house"] is home
        return {"list": (
            [{"serialNumber": "history-id", "videoEvent": "motion"}]
            if serials == ["history-id"] else []
        )}

    client.addx_call = addx_call
    client._get_camera_library_pages = history_pages
    first = await client.get_camera_webrtc_ticket(camera, force_refresh=True)
    assert first["serialNumber"] == "live-id"

    history = await client.get_camera_library_history_for_cameras([camera], 1, 2)
    assert len(history["list"]) == 1
    assert data["addxAccessSerialNumber"] == "history-id"

    refreshed = await client.get_camera_webrtc_ticket(camera, force_refresh=True)
    expected = ["live-id", "live-id"]
    if reject_previous:
        expected.append("history-id")
    assert calls == expected
    assert refreshed["serialNumber"] == expected[-1]
