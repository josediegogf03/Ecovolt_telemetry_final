# maindata.py
import asyncio
import csv
import json
import logging
import math
import os
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import threading
import queue
import struct

try:
    from ably import AblyRealtime
except ImportError:
    print("Error: Ably library not installed. Run: pip install ably")
    sys.exit(1)

try:
    from supabase import create_client, Client
except ImportError:
    print("Error: Supabase library not installed. Run: pip install supabase")
    sys.exit(1)

import numpy as np

# ------------------------------
# Configuration
# ------------------------------

# ESP32 MQTT source
ESP32_ABLY_API_KEY = (
    "ja_fwQ.K6CTEw:F-aWFMdJXPCv9MvxhYztCGna3XdRJZVgA0qm9pMfDOQ"
)
ESP32_CHANNEL_NAME = "EcoTele"

# Dashboard output
DASHBOARD_ABLY_API_KEY = (
    "DxuYSw.fQHpug:sa4tOcqWDkYBW9ht56s7fT0G091R1fyXQc6mc8WthxQ"
)
DASHBOARD_CHANNEL_NAME = "telemetry-dashboard-channel"

# Supabase
SUPABASE_URL = "https://rtfqlrvbmsnrkrdrknsp.supabase.co"
SUPABASE_API_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJ0ZnFscnZibXNucmtyZHJrbnNwIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc1MjEyMDc2NCwiZXhwIjoyMDY3Njk2NzY0fQ.DSitl6aXaAvzPg_Jx4yto4jC_4XzNqwvasOOUM1Qz4k"
)
SUPABASE_TABLE_NAME = "telemetry"

# Timings
MOCK_DATA_INTERVAL = 0.2  # seconds
DB_BATCH_INTERVAL = 9.0  # seconds
MAX_BATCH_SIZE = 200  # records per insert
RETRY_BASE_BACKOFF = 3.0  # seconds
RETRY_BACKOFF_MAX = 60.0  # seconds

# Reliability settings
CONNECTION_TIMEOUT = 15.0  # seconds
WATCHDOG_TIMEOUT = 30.0  # seconds - trigger reconnect if no data
HEALTH_CHECK_INTERVAL = 10.0  # seconds
MAX_QUEUE_SIZE = 5000  # prevent memory issues
RECONNECT_MAX_ATTEMPTS = 10
RECONNECT_BASE_DELAY = 1.0  # seconds

# Rate limiting settings
PUBLISH_RATE_LIMIT = 500  # messages per second
PUBLISH_BURST_CAPACITY = 100  # token bucket initial/max capacity
PUBLISH_QUEUE_MAX_SIZE = 10000  # max queued messages during bursts
PUBLISH_DRAIN_INTERVAL = 0.002  # 2ms between drain attempts

# Durability paths
SPOOL_DIR = "./spool"
EXPORT_DIR = "./export"

# Logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("TelemetryBridge")


# ============================================================
# MODULE: OUTLIER DETECTION ENGINE
# NumPy-based outlier detection for telemetry data
# Target latency: <5ms per message
# ============================================================

@dataclass
class OutlierConfig:
    """Configuration for outlier detection thresholds"""
    window_size: int = 50
    z_score_threshold: float = 5.0
    voltage_min: float = 35.0
    voltage_max: float = 60.0
    current_min: float = -10.0
    current_max: float = 35.0
    power_min: float = -500.0
    power_max: float = 2500.0
    electrical_jump_pct: float = 0.50
    stuck_sensor_count: int = 15
    accel_magnitude_max: float = 80.0
    gyro_rate_max: float = 1000.0
    altitude_min: float = -500.0
    altitude_max: float = 10000.0
    gps_speed_distance_ratio: float = 20.0
    gps_impossible_speed: float = 500.0
    altitude_rate_max: float = 50.0
    track_coherence_mad_mult: float = 10.0
    speed_max: float = 50.0
    speed_impossible_accel: float = 50.0
    sample_interval: float = 0.2


