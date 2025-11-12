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
from datetime import datetime, timezone
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
MOCK_DATA_INTERVAL = .2  # seconds
DB_BATCH_INTERVAL = 9.0  # seconds
MAX_BATCH_SIZE = 200  # records per insert
RETRY_BASE_BACKOFF = 3.0  # seconds
RETRY_BACKOFF_MAX = 60.0  # seconds

# Durability paths
SPOOL_DIR = "./spool"
EXPORT_DIR = "./export"

# Logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("TelemetryBridge")


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
    """

    def __init__(self, mock_mode: bool = False, session_name: Optional[str] = None):
        self.mock_mode = mock_mode
        self.esp32_client: Optional[AblyRealtime] = None
        self.dashboard_client: Optional[AblyRealtime] = None
        self.supabase_client: Optional[Client] = None
        self.esp32_channel = None
        self.dashboard_channel = None

        self.running = False
        self.shutdown_event = asyncio.Event()

        self.message_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
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

        self.stats = {
            "messages_received": 0,
            "messages_republished": 0,
            "messages_stored_db": 0,
            "last_message_time": None,
            "last_db_write_time": None,
            "errors": 0,
            "last_error": None,
            "current_session_id": self.session_id,
            "current_session_name": self.session_name,
            "session_start_time": self.session_start_time.isoformat(),
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

        # Mock sim state
        self.cumulative_distance = 0.0
        self.cumulative_energy = 0.0
        self.simulation_time = 0
        self.prev_speed = 0.0
        self.message_count = 0
        self.base_altitude = 100.0

        # Signals
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(f"🆔 New session: {self.session_id}")
        logger.info(f"📝 Session name: {self.session_name}")
        logger.info("🎭 MOCK MODE ENABLED" if self.mock_mode else "🔗 REAL MODE ENABLED")

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.running = False
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self.shutdown_event.set)
        except Exception:
            pass

    # ------------- Connections -------------

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
            await self._wait_for_connection(self.esp32_client, "ESP32")
            self.esp32_channel = self.esp32_client.channels.get(ESP32_CHANNEL_NAME)
            await self.esp32_channel.subscribe(self._on_esp32_message_received)
            logger.info(f"✅ Subscribed to ESP32 channel: {ESP32_CHANNEL_NAME}")
            return True
        except Exception as e:
            self._count_error(f"ESP32 connect failed: {e}")
            return False

    async def connect_dashboard_publisher(self) -> bool:
        try:
            self.dashboard_client = AblyRealtime(DASHBOARD_ABLY_API_KEY)
            await self._wait_for_connection(self.dashboard_client, "Dashboard")
            self.dashboard_channel = self.dashboard_client.channels.get(
                DASHBOARD_CHANNEL_NAME
            )
            logger.info(f"✅ Connected to dashboard channel: {DASHBOARD_CHANNEL_NAME}")
            return True
        except Exception as e:
            self._count_error(f"Dashboard connect failed: {e}")
            return False

    async def _wait_for_connection(self, client, name: str, timeout=10):
        logger.info(f"Waiting for {name} connection...")
        start = time.time()
        while time.time() - start < timeout:
            if client.connection.state == "connected":
                logger.info(f"✅ {name} connected")
                return
            await asyncio.sleep(0.1)
        logger.warning(f"⏰ {name} connect timeout; continuing")

    # ------------- Mock generation -------------

    def generate_mock_telemetry_data(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)

        base_speed = 15.0 + 5.0 * math.sin(self.simulation_time * 0.1)
        speed_variation = random.gauss(0, 1.4)
        speed = max(0, min(25, base_speed + speed_variation))

        voltage = max(40, min(55, 48.0 + random.gauss(0, 1.4)))
        current = max(0, min(15, 7.5 + speed * 0.2 + random.gauss(0, 0.9)))
        power = voltage * current

        energy_delta = power * MOCK_DATA_INTERVAL
        distance_delta = speed * MOCK_DATA_INTERVAL
        self.cumulative_energy += energy_delta
        self.cumulative_distance += distance_delta

        base_lat, base_lon = 40.7128, -74.0060
        lat_offset = 0.001 * math.sin(self.simulation_time * 0.05)
        lon_offset = 0.001 * math.cos(self.simulation_time * 0.05)
        latitude = base_lat + lat_offset + random.gauss(0, 0.0001)
        longitude = base_lon + lon_offset + random.gauss(0, 0.0001)

        altitude_variation = 10.0 * math.sin(self.simulation_time * 0.03)
        altitude = self.base_altitude + altitude_variation + random.gauss(0, 1.0)

        turning_rate = 2.0 * math.sin(self.simulation_time * 0.08)
        gyro_x = random.gauss(0, 0.5)
        gyro_y = random.gauss(0, 0.3)
        gyro_z = turning_rate + random.gauss(0, 0.8)

        speed_acc = (speed - self.prev_speed) / MOCK_DATA_INTERVAL
        self.prev_speed = speed
        accel_x = speed_acc + random.gauss(0, 0.2)
        accel_y = turning_rate * speed * 0.1 + random.gauss(0, 0.1)
        accel_z = 9.81 + random.gauss(0, 0.05)
        vib = speed * 0.02
        accel_x += random.gauss(0, vib)
        accel_y += random.gauss(0, vib)
        accel_z += random.gauss(0, vib)
        total_acc = math.sqrt(accel_x**2 + accel_y**2 + accel_z**2)

        # Driver inputs (mock)
        phase = (math.sin(self.simulation_time * 0.06) + 1) / 2  # 0..1
        th_base = 20 + 70 * phase  # 20..90%
        brake_event = (self.simulation_time % 120) in range(0, 12) or random.random() < 0.03
        if brake_event:
            brake_pct = min(100.0, max(15.0, 60 + random.gauss(0, 15)))
            throttle_pct = max(0.0, th_base - brake_pct * 0.6)
        else:
            brake_pct = max(0.0, random.gauss(2, 1))
            throttle_pct = min(100.0, max(5.0, th_base + random.gauss(0, 5)))

        self.simulation_time += 1
        self.message_count += 1

        return {
            "timestamp": now.isoformat(),
            "speed_ms": round(speed, 2),
            "voltage_v": round(voltage, 2),
            "current_a": round(current, 2),
            "power_w": round(power, 2),
            "energy_j": round(self.cumulative_energy, 2),
            "distance_m": round(self.cumulative_distance, 2),
            "latitude": round(latitude, 6),
            "longitude": round(longitude, 6),
            "altitude": round(altitude, 2),
            "gyro_x": round(gyro_x, 3),
            "gyro_y": round(gyro_y, 3),
            "gyro_z": round(gyro_z, 3),
            "accel_x": round(accel_x, 3),
            "accel_y": round(accel_y, 3),
            "accel_z": round(accel_z, 3),
            "total_acceleration": round(total_acc, 3),
            "message_id": self.message_count,
            "uptime_seconds": self.simulation_time * MOCK_DATA_INTERVAL,
            "data_source": "MOCK_GENERATOR",
            "session_id": self.session_id,
            "session_name": self.session_name,
            "throttle_pct": round(throttle_pct, 1),
            "brake_pct": round(brake_pct, 1),
            "throttle": round(throttle_pct / 100.0, 3),
            "brake": round(brake_pct / 100.0, 3),
        }

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

            normalized = self._normalize_telemetry_data(data)

            # 1) durable journal
            self.journal.append(normalized)

            # 2) realtime + db buffer
            self.message_queue.put(normalized)
            with self.db_buffer_lock:
                self.db_buffer.append(normalized)

            self.stats["messages_received"] += 1
            self.stats["last_message_time"] = datetime.now(timezone.utc)

        except Exception as e:
            self._count_error(f"ESP32 handler error: {e}")

    # ------------- Mock loop -------------

    async def generate_mock_data_loop(self):
        if not self.mock_mode:
            return
        while self.running and not self.shutdown_event.is_set():
            try:
                mock = self.generate_mock_telemetry_data()
                self.journal.append(mock)
                self.message_queue.put(mock)
                with self.db_buffer_lock:
                    self.db_buffer.append(mock)
                self.stats["messages_received"] += 1
                self.stats["last_message_time"] = datetime.now(timezone.utc)
                await asyncio.sleep(MOCK_DATA_INTERVAL)
            except Exception as e:
                self._count_error(f"Mock loop error: {e}")

    # ------------- Republish -------------

    async def republish_messages(self):
        while self.running and not self.shutdown_event.is_set():
            try:
                batch = []
                while not self.message_queue.empty() and len(batch) < 20:
                    batch.append(self.message_queue.get_nowait())
                for m in batch:
                    try:
                        await self.dashboard_channel.publish("telemetry_update", m)
                        self.stats["messages_republished"] += 1
                    except Exception as e:
                        self._count_error(f"Republish failed: {e}")
                await asyncio.sleep(0.05)
            except Exception as e:
                self._count_error(f"Republish loop error: {e}")

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
                mode = "MOCK" if self.mock_mode else "REAL"
                buf_len = len(self.db_buffer)
                retry_batches = len(self.db_retry_queue)
                logger.info(
                    f"📊 STATS ({mode}) - "
                    f"Received: {self.stats['messages_received']}, "
                    f"Republished: {self.stats['messages_republished']}, "
                    f"DB Stored: {self.stats['messages_stored_db']}, "
                    f"Buffer: {buf_len}, RetryBatches: {retry_batches}, "
                    f"Errors: {self.stats['errors']}"
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

            tasks: List[asyncio.Task] = [
                asyncio.create_task(self.republish_messages(), name="republish"),
                asyncio.create_task(
                    self.database_batch_writer(), name="db_writer"
                ),
                asyncio.create_task(self.print_stats(), name="stats"),
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
                await self.esp32_client.close()
            if self.dashboard_client:
                await self.dashboard_client.close()

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

def get_user_preferences() -> (bool, str):
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

    session_name = input("Enter a session name (label): ").strip()
    if not session_name:
        session_name = f"{'M ' if mock_mode else ''}Session {str(uuid.uuid4())[:8]}"
    if mock_mode and not session_name.startswith("M "):
        session_name = "M " + session_name
    print(f"📝 Session name: {session_name}\n")
    return mock_mode, session_name


async def main():
    try:
        mock_mode, session_name = get_user_preferences()
        bridge = TelemetryBridgeWithDB(
            mock_mode=mock_mode, session_name=session_name
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
