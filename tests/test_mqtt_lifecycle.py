"""Exercise the broker-facing MQTT subscription lifecycle."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from custom_components.xsense import mqtt


@callback
def receive_message(_message):
    """Receive a test subscription message."""


class BrokerClient:
    """Return Paho results and deliver SUBACKs on the event loop."""

    def __init__(self, loop):
        self.loop = loop
        self.calls = []
        self.result = 0
        self.reasons = [1]
        self.mid = 0

    def subscribe(self, topics):
        self.calls.append(topics)
        self.mid += 1
        if self.result:
            return self.result, None
        if self.reasons is not None:
            self.loop.call_soon(
                self.on_subscribe, self, None, self.mid, self.reasons, None
            )
        return 0, self.mid

    def publish(self, *_args):
        self.mid += 1
        self.on_publish(self, None, self.mid)
        return SimpleNamespace(mid=self.mid, rc=0)


def client():
    loop = asyncio.get_running_loop()
    broker = BrokerClient(loop)
    result = mqtt.XSenseMQTT(
        SimpleNamespace(loop=loop),
        SimpleNamespace(title="Test account"),
        SimpleNamespace(client=broker),
    )
    result.init_client()
    return result, broker


@pytest.mark.asyncio
async def test_connected_subscription_uses_qos_and_consumes_suback():
    connection, broker = client()
    connection.connected = True

    await connection.async_subscribe("home/events", receive_message, 1)

    assert broker.calls == [[("home/events", 1)]]
    assert connection.is_subscribed("home/events")
    assert connection._pending_operations == {}
    broker.on_subscribe(broker, None, broker.mid, [1], None)
    assert connection._pending_operations == {}


@pytest.mark.asyncio
async def test_reconnect_subscriptions_preserve_each_requested_qos():
    connection, broker = client()
    await connection.async_subscribe("home/events", receive_message, 1)
    await connection.async_subscribe("home/state", receive_message, 0)
    assert broker.calls == []

    await connection._async_perform_subscriptions()

    assert broker.calls == [[("home/events", 1)], [("home/state", 0)]]
    assert connection._pending_operations == {}


@pytest.mark.asyncio
async def test_send_failure_untracks_subscription_and_allows_retry():
    connection, broker = client()
    connection.connected = True
    broker.result = mqtt.mqtt.MQTT_ERR_NO_CONN

    with pytest.raises(HomeAssistantError):
        await connection.async_subscribe("home/events", receive_message, 1)

    assert not connection.is_subscribed("home/events")
    assert connection._pending_operations == {}
    broker.result = 0
    await connection.async_subscribe("home/events", receive_message, 1)
    assert connection.is_subscribed("home/events")
    assert len(connection.subscriptions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [128, SimpleNamespace(value=135)])
async def test_rejected_suback_untracks_subscription(reason):
    connection, broker = client()
    connection.connected = True
    broker.reasons = [reason]

    with pytest.raises(HomeAssistantError):
        await connection.async_subscribe("home/events", receive_message, 1)

    assert not connection.is_subscribed("home/events")
    assert connection._pending_operations == {}


@pytest.mark.asyncio
async def test_suback_timeout_untracks_and_ignores_late_ack(monkeypatch):
    connection, broker = client()
    connection.connected = True
    broker.reasons = None
    monkeypatch.setattr(mqtt, "TIMEOUT_ACK", 0.001)

    with pytest.raises(TimeoutError):
        await connection.async_subscribe("home/events", receive_message, 1)

    assert not connection.is_subscribed("home/events")
    assert connection._pending_operations == {}
    broker.on_subscribe(broker, None, broker.mid, [1], None)
    assert connection._pending_operations == {}


@pytest.mark.asyncio
async def test_cancelled_subscription_removes_waiter_and_tracking():
    connection, broker = client()
    connection.connected = True
    broker.reasons = None
    task = asyncio.create_task(
        connection.async_subscribe("home/events", receive_message, 1)
    )
    await asyncio.sleep(0)
    assert connection._pending_operations
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection._pending_operations == {}
    assert not connection.is_subscribed("home/events")
    broker.on_subscribe(broker, None, broker.mid, [1], None)
    assert connection._pending_operations == {}


@pytest.mark.asyncio
async def test_reconnect_rejection_is_not_kept_as_subscribed():
    connection, broker = client()
    await connection.async_subscribe("home/events", receive_message, 1)
    broker.reasons = [128]

    await connection._async_perform_subscriptions()

    assert not connection.is_subscribed("home/events")
    assert connection._pending_operations == {}


@pytest.mark.asyncio
async def test_publish_callback_before_wait_is_still_consumed():
    connection, _broker = client()

    await connection.async_publish("home/request", "{}", 0, False)

    assert connection._pending_operations == {}


@pytest.mark.asyncio
async def test_cancelled_reconnect_batch_retains_intent_and_retries():
    connection, broker = client()
    await connection.async_subscribe("home/events", receive_message, 1)
    await connection.async_subscribe("home/state", receive_message, 0)
    assert broker.calls == []
    assert connection.is_subscribed("home/events")
    assert connection.is_subscribed("home/state")
    broker.reasons = None

    task = asyncio.create_task(connection._async_perform_subscriptions())
    await asyncio.sleep(0)
    assert connection._pending_operations
    cancelled_mid = broker.mid
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection._pending_operations == {}
    assert connection.is_subscribed("home/events")
    assert connection.is_subscribed("home/state")
    broker.on_subscribe(broker, None, cancelled_mid, [1], None)
    assert connection._pending_operations == {}

    broker.reasons = [1]
    await connection._async_perform_subscriptions()

    assert broker.calls == [
        [("home/events", 1)],
        [("home/events", 1)],
        [("home/state", 0)],
    ]
    assert connection._pending_operations == {}
    assert len(connection.subscriptions) == 2
