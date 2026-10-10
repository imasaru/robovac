"""Tests for T2194 (L35 Hybrid) room cleaning, status mapping and sleep handling."""

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.vacuum import VacuumActivity, VacuumEntityFeature
from homeassistant.const import CONF_DESCRIPTION, CONF_MODEL
from homeassistant.exceptions import ServiceValidationError

from custom_components.robovac.const import CONF_ROOM_SEGMENT_MAP_ID, CONF_ROOM_SEGMENTS
from custom_components.robovac.robovac import RoboVac
from custom_components.robovac.tuyalocalapi import TuyaException
from custom_components.robovac.vacuum import (
    UPDATE_RETRIES,
    RoboVacEntity,
    _normalize_room_ids,
    _parse_clean_count,
)
from custom_components.robovac.vacuums import ROBOVAC_MODELS
from custom_components.robovac.vacuums.base import RobovacModelDetails


def _make_robovac(model_code: str = "T2194") -> RoboVac:
    with patch("custom_components.robovac.robovac.TuyaDevice.__init__", return_value=None):
        robovac = RoboVac(
            model_code=model_code,
            device_id="test_id",
            host="192.168.1.100",
            local_key="test_key",
        )
    robovac._dps = {}
    robovac.async_set = AsyncMock(return_value=True)  # type: ignore[method-assign]
    robovac.async_get = AsyncMock(return_value={})  # type: ignore[method-assign]
    return robovac


def _make_entity(
    mock_vacuum_data: dict[str, Any], robovac: RoboVac, **extra: Any
) -> RoboVacEntity:
    data = {
        **mock_vacuum_data,
        CONF_MODEL: robovac.model_code,
        CONF_DESCRIPTION: "RoboVac L35 Hybrid",
        **extra,
    }
    with patch("custom_components.robovac.vacuum.RoboVac", return_value=robovac):
        return RoboVacEntity(data)


def _sent_room_clean(robovac: RoboVac) -> dict[str, Any]:
    """Decode the selectRoomsClean payload written to DPS 124."""
    calls = robovac.async_set.await_args_list  # type: ignore[attr-defined]
    payload = calls[0].args[0]["124"]
    return json.loads(base64.b64decode(payload).decode("utf8"))


@pytest.fixture
def no_sleep():
    with patch("custom_components.robovac.vacuum.asyncio.sleep", new=AsyncMock()) as sleep:
        yield sleep


# --- Features / fallback segments -------------------------------------------------


def test_t2194_model_opts_in_to_room_fallback() -> None:
    """T2194 declares the explicit Room 1-31 fallback; the default is off."""
    assert ROBOVAC_MODELS["T2194"].room_clean_fallback_room_count == 31
    assert RobovacModelDetails.room_clean_fallback_room_count == 0
    assert _make_robovac("T2194").getRoomCleanFallbackRoomCount() == 31
    assert _make_robovac("T2193").getRoomCleanFallbackRoomCount() == 0


def test_t2194_entity_supports_clean_area_without_segments(mock_vacuum_data) -> None:
    """The L35 keeps CLEAN_AREA with no configured segments (fallback rooms)."""
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2194"))
    assert entity.supported_features & VacuumEntityFeature.CLEAN_AREA


def test_non_fallback_model_has_no_clean_area_without_segments(mock_vacuum_data) -> None:
    """Models that don't opt in only get CLEAN_AREA once segments are configured."""
    robovac = _make_robovac("T2193")
    entity = _make_entity(mock_vacuum_data, robovac)
    assert not entity.supported_features & VacuumEntityFeature.CLEAN_AREA

    entity = _make_entity(mock_vacuum_data, robovac, **{CONF_ROOM_SEGMENTS: "1:Kitchen"})
    assert entity.supported_features & VacuumEntityFeature.CLEAN_AREA


@pytest.mark.asyncio
async def test_t2194_fallback_segments_room_1_to_31(mock_vacuum_data) -> None:
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2194"))
    segments = await entity.async_get_segments()
    assert [(s.id, s.name) for s in segments] == [
        (str(i), f"Room {i}") for i in range(1, 32)
    ]