class OutlierSeverity(Enum):
    """Severity levels for detected outliers"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class OutlierReason(Enum):
    """Reason codes for outlier detection"""
    Z_SCORE_EXCEEDED = "z_score_exceeded"
    ABSOLUTE_BOUND = "absolute_bound"
    SUDDEN_JUMP = "sudden_jump"
    STUCK_SENSOR = "stuck_sensor"
    MAGNITUDE_EXCEEDED = "magnitude_exceeded"
    RATE_OF_CHANGE = "rate_of_change"
    CROSS_VALIDATION_FAILED = "cross_validation_failed"
    GPS_SPEED_MISMATCH = "gps_speed_mismatch"
    IMPOSSIBLE_SPEED = "impossible_speed"
    TRACK_DEVIATION = "track_deviation"
    ALTITUDE_RATE = "altitude_rate"
    NEGATIVE_VALUE = "negative_value"
    NON_MONOTONIC = "non_monotonic"
    IMPLAUSIBLE_INCREASE = "implausible_increase"


class RollingWindow:
    """Circular buffer for rolling statistics with NumPy"""
    
    def __init__(self, size: int = 50):
        self.size = size
        self.buffer = np.zeros(size, dtype=np.float64)
        self.count = 0
        self.index = 0
        self._mean_cache = None
        self._std_cache = None
        self._dirty = True
    
    def push(self, value: float) -> None:
        self.buffer[self.index] = value
        self.index = (self.index + 1) % self.size
        self.count = min(self.count + 1, self.size)
        self._dirty = True
    
    def get_values(self) -> np.ndarray:
        if self.count < self.size:
            return self.buffer[:self.count]
        return self.buffer
    
    def mean(self) -> float:
        if self.count == 0:
            return 0.0
        if self._dirty:
            self._update_stats()
        return self._mean_cache
    
    def std(self) -> float:
        if self.count < 2:
            return 0.0
        if self._dirty:
            self._update_stats()
        return self._std_cache
    
    def _update_stats(self) -> None:
        values = self.get_values()
        if len(values) > 0:
            self._mean_cache = float(np.mean(values))
            self._std_cache = float(np.std(values)) if len(values) > 1 else 0.0
        else:
            self._mean_cache = 0.0
            self._std_cache = 0.0
        self._dirty = False
    
    def last_n(self, n: int) -> np.ndarray:
        if self.count == 0:
            return np.array([])
        n = min(n, self.count)
        if self.count < self.size:
            return self.buffer[max(0, self.count - n):self.count]
        end = self.index
        start = (end - n) % self.size
        if start < end:
            return self.buffer[start:end]
        return np.concatenate([self.buffer[start:], self.buffer[:end]])
    
    def reset(self) -> None:
        self.buffer.fill(0)
        self.count = 0
        self.index = 0
        self._dirty = True


class GPSTrackWindow:
    """Rolling window for GPS track analysis"""
    
    def __init__(self, size: int = 20):
        self.size = size
        self.lats = np.zeros(size, dtype=np.float64)
        self.lons = np.zeros(size, dtype=np.float64)
        self.alts = np.zeros(size, dtype=np.float64)
        self.times = np.zeros(size, dtype=np.float64)
        self.count = 0
        self.index = 0
    
    def push(self, lat: float, lon: float, alt: float, timestamp: float) -> None:
        self.lats[self.index] = lat
        self.lons[self.index] = lon
        self.alts[self.index] = alt
        self.times[self.index] = timestamp
        self.index = (self.index + 1) % self.size
        self.count = min(self.count + 1, self.size)
    
    def get_last(self) -> Optional[tuple]:
        if self.count < 2:
            return None
        prev_idx = (self.index - 2) % self.size
        return (self.lats[prev_idx], self.lons[prev_idx], self.alts[prev_idx], self.times[prev_idx])
    
    def reset(self) -> None:
        self.lats.fill(0)
        self.lons.fill(0)
        self.alts.fill(0)
        self.times.fill(0)
        self.count = 0
        self.index = 0


class OutlierDetector:
    """NumPy-based outlier detection for telemetry data"""
    
    ROLLING_FIELDS = ["voltage_v", "current_a", "power_w", "gyro_x", "gyro_y", "gyro_z",
                      "accel_x", "accel_y", "accel_z", "speed_ms"]
    CRITICAL_FIELDS = {"voltage_v", "current_a", "power_w"}
    
    def __init__(self, config: Optional[OutlierConfig] = None):
        self.config = config or OutlierConfig()
        self.windows: Dict[str, RollingWindow] = {
            field: RollingWindow(self.config.window_size) for field in self.ROLLING_FIELDS
        }
        self.gps_track = GPSTrackWindow(size=20)
        self.last_energy = None
        self.last_distance = None
        self.stuck_counters: Dict[str, int] = {}
        self.last_values: Dict[str, float] = {}
        self.stats = {
            "total_messages": 0, "messages_with_outliers": 0,
            "outliers_by_field": {}, "outliers_by_severity": {"info": 0, "warning": 0, "critical": 0},
            "avg_detection_time_ms": 0.0, "detection_times": [],
        }
    
    def reset(self) -> None:
        for window in self.windows.values():
            window.reset()
        self.gps_track.reset()
        self.last_energy = None
        self.last_distance = None
        self.stuck_counters.clear()
        self.last_values.clear()
        self.stats = {"total_messages": 0, "messages_with_outliers": 0, "outliers_by_field": {},
                      "outliers_by_severity": {"info": 0, "warning": 0, "critical": 0},
                      "avg_detection_time_ms": 0.0, "detection_times": []}
    
    def detect(self, data: Dict[str, Any]) -> Dict[str, Any]:
        start_time = time.perf_counter()
        flagged_fields: set = set()
        confidence: Dict[str, float] = {}
        reasons: Dict[str, str] = {}
        max_severity = OutlierSeverity.INFO
        
        self._detect_electrical(data, flagged_fields, confidence, reasons)
        self._detect_imu(data, flagged_fields, confidence, reasons)
        self._detect_gps(data, flagged_fields, confidence, reasons)
        self._detect_speed(data, flagged_fields, confidence, reasons)
        self._detect_cumulative(data, flagged_fields, confidence, reasons)
        self._detect_stuck_sensors(data, flagged_fields, confidence, reasons)
        self._update_windows(data)
        
        if flagged_fields:
            if flagged_fields & self.CRITICAL_FIELDS:
                max_severity = OutlierSeverity.CRITICAL
            elif len(flagged_fields) >= 3:
                max_severity = OutlierSeverity.WARNING
            else:
                max_severity = OutlierSeverity.WARNING if any(c > 0.9 for c in confidence.values()) else OutlierSeverity.INFO
        
        detection_time = (time.perf_counter() - start_time) * 1000
        self.stats["total_messages"] += 1
        if flagged_fields:
            self.stats["messages_with_outliers"] += 1
            self.stats["outliers_by_severity"][max_severity.value] += 1
            for f in flagged_fields:
                self.stats["outliers_by_field"][f] = self.stats["outliers_by_field"].get(f, 0) + 1
        
        self.stats["detection_times"].append(detection_time)
        if len(self.stats["detection_times"]) > 100:
            self.stats["detection_times"] = self.stats["detection_times"][-100:]
        self.stats["avg_detection_time_ms"] = sum(self.stats["detection_times"]) / len(self.stats["detection_times"])
        
        if not flagged_fields:
            return {}
        return {"flagged_fields": list(flagged_fields), "confidence": confidence, "reasons": reasons, "severity": max_severity.value}
    
    def _detect_electrical(self, data, flagged, confidence, reasons):
        cfg = self.config
        if "voltage_v" in data:
            v = data["voltage_v"]
            window = self.windows["voltage_v"]
            if v < cfg.voltage_min or v > cfg.voltage_max:
                flagged.add("voltage_v"); confidence["voltage_v"] = 1.0; reasons["voltage_v"] = OutlierReason.ABSOLUTE_BOUND.value
            elif window.count >= 10:
                mean, std = window.mean(), window.std()
                if std > 0:
                    z = abs(v - mean) / std
                    if z > cfg.z_score_threshold:
                        flagged.add("voltage_v"); confidence["voltage_v"] = min(1.0, z / (cfg.z_score_threshold * 2)); reasons["voltage_v"] = OutlierReason.Z_SCORE_EXCEEDED.value
                if mean > 0 and abs(v - mean) / mean > cfg.electrical_jump_pct and "voltage_v" not in flagged:
                    flagged.add("voltage_v"); confidence["voltage_v"] = 0.7; reasons["voltage_v"] = OutlierReason.SUDDEN_JUMP.value
        if "current_a" in data:
            c = data["current_a"]
            window = self.windows["current_a"]
            if c < cfg.current_min or c > cfg.current_max:
                flagged.add("current_a"); confidence["current_a"] = 1.0; reasons["current_a"] = OutlierReason.ABSOLUTE_BOUND.value
            elif window.count >= 10:
                mean, std = window.mean(), window.std()
                if std > 0:
                    z = abs(c - mean) / std
                    if z > cfg.z_score_threshold:
                        flagged.add("current_a"); confidence["current_a"] = min(1.0, z / (cfg.z_score_threshold * 2)); reasons["current_a"] = OutlierReason.Z_SCORE_EXCEEDED.value
        if "power_w" in data:
            p = data["power_w"]
            if p < cfg.power_min or p > cfg.power_max:
                flagged.add("power_w"); confidence["power_w"] = 1.0; reasons["power_w"] = OutlierReason.ABSOLUTE_BOUND.value
    
    def _detect_imu(self, data, flagged, confidence, reasons):
        cfg = self.config
        ax, ay, az = data.get("accel_x", 0), data.get("accel_y", 0), data.get("accel_z", 0)
        if any(f in data for f in ["accel_x", "accel_y", "accel_z"]):
            magnitude = math.sqrt(ax**2 + ay**2 + az**2)
            if magnitude > cfg.accel_magnitude_max:
                max_axis = max([("accel_x", abs(ax)), ("accel_y", abs(ay)), ("accel_z", abs(az))], key=lambda x: x[1])
                flagged.add(max_axis[0]); confidence[max_axis[0]] = min(1.0, magnitude / cfg.accel_magnitude_max); reasons[max_axis[0]] = OutlierReason.MAGNITUDE_EXCEEDED.value
        for gyro_field in ["gyro_x", "gyro_y", "gyro_z"]:
            if gyro_field in data:
                window = self.windows[gyro_field]
                if window.count > 0:
                    last_vals = window.last_n(1)
                    if len(last_vals) > 0:
                        rate = abs(data[gyro_field] - last_vals[0])
                        if rate > cfg.gyro_rate_max:
                            flagged.add(gyro_field); confidence[gyro_field] = min(1.0, rate / (cfg.gyro_rate_max * 2)); reasons[gyro_field] = OutlierReason.RATE_OF_CHANGE.value
    
    def _detect_gps(self, data, flagged, confidence, reasons):
        cfg = self.config
        lat, lon, alt, speed = data.get("latitude"), data.get("longitude"), data.get("altitude", 0), data.get("speed_ms", 0)
        if lat is None or lon is None:
            return
        if not (-90 <= lat <= 90):
            flagged.add("latitude"); confidence["latitude"] = 1.0; reasons["latitude"] = OutlierReason.ABSOLUTE_BOUND.value
        if not (-180 <= lon <= 180):
            flagged.add("longitude"); confidence["longitude"] = 1.0; reasons["longitude"] = OutlierReason.ABSOLUTE_BOUND.value
        if alt < cfg.altitude_min or alt > cfg.altitude_max:
            flagged.add("altitude"); confidence["altitude"] = 1.0; reasons["altitude"] = OutlierReason.ABSOLUTE_BOUND.value
        prev = self.gps_track.get_last()
        if prev is not None:
            prev_lat, prev_lon, prev_alt, _ = prev
            dlat, dlon = lat - prev_lat, lon - prev_lon
            dist_m = math.sqrt((dlat * 111000)**2 + (dlon * 78000)**2)
            dt = cfg.sample_interval
            expected_dist = speed * dt
            if expected_dist > 0 and dist_m / expected_dist > cfg.gps_speed_distance_ratio:
                flagged.add("latitude"); confidence["latitude"] = min(1.0, (dist_m / expected_dist) / (cfg.gps_speed_distance_ratio * 2)); reasons["latitude"] = OutlierReason.GPS_SPEED_MISMATCH.value
            if dist_m / dt > cfg.gps_impossible_speed and "latitude" not in flagged:
                flagged.add("latitude"); confidence["latitude"] = min(1.0, (dist_m / dt) / (cfg.gps_impossible_speed * 2)); reasons["latitude"] = OutlierReason.IMPOSSIBLE_SPEED.value
            if abs(alt - prev_alt) > cfg.altitude_rate_max and "altitude" not in flagged:
                flagged.add("altitude"); confidence["altitude"] = min(1.0, abs(alt - prev_alt) / (cfg.altitude_rate_max * 2)); reasons["altitude"] = OutlierReason.ALTITUDE_RATE.value
        self.gps_track.push(lat, lon, alt, time.time())
    
    def _detect_speed(self, data, flagged, confidence, reasons):
        cfg = self.config
        if "speed_ms" not in data:
            return
        speed = data["speed_ms"]
        window = self.windows["speed_ms"]
        if speed < 0:
            flagged.add("speed_ms"); confidence["speed_ms"] = 1.0; reasons["speed_ms"] = OutlierReason.NEGATIVE_VALUE.value; return
        if speed > cfg.speed_max:
            flagged.add("speed_ms"); confidence["speed_ms"] = min(1.0, speed / (cfg.speed_max * 1.5)); reasons["speed_ms"] = OutlierReason.ABSOLUTE_BOUND.value; return
        if window.count > 0:
            last_vals = window.last_n(1)
            if len(last_vals) > 0:
                accel = abs(speed - last_vals[0]) / cfg.sample_interval
                if accel > cfg.speed_impossible_accel:
                    flagged.add("speed_ms"); confidence["speed_ms"] = min(1.0, accel / (cfg.speed_impossible_accel * 2)); reasons["speed_ms"] = OutlierReason.RATE_OF_CHANGE.value
    
    def _detect_cumulative(self, data, flagged, confidence, reasons):
        if "energy_j" in data:
            energy = data["energy_j"]
            if self.last_energy is not None:
                if energy < self.last_energy:
                    flagged.add("energy_j"); confidence["energy_j"] = 1.0; reasons["energy_j"] = OutlierReason.NON_MONOTONIC.value
                elif energy - self.last_energy > 50000:
                    flagged.add("energy_j"); confidence["energy_j"] = 0.8; reasons["energy_j"] = OutlierReason.IMPLAUSIBLE_INCREASE.value
            self.last_energy = energy
        if "distance_m" in data:
            distance = data["distance_m"]
            if self.last_distance is not None:
                if distance < self.last_distance:
                    flagged.add("distance_m"); confidence["distance_m"] = 1.0; reasons["distance_m"] = OutlierReason.NON_MONOTONIC.value
                elif distance - self.last_distance > 100:
                    flagged.add("distance_m"); confidence["distance_m"] = 0.8; reasons["distance_m"] = OutlierReason.IMPLAUSIBLE_INCREASE.value
            self.last_distance = distance
    
    def _detect_stuck_sensors(self, data, flagged, confidence, reasons):
        cfg = self.config
        for field in self.ROLLING_FIELDS:
            if field not in data:
                continue
            val = data[field]
            if field in self.last_values and self.last_values[field] == val:
                self.stuck_counters[field] = self.stuck_counters.get(field, 0) + 1
                if self.stuck_counters[field] >= cfg.stuck_sensor_count and field not in flagged:
                    flagged.add(field); confidence[field] = min(1.0, self.stuck_counters[field] / (cfg.stuck_sensor_count * 2)); reasons[field] = OutlierReason.STUCK_SENSOR.value
            else:
                self.stuck_counters[field] = 0
            self.last_values[field] = val
    
    def _update_windows(self, data: Dict[str, Any]) -> None:
        for field in self.ROLLING_FIELDS:
            if field in data:
                self.windows[field].push(data[field])
    
    def get_stats(self) -> Dict[str, Any]:
        return {**self.stats, "outliers_by_field": dict(self.stats["outliers_by_field"])}


# ============================================================
# END MODULE: OUTLIER DETECTION ENGINE
# ============================================================


# ------------------------------
# Mock Mode Configuration
# ------------------------------

class MockScenario(Enum):
    """Available mock simulation scenarios"""
    NORMAL = "normal"
    SENSOR_FAILURES = "sensor_failures"
    DATA_STALLS = "data_stalls"
    INTERMITTENT = "intermittent"
    GPS_ISSUES = "gps_issues"
    CHAOS = "chaos"


@dataclass
class MockModeConfig:
    """Configuration for mock data simulation with error scenarios and granular data parameters"""
    scenario: MockScenario = MockScenario.NORMAL
    
    # Data generation interval
    data_interval: float = 0.2  # seconds between data points
    
    # Electrical generation parameters (granular control)
    voltage_base: float = 48.0
    voltage_noise: float = 1.4
    voltage_min: float = 40.0
    voltage_max: float = 55.0
    current_base: float = 7.5
    current_noise: float = 0.9
    current_speed_factor: float = 0.2  # current increases with speed
    
    # Speed generation parameters
    speed_base: float = 15.0
    speed_amplitude: float = 5.0  # oscillation amplitude
    speed_noise: float = 1.4
    speed_max: float = 25.0
    
    # GPS base location
    gps_base_lat: float = 40.7128
    gps_base_lon: float = -74.0060
    gps_base_alt: float = 100.0
    gps_circle_radius: float = 0.001  # degrees, circular path
    gps_noise: float = 0.0001
    
    # IMU parameters
    imu_gyro_noise: float = 0.5
    imu_accel_noise: float = 0.2
    
    # Sensor failure settings
    sensor_failure_probability: float = 0.0  # 0-1, chance per message
    failed_sensors: List[str] = field(default_factory=list)
    sensor_failure_duration: int = 0  # messages to fail for
    
    # Data stall settings
    stall_probability: float = 0.0  # chance to start a stall
    stall_duration_min: float = 3.0  # seconds
    stall_duration_max: float = 15.0  # seconds
    stall_active: bool = False
    stall_end_time: float = 0.0
    
    # Intermittent connection settings
    drop_probability: float = 0.0  # chance to drop a message
    burst_drop_probability: float = 0.0  # chance to drop multiple messages
    burst_drop_count: int = 0  # remaining messages to drop in burst
    
    # GPS issues settings
    gps_drift_active: bool = False
    gps_accuracy_degraded: bool = False
    gps_jump_probability: float = 0.0  # chance of sudden position jump
    
    @classmethod
    def from_scenario(cls, scenario: MockScenario) -> "MockModeConfig":
        """Create configuration for a specific scenario"""
        config = cls(scenario=scenario)
        
        if scenario == MockScenario.NORMAL:
            pass  # All defaults (no errors)
            
        elif scenario == MockScenario.SENSOR_FAILURES:
            config.sensor_failure_probability = 0.08
            config.sensor_failure_duration = 25
            
        elif scenario == MockScenario.DATA_STALLS:
            config.stall_probability = 0.02
            config.stall_duration_min = 5.0
            config.stall_duration_max = 20.0
            
        elif scenario == MockScenario.INTERMITTENT:
            config.drop_probability = 0.05
            config.burst_drop_probability = 0.02
            
        elif scenario == MockScenario.GPS_ISSUES:
            config.gps_drift_active = True
            config.gps_accuracy_degraded = True
            config.gps_jump_probability = 0.01
            
        elif scenario == MockScenario.CHAOS:
            # Everything enabled at moderate levels
            config.sensor_failure_probability = 0.04
            config.sensor_failure_duration = 15
            config.stall_probability = 0.01
            config.stall_duration_min = 3.0
            config.stall_duration_max = 10.0
            config.drop_probability = 0.03
            config.burst_drop_probability = 0.01
            config.gps_drift_active = True
            config.gps_jump_probability = 0.005
            
        return config


# ============================================================
# MODULE: MOCK DATA GENERATOR
# Standalone mock telemetry data generation with error simulation
# ============================================================

class MockDataGenerator:
    """
    Standalone mock telemetry data generator.
    Can be used independently for testing or by the TelemetryBridge.
    Uses MockModeConfig for all parameters.
    """
    
    def __init__(self, config: Optional[MockModeConfig] = None, session_id: str = "mock-session", 
                 session_name: str = "Mock Session"):
        self.config = config or MockModeConfig()
        self.session_id = session_id
        self.session_name = session_name
        
        # Simulation state
        self.cumulative_distance = 0.0
        self.cumulative_energy = 0.0
        self.simulation_time = 0
        self.prev_speed = 0.0
        self.message_count = 0
        
        # Error simulation state
        self._sensor_failure_remaining = 0
        self._current_failed_sensors: List[str] = []
        self._gps_drift_offset = (0.0, 0.0)
        
        # Stats
        self.stats = {"messages_generated": 0, "messages_dropped": 0, "sensor_failures": 0, 
                      "gps_jumps": 0, "stalls": 0}
    
    def reset(self) -> None:
        """Reset generator state for a new session"""
        self.cumulative_distance = 0.0
        self.cumulative_energy = 0.0
        self.simulation_time = 0
        self.prev_speed = 0.0
        self.message_count = 0
        self._sensor_failure_remaining = 0
        self._current_failed_sensors = []
        self._gps_drift_offset = (0.0, 0.0)
        self.stats = {k: 0 for k in self.stats}
    
    def _apply_sensor_failures(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply sensor failure simulation"""
        cfg = self.config
        if self._sensor_failure_remaining <= 0:
            if random.random() < cfg.sensor_failure_probability:
                self._sensor_failure_remaining = cfg.sensor_failure_duration
                all_sensors = ["voltage_v", "current_a", "gyro_x", "gyro_y", "gyro_z", 
                               "accel_x", "accel_y", "accel_z"]
                self._current_failed_sensors = random.sample(all_sensors, random.randint(1, 4))
                self.stats["sensor_failures"] += 1
                logger.warning(f"⚠️ MOCK: Sensor failure started for {self._current_failed_sensors}")
        
        if self._sensor_failure_remaining > 0:
            for sensor in self._current_failed_sensors:
                if sensor in data:
                    data[sensor] = 0.0 if random.random() < 0.7 else random.uniform(-999, 999)
            self._sensor_failure_remaining -= 1
            if self._sensor_failure_remaining == 0:
                logger.info("✅ MOCK: Sensor failure recovered")
        return data
    
    def _apply_gps_issues(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Apply GPS simulation issues"""
        cfg = self.config
        if not cfg.gps_drift_active and not cfg.gps_accuracy_degraded:
            return data
        
        if cfg.gps_drift_active:
            self._gps_drift_offset = (
                self._gps_drift_offset[0] + random.gauss(0, 0.00002),
                self._gps_drift_offset[1] + random.gauss(0, 0.00002)
            )
            if random.random() < 0.005:
                self._gps_drift_offset = (self._gps_drift_offset[0] * 0.5, self._gps_drift_offset[1] * 0.5)
            data["latitude"] = data.get("latitude", 0) + self._gps_drift_offset[0]
            data["longitude"] = data.get("longitude", 0) + self._gps_drift_offset[1]
        
        if cfg.gps_accuracy_degraded:
            data["latitude"] = data.get("latitude", 0) + random.gauss(0, 0.0005)
            data["longitude"] = data.get("longitude", 0) + random.gauss(0, 0.0005)
            data["altitude"] = data.get("altitude", 0) + random.gauss(0, 5)
        
        if random.random() < cfg.gps_jump_probability:
            jump_lat, jump_lon = random.uniform(-0.01, 0.01), random.uniform(-0.01, 0.01)
            data["latitude"] = data.get("latitude", 0) + jump_lat
            data["longitude"] = data.get("longitude", 0) + jump_lon
            self.stats["gps_jumps"] += 1
            logger.warning(f"⚠️ MOCK: GPS position jump ({jump_lat:.4f}, {jump_lon:.4f})")
        return data
    
    def _should_stall(self) -> bool:
        """Check if we should stall data generation"""
        cfg = self.config
        now = time.monotonic()
        if cfg.stall_active:
            if now < cfg.stall_end_time:
                return True
            cfg.stall_active = False
            logger.info("✅ MOCK: Data stall ended")
            return False
        if random.random() < cfg.stall_probability:
            duration = random.uniform(cfg.stall_duration_min, cfg.stall_duration_max)
            cfg.stall_active = True
            cfg.stall_end_time = now + duration
            self.stats["stalls"] += 1
            logger.warning(f"⚠️ MOCK: Data stall started ({duration:.1f}s)")
            return True
        return False
    
    def _should_drop_message(self) -> bool:
        """Check if we should drop this message"""
        cfg = self.config
        if cfg.burst_drop_count > 0:
            cfg.burst_drop_count -= 1
            return True
        if random.random() < cfg.burst_drop_probability:
            cfg.burst_drop_count = random.randint(3, 10)
            return True
        return random.random() < cfg.drop_probability
    
    def generate(self) -> Optional[Dict[str, Any]]:
        """Generate a single mock telemetry data point. Returns None if stalled/dropped."""
        if self._should_stall():
            return None
        if self._should_drop_message():
            self.stats["messages_dropped"] += 1
            return None
        
        cfg = self.config
        now = datetime.now(timezone.utc)
        
        # Speed with oscillation and noise
        base_speed = cfg.speed_base + cfg.speed_amplitude * math.sin(self.simulation_time * 0.1)
        speed = max(0, min(cfg.speed_max, base_speed + random.gauss(0, cfg.speed_noise)))
        
        # Electrical values using config parameters
        voltage = max(cfg.voltage_min, min(cfg.voltage_max, cfg.voltage_base + random.gauss(0, cfg.voltage_noise)))
        current = max(0, min(15, cfg.current_base + speed * cfg.current_speed_factor + random.gauss(0, cfg.current_noise)))
        power = voltage * current
        
        # Cumulative values
        energy_delta = power * cfg.data_interval
        distance_delta = speed * cfg.data_interval
        self.cumulative_energy += energy_delta
        self.cumulative_distance += distance_delta
        
        # GPS using config base location
        lat_offset = cfg.gps_circle_radius * math.sin(self.simulation_time * 0.05)
        lon_offset = cfg.gps_circle_radius * math.cos(self.simulation_time * 0.05)
        latitude = cfg.gps_base_lat + lat_offset + random.gauss(0, cfg.gps_noise)
        longitude = cfg.gps_base_lon + lon_offset + random.gauss(0, cfg.gps_noise)
        altitude_variation = 10.0 * math.sin(self.simulation_time * 0.03)
        altitude = cfg.gps_base_alt + altitude_variation + random.gauss(0, 1.0)
        
        # IMU using config noise parameters
        turning_rate = 2.0 * math.sin(self.simulation_time * 0.08)
        gyro_x = random.gauss(0, cfg.imu_gyro_noise)
        gyro_y = random.gauss(0, cfg.imu_gyro_noise * 0.6)
        gyro_z = turning_rate + random.gauss(0, cfg.imu_gyro_noise * 1.6)
        
        speed_acc = (speed - self.prev_speed) / cfg.data_interval
        self.prev_speed = speed
        accel_x = speed_acc + random.gauss(0, cfg.imu_accel_noise)
        accel_y = turning_rate * speed * 0.1 + random.gauss(0, cfg.imu_accel_noise * 0.5)
        accel_z = 9.81 + random.gauss(0, cfg.imu_accel_noise * 0.25)
        vib = speed * 0.02
        accel_x += random.gauss(0, vib)
        accel_y += random.gauss(0, vib)
        accel_z += random.gauss(0, vib)
        total_acc = math.sqrt(accel_x**2 + accel_y**2 + accel_z**2)
        
        # Driver inputs
        phase = (math.sin(self.simulation_time * 0.06) + 1) / 2
        th_base = 20 + 70 * phase
        brake_event = (self.simulation_time % 120) in range(0, 12) or random.random() < 0.03
        if brake_event:
            brake_pct = min(100.0, max(15.0, 60 + random.gauss(0, 15)))
            throttle_pct = max(0.0, th_base - brake_pct * 0.6)
        else:
            brake_pct = max(0.0, random.gauss(2, 1))
            throttle_pct = min(100.0, max(5.0, th_base + random.gauss(0, 5)))
        
        self.simulation_time += 1
        self.message_count += 1
        self.stats["messages_generated"] += 1
        
        data = {
            "timestamp": now.isoformat(), "speed_ms": round(speed, 2), "voltage_v": round(voltage, 2),
            "current_a": round(current, 2), "power_w": round(power, 2), "energy_j": round(self.cumulative_energy, 2),
            "distance_m": round(self.cumulative_distance, 2), "latitude": round(latitude, 6),
            "longitude": round(longitude, 6), "altitude": round(altitude, 2), "gyro_x": round(gyro_x, 3),
            "gyro_y": round(gyro_y, 3), "gyro_z": round(gyro_z, 3), "accel_x": round(accel_x, 3),
            "accel_y": round(accel_y, 3), "accel_z": round(accel_z, 3), "total_acceleration": round(total_acc, 3),
            "message_id": self.message_count, "uptime_seconds": self.simulation_time * cfg.data_interval,
            "data_source": f"MOCK_{cfg.scenario.value.upper()}", "session_id": self.session_id,
            "session_name": self.session_name, "throttle_pct": round(throttle_pct, 1),
            "brake_pct": round(brake_pct, 1), "throttle": round(throttle_pct / 100.0, 3),
            "brake": round(brake_pct / 100.0, 3),
        }
        
        # Apply error simulations
        if cfg.scenario in (MockScenario.SENSOR_FAILURES, MockScenario.CHAOS):
            data = self._apply_sensor_failures(data)
        if cfg.scenario in (MockScenario.GPS_ISSUES, MockScenario.CHAOS):
            data = self._apply_gps_issues(data)
        
        return data
    
    def generate_batch(self, count: int, include_stalls: bool = False) -> List[Dict[str, Any]]:
        """Generate multiple data points for batch testing"""
        results = []
        for _ in range(count):
            data = self.generate()
            if data is not None or include_stalls:
                results.append(data)
        return results


# ============================================================
# END MODULE: MOCK DATA GENERATOR
# ============================================================


# ------------------------------
# Connection Health Monitor
# ------------------------------

@dataclass
class ConnectionHealth:
    """Tracks connection health and metrics"""
    is_connected: bool = False
    last_message_time: float = 0.0
    last_health_check: float = 0.0
    reconnect_attempts: int = 0
    total_reconnects: int = 0
    messages_since_connect: int = 0
    error_count: int = 0
    error_rate: float = 0.0  # errors per minute
    last_error_time: float = 0.0
    
    def record_message(self):
        self.last_message_time = time.monotonic()
        self.messages_since_connect += 1
        
    def record_error(self):
        now = time.monotonic()
        self.error_count += 1
        # Calculate rolling error rate (errors in last minute)
        if now - self.last_error_time > 60:
            self.error_rate = 1.0
        else:
            self.error_rate = min(100, self.error_rate + 1)
        self.last_error_time = now
        
    def is_stale(self, timeout: float) -> bool:
        if self.last_message_time == 0:
            return False
        return time.monotonic() - self.last_message_time > timeout
        
    def reset_for_reconnect(self):
        self.is_connected = False
        self.reconnect_attempts += 1
        self.total_reconnects += 1
        self.messages_since_connect = 0


# ------------------------------
# Rate-Limited Publisher
# ------------------------------

class RateLimitedPublisher:
    """
    Token bucket rate limiter with FIFO queue for message publishing.
    Prevents Ably rate limit violations during bursts or reconnection.
    """
    
    def __init__(
        self,
        rate_limit: float = PUBLISH_RATE_LIMIT,
        burst_capacity: float = PUBLISH_BURST_CAPACITY,
        max_queue_size: int = PUBLISH_QUEUE_MAX_SIZE,
        drain_interval: float = PUBLISH_DRAIN_INTERVAL
    ):
        self.rate_limit = rate_limit  # tokens per second
        self.burst_capacity = burst_capacity  # max tokens
        self.max_queue_size = max_queue_size
        self.drain_interval = drain_interval
        
        # Token bucket state
        self._tokens = burst_capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        
        # Message queue for overflow
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max_queue_size)
        
        # Statistics
        self.stats = {
            "queue_depth": 0,
            "burst_events": 0,
            "messages_delayed": 0,
            "messages_dropped": 0,
            "messages_published": 0,
            "drain_cycles": 0,
        }
    
    def _refill_tokens(self) -> None:
        """Refill tokens based on elapsed time"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.rate_limit
        self._tokens = min(self.burst_capacity, self._tokens + new_tokens)
        self._last_refill = now
    
    def _try_consume_token(self) -> bool:
        """Try to consume a token. Returns True if token was available."""
        with self._lock:
            self._refill_tokens()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False
    
    def queue_message(self, message: Dict[str, Any]) -> bool:
        """
        Queue a message for publishing.
        Returns True if message was queued, False if dropped due to full queue.
        """
        try:
            self._queue.put_nowait(message)
            self.stats["messages_delayed"] += 1
            self.stats["queue_depth"] = self._queue.qsize()
            return True
        except queue.Full:
            self.stats["messages_dropped"] += 1
            logger.warning(f"⚠️ Rate limiter queue full, dropping message")
            return False
    
    async def publish(
        self, 
        channel, 
        event_name: str, 
        message: Dict[str, Any],
        force_queue: bool = False
    ) -> bool:
        """
        Publish a message with rate limiting.
        If under rate limit, publishes immediately.
        If over rate limit, queues the message for later drain.
        
        Args:
            channel: Ably channel to publish to
            event_name: Event name for the publish
            message: Message data to publish
            force_queue: If True, queue instead of trying immediate publish
        
        Returns:
            True if published or queued, False if dropped
        """
        if force_queue:
            return self.queue_message(message)
        
        if self._try_consume_token():
            # Token available - publish immediately
            try:
                await channel.publish(event_name, message)
                self.stats["messages_published"] += 1
                return True
            except Exception as e:
                # On error, queue for retry
                logger.warning(f"⚠️ Publish failed, queuing: {e}")
                return self.queue_message(message)
        else:
            # No token - queue for later
            self.stats["burst_events"] += 1
            return self.queue_message(message)
    
    async def drain_queue(self, channel, event_name: str) -> int:
        """
        Drain queued messages at controlled rate.
        Call this periodically to drain the queue.
        
        Returns:
            Number of messages drained
        """
        drained = 0
        
        while not self._queue.empty():
            if not self._try_consume_token():
                # No tokens available, wait and retry
                await asyncio.sleep(self.drain_interval)
                continue
            
            try:
                message = self._queue.get_nowait()
                await channel.publish(event_name, message)
                self.stats["messages_published"] += 1
                drained += 1
                self.stats["queue_depth"] = self._queue.qsize()
            except queue.Empty:
                break
            except Exception as e:
                logger.warning(f"⚠️ Drain publish failed: {e}")
                # Put back for retry
                try:
                    self._queue.put_nowait(message)
                except queue.Full:
                    self.stats["messages_dropped"] += 1
                break
        
        if drained > 0:
            self.stats["drain_cycles"] += 1
        
        return drained
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current rate limiter statistics"""
        with self._lock:
            self._refill_tokens()
            return {
                **self.stats,
                "available_tokens": round(self._tokens, 2),
                "queue_depth": self._queue.qsize(),
            }
    
    def reset_stats(self) -> None:
        """Reset statistics counters"""
        self.stats = {k: 0 for k in self.stats}


# ------------------------------
# Local durable journal
# ------------------------------

class LocalJournal:
    """
    Append-only NDJSON per session to guarantee durability.
    """

    def __init__(self, spool_dir: str, session_id: str):
        os.makedirs(spool_dir, exist_ok=True)
        self.path = os.path.join(spool_dir, f"{session_id}.ndjson")
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")

    def append(self, record: Dict[str, Any]) -> None:
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"❌ Failed to append to journal: {e}")

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass

    def iter_records(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return

    def export_csv(self, out_path: str, field_order: List[str]) -> int:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        n = 0
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(field_order)
            for rec in self.iter_records():
                row = []
                for col in field_order:
                    v = rec.get(col, "")
                    row.append(v)
                w.writerow(row)
                n += 1
        return n


# ------------------------------
# Bridge
# ------------------------------

class TelemetryBridgeWithDB:
    """
    - Subscribes to ESP32 (real) or generates mock data
    - Republishes to dashboard
    - Stores to Supabase in batches with retry/backoff
    - Local-first journaling (NDJSON) to avoid data loss
    - On shutdown, exports CSV if any DB failure or pending retries
    - Enhanced reliability: connection monitoring, auto-reconnect, watchdog
    - Mock mode: configurable error simulations
    """

    def __init__(
        self, 
        mock_mode: bool = False, 
        session_name: Optional[str] = None,
        mock_config: Optional[MockModeConfig] = None
    ):
        self.mock_mode = mock_mode
        self.mock_config = mock_config or MockModeConfig()
        
        self.esp32_client: Optional[AblyRealtime] = None
        self.dashboard_client: Optional[AblyRealtime] = None
        self.supabase_client: Optional[Client] = None
        self.esp32_channel = None
        self.dashboard_channel = None

        self.running = False
        self.shutdown_event = asyncio.Event()

        # Use bounded queue to prevent memory issues
        self.message_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self.db_buffer: List[Dict[str, Any]] = []
        self.db_buffer_lock = threading.Lock()

        self.db_retry_queue: List[List[Dict[str, Any]]] = []
        self.db_retry_backoff = RETRY_BASE_BACKOFF
        self.db_write_failures = 0

        self.session_id = str(uuid.uuid4())
        self.session_start_time = datetime.now(timezone.utc)

        if session_name and session_name.strip():
            self.session_name = session_name.strip()
        else:
            self.session_name = f"Session {self.session_id[:8]}"

        self.journal = LocalJournal(SPOOL_DIR, self.session_id)

        # Connection health tracking
        self.esp32_health = ConnectionHealth()
        self.dashboard_health = ConnectionHealth()
        self._reconnect_lock = asyncio.Lock()
        
        # Rate-limited publisher
        self.rate_limiter = RateLimitedPublisher()
        
        # Outlier detector (embedded module)
        self.outlier_detector = OutlierDetector()

        self.stats = {
            "messages_received": 0,
            "messages_republished": 0,
            "messages_stored_db": 0,
            "messages_dropped": 0,
            "last_message_time": None,
            "last_db_write_time": None,
            "errors": 0,
            "last_error": None,
            "current_session_id": self.session_id,
            "current_session_name": self.session_name,
            "session_start_time": self.session_start_time.isoformat(),
            "mock_scenario": self.mock_config.scenario.value if mock_mode else None,
            "reconnect_count": 0,
        }

        # ESP32 binary format
        self.BINARY_FORMAT = "<ffffffI"
        self.BINARY_FIELD_NAMES = [
            "speed_ms",
            "voltage_v",
            "current_a",
            "latitude",
            "longitude",
            "altitude",
            "message_id",
        ]
        self.BINARY_MESSAGE_SIZE = struct.calcsize(self.BINARY_FORMAT)

        # Mock data generator (uses new standalone module)
        if self.mock_mode:
            self.mock_generator = MockDataGenerator(
                config=self.mock_config,
                session_id=self.session_id,
                session_name=self.session_name
            )
        else:
            self.mock_generator = None

        # Signals
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(f"🆔 New session: {self.session_id}")
        logger.info(f"📝 Session name: {self.session_name}")
        if self.mock_mode:
            logger.info(f"🎭 MOCK MODE: {self.mock_config.scenario.value.upper()}")
        else:
            logger.info("🔗 REAL MODE ENABLED")

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.running = False
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self.shutdown_event.set)
        except Exception:
            pass

    # ------------- Connections with reliability -------------

    async def connect_supabase(self) -> bool:
        try:
            self.supabase_client = create_client(SUPABASE_URL, SUPABASE_API_KEY)
            logger.info("✅ Connected to Supabase")
            return True
        except Exception as e:
            self._count_error(f"Supabase connect failed: {e}")
            return False

    async def connect_esp32_subscriber(self) -> bool:
        if self.mock_mode:
            return True
        try:
            self.esp32_client = AblyRealtime(ESP32_ABLY_API_KEY)
            await self._wait_for_connection(self.esp32_client, "ESP32", CONNECTION_TIMEOUT)
            self.esp32_channel = self.esp32_client.channels.get(ESP32_CHANNEL_NAME)
            await self.esp32_channel.subscribe(self._on_esp32_message_received)
            self.esp32_health.is_connected = True
            self.esp32_health.reconnect_attempts = 0
            logger.info(f"✅ Subscribed to ESP32 channel: {ESP32_CHANNEL_NAME}")
            return True
        except Exception as e:
            self._count_error(f"ESP32 connect failed: {e}")
            self.esp32_health.record_error()
            return False

    async def connect_dashboard_publisher(self) -> bool:
        try:
            self.dashboard_client = AblyRealtime(DASHBOARD_ABLY_API_KEY)
            await self._wait_for_connection(self.dashboard_client, "Dashboard", CONNECTION_TIMEOUT)
            self.dashboard_channel = self.dashboard_client.channels.get(
                DASHBOARD_CHANNEL_NAME
            )
            self.dashboard_health.is_connected = True
            self.dashboard_health.reconnect_attempts = 0
            logger.info(f"✅ Connected to dashboard channel: {DASHBOARD_CHANNEL_NAME}")
            return True
        except Exception as e:
            self._count_error(f"Dashboard connect failed: {e}")
            self.dashboard_health.record_error()
            return False

    async def _wait_for_connection(self, client, name: str, timeout: float = 10):
        logger.info(f"Waiting for {name} connection...")
        start = time.time()
        while time.time() - start < timeout:
            if client.connection.state == "connected":
                logger.info(f"✅ {name} connected")
                return
            if client.connection.state in ("failed", "closed", "suspended"):
                raise ConnectionError(f"{name} connection state: {client.connection.state}")
            await asyncio.sleep(0.1)
        raise TimeoutError(f"{name} connection timeout after {timeout}s")

    async def _reconnect_esp32(self) -> bool:
        """Attempt to reconnect to ESP32 with exponential backoff"""
        async with self._reconnect_lock:
            if self.esp32_health.reconnect_attempts >= RECONNECT_MAX_ATTEMPTS:
                logger.error(f"❌ ESP32 max reconnect attempts ({RECONNECT_MAX_ATTEMPTS}) reached")
                return False
            
            self.esp32_health.reset_for_reconnect()
            delay = min(
                RECONNECT_BASE_DELAY * (2 ** self.esp32_health.reconnect_attempts),
                RETRY_BACKOFF_MAX
            )
            
            logger.warning(f"🔄 Reconnecting to ESP32 (attempt {self.esp32_health.reconnect_attempts}) in {delay:.1f}s...")
            await asyncio.sleep(delay)
            
            try:
                if self.esp32_client:
                    try:
                        await self.esp32_client.close()
                    except Exception:
                        pass
                
                self.esp32_client = AblyRealtime(ESP32_ABLY_API_KEY)
                await self._wait_for_connection(self.esp32_client, "ESP32", CONNECTION_TIMEOUT)
                self.esp32_channel = self.esp32_client.channels.get(ESP32_CHANNEL_NAME)
                await self.esp32_channel.subscribe(self._on_esp32_message_received)
                
                self.esp32_health.is_connected = True
                self.esp32_health.reconnect_attempts = 0
                self.stats["reconnect_count"] += 1
                logger.info("✅ ESP32 reconnected successfully")
                return True
                
            except Exception as e:
                self._count_error(f"ESP32 reconnect failed: {e}")
                self.esp32_health.record_error()
                return False

    async def _reconnect_dashboard(self) -> bool:
        """Attempt to reconnect to dashboard with exponential backoff"""
        async with self._reconnect_lock:
            if self.dashboard_health.reconnect_attempts >= RECONNECT_MAX_ATTEMPTS:
                logger.error(f"❌ Dashboard max reconnect attempts ({RECONNECT_MAX_ATTEMPTS}) reached")
                return False
            
            self.dashboard_health.reset_for_reconnect()
            delay = min(
                RECONNECT_BASE_DELAY * (2 ** self.dashboard_health.reconnect_attempts),
                RETRY_BACKOFF_MAX
            )
            
            logger.warning(f"🔄 Reconnecting to Dashboard (attempt {self.dashboard_health.reconnect_attempts}) in {delay:.1f}s...")
            await asyncio.sleep(delay)
            
            try:
                if self.dashboard_client:
                    try:
                        await self.dashboard_client.close()
                    except Exception:
                        pass
                
                self.dashboard_client = AblyRealtime(DASHBOARD_ABLY_API_KEY)
                await self._wait_for_connection(self.dashboard_client, "Dashboard", CONNECTION_TIMEOUT)
                self.dashboard_channel = self.dashboard_client.channels.get(DASHBOARD_CHANNEL_NAME)
                
                self.dashboard_health.is_connected = True
                self.dashboard_health.reconnect_attempts = 0
                self.stats["reconnect_count"] += 1
                logger.info("✅ Dashboard reconnected successfully")
                return True
                
            except Exception as e:
                self._count_error(f"Dashboard reconnect failed: {e}")
                self.dashboard_health.record_error()
                return False

    # ------------- Mock generation (delegates to MockDataGenerator module) -------------

    def generate_mock_telemetry_data(self) -> Optional[Dict[str, Any]]:
        """Generate mock telemetry data - delegates to MockDataGenerator module"""
        if self.mock_generator is None:
            return None
        return self.mock_generator.generate()

    # ------------- Parsers -------------

    def _parse_json_message(self, b: bytes) -> Optional[Dict]:
        try:
            s = b.decode("utf-8") if isinstance(b, (bytes, bytearray)) else str(b)
            return json.loads(s)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _parse_binary_message(self, b: bytes) -> Optional[Dict]:
        if not isinstance(b, (bytes, bytearray)) or len(b) != self.BINARY_MESSAGE_SIZE:
            return None
        try:
            vals = struct.unpack(self.BINARY_FORMAT, b)
            d = dict(zip(self.BINARY_FIELD_NAMES, vals))
            d["power_w"] = d["voltage_v"] * d["current_a"]
            return d
        except struct.error:
            return None

    def _validate_message(self, data: Dict[str, Any]) -> bool:
        """Validate that message has minimum required fields and sane values"""
        if not isinstance(data, dict):
            return False
        
        # Check for at least some core fields
        core_fields = ["speed_ms", "voltage_v", "current_a"]
        has_core = any(field in data for field in core_fields)
        
        if not has_core:
            return False
        
        # Sanity check numeric values (prevent NaN/Inf)
        for key, val in data.items():
            if isinstance(val, float):
                if math.isnan(val) or math.isinf(val):
                    logger.warning(f"⚠️ Invalid value for {key}: {val}")
                    data[key] = 0.0
        
        return True

    # ------------- Normalization -------------

    def _normalize_telemetry_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        out = data.copy()
        out["session_id"] = self.session_id
        out["session_name"] = self.session_name

        # timestamp
        if "timestamp" not in out or str(out["timestamp"]).startswith("1970-01-01"):
            out["timestamp"] = datetime.now(timezone.utc).isoformat()
        if isinstance(out["timestamp"], str):
            try:
                dt = datetime.fromisoformat(out["timestamp"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                out["timestamp"] = dt.isoformat()
            except Exception:
                out["timestamp"] = datetime.now(timezone.utc).isoformat()

        # defaults
        defaults = {
            "speed_ms": 0.0,
            "voltage_v": 0.0,
            "current_a": 0.0,
            "power_w": 0.0,
            "energy_j": 0.0,
            "distance_m": 0.0,
            "latitude": 0.0,
            "longitude": 0.0,
            "altitude": 0.0,
            "gyro_x": 0.0,
            "gyro_y": 0.0,
            "gyro_z": 0.0,
            "accel_x": 0.0,
            "accel_y": 0.0,
            "accel_z": 0.0,
            "total_acceleration": 0.0,
            "message_id": 0,
            "uptime_seconds": 0.0,
            "throttle_pct": 0.0,
            "brake_pct": 0.0,
            "throttle": 0.0,
            "brake": 0.0,
            "data_source": "ESP32_REAL" if not self.mock_mode else "MOCK_GENERATOR",
        }
        for k, v in defaults.items():
            out.setdefault(k, v)

        if not out.get("power_w"):
            out["power_w"] = out.get("voltage_v", 0.0) * out.get("current_a", 0.0)

        if not out.get("total_acceleration"):
            out["total_acceleration"] = math.sqrt(
                out.get("accel_x", 0.0) ** 2
                + out.get("accel_y", 0.0) ** 2
                + out.get("accel_z", 0.0) ** 2
            )

        # sync driver inputs between % and 0..1
        def _clamp01(v: float) -> float:
            try:
                return max(0.0, min(1.0, float(v)))
            except Exception:
                return 0.0

        if out.get("throttle_pct", 0) == 0 and out.get("throttle", 0) != 0:
            out["throttle_pct"] = round(_clamp01(out["throttle"]) * 100.0, 2)
        if out.get("brake_pct", 0) == 0 and out.get("brake", 0) != 0:
            out["brake_pct"] = round(_clamp01(out["brake"]) * 100.0, 2)
        if out.get("throttle", 0) == 0 and out.get("throttle_pct", 0) != 0:
            out["throttle"] = round(_clamp01(out["throttle_pct"] / 100.0), 3)
        if out.get("brake", 0) == 0 and out.get("brake_pct", 0) != 0:
            out["brake"] = round(_clamp01(out["brake_pct"] / 100.0), 3)

        # Run outlier detection (always available - embedded module)
        try:
            outliers = self.outlier_detector.detect(out)
            out["outliers"] = outliers if outliers else None
        except Exception as e:
            logger.warning(f"⚠️ Outlier detection failed: {e}")
            out["outliers"] = None

        return out

    # ------------- ESP32 handler -------------

    def _on_esp32_message_received(self, message):
        try:
            data = None
            if isinstance(message.data, (bytes, bytearray)):
                data = self._parse_json_message(message.data) or self._parse_binary_message(
                    message.data
                )
            elif isinstance(message.data, str):
                try:
                    data = json.loads(message.data)
                except json.JSONDecodeError:
                    data = None
            elif isinstance(message.data, dict):
                data = message.data

            if data is None:
                self._count_error("Failed to parse incoming ESP32 message")
                return
            
            # Validate message
            if not self._validate_message(data):
                self._count_error("Message validation failed")
                return

            normalized = self._normalize_telemetry_data(data)

            # 1) durable journal
            self.journal.append(normalized)

            # 2) realtime + db buffer (with backpressure)
            try:
                self.message_queue.put_nowait(normalized)
            except queue.Full:
                # Drop oldest messages if queue is full
                try:
                    self.message_queue.get_nowait()
                    self.message_queue.put_nowait(normalized)
                    self.stats["messages_dropped"] += 1
                except queue.Empty:
                    pass
            
            with self.db_buffer_lock:
                self.db_buffer.append(normalized)

            self.stats["messages_received"] += 1
            self.stats["last_message_time"] = datetime.now(timezone.utc)
            self.esp32_health.record_message()

        except Exception as e:
            self._count_error(f"ESP32 handler error: {e}")
            self.esp32_health.record_error()

    # ------------- Mock loop -------------

    async def generate_mock_data_loop(self):
        if not self.mock_mode:
            return
        while self.running and not self.shutdown_event.is_set():
            try:
                mock = self.generate_mock_telemetry_data()
                
                if mock is None:
                    # Stall or drop simulation - just wait
                    await asyncio.sleep(MOCK_DATA_INTERVAL)
                    continue
                
                # IMPORTANT: Pass mock data through same outlier detection pipeline as real data
                normalized = self._normalize_telemetry_data(mock)
                
                self.journal.append(normalized)
                
                try:
                    self.message_queue.put_nowait(normalized)
                except queue.Full:
                    try:
                        self.message_queue.get_nowait()
                        self.message_queue.put_nowait(normalized)
                        self.stats["messages_dropped"] += 1
                    except queue.Empty:
                        pass
                
                with self.db_buffer_lock:
                    self.db_buffer.append(normalized)
                
                self.stats["messages_received"] += 1
                self.stats["last_message_time"] = datetime.now(timezone.utc)
                await asyncio.sleep(MOCK_DATA_INTERVAL)
            except Exception as e:
                self._count_error(f"Mock loop error: {e}")

    # ------------- Republish with rate limiting -------------

    async def republish_messages(self):
        """
        Republish messages to dashboard with rate limiting.
        Uses token bucket algorithm to prevent Ably rate limit violations.
        """
        while self.running and not self.shutdown_event.is_set():
            try:
                # Check connection health
                if not self.dashboard_health.is_connected:
                    await self._reconnect_dashboard()
                    if not self.dashboard_health.is_connected:
                        await asyncio.sleep(1)
                        continue
                
                # First, drain any queued messages from rate limiter
                drained = await self.rate_limiter.drain_queue(
                    self.dashboard_channel, "telemetry_update"
                )
                if drained > 0:
                    self.stats["messages_republished"] += drained
                    self.dashboard_health.record_message()
                
                # Process new messages from main queue
                batch = []
                while not self.message_queue.empty() and len(batch) < 20:
                    try:
                        batch.append(self.message_queue.get_nowait())
                    except queue.Empty:
                        break
                
                for m in batch:
                    try:
                        # Use rate-limited publish
                        success = await self.rate_limiter.publish(
                            self.dashboard_channel, "telemetry_update", m
                        )
                        if success:
                            self.stats["messages_republished"] += 1
                            self.dashboard_health.record_message()
                    except Exception as e:
                        self._count_error(f"Republish failed: {e}")
                        self.dashboard_health.record_error()
                        self.dashboard_health.is_connected = False
                        # Queue message for retry via rate limiter
                        self.rate_limiter.queue_message(m)
                        break
                
                await asyncio.sleep(0.05)
            except Exception as e:
                self._count_error(f"Republish loop error: {e}")

    # ------------- Health check / watchdog -------------

    async def health_monitor(self):
        """Monitor connection health and trigger reconnects"""
        while self.running and not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                
                # Check ESP32 connection (real mode only)
                if not self.mock_mode:
                    if self.esp32_health.is_stale(WATCHDOG_TIMEOUT):
                        logger.warning(f"⚠️ ESP32 data stale for {WATCHDOG_TIMEOUT}s - triggering reconnect")
                        self.esp32_health.is_connected = False
                        await self._reconnect_esp32()
                    
                    # Check Ably connection state
                    if self.esp32_client and self.esp32_client.connection.state != "connected":
                        logger.warning(f"⚠️ ESP32 Ably state: {self.esp32_client.connection.state}")
                        self.esp32_health.is_connected = False
                
                # Check dashboard connection
                if self.dashboard_client and self.dashboard_client.connection.state != "connected":
                    logger.warning(f"⚠️ Dashboard Ably state: {self.dashboard_client.connection.state}")
                    self.dashboard_health.is_connected = False
                
            except Exception as e:
                self._count_error(f"Health monitor error: {e}")

    # ------------- DB writer & retry -------------

    async def database_batch_writer(self):
        next_retry_at = time.monotonic()
        while self.running and not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(DB_BATCH_INTERVAL)

                # Retry failed batches if it's time
                now_mono = time.monotonic()
                if self.db_retry_queue and now_mono >= next_retry_at:
                    retry_batches = list(self.db_retry_queue)
                    self.db_retry_queue.clear()
                    ok = await self._write_batches_to_database(retry_batches)
                    if not ok:
                        self.db_retry_backoff = min(
                            RETRY_BACKOFF_MAX, self.db_retry_backoff * 2
                        )
                    else:
                        self.db_retry_backoff = RETRY_BASE_BACKOFF
                    next_retry_at = time.monotonic() + self.db_retry_backoff

                # Flush new buffer
                with self.db_buffer_lock:
                    buffer_copy = list(self.db_buffer)
                    self.db_buffer.clear()
                if buffer_copy:
                    chunks = [
                        buffer_copy[i : i + MAX_BATCH_SIZE]
                        for i in range(0, len(buffer_copy), MAX_BATCH_SIZE)
                    ]
                    ok = await self._write_batches_to_database(chunks)
                    if not ok:
                        next_retry_at = time.monotonic() + self.db_retry_backoff

            except Exception as e:
                self._count_error(f"DB writer loop error: {e}")

    async def _write_batches_to_database(
        self, batches: List[List[Dict[str, Any]]]
    ) -> bool:
        if not self.supabase_client:
            self.db_retry_queue.extend(batches)
            self.db_write_failures += len(batches)
            return False

        all_ok = True
        for batch in batches:
            try:
                rows = []
                for r in batch:
                    rows.append(
                        {
                            "session_id": r["session_id"],
                            "session_name": r.get("session_name", self.session_name),
                            "timestamp": r["timestamp"],
                            "speed_ms": r.get("speed_ms"),
                            "voltage_v": r.get("voltage_v"),
                            "current_a": r.get("current_a"),
                            "power_w": r.get("power_w"),
                            "energy_j": r.get("energy_j"),
                            "distance_m": r.get("distance_m"),
                            "latitude": r.get("latitude"),
                            "longitude": r.get("longitude"),
                            "altitude_m": r.get("altitude"),
                            "gyro_x": r.get("gyro_x"),
                            "gyro_y": r.get("gyro_y"),
                            "gyro_z": r.get("gyro_z"),
                            "accel_x": r.get("accel_x"),
                            "accel_y": r.get("accel_y"),
                            "accel_z": r.get("accel_z"),
                            "total_acceleration": r.get("total_acceleration"),
                            "message_id": r.get("message_id"),
                            "uptime_seconds": r.get("uptime_seconds"),
                            "throttle_pct": r.get("throttle_pct"),
                            "brake_pct": r.get("brake_pct"),
                            "throttle": r.get("throttle"),
                            "brake": r.get("brake"),
                            "data_source": r.get("data_source"),
                            "outliers": json.dumps(r.get("outliers")) if r.get("outliers") else None,
                        }
                    )

                resp = (
                    self.supabase_client.table(SUPABASE_TABLE_NAME)
                    .insert(rows)
                    .execute()
                )
                if not resp.data:
                    raise RuntimeError("Supabase insert returned no data")

                self.stats["messages_stored_db"] += len(resp.data)
                self.stats["last_db_write_time"] = datetime.now(timezone.utc)

            except Exception as e:
                all_ok = False
                self.db_write_failures += 1
                self._count_error(f"DB write failed (batch {len(batch)}): {e}")
                self.db_retry_queue.append(batch)

        return all_ok

    # ------------- Stats -------------

    async def print_stats(self):
        while self.running and not self.shutdown_event.is_set():
            try:
                await asyncio.sleep(30)
                mode = f"MOCK/{self.mock_config.scenario.value}" if self.mock_mode else "REAL"
                buf_len = len(self.db_buffer)
                retry_batches = len(self.db_retry_queue)
                dropped = self.stats.get("messages_dropped", 0)
                reconnects = self.stats.get("reconnect_count", 0)
                
                # Get rate limiter stats
                rl_stats = self.rate_limiter.get_stats()
                
                logger.info(
                    f"📊 STATS ({mode}) - "
                    f"Received: {self.stats['messages_received']}, "
                    f"Republished: {self.stats['messages_republished']}, "
                    f"DB Stored: {self.stats['messages_stored_db']}, "
                    f"Dropped: {dropped}, "
                    f"Buffer: {buf_len}, RetryBatches: {retry_batches}, "
                    f"Reconnects: {reconnects}, "
                    f"Errors: {self.stats['errors']}"
                )
                
                # Log rate limiter stats if there's activity
                if rl_stats["burst_events"] > 0 or rl_stats["queue_depth"] > 0:
                    logger.info(
                        f"🚦 RATE LIMITER - "
                        f"QueueDepth: {rl_stats['queue_depth']}, "
                        f"BurstEvents: {rl_stats['burst_events']}, "
                        f"Delayed: {rl_stats['messages_delayed']}, "
                        f"Tokens: {rl_stats['available_tokens']}"
                    )
                
                if self.stats["last_error"]:
                    logger.info(f"🔍 Last Error: {self.stats['last_error']}")
            except Exception as e:
                self._count_error(f"Stats loop error: {e}")

    # ------------- Lifecycle -------------

    async def run(self):
        try:
            ok_db = await self.connect_supabase()
            ok_src = await self.connect_esp32_subscriber()
            ok_out = await self.connect_dashboard_publisher()
            if not ok_out or (not ok_src and not self.mock_mode):
                logger.error("❌ Required connections failed. Exiting.")
                return

            self.running = True
            logger.info(
                f"🚀 Bridge started (Session: {self.session_name} / {self.session_id[:8]})"
            )
            if self.mock_mode:
                logger.info(f"🎭 Simulation scenario: {self.mock_config.scenario.value}")

            tasks: List[asyncio.Task] = [
                asyncio.create_task(self.republish_messages(), name="republish"),
                asyncio.create_task(
                    self.database_batch_writer(), name="db_writer"
                ),
                asyncio.create_task(self.print_stats(), name="stats"),
                asyncio.create_task(self.health_monitor(), name="health"),
            ]
            if self.mock_mode:
                tasks.append(
                    asyncio.create_task(
                        self.generate_mock_data_loop(), name="mock_loop"
                    )
                )

            shutdown_wait = asyncio.create_task(
                self.shutdown_event.wait(), name="shutdown_wait"
            )

            done, pending = await asyncio.wait(
                tasks + [shutdown_wait],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # If shutdown_wait fired, stop loops
            self.running = False

            # Cancel any pending tasks gracefully
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        except Exception as e:
            self._count_error(f"Run error: {e}")
        finally:
            await self.cleanup()

    async def cleanup(self):
        try:
            logger.info("🧹 Cleaning up ...")

            # Flush leftover db_buffer
            with self.db_buffer_lock:
                pending = list(self.db_buffer)
                self.db_buffer.clear()
            if pending:
                chunks = [
                    pending[i : i + MAX_BATCH_SIZE]
                    for i in range(0, len(pending), MAX_BATCH_SIZE)
                ]
                logger.info(f"💾 Flushing final DB buffer ({len(pending)})")
                await self._write_batches_to_database(chunks)

            # If failures or pending retries, export full CSV for the session
            if self.db_write_failures > 0 or self.db_retry_queue:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_csv = os.path.join(
                    EXPORT_DIR, f"telemetry_{self.session_id}_{ts}.csv"
                )
                field_order = [
                    "session_id",
                    "session_name",
                    "timestamp",
                    "speed_ms",
                    "voltage_v",
                    "current_a",
                    "power_w",
                    "energy_j",
                    "distance_m",
                    "latitude",
                    "longitude",
                    "altitude",
                    "gyro_x",
                    "gyro_y",
                    "gyro_z",
                    "accel_x",
                    "accel_y",
                    "accel_z",
                    "total_acceleration",
                    "message_id",
                    "uptime_seconds",
                    "throttle_pct",
                    "brake_pct",
                    "throttle",
                    "brake",
                    "data_source",
                ]
                n = self.journal.export_csv(out_csv, field_order)
                logger.warning(
                    f"📤 Exported session CSV with {n} rows to {out_csv} "
                    f"(DB failures or pending retries)"
                )

            # Close Ably
            if self.esp32_client:
                try:
                    await self.esp32_client.close()
                except Exception:
                    pass
            if self.dashboard_client:
                try:
                    await self.dashboard_client.close()
                except Exception:
                    pass

            # Close journal
            self.journal.close()

            logger.info("✅ Cleanup done")
        except Exception as e:
            self._count_error(f"Cleanup error: {e}")

    # ------------- Helpers -------------

    def _count_error(self, msg: str):
        logger.error(f"❌ {msg}")
        self.stats["errors"] += 1
        self.stats["last_error"] = msg


# ------------------------------
# CLI
# ------------------------------

def get_user_preferences() -> tuple:
    print("\n" + "=" * 70)
    print("🚀 TELEMETRY BRIDGE WITH DATABASE")
    print("=" * 70)
    print("1. 🔗 REAL DATA (ESP32)")
    print("2. 🎭 MOCK DATA (simulated)")
    while True:
        choice = input("Enter your choice (1 or 2): ").strip()
        if choice == "1":
            mock_mode = False
            print("✅ Selected REAL DATA\n")
            break
        if choice == "2":
            mock_mode = True
            print("✅ Selected MOCK DATA\n")
            break
        print("❌ Invalid choice.")

    mock_config = MockModeConfig()
    
    if mock_mode:
        print("-" * 50)
        print("📊 MOCK SIMULATION SCENARIOS:")
        print("-" * 50)
        print("1. 🟢 NORMAL     - Realistic smooth simulation")
        print("2. ⚠️  SENSOR    - Simulate sensor failures/dropouts")
        print("3. ⏸️  STALLS    - Simulate data stream pauses")
        print("4. 📡 INTERMIT  - Simulate intermittent connection")
        print("5. 🛰️  GPS       - Simulate GPS signal issues")
        print("6. 💥 CHAOS     - All problems combined!")
        print("-" * 50)
        
        scenario_map = {
            "1": MockScenario.NORMAL,
            "2": MockScenario.SENSOR_FAILURES,
            "3": MockScenario.DATA_STALLS,
            "4": MockScenario.INTERMITTENT,
            "5": MockScenario.GPS_ISSUES,
            "6": MockScenario.CHAOS,
        }
        
        while True:
            scenario_choice = input("Select scenario (1-6) [default=1]: ").strip() or "1"
            if scenario_choice in scenario_map:
                mock_config = MockModeConfig.from_scenario(scenario_map[scenario_choice])
                print(f"✅ Selected: {mock_config.scenario.value.upper()}\n")
                break
            print("❌ Invalid choice. Enter 1-6.")

    session_name = input("Enter a session name (label): ").strip()
    if not session_name:
        scenario_tag = f"_{mock_config.scenario.value}" if mock_mode else ""
        session_name = f"{'M' if mock_mode else ''}Session{scenario_tag}_{str(uuid.uuid4())[:8]}"
    if mock_mode and not session_name.startswith("M "):
        session_name = "M " + session_name
    print(f"📝 Session name: {session_name}\n")
    
    return mock_mode, session_name, mock_config


async def main():
    try:
        mock_mode, session_name, mock_config = get_user_preferences()
        bridge = TelemetryBridgeWithDB(
            mock_mode=mock_mode, 
            session_name=session_name,
            mock_config=mock_config
        )
        await bridge.run()
    except KeyboardInterrupt:
        logger.info("🛑 Stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
    finally:
        logger.info("🏁 Exited")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Interrupted")
    except Exception as e:
        logger.error(f"❌ Application error: {e}")
