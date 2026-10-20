"""Outcome tests: a sleeping/standby vacuum is not reported as CONNECTION_FAILED.

Drives RoboVacEntity.async_update(); only the device boundary (RoboVac) is mocked.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.vacuum import VacuumActivity

from custom_components.robovac.tuyalocalapi import ResponseTimeoutException
from custom_components.robovac.vacuum import UPDATE_RETRIES, RoboVacEntity


async def _entity_after_timeouts(mock_robovac, mock_vacuum_data, raw_status: str):
    with patch("custom_components.robovac.vacuum.RoboVac", return_value=mock_robovac):
        entity = RoboVacEntity(mock_vacuum_data)
    entity.hass = None

    # One good poll reports the raw status, then the device stops answering.
    mock_robovac._dps.update({entity.get_dps_code("STATUS"): raw_status})
    await entity.async_update()
    assert entity.error_code != "CONNECTION_FAILED"

    mock_robovac.async_get = AsyncMock(
        side_effect=ResponseTimeoutException("Timed out waiting for response")
    )
    for _ in range(UPDATE_RETRIES + 2):
        await entity.async_update()
    return entity


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_status", ["Standby", "standby", "STANDBY", "Sleeping", "sleeping"])
async def test_sleeping_vacuum_is_not_connection_failed(
    mock_robovac, mock_vacuum_data, raw_status
) -> None:
    entity = await _entity_after_timeouts(mock_robovac, mock_vacuum_data, raw_status)

    assert entity.error_code != "CONNECTION_FAILED"
    assert entity.activity == VacuumActivity.IDLE


@pytest.mark.asyncio
async def test_awake_vacuum_still_reports_connection_failed(
    mock_robovac, mock_vacuum_data
) -> None:
    entity = await _entity_after_timeouts(mock_robovac, mock_vacuum_data, "Running")

    assert entity.error_code == "CONNECTION_FAILED"
    assert entity.activity == VacuumActivity.ERROR