@pytest.mark.asyncio
async def test_non_fallback_model_has_no_fallback_segments(mock_vacuum_data) -> None:
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2193"))
    assert await entity.async_get_segments() == []


@pytest.mark.asyncio
async def test_configured_segments_override_fallback(mock_vacuum_data) -> None:
    entity = _make_entity(
        mock_vacuum_data,
        _make_robovac("T2194"),
        **{CONF_ROOM_SEGMENTS: "3:Kitchen,5:Hall", CONF_ROOM_SEGMENT_MAP_ID: 2},
    )
    segments = await entity.async_get_segments()
    assert [(s.id, s.name) for s in segments] == [("3", "Kitchen"), ("5", "Hall")]


# --- clean_area (async_clean_segments) through the fallback path -------------------


@pytest.mark.asyncio
async def test_fallback_clean_segments_converts_string_ids(
    mock_vacuum_data, no_sleep
) -> None:
    """String segment ids from HA become ints in the selectRoomsClean payload."""
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    await entity.async_clean_segments(["2", "7"])

    method_call = _sent_room_clean(robovac)
    assert method_call["method"] == "selectRoomsClean"
    assert method_call["data"] == {"roomIds": [2, 7], "cleanTimes": 1}
    robovac.async_set.assert_any_await({"2": True})  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_fallback_clean_segments_includes_configured_map_id(
    mock_vacuum_data, no_sleep
) -> None:
    """The configured map id is forwarded on the fallback path (user setup: map 1)."""
    robovac = _make_robovac("T2194")
    entity = _make_entity(
        mock_vacuum_data,
        robovac,
        **{CONF_ROOM_SEGMENTS: "", CONF_ROOM_SEGMENT_MAP_ID: 1},
    )

    await entity.async_clean_segments(["4"], repeats=2)

    assert _sent_room_clean(robovac)["data"] == {
        "roomIds": [4],
        "cleanTimes": 2,
        "mapId": 1,
    }


@pytest.mark.asyncio
async def test_fallback_clean_segments_ignores_unknown_and_invalid_ids(
    mock_vacuum_data, no_sleep
) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    await entity.async_clean_segments(["bad", "32", "0", "3", "3"])

    assert _sent_room_clean(robovac)["data"]["roomIds"] == [3]


@pytest.mark.asyncio
async def test_fallback_all_invalid_ids_send_nothing(mock_vacuum_data, no_sleep) -> None:
    """No valid ids: nothing is written, in particular no DPS 2 start."""
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    await entity.async_clean_segments(["bad", "99", "-1"])

    robovac.async_set.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, 1), (-3, 1), ("abc", 1), (None, 1), (True, 1), (2.5, 1), ("3", 3), (2.0, 2)],
)
async def test_fallback_bad_clean_times_are_validated(
    mock_vacuum_data, no_sleep, count, expected
) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    await entity.async_clean_segments(["1"], count=count)

    assert _sent_room_clean(robovac)["data"]["cleanTimes"] == expected


# --- roomClean send_command validation --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"count": 2},  # missing roomIds key: must NOT default to room 1
        {"roomIds": []},
        {"roomIds": ["kitchen", None, True, "-2"]},
        [],
        [{"count": 1}],
    ],
)
async def test_room_clean_without_valid_ids_sends_nothing(
    mock_vacuum_data, no_sleep, params
) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    with pytest.raises(ServiceValidationError):
        await entity.async_send_command("roomClean", params)

    robovac.async_set.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_room_clean_converts_string_ids_and_count(mock_vacuum_data, no_sleep) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    await entity.async_send_command(
        "roomClean", {"roomIds": ["1", " 4 ", 4, "x"], "count": "2", "mapId": "1"}
    )

    assert _sent_room_clean(robovac)["data"] == {
        "roomIds": [1, 4],
        "cleanTimes": 2,
        "mapId": 1,
    }


