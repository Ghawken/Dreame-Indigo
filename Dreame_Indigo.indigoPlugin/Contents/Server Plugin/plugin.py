#! /usr/bin/env python
# -*- coding: utf-8 -*-
try:
    import indigo
except ImportError:
    pass

import asyncio
import threading
import logging
import logging.handlers
import traceback
import os
import sys
import platform
from os import path
from datetime import datetime

from dreame_client import AsyncDreameClient, DreameStatus
from dreame.device import DreameVacuumDevice
from dreame.protocol import DreameVacuumProtocol, DeviceException
# Add near top of file with other imports
from dreame_camera import DreameCameraHelper, DreameCameraConfig
# add with other imports at the top

import logging, traceback
from dreame.protocol import DreameVacuumProtocol

class IndigoLogHandler(logging.Handler):
    def __init__(self, display_name: str, level=logging.NOTSET, force_debug: bool = False):

        super().__init__(level)
        self.displayName = display_name
        self.force_debug = force_debug  # if True, always log at DEBUG to Indigo

    def emit(self, record):
        logmessage = ""
        is_error = False
        # Original level from logger
        orig_level = getattr(record, "levelno", logging.INFO)

        # What level Indigo sees
        levelno = logging.DEBUG if self.force_debug else orig_level
        try:
            if self.level <= levelno:
                is_exception = record.exc_info is not None

                if levelno == 5 or levelno == logging.DEBUG:
                    logmessage = "({}:{}:{}): {}".format(
                        path.basename(record.pathname),
                        record.funcName,
                        record.lineno,
                        record.getMessage(),
                    )
                elif levelno == logging.INFO:
                    logmessage = record.getMessage()
                elif levelno == logging.WARNING:
                    logmessage = record.getMessage()
                elif levelno == logging.ERROR:
                    logmessage = "({}: Function: {}  line: {}):    Error :  Message : {}".format(
                        path.basename(record.pathname),
                        record.funcName,
                        record.lineno,
                        record.getMessage(),
                    )
                    is_error = True

                if is_exception:
                    logmessage = "({}: Function: {}  line: {}):    Exception :  Message : {}".format(
                        path.basename(record.pathname),
                        record.funcName,
                        record.lineno,
                        record.getMessage(),
                    )
                    indigo.server.log(message=logmessage, type=self.displayName, isError=is_error, level=levelno)
                    etype, value, tb = record.exc_info
                    tb_string = "".join(traceback.format_tb(tb))
                    indigo.server.log(f"Traceback:\n{tb_string}", type=self.displayName, isError=is_error, level=levelno)
                    indigo.server.log(f"Error in plugin execution:\n\n{traceback.format_exc(30)}",
                                      type=self.displayName, isError=is_error, level=levelno)
                    indigo.server.log(
                        f"\nExc_info: {record.exc_info} \nExc_Text: {record.exc_text} \nStack_info: {record.stack_info}",
                        type=self.displayName, isError=is_error, level=levelno,
                    )
                    return

                indigo.server.log(message=logmessage, type=self.displayName, isError=is_error, level=levelno)
        except Exception as ex:
            indigo.server.log(f"Error in Logging: {ex}", type=self.displayName, isError=True, level=logging.ERROR)


