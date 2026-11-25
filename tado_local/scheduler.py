#
# Copyright 2025-2026 The TadoLocal and AmpScm contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Local Schedule Engine for Tado Local.

Allows setting per-zone heating schedules locally on the LAN without relying
on or sending data to the Tado Cloud app. When active, it overrules the Tado Cloud
app schedule by maintaining target temperatures on schedule transitions.
"""

import asyncio
import datetime
import logging
import sqlite3
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)


class LocalScheduler:
    """Manages local heating schedules and automates temperature transitions."""

    def __init__(self, api, db_path: str):
        self.api = api
        self.db_path = db_path
        self.task: Optional[asyncio.Task] = None
        self.is_running: bool = False
        # Track the last applied block per zone: zone_id -> (day_of_week, start_time, target_temperature)
        self.last_applied_block: Dict[int, Tuple[int, str, float]] = {}

    async def start(self):
        """Start the background scheduler evaluation loop."""
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self._run_loop())
        logger.info("Local Schedule Engine started")

    async def stop(self):
        """Stop the background scheduler loop."""
        self.is_running = False
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("Local Schedule Engine stopped")

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_zone_schedule(self, zone_id: int) -> Dict[str, Any]:
        """Retrieve the schedule and enabled status for a given zone."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            # Fetch config
            cur.execute(
                "SELECT enabled, mode, updated_at FROM zone_schedule_config WHERE zone_id = ?",
                (zone_id,)
            )
            config_row = cur.fetchone()
            enabled = bool(config_row["enabled"]) if config_row else False
            mode = config_row["mode"] if config_row else "local"

            # Fetch blocks
            cur.execute(
                """
                SELECT id, zone_id, day_of_week, start_time, target_temperature
                FROM local_schedules
                WHERE zone_id = ?
                ORDER BY day_of_week ASC, start_time ASC
                """,
                (zone_id,)
            )
            rows = cur.fetchall()
            blocks = [
                {
                    "id": row["id"],
                    "zone_id": row["zone_id"],
                    "day_of_week": row["day_of_week"],
                    "start_time": row["start_time"],
                    "target_temperature": float(row["target_temperature"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

        # Determine currently active block for preview
        active_block = self.get_active_block(zone_id)

        return {
            "zone_id": zone_id,
            "enabled": enabled,
            "mode": mode,
            "blocks": blocks,
            "active_block": active_block,
        }

    def get_all_schedules(self) -> Dict[int, Dict[str, Any]]:
        """Retrieve schedule config and block counts for all zones."""
        configs = {}
        counts = {}
        summary = {}

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT zone_id, enabled, mode FROM zone_schedule_config")
            configs = {row["zone_id"]: {"enabled": bool(row["enabled"]), "mode": row["mode"]} for row in cur.fetchall()}

            cur.execute("SELECT zone_id, COUNT(*) as count FROM local_schedules GROUP BY zone_id")
            counts = {row["zone_id"]: row["count"] for row in cur.fetchall()}

            cur.execute("SELECT zone_id, name FROM zones")
            for row in cur.fetchall():
                zid = row["zone_id"]
                cfg = configs.get(zid, {"enabled": False, "mode": "local"})
                summary[zid] = {
                    "zone_id": zid,
                    "name": row["name"],
                    "enabled": cfg["enabled"],
                    "mode": cfg["mode"],
                    "block_count": counts.get(zid, 0),
                }
        finally:
            conn.close()

        return summary

    def set_zone_enabled(self, zone_id: int, enabled: bool) -> Dict[str, Any]:
        """Toggle local schedule overrule on/off for a zone."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO zone_schedule_config (zone_id, enabled, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(zone_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (zone_id, 1 if enabled else 0)
            )
            conn.commit()
        finally:
            conn.close()

        # Clear tracked block so state changes apply immediately
        self.last_applied_block.pop(zone_id, None)
        logger.info(f"Local schedule for zone {zone_id} set to enabled={enabled}")

        return {"zone_id": zone_id, "enabled": enabled}

    def save_zone_schedule(
        self,
        zone_id: int,
        blocks: List[Dict[str, Any]],
        enabled: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Save schedule blocks for a zone and optionally update enabled state."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()

            if enabled is not None:
                cur.execute(
                    """
                    INSERT INTO zone_schedule_config (zone_id, enabled, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(zone_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (zone_id, 1 if enabled else 0)
                )

            # Replace existing blocks
            cur.execute("DELETE FROM local_schedules WHERE zone_id = ?", (zone_id,))

            for b in blocks:
                day = int(b.get("day_of_week", -1))
                time_str = str(b.get("start_time", "00:00")).strip()
                # Basic normalization of HH:MM
                parts = time_str.split(":")
                hh = int(parts[0])
                mm = int(parts[1]) if len(parts) > 1 else 0
                norm_time = f"{hh:02d}:{mm:02d}"

                temp = float(b.get("target_temperature", 20.0))
                temp = max(5.0, min(30.0, round(temp, 1)))

                cur.execute(
                    """
                    INSERT INTO local_schedules (zone_id, day_of_week, start_time, target_temperature)
                    VALUES (?, ?, ?, ?)
                    """,
                    (zone_id, day, norm_time, temp)
                )

            conn.commit()
        finally:
            conn.close()

        # Invalidate last applied cache so new schedule is applied
        self.last_applied_block.pop(zone_id, None)
        logger.info(f"Saved {len(blocks)} local schedule blocks for zone {zone_id}")

        return self.get_zone_schedule(zone_id)

    def get_active_block(
        self,
        zone_id: int,
        now: Optional[datetime.datetime] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Determine which schedule block is active right now for a given zone.
        Handles wrapping around midnight to the prior day's last block if the current time
        is before the day's first scheduled block.
        """
        now = now or datetime.datetime.now()
        current_weekday = now.weekday()  # 0=Monday, ..., 6=Sunday
        current_time_str = now.strftime("%H:%M")

        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, zone_id, day_of_week, start_time, target_temperature
                FROM local_schedules
                WHERE zone_id = ?
                ORDER BY start_time ASC
                """,
                (zone_id,)
            )
            all_blocks = [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

        if not all_blocks:
            return None

        # Filter blocks applicable for today: specific day_of_week matches or all-days (-1)
        today_blocks = [
            b for b in all_blocks
            if b["day_of_week"] == current_weekday or b["day_of_week"] == -1
        ]

        # Prefer day-specific blocks if any exist for today
        day_specific = [b for b in today_blocks if b["day_of_week"] == current_weekday]
        active_today_pool = day_specific if day_specific else today_blocks
        active_today_pool.sort(key=lambda x: x["start_time"])

        # Find the latest block that has already started today
        candidate = None
        for b in active_today_pool:
            if b["start_time"] <= current_time_str:
                candidate = b
            else:
                break

        if candidate:
            return candidate

        # If current time is before the first block today, fall back to the last block of yesterday
        prev_weekday = (current_weekday - 1) % 7
        yesterday_blocks = [
            b for b in all_blocks
            if b["day_of_week"] == prev_weekday or b["day_of_week"] == -1
        ]
        prev_specific = [b for b in yesterday_blocks if b["day_of_week"] == prev_weekday]
        yesterday_pool = prev_specific if prev_specific else yesterday_blocks
        yesterday_pool.sort(key=lambda x: x["start_time"])

        if yesterday_pool:
            return yesterday_pool[-1]

        # Final fallback: return the last block of today's pool
        if active_today_pool:
            return active_today_pool[-1]

        return None

    async def evaluate_zone(self, zone_id: int, force: bool = False) -> Optional[float]:
        """
        Check and apply the scheduled temperature for a single zone if active and changed.
        Returns the applied temperature if a change was made, or None otherwise.
        """
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT enabled FROM zone_schedule_config WHERE zone_id = ?", (zone_id,))
            row = cur.fetchone()
            if not row or not row["enabled"]:
                return None
        finally:
            conn.close()

        active_block = self.get_active_block(zone_id)
        if not active_block:
            return None

        block_key = (
            active_block["day_of_week"],
            active_block["start_time"],
            active_block["target_temperature"]
        )

        last_key = self.last_applied_block.get(zone_id)
        if force or last_key != block_key:
            target_temp = active_block["target_temperature"]
            logger.info(
                f"[LocalScheduler] Applying schedule for zone {zone_id}: {target_temp}°C "
                f"(block: start={active_block['start_time']}, day={active_block['day_of_week']})"
            )
            try:
                success = await self.api.set_zone_temperature(zone_id, target_temp)
                if success:
                    self.last_applied_block[zone_id] = block_key
                    return target_temp
                else:
                    logger.warning(f"[LocalScheduler] Failed to set zone temperature for zone {zone_id}")
            except Exception as e:
                logger.error(f"[LocalScheduler] Exception applying schedule for zone {zone_id}: {e}")

        return None

    async def evaluate_all(self, force: bool = False):
        """Evaluate all enabled zones."""
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT zone_id FROM zone_schedule_config WHERE enabled = 1")
            enabled_zones = [row["zone_id"] for row in cur.fetchall()]
        finally:
            conn.close()

        for zid in enabled_zones:
            try:
                await self.evaluate_zone(zid, force=force)
            except Exception as e:
                logger.error(f"[LocalScheduler] Error evaluating zone {zid}: {e}")

    async def _run_loop(self):
        """Periodic evaluation loop."""
        logger.info("[LocalScheduler] Background loop active")
        # Run an initial evaluation shortly after startup
        await asyncio.sleep(5)
        while self.is_running:
            try:
                await self.evaluate_all()
            except Exception as e:
                logger.error(f"[LocalScheduler] Evaluation error in background loop: {e}")
            # Check every 25 seconds
            await asyncio.sleep(25)
