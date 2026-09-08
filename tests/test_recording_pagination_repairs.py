"""Regression coverage for changing signed URLs and paginated totals."""

import importlib
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {"traceId": "trace"},
        {"traceIds": ["trace"]},
        {"id": "id"},
        {"timestamp": 123},
        {"startTime": 123, "endTime": 125},
    ],
)
async def test_repeated_record_with_refreshed_urls_stops(identity):
    module = importlib.import_module(
        "custom_components.xsense.python_xsense.async_xsense"
    )
    client = object.__new__(module.AsyncXSense)
    record = {"serialNumber": "CAM", **identity}
    client.addx_call = AsyncMock(
        side_effect=[
            {"list": [{**record, "videoUrl": "https://old"}], "total": 100},
            {"list": [{**record, "videoUrl": "https://fresh"}], "total": 100},
        ]
    )
    result = await client._get_camera_library_pages(
        ["CAM"], 1, 2, house=None, start=0, limit=1
    )
    assert len(result["list"]) == 1
    assert client.addx_call.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [2, "2"])
async def test_valid_total_stops_without_extra_request(total):
    module = importlib.import_module(
        "custom_components.xsense.python_xsense.async_xsense"
    )
    client = object.__new__(module.AsyncXSense)
    client.addx_call = AsyncMock(
        return_value={"data": {"list": [{"id": 1}, {"id": 2}], "total": total}}
    )
    result = await client._get_camera_library_pages(
        ["CAM"], 1, 2, house=None, start=0, limit=2
    )
    assert len(result["list"]) == 2
    client.addx_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_total_continues_server_short_pages_without_losing_records():
    module = importlib.import_module(
        "custom_components.xsense.python_xsense.async_xsense"
    )
    client = object.__new__(module.AsyncXSense)
    client.addx_call = AsyncMock(
        side_effect=[
            {
                "list": [{"serialNumber": "CAM", "startTime": 123, "endTime": 124}],
                "total": 2,
            },
            {
                "list": [{"serialNumber": "CAM", "startTime": 123, "endTime": 125}],
                "total": 2,
            },
        ]
    )
    result = await client._get_camera_library_pages(
        ["CAM"], 1, 2, house=None, start=0, limit=20
    )
    assert len(result["list"]) == 2
    assert client.addx_call.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [True, -1, 1.5, "bad", None])
async def test_invalid_total_does_not_truncate_history(total):
    module = importlib.import_module(
        "custom_components.xsense.python_xsense.async_xsense"
    )
    client = object.__new__(module.AsyncXSense)
    client.addx_call = AsyncMock(
        side_effect=[
            {"list": [{"id": 1}], "total": total},
            {"list": [{"id": 2}], "total": total},
            {"list": [], "total": total},
        ]
    )
    result = await client._get_camera_library_pages(
        ["CAM"], 1, 2, house=None, start=0, limit=1
    )
    assert len(result["list"]) == 2
    assert client.addx_call.await_count == 3