@pytest.mark.asyncio
async def test_room_clean_single_scalar_id(mock_vacuum_data, no_sleep) -> None:
    """A scalar id like '12' is one room, not the digits 1 and 2."""
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    await entity.async_send_command("roomClean", {"roomIds": "12"})

    assert _sent_room_clean(robovac)["data"]["roomIds"] == [12]


@pytest.mark.asyncio
async def test_room_clean_rejects_invalid_map_id(mock_vacuum_data, no_sleep) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    with pytest.raises(ServiceValidationError):
        await entity.async_send_command("roomClean", {"roomIds": [1], "mapId": "main"})

    robovac.async_set.assert_not_awaited()  # type: ignore[attr-defined]


def test_normalize_room_ids_helper() -> None:
    assert _normalize_room_ids(["1", 2, 2.0, "x", False, -1]) == [1, 2]
    assert _normalize_room_ids([1, 2, 3], known_room_ids={2}) == [2]
    assert _normalize_room_ids(None) == []
    assert _normalize_room_ids({"a": 1}) == []


def test_parse_clean_count_helper() -> None:
    assert _parse_clean_count(1) == 1
    assert _parse_clean_count("2") == 2
    assert _parse_clean_count(0) == 1
    assert _parse_clean_count("nope") == 1


# --- start command flag -----------------------------------------------------------


def test_room_clean_send_start_defaults_to_true() -> None:
    assert RobovacModelDetails.room_clean_send_start is True
    assert _make_robovac("T2194").getRoomCleanSendStart() is True
    assert _make_robovac("T2193").getRoomCleanSendStart() is True


@pytest.mark.asyncio
async def test_room_clean_sends_start_by_default(mock_vacuum_data, no_sleep) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    await entity.async_send_command("roomClean", {"roomIds": [1]})

    calls = robovac.async_set.await_args_list  # type: ignore[attr-defined]
    assert len(calls) == 2
    assert "124" in calls[0].args[0]
    assert calls[1].args[0] == {"2": True}
    no_sleep.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_room_clean_skips_start_when_model_flag_off(
    mock_vacuum_data, no_sleep
) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    with patch.object(ROBOVAC_MODELS["T2194"], "room_clean_send_start", False):
        assert robovac.getRoomCleanSendStart() is False
        await entity.async_send_command("roomClean", {"roomIds": [1]})

    calls = robovac.async_set.await_args_list  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert "124" in calls[0].args[0]
    no_sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("override", "expected_calls"), [(False, 1), (True, 2)])
async def test_room_clean_send_start_param_overrides_model(
    mock_vacuum_data, no_sleep, override, expected_calls
) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    with patch.object(ROBOVAC_MODELS["T2194"], "room_clean_send_start", not override):
        await entity.async_send_command(
            "roomClean", {"roomIds": [1], "send_start": override}
        )

    assert len(robovac.async_set.await_args_list) == expected_calls  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_room_clean_invalid_send_start_sends_nothing(
    mock_vacuum_data, no_sleep
) -> None:
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)

    with pytest.raises(ServiceValidationError):
        await entity.async_send_command("roomClean", {"roomIds": [1], "sendStart": "no"})

    robovac.async_set.assert_not_awaited()  # type: ignore[attr-defined]


