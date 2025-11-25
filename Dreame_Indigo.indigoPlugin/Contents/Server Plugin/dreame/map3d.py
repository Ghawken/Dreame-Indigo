"""
3D Map Visualization System for Dreame Vacuum
Floor plan visualization with wall outlines around open spaces
"""

from __future__ import annotations
import numpy as np
import time
import logging
import sys
import os
from typing import Dict, List, Tuple, Optional, Any, Set
from dataclasses import dataclass
from enum import Enum

# Import existing Dreame components
from .device import DreameVacuumDevice
from .map import DreameMapVacuumMapManager, MapData, MapPixelType

# Safe matplotlib import with backend configuration
MATPLOTLIB_AVAILABLE = False
try:
    import matplotlib

    matplotlib.use('Agg')  # Use non-interactive backend to prevent crashes
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    plt.ioff()  # Turn off interactive mode
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False
except Exception as ex:
    print(f"Matplotlib import error: {ex}")
    MATPLOTLIB_AVAILABLE = False

# Safe plotly import
PLOTLY_AVAILABLE = False
try:
    import plotly.graph_objects as go

    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False
except Exception as ex:
    print(f"Plotly import error: {ex}")
    PLOTLY_AVAILABLE = False

_LOGGER = logging.getLogger("Plugin.dreame.map3d")


class Visualization3DType(Enum):
    """3D visualization types"""
    FLOOR_PLAN = "floor_plan"
    WALLS_ONLY = "walls_only"
    DOOM_STYLE = "doom_style"


@dataclass
class DreameCamera3DConfig:
    """Configuration for 3D camera helper"""
    color_scheme: Optional[str] = None
    icon_set: Optional[str] = None
    low_resolution: bool = False
    square: bool = False
    map_index: int = 0
    wifi_map: bool = False
    visualization_type: str = "floor_plan"
    wall_height: float = 250.0
    floor_height: float = 0.0
    room_height: float = 5.0


class FloorRoom:
    """Represents a floor room/area"""

    def __init__(self, pixels: List[Tuple[int, int]], room_id: int):
        self.pixels = pixels
        self.room_id = room_id
        self.outline = []


class WallSegment:
    """Represents a wall segment"""

    def __init__(self, start_x: float, start_y: float, end_x: float, end_y: float, height: float):
        self.start_x = start_x
        self.start_y = start_y
        self.end_x = end_x
        self.end_y = end_y
        self.height = height


