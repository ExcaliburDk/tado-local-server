"""
Tado Local Server - Skills Engine

Implements native local versions of Tado App Skills:
1. Open Window Detection (local Auto-Assist heating shut-off)
2. Away Mode (home/away toggle with eco temperature)
3. Early Start (smart preheating ahead of schedule blocks)
4. Air Comfort & Mold Risk (dew point, mold risk, ventilation guidance)
5. Care & Protect (heating performance diagnostics, stall detection, battery health)
"""

import asyncio
import datetime
import json
import logging
import math
import sqlite3
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tado_local.skills")

# Skill identifiers
SKILL_OPEN_WINDOW = "open_window"
SKILL_AWAY_MODE = "away_mode"
SKILL_EARLY_START = "early_start"
SKILL_AIR_COMFORT = "air_comfort"
SKILL_CARE_PROTECT = "care_protect"

ALL_SKILLS = [
    SKILL_OPEN_WINDOW,
    SKILL_AWAY_MODE,
    SKILL_EARLY_START,
    SKILL_AIR_COMFORT,
    SKILL_CARE_PROTECT,
]

DEFAULT_SKILLS_META = {
    SKILL_OPEN_WINDOW: {
        "name": "Open Window Detection",
        "description": "Automatically pauses heating in a zone when a sudden temperature drop (open window) is detected.",
        "icon": "🪟",
        "default_enabled": True,
        "default_config": {
            "action": "frost_protect",  # "frost_protect" (5.0°C) or "turn_off" (mode 0)
            "frost_temp": 5.0,
            "temp_drop_threshold": 1.0,  # °C drop to trigger
            "time_window_minutes": 20,  # minutes window to measure drop
            "default_open_duration": 30,  # minutes to keep heating paused
            "default_rest_duration": 15,  # minutes of rest before re-triggering
        },
    },
    SKILL_AWAY_MODE: {
        "name": "Away Mode",
        "description": "Lowers target temperature across zones to an eco temperature when you leave home.",
        "icon": "🚶",
        "default_enabled": True,
        "default_config": {
            "is_away": False,
            "away_temp": 16.0,
            "resume_schedule_on_home": True,
        },
    },
    SKILL_EARLY_START: {
        "name": "Early Start",
        "description": "Intelligently starts preheating before scheduled time blocks so rooms reach target temperature on time.",
        "icon": "⏱️",
        "default_enabled": True,
        "default_config": {
            "lookahead_minutes": 30,
            "min_temp_deficit": 0.5,
        },
    },
    SKILL_AIR_COMFORT: {
        "name": "Air Comfort & Mold Risk",
        "description": "Monitors room humidity and dew point to alert against high mold risk and advise ventilation.",
        "icon": "🍃",
        "default_enabled": True,
        "default_config": {
            "mold_risk_humidity": 70.0,
            "dry_air_humidity": 30.0,
        },
    },
    SKILL_CARE_PROTECT: {
        "name": "Care & Protect",
        "description": "Monitors heating performance, detects stalled radiators/heating anomalies, and tracks battery health.",
        "icon": "🛡️",
        "default_enabled": True,
        "default_config": {
            "anomaly_timeout_minutes": 60,
            "min_temp_increase": 0.2,
            "low_battery_threshold": 15,
        },
    },
}