# --- T2194 status mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("Charging", VacuumActivity.DOCKED),
        ("completed", VacuumActivity.DOCKED),
        ("Running", VacuumActivity.CLEANING),
        ("Recharge", VacuumActivity.RETURNING),
        ("standby", VacuumActivity.IDLE),
        ("Sleeping", VacuumActivity.IDLE),
        ("recharge_needed", VacuumActivity.IDLE),
    ],
)
def test_t2194_status_dps_to_activity(mock_vacuum_data, raw_status, expected) -> None:
    """Raw DPS 15 values map through human-readable names to HA activities."""
    robovac = _make_robovac("T2194")
    entity = _make_entity(mock_vacuum_data, robovac)
    robovac._dps = {"15": raw_status}

    entity.update_entity_values()

    assert entity.activity == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("Charging", VacuumActivity.DOCKED),
        ("Completed", VacuumActivity.DOCKED),
        ("Running", VacuumActivity.CLEANING),
        ("Returning to Dock", VacuumActivity.RETURNING),
        ("Standby", VacuumActivity.IDLE),
        ("Sleeping", VacuumActivity.IDLE),
        ("Recharge needed", VacuumActivity.IDLE),
        ("charging", VacuumActivity.DOCKED),  # case-insensitive mapping lookup
        ("STANDBY", VacuumActivity.IDLE),
    ],
)
def test_t2194_activity_mapping(mock_vacuum_data, state, expected) -> None:
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2194"))
    entity.tuya_state = state
    assert entity.activity == expected


@pytest.mark.parametrize("state", ["Standby", "Sleeping", "Recharge needed"])
def test_t2194_idle_status_not_overridden_by_stale_mode(mock_vacuum_data, state) -> None:
    """A leftover 'auto' mode must not turn an idle L35 into phantom cleaning."""
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2194"))
    entity.tuya_state = state
    entity._attr_mode = "auto"
    assert entity.activity == VacuumActivity.IDLE


# --- case-insensitive fallback (models without activity_mapping) ------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("CHARGING", VacuumActivity.DOCKED),
        ("Completed", VacuumActivity.DOCKED),
        ("Recharging", VacuumActivity.DOCKED),
        ("RECHARGE", VacuumActivity.RETURNING),
        ("Going_To_Recharge", VacuumActivity.RETURNING),
        ("Standby", VacuumActivity.IDLE),
        ("sleeping", VacuumActivity.IDLE),
        ("Idle", VacuumActivity.IDLE),
        ("Recharge Needed", VacuumActivity.IDLE),
        ("PAUSED", VacuumActivity.PAUSED),
        ("Auto cleaning", VacuumActivity.CLEANING),
    ],
)
def test_case_insensitive_state_fallback(
    mock_robovac, mock_vacuum_data, state, expected
) -> None:
    with patch("custom_components.robovac.vacuum.RoboVac", return_value=mock_robovac):
        entity = RoboVacEntity(mock_vacuum_data)
    assert entity.activity_mapping is None
    entity.tuya_state = state
    assert entity.activity == expected


# --- connection errors while sleeping ---------------------------------------------


@pytest.mark.parametrize("state", ["Standby", "standby", "Sleeping", "SLEEPING"])
def test_connection_failed_while_sleeping_is_idle(mock_vacuum_data, state) -> None:
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2194"))
    entity.tuya_state = state
    entity.error_code = "CONNECTION_FAILED"
    assert entity.activity == VacuumActivity.IDLE


def test_connection_failed_while_running_is_error(mock_vacuum_data) -> None:
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2194"))
    entity.tuya_state = "Running"
    entity.error_code = "CONNECTION_FAILED"
    assert entity.activity == VacuumActivity.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected_error"),
    [("Standby", None), ("Sleeping", None), ("Running", "CONNECTION_FAILED")],
)
async def test_max_retries_ignored_while_sleeping(
    mock_vacuum_data, state, expected_error
) -> None:
    robovac = _make_robovac("T2194")
    robovac.async_get = AsyncMock(side_effect=TuyaException("timeout"))  # type: ignore[method-assign]
    entity = _make_entity(mock_vacuum_data, robovac)
    entity.tuya_state = state
    entity._attr_error_code = None

    for _ in range(UPDATE_RETRIES):
        await entity.async_update()

    assert entity.update_failures == UPDATE_RETRIES
    assert entity._attr_error_code == expected_error


def test_raw_tuya_state_attribute(mock_vacuum_data) -> None:
    entity = _make_entity(mock_vacuum_data, _make_robovac("T2194"))
    entity.tuya_state = "Standby"
    assert entity.extra_state_attributes["raw_tuya_state"] == "Standby"