class DreameCameraHelper3D:
    """3D camera helper that creates floor plan visualizations"""

    def __init__(self, device: DreameVacuumDevice, config: Optional[DreameCamera3DConfig] = None):
        self._device = device
        self._config = config or DreameCamera3DConfig()

        print(f"3D DEBUG: DreameCameraHelper3D initialized for floor plan visualization")
        _LOGGER.info(f"DreameCameraHelper3D initialized for floor plan visualization")

    def save_3d_snapshot_to_file(self, base_dir: str, dev_id: str, prefix: str = "DreameMap3D",
                                 visualization_type: str = "floor_plan") -> Optional[str]:
        """Save 3D map snapshot to file"""
        if not MATPLOTLIB_AVAILABLE:
            print(f"3D DEBUG: Matplotlib not available for 3D snapshot")
            _LOGGER.error("Matplotlib not available for 3D snapshot")
            return None

        try:
            print(f"3D DEBUG: Starting save_3d_snapshot_to_file")
            _LOGGER.info(f"=== Creating 3D snapshot: {visualization_type} ===")

            # Get map data from device
            map_data = self._get_map_data_from_device()
            if not map_data:
                print(f"3D DEBUG: No map data, creating demo")
                _LOGGER.error("No map data available - creating demo visualization")
                floor_rooms, wall_segments, robot_pos, bounds = self._create_demo_floor_plan()
            else:
                print(f"3D DEBUG: Extracting floor plan from map")
                floor_rooms, wall_segments, robot_pos, bounds = self._extract_floor_plan_from_map_data(map_data)

            print(f"3D DEBUG: Found {len(floor_rooms)} rooms and {len(wall_segments)} wall segments")

            # Generate matplotlib figure
            fig = self._create_floor_plan_matplotlib_figure(floor_rooms, wall_segments, robot_pos, bounds,
                                                            visualization_type, map_data)
            if not fig:
                print(f"3D DEBUG: Failed to create matplotlib figure")
                _LOGGER.error("Failed to create matplotlib figure")
                return None

            # Create filename
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{prefix}_{dev_id}_{visualization_type}_{timestamp}.png"
            full_path = os.path.join(base_dir, filename)

            # Save the figure with error handling
            try:
                fig.savefig(full_path, dpi=300, bbox_inches='tight', facecolor='black')
                plt.close(fig)  # Clean up
                plt.clf()  # Clear current figure
                plt.cla()  # Clear current axes
            except Exception as save_ex:
                print(f"3D DEBUG: Error saving figure: {save_ex}")
                _LOGGER.error(f"Error saving figure: {save_ex}")
                try:
                    plt.close('all')  # Close all figures
                except:
                    pass
                return None

            print(f"3D DEBUG: File saved successfully to {full_path}")
            _LOGGER.info(f"=== 3D snapshot saved to: {full_path} ===")
            return full_path

        except Exception as ex:
            print(f"3D DEBUG ERROR: {ex}")
            _LOGGER.error(f"=== Error saving 3D snapshot: {ex} ===")
            import traceback
            print(f"3D DEBUG TRACEBACK: {traceback.format_exc()}")
            _LOGGER.error(f"Traceback: {traceback.format_exc()}")

            # Clean up matplotlib in case of error
            try:
                plt.close('all')
            except:
                pass

            return None

    def save_3d_html_to_file(self, base_dir: str, dev_id: str, prefix: str = "DreameMap3D",
                             visualization_type: str = "floor_plan") -> Optional[str]:
        """Save interactive 3D map HTML to file"""
        if not PLOTLY_AVAILABLE:
            print(f"3D DEBUG: Plotly not available for 3D HTML")
            _LOGGER.error("Plotly not available for 3D HTML")
            return None

        try:
            print(f"3D DEBUG: Starting save_3d_html_to_file")
            _LOGGER.info(f"=== Creating 3D HTML: {visualization_type} ===")

            # Get map data from device
            map_data = self._get_map_data_from_device()
            if not map_data:
                print(f"3D DEBUG: No map data, creating demo")
                _LOGGER.error("No map data available - creating demo visualization")
                floor_rooms, wall_segments, robot_pos, bounds = self._create_demo_floor_plan()
            else:
                print(f"3D DEBUG: Extracting floor plan from map")
                floor_rooms, wall_segments, robot_pos, bounds = self._extract_floor_plan_from_map_data(map_data)

            print(f"3D DEBUG: Found {len(floor_rooms)} rooms and {len(wall_segments)} wall segments")

            # Generate plotly figure
            fig = self._create_floor_plan_plotly_figure(floor_rooms, wall_segments, robot_pos, bounds,
                                                        visualization_type, map_data)
            if not fig:
                print(f"3D DEBUG: Failed to create figure")
                _LOGGER.error("Failed to create plotly figure")
                return None

            print(f"3D DEBUG: Figure created successfully")

            # Create filename
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{prefix}_{dev_id}_{visualization_type}_{timestamp}.html"
            full_path = os.path.join(base_dir, filename)

            print(f"3D DEBUG: Saving to {full_path}")

            # Save HTML with UTF-8 encoding
            html_content = fig.to_html(include_plotlyjs=True)
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

            print(f"3D DEBUG: File saved successfully")
            _LOGGER.info(f"=== 3D HTML saved to: {full_path} ===")
            return full_path

        except Exception as ex:
            print(f"3D DEBUG ERROR: {ex}")
            _LOGGER.error(f"=== Error saving 3D HTML: {ex} ===")
            import traceback
            print(f"3D DEBUG TRACEBACK: {traceback.format_exc()}")
            _LOGGER.error(f"Traceback: {traceback.format_exc()}")
            return None

    def _get_map_data_from_device(self) -> Optional[MapData]:
        """Get map data directly from device - using _map_manager"""
        try:
            print(f"3D DEBUG: Getting map data from device")
            _LOGGER.info(f"--- Getting map data from device ---")

            if not hasattr(self._device, '_map_manager'):
                print(f"3D DEBUG: Device has no _map_manager attribute")
                return None

            map_manager = self._device._map_manager
            if not map_manager or not isinstance(map_manager, DreameMapVacuumMapManager):
                print(f"3D DEBUG: Invalid map manager")
                return None

            # Try to get current map using get_map method
            current_map = map_manager.get_map(self._config.map_index)

            if current_map:
                print(f"3D DEBUG: Retrieved map: ID={current_map.map_id}")
                print(f"3D DEBUG: Map dimensions: {current_map.dimensions.width}x{current_map.dimensions.height}")
                print(f"3D DEBUG: Map has pixel_type: {current_map.pixel_type is not None}")
                if current_map.pixel_type is not None:
                    unique_vals = np.unique(current_map.pixel_type)
                    print(f"3D DEBUG: Pixel values in map: {unique_vals}")
                return current_map
            else:
                print(f"3D DEBUG: No map found, trying fallbacks")

                # Try fallbacks
                if hasattr(map_manager, 'selected_map') and map_manager.selected_map:
                    return map_manager.selected_map

                if hasattr(map_manager, '_map_data') and map_manager._map_data:
                    return map_manager._map_data

                return None

        except Exception as ex:
            print(f"3D DEBUG: Exception getting map data: {ex}")
            import traceback
            print(f"3D DEBUG: Traceback: {traceback.format_exc()}")
            return None

    def _extract_floor_plan_from_map_data(self, map_data: MapData) -> Tuple[
        List[FloorRoom], List[WallSegment], Tuple[float, float], Tuple[float, float, float, float]]:
        """Extract floor plan with rooms and wall outlines from real map data"""
        try:
            print(f"3D DEBUG: Extracting floor plan from MapData")

            if map_data.pixel_type is None or map_data.pixel_type.size == 0:
                print(f"3D DEBUG: No pixel data, using demo")
                return self._create_demo_floor_plan()

            pixel_array = map_data.pixel_type  # Shape is (width, height)
            width, height = pixel_array.shape

            print(f"3D DEBUG: Processing pixel array shape: {pixel_array.shape}")

            # Get unique values and identify what we're working with
            unique_vals = np.unique(pixel_array)
            print(f"3D DEBUG: Unique pixel values: {unique_vals}")

            # Identify floor/room areas (not walls or outside)
            floor_rooms = []
            room_pixels_by_id = {}

            # Process each unique value to categorize areas
            for val in unique_vals:
                val_int = int(val)
                if val_int == MapPixelType.OUTSIDE.value:  # 0 - outside/unmapped
                    continue
                elif val_int == MapPixelType.WALL.value:  # 255 - walls
                    continue
                elif val_int == MapPixelType.FLOOR.value:  # 254 - general floor
                    room_id = 'floor'
                elif val_int == MapPixelType.NEW_SEGMENT.value:  # 253 - being mapped
                    room_id = 'new'
                elif 1 <= val_int <= 61:  # Room segments
                    room_id = val_int
                else:
                    continue  # Skip other values

                # Get all pixels for this room/area
                pixels = np.where(pixel_array == val)
                if len(pixels[0]) > 0:
                    pixel_list = list(zip(pixels[0], pixels[1]))
                    if room_id not in room_pixels_by_id:
                        room_pixels_by_id[room_id] = []
                    room_pixels_by_id[room_id].extend(pixel_list)

            print(f"3D DEBUG: Found room areas: {list(room_pixels_by_id.keys())}")

            # Create FloorRoom objects
            for room_id, pixels in room_pixels_by_id.items():
                if len(pixels) > 10:  # Only include rooms with sufficient pixels
                    floor_room = FloorRoom(pixels, room_id)
                    floor_rooms.append(floor_room)
                    print(f"3D DEBUG: Room {room_id}: {len(pixels)} pixels")

            # Extract wall outlines around the mapped area
            wall_segments = self._extract_wall_outlines_from_map(pixel_array, width, height)

            # Calculate bounds
            all_mapped = pixel_array != MapPixelType.OUTSIDE.value
            if np.any(all_mapped):
                y_coords, x_coords = np.where(all_mapped)
                bounds = (float(np.min(x_coords)), float(np.min(y_coords)),
                          float(np.max(x_coords)), float(np.max(y_coords)))
            else:
                bounds = (0.0, 0.0, float(width), float(height))

            # Get robot position
            robot_pos = (width // 2, height // 2)  # Default center
            if map_data.robot_position and map_data.dimensions:
                robot_x = (map_data.robot_position.x - map_data.dimensions.left) / map_data.dimensions.grid_size
                robot_y = (map_data.robot_position.y - map_data.dimensions.top) / map_data.dimensions.grid_size
                robot_pos = (float(robot_x), float(robot_y))

            print(f"3D DEBUG: Extracted {len(floor_rooms)} floor areas and {len(wall_segments)} wall segments")
            print(f"3D DEBUG: Bounds: {bounds}, Robot: {robot_pos}")

            return floor_rooms, wall_segments, robot_pos, bounds

        except Exception as ex:
            print(f"3D DEBUG: Error extracting floor plan: {ex}")
            import traceback
            print(f"3D DEBUG: Traceback: {traceback.format_exc()}")
            return self._create_demo_floor_plan()

    def _extract_wall_outlines_from_map(self, pixel_array: np.ndarray, width: int, height: int) -> List[WallSegment]:
        """Extract wall outlines around mapped areas"""
        wall_segments = []

        try:
            # Create a mask for all non-outside areas (mapped areas)
            mapped_mask = pixel_array != MapPixelType.OUTSIDE.value

            # Find the boundary/edge pixels of mapped areas
            boundary_pixels = set()

            for x in range(width):
                for y in range(height):
                    if mapped_mask[x, y]:  # If this pixel is mapped
                        # Check if it's on the boundary (adjacent to unmapped area)
                        is_boundary = False

                        # Check all 4 directions
                        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                            nx, ny = x + dx, y + dy
                            if (nx < 0 or nx >= width or ny < 0 or ny >= height or
                                    not mapped_mask[nx, ny]):
                                is_boundary = True
                                break

                        if is_boundary:
                            boundary_pixels.add((x, y))

            print(f"3D DEBUG: Found {len(boundary_pixels)} boundary pixels")

            # Convert boundary pixels to wall segments
            # This is a simplified approach - trace horizontal and vertical segments
            wall_segments = self._trace_wall_segments_from_boundary(boundary_pixels)

        except Exception as ex:
            print(f"3D DEBUG: Error extracting wall outlines: {ex}")

        return wall_segments

    def _trace_wall_segments_from_boundary(self, boundary_pixels: Set[Tuple[int, int]]) -> List[WallSegment]:
        """Trace wall segments from boundary pixels"""
        wall_segments = []

        try:
            # Convert to sorted list for consistent processing
            boundary_list = sorted(list(boundary_pixels))

            # Group boundary pixels into horizontal and vertical segments
            # This is a simplified approach

            # Find horizontal segments
            y_groups = {}
            for x, y in boundary_list:
                if y not in y_groups:
                    y_groups[y] = []
                y_groups[y].append(x)

            for y, x_coords in y_groups.items():
                x_coords.sort()
                # Find continuous runs of x coordinates
                segments = self._find_continuous_segments(x_coords)
                for start_x, end_x in segments:
                    if end_x - start_x >= 2:  # Minimum length
                        wall_segments.append(WallSegment(
                            start_x, y, end_x, y, self._config.wall_height
                        ))

            # Find vertical segments
            x_groups = {}
            for x, y in boundary_list:
                if x not in x_groups:
                    x_groups[x] = []
                x_groups[x].append(y)

            for x, y_coords in x_groups.items():
                y_coords.sort()
                segments = self._find_continuous_segments(y_coords)
                for start_y, end_y in segments:
                    if end_y - start_y >= 2:  # Minimum length
                        wall_segments.append(WallSegment(
                            x, start_y, x, end_y, self._config.wall_height
                        ))

            print(f"3D DEBUG: Created {len(wall_segments)} wall segments from boundary")

        except Exception as ex:
            print(f"3D DEBUG: Error tracing wall segments: {ex}")

        return wall_segments

    def _find_continuous_segments(self, coords: List[int]) -> List[Tuple[int, int]]:
        """Find continuous segments in a list of coordinates"""
        segments = []
        if not coords:
            return segments

        start = coords[0]
        end = coords[0]

        for i in range(1, len(coords)):
            if coords[i] == end + 1:  # Continuous
                end = coords[i]
            else:  # Gap found
                segments.append((start, end))
                start = coords[i]
                end = coords[i]

        # Add the last segment
        segments.append((start, end))

        return segments

    def _create_demo_floor_plan(self) -> Tuple[
        List[FloorRoom], List[WallSegment], Tuple[float, float], Tuple[float, float, float, float]]:
        """Create demo floor plan for testing"""
        print(f"3D DEBUG: Creating demo floor plan")

        # Create some demo rooms
        floor_rooms = []

        # Living room
        living_room_pixels = []
        for x in range(10, 50):
            for y in range(10, 40):
                living_room_pixels.append((x, y))
        floor_rooms.append(FloorRoom(living_room_pixels, 'living_room'))

        # Kitchen
        kitchen_pixels = []
        for x in range(50, 80):
            for y in range(10, 30):
                kitchen_pixels.append((x, y))
        floor_rooms.append(FloorRoom(kitchen_pixels, 'kitchen'))

        # Bedroom
        bedroom_pixels = []
        for x in range(10, 40):
            for y in range(40, 70):
                bedroom_pixels.append((x, y))
        floor_rooms.append(FloorRoom(bedroom_pixels, 'bedroom'))

        # Wall segments around the perimeter
        wall_segments = [
            WallSegment(5, 5, 85, 5, self._config.wall_height),  # Top wall
            WallSegment(85, 5, 85, 75, self._config.wall_height),  # Right wall
            WallSegment(85, 75, 5, 75, self._config.wall_height),  # Bottom wall
            WallSegment(5, 75, 5, 5, self._config.wall_height),  # Left wall
            # Interior walls
            WallSegment(50, 5, 50, 30, self._config.wall_height),  # Kitchen divider
            WallSegment(5, 40, 40, 40, self._config.wall_height),  # Bedroom divider
        ]

        robot_pos = (30.0, 25.0)
        bounds = (5.0, 5.0, 85.0, 75.0)

        print(f"3D DEBUG: Created demo with {len(floor_rooms)} rooms and {len(wall_segments)} walls")
        return floor_rooms, wall_segments, robot_pos, bounds

    def _create_floor_plan_matplotlib_figure(self, floor_rooms: List[FloorRoom], wall_segments: List[WallSegment],
                                             robot_pos: Tuple[float, float], bounds: Tuple[float, float, float, float],
                                             visualization_type: str, map_data):
        """Create matplotlib figure showing floor plan with rooms and wall outlines"""
        try:
            print(f"3D DEBUG: Creating floor plan matplotlib figure")

            # Create figure with dark theme
            plt.style.use('dark_background')
            fig = plt.figure(figsize=(16, 12), dpi=100, facecolor='black')
            ax = fig.add_subplot(111, projection='3d')
            ax.set_facecolor('black')

            # Define colors for different rooms
            room_colors = [
                '#4CAF50',  # Green
                '#2196F3',  # Blue
                '#FF9800',  # Orange
                '#9C27B0',  # Purple
                '#F44336',  # Red
                '#00BCD4',  # Cyan
                '#FFEB3B',  # Yellow
                '#795548',  # Brown
            ]

            # Draw floor areas for each room
            for i, room in enumerate(floor_rooms):
                if not room.pixels:
                    continue

                # Get room color
                color = room_colors[i % len(room_colors)]

                # Create floor tiles for this room
                room_x = [p[0] for p in room.pixels]
                room_y = [p[1] for p in room.pixels]
                room_z = [self._config.room_height] * len(room.pixels)

                # Draw room floor as scatter points (faster than individual tiles)
                ax.scatter(room_x, room_y, room_z, c=color, alpha=0.6, s=20, label=f'Room {room.room_id}')

                print(f"3D DEBUG: Added room {room.room_id} with {len(room.pixels)} pixels")

            # Draw walls as vertical lines/faces
            for segment in wall_segments:
                # Wall outline
                wall_x = [segment.start_x, segment.end_x, segment.end_x, segment.start_x, segment.start_x]
                wall_y = [segment.start_y, segment.end_y, segment.end_y, segment.start_y, segment.start_y]
                wall_z_bottom = [self._config.floor_height] * 5
                wall_z_top = [segment.height] * 5

                # Draw wall edges
                ax.plot(wall_x, wall_y, wall_z_top, color='white', linewidth=2, alpha=0.9)
                ax.plot(wall_x, wall_y, wall_z_bottom, color='gray', linewidth=1, alpha=0.7)

                # Draw vertical edges
                ax.plot([segment.start_x, segment.start_x], [segment.start_y, segment.start_y],
                        [self._config.floor_height, segment.height], color='white', linewidth=2, alpha=0.8)
                ax.plot([segment.end_x, segment.end_x], [segment.end_y, segment.end_y],
                        [self._config.floor_height, segment.height], color='white', linewidth=2, alpha=0.8)

                # Fill wall face
                wall_verts = [
                    [(segment.start_x, segment.start_y, self._config.floor_height),
                     (segment.end_x, segment.end_y, self._config.floor_height),
                     (segment.end_x, segment.end_y, segment.height),
                     (segment.start_x, segment.start_y, segment.height)]
                ]
                ax.add_collection3d(Poly3DCollection(wall_verts, alpha=0.3, facecolor='lightgray', edgecolor='white'))

            print(f"3D DEBUG: Added {len(wall_segments)} wall segments")

            # Add robot marker
            robot_height = 25.0
            ax.scatter([robot_pos[0]], [robot_pos[1]], [robot_height],
                       color='red', s=400, alpha=1.0, marker='o', label='Robot', edgecolors='white', linewidth=2)

            # Set up view
            margin = 10
            ax.set_xlim(bounds[0] - margin, bounds[2] + margin)
            ax.set_ylim(bounds[1] - margin, bounds[3] + margin)
            ax.set_zlim(0, self._config.wall_height + 20)

            ax.set_xlabel('X (map units)', color='white', fontsize=12)
            ax.set_ylabel('Y (map units)', color='white', fontsize=12)
            ax.set_zlabel('Height (cm)', color='white', fontsize=12)

            # FPS-style view (slightly elevated for floor plan overview)
            ax.view_init(elev=25, azim=45)

            # Style the axes
            ax.xaxis.pane.fill = False
            ax.yaxis.pane.fill = False
            ax.zaxis.pane.fill = False

            ax.xaxis.pane.set_edgecolor('gray')
            ax.yaxis.pane.set_edgecolor('gray')
            ax.zaxis.pane.set_edgecolor('gray')
            ax.xaxis.pane.set_alpha(0.1)
            ax.yaxis.pane.set_alpha(0.1)
            ax.zaxis.pane.set_alpha(0.1)

            # Set tick colors
            ax.tick_params(colors='white')

            # Title
            map_info = f"Map ID: {map_data.map_id}" if map_data and hasattr(map_data, 'map_id') else "Demo Map"
            ax.set_title(f'Dreame 3D Floor Plan - {map_info}', color='white', fontsize=16, pad=20)

            # Legend
            ax.legend(loc='upper right', bbox_to_anchor=(1.15, 1))

            print(f"3D DEBUG: Floor plan matplotlib figure created successfully")
            return fig

        except Exception as ex:
            print(f"3D DEBUG: Error creating floor plan matplotlib figure: {ex}")
            import traceback
            print(f"3D DEBUG: Figure traceback: {traceback.format_exc()}")

            try:
                plt.close('all')
            except:
                pass

            return None

    def _create_floor_plan_plotly_figure(self, floor_rooms: List[FloorRoom], wall_segments: List[WallSegment],
                                         robot_pos: Tuple[float, float], bounds: Tuple[float, float, float, float],
                                         visualization_type: str, map_data):
        """Create plotly figure showing floor plan"""
        try:
            print(f"3D DEBUG: Creating floor plan plotly figure")

            fig = go.Figure()

            # Define colors for rooms
            room_colors = [
                'green', 'blue', 'orange', 'purple', 'red', 'cyan', 'yellow', 'brown'
            ]

            # Add floor areas for each room
            for i, room in enumerate(floor_rooms):
                if not room.pixels:
                    continue

                color = room_colors[i % len(room_colors)]

                room_x = [p[0] for p in room.pixels]
                room_y = [p[1] for p in room.pixels]
                room_z = [self._config.room_height] * len(room.pixels)

                fig.add_trace(go.Scatter3d(
                    x=room_x, y=room_y, z=room_z,
                    mode='markers',
                    marker=dict(size=3, color=color, opacity=0.6),
                    name=f'Room {room.room_id}',
                    hoverinfo='name'
                ))

            # Add walls
            for segment in wall_segments:
                # Wall outline
                wall_x = [segment.start_x, segment.end_x, segment.end_x, segment.start_x, segment.start_x, None,
                          segment.start_x, segment.start_x, None, segment.end_x, segment.end_x]
                wall_y = [segment.start_y, segment.end_y, segment.end_y, segment.start_y, segment.start_y, None,
                          segment.start_y, segment.start_y, None, segment.end_y, segment.end_y]
                wall_z = [self._config.floor_height, self._config.floor_height, segment.height, segment.height,
                          self._config.floor_height, None,
                          self._config.floor_height, segment.height, None, self._config.floor_height, segment.height]

                fig.add_trace(go.Scatter3d(
                    x=wall_x, y=wall_y, z=wall_z,
                    mode='lines',
                    line=dict(color='white', width=4),
                    name='Walls' if segment == wall_segments[0] else None,
                    showlegend=(segment == wall_segments[0]),
                    hoverinfo='none'
                ))

            # Add robot position
            fig.add_trace(go.Scatter3d(
                x=[robot_pos[0]], y=[robot_pos[1]], z=[25],
                mode='markers',
                marker=dict(size=15, color='red', symbol='circle', line=dict(width=2, color='white')),
                name='Robot Position'
            ))

            # Layout
            map_info = f"Map ID: {map_data.map_id}" if map_data and hasattr(map_data, 'map_id') else "Demo Map"
            margin = 10

            fig.update_layout(
                title=f'Dreame 3D Floor Plan - {map_info}',
                scene=dict(
                    xaxis_title='X (map units)',
                    yaxis_title='Y (map units)',
                    zaxis_title='Height (cm)',
                    xaxis=dict(range=[bounds[0] - margin, bounds[2] + margin]),
                    yaxis=dict(range=[bounds[1] - margin, bounds[3] + margin]),
                    zaxis=dict(range=[0, self._config.wall_height + 20]),
                    camera=dict(
                        eye=dict(x=1.3, y=1.3, z=0.7),
                        center=dict(x=0, y=0, z=0.2),
                        up=dict(x=0, y=0, z=1)
                    ),
                    aspectmode='manual',
                    aspectratio=dict(x=1, y=(bounds[3] - bounds[1]) / (bounds[2] - bounds[0]), z=0.4),
                    bgcolor='black'
                ),
                paper_bgcolor='black',
                plot_bgcolor='black',
                font=dict(color='white'),
                showlegend=True,
                width=1400,
                height=900
            )

            print(f"3D DEBUG: Floor plan plotly figure created successfully")
            return fig

        except Exception as ex:
            print(f"3D DEBUG: Error creating floor plan plotly figure: {ex}")
            import traceback
            print(f"3D DEBUG: Figure traceback: {traceback.format_exc()}")
            return None


def check_3d_dependencies() -> Dict[str, bool]:
    """Check if 3D visualization dependencies are available"""
    return {
        'matplotlib': MATPLOTLIB_AVAILABLE,
        'plotly': PLOTLY_AVAILABLE
    }