class SkillsManager:
    """Manages all local Tado skills, their persistence, and runtime behaviors."""

    def __init__(self, db_path: str, api=None):
        self.db_path = db_path
        self.api = api
        self.window_saved_states: Dict[int, Dict[str, Any]] = {}  # zone_id -> saved state
        self.window_timers: Dict[int, asyncio.Task] = {}  # zone_id -> close timer task
        self.window_open_timestamps: Dict[int, float] = {}  # zone_id -> opened_at timestamp
        self.away_saved_states: Dict[int, Dict[str, Any]] = {}  # zone_id -> saved state
        self.heating_start_times: Dict[int, Dict[str, Any]] = {}  # zone_id -> {'time': ts, 'start_temp': temp}

        self._ensure_default_skills()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_default_skills(self):
        """Seed default rows in skills_config if not already present."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            for skill_id, meta in DEFAULT_SKILLS_META.items():
                cur.execute("SELECT skill_id FROM skills_config WHERE skill_id = ?", (skill_id,))
                if not cur.fetchone():
                    cur.execute(
                        """
                        INSERT INTO skills_config (skill_id, enabled, config_json)
                        VALUES (?, ?, ?)
                        """,
                        (skill_id, 1 if meta["default_enabled"] else 0, json.dumps(meta["default_config"])),
                    )
            conn.commit()
        except Exception as e:
            logger.error(f"Error seeding default skills: {e}")
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # Generic Skill CRUD
    # -------------------------------------------------------------------------

    def get_skills(self) -> Dict[str, Dict[str, Any]]:
        """Return all skills with metadata and current status."""
        conn = self._get_connection()
        skills = {}
        try:
            cur = conn.cursor()
            cur.execute("SELECT skill_id, enabled, config_json, updated_at FROM skills_config")
            rows = {r["skill_id"]: r for r in cur.fetchall()}

            for skill_id, meta in DEFAULT_SKILLS_META.items():
                row = rows.get(skill_id)
                enabled = bool(row["enabled"]) if row else meta["default_enabled"]
                config = json.loads(row["config_json"]) if row and row["config_json"] else meta["default_config"].copy()
                updated_at = row["updated_at"] if row else None

                skills[skill_id] = {
                    "id": skill_id,
                    "name": meta["name"],
                    "description": meta["description"],
                    "icon": meta["icon"],
                    "enabled": enabled,
                    "config": config,
                    "updated_at": updated_at,
                }
        finally:
            conn.close()
        return skills

    def get_skill(self, skill_id: str) -> Optional[Dict[str, Any]]:
        """Return single skill info."""
        all_s = self.get_skills()
        return all_s.get(skill_id)

    def is_skill_enabled(self, skill_id: str, zone_id: Optional[int] = None) -> bool:
        """Check if skill is enabled globally and (optionally) for a specific zone."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT enabled FROM skills_config WHERE skill_id = ?", (skill_id,))
            row = cur.fetchone()
            if not row or not row["enabled"]:
                return False

            if zone_id is not None:
                cur.execute(
                    "SELECT enabled FROM zone_skills_config WHERE zone_id = ? AND skill_id = ?",
                    (zone_id, skill_id),
                )
                z_row = cur.fetchone()
                if z_row is not None and not z_row["enabled"]:
                    return False

            return True
        finally:
            conn.close()

    def set_skill_enabled(self, skill_id: str, enabled: bool) -> Dict[str, Any]:
        """Toggle a skill on or off globally."""
        if skill_id not in DEFAULT_SKILLS_META:
            raise ValueError(f"Unknown skill: {skill_id}")

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE skills_config
                SET enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE skill_id = ?
                """,
                (1 if enabled else 0, skill_id),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info(f"[Skills] Skill '{skill_id}' set enabled={enabled}")
        return self.get_skill(skill_id)

    def update_skill_config(self, skill_id: str, config_updates: Dict[str, Any]) -> Dict[str, Any]:
        """Merge updates into a skill's configuration."""
        skill = self.get_skill(skill_id)
        if not skill:
            raise ValueError(f"Unknown skill: {skill_id}")

        current_config = skill["config"]
        current_config.update(config_updates)

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE skills_config
                SET config_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE skill_id = ?
                """,
                (json.dumps(current_config), skill_id),
            )
            conn.commit()
        finally:
            conn.close()

        logger.info(f"[Skills] Skill '{skill_id}' config updated: {config_updates}")
        return self.get_skill(skill_id)

    # -------------------------------------------------------------------------
    # Skill 1: Open Window Detection & Auto-Assist
    # -------------------------------------------------------------------------

    async def handle_temperature_update_for_window(
        self, device_id: int, device_info: Dict[str, Any], char_type: str
    ):
        """Analyze temperature updates on leader devices for open window detection."""
        if not self.is_skill_enabled(SKILL_OPEN_WINDOW):
            return

        zone_id = device_info.get("zone_id")
        if not zone_id:
            return

        if not self.is_skill_enabled(SKILL_OPEN_WINDOW, zone_id=zone_id):
            return

        skill = self.get_skill(SKILL_OPEN_WINDOW)
        config = skill["config"] if skill else {}
        drop_threshold = float(config.get("temp_drop_threshold", 1.0))
        time_window = int(config.get("time_window_minutes", 20))

        if not self.api or not self.api.state_manager:
            return

        leader_state = self.api.state_manager.get_current_state(device_id)
        if not leader_state:
            return

        # Fetch recent temperature history
        history = self.api.state_manager.get_device_history_info(device_id, age=time_window)
        if not history or history.get("history_count", 0) < 2:
            return

        earliest_temp = history["earliest_entry"][0]
        latest_temp = history["latest_entry"][0]
        if earliest_temp is None or latest_temp is None:
            return

        temp_drop = earliest_temp - latest_temp
        current_window_state = leader_state.get("window", 0)

        # Check if window already open
        if current_window_state == 1:
            return

        # Check rest window: if in rest state (2), ensure rest time passed
        latest_window_ts = history["latest_entry"][2]
        if current_window_state == 2 and latest_window_ts:
            rest_minutes_passed = (time.time() - int(latest_window_ts)) // 60
            rest_threshold = device_info.get("window_rest_time", config.get("default_rest_duration", 15))
            if rest_minutes_passed < rest_threshold:
                return

        # Significant temp drop detected! Trigger open window action
        if temp_drop >= drop_threshold:
            zone_name = device_info.get("zone_name", f"Zone {zone_id}")
            logger.warning(
                f"[Skill:OpenWindow] Open window detected in '{zone_name}'! "
                f"Temp dropped {temp_drop:.1f}°C in last {time_window} min."
            )
            await self.trigger_open_window(zone_id, device_id, device_info)

    async def trigger_open_window(self, zone_id: int, device_id: int, device_info: Dict[str, Any]):
        """Execute local Auto-Assist: pause heating and start open window timer."""
        skill = self.get_skill(SKILL_OPEN_WINDOW)
        config = skill["config"] if skill else {}
        open_duration = device_info.get("window_open_time", config.get("default_open_duration", 30))

        # Save current state before shutting off
        current_state = self.api.state_manager.get_current_state(device_id) if self.api else {}
        saved_target = current_state.get("target_temperature", 20.0)
        saved_mode = current_state.get("target_heating_cooling_state", 1)

        self.window_saved_states[zone_id] = {
            "target_temperature": saved_target,
            "target_heating_cooling_state": saved_mode,
            "device_id": device_id,
            "device_info": device_info,
            "open_duration": open_duration,
        }
        self.window_open_timestamps[zone_id] = time.time()

        # Update state manager window status to 1 (Open)
        if self.api and self.api.state_manager:
            self.api.state_manager.update_device_window_status(device_id, 1)

        # Turn off heating or apply frost protect
        action = config.get("action", "frost_protect")
        frost_temp = float(config.get("frost_temp", 5.0))
        target_temp = frost_temp if action == "frost_protect" else 5.0

        if self.api:
            try:
                await self.api.set_zone_temperature(zone_id, target_temp, heating_enabled=False)
                logger.info(
                    f"[Skill:OpenWindow] Paused heating for zone {zone_id} ({device_info.get('zone_name')}) "
                    f"to {target_temp}°C for {open_duration} minutes."
                )
            except Exception as e:
                logger.error(f"[Skill:OpenWindow] Failed to pause heating in zone {zone_id}: {e}")

        # Cancel any existing timer
        if zone_id in self.window_timers:
            task = self.window_timers.pop(zone_id)
            if not task.done():
                task.cancel()

        # Schedule timer to restore heating
        task = asyncio.create_task(self._window_close_timer(zone_id, device_id, device_info, open_duration))
        self.window_timers[zone_id] = task

        # Broadcast SSE event
        if self.api and hasattr(self.api, "broadcast_event"):
            await self.api.broadcast_event({
                "type": "skill_open_window",
                "zone_id": zone_id,
                "status": "open",
                "duration_minutes": open_duration,
                "opened_at": self.window_open_timestamps[zone_id],
            })

    async def _window_close_timer(
        self, zone_id: int, device_id: int, device_info: Dict[str, Any], open_duration: int
    ):
        """Timer task that automatically closes the window and resumes heating."""
        try:
            await asyncio.sleep(open_duration * 60)
            logger.info(f"[Skill:OpenWindow] Open duration ({open_duration}m) elapsed for zone {zone_id}.")
            await self.restore_open_window(zone_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Skill:OpenWindow] Error in timer for zone {zone_id}: {e}")

    async def restore_open_window(self, zone_id: int):
        """Restore heating after window is closed or timer elapsed."""
        saved = self.window_saved_states.pop(zone_id, None)
        self.window_open_timestamps.pop(zone_id, None)
        task = self.window_timers.pop(zone_id, None)
        if task and not task.done():
            task.cancel()

        device_id = saved.get("device_id") if saved else None
        if not device_id and self.api and self.api.state_manager:
            zone = self.api.state_manager.zone_cache.get(zone_id)
            if zone:
                device_id = zone.get("leader_device_id")

        # Set window status to rest state (2)
        if device_id and self.api and self.api.state_manager:
            self.api.state_manager.update_device_window_status(device_id, 2)

        # Restore heating
        if self.api:
            scheduler = getattr(self.api, "scheduler", None)
            is_sched = False
            if scheduler and hasattr(scheduler, "get_zone_schedule"):
                try:
                    sched_cfg = scheduler.get_zone_schedule(zone_id)
                    if asyncio.iscoroutine(sched_cfg):
                        sched_cfg = await sched_cfg
                    if isinstance(sched_cfg, dict):
                        is_sched = sched_cfg.get("enabled", False)
                except Exception:
                    is_sched = False

            if is_sched and scheduler:
                logger.info(f"[Skill:OpenWindow] Resuming local schedule for zone {zone_id}")
                await scheduler.evaluate_zone(zone_id, force=True)
            elif saved:
                orig_temp = saved.get("target_temperature", 20.0)
                orig_mode = saved.get("target_heating_cooling_state", 1)
                logger.info(f"[Skill:OpenWindow] Restoring saved heating: {orig_temp}°C (mode {orig_mode}) for zone {zone_id}")
                await self.api.set_zone_temperature(zone_id, orig_temp, heating_enabled=(orig_mode != 0))

        # Broadcast SSE event
        if self.api and hasattr(self.api, "broadcast_event"):
            await self.api.broadcast_event({
                "type": "skill_open_window",
                "zone_id": zone_id,
                "status": "closed",
            })

    def get_open_window_status(self) -> Dict[str, Any]:
        """Get live open window status across all zones."""
        active_windows = {}
        now = time.time()
        for zone_id, saved in self.window_saved_states.items():
            opened_at = self.window_open_timestamps.get(zone_id, now)
            duration = saved.get("open_duration", 30)
            elapsed = int(now - opened_at)
            remaining = max(0, (duration * 60) - elapsed)
            active_windows[zone_id] = {
                "zone_id": zone_id,
                "opened_at": opened_at,
                "duration_minutes": duration,
                "remaining_seconds": remaining,
            }
        return active_windows

    # -------------------------------------------------------------------------
    # Skill 2: Away Mode / Geofencing
    # -------------------------------------------------------------------------

    def get_away_status(self) -> Dict[str, Any]:
        """Return current Away mode status."""
        skill = self.get_skill(SKILL_AWAY_MODE)
        config = skill["config"] if skill else {}
        return {
            "enabled": skill["enabled"] if skill else True,
            "is_away": bool(config.get("is_away", False)),
            "away_temp": float(config.get("away_temp", 16.0)),
        }

    async def set_away_mode(self, is_away: bool, away_temp: Optional[float] = None) -> Dict[str, Any]:
        """Toggle Away Mode on or off and apply to all zones."""
        skill = self.get_skill(SKILL_AWAY_MODE)
        config = skill["config"] if skill else {}
        current_is_away = bool(config.get("is_away", False))
        temp = away_temp if away_temp is not None else float(config.get("away_temp", 16.0))

        if is_away == current_is_away:
            # Still update away_temp if provided
            if away_temp is not None:
                self.update_skill_config(SKILL_AWAY_MODE, {"away_temp": temp})
            return self.get_away_status()

        self.update_skill_config(SKILL_AWAY_MODE, {"is_away": is_away, "away_temp": temp})

        if not self.api:
            return self.get_away_status()

        zones = self.api.state_manager.zone_cache if self.api.state_manager else {}

        if is_away:
            # Transition to Away: save current temperatures and apply away temp
            logger.info(f"[Skill:AwayMode] Activating Away Mode at {temp}°C across {len(zones)} zones")
            for zone_id, zinfo in zones.items():
                leader_id = zinfo.get("leader_device_id")
                cur_state = self.api.state_manager.get_current_state(leader_id) if leader_id else {}
                self.away_saved_states[zone_id] = {
                    "target_temperature": cur_state.get("target_temperature", 20.0),
                    "target_heating_cooling_state": cur_state.get("target_heating_cooling_state", 1),
                }
                try:
                    await self.api.set_zone_temperature(zone_id, temp, heating_enabled=True)
                except Exception as e:
                    logger.error(f"[Skill:AwayMode] Failed setting zone {zone_id} to away temp: {e}")
        else:
            # Transition to Home: restore temperatures or schedule
            logger.info(f"[Skill:AwayMode] Deactivating Away Mode (returning Home)")
            scheduler = getattr(self.api, "scheduler", None)
            for zone_id, zinfo in zones.items():
                is_sched = False
                if scheduler and hasattr(scheduler, "get_zone_schedule"):
                    try:
                        sched_cfg = scheduler.get_zone_schedule(zone_id)
                        if asyncio.iscoroutine(sched_cfg):
                            sched_cfg = await sched_cfg
                        if isinstance(sched_cfg, dict):
                            is_sched = sched_cfg.get("enabled", False)
                    except Exception:
                        is_sched = False

                if is_sched and scheduler:
                    await scheduler.evaluate_zone(zone_id, force=True)
                elif zone_id in self.away_saved_states:
                    saved = self.away_saved_states.pop(zone_id)
                    try:
                        await self.api.set_zone_temperature(
                            zone_id,
                            saved.get("target_temperature", 20.0),
                            heating_enabled=(saved.get("target_heating_cooling_state", 1) != 0),
                        )
                    except Exception as e:
                        logger.error(f"[Skill:AwayMode] Failed restoring zone {zone_id}: {e}")

        # Broadcast SSE event
        if self.api and hasattr(self.api, "broadcast_event"):
            await self.api.broadcast_event({
                "type": "skill_away_mode",
                "is_away": is_away,
                "away_temp": temp,
            })

        return self.get_away_status()

    # -------------------------------------------------------------------------
    # Skill 3: Early Start Preheating
    # -------------------------------------------------------------------------

    def check_early_start(
        self,
        zone_id: int,
        current_temp: float,
        next_block_temp: float,
        minutes_until_block: int,
    ) -> Optional[float]:
        """
        Check if Early Start should begin preheating ahead of next block.
        Returns the preheat target temperature if applicable, or None.
        """
        if not self.is_skill_enabled(SKILL_EARLY_START, zone_id=zone_id):
            return None

        skill = self.get_skill(SKILL_EARLY_START)
        config = skill["config"] if skill else {}
        lookahead = int(config.get("lookahead_minutes", 30))
        min_deficit = float(config.get("min_temp_deficit", 0.5))

        # Only preheat if within lookahead window and next block is warmer
        if 0 < minutes_until_block <= lookahead:
            deficit = next_block_temp - current_temp
            if deficit >= min_deficit:
                logger.info(
                    f"[Skill:EarlyStart] Preheating zone {zone_id} to {next_block_temp}°C "
                    f"({minutes_until_block}m ahead, deficit: {deficit:.1f}°C)"
                )
                return next_block_temp

        return None

    # -------------------------------------------------------------------------
    # Skill 4: Air Comfort & Mold Risk Analysis
    # -------------------------------------------------------------------------

    @staticmethod
    def calculate_dew_point(temp: Optional[float], humidity: Optional[float]) -> Optional[float]:
        """Calculate dew point using Magnus-Tetens approximation."""
        if temp is None or humidity is None or humidity <= 0:
            return None
        a = 17.27
        b = 237.7
        alpha = ((a * temp) / (b + temp)) + math.log(humidity / 100.0)
        return round((b * alpha) / (a - alpha), 1)

    def evaluate_zone_air_comfort(
        self, temp: Optional[float], humidity: Optional[float]
    ) -> Dict[str, Any]:
        """Compute comprehensive air comfort, dew point, and mold risk."""
        dew_point = self.calculate_dew_point(temp, humidity)

        if temp is None or humidity is None:
            return {
                "temp": temp,
                "humidity": humidity,
                "dew_point": dew_point,
                "mold_risk": "unknown",
                "mold_risk_label": "Unknown",
                "recommendation": "normal",
                "recommendation_label": "Sensor data unavailable",
                "comfort_level": "unknown",
            }

        # Mold risk calculation based on humidity
        if humidity >= 70.0:
            mold_risk = "high"
            mold_risk_label = "High Mold Risk"
            recommendation = "ventilate"
            recommendation_label = "Open window to reduce humidity"
        elif humidity >= 60.0:
            mold_risk = "moderate"
            mold_risk_label = "Elevated Humidity"
            recommendation = "ventilate_soon"
            recommendation_label = "Consider airing out soon"
        elif humidity < 30.0:
            mold_risk = "low"
            mold_risk_label = "Dry Air"
            recommendation = "too_dry"
            recommendation_label = "Air is dry; humidification suggested"
        else:
            mold_risk = "low"
            mold_risk_label = "Optimal"
            recommendation = "optimal"
            recommendation_label = "Indoor climate is optimal"

        # General comfort level
        if humidity > 75:
            comfort_level = "humid"
        elif humidity < 30:
            comfort_level = "dry"
        elif 20 <= temp <= 23 and 40 <= humidity <= 60:
            comfort_level = "perfect"
        else:
            comfort_level = "comfortable"

        return {
            "temp": temp,
            "humidity": humidity,
            "dew_point": dew_point,
            "mold_risk": mold_risk,
            "mold_risk_label": mold_risk_label,
            "recommendation": recommendation,
            "recommendation_label": recommendation_label,
            "comfort_level": comfort_level,
        }

    def get_all_air_comfort(self) -> Dict[int, Dict[str, Any]]:
        """Get air comfort metrics for all zones."""
        results = {}
        if not self.api or not self.api.state_manager:
            return results

        for zone_id, zinfo in self.api.state_manager.zone_cache.items():
            leader_id = zinfo.get("leader_device_id")
            cur_state = self.api.state_manager.get_current_state(leader_id) if leader_id else {}
            temp = cur_state.get("current_temperature")
            hum = cur_state.get("humidity")
            eval_data = self.evaluate_zone_air_comfort(temp, hum)
            eval_data["zone_id"] = zone_id
            eval_data["zone_name"] = zinfo.get("name", f"Zone {zone_id}")
            results[zone_id] = eval_data

        return results

    # -------------------------------------------------------------------------
    # Skill 5: Care & Protect / System Diagnostics
    # -------------------------------------------------------------------------

    def check_heating_stall(self, zone_id: Optional[int], cur_heating: int, current_temp: Optional[float]):
        """Track heating start time to detect heating stall anomalies."""
        if not zone_id:
            return
        now = time.time()
        if cur_heating == 1 and current_temp is not None:
            if zone_id not in self.heating_start_times:
                self.heating_start_times[zone_id] = {
                    "started_at": now,
                    "start_temp": current_temp,
                    "max_temp": current_temp,
                }
            else:
                entry = self.heating_start_times[zone_id]
                entry["max_temp"] = max(entry["max_temp"], current_temp)
        else:
            self.heating_start_times.pop(zone_id, None)

    def get_diagnostics(self) -> Dict[str, Any]:
        """
        Run diagnostics across heating systems, battery states, and device connectivity.
        """
        anomalies = []
        low_batteries = []
        now = time.time()

        if not self.api or not self.api.state_manager:
            return {"status": "ok", "anomalies": [], "low_batteries": []}

        skill = self.get_skill(SKILL_CARE_PROTECT)
        config = skill["config"] if skill else {}
        timeout_min = int(config.get("anomaly_timeout_minutes", 60))
        min_rise = float(config.get("min_temp_increase", 0.2))

        # Check heating stall anomalies
        for zone_id, entry in list(self.heating_start_times.items()):
            running_minutes = int((now - entry["started_at"]) / 60)
            if running_minutes >= timeout_min:
                zinfo = self.api.state_manager.zone_cache.get(zone_id, {})
                leader_id = zinfo.get("leader_device_id")
                cur_state = self.api.state_manager.get_current_state(leader_id) if leader_id else {}
                current_temp = cur_state.get("current_temperature", entry["start_temp"])
                rise = current_temp - entry["start_temp"]
                if rise < min_rise:
                    anomalies.append({
                        "zone_id": zone_id,
                        "zone_name": zinfo.get("name", f"Zone {zone_id}"),
                        "running_minutes": running_minutes,
                        "start_temp": entry["start_temp"],
                        "current_temp": current_temp,
                        "issue": "Heating active but room temperature is not rising. Check radiator valve or boiler.",
                    })

        # Check battery health across all devices
        for dev in self.api.state_manager.get_all_devices():
            dev_id = dev.get("device_id")
            cur_state = self.api.state_manager.get_current_state(dev_id) if dev_id else {}
            battery_state = cur_state.get("battery_state")
            battery_level = cur_state.get("battery_level")

            if battery_state == "LOW" or (battery_level is not None and battery_level < 15):
                anomalies.append({
                    "device_id": dev_id,
                    "name": dev.get("name"),
                    "zone_name": dev.get("zone_name"),
                    "battery_level": battery_level,
                    "battery_state": battery_state,
                    "issue": f"Low battery ({battery_level or 'LOW'}%) - replacement recommended.",
                })
                low_batteries.append({
                    "device_id": dev_id,
                    "name": dev.get("name"),
                    "battery_level": battery_level,
                })

        status = "warning" if anomalies else "ok"
        return {
            "status": status,
            "anomalies": anomalies,
            "low_batteries": low_batteries,
            "total_issues": len(anomalies),
        }