class Plugin(indigo.PluginBase):
    ########################################
    def __init__(
        self,
        plugin_id: str,
        plugin_display_name: str,
        plugin_version: str,
        plugin_prefs: indigo.Dict,
        **kwargs,
    ) -> None:
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs)

        # Async
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread: threading.Thread | None = None
        self.stopThread: bool = False

        # Per-device clients and poll tasks
        self._clients: dict[int, AsyncDreameClient] = {}
        self._poll_tasks: dict[int, asyncio.Task] = {}
        # inside Plugin.__init__ after other instance attributes
        # Per-device map poll tasks
        self._map_tasks: dict[int, asyncio.Task] = {}

        # --- Logging setup (DeviceTimer / EVSE style, but quieter for libs) ---
        if hasattr(self, "indigo_log_handler") and self.indigo_log_handler:
            self.logger.removeHandler(self.indigo_log_handler)

        # Collect everything at logger; handlers filter
        self.logger.setLevel(logging.DEBUG)

        try:
            self.logLevel = int(self.pluginPrefs.get("showDebugLevel", logging.INFO))
            self.fileloglevel = int(self.pluginPrefs.get("showDebugFileLevel", logging.DEBUG))
        except Exception:
            self.logLevel = logging.INFO
            self.fileloglevel = logging.DEBUG

        # Indigo log handler
        # Indigo handler for plugin messages (respects user-selected level)
        try:
            self.indigo_log_handler = IndigoLogHandler(plugin_display_name, level=self.logLevel, force_debug=False)
            self.indigo_log_handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(self.indigo_log_handler)
        except Exception as exc:
            indigo.server.log(f"Failed to create IndigoLogHandler: {exc}", isError=True)


        # File handler
        try:
            self.plugin_file_handler.setLevel(self.fileloglevel)
            # Attach to plugin logger
            self.logger.addHandler(self.plugin_file_handler)
        except Exception as exc:
            self.logger.exception(exc)

        # Route library logging: full DEBUG to file, INFO+ optionally to Indigo
        # Route library logging: always to file; optionally to Indigo at DEBUG when showDebugInfo is True
        try:
            root_logger = logging.getLogger()
            if self.plugin_file_handler not in root_logger.handlers:
                root_logger.addHandler(self.plugin_file_handler)
            if root_logger.level > logging.DEBUG:
                root_logger.setLevel(logging.DEBUG)

            # Check pref: do we want library logs in Indigo?
            show_lib_debug = bool(self.pluginPrefs.get("showDebugInfo", False))

            lib_indigo_handler = None
            if show_lib_debug:
                # All library logs will appear as DEBUG in Indigo
                lib_indigo_handler = IndigoLogHandler(plugin_display_name, level=logging.DEBUG, force_debug=True)
                lib_indigo_handler.setFormatter(logging.Formatter("%(message)s"))

            def _wire_logger(name: str, to_indigo: bool):
                lg = logging.getLogger(name)
                lg.setLevel(logging.DEBUG)
                lg.handlers[:] = []
                lg.addHandler(self.plugin_file_handler)
                if to_indigo and lib_indigo_handler:
                    lg.addHandler(lib_indigo_handler)
                lg.propagate = False

            # Dreame family: always to file; to Indigo only when showDebugInfo is True
            _wire_logger("dreame_client", show_lib_debug)
            _wire_logger("dreame_camera", show_lib_debug)
            _wire_logger("dreame", show_lib_debug)

            # python-miio: you can treat it the same way (or leave Indigo off always)
            _wire_logger("miio", show_lib_debug)

            # HTTP libs: file only, no Indigo spam
            for http_name in ("urllib3", "requests"):
                hl = logging.getLogger(http_name)
                hl.setLevel(logging.WARNING)
                hl.handlers[:] = []
                hl.addHandler(self.plugin_file_handler)
                hl.propagate = False

            self.logger.debug("Attached dreame_client, dreame, miio, urllib3, requests loggers to handlers")
        except Exception as exc:
            self.logger.exception(exc)

        # Header
        self.logger.info("")
        self.logger.info("{0:=^100}".format("⚪️ Initializing Dreame Vacuum ⚪️"))
        self.logger.info(f"{'Plugin name:':<24}{plugin_display_name}")
        self.logger.info(f"{'Plugin version:':<24}{plugin_version}")
        self.logger.info(f"{'Plugin ID:':<24}{plugin_id}")
        self.logger.info(f"{'Indigo version:':<24}{indigo.server.version}")
        self.logger.info(f"{'Platform:':<24}{platform.machine()}")
        self.logger.info(f"{'Python:':<24}{sys.version.replace(os.linesep, ' ')}")
        self.logger.info("{0:=^100}".format("🤖🤖🤖 End Initializing 🤖🤖🤖"))

    ########################################
   # TESTING
    ## 2FA

    def _update_dreame_login_info(
        self,
        dev: indigo.Device,
        message: str | None = None,
        auth_key: str | None = None,
    ) -> None:
        """
        Store cloud-login status and optional auth_key into device pluginProps.
        """
        try:
            new_props = dev.pluginProps
            if message is not None:
                new_props["dreameLoginInfo"] = message
            if auth_key is not None:
                new_props["authKey"] = auth_key
            dev.replacePluginPropsOnServer(new_props)
        except Exception as exc:
            self.logger.debug(f"_update_dreame_login_info failed for '{dev.name}': {exc}")

    def _get_menu_value(self, raw):
        """
        Indigo UI can pass menu fields as:
          - a scalar (str/int), or
          - a list, where the first item is the actual value.

        This normalizes to a stripped string or ''.
        """
        if raw is None:
            return ""
        # If it's a list-like, take first element
        try:
            if isinstance(raw, (list, tuple)) and raw:
                raw = raw[0]
        except Exception:
            pass
        try:
            return str(raw).strip()
        except Exception:
            return ""

    def dreame_loginAccount(self, values_dict, type_id, dev_id):
        """
        Device config button callback (Login Dreame / Mi Cloud).
        Tries a cloud login with current creds; if 2FA is needed, tells user via dreameLoginInfo.
        """
        try:
            dev = indigo.devices[dev_id]
        except Exception:
            self.logger.error(f"dreame_loginAccount: invalid device id {dev_id}")
            return values_dict

        login_mode = self._get_menu_value(values_dict.get("loginMode")).lower()
        account_type_raw = self._get_menu_value(values_dict.get("accountType")).lower()

        if login_mode != "cloud":
            msg = "Cloud login only applies when Login Mode is 'Cloud'."
            self.logger.info(f"{dev.name}: {msg}")
            self._update_dreame_login_info(dev, msg)
            return values_dict

        username = self._get_menu_value(values_dict.get("username"))
        password = self._get_menu_value(values_dict.get("password"))
        country = self._get_menu_value(values_dict.get("country")) or "sg"
        country = country.lower()

        if not username or not password:
            msg = "Username and password are required for cloud login."
            self.logger.info(f"{dev.name}: {msg}")
            self._update_dreame_login_info(dev, msg)
            return values_dict

        # HA-style normalization
        if account_type_raw == "mihome":
            account_type = "mi"
        elif account_type_raw in ("dreame", "mova", "local"):
            account_type = account_type_raw
        else:
            account_type = "mi"

        self.logger.info(
            f"Testing cloud login for '{dev.name}' (account_type={account_type}, country={country})"
        )

        try:
            from dreame.protocol import DreameVacuumProtocol

            proto = DreameVacuumProtocol(
                username=username,
                password=password,
                country=country,
                prefer_cloud=True,
                account_type=account_type,
            )

            cloud = proto.cloud
            if cloud is None:
                msg = "Cloud login not available for this account type."
                self.logger.info(f"{dev.name}: {msg}")
                self._update_dreame_login_info(dev, msg)
                return values_dict

            ok = cloud.login()
            self.logger.debug(
                f"Cloud login result: ok={ok}, logged_in={cloud.logged_in}, "
                f"auth_failed={cloud.auth_failed}, verification_url={getattr(cloud, 'verification_url', None)!r}"
            )

            if cloud.logged_in and not cloud.auth_failed:
                auth_key = getattr(cloud, "auth_key", None)
                msg = "Cloud login OK."
                if auth_key:
                    msg += " Auth key saved."
                self._update_dreame_login_info(dev, msg, auth_key=auth_key)
            else:
                v_url = getattr(cloud, "verification_url", None)
                if v_url:
                    msg = (
                        "2FA required. Check your phone/email, then enter the verification "
                        "code below and press 'Submit Code'."
                    )
                else:
                    msg = "Cloud login failed. Check credentials or try again."
                self._update_dreame_login_info(dev, msg)

        except Exception as exc:
            self.logger.error(f"Cloud login test failed for '{dev.name}': {exc}")
            self._update_dreame_login_info(dev, f"Cloud login error: {exc}")

        return values_dict

    def dreame_submitCode(self, values_dict, type_id, dev_id):
        """
        Device config button callback (Submit Code).
        Sends 2FA verification code for Mi Home accounts.
        """
        try:
            dev = indigo.devices[dev_id]
        except Exception:
            self.logger.error(f"dreame_submitCode: invalid device id {dev_id}")
            return values_dict

        code = self._get_menu_value(values_dict.get("dreameVerificationCode"))

        if not code:
            msg = "No 2FA code entered."
            self.logger.info(f"{dev.name}: {msg}")
            self._update_dreame_login_info(dev, msg)
            return values_dict

        login_mode = self._get_menu_value(values_dict.get("loginMode")).lower()
        account_type_raw = self._get_menu_value(values_dict.get("accountType")).lower()

        if login_mode != "cloud":
            msg = "2FA submission only applies when Login Mode is 'Cloud'."
            self.logger.info(f"{dev.name}: {msg}")
            self._update_dreame_login_info(dev, msg)
            return values_dict

        if account_type_raw != "mihome":
            msg = "2FA is only used for Xiaomi / Mi Home accounts."
            self.logger.info(f"{dev.name}: {msg}")
            self._update_dreame_login_info(dev, msg)
            return values_dict

        username = self._get_menu_value(values_dict.get("username"))
        password = self._get_menu_value(values_dict.get("password"))
        country = self._get_menu_value(values_dict.get("country")) or "eu"
        country = country.lower()

        if not username or not password:
            msg = "Username and password are required to verify 2FA."
            self.logger.info(f"{dev.name}: {msg}")
            self._update_dreame_login_info(dev, msg)
            return values_dict

        # previously stored authKey (may be empty before first login)
        stored_auth = (dev.pluginProps.get("authKey") or "").strip() or None

        self.logger.info(f"Submitting 2FA code for '{dev.name}'")

        try:
            from dreame.protocol import DreameVacuumProtocol

            proto = DreameVacuumProtocol(
                username=username,
                password=password,
                country=country,
                prefer_cloud=True,
                account_type="mi",
                device_id=None,
                auth_key=stored_auth,
            )

            cloud = proto.cloud
            if cloud is None:
                msg = "Cloud protocol not available for 2FA."
                self.logger.info(f"{dev.name}: {msg}")
                self._update_dreame_login_info(dev, msg)
                return values_dict

            ok = cloud.verify_code(code)
            self.logger.debug(
                f"2FA verify_code result: ok={ok}, logged_in={cloud.logged_in}, "
                f"auth_failed={cloud.auth_failed}"
            )

            if ok and cloud.logged_in and not cloud.auth_failed:
                new_key = getattr(cloud, "auth_key", None)
                msg = "2FA verification successful. Auth key updated."
                self._update_dreame_login_info(dev, msg, auth_key=new_key)
            else:
                msg = "2FA verification failed. Check code and try again."
                self._update_dreame_login_info(dev, msg)

        except Exception as exc:
            self.logger.error(f"2FA verification failed for '{dev.name}': {exc}")
            self._update_dreame_login_info(dev, f"2FA error: {exc}")

        return values_dict

    ##

    ## END 2FA CODE

    def test_dreame_cloud_login(self, username: str, password: str, country: str = "sg"):
        """
        Directly call DreameVacuumCloudProtocol.login() with given creds/region.
        Logs result via Indigo plugin logger (self.logger) and returns the protocol object.
        Usage in shell:
            p = indigo.activePlugin
            p.logger.info("Running Dreame cloud login test...")
            proto = test_dreame_cloud_login("YOUR_EMAIL", "YOUR_PASSWORD", "sg")
        """
        logger = indigo.activePlugin.logger
        logger.info(f"=== Dreame cloud login diagnostic ===")
        logger.info(f"Username set: {bool(username)}, country='{country}'")

        proto = DreameVacuumCloudProtocol(username, password, country, auth_key=None, device_id=None)
        logger.debug(
            f"Initial DreameVacuumCloudProtocol state: logged_in={getattr(proto, '_logged_in', None)}, auth_failed={getattr(proto, '_auth_failed', None)}")

        try:
            ok = proto.login()
            logger.info(f"login() returned: {ok}")
        except Exception as exc:
            logger.error(f"DreameVacuumCloudProtocol.login() raised: {exc}")
            logger.error(traceback.format_exc())
            return None

        logger.info(
            f"After login: logged_in={getattr(proto, '_logged_in', None)}, auth_failed={getattr(proto, '_auth_failed', None)}")
        logger.info(
            f"userId={getattr(proto, '_userId', None)}, ssecurity={bool(getattr(proto, '_ssecurity', None))}, serviceToken={bool(getattr(proto, '_service_token', None))}")
        logger.info(f"country={getattr(proto, '_country', None)}, locale={getattr(proto, '_locale', None)}")
        logger.info(f"======================================")

        return proto

    # Async skeleton from your instructions
    ########################################
    def startup(self) -> None:
        self.logger.debug("startup called")

        self._event_loop = asyncio.new_event_loop()
        self._async_thread = threading.Thread(target=self._run_async_thread, name="DreameAsync", daemon=True)
        self._async_thread.start()


    def shutdown(self) -> None:
        self.logger.debug("shutdown called")
        self.stopThread = True

    def _run_async_thread(self) -> None:
        self.logger.debug("_run_async_thread starting")
        assert self._event_loop is not None
        asyncio.set_event_loop(self._event_loop)

        self._event_loop.create_task(self._async_start())
        self._event_loop.run_until_complete(self._async_stop())

        self._event_loop.close()

    async def _async_start(self) -> None:
        self.logger.debug("_async_start")
        self.logger.debug("Starting event loop and setting up any connections")
        # nothing global yet

    async def _async_stop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            if self.stopThread:
                # Disconnect all clients & cancel polls
                for dev_id, t in list(self._poll_tasks.items()):
                    if t and not t.done():
                        t.cancel()
                for dev_id, t in list(self._map_tasks.items()):
                    if t and not t.done():
                        t.cancel()
                for dev_id, client in list(self._clients.items()):
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                self._poll_tasks.clear()
                self._map_tasks.clear()
                self._clients.clear()
                break

    ########################################
    # Plugin config
    ########################################
    ########################################
    # Plugin config
    ########################################
    def closedPrefsConfigUi(self, values_dict: indigo.Dict, user_cancelled: bool) -> None:
        self.logger.debug(f"closedPluginConfigUi called (cancelled={user_cancelled})")
        if user_cancelled:
            return

        try:
            self.pluginPrefs["showDebugInfo"] = bool(values_dict.get("showDebugInfo", False))
            self.pluginPrefs["showDebugLevel"] = int(values_dict.get("showDebugLevel", logging.INFO))
            self.pluginPrefs["showDebugFileLevel"] = int(values_dict.get("showDebugFileLevel", logging.DEBUG))
            indigo.server.savePluginPrefs()

            self.logLevel = int(values_dict.get("showDebugLevel", logging.INFO))
            self.fileloglevel = int(values_dict.get("showDebugFileLevel", logging.DEBUG))
            show_lib_debug = bool(values_dict.get("showDebugInfo", False))

            # Update handler levels
            if hasattr(self, "indigo_log_handler") and self.indigo_log_handler:
                self.indigo_log_handler.setLevel(self.logLevel)
            if hasattr(self, "plugin_file_handler") and self.plugin_file_handler:
                self.plugin_file_handler.setLevel(self.fileloglevel)

            # Rewire library loggers according to updated showDebugInfo
            try:
                lib_indigo_handler = None
                if show_lib_debug:
                    lib_indigo_handler = IndigoLogHandler(self.pluginDisplayName, level=logging.DEBUG, force_debug=True)
                    lib_indigo_handler.setFormatter(logging.Formatter("%(message)s"))

                def _rewire(name: str, to_indigo: bool):
                    lg = logging.getLogger(name)
                    lg.setLevel(logging.DEBUG)
                    lg.handlers[:] = []
                    lg.addHandler(self.plugin_file_handler)
                    if to_indigo and lib_indigo_handler:
                        lg.addHandler(lib_indigo_handler)
                    lg.propagate = False

                _rewire("dreame_client", show_lib_debug)
                _rewire("dreame_camera", show_lib_debug)
                _rewire("dreame", show_lib_debug)
                _rewire("miio", show_lib_debug)

                for http_name in ("urllib3", "requests"):
                    hl = logging.getLogger(http_name)
                    hl.setLevel(logging.WARNING)
                    hl.handlers[:] = []
                    hl.addHandler(self.plugin_file_handler)
                    hl.propagate = False
            except Exception as exc:
                self.logger.debug(f"Rewiring library loggers failed: {exc}")

            self.logger.debug(f"logLevel = {self.logLevel}")
            self.logger.debug("User prefs saved.")
            self.logger.debug(
                f"Applied logging prefs: EventLog={logging.getLevelName(self.logLevel)}, "
                f"File={logging.getLevelName(self.fileloglevel)}, "
                f"LibraryInIndigo={show_lib_debug}"
            )
        except Exception as exc:
            self.logger.exception(exc)

    ########################################
    # Device config
    ########################################
    ########################################
    # Device config
    ########################################
    def validateDeviceConfigUi(self, values_dict: indigo.Dict, type_id: str, dev_id: int):
        errors = {}
        login_mode = (values_dict.get("loginMode") or "cloud").strip().lower()

        if login_mode == "cloud":
            username = (values_dict.get("username") or "").strip()
            password = (values_dict.get("password") or "").strip()
            if not username:
                errors["username"] = "Username / email is required for cloud mode."
            if not password:
                errors["password"] = "Password is required for cloud mode."
            country = (values_dict.get("country") or "").strip()
            if not country:
                errors["country"] = "Country code is required for cloud mode."
        else:
            host = (values_dict.get("host") or "").strip()
            token = (values_dict.get("token") or "").strip()
            if not host:
                errors["host"] = "Local IP/host is required for local mode."
            if not token:
                errors["token"] = "Local device token is required for local mode."

        if errors:
            return (False, errors, values_dict)
        return (True, values_dict)
    ########################################
    # Device lifecycle
    ########################################
    ########################################
    # Device lifecycle
    ########################################
    def deviceStartComm(self, dev: indigo.Device) -> None:
        if dev.deviceTypeId != "dreame_vacuum":
            return

        self.logger.info(f"Starting Dreame vacuum '{dev.name}'")
        dev.stateListOrDisplayStateIdChanged()

        # Reflect mapping_updates_enabled state from config (optional)
        try:
            ena = bool(dev.pluginProps.get("enableMappingUpdates", False))
            dev.updateStateOnServer("mapping_updates_enabled", ena)
        except Exception:
            pass

        # Initialise extended states to something sane
        try:
            kv = [
                # Core summary
                {"key": "status", "value": "Initializing"},
                {"key": "battery", "value": 0},
                {"key": "fan_speed", "value": ""},
                {"key": "area_cleaned_m2", "value": 0.0},
                {"key": "duration_min", "value": 0},
                {"key": "shortcuts", "value": ""},
                {"key": "charging", "value": False},
                {"key": "error_text", "value": ""},
                {"key": "last_update", "value": ""},

                # Extended robot / dock / task states
                {"key": "robot_state", "value": "Unknown"},
                {"key": "robot_state_detail", "value": "Initializing"},
                {"key": "station_state", "value": ""},
                {"key": "task_status", "value": ""},
                {"key": "cleaning_mode", "value": ""},

                # Cleaning parameters
                {"key": "water_volume", "value": 0},
                {"key": "mop_wetness_level", "value": 0},
                {"key": "cleaning_progress", "value": 0},

                # Base / self-wash / drying
                {"key": "self_wash_base_status", "value": ""},
                {"key": "drying_progress", "value": 0},
                {"key": "auto_empty_status", "value": ""},
                {"key": "station_drainage_status", "value": ""},

                # Combined / water temp
                {"key": "combined_status", "value": "Initializing"},
                {"key": "water_temperature", "value": ""},

                # Consumables / health
                {"key": "main_brush_left", "value": 0},
                {"key": "side_brush_left", "value": 0},
                {"key": "filter_left", "value": 0},
                {"key": "dirty_water_tank_left", "value": 0},
                {"key": "scale_inhibitor_left", "value": 0},

                # AI / mapping capability summary
                {"key": "ai_obstacle_detection", "value": False},
                {"key": "ai_pet_detection", "value": False},

                # Map / room metadata
                {"key": "map_list", "value": ""},
                {"key": "current_map_id", "value": 0},
                {"key": "multi_floor_map", "value": False},
                {"key": "mapping_updates_enabled", "value": bool(dev.pluginProps.get("enableMappingUpdates", False))},
                {"key": "selected_map", "value": ""},
                {"key": "room_list", "value": ""},
                {"key": "current_room", "value": ""},
                {"key": "current_segment_id", "value": 0},
                {"key": "cleaning_sequence", "value": ""},

                # Raw vacuum_state
                {"key": "vacuum_state", "value": ""},
            ]
            dev.updateStatesOnServer(kv)
        except Exception:
            pass

        if hasattr(dev, 'onState') == False:  ## if custom
            self.logger.debug("onState Not in Props converting device..")
            device = indigo.device.changeDeviceTypeId(dev, dev.deviceTypeId)
            device.replaceOnServer()

        device = indigo.devices[dev.id]
        props = device.pluginProps
        props["SupportsSensorValue"] = True
        props["SupportsOnState"] = True
        props["AllowSensorValueChange"] = False
        props["AllowOnStateChange"] = True
        props["SupportsStatusRequest"] = False
        device.replacePluginPropsOnServer(props)
        device.updateStateOnServer(key="onOffState", value=False)

        if self._event_loop:
            asyncio.run_coroutine_threadsafe(self._async_device_connect(dev), self._event_loop)


    def deviceStopComm(self, dev: indigo.Device) -> None:
        if dev.deviceTypeId != "dreame_vacuum":
            return

        self.logger.info(f"Stopping Dreame vacuum '{dev.name}'")
        client = self._clients.pop(dev.id, None)
        task = self._poll_tasks.pop(dev.id, None)
        map_task = self._map_tasks.pop(dev.id, None)

        if task and self._event_loop:
            task.cancel()
        if map_task and self._event_loop:
            map_task.cancel()
        if client and self._event_loop:
            asyncio.run_coroutine_threadsafe(client.disconnect(), self._event_loop)

    ########################################
    # Actions (relay semantics)
    ########################################
    # Actions.xml callbacks (add below set_fan_speed)
    # ----- Basic Actions.xml commands -----
    def start_shortcut(self, plugin_action, dev):
        """
        Action: start a Dreame shortcut (favourite) by id selected from menu.
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.debug(f"start_shortcut: props={plugin_action.props}")

        # Indigo menus often return ['32'] not '32'
        sid_raw = self._get_menu_value(plugin_action.props.get("shortcutMenu"))

        if not sid_raw:
            self.logger.error(f"Start Shortcut: no shortcut selected for '{dev.name}'")
            return

        # Keep as string; dreame lib is fine with string IDs
        self.logger.info(f"Start Shortcut: id={sid_raw!r} requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(
            self._async_start_shortcut(dev, sid_raw),
            self._event_loop,
        )

    async def _async_start_shortcut(self, dev: indigo.Device, shortcut_id: str):
        client = self._clients.get(dev.id)
        if not client:
            self.logger.error(f"_async_start_shortcut: no client for '{dev.name}'")
            self._update_status(dev, "Not connected")
            return

        # Dreame/Mova shortcuts are numeric ids; pass int when possible
        try:
            sid = int(shortcut_id)
        except Exception:
            sid = shortcut_id  # fall back to raw string

        self.logger.debug(
            f"_async_start_shortcut: calling client.start_shortcut({sid!r}) for '{dev.name}'"
        )

        try:
            await client.start_shortcut(sid)
            self._update_status(dev, f"Shortcut {shortcut_id} started")
        except Exception as exc:
            self.logger.error(
                f"_async_start_shortcut failed for '{dev.name}', shortcut_id={shortcut_id!r}: {exc}"
            )
            self._update_status(dev, f"Start shortcut failed: {exc}")

    def start_washing(self, plugin_action, dev):
        """
        Action: start washing (self-wash base).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Start washing requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(
            self._async_call_client_wash_action(dev, "start_washing"),
            self._event_loop,
        )

    def pause_washing(self, plugin_action, dev):
        """
        Action: pause washing (self-wash base).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Pause washing requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(
            self._async_call_client_wash_action(dev, "pause_washing"),
            self._event_loop,
        )

    def start_drying(self, plugin_action, dev):
        """
        Action: start drying (self-wash base).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Start drying requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(
            self._async_call_client_wash_action(dev, "start_drying"),
            self._event_loop,
        )

    def stop_drying(self, plugin_action, dev):
        """
        Action: stop drying (self-wash base).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Stop drying requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(
            self._async_call_client_wash_action(dev, "stop_drying"),
            self._event_loop,
        )

    def start_draining(self, plugin_action, dev):
        """
        Action: start draining (self-wash base).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        clean_tank = bool(plugin_action.props.get("cleanWaterTank"))
        self.logger.info(
            f"Start draining requested for '{dev.name}' (clean_water_tank={clean_tank})"
        )
        asyncio.run_coroutine_threadsafe(
            self._async_call_client_wash_action(dev, "start_draining", clean_tank),
            self._event_loop,
        )

    async def _async_call_client_wash_action(self, dev: indigo.Device, method: str, *args):
        """
        Helper: call AsyncDreameClient washing/drying/draining coroutines by name.
        """
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        fn = getattr(client, method, None)
        if not fn or not asyncio.iscoroutinefunction(fn):
            self.logger.error(f"_async_call_client_wash_action: client has no async method {method}")
            return
        try:
            await fn(*args)
            self._update_status(dev, f"{method} OK")
        except Exception as exc:
            self.logger.error(
                f"_async_call_client_wash_action({method}) failed for '{dev.name}': {exc}"
            )
            self._update_status(dev, f"{method} failed: {exc}")

    def start_clean(self, plugin_action, dev):
        """
        Action: start/resume cleaning (HA async_start equivalent).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Start cleaning requested (action) for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_start_clean(dev), self._event_loop)

    def start_pause(self, plugin_action, dev):
        """
        Action: start or pause cleaning (HA async_start_pause equivalent).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Start/Pause requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_start_pause(dev), self._event_loop)

    async def _async_start_pause(self, dev: indigo.Device):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.start_pause()
            # status will be corrected by next poll; simple message here
            self._update_status(dev, "Start/Pause command sent")
        except Exception as exc:
            self._update_status(dev, f"Start/Pause failed: {exc}")

    def stop_clean(self, plugin_action, dev):
        """
        Action: stop cleaning (HA async_stop equivalent).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Stop cleaning requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_stop_clean(dev), self._event_loop)

    async def _async_stop_clean(self, dev: indigo.Device):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.stop_cleaning()
            self._update_status(dev, "Cleaning stopped")
        except Exception as exc:
            self._update_status(dev, f"Stop failed: {exc}")

    def pause_clean(self, plugin_action, dev):
        """
        Action: pause cleaning (HA async_pause equivalent).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Pause cleaning requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_pause_clean(dev), self._event_loop)

    async def _async_pause_clean(self, dev: indigo.Device):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.pause()
            self._update_status(dev, "Cleaning paused")
        except Exception as exc:
            self._update_status(dev, f"Pause failed: {exc}")

    def return_to_base_action(self, plugin_action, dev):
        """
        Action: return to base (HA async_return_to_base).
        Wrapper just to avoid clashing with _async_return_to_dock.
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Return to base requested (action) for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_return_to_dock(dev), self._event_loop)

    def custom_clean_room(self, plugin_action, dev):
        """
        Action: custom clean a single room (segment) with specific suction/water/repeats/mode.
        Uses menu-driven values that broadly match the Dreame library expectations.
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return

        # Room menu returns segment id as value string
        seg_str = (plugin_action.props.get("roomMenu") or "").strip()
        if not seg_str:
            self.logger.error(f"Custom Clean Room: no room selected for '{dev.name}'")
            return

        try:
            segment_id = int(seg_str)
        except Exception:
            self.logger.error(f"Custom Clean Room: invalid segment id '{seg_str}' for '{dev.name}'")
            return

        # Menus: suction_level, water_volume, repeats, cleaning_mode, wetness_level
        suction_level = (plugin_action.props.get("suction_level") or "").strip()
        water_volume = (plugin_action.props.get("water_volume") or "").strip()
        repeats_str = (plugin_action.props.get("repeats") or "").strip() or "1"

        if not suction_level or not water_volume:
            self.logger.error(
                f"Custom Clean Room: suction_level and water_volume are required for '{dev.name}'"
            )
            return

        try:
            repeats = int(repeats_str)
        except Exception:
            repeats = 1

        cleaning_mode_raw = (plugin_action.props.get("cleaning_mode") or "").strip()
        wetness_raw = (plugin_action.props.get("wetness_level") or "").strip()

        cleaning_mode = cleaning_mode_raw or None
        wetness_level = wetness_raw or None

        self.logger.info(
            f"Custom clean room segment {segment_id} requested for '{dev.name}' "
            f"(suction={suction_level!r}, water={water_volume!r}, repeats={repeats}, "
            f"mode={cleaning_mode!r}, wetness={wetness_level!r})"
        )

        asyncio.run_coroutine_threadsafe(
            self._async_custom_clean_room(
                dev,
                segment_id=segment_id,
                suction_level=suction_level,
                water_volume=water_volume,
                repeats=repeats,
                cleaning_mode=cleaning_mode,
                wetness_level=wetness_level,
            ),
            self._event_loop,
        )

    async def _async_custom_clean_room(
        self,
        dev: indigo.Device,
        segment_id: int,
        suction_level: str,
        water_volume: str,
        repeats: int,
        cleaning_mode: str | None = None,
        wetness_level: str | None = None,
    ):
        """
        Build single-element arrays and call AsyncDreameClient.set_custom_cleaning.
        All values come from Actions.xml menus.
        """
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return

        # HA passes lists; we mirror that for a single segment.
        segment_ids = [segment_id]
        suction_levels = [suction_level]
        water_volumes = [water_volume]
        repeats_list = [repeats]

        cleaning_modes = [cleaning_mode] if cleaning_mode is not None else None
        wetness_levels = [wetness_level] if wetness_level is not None else None

        cleaning_routes = None
        custom_mopping_routes = None

        try:
            await client.set_custom_cleaning(
                segment_ids=segment_ids,
                suction_levels=suction_levels,
                water_volumes=water_volumes,
                repeats=repeats_list,
                cleaning_modes=cleaning_modes,
                wetness_levels=wetness_levels,
                cleaning_routes=cleaning_routes,
                custom_mopping_routes=custom_mopping_routes,
            )
            self._update_status(dev, f"Custom cleaning room {segment_id}")
        except Exception as exc:
            self._update_status(dev, f"Custom clean failed: {exc}")


    def actionControlDevice(self, action, dev):
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return

        # Read relay mapping from device props (menus can be list-like)
        props = dev.pluginProps
        relay_on_action = self._get_menu_value(props.get("relayOnAction")) or "start_clean"
        relay_off_action = self._get_menu_value(props.get("relayOffAction")) or "dock"
        relay_on_shortcut = self._get_menu_value(props.get("relayOnShortcut"))
        relay_off_shortcut = self._get_menu_value(props.get("relayOffShortcut"))

        if action.deviceAction == indigo.kDeviceAction.TurnOn:
            # ON: either start cleaning or run shortcut
            if relay_on_action == "shortcut" and relay_on_shortcut:
                self.logger.info(
                    f"Relay ON → start shortcut {relay_on_shortcut!r} for '{dev.name}'"
                )
                asyncio.run_coroutine_threadsafe(
                    self._async_start_shortcut(dev, relay_on_shortcut),
                    self._event_loop,
                )
            else:
                self.logger.info(f"Relay ON → start cleaning for '{dev.name}'")
                asyncio.run_coroutine_threadsafe(
                    self._async_start_clean(dev),
                    self._event_loop,
                )

        elif action.deviceAction == indigo.kDeviceAction.TurnOff:
            # OFF: dock, pause, stop, or shortcut
            if relay_off_action == "pause":
                self.logger.info(f"Relay OFF → pause cleaning for '{dev.name}'")
                asyncio.run_coroutine_threadsafe(
                    self._async_pause_clean(dev),
                    self._event_loop,
                )
            elif relay_off_action == "stop":
                self.logger.info(f"Relay OFF → stop cleaning for '{dev.name}'")
                asyncio.run_coroutine_threadsafe(
                    self._async_stop_clean(dev),
                    self._event_loop,
                )
            elif relay_off_action == "shortcut" and relay_off_shortcut:
                self.logger.info(
                    f"Relay OFF → start shortcut {relay_off_shortcut!r} for '{dev.name}'"
                )
                asyncio.run_coroutine_threadsafe(
                    self._async_start_shortcut(dev, relay_off_shortcut),
                    self._event_loop,
                )
            else:
                # Default: return to dock
                self.logger.info(f"Relay OFF → return to dock for '{dev.name}'")
                asyncio.run_coroutine_threadsafe(
                    self._async_return_to_dock(dev),
                    self._event_loop,
                )

        elif action.deviceAction == indigo.kDeviceAction.Toggle:
            # Keep simple toggle heuristic based on status text
            status = (dev.states.get("status") or "").lower()
            if any(x in status for x in ("clean", "zone", "segment")):
                self.logger.info(f"Toggling OFF (dock) '{dev.name}'")
                asyncio.run_coroutine_threadsafe(
                    self._async_return_to_dock(dev),
                    self._event_loop,
                )
            else:
                self.logger.info(f"Toggling ON (clean) '{dev.name}'")
                asyncio.run_coroutine_threadsafe(
                    self._async_start_clean(dev),
                    self._event_loop,
                )

    # Actions.xml
    ########################################
    # Actions.xml
    ########################################
    def locate_vacuum(self, plugin_action, dev):
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Locate vacuum requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_locate(dev), self._event_loop)

    def set_fan_speed(self, plugin_action, dev):
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        speed = plugin_action.props.get("speed", "")
        self.logger.info(f"Set fan speed '{speed}' requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_set_fan_speed(dev, speed), self._event_loop)

    # Add this new Indigo Action callback to Plugin class, alongside locate_vacuum / set_fan_speed

    def save_map_snapshot(self, plugin_action, dev):
        """
        Action: save current floor map snapshot as PNG into ~/Pictures.
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"Map snapshot requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_save_map_snapshot(dev, wifi=False), self._event_loop)

    def save_wifi_map_snapshot(self, plugin_action, dev):
        """
        Action: save current WiFi coverage map snapshot as PNG into ~/Pictures (if available).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return
        self.logger.info(f"WiFi map snapshot requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(self._async_save_wifi_map_snapshot(dev), self._event_loop)
    ########################################
    # Async methods
    ########################################
    async def _async_device_connect(self, dev: indigo.Device):
        """Create AsyncDreameClient, perform HA-like cloud login/discovery (if needed), and start polling."""
        try:
            if not self._event_loop:
                return

            props = dev.pluginProps
            username = (props.get("username") or "").strip() or None
            password = (props.get("password") or "").strip() or None
            country = (props.get("country") or "").strip().lower() or "eu"
            login_mode = (props.get("loginMode") or "cloud").strip().lower()
            raw_account_type = props.get("accountType")
            dreame_device_id = (props.get("dreame_device_id") or "").strip() or None

            # Normalize Indigo menu value to a lower-case string:
            # - menu can be a list like ['mova'] or just 'mova'
            def _normalize_account_type(raw):
                if raw is None:
                    return "dreame"
                try:
                    if isinstance(raw, (list, tuple)) and raw:
                        raw = raw[0]
                except Exception:
                    pass
                try:
                    at = str(raw).strip().lower()
                    return at or "dreame"
                except Exception:
                    return "dreame"

            account_type_raw_str = _normalize_account_type(raw_account_type)

            self.logger.debug(
                f"_async_device_connect: dev='{dev.name}', loginMode={login_mode!r}, "
                f"username_set={bool(username)}, country={country!r}, dreame_device_id={dreame_device_id!r}, "
                f"raw_account_type={raw_account_type!r} -> account_type_raw_str={account_type_raw_str!r}"
            )

            host_for_client = None
            token_for_client = None
            mac_for_client = None
            device_id_for_client = None
            auth_key_for_client = None
            prefer_cloud = True

            # ====== CLOUD MODE (HA-like) ======
            if login_mode == "cloud":
                cloud_info = await self._cloud_login_and_pick_device(
                    dev=dev,
                    username=username,
                    password=password,
                    country=country,
                    account_type_raw=account_type_raw_str,
                )
                if not cloud_info:
                    self._update_status(dev, "Cloud login or discovery failed")
                    return

                # cloud_info already has HA-style account_type ("mi"/"dreame"/"mova")
                account_type_norm = cloud_info["account_type"]
                host_for_client = cloud_info["host"]
                token_for_client = cloud_info["token"]
                mac_for_client = cloud_info["mac"]
                device_id_for_client = cloud_info["device_id"]
                auth_key_for_client = cloud_info["auth_key"]

                self.logger.debug(
                    f"Cloud login/discovery OK for '{dev.name}': host={host_for_client!r}, "
                    f"token_len={len(token_for_client) if token_for_client else 0}, "
                    f"mac={mac_for_client!r}, did={device_id_for_client!r}, "
                    f"account_type={account_type_norm!r}, auth_key_present={bool(auth_key_for_client)}"
                )

                final_account_type = account_type_norm

            # ====== LOCAL MODE (manual IP + token, no cloud) ======
            else:
                host_for_client = (props.get("host") or "").strip() or None
                token_for_client = (props.get("token") or "").strip() or None
                prefer_cloud = False
                final_account_type = "local"

                self.logger.debug(
                    f"Local mode for '{dev.name}': host={host_for_client!r}, "
                    f"token_len={len(token_for_client) if token_for_client else 0}"
                )

                if not host_for_client or not token_for_client:
                    self._update_status(dev, "Local mode requires host and token")
                    return

            # Build AsyncDreameClient with the discovered / entered details
            client = AsyncDreameClient(
                loop=self._event_loop,
                name=dev.name,
                host=host_for_client,
                token=token_for_client,
                mac=mac_for_client,
                username=username,
                password=password,
                country=country,
                prefer_cloud=prefer_cloud,
                device_id=device_id_for_client,
                auth_key=auth_key_for_client,
                account_type=final_account_type,
            )

            self._clients[dev.id] = client
            await client.connect()
            self._update_status(dev, "Connected")

            await self._async_refresh_state(dev, client)

            if dev.id in self._poll_tasks and not self._poll_tasks[dev.id].done():
                self._poll_tasks[dev.id].cancel()
            self._poll_tasks[dev.id] = asyncio.create_task(self._poll_loop(dev, client))

            # Start map poll loop (only after client exists)
            if dev.id in self._map_tasks and not self._map_tasks[dev.id].done():
                self._map_tasks[dev.id].cancel()
            self._map_tasks[dev.id] = asyncio.create_task(self._map_poll_loop(dev))

        except DeviceException as de:
            import traceback as _tb
            self.logger.error(
                f"Dreame DeviceException while connecting '{dev.name}': {de}\n"
                f"Traceback:\n{_tb.format_exc()}"
            )
            self._update_status(dev, f"Dreame error: {de}")
        except Exception as exc:
            import traceback as _tb
            self.logger.error(
                f"Failed to connect Dreame for '{dev.name}': {exc}\n"
                f"Traceback:\n{_tb.format_exc()}"
            )
            self._update_status(dev, f"Error: {exc}")

## Save images
    def export_map_resources(self, plugin_action=None):
        """
        One-off utility: extract embedded PNG resources from dreame.map.resources
        and save them to disk for inspection/use.

        It looks for string attributes whose names suggest an image/icon.
        """
        import os
        import base64
        import zlib

        try:
            # In this repo, resources are in dreame/resources.py
            import dreame.resources as dreame_resources
        except Exception as exc:
            self.logger.error(f"export_map_resources: could not import dreame.resources: {exc}")
            return

        # Choose a target directory under Indigo's Pictures folder
        pics_dir = os.path.expanduser("~/Pictures")
        # e.g. "/Library/Application Support/Perceptive Automation/Indigo 2025.1"
        export_dir = os.path.join(pics_dir, "exported_resources")
        try:
            os.makedirs(export_dir, exist_ok=True)
        except Exception as exc:
            self.logger.error(f"export_map_resources: could not create export dir '{export_dir}': {exc}")
            return

        count = 0
        skipped = 0

        for name in dir(dreame_resources):
            if not name.isupper():
                continue
            # Only consider likely image/icon constants
            if not any(k in name for k in ("IMAGE", "ICON", "MAP_FONT", "DEFAULT_MAP")):
                continue

            value = getattr(dreame_resources, name, None)
            if not isinstance(value, str):
                skipped += 1
                continue

            # Determine extension: font vs PNG
            if name.startswith("MAP_FONT"):
                ext = ".ttf"
            else:
                ext = ".png"

            # Some assets may be raw PNG base64, some may be gzipped+base64
            data = None
            b = None
            try:
                b = base64.b64decode(value, validate=False)
            except Exception:
                # Not base64, skip
                skipped += 1
                continue

            # Try raw first
            try:
                if ext == ".png":
                    # crude PNG header check
                    if b.startswith(b"\x89PNG\r\n\x1a\n"):
                        data = b
                    else:
                        # maybe gzip-wrapped png
                        data = zlib.decompress(b, zlib.MAX_WBITS | 32)
                else:
                    # font or other
                    data = zlib.decompress(b, zlib.MAX_WBITS | 32)
            except Exception:
                # if decompression fails, just use raw
                if ext == ".png":
                    data = b
                else:
                    skipped += 1
                    continue

            # Write file
            safe_name = name.lower().replace("map_", "").replace("__", "_")
            filename = f"{safe_name}{ext}"
            out_path = os.path.join(export_dir, filename)
            try:
                with open(out_path, "wb") as f:
                    f.write(data)
                count += 1
            except Exception as exc:
                self.logger.error(f"export_map_resources: failed to write {out_path}: {exc}")
                skipped += 1

        self.logger.info(
            f"export_map_resources: wrote {count} files to '{export_dir}' (skipped {skipped} entries)."
        )

    ##

    async def _async_request_map(self, dev: indigo.Device):
        """
        Ask the Dreame device to send fresh map data.
        Uses the underlying send_command via AsyncDreameClient.send_raw_command.
        """
        await self._async_save_map_snapshot(dev, wifi=False)

    async def _map_poll_loop(self, dev: indigo.Device):
        """
        Periodically request map data while the vacuum is actively cleaning
        and device config 'enableMappingUpdates' is True.
        """
        dev_id = dev.id
        self.logger.debug(f"Map poll loop starting for '{dev.name}'")

        while not self.stopThread and dev.id in self._clients:
            try:
                # Refresh dev reference (it may be reloaded)
                dev = indigo.devices.get(dev_id)
                if not dev or not dev.enabled or dev.deviceTypeId != "dreame_vacuum":
                    break

                # Check config flag
                enable = bool(dev.pluginProps.get("enableMappingUpdates", False))
                try:
                    dev.updateStateOnServer("mapping_updates_enabled", enable)
                except Exception:
                    pass

                if enable:
                    # --- Decide whether we should poll map data ---

                    # 1) Try to use rich status flags exported into Indigo states.
                    #    These are set in _async_refresh_state from s.attributes.
                    vacuum_state = (dev.states.get("vacuum_state") or "").lower()
                    robot_state = (dev.states.get("robot_state") or "").lower()
                    station_state = (dev.states.get("station_state") or "").lower()

                    # Some models expose a 'mapping' flag in attributes; we mirror it into states.
                    # If present and True, we should be polling.
                    mapping_flag = dev.states.get("mapping")
                    if mapping_flag is None:
                        # older devices or if we never set it → treat as unknown
                        is_mapping = False
                    else:
                        # any truthy value counts
                        is_mapping = bool(mapping_flag)

                    # Activity booleans we derive from our own states
                    segment_cleaning = bool(dev.states.get("segment_cleaning", False))
                    zone_cleaning = bool(dev.states.get("zone_cleaning", False))
                    spot_cleaning = bool(dev.states.get("spot_cleaning", False))
                    returning = bool(dev.states.get("returning", False))

                    # When washing at the base, map image changes (parking/wash zone etc.)
                    washing = "wash" in vacuum_state or "washing" in robot_state or "station_cleaning" in station_state

                    # 2) Fallback: old string-based status heuristic
                    status_txt = (dev.states.get("status") or "").lower()
                    active_keywords = ("clean", "mopp", "wash", "return", "spot")

                    is_active_text = any(k in status_txt for k in active_keywords)

                    # 3) Final decision:
                    is_active = (
                        is_mapping
                        or segment_cleaning
                        or zone_cleaning
                        or spot_cleaning
                        or returning
                        or washing
                        or is_active_text
                    )

                    if is_active:
                        self.logger.debug(
                            f"Map poll: requesting map update for '{dev.name}' "
                            f"(vacuum_state='{vacuum_state}', status='{status_txt}')"
                        )
                        # fire-and-forget; ignore errors
                        asyncio.create_task(self._async_request_map(dev))
                    else:
                        self.logger.debug(
                            f"Map poll: not active for '{dev.name}' "
                            f"(vacuum_state='{vacuum_state}', status='{status_txt}')"
                        )

                # sleep between map polls (currently ~15s)
                await asyncio.sleep(15.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error(f"Map poll error for '{dev.name}': {exc}")
                await asyncio.sleep(60.0)

        self.logger.debug(f"Map poll loop exiting for device id {dev_id}")


    async def _poll_loop(self, dev: indigo.Device, client: AsyncDreameClient):
        """Simple periodic polling using DreameVacuumDevice.update()."""
        while not self.stopThread and self._clients.get(dev.id) is client:
            try:
                await self._async_refresh_state(dev, client)
            except Exception as exc:
                self.logger.error(f"Poll error for '{dev.name}': {exc}")
            await asyncio.sleep(30)  # Adjust to taste

        # In plugin.py inside Plugin._async_refresh_state – wrap error_text and add a guard so
        # Indigo only ever sees valid primitive types.

        # plugin.py – replace _async_refresh_state with extended mapping + safe error_text coercion

    async def _async_refresh_state(self, dev: indigo.Device, client: AsyncDreameClient):
        """
        Refresh Indigo device states from DreameStatus and DreameVacuumDevice.status.
        Polling only (no push callbacks).
        """
        status: DreameStatus = await client.get_status()
        kv: list[dict] = []

        self.logger.debug(f"_async_refresh_state: dev='{dev.name}', status={status}")

        # --- Core summary states ---
        kv.append({"key": "status", "value": status.state_text})
        kv.append({"key": "battery", "value": int(status.battery)})
        kv.append({"key": "fan_speed", "value": status.fan_speed})
        kv.append({"key": "area_cleaned_m2", "value": float(status.area_m2)})
        kv.append({"key": "duration_min", "value": int(status.duration_min)})
        kv.append({"key": "charging", "value": bool(status.is_charging)})

        # error_text from library can be list/tuple; stringify safely
        if isinstance(status.error_text, (list, tuple)):
            safe_error_text = " | ".join(str(p) for p in status.error_text)
        else:
            safe_error_text = "" if status.error_text is None else str(status.error_text)
        kv.append({"key": "error_text", "value": safe_error_text})

        kv.append({"key": "last_update", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

        # --- On/off summary for relay-like integrations ---
        # Treat any active cleaning OR washing/drying/station work as "on".
        attrs = getattr(getattr(client, "_device", None), "status", None)
        attrs = getattr(attrs, "attributes", None) or {}

        vacuum_state = str(attrs.get("vacuum_state", "") or "").lower()
        seg_clean = bool(attrs.get("segment_cleaning", False))
        zone_clean = bool(attrs.get("zone_cleaning", False))
        spot_clean = bool(attrs.get("spot_cleaning", False))
        returning = bool(attrs.get("returning", False))
        washing = bool(attrs.get("washing", False)) or bool(attrs.get("self_clean", False))
        drying = bool(attrs.get("drying", False))
        station_cleaning = bool(attrs.get("station_cleaning", False))

        base_on = status.state.upper() in (
            "CLEANING",
            "AUTO_CLEANING",
            "AUTO_CLEAN",
            "ZONE_CLEANING",
            "SEGMENT_CLEANING",
            "BACK_HOME",
        )

        # Also treat vacuum_state mopping/washing/drying as "on"
        vs = vacuum_state
        vs_wash_or_mop = any(k in vs for k in ("mopp", "wash", "dry"))

        is_on = (
            base_on
            or seg_clean
            or zone_clean
            or spot_clean
            or returning
            or washing
            or drying
            or station_cleaning
            or vs_wash_or_mop
        )

        kv.append({"key": "onOffState", "value": bool(is_on)})

        # --- Extended states from DreameVacuumDevice.status ---
        device = getattr(client, "_device", None)
        s = getattr(device, "status", None)

        # Log attributes for debugging / exploration
        self.logger.debug(f"Dreame status attributes for '{dev.name}': {getattr(s, 'attributes', None)}")

        if s is not None:
            try:
                attrs = getattr(s, "attributes", None) or {}

                # Robot state / detail
                raw_state = getattr(s, "status", None) or getattr(s, "state", None)
                # raw_state can be an Enum or a string; attributes['status'] is already human-readable too
                if hasattr(raw_state, "name"):
                    state_str = raw_state.name
                else:
                    state_str = str(raw_state) if raw_state is not None else ""
                kv.append({"key": "robot_state", "value": state_str})
                kv.append({"key": "robot_state_detail", "value": status.state_text})

                # Live vacuum_state: 'mopping', 'drying', 'washing', etc.
                vacuum_state = attrs.get("vacuum_state")
                if vacuum_state is not None:
                    kv.append({"key": "vacuum_state", "value": str(vacuum_state)})

                # "Cleaning mode" here is a configuration (Sweeping/Mopping/etc.), not live state
                mode_config = attrs.get("cleaning_mode")
                if mode_config is not None:
                    kv.append({"key": "cleaning_mode", "value": str(mode_config)})

                # Water / mop parameters
                water_vol = getattr(s, "water_volume", None)
                if water_vol is not None:
                    try:
                        if hasattr(water_vol, "value"):
                            kv.append({"key": "water_volume", "value": int(getattr(water_vol, "value", 0))})
                        else:
                            kv.append({"key": "water_volume", "value": int(water_vol)})
                    except Exception:
                        pass

                # Station / dock state (if library exposes it)
                station_status = getattr(s, "station_status", None)
                if station_status is not None:
                    st_name = station_status.name if hasattr(station_status, "name") else str(station_status)
                    kv.append({"key": "station_state", "value": st_name})

                # Task status (current job type)
                try:
                    # Task status (derived current job type)
                    # Prefer Dreame booleans / vacuum_state, then fall back to text status.
                    seg_clean = bool(attrs.get("segment_cleaning", False))
                    zone_clean = bool(attrs.get("zone_cleaning", False))
                    spot_clean = bool(attrs.get("spot_cleaning", False))
                    shortcut_job = bool(attrs.get("shortcut_task", False))
                    self_clean = bool(attrs.get("self_clean", False))
                    washing = bool(attrs.get("washing", False))
                    drying = bool(attrs.get("drying", False))
                    vacuum_state = str(attrs.get("vacuum_state", "") or "").lower()
                    attr_status = str(attrs.get("status", "") or "")

                    derived_task = None
                    if shortcut_job:
                        derived_task = "Shortcut job"
                    elif seg_clean:
                        derived_task = "Segment cleaning"
                    elif zone_clean:
                        derived_task = "Zone cleaning"
                    elif spot_clean:
                        derived_task = "Spot cleaning"
                    elif washing or self_clean or "wash" in vacuum_state:
                        derived_task = "Self-cleaning / Washing mop"
                    elif drying or "dry" in vacuum_state:
                        derived_task = "Drying mop"
                    elif attr_status:
                        derived_task = attr_status
                    else:
                        derived_task = status.state_text or ""

                    kv.append({"key": "task_status", "value": derived_task})
                except:
                    self.logger.debug(f"Error deriving task status for '{dev.name}': {traceback.format_exc()}")

                # Water / mop parameters
                water_vol = getattr(s, "water_volume", None)
                if water_vol is not None:
                    try:
                        if hasattr(water_vol, "value"):
                            kv.append({"key": "water_volume", "value": int(getattr(water_vol, "value", 0))})
                        else:
                            kv.append({"key": "water_volume", "value": int(water_vol)})
                    except Exception:
                        pass

                mop_wet = attrs.get("wetness_level", None) or getattr(s, "wetness_level", None)
                if mop_wet is not None:
                    try:
                        kv.append({"key": "mop_wetness_level", "value": int(mop_wet)})
                    except Exception:
                        pass

                # Cleaning progress (%)
                cleaning_progress = attrs.get("cleaning_progress", None)
                if cleaning_progress is not None:
                    try:
                        kv.append({"key": "cleaning_progress", "value": int(cleaning_progress)})
                    except Exception:
                        pass

                # Water temperature (Normal/Mild/Warm/Hot)
                water_temp = attrs.get("water_temperature", None)
                if water_temp is not None:
                    kv.append({"key": "water_temperature", "value": str(water_temp)})

                # Self-wash / base status & drying
                base_status = getattr(s, "self_wash_base_status", None)
                if base_status is not None:
                    bs_name = base_status.name if hasattr(base_status, "name") else str(base_status)
                    kv.append({"key": "self_wash_base_status", "value": bs_name})

                drying_prog = attrs.get("drying_progress", None) or getattr(s, "drying_progress", None)
                if drying_prog is not None:
                    try:
                        kv.append({"key": "drying_progress", "value": int(drying_prog)})
                    except Exception:
                        pass

                # Auto-empty / drainage status
                auto_empty_status = attrs.get("auto_empty_status")
                if auto_empty_status is not None:
                    kv.append({"key": "auto_empty_status", "value": str(auto_empty_status)})

                station_drainage_status = attrs.get("station_drainage_status")
                if station_drainage_status is not None:
                    kv.append({"key": "station_drainage_status", "value": str(station_drainage_status)})

                # Consumables
                for src, dest in [
                    ("main_brush_left", "main_brush_left"),
                    ("side_brush_left", "side_brush_left"),
                    ("filter_left", "filter_left"),
                    ("dirty_water_tank_left", "dirty_water_tank_left"),
                    ("scale_inhibitor_left", "scale_inhibitor_left"),
                ]:
                    # prefer attributes dict where these already exist as ints
                    val = attrs.get(src, getattr(s, src, None))
                    if val is not None:
                        try:
                            kv.append({"key": dest, "value": int(val)})
                        except Exception:
                            pass

                # AI capabilities / flags
                for src, dest in [
                    ("ai_obstacle_detection", "ai_obstacle_detection"),
                    ("ai_pet_detection", "ai_pet_detection"),
                ]:
                    val = attrs.get(src, getattr(s, src, None))
                    if val is not None:
                        kv.append({"key": dest, "value": bool(val)})

                # Map / multi-floor
                map_list = getattr(s, "map_list", None)
                if map_list is not None:
                    kv.append({"key": "map_list", "value": str(map_list)})

                # selected_map (name + id)
                selected_map_name = attrs.get("selected_map")
                if selected_map_name is not None:
                    kv.append({"key": "selected_map", "value": str(selected_map_name)})

                selected_map_id = attrs.get("selected_map_id")
                if selected_map_id is not None:
                    try:
                        kv.append({"key": "current_map_id", "value": int(selected_map_id)})
                    except Exception:
                        pass

                multi_floor = attrs.get("multi_floor_map", getattr(s, "multi_floor_map", None))
                if multi_floor is not None:
                    kv.append({"key": "multi_floor_map", "value": bool(multi_floor)})

                # Rooms / segments: use attributes['rooms'] plus attributes['current_segment']
                rooms_map = attrs.get("rooms") or {}
                room_list_str = ""
                current_room_str = ""
                current_segment_id = attrs.get("current_segment")
                if isinstance(current_segment_id, dict):
                    # Just in case some models use a dict here
                    try:
                        current_segment_id = current_segment_id.get("id") or current_segment_id.get("segment_id")
                    except Exception:
                        pass

                # Shortcuts (favourites): attrs['shortcuts'] is usually a dict {id: {name: ...}, ...}
                shortcuts = attrs.get("shortcuts")
                if isinstance(shortcuts, dict) and shortcuts:
                    try:
                        # Build a simple CSV: "id:name, id:name, ..."
                        parts = []
                        for sid, meta in shortcuts.items():
                            try:
                                sid_str = str(sid)
                                nm = meta.get("name") if isinstance(meta, dict) else str(meta)
                                if not nm:
                                    continue
                                parts.append(f"{sid_str}:{nm}")
                            except Exception:
                                continue
                        if parts:
                            kv.append({"key": "shortcuts", "value": ", ".join(parts)})
                    except Exception:
                        pass

                # Build "id:name" CSV for current selected_map
                if isinstance(rooms_map, dict):
                    # keys are map names ("Downstairs")
                    sel_map = selected_map_name if selected_map_name in rooms_map else None
                    if sel_map is None and rooms_map:
                        # fallback: first key
                        sel_map = next(iter(rooms_map.keys()))
                    try:
                        entries = rooms_map.get(sel_map, []) if sel_map else []
                        parts = []
                        current_room_name = None
                        for room in entries:
                            rid = room.get("id")
                            nm = room.get("name")
                            if rid is None or not nm:
                                continue
                            parts.append(f"{rid}:{nm}")
                            if current_segment_id is not None and int(rid) == int(current_segment_id):
                                current_room_name = nm
                        if parts:
                            room_list_str = ", ".join(parts)
                            kv.append({"key": "room_list", "value": room_list_str})
                        if current_room_name is not None:
                            current_room_str = f"{current_segment_id}:{current_room_name}"
                            kv.append({"key": "current_room", "value": current_room_str})
                    except Exception:
                        # ignore room list errors, keep polling
                        pass

                # Raw current_segment id
                if current_segment_id is not None:
                    try:
                        kv.append({"key": "current_segment_id", "value": int(current_segment_id)})
                    except Exception:
                        pass

                # Cleaning sequence (list of segment IDs in order)
                cleaning_seq = attrs.get("cleaning_sequence")
                if cleaning_seq is not None:
                    try:
                        if isinstance(cleaning_seq, (list, tuple)):
                            seq_str = ",".join(str(int(x)) for x in cleaning_seq)
                        else:
                            seq_str = str(cleaning_seq)
                        kv.append({"key": "cleaning_sequence", "value": seq_str})
                    except Exception:
                        pass

            except Exception as exc:
                # Don't kill poll loop on mapping issues
                self.logger.debug(f"Extended state mapping failed for '{dev.name}': {exc}")
        ### Plain English Update
        # --- Build combined English status string ---
        try:
            combined = None

            # Prefer attributes dict if present
            attrs = getattr(s, "attributes", None) or {}

            # Basic pieces
            vacuum_state = attrs.get("vacuum_state")  # e.g. "mopping"
            attr_status_text = attrs.get("status")    # e.g. "Room cleaning"
            cleaning_progress = attrs.get("cleaning_progress")
            battery_pct = attrs.get("battery", status.battery)

            # Room name from earlier mapping (room_list/current_room/current_segment_id/rooms)
            rooms_map = attrs.get("rooms") or {}
            selected_map_name = attrs.get("selected_map")
            current_segment_id = attrs.get("current_segment")

            current_room_name = None
            if isinstance(current_segment_id, dict):
                try:
                    current_segment_id = current_segment_id.get("id") or current_segment_id.get("segment_id")
                except Exception:
                    pass

            if isinstance(rooms_map, dict):
                sel_map = selected_map_name if selected_map_name in rooms_map else None
                if sel_map is None and rooms_map:
                    sel_map = next(iter(rooms_map.keys()))
                try:
                    for room in rooms_map.get(sel_map, []):
                        rid = room.get("id")
                        nm = room.get("name")
                        if rid is not None and nm and current_segment_id is not None and int(rid) == int(current_segment_id):
                            current_room_name = nm
                            break
                except Exception:
                    pass

            # Normalize progress text
            progress_str = None
            try:
                if cleaning_progress is not None:
                    p_int = int(cleaning_progress)
                    progress_str = f"{p_int}% completed"
            except Exception:
                progress_str = None

            # Derive a main verb phrase from vacuum_state / attr_status / high-level state_text
            verb = None
            vs = (str(vacuum_state).strip().lower() if vacuum_state else "")
            st = (str(attr_status_text).strip().lower() if attr_status_text else "")

            # Examples from const.py: SWEEPING, MOPPING, SWEEPING_AND_MOPPING, RETURNING, CHARGING, WASHING, DRYING, etc.
            if "mopp" in vs or "mopping" in st:
                # Could also differentiate "Sweeping and mopping"
                if "sweeping and mopping" in st or "sweeping and mopping" in vs:
                    verb = "Sweeping and mopping"
                else:
                    verb = "Mopping"
            elif "sweep" in vs or "sweep" in st or "clean" in st:
                verb = "Cleaning"
            elif "return" in vs or "return" in st or status.state.upper() in ("BACK_HOME", "RETURNING"):
                verb = "Returning to dock"
            elif "charging" in vs or "charging" in st or status.is_charging:
                verb = "Charging"
            elif "washing" in vs or "washing" in st:
                verb = "Washing mop"
            elif "drying" in vs or "drying" in st:
                verb = "Drying mop"
            elif "docked" in st or status.state.upper() == "DOCKED":
                verb = "Docked"
            elif "paused" in st or status.state.upper() == "PAUSED":
                verb = "Paused"
            elif "idle" in st or status.state.upper() == "IDLE":
                verb = "Idle"
            else:
                # Fallback to existing human-readable state_text (e.g. "Room cleaning")
                verb = status.state_text or "Unknown"

            # Now assemble a natural English sentence
            if verb.startswith("Charging") or "charging" in verb.lower():
                # "Charging, Battery 50%"
                combined = f"{verb}, Battery {int(battery_pct)}%"
            elif verb.startswith("Returning"):
                # "Returning to dock, Battery 50%"
                combined = f"{verb}, Battery {int(battery_pct)}%"
            elif "washing mop" in verb.lower() or "drying mop" in verb.lower():
                # "Washing mop" / "Drying mop"
                combined = verb
            elif ("clean" in verb.lower() or "mopp" in verb.lower()) and current_room_name:
                # "Mopping 'Kitchen 2' 35% completed"
                if progress_str:
                    combined = f"{verb} '{current_room_name}' {progress_str}"
                else:
                    combined = f"{verb} '{current_room_name}'"
            elif "clean" in verb.lower() or "mopp" in verb.lower():
                # "Cleaning, 35% completed" / "Mopping, 35% completed"
                if progress_str:
                    combined = f"{verb}, {progress_str}"
                else:
                    combined = verb
            else:
                # Generic fallback: include battery if it makes sense
                if battery_pct is not None:
                    combined = f"{verb}, Battery {int(battery_pct)}%"
                else:
                    combined = verb

            if combined is not None:
                kv.append({"key": "combined_status", "value": combined})

        except Exception as exc:
            self.logger.debug(f"Combined status build failed for '{dev.name}': {exc}")

        # Push state updates to Indigo
        try:
            dev.updateStatesOnServer(kv)
            # Optionally update image based on onOffState
        except Exception as exc:
            self.logger.error(f"Failed to update states for '{dev.name}': {exc}")

    async def _async_start_clean(self, dev: indigo.Device):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.start_cleaning()
            self._update_status(dev, "Cleaning started")
        except Exception as exc:
            self._update_status(dev, f"Start failed: {exc}")

## Dynamic Menu
    ########################################
    def clean_room(self, plugin_action, dev):
        """
        Action: Clean selected room by name (via segment id from roomMenu).
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return

        segment_id_str = (plugin_action.props.get("roomMenu") or "").strip()
        if not segment_id_str:
            self.logger.error(f"Clean Room: no room selected for '{dev.name}'")
            return

        try:
            segment_id = int(segment_id_str)
        except Exception:
            self.logger.error(f"Clean Room: invalid segment id '{segment_id_str}' for '{dev.name}'")
            return

        repeats_raw = (plugin_action.props.get("repeats") or "").strip() or "1"
        try:
            repeats = int(repeats_raw)
        except Exception:
            repeats = 1

        self.logger.info(f"Clean Room: segment {segment_id} (repeats={repeats}) requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(
            self._async_clean_segments(dev, [segment_id], repeats),
            self._event_loop,
        )

    async def _async_clean_segments(
        self,
        dev: indigo.Device,
        segments: list[int],
        repeats: int = 1,
        suction_level: str = "",
        water_volume: str = "",
    ):
        """
        Async helper: call AsyncDreameClient.clean_segment.
        """
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.clean_segment(segments, repeats=repeats, suction_level=suction_level, water_volume=water_volume)
            # Optional: use first segment for status text
            seg_str = ", ".join(str(s) for s in segments)
            self._update_status(dev, f"Cleaning segments {seg_str}")
        except Exception as exc:
            self._update_status(dev, f"Segment clean failed: {exc}")
    # Dynamic lists for Actions (room selection)
    ########################################
    def shortcut_menu(
        self,
        filter_str: str = "",
        values_dict: indigo.Dict | None = None,
        type_id: str = "",
        dev_id: int = 0,
    ) -> list[tuple[str, str]]:
        """
        Dynamic menu for 'shortcutMenu' in the Start Shortcut action.

        Uses device 'shortcuts' state formatted as: 'id:name, id:name, ...'
        Returns list of (id, 'name (id)') tuples.
        """
        self.logger.debug(f"shortcut_menu called: filter={filter_str}, type_id={type_id}, dev_id={dev_id}")
        result: list[tuple[str, str]] = []

        dev = indigo.devices.get(dev_id)
        if not dev or dev.deviceTypeId != "dreame_vacuum":
            return result

        raw = dev.states.get("shortcuts", "") or ""
        raw = raw.strip()
        if not raw:
            self.logger.debug(f"shortcut_menu: no shortcuts state for '{dev.name}'")
            return result

        try:
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                if ":" not in part:
                    continue
                sid_str, name = part.split(":", 1)
                sid_str = sid_str.strip()
                name = name.strip()
                if not sid_str or not name:
                    continue
                # Ensure sid is numeric; if not, we still allow it as a string id.
                try:
                    int(sid_str)
                except Exception:
                    pass
                label = f"{name} ({sid_str})"
                result.append((sid_str, label))
        except Exception as exc:
            self.logger.debug(f"shortcut_menu: failed to parse shortcuts '{raw}' for '{dev.name}': {exc}")
            return []

        return result


    def room_menu(
        self,
        filter_str: str = "",
        values_dict: indigo.Dict | None = None,
        type_id: str = "",
        dev_id: int = 0,
    ) -> list[tuple[str, str]]:
        """
        Build the dynamic menu for 'roomMenu' in the Clean Room action.

        Returns a list of (value, label) tuples where:
          value = segment id as string
          label = 'Room Name (id)'
        """
        self.logger.debug(f"room_menu called: filter={filter_str}, type_id={type_id}, dev_id={dev_id}")
        result: list[tuple[str, str]] = []

        # Indigo passes the target device id in dev_id for action UI
        dev = indigo.devices.get(dev_id)
        if not dev or dev.deviceTypeId != "dreame_vacuum":
            return result

        # We expect 'room_list' state to look like: "1:Balcony, 2:Laundry, 7:Kitchen 2, ..."
        raw = dev.states.get("room_list", "") or ""
        raw = raw.strip()
        if not raw:
            # fallback: maybe we at least show current map name
            self.logger.debug(f"room_menu: no room_list state for '{dev.name}'")
            return result

        try:
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                # "id:name"
                if ":" in part:
                    sid_str, name = part.split(":", 1)
                    sid_str = sid_str.strip()
                    name = name.strip()
                    if not sid_str or not name:
                        continue
                    # Ensure it's numeric so we don't hand garbage to the action
                    int(sid_str)
                    label = f"{name} ({sid_str})"
                    result.append((sid_str, label))
        except Exception as exc:
            self.logger.debug(f"room_menu: failed to parse room_list '{raw}' for '{dev.name}': {exc}")
            return []

        return result

    async def _async_return_to_dock(self, dev: indigo.Device):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.return_to_dock()
            self._update_status(dev, "Returning to dock")
        except Exception as exc:
            self._update_status(dev, f"Dock failed: {exc}")

    async def _async_locate(self, dev: indigo.Device):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.locate()
            self._update_status(dev, "Locate command sent")
        except Exception as exc:
            self._update_status(dev, f"Locate failed: {exc}")

    async def _async_set_fan_speed(self, dev: indigo.Device, speed: str):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.set_fan_speed(speed)
            self._update_status(dev, f"Fan speed set to {speed}")
        except Exception as exc:
            self._update_status(dev, f"Fan speed failed: {exc}")

    ########################################
    def _update_status(self, dev: indigo.Device, text: str):
        try:
            dev.updateStateOnServer("status", text)
        except Exception:
            pass
    ####
    #Actions

    async def _async_clean_segments(
        self,
        dev: indigo.Device,
        segments: list[int],
        repeats: int = 1,
        suction_level: str = "",
        water_volume: str = "",
    ):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.clean_segment(segments, repeats=repeats, suction_level=suction_level, water_volume=water_volume)
            self._update_status(dev, f"Cleaning segments {segments}")
        except Exception as exc:
            self._update_status(dev, f"Segment clean failed: {exc}")

    async def _async_clean_zones(self, dev: indigo.Device, zones: list[list[int]], repeats: int = 1):
        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return
        try:
            await client.clean_zone(zones, repeats=repeats)
            self._update_status(dev, f"Zone clean started")
        except Exception as exc:
            self._update_status(dev, f"Zone clean failed: {exc}")
    # Add this async helper method to Plugin class (e.g. after _async_set_fan_speed)
    def clean_segments(self, plugin_action, dev):
        """
        Action: clean specific segments (rooms).
        segments: comma-separated list of ids, e.g. "2,3,5"
        repeats, suction_level, water_volume are optional.
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return

        raw_segments = (plugin_action.props.get("segments") or "").strip()
        if not raw_segments:
            self.logger.error(f"Clean segments requested for '{dev.name}' but no segments provided")
            return

        try:
            segments = [int(s.strip()) for s in raw_segments.split(",") if s.strip()]
        except Exception:
            self.logger.error(
                f"Clean segments requested for '{dev.name}' but segments '{raw_segments}' are invalid"
            )
            return

        repeats_raw = (plugin_action.props.get("repeats") or "").strip() or "1"
        try:
            repeats = int(repeats_raw)
        except Exception:
            repeats = 1

        suction_level = (plugin_action.props.get("suction_level") or "").strip()
        water_volume = (plugin_action.props.get("water_volume") or "").strip()

        self.logger.info(
            f"Clean segments {segments} (repeats={repeats}, suction={suction_level!r}, "
            f"water={water_volume!r}) requested for '{dev.name}'"
        )
        asyncio.run_coroutine_threadsafe(
            self._async_clean_segments(dev, segments, repeats, suction_level, water_volume),
            self._event_loop,
        )

    def clean_zones(self, plugin_action, dev):
        """
        Action: clean one or more rectangular zones.
        zones string: "x1,y1,x2,y2; x1,y1,x2,y2; ..."
        """
        if dev.deviceTypeId != "dreame_vacuum" or not self._event_loop:
            return

        raw_zones = (plugin_action.props.get("zones") or "").strip()
        if not raw_zones:
            self.logger.error(f"Clean zones requested for '{dev.name}' but no zones provided")
            return

        zones = []
        for part in raw_zones.split(";"):
            part = part.strip()
            if not part:
                continue
            try:
                x1, y1, x2, y2 = [int(v.strip()) for v in part.split(",")]
                zones.append([x1, y1, x2, y2])
            except Exception:
                self.logger.error(f"Invalid zone rectangle '{part}' for '{dev.name}'")
                return

        repeats_raw = (plugin_action.props.get("repeats") or "").strip() or "1"
        try:
            repeats = int(repeats_raw)
        except Exception:
            repeats = 1

        self.logger.info(f"Clean zones {zones} (repeats={repeats}) requested for '{dev.name}'")
        asyncio.run_coroutine_threadsafe(
            self._async_clean_zones(dev, zones, repeats),
            self._event_loop,
        )

    async def _async_save_map_snapshot(self, dev: indigo.Device, wifi: bool = False):
        """
        Common helper for saving a floor or wifi map snapshot to ~/Pictures.
        wifi=False → floor map, wifi=True → wifi map (if available).
        """
        from dreame_camera import DreameCameraHelper, DreameCameraConfig  # local helper

        client = self._clients.get(dev.id)
        if not client:
            self._update_status(dev, "Not connected")
            return

        device: DreameVacuumDevice | None = getattr(client, "_device", None)
        if device is None:
            self._update_status(dev, "No Dreame device object")
            return

        try:
            cfg = DreameCameraConfig(
                color_scheme=None,
                icon_set=None,
                low_resolution=False,
                square=False,
                map_index=0,
                wifi_map=wifi,
            )
            helper = DreameCameraHelper(device, cfg)

            import os
            pictures_dir = os.path.expanduser("~/Pictures")
            full_path = helper.save_snapshot_to_file(
                base_dir=pictures_dir,
                dev_id=dev.id,
                prefix="DreameMap",
                wifi=wifi,
            )

            if not full_path:
                msg = "WiFi map snapshot failed: no image data" if wifi else "Map snapshot failed: no image data"
                self._update_status(dev, msg)
                return

            kind = "WiFi map" if wifi else "Map"
            self.logger.debug(f"Saved {kind} snapshot for '{dev.name}' to {full_path}")

        except Exception as exc:
            kind = "WiFi map" if wifi else "Map"
            self.logger.error(f"{kind} snapshot failed for '{dev.name}': {exc}")

    async def _async_save_wifi_map_snapshot(self, dev: indigo.Device):
        """
        Thin wrapper so we have a dedicated coroutine for WiFi maps if needed.
        """
        await self._async_save_map_snapshot(dev, wifi=True)
    #########################
    # Helpers


    #######
    # ===== HA-like helpers for cloud login & device discovery =====
    async def _cloud_login_and_pick_device(
        self,
        dev: indigo.Device,
        username: str | None,
        password: str | None,
        country: str,
        account_type_raw: str,
    ):
        """
        HA-like flow:

        - Normalize account_type ('mihome' -> 'mi', etc.)
        - DreameVacuumProtocol(..., account_type)
        - protocol.cloud.login()
        - cloud.get_supported_devices(models, host, mac)
        - if dreame_device_id set, pick that device; else first supported device
        - return (host, token, mac, model, device_id, auth_key, account_type_normalized)
        """
        from dreame.protocol import DreameVacuumProtocol

        # Normalize from Indigo UI values to HA-style internal values
        # Indigo: 'dreame', 'mihome', 'mova'
        # HA:     'dreame', 'mi',     'mova', 'local'
        if account_type_raw == "mihome":
            account_type = "mi"
        elif account_type_raw in ("dreame", "mova", "local"):
            account_type = account_type_raw
        else:
            account_type = "mi"  # safe default

        # Local-only path: HA would go to async_step_local; here we just say "no cloud device"
        if account_type == "local":
            return None

        if not username or not password:
            self.logger.error(f"Cloud login requires username/password for device '{dev.name}'")
            return None

        self.logger.debug(
            f"_cloud_login_and_pick_device: dev='{dev.name}', account_type={account_type!r}, "
            f"country={country!r}, username_set={bool(username)}"
        )

        # 1) Build protocol for cloud login
        proto = DreameVacuumProtocol(
            username=username,
            password=password,
            country=country,
            prefer_cloud=True,
            account_type=account_type,
        )

        # 2) Call cloud.login() in executor (HA uses hass.async_add_executor_job)
        def _login():
            return proto.cloud.login()

        ok = await asyncio.get_event_loop().run_in_executor(None, _login)
        if not ok or not proto.cloud.logged_in:
            self.logger.error(
                f"Cloud login failed for '{dev.name}' (account_type={account_type}, country={country})"
            )
            return None

        self.logger.debug(
            f"Cloud login OK for '{dev.name}': logged_in={proto.cloud.logged_in}, "
            f"auth_failed={getattr(proto.cloud, 'auth_failed', None)}, "
            f"auth_key_present={bool(proto.cloud.auth_key)}"
        )

        # 3) Load models map and get supported devices from cloud
        models = self._load_models_from_device_info()
        host = None
        mac = None

        def _get_supported():
            return proto.cloud.get_supported_devices(models, host, mac)

        try:
            supported_devices, unsupported_devices = await asyncio.get_event_loop().run_in_executor(
                None, _get_supported
            )
        except Exception as exc:
            self.logger.error(f"get_supported_devices failed for '{dev.name}': {exc}")
            return None

        if not supported_devices:
            self.logger.error(f"No supported devices found on account for '{dev.name}'")
            return None

        self.logger.debug(
            f"Cloud returned {len(supported_devices)} supported devices for '{dev.name}'"
        )

        # 4) Choose device: use dreame_device_id if set, else first entry
        wanted_did = (dev.pluginProps.get("dreame_device_id") or "").strip() or None
        chosen_key = None
        chosen_device = None

        if wanted_did:
            for key, d in supported_devices.items():
                if str(d.get("did")) == str(wanted_did):
                    chosen_key = key
                    chosen_device = d
                    break
            if not chosen_device:
                self.logger.warning(
                    f"DID {wanted_did!r} not found among supported devices; "
                    f"falling back to first device for '{dev.name}'"
                )

        if chosen_device is None:
            # First device
            chosen_key = next(iter(supported_devices.keys()))
            chosen_device = supported_devices[chosen_key]

        self.logger.debug(
            f"Chosen cloud device for '{dev.name}': key={chosen_key!r}, did={chosen_device.get('did')!r}, "
            f"model={chosen_device.get('model')!r}"
        )

        # 5) Extract info like HA's extract_info
        host2, token2, mac2, model2, name2, device_id2 = self._extract_device_info(
            account_type,
            chosen_device,
            host,
            None,
        )

        auth_key = getattr(proto.cloud, "auth_key", None)

        self.logger.debug(
            f"After extract: host={host2!r}, token_len={len(token2) if token2 else 0}, "
            f"mac={mac2!r}, model={model2!r}, name={name2!r}, did={device_id2!r}, "
            f"auth_key_present={bool(auth_key)}"
        )

        # We do NOT disconnect proto here; the AsyncDreameClient will build its own protocol
        return {
            "account_type": account_type,
            "host": host2,
            "token": token2,
            "mac": mac2,
            "model": model2,
            "name": name2,
            "device_id": device_id2,
            "auth_key": auth_key,
        }
    def _load_models_from_device_info(self) -> dict[str, int]:
        """
        Port of HA's DreameVacuumFlowHandler.load_devices(), but only returning `models` mapping.

        models: { "xiaomi.vacuum.xxx" | "mova.vacuum.xxx" | "dreame.vacuum.xxx" : model_type_id }
        """
        from dreame.const import DEVICE_INFO  # or wherever DEVICE_INFO lives in your vendored dreame

        models: dict[str, int] = {}
        try:
            import base64, json, zlib

            device_info = json.loads(
                zlib.decompress(base64.b64decode(DEVICE_INFO), zlib.MAX_WBITS | 32)
            )
            # device_info[3]: model keys; device_info[0]: info indexed by that mapping
            for k in device_info[3]:
                info = device_info[0][device_info[3][k]]
                if info:
                    # info[0] == 1 → Xiaomi, 2 → Mova, else Dreame (mirrors HA)
                    vendor_prefix = (
                        "xiaomi"
                        if info[0] == 1
                        else "mova"
                        if info[0] == 2
                        else "dreame"
                    )
                    models[f"{vendor_prefix}.vacuum.{k}"] = info[1]
        except Exception as exc:
            self.logger.error(f"Failed to load models from DEVICE_INFO: {exc}")
        return models

    def _extract_device_info(
        self,
        account_type: str,
        device: dict,
        host: str | None,
        token: str | None,
    ):
        """
        Port of HA's extract_info(), but returning a tuple:

            (host, token, mac, model, name, device_id)

        account_type here should be one of: 'mi', 'dreame', 'mova', 'local'.
        Indigo UI uses 'mihome' for Xiaomi; we map that earlier before calling this.
        """
        mac = None
        model = None
        name = None
        device_id = None

        at = account_type

        if at == "mi":
            # HA's ACCOUNT_TYPE_MI
            if host is None:
                host = device.get("localip")
            if mac is None:
                mac = device.get("mac")
            if model is None:
                model = device.get("model")
            if name is None:
                name = device.get("name")
            token = device.get("token")
            device_id = device.get("did")
        elif at in ("dreame", "mova"):
            # HA's ACCOUNT_TYPE_DREAME / ACCOUNT_TYPE_MOVA
            if token is None:
                token = " "
            if host is None:
                host = device.get("bindDomain")
            if mac is None:
                mac = device.get("mac")
            if model is None:
                model = device.get("model")
            if name is None:
                custom_name = device.get("customName") or ""
                device_info = (device.get("deviceInfo") or {})
                display_name = device_info.get("displayName") or "Dreame Vacuum"
                name = custom_name if len(custom_name) > 0 else display_name
            device_id = device.get("did")

        return host, token, mac, model, name, device_id