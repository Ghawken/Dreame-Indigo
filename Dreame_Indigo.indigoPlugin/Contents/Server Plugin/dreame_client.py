import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from dreame.device import DreameVacuumDevice
from dreame.protocol import DreameVacuumProtocol
from dreame.exceptions import DeviceException, DeviceUpdateFailedException

try:
    import indigo
except ImportError:
    pass

_LOGGER = logging.getLogger("dreame_client")


@dataclass
class DreameStatus:
    state: str
    state_text: str
    battery: int
    fan_speed: str
    fan_modes: List[str]
    area_m2: float
    duration_min: int
    is_charging: bool
    error_code: Optional[str] = None
    error_text: Optional[str] = None


class AsyncDreameClient:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        name: str,
        host: Optional[str],
        token: Optional[str],
        mac: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        country: Optional[str] = None,
        prefer_cloud: bool = True,
        device_id: Optional[str] = None,
        auth_key: Optional[str] = None,
        account_type: Optional[str] = "dreame",
    ) -> None:
        self._loop = loop
        self._name = name or "Dreame Vacuum"

        self._host = (host or "").strip() or None

        # Preserve dummy space token for dreame/mova cloud accounts (HA behaviour)
        at = (account_type or "dreame").strip().lower()
        if at in ("dreame", "mova"):
            # HA's extract_info sets token to " " when it's None; don't strip it away
            if token is None:
                self._token = None
            else:
                # Keep as-is (including a single space) so dreame lib sees a non-empty token
                self._token = str(token)
        else:
            # mi/local: normal trimming, empty => None
            self._token = (token or "").strip() or None

        self._mac = (mac or "").strip() or None
        self._username = (username or "").strip() or None
        self._password = (password or "").strip() or None
        self._country = (country or "eu").strip().lower()
        self._prefer_cloud = bool(prefer_cloud)
        self._device_id = str(device_id).strip() if device_id else None
        self._auth_key = auth_key
        self._account_type = at

        self._protocol: Optional[DreameVacuumProtocol] = None
        self._device: Optional[DreameVacuumDevice] = None
        self._connected: bool = False

        _LOGGER.debug(
            "AsyncDreameClient.__init__: name=%s host=%r token_len=%s mac=%r "
            "user_set=%s country=%r prefer_cloud=%s device_id=%r account_type=%r",
            self._name,
            self._host,
            len(self._token) if isinstance(self._token, str) else 0,
            self._mac,
            bool(self._username and self._password),
            self._country,
            self._prefer_cloud,
            self._device_id,
            self._account_type,
        )

    async def _run(self, func, *args, **kwargs):
        """
        Run blocking dreame.* call in Indigo's async executor.
        """
        return await self._loop.run_in_executor(None, lambda: func(*args, **kwargs))

    async def connect(self) -> None:
        """
        HA-like connect:

        - Build DreameVacuumProtocol with our parameters.
        - For dreame/mova: ensure cloud.login() has been called on this final instance.
        - Call protocol.connect() in executor.
        - Use returned info to build DreameVacuumDevice and call .update() once.
        """
        _LOGGER.debug(
            "AsyncDreameClient.connect START: "
            "name=%s host=%r token_len=%s user_set=%s country=%r prefer_cloud=%s device_id=%r account_type=%r",
            self._name,
            self._host,
            len(self._token) if self._token else 0,
            bool(self._username and self._password),
            self._country,
            self._prefer_cloud,
            self._device_id,
            self._account_type,
        )

        if self._protocol is None:
            # Match HA config_flow: pass account_type and cloud auth_key
            self._protocol = DreameVacuumProtocol(
                ip=self._host,
                token=self._token,
                username=self._username,
                password=self._password,
                country=self._country,
                prefer_cloud=self._prefer_cloud,
                account_type=self._account_type,
                device_id=self._device_id,
                auth_key=self._auth_key,
            )
            _LOGGER.debug(
                "Created DreameVacuumProtocol: "
                "ip=%r token_set=%s username_set=%s country=%r prefer_cloud=%s "
                "device=%s cloud=%s device_cloud=%s account_type=%r",
                self._host,
                bool(self._token),
                bool(self._username and self._password),
                self._country,
                self._protocol.prefer_cloud,
                bool(self._protocol.device),
                bool(self._protocol.cloud),
                bool(getattr(self._protocol, "device_cloud", None)),
                self._account_type,
            )

        # ---- Critical bit for dreame/mova: login this final protocol's cloud before connect() ----
        if (
            self._protocol.cloud is not None
            and self._account_type in ("dreame", "mova")
            and not self._protocol.cloud.logged_in
        ):
            _LOGGER.debug(
                "Calling Dreame cloud login on final protocol instance "
                "(account_type=%r, device_id=%r, auth_key_present=%s)",
                self._account_type,
                getattr(self._protocol.cloud, "device_id", None),
                bool(self._protocol.cloud.auth_key),
            )
            try:
                ok = await self._run(self._protocol.cloud.login)
                _LOGGER.debug(
                    "Final protocol cloud.login() returned: %r (logged_in=%r, auth_failed=%r, connected=%r)",
                    ok,
                    self._protocol.cloud.logged_in,
                    getattr(self._protocol.cloud, "auth_failed", None),
                    self._protocol.cloud.connected,
                )
                if not ok or not self._protocol.cloud.logged_in:
                    raise DeviceException("Cloud login on final protocol instance failed")
            except DeviceException:
                raise
            except Exception as ex:
                _LOGGER.error("Cloud login on final protocol instance raised: %s", ex)
                raise DeviceException(f"Cloud login failed: {ex}") from ex

        # Now call connect() which internally uses local or cloud as appropriate
        try:
            info = await self._run(self._protocol.connect, None, None, 3)
            _LOGGER.debug("DreameVacuumProtocol.connect returned: %r", info)

        except DeviceException as ex:
            _LOGGER.error("Dreame protocol connect failed: %s", ex)
            raise
        except Exception as ex:
            _LOGGER.error("Unexpected error during Dreame protocol connect: %s", ex)
            raise DeviceException(str(ex)) from ex

        if info is None:
            _LOGGER.error("Dreame protocol returned no device info")
            raise DeviceException("No device info from dreame cloud/local protocol")

        if self._device is None:
            self._device = DreameVacuumDevice(
                name=self._name,
                host=self._host,
                token=self._token,
                mac=self._mac,
                username=self._username,
                password=self._password,
                country=self._country,
                prefer_cloud=self._prefer_cloud,
                device_id=self._device_id,
                auth_key=self._auth_key,
                account_type=self._account_type,
            )
            _LOGGER.debug(
                "Created DreameVacuumDevice: name=%s host=%r mac=%r token_len=%s "
                "user_set=%s country=%r prefer_cloud=%s device_id=%r account_type=%r",
                self._name,
                self._host,
                self._mac,
                len(self._token) if self._token else 0,
                bool(self._username and self._password),
                self._country,
                self._prefer_cloud,
                self._device_id,
                self._account_type,
            )

        # Initial update
        # Initial update
        try:
            await self._run(self._device.update, info)
        except DeviceUpdateFailedException as ex:
            _LOGGER.warning("Initial device.update() failed (non-fatal): %s", ex)
        except DeviceException:
            # Genuine fatal Dreame error -> still raise
            raise
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        if self._protocol is not None:
            try:
                await self._run(self._protocol.disconnect)
            except Exception:
                pass

    async def get_status(self) -> DreameStatus:
        """
        Update DreameVacuumDevice and map its status -> DreameStatus.
        """
        if not self._device:
            raise RuntimeError("Dreame client not connected")

        _LOGGER.debug("AsyncDreameClient.get_status(): calling DreameVacuumDevice.update()")
        await self._run(self._device.update)
        s = self._device.status

        raw_state = getattr(s, "status", None) or getattr(s, "state", None)
        if hasattr(raw_state, "name"):
            state_str = raw_state.name
        else:
            state_str = str(raw_state) if raw_state is not None else "UNKNOWN"

        mapping = {
            "AUTO_CLEANING": "Cleaning",
            "CLEANING": "Cleaning",
            "ZONE_CLEANING": "Zone cleaning",
            "SEGMENT_CLEANING": "Room cleaning",
            "BACK_HOME": "Returning to dock",
            "DOCKED": "Docked",
            "IDLE": "Idle",
            "PAUSED": "Paused",
            "STANDBY": "Standby",
            "ERROR": "Error",
        }
        state_text = mapping.get(state_str.upper(), state_str.title())

        battery = int(getattr(s, "battery_level", 0) or 0)

        suction_enum = getattr(s, "suction_level", None)
        if hasattr(suction_enum, "name"):
            fan_speed = suction_enum.name.title()
        else:
            fan_speed = str(suction_enum) if suction_enum is not None else ""

        fan_modes: List[str] = []
        levels = getattr(s, "suction_levels", None)
        if isinstance(levels, dict):
            for v in levels.values():
                if hasattr(v, "name"):
                    fan_modes.append(v.name.title())
                else:
                    fan_modes.append(str(v))

        # --- Area / duration: prefer direct attributes, else fall back to attributes dict ---
        attrs = getattr(s, "attributes", None) or {}

        raw_area = getattr(s, "cleaned_area", None)
        if raw_area is None:
            raw_area = attrs.get("cleaned_area")
        try:
            area_m2 = float(raw_area or 0.0)
        except Exception:
            area_m2 = 0.0

        raw_time = getattr(s, "cleaning_time", None)
        if raw_time is None:
            raw_time = attrs.get("cleaning_time")
        try:
            duration_min = int(raw_time or 0)
        except Exception:
            duration_min = 0

        is_charging = bool(getattr(s, "charging", False))

        error = getattr(s, "error", None)
        if hasattr(error, "name"):
            error_code = error.name
        else:
            error_code = str(error) if error not in (None, 0) else None
        error_text = getattr(s, "error_description", None) or None

        _LOGGER.debug(
            "Status snapshot: state=%s (%s), battery=%d%%, fan=%s, area=%.2f m^2, "
            "duration=%d min, charging=%s, error=%s (%s)",
            state_str,
            state_text,
            battery,
            fan_speed,
            area_m2,
            duration_min,
            is_charging,
            error_code,
            error_text,
        )

        return DreameStatus(
            state=state_str,
            state_text=state_text,
            battery=battery,
            fan_speed=fan_speed,
            fan_modes=fan_modes,
            area_m2=area_m2,
            duration_min=duration_min,
            is_charging=is_charging,
            error_code=error_code,
            error_text=error_text,
        )

    # ========= Commands =========

    async def start_cleaning(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("start_cleaning() called")
        await self._run(self._device.start)

    async def start_shortcut(self, shortcut_id) -> None:
        """
        Start a Dreame shortcut by ID.

        shortcut_id can be:
          - an int (e.g. 25)
          - a stringified int (e.g. "25")
        We validate it against the device.status.attributes['shortcuts'] first
        to ensure the shortcut actually exists.
        """
        if not self._device:
            raise RuntimeError("Not connected")

        # Normalize to int if possible
        sid_int = None
        if isinstance(shortcut_id, int):
            sid_int = shortcut_id
        else:
            try:
                sid_int = int(str(shortcut_id).strip())
            except Exception:
                sid_int = None

        # Try to validate against current shortcuts
        shortcuts_attr = None
        try:
            s = self._device.status
            attrs = getattr(s, "attributes", None) or {}
            shortcuts_attr = attrs.get("shortcuts") or {}
        except Exception:
            shortcuts_attr = {}

        if shortcuts_attr and sid_int is not None and sid_int not in shortcuts_attr:
            raise ValueError(f"Invalid shortcut ID: {shortcut_id!r} (not in device shortcuts)")

        _LOGGER.debug("AsyncDreameClient.start_shortcut(id=%r) called", sid_int or shortcut_id)

        # Dreame library expects an int id
        try:
            await self._run(self._device.start_shortcut, sid_int if sid_int is not None else shortcut_id)
            _LOGGER.debug("AsyncDreameClient.start_shortcut(%r) completed OK", sid_int or shortcut_id)
        except Exception as exc:
            _LOGGER.error("AsyncDreameClient.start_shortcut(%r) failed: %s", sid_int or shortcut_id, exc)
            raise
###
    async def start_washing(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("start_washing() called")
        await self._run(self._device.start_washing)

    async def pause_washing(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("pause_washing() called")
        await self._run(self._device.pause_washing)

    async def start_drying(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("start_drying() called")
        await self._run(self._device.start_drying)

    async def stop_drying(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("stop_drying() called")
        await self._run(self._device.stop_drying)

    async def start_draining(self, clean_water_tank: bool = False) -> None:
        """
        Start drainage; if clean_water_tank is True, also empty clean tank when supported.
        """
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("start_draining(clean_water_tank=%r) called", clean_water_tank)
        await self._run(self._device.start_draining, clean_water_tank)


###
    async def stop_cleaning(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("stop_cleaning() called")
        await self._run(self._device.stop)

    async def pause(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("pause() called")
        await self._run(self._device.pause)

    async def start_pause(self) -> None:
        """
        Toggle start/pause if supported by DreameVacuumDevice.
        """
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("start_pause() called")
        await self._run(self._device.start_pause)

    async def return_to_dock(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("return_to_dock() called")
        await self._run(self._device.return_to_base)

    async def locate(self) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("locate() called")
        await self._run(self._device.locate)

    async def set_fan_speed(self, speed: str) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("set_fan_speed(%s) called", speed)
        await self._run(self._device.set_suction_level, speed)

    async def clean_segment(self, segments, repeats=1, suction_level="", water_volume="") -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug(
            "clean_segment(segments=%r, repeats=%r, suction_level=%r, water_volume=%r)",
            segments,
            repeats,
            suction_level,
            water_volume,
        )
        await self._run(self._device.clean_segment, segments, repeats, suction_level, water_volume)

    async def clean_zone(self, zones, repeats=1) -> None:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("clean_zone(zones=%r, repeats=%r) called", zones, repeats)
        await self._run(self._device.clean_zone, zones, repeats)

    async def send_raw_command(self, method: str, params: Dict[str, Any]) -> Any:
        if not self._device:
            raise RuntimeError("Not connected")
        _LOGGER.debug("send_raw_command(method=%r, params=%r)", method, params)
        return await self._run(self._device.send_command, method, params)

    async def set_custom_cleaning(
        self,
        segment_ids,
        suction_levels,
        water_volumes,
        repeats,
        cleaning_modes=None,
        wetness_levels=None,
        cleaning_routes=None,
        custom_mopping_routes=None,
    ) -> None:
        """
        Thin wrapper around DreameVacuumDevice.set_custom_cleaning.

        All inputs are already lists/arrays (mirroring HA).
        """
        if not self._device:
            raise RuntimeError("Not connected")

        _LOGGER.debug(
            "set_custom_cleaning(segment_ids=%r, suction=%r, water=%r, repeats=%r, "
            "cleaning_modes=%r, wetness=%r, routes=%r, custom_routes=%r)",
            segment_ids,
            suction_levels,
            water_volumes,
            repeats,
            cleaning_modes,
            wetness_levels,
            cleaning_routes,
            custom_mopping_routes,
        )

        await self._run(
            self._device.set_custom_cleaning,
            segment_ids,
            suction_levels,
            water_volumes,
            repeats,
            cleaning_modes,
            custom_mopping_routes,
            cleaning_routes,
            wetness_levels,
        )