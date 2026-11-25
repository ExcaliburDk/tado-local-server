"""Unit tests for Tado Local Skills Engine."""

import pytest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tado_local.database import ensure_schema_and_migrate
from tado_local.skills import (
    SkillsManager,
    SKILL_OPEN_WINDOW,
    SKILL_AWAY_MODE,
    SKILL_EARLY_START,
    SKILL_AIR_COMFORT,
    SKILL_CARE_PROTECT,
)


@pytest.fixture
def skills_db(tmp_path):
    db_file = str(tmp_path / "test_skills.db")
    ensure_schema_and_migrate(db_file)
    return db_file


@pytest.fixture
def mock_api(skills_db):
    api = AsyncMock()
    api.db_path = skills_db
    api.set_zone_temperature = AsyncMock(return_value=True)
    api.broadcast_event = AsyncMock()

    # Mock state manager
    state_mgr = MagicMock()
    state_mgr.zone_cache = {
        1: {"zone_id": 1, "name": "Living Room", "leader_device_id": 10},
        2: {"zone_id": 2, "name": "Bedroom", "leader_device_id": 20},
    }
    state_mgr.get_current_state = MagicMock(return_value={
        "target_temperature": 21.0,
        "current_temperature": 19.5,
        "target_heating_cooling_state": 1,
        "current_heating_cooling_state": 0,
        "humidity": 55.0,
        "window": 0,
    })
    state_mgr.get_all_devices = MagicMock(return_value=[
        {"device_id": 10, "name": "Living Thermostat", "zone_name": "Living Room"},
        {"device_id": 20, "name": "Bed Thermostat", "zone_name": "Bedroom"},
    ])
    state_mgr.update_device_window_status = MagicMock()

    api.state_manager = state_mgr
    return api


class TestSkillsManager:
    """Test suite for SkillsManager operations."""

    def test_default_skills_seeded(self, skills_db):
        mgr = SkillsManager(skills_db)
        skills = mgr.get_skills()

        assert SKILL_OPEN_WINDOW in skills
        assert SKILL_AWAY_MODE in skills
        assert SKILL_EARLY_START in skills
        assert SKILL_AIR_COMFORT in skills
        assert SKILL_CARE_PROTECT in skills

        # Check default enabled
        assert skills[SKILL_OPEN_WINDOW]["enabled"] is True
        assert skills[SKILL_AWAY_MODE]["enabled"] is True

    def test_set_skill_enabled(self, skills_db):
        mgr = SkillsManager(skills_db)
        res = mgr.set_skill_enabled(SKILL_OPEN_WINDOW, False)
        assert res["enabled"] is False
        assert mgr.is_skill_enabled(SKILL_OPEN_WINDOW) is False

        res = mgr.set_skill_enabled(SKILL_OPEN_WINDOW, True)
        assert res["enabled"] is True
        assert mgr.is_skill_enabled(SKILL_OPEN_WINDOW) is True

    def test_update_skill_config(self, skills_db):
        mgr = SkillsManager(skills_db)
        res = mgr.update_skill_config(SKILL_AWAY_MODE, {"away_temp": 14.5})
        assert res["config"]["away_temp"] == 14.5

    @pytest.mark.asyncio
    async def test_away_mode_toggle(self, skills_db, mock_api):
        mgr = SkillsManager(skills_db, api=mock_api)

        # Activate away mode
        status = await mgr.set_away_mode(True, away_temp=15.0)
        assert status["is_away"] is True
        assert status["away_temp"] == 15.0
        assert mock_api.set_zone_temperature.call_count == 2
        mock_api.set_zone_temperature.assert_any_call(1, 15.0, heating_enabled=True)
        mock_api.set_zone_temperature.assert_any_call(2, 15.0, heating_enabled=True)

        # Deactivate away mode
        status = await mgr.set_away_mode(False)
        assert status["is_away"] is False

    @pytest.mark.asyncio
    async def test_open_window_trigger_and_restore(self, skills_db, mock_api):
        mgr = SkillsManager(skills_db, api=mock_api)
        device_info = {"zone_id": 1, "zone_name": "Living Room", "window_open_time": 15}

        # Trigger open window
        await mgr.trigger_open_window(zone_id=1, device_id=10, device_info=device_info)
        assert 1 in mgr.window_saved_states
        mock_api.set_zone_temperature.assert_called_with(1, 5.0, heating_enabled=False)
        mock_api.state_manager.update_device_window_status.assert_called_with(10, 1)

        # Active windows status
        active = mgr.get_open_window_status()
        assert 1 in active
        assert active[1]["duration_minutes"] == 15

        # Restore open window
        await mgr.restore_open_window(zone_id=1)
        assert 1 not in mgr.window_saved_states
        mock_api.state_manager.update_device_window_status.assert_called_with(10, 2)

    def test_early_start_calculation(self, skills_db):
        mgr = SkillsManager(skills_db)

        # Deficit is 2.0°C (21 - 19), 20 min until block <= 30 min lookahead -> Should preheat
        preheat = mgr.check_early_start(zone_id=1, current_temp=19.0, next_block_temp=21.0, minutes_until_block=20)
        assert preheat == 21.0

        # Minutes until block (45) > lookahead (30) -> Should NOT preheat yet
        preheat_far = mgr.check_early_start(zone_id=1, current_temp=19.0, next_block_temp=21.0, minutes_until_block=45)
        assert preheat_far is None

        # Deficit < 0.5°C -> Should NOT preheat
        preheat_small = mgr.check_early_start(zone_id=1, current_temp=20.8, next_block_temp=21.0, minutes_until_block=15)
        assert preheat_small is None

    def test_air_comfort_and_mold_risk(self, skills_db):
        mgr = SkillsManager(skills_db)

        # High humidity test
        res_high = mgr.evaluate_zone_air_comfort(temp=20.0, humidity=75.0)
        assert res_high["mold_risk"] == "high"
        assert res_high["recommendation"] == "ventilate"
        assert res_high["dew_point"] is not None

        # Normal humidity test
        res_norm = mgr.evaluate_zone_air_comfort(temp=21.0, humidity=48.0)
        assert res_norm["mold_risk"] == "low"
        assert res_norm["recommendation"] == "optimal"

        # Dry air test
        res_dry = mgr.evaluate_zone_air_comfort(temp=22.0, humidity=25.0)
        assert res_dry["recommendation"] == "too_dry"

    def test_care_and_protect_diagnostics(self, skills_db, mock_api):
        mgr = SkillsManager(skills_db, api=mock_api)

        # Normal condition
        diag = mgr.get_diagnostics()
        assert diag["status"] == "ok"
        assert len(diag["anomalies"]) == 0

        # Inject low battery device
        mock_api.state_manager.get_current_state = MagicMock(return_value={
            "battery_state": "LOW",
            "battery_level": 10,
        })
        diag_warn = mgr.get_diagnostics()
        assert diag_warn["status"] == "warning"
        assert len(diag_warn["anomalies"]) >= 1
