"""Regressions for setup recovery, device metadata and notification routing."""

from http import HTTPStatus
from unittest.mock import patch

import httpx
import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.core import valid_entity_id
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikvision_next.const import DOMAIN
from custom_components.hikvision_next.isapi import ISAPIUnauthorizedError
from custom_components.hikvision_next.notifications import EventNotificationsView
from custom_components.hikvision_next.sensor import AlarmServerSensor

from .conftest import TEST_CONFIG, TEST_CONFIG_WITH_ALARM_SERVER
from .test_notifications import mock_event_notification


@pytest.mark.parametrize("error", [httpx.ReadError("disconnected"), httpx.RemoteProtocolError("disconnected")])
@pytest.mark.parametrize("mock_config_entry", [TEST_CONFIG_WITH_ALARM_SERVER], indirect=True)
@pytest.mark.parametrize("init_integration", [("DS-2CD2386G2-IU", True)], indirect=True)
async def test_alarm_server_transport_failure_retries(hass, init_integration, error):
    """A disconnect after discovery must retry instead of leaving setup_error."""
    entry = init_integration
    with patch("custom_components.hikvision_next.isapi.ISAPIClient.set_alarm_server", side_effect=error):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert not hasattr(entry, "runtime_data")

    with patch("custom_components.hikvision_next.isapi.ISAPIClient.set_alarm_server"):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("mock_config_entry", [TEST_CONFIG_WITH_ALARM_SERVER], indirect=True)
@pytest.mark.parametrize("init_integration", [("DS-2CD2386G2-IU", True)], indirect=True)
async def test_alarm_server_auth_failure_requests_reauth(hass, init_integration):
    """An authentication failure in late setup must follow HA's reauth path."""
    request = httpx.Request("GET", "http://example.test/ISAPI/Event/notification/httpHosts")
    error = ISAPIUnauthorizedError(httpx.HTTPStatusError(
        "Unauthorized", request=request, response=httpx.Response(401, request=request)
    ))
    with patch("custom_components.hikvision_next.isapi.ISAPIClient.set_alarm_server", side_effect=error):
        assert not await hass.config_entries.async_setup(init_integration.entry_id)
        assert init_integration.state is ConfigEntryState.SETUP_ERROR
        await hass.async_block_till_done()
    assert any(flow["context"].get("source") == "reauth" for flow in hass.config_entries.flow.async_progress())


@pytest.mark.parametrize("init_integration", ["DS-7608NXI-I2", "DS-2CD2386G2-IU"], indirect=True)
async def test_registered_entities_and_parent_links(hass, init_integration, caplog):
    """NVR children link to the registry parent; standalone cameras have no via key."""
    device = init_integration.runtime_data
    registry = dr.async_get(hass)
    parent = registry.async_get(device.registry_device_id)
    entities = er.async_entries_for_config_entry(er.async_get(hass), init_integration.entry_id)
    assert entities
    assert any(entity.domain == "image" for entity in entities)
    for entity in entities:
        assert valid_entity_id(entity.entity_id)
    for camera in device.cameras:
        info = device.hass_device_info(camera.id)
        assert "via_device" not in info
        if device.device_info.is_nvr:
            assert info["via_device_id"] == parent.id
        else:
            assert "via_device_id" not in info
    assert "Error adding entity" not in caplog.text
    assert "sets an invalid entity ID" not in caplog.text
    assert "sets an entity ID with wrong domain" not in caplog.text


@pytest.mark.parametrize("init_integration", ["DS-2CD2386G2-IU"], indirect=True)
async def test_failed_entry_does_not_block_renamed_sensor(hass, init_integration, caplog):
    """An unloaded first entry cannot swallow another camera's valid event."""
    failed = MockConfigEntry(domain=DOMAIN, data=TEST_CONFIG, version=3)
    original = "binary_sensor.ds_2cd2386g2_iu00000000aawrj00000000_1_fielddetection"
    renamed = "binary_sensor.renamed_intrusion"
    er.async_get(hass).async_update_entity(original, new_entity_id=renamed)
    await hass.async_block_till_done()
    view = EventNotificationsView(hass)
    with patch.object(hass.config_entries, "async_entries", return_value=[failed, init_integration]):
        response = await view.post(mock_event_notification("ipc_1_fielddetection"))
    assert response.status == HTTPStatus.OK
    assert hass.states.get(renamed).state == STATE_ON
    assert "Cannot process incoming event" not in caplog.text


@pytest.mark.parametrize("init_integration", ["DS-2CD2386G2-IU"], indirect=True)
async def test_unknown_sender_is_not_assigned_to_only_camera(hass, init_integration, caplog):
    """The only configured camera must still match the notification sender."""
    view = EventNotificationsView(hass)
    request = mock_event_notification("ipc_1_fielddetection")
    request.remote = "192.0.2.200"
    original_read = request.read

    async def read():
        data = await original_read()
        return data.replace(init_integration.runtime_data.device_info.mac_address.encode(), b"00:00:00:00:00:01")

    request.read = read
    with patch.object(view, "trigger_sensor") as trigger:
        assert (await view.post(request)).status == HTTPStatus.OK
        trigger.assert_not_called()
    assert "Cannot process incoming event" not in caplog.text


@pytest.mark.parametrize("init_integration", ["DS-2CD2386G2-IU"], indirect=True)
async def test_unconfigured_event_channel_is_quiet(hass, init_integration, caplog):
    """A valid notification can refer to a channel without a configured entity."""
    request = mock_event_notification("ipc_1_fielddetection")
    original_read = request.read

    async def read():
        return (await original_read()).replace(b"<channelID>1</channelID>", b"<channelID>99</channelID>")

    request.read = read
    assert (await EventNotificationsView(hass).post(request)).status == HTTPStatus.OK
    assert "Cannot process incoming event" not in caplog.text


@pytest.mark.parametrize("init_integration", ["DS-2CD2386G2-IU"], indirect=True)
async def test_alarm_sensor_handles_failed_poll(hass, init_integration):
    """Missing alarm-host data after a failed poll must not crash the entity."""
    coordinator = init_integration.runtime_data.coordinators["secondary"]
    sensor = AlarmServerSensor(coordinator, "address")
    coordinator.async_set_updated_data({})
    assert sensor.native_value is None
