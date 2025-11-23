#! /usr/bin/env python
# -*- coding: utf-8 -*-
"""
Indigo-only Dreame map / wifi-map renderer + helpers.

We use the vendored dreame.map renderers directly (no Home Assistant, no custom_components):
- DreameVacuumMapRenderer for PNG map images
- DreameVacuumMapDataJsonRenderer for JSON/geo data (optional)

This module is intentionally self-contained and only depends on the dreame library
and DreameVacuumDevice.
"""

#### DELETE ME

from __future__ import annotations

try:
    import indigo  # type: ignore
except ImportError:  # pragma: no cover
    indigo = None

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from dreame.device import DreameVacuumDevice

try:
    from dreame.map import DreameVacuumMapRenderer, DreameVacuumMapDataJsonRenderer  # type: ignore
except Exception:  # pragma: no cover
    DreameVacuumMapRenderer = None
    DreameVacuumMapDataJsonRenderer = None

_LOGGER = logging.getLogger("dreame_camera")


@dataclass
class DreameCameraConfig:
    """
    Minimal configuration for map rendering.

    NOTE: all fields are optional – you can extend to expose these via Indigo
    device props later if desired.
    """

    color_scheme: Optional[str] = None
    icon_set: Optional[str] = None
    low_resolution: bool = False
    square: bool = False
    # Which map index to use; 0 == current/active map, >0 == saved maps
    map_index: int = 0
    # If True: render wifi coverage map instead of floor map (when available)
    wifi_map: bool = False


