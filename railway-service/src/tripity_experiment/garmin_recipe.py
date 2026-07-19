"""Garmin semantic tools for Tripity browser connections.

The generic browser `query(path)` tool proves the architecture, but a real user
should not expose endpoint paths to the AI. This recipe gives Garmin connections
clear, stable tool names while still executing through the user's authenticated
cloud browser session.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from tripity_experiment.browser_connector import SessionExecutor

GARMIN_ORIGIN = "https://connect.garmin.com"


def is_garmin_origin(origin: str) -> bool:
    return origin.rstrip("/").lower() == GARMIN_ORIGIN


def _daily_summary_path(endpoints: list[str] | None) -> str:
    for endpoint in endpoints or []:
        if endpoint.startswith("/gc-api/usersummary-service/usersummary/daily/"):
            return endpoint
    return "/gc-api/usersummary-service/usersummary/daily/{user_id}"


def register_garmin_tools(
    gateway: FastMCP,
    executor: SessionExecutor,
    *,
    prefix: str = "garmin",
    endpoints: list[str] | None = None,
) -> None:
    """Register semantic Garmin tools backed by the browser executor."""

    daily_path = _daily_summary_path(endpoints)

    async def dashboard() -> dict[str, Any]:
        """Get Garmin dashboard summary cards and recent wellness/training sections."""
        return await executor.execute(
            "GET",
            GARMIN_ORIGIN + "/gc-api/userfocus-service/dashboard",
            params={"nutritionMigrationAllowed": "true"},
        )

    async def devices() -> dict[str, Any]:
        """List Garmin devices registered to the logged-in account."""
        return await executor.execute(
            "GET",
            GARMIN_ORIGIN + "/gc-api/device-service/deviceregistration/devices",
            params={},
        )

    async def sync_status() -> dict[str, Any]:
        """Get Garmin wellness sync timestamp/status for the logged-in account."""
        return await executor.execute(
            "GET",
            GARMIN_ORIGIN + "/gc-api/wellness-service/wellness/syncTimestamp",
            params={},
        )

    async def recent_activity_status() -> dict[str, Any]:
        """Get Garmin recent activity/newsfeed status."""
        return await executor.execute(
            "GET",
            GARMIN_ORIGIN + "/gc-api/activitylist-service/activities/v2/newsfeed/status",
            params={},
        )

    async def sleep_summary(calendar_date: str) -> dict[str, Any]:
        """Get Garmin sleep summary for a calendar date (YYYY-MM-DD)."""
        return await executor.execute(
            "GET",
            GARMIN_ORIGIN + "/gc-api/sleep-service/sleep/dailySleepData",
            params={"date": calendar_date},
        )

    async def daily_summary(calendar_date: str, user_id: str = "") -> dict[str, Any]:
        """Get Garmin daily summary including steps/calories for a date.

        If Tripity discovered the account-specific `/usersummary/daily/<id>`
        endpoint, user_id is not needed. Otherwise provide the Garmin profile id.
        """
        path = daily_path
        if "{user_id}" in path:
            if not user_id:
                raise ValueError("Garmin user_id is required until the daily summary endpoint is discovered")
            path = path.replace("{user_id}", user_id)
        return await executor.execute(
            "GET",
            GARMIN_ORIGIN + path,
            params={"calendarDate": calendar_date},
        )

    gateway.tool(dashboard, name=f"{prefix}_dashboard")
    gateway.tool(devices, name=f"{prefix}_devices")
    gateway.tool(sync_status, name=f"{prefix}_sync_status")
    gateway.tool(recent_activity_status, name=f"{prefix}_recent_activity_status")
    gateway.tool(sleep_summary, name=f"{prefix}_sleep_summary")
    gateway.tool(daily_summary, name=f"{prefix}_daily_summary")