class DreameCameraHelper:
    """
    Thin wrapper around DreameVacuumDevice to generate:

    - Floor map PNG bytes
    - Wifi map PNG bytes (when supported by the device)
    - Optional JSON map data (if DreameVacuumMapDataJsonRenderer is available)

    Usage from Indigo plugin:

        device: DreameVacuumDevice = client._device
        helper = DreameCameraHelper(device, DreameCameraConfig(map_index=0))
        png_bytes = helper.render_floor_map()      # current floor map
        wifi_bytes = helper.render_wifi_map()      # wifi map if present
        json_str = helper.map_data_json(include_resources=True)  # optional
    """

    def __init__(self, device: DreameVacuumDevice, config: Optional[DreameCameraConfig] = None) -> None:
        if DreameVacuumMapRenderer is None:
            raise RuntimeError(
                "DreameCameraHelper: DreameVacuumMapRenderer not available; "
                "ensure dreame.map is included in this plugin."
            )

        self._device = device
        self._config = config or DreameCameraConfig()

        # Hidden objects: start empty; you can later expose a list of hidden map
        # objects via Indigo plugin props if you want to match HA behaviour.
        hidden_objects: list[str] = []

        self._renderer = DreameVacuumMapRenderer(
            self._config.color_scheme,
            self._config.icon_set,
            hidden_objects,
            self._device.capability.robot_type,
            self._config.low_resolution,
            self._config.square,
        )

        # Optional JSON renderer
        self._json_renderer: Optional[DreameVacuumMapDataJsonRenderer]
        if DreameVacuumMapDataJsonRenderer is not None:
            self._json_renderer = DreameVacuumMapDataJsonRenderer()
        else:
            self._json_renderer = None

        self._last_calibration_points: Any = None

    # ------------------------------------------------------------------
    # Map data helpers
    # ------------------------------------------------------------------
    def _get_floor_map_data(self) -> Optional[Any]:
        """
        Get floor-map map_data for configured map_index.
        """
        try:
            map_data = self._device.get_map(self._config.map_index)
            if not map_data:
                return None
            # If wifi_map=True on config, we *don't* want wifi_map_data here.
            return map_data
        except Exception as ex:
            _LOGGER.error(f"DreameCameraHelper: get_map({self._config.map_index}) failed: {ex}")
            return None

    def _get_wifi_map_data(self) -> Optional[Any]:
        """
        Get wifi map_data for configured map_index, if available.

        On HA, this logic roughly matches DreameVacuumCameraEntity.wifi_map_data.
        """
        try:
            if self._config.map_index == 0:
                selected_map = self._device.status.selected_map
                map_data = selected_map if selected_map else None
            else:
                map_data = self._device.get_map(self._config.map_index)

            if not map_data:
                return None

            wifi_map_data = getattr(map_data, "wifi_map_data", None)
            return wifi_map_data
        except Exception as ex:
            _LOGGER.error(f"DreameCameraHelper: fetching wifi_map_data failed: {ex}")
            return None

    def _get_map_data_for_render(self, map_data: Any) -> Any:
        """
        Convert raw dreame map_data -> renderer-ready structure.
        """
        return self._device.get_map_for_render(map_data)

    # ------------------------------------------------------------------
    # PNG render helpers
    # ------------------------------------------------------------------
    def _render_map_generic(self, map_data: Any) -> Optional[bytes]:
        """
        Internal: given a map_data object, run through renderer and return PNG bytes.
        """
        try:
            render_map = self._get_map_data_for_render(map_data)
        except Exception as ex:
            _LOGGER.error(f"DreameCameraHelper: get_map_for_render failed: {ex}")
            return None

        robot_status = getattr(self._device.status, "robot_status", None)
        station_status = getattr(self._device.status, "station_status", None)

        try:
            image_bytes = self._renderer.render_map(render_map, robot_status, station_status)
        except Exception as ex:
            _LOGGER.warning(f"DreameCameraHelper: render_map failed: {ex}")
            return None

        if not image_bytes:
            _LOGGER.debug("DreameCameraHelper: render_map returned no image data")
            return None

        # Track calibration points if available
        try:
            if self._last_calibration_points != self._renderer.calibration_points:
                self._last_calibration_points = self._renderer.calibration_points
        except Exception:
            pass

        return image_bytes

    def render_floor_map(self) -> Optional[bytes]:
        """
        Render current/saved floor map as PNG bytes.

        Returns:
            PNG bytes or None if no usable map is available.
        """
        try:
            # For the "current" map, HA camera calls device.update_map() prior to rendering.
            try:
                if self._config.map_index == 0:
                    self._device.update_map()
            except Exception as ex:
                _LOGGER.debug(f"DreameCameraHelper: update_map failed or not supported: {ex}")

            map_data = self._get_floor_map_data()
            if not map_data or getattr(map_data, "empty_map", False):
                _LOGGER.debug("DreameCameraHelper: no floor map data or empty_map=True")
                try:
                    return self._renderer.default_map_image
                except Exception:
                    return None

            return self._render_map_generic(map_data)
        except Exception as ex:
            _LOGGER.warning(f"DreameCameraHelper: floor map render failed: {ex}")
            return None

    def render_wifi_map(self) -> Optional[bytes]:
        """
        Render wifi coverage map as PNG bytes, if the device exposes wifi_map_data.

        Returns:
            PNG bytes or None if wifi map is not available.
        """
        if not getattr(self._device.capability, "wifi_map", False):
            _LOGGER.debug("DreameCameraHelper: device.capability.wifi_map is False/absent")
            return None

        try:
            wifi_map_data = self._get_wifi_map_data()
            if not wifi_map_data:
                _LOGGER.debug("DreameCameraHelper: no wifi_map_data found")
                return None

            # For wifi_map_data, we still want to pass through get_map_for_render
            return self._render_map_generic(wifi_map_data)
        except Exception as ex:
            _LOGGER.warning(f"DreameCameraHelper: wifi map render failed: {ex}")
            return None

    # ------------------------------------------------------------------
    # JSON data helper (optional)
    # ------------------------------------------------------------------
    def map_data_json(self, include_resources: bool = False) -> str:
        """
        Return current floor-map data as JSON string, optionally with resources
        (icons / colours) embedded.

        This mirrors HA's map_data_string() behaviour but is simplified for Indigo.

        Returns:
            JSON string, or "{}" on failure.
        """
        if self._json_renderer is None:
            return "{}"

        try:
            if self._config.map_index == 0:
                try:
                    self._device.update_map()
                except Exception as ex:
                    _LOGGER.debug(f"DreameCameraHelper: update_map failed for JSON render: {ex}")

            map_data = self._get_floor_map_data()
            if not map_data:
                return "{}"

            render_map = self._get_map_data_for_render(map_data)

            resources = None
            if include_resources:
                try:
                    resources = self._json_renderer.get_resources(self._device.capability)  # type: ignore[attr-defined]
                except Exception:
                    resources = None

            robot_status = getattr(self._device.status, "robot_status", None)
            station_status = getattr(self._device.status, "station_status", None)

            return self._json_renderer.get_data_string(render_map, resources, robot_status, station_status)  # type: ignore[union-attr]
        except Exception as ex:
            _LOGGER.warning(f"DreameCameraHelper: map_data_json failed: {ex}")
            return "{}"

    # ------------------------------------------------------------------
    # Convenience for Indigo "save snapshot" actions
    # ------------------------------------------------------------------
    def save_snapshot_to_file(
        self,
        base_dir: str,
        dev_id: int,
        prefix: str = "DreameMap",
        wifi: bool = False,
    ) -> Optional[str]:
        """
        Render current floor or wifi map & save PNG to base_dir.

        Args:
            base_dir: directory path (will be created if needed)
            dev_id: Indigo device id – used in filename
            prefix: filename prefix
            wifi: if True, save wifi map; else floor map

        Returns:
            Full path to written PNG file, or None on failure.
        """
        import os

        img_bytes = self.render_wifi_map() if wifi else self.render_floor_map()
        if not img_bytes:
            return None

        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception:
            pass

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = "Wifi" if wifi else "Floor"
        filename = f"{prefix}-{suffix}-{dev_id}.png"
        full_path = os.path.join(base_dir, filename)
        try:
            with open(full_path, "wb") as f:
                f.write(img_bytes)
        except Exception as ex:
            _LOGGER.error(f"DreameCameraHelper: failed writing snapshot to {full_path}: {ex}")
            return None

        return full_path

    @property
    def last_calibration_points(self) -> Any:
        """
        Last known calibration points from renderer, if any.
        """
        return self._last_calibration_points