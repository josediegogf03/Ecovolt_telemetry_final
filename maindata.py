import asyncio
import json
import logging
import math
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
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

# Configuration for ESP32 MQTT source (from Transmiter.cpp)
ESP32_ABLY_API_KEY = "ja_fwQ.K6CTEw:F-aWFMdJXPCv9MvxhYztCGna3XdRJZVgA0qm9pMfDOQ"
ESP32_CHANNEL_NAME = "EcoTele"

# Configuration for Dashboard output (existing configuration)
DASHBOARD_ABLY_API_KEY = "DxuYSw.fQHpug:sa4tOcqWDkYBW9ht56s7fT0G091R1fyXQc6mc8WthxQ"
DASHBOARD_CHANNEL_NAME = "telemetry-dashboard-channel"

# Supabase configuration
SUPABASE_URL = "https://rtfqlrvbmsnrkrdrknsp.supabase.co"
SUPABASE_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJ0ZnFscnZibXNucmtyZHJrbnNwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTIxMjA3NjQsImV4cCI6MjA2NzY5Njc2NH0.bz23OIWDRPJXgEvJdEof21UVDSi572IfcAdZS2C4gTU"
SUPABASE_TABLE_NAME = "telemetry"

# Mock data configuration
MOCK_DATA_INTERVAL = 2.0  # seconds
DB_BATCH_INTERVAL = 9.0   # seconds - send to database every 9 seconds
MAX_BATCH_SIZE = 100      # maximum number of records to batch before forcing write

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


class TelemetryBridgeWithDB:
    """
    Bridge class that:
    1. Subscribes to ESP32 MQTT telemetry data via Ably OR generates mock data
    2. Republishes to dashboard channel for real-time updates
    3. Batches and stores data in Supabase database every 9 seconds
    4. Manages sessions for historical data retrieval
    """

    def __init__(self, mock_mode: bool = False, session_name: Optional[str] = None):
        self.mock_mode = mock_mode
        self.esp32_client = None
        self.dashboard_client = None
        self.supabase_client = None
        self.esp32_channel = None
        self.dashboard_channel = None
        self.running = False
        self.message_queue = queue.Queue()
        self.db_buffer = []
        self.db_buffer_lock = threading.Lock()
        self.session_id = str(uuid.uuid4())
        self.session_start_time = datetime.now(timezone.utc)

        # Session name (user supplied). If None, use a generated default.
        if session_name and session_name.strip():
            self.session_name = session_name.strip()
        else:
            # sensible default if user provided empty name
            short_id = self.session_id[:8]
            self.session_name = f"Session {short_id}"

        # Statistics
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

        # Binary message parsing (updated for new structure)
        self.BINARY_FORMAT = "<ffffffI"  # speed, voltage, current, lat, lon,
        # altitude, message_id
        self.BINARY_FIELD_NAMES = [
            'speed_ms',
            'voltage_v',
            'current_a',
            'latitude',
            'longitude',
            'altitude',
            'message_id',
        ]
        self.BINARY_MESSAGE_SIZE = struct.calcsize(self.BINARY_FORMAT)

        # Mock data simulation state
        self.cumulative_distance = 0.0
        self.cumulative_energy = 0.0
        self.simulation_time = 0
        self.prev_speed = 0.0
        self.message_count = 0
        self.base_altitude = 100.0  # Base altitude for simulation

        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info(f"🆔 New session started: {self.session_id}")
        logger.info(f"📝 Session name: {self.session_name}")

        if self.mock_mode:
            logger.info("🎭 MOCK MODE ENABLED - Will generate simulated telemetry data")
        else:
            logger.info("🔗 REAL MODE ENABLED - Will connect to ESP32 for real telemetry data")

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        self.running = False

    async def connect_supabase(self) -> bool:
        """Initialize Supabase client connection"""
        try:
            self.supabase_client = create_client(SUPABASE_URL, SUPABASE_API_KEY)
            logger.info("✅ Connected to Supabase database")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to connect to Supabase: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)
            return False

    async def connect_esp32_subscriber(self) -> bool:
        """Connect to ESP32 MQTT source via Ably"""
        if self.mock_mode:
            return True
        try:
            self.esp32_client = AblyRealtime(ESP32_ABLY_API_KEY)
            await self._wait_for_connection(self.esp32_client, "ESP32")
            self.esp32_channel = self.esp32_client.channels.get(ESP32_CHANNEL_NAME)
            await self.esp32_channel.subscribe(self._on_esp32_message_received)
            logger.info(f"✅ Connected to ESP32 channel: {ESP32_CHANNEL_NAME}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to connect to ESP32 source: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)
            return False

    async def connect_dashboard_publisher(self) -> bool:
        """Connect to dashboard output channel via Ably"""
        try:
            self.dashboard_client = AblyRealtime(DASHBOARD_ABLY_API_KEY)
            await self._wait_for_connection(self.dashboard_client, "Dashboard")
            self.dashboard_channel = self.dashboard_client.channels.get(
                DASHBOARD_CHANNEL_NAME
            )
            logger.info(f"✅ Connected to Dashboard channel: {DASHBOARD_CHANNEL_NAME}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to connect to Dashboard output: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)
            return False

    async def _wait_for_connection(self, client, name: str, timeout=10):
        """Wait for Ably client to connect"""
        logger.info(f"Waiting for {name} connection...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            if client.connection.state == 'connected':
                logger.info(f"✅ {name} connection established")
                return
            await asyncio.sleep(0.1)
        logger.info(f"⏰ {name} connection timeout, assuming connected")

    def generate_mock_telemetry_data(self) -> Dict[str, Any]:
        """Generate realistic mock telemetry data"""
        current_time = datetime.now(timezone.utc)

        # Generate realistic vehicle dynamics
        base_speed = 15.0 + 5.0 * math.sin(self.simulation_time * 0.1)
        speed_variation = random.gauss(0, 1.5)
        speed = max(0, min(25, base_speed + speed_variation))

        # Electrical system simulation
        voltage = max(40, min(55, 48.0 + random.gauss(0, 1.5)))
        current = max(0, min(15, 8.0 + speed * 0.2 + random.gauss(0, 1.0)))
        power = voltage * current

        # Energy and distance integration
        energy_delta = power * MOCK_DATA_INTERVAL
        distance_delta = speed * MOCK_DATA_INTERVAL
        self.cumulative_energy += energy_delta
        self.cumulative_distance += distance_delta

        # GPS simulation (circular track with altitude variation)
        base_lat, base_lon = 40.7128, -74.0060
        lat_offset = 0.001 * math.sin(self.simulation_time * 0.05)
        lon_offset = 0.001 * math.cos(self.simulation_time * 0.05)
        latitude = base_lat + lat_offset + random.gauss(0, 0.0001)
        longitude = base_lon + lon_offset + random.gauss(0, 0.0001)

        # Altitude simulation (varies with track elevation)
        altitude_variation = 10.0 * math.sin(self.simulation_time * 0.03)
        altitude = self.base_altitude + altitude_variation + random.gauss(0, 1.0)

        # IMU simulation
        turning_rate = 2.0 * math.sin(self.simulation_time * 0.08)
        gyro_x = random.gauss(0, 0.5)
        gyro_y = random.gauss(0, 0.3)
        gyro_z = turning_rate + random.gauss(0, 0.8)

        # Acceleration simulation
        speed_acceleration = (speed - self.prev_speed) / MOCK_DATA_INTERVAL
        self.prev_speed = speed

        accel_x = speed_acceleration + random.gauss(0, 0.2)
        accel_y = turning_rate * speed * 0.1 + random.gauss(0, 0.1)
        accel_z = 9.81 + random.gauss(0, 0.05)

        # Add vibration effects
        vibration_factor = speed * 0.02
        accel_x += random.gauss(0, vibration_factor)
        accel_y += random.gauss(0, vibration_factor)
        accel_z += random.gauss(0, vibration_factor)

        # Calculate total acceleration
        total_acceleration = math.sqrt(accel_x**2 + accel_y**2 + accel_z**2)

        self.simulation_time += 1
        self.message_count += 1

        return {
            'timestamp': current_time.isoformat(),
            'speed_ms': round(speed, 2),
            'voltage_v': round(voltage, 2),
            'current_a': round(current, 2),
            'power_w': round(power, 2),
            'energy_j': round(self.cumulative_energy, 2),
            'distance_m': round(self.cumulative_distance, 2),
            'latitude': round(latitude, 6),
            'longitude': round(longitude, 6),
            'altitude': round(altitude, 2),
            'gyro_x': round(gyro_x, 3),
            'gyro_y': round(gyro_y, 3),
            'gyro_z': round(gyro_z, 3),
            'accel_x': round(accel_x, 3),
            'accel_y': round(accel_y, 3),
            'accel_z': round(accel_z, 3),
            'total_acceleration': round(total_acceleration, 3),
            'message_id': self.message_count,
            'uptime_seconds': self.simulation_time * MOCK_DATA_INTERVAL,
            'data_source': 'MOCK_GENERATOR',
            'session_id': self.session_id,
            'session_name': self.session_name,
        }

    def _parse_json_message(self, data_bytes: bytes) -> Optional[Dict]:
        """Parse JSON message from bytes"""
        try:
            if isinstance(data_bytes, (bytes, bytearray)):
                data_str = data_bytes.decode('utf-8')
            else:
                data_str = str(data_bytes)

            parsed_data = json.loads(data_str)
            logger.debug(f"✅ Successfully parsed JSON message with "
                         f"{len(parsed_data)} fields")
            return parsed_data
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"JSON parsing failed: {e}")
            return None

    def _parse_binary_message(self, data_bytes: bytes) -> Optional[Dict]:
        """Parse binary message from bytes"""
        if len(data_bytes) != self.BINARY_MESSAGE_SIZE:
            logger.debug(
                f"Binary message size mismatch. Expected "
                f"{self.BINARY_MESSAGE_SIZE}, got {len(data_bytes)}"
            )
            return None

        try:
            unpacked_values = struct.unpack(self.BINARY_FORMAT, data_bytes)
            data_dict = dict(zip(self.BINARY_FIELD_NAMES, unpacked_values))
            data_dict['power_w'] = data_dict['voltage_v'] * data_dict['current_a']
            logger.debug(f"✅ Successfully parsed binary message")
            return data_dict
        except struct.error as e:
            logger.debug(f"Binary parsing failed: {e}")
            return None

    def _normalize_telemetry_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize and enhance telemetry data for consistent format"""
        normalized = data.copy()

        # Add session information (user-supplied name + uuid)
        normalized['session_id'] = self.session_id
        normalized['session_name'] = self.session_name

        # Ensure we have a current timestamp
        if 'timestamp' not in normalized or normalized['timestamp'].startswith(
            '1970-01-01'
        ):
            normalized['timestamp'] = datetime.now(timezone.utc).isoformat()

        # Ensure timestamp is properly formatted for database
        if isinstance(normalized['timestamp'], str):
            try:
                dt = datetime.fromisoformat(normalized['timestamp'].replace('Z',
                                                                           '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                normalized['timestamp'] = dt.isoformat()
            except ValueError:
                normalized['timestamp'] = datetime.now(timezone.utc).isoformat()

        # Ensure all required fields are present with reasonable defaults
        required_fields = {
            'speed_ms': 0.0,
            'voltage_v': 0.0,
            'current_a': 0.0,
            'power_w': 0.0,
            'energy_j': 0.0,
            'distance_m': 0.0,
            'latitude': 0.0,
            'longitude': 0.0,
            'altitude': 0.0,
            'gyro_x': 0.0,
            'gyro_y': 0.0,
            'gyro_z': 0.0,
            'accel_x': 0.0,
            'accel_y': 0.0,
            'accel_z': 0.0,
            'total_acceleration': 0.0,
            'message_id': 0,
            'uptime_seconds': 0.0
        }

        for field, default_value in required_fields.items():
            if field not in normalized:
                normalized[field] = default_value

        # Calculate power if not present
        if 'power_w' not in normalized or normalized['power_w'] == 0:
            normalized['power_w'] = normalized['voltage_v'] * normalized['current_a']

        # Calculate total acceleration if not present
        if 'total_acceleration' not in normalized or normalized[
            'total_acceleration'
        ] == 0:
            normalized['total_acceleration'] = math.sqrt(
                normalized['accel_x']**2 +
                normalized['accel_y']**2 +
                normalized['accel_z']**2
            )

        # Add data source tag
        normalized['data_source'] = 'ESP32_REAL' if not self.mock_mode else 'MOCK_GENERATOR'

        return normalized

    def _on_esp32_message_received(self, message):
        """Handle incoming messages from ESP32 with improved parsing"""
        try:
            logger.debug(f"📨 Received message from ESP32 - Type: {type(message.data)}")

            data = None

            # Try to parse the message data
            if isinstance(message.data, (bytes, bytearray)):
                # Try JSON first (most common case)
                data = self._parse_json_message(message.data)
                if data is None:
                    # If JSON fails, try binary parsing
                    data = self._parse_binary_message(message.data)
            elif isinstance(message.data, str):
                try:
                    data = json.loads(message.data)
                except json.JSONDecodeError:
                    logger.error("❌ Failed to parse string as JSON")
            elif isinstance(message.data, dict):
                data = message.data
            else:
                logger.warning(f"⚠️ Unhandled message data type: {type(message.data)}")
                return

            if data is None:
                logger.error("❌ Failed to parse message in any known format")
                self.stats["errors"] += 1
                self.stats["last_error"] = "Failed to parse message"
                return

            # Normalize the data (this will attach session_name/session_id)
            normalized_data = self._normalize_telemetry_data(data)

            # Add to message queue for real-time republishing
            self.message_queue.put(normalized_data)

            # Add to database buffer
            with self.db_buffer_lock:
                self.db_buffer.append(normalized_data)

            self.stats["messages_received"] += 1
            self.stats["last_message_time"] = datetime.now(timezone.utc)

            logger.debug(
                f"📊 ESP32 Data Processed - Speed: {normalized_data.get('speed_ms', 0):.2f} "
                f"m/s, Power: {normalized_data.get('power_w', 0):.2f} W, "
                f"Altitude: {normalized_data.get('altitude', 0):.2f} m"
            )

        except Exception as e:
            logger.error(f"❌ Error handling ESP32 message: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)

    async def generate_mock_data_loop(self):
        """Generate mock data at regular intervals"""
        if not self.mock_mode:
            return

        while self.running:
            try:
                mock_data = self.generate_mock_telemetry_data()

                # Add to message queue for real-time republishing
                self.message_queue.put(mock_data)

                # Add to database buffer
                with self.db_buffer_lock:
                    self.db_buffer.append(mock_data)

                self.stats["messages_received"] += 1
                self.stats["last_message_time"] = datetime.now(timezone.utc)

                await asyncio.sleep(MOCK_DATA_INTERVAL)

            except Exception as e:
                logger.error(f"❌ Error generating mock data: {e}")
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)

    async def republish_messages(self):
        """Republish messages to dashboard channel for real-time updates"""
        while self.running:
            try:
                if not self.message_queue.empty():
                    messages_to_publish = []
                    while not self.message_queue.empty() and len(
                        messages_to_publish
                    ) < 10:
                        messages_to_publish.append(self.message_queue.get_nowait())

                    for message_data in messages_to_publish:
                        try:
                            await self.dashboard_channel.publish(
                                'telemetry_update', message_data
                            )
                            self.stats["messages_republished"] += 1
                        except Exception as e:
                            logger.error(f"❌ Failed to republish message: {e}")
                            self.stats["errors"] += 1
                            self.stats["last_error"] = str(e)

                    if messages_to_publish:
                        source_info = "MOCK" if self.mock_mode else "ESP32"
                        logger.debug(f"📡 Republished {len(messages_to_publish)} "
                                     f"{source_info} messages")

                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"❌ Error in republish loop: {e}")
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)

    async def database_batch_writer(self):
        """Write batched data to Supabase database every 9 seconds"""
        while self.running:
            try:
                await asyncio.sleep(DB_BATCH_INTERVAL)

                # Get current batch of data
                batch_data = []
                with self.db_buffer_lock:
                    if self.db_buffer:
                        batch_data = self.db_buffer.copy()
                        self.db_buffer.clear()

                # Write batch to database if we have data
                if batch_data:
                    await self._write_batch_to_database(batch_data)

            except Exception as e:
                logger.error(f"❌ Error in database batch writer: {e}")
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)

    async def _write_batch_to_database(self, batch_data: List[Dict[str, Any]]):
        """Write a batch of telemetry data to Supabase database"""
        try:
            if not self.supabase_client or not batch_data:
                return

            # Prepare data for database insertion
            db_records = []
            for record in batch_data:
                db_record = {
                    'session_id': record['session_id'],
                    'session_name': record.get('session_name', self.session_name),
                    'timestamp': record['timestamp'],
                    'speed_ms': record['speed_ms'],
                    'voltage_v': record['voltage_v'],
                    'current_a': record['current_a'],
                    'power_w': record['power_w'],
                    'energy_j': record['energy_j'],
                    'distance_m': record['distance_m'],
                    'latitude': record['latitude'],
                    'longitude': record['longitude'],
                    'altitude_m': record['altitude'],
                    'gyro_x': record['gyro_x'],
                    'gyro_y': record['gyro_y'],
                    'gyro_z': record['gyro_z'],
                    'accel_x': record['accel_x'],
                    'accel_y': record['accel_y'],
                    'accel_z': record['accel_z'],
                    'total_acceleration': record['total_acceleration'],
                    'message_id': record['message_id'],
                    'uptime_seconds': record['uptime_seconds'],
                }
                db_records.append(db_record)

            # Insert batch into database
            response = self.supabase_client.table(SUPABASE_TABLE_NAME).insert(
                db_records
            ).execute()

            if response.data:
                records_written = len(response.data)
                self.stats["messages_stored_db"] += records_written
                self.stats["last_db_write_time"] = datetime.now(timezone.utc)

                logger.info(
                    f"💾 Wrote {records_written} records to database "
                    f"(Session: {self.session_name} / {self.session_id[:8]}...)"
                )
            else:
                logger.warning("⚠️ Database write returned no data")

        except Exception as e:
            logger.error(f"❌ Failed to write batch to database: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = f"Database write error: {str(e)}"

    async def print_stats(self):
        """Print periodic statistics"""
        while self.running:
            try:
                await asyncio.sleep(30)

                mode_info = "MOCK DATA" if self.mock_mode else "ESP32 DATA"
                buffer_size = len(self.db_buffer)

                logger.info(
                    f"📊 STATS ({mode_info}) - "
                    f"Received: {self.stats['messages_received']}, "
                    f"Republished: {self.stats['messages_republished']}, "
                    f"DB Stored: {self.stats['messages_stored_db']}, "
                    f"Buffer: {buffer_size}, "
                    f"Errors: {self.stats['errors']}, "
                    f"Session: {self.session_name} ({self.session_id[:8]}...)"
                )

                if self.stats['last_error']:
                    logger.info(f"🔍 Last Error: {self.stats['last_error']}")

            except Exception as e:
                logger.error(f"❌ Error in stats loop: {e}")

    async def run(self):
        """Main run loop for the telemetry bridge"""
        try:
            # Connect to Supabase first
            if not await self.connect_supabase():
                logger.error("❌ Failed to connect to Supabase, exiting")
                return

            # Connect to ESP32 source (or skip if mock mode)
            if not await self.connect_esp32_subscriber() and not self.mock_mode:
                logger.error("❌ Failed to connect to ESP32, exiting")
                return

            # Connect to dashboard output
            if not await self.connect_dashboard_publisher():
                logger.error("❌ Failed to connect to dashboard, exiting")
                return

            self.running = True
            logger.info(
                f"🚀 Telemetry bridge started successfully "
                f"(Session: {self.session_name} / {self.session_id[:8]}...)"
            )

            # Start all async tasks
            tasks = [
                self.republish_messages(),
                self.database_batch_writer(),
                self.print_stats()
            ]

            # Add mock data generation if in mock mode
            if self.mock_mode:
                tasks.append(self.generate_mock_data_loop())

            # Run all tasks concurrently
            await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"❌ Fatal error in main run loop: {e}")
            self.stats["errors"] += 1
            self.stats["last_error"] = str(e)
        finally:
            await self.cleanup()

    async def cleanup(self):
        """Cleanup resources and connections"""
        try:
            logger.info("🧹 Cleaning up...")

            # Write any remaining buffered data to database
            with self.db_buffer_lock:
                remaining = self.db_buffer.copy()
                self.db_buffer.clear()

            if remaining:
                logger.info(f"💾 Writing final batch of {len(remaining)} records to database")
                await self._write_batch_to_database(remaining)

            # Close Ably connections
            if self.esp32_client:
                await self.esp32_client.close()
            if self.dashboard_client:
                await self.dashboard_client.close()

            # Supabase client doesn't need explicit cleanup

            logger.info("✅ Cleanup completed")

        except Exception as e:
            logger.error(f"❌ Error during cleanup: {e}")


def get_user_preferences() -> (bool, str):
    """Get user preferences through terminal input and ask for session name.

    Returns:
        (mock_mode, session_name)
    """
    print("\n" + "=" * 70)
    print("🚀 TELEMETRY BRIDGE WITH DATABASE - ESP32 to Dashboard & Supabase")
    print("=" * 70)
    print()
    print("This bridge will:")
    print("  • Connect to ESP32 telemetry OR generate mock data")
    print("  • Republish to dashboard for real-time updates")
    print("  • Store data in Supabase database every 9 seconds")
    print("  • Manage sessions for historical data retrieval")
    print()
    print("Choose data source mode:")
    print("1. 🔗 REAL DATA - Connect to ESP32 hardware")
    print("2. 🎭 MOCK DATA - Generate simulated telemetry data")
    print()

    while True:
        choice = input("Enter your choice (1 or 2): ").strip()
        if choice == '1':
            mock_mode = False
            print("\n✅ Selected: REAL DATA mode")
            break
        elif choice == '2':
            mock_mode = True
            print("\n✅ Selected: MOCK DATA mode")
            break
        else:
            print("❌ Invalid choice. Please enter 1 or 2.")

    # Ask for a user-friendly session name (not the session UUID)
    print()
    if mock_mode:
        prompt = ("Enter a session name (label) for this MOCK session "
                  "(e.g. 'Practice session 2'):\n> ")
    else:
        prompt = ("Enter a session name (label) for this REAL session "
                  "(e.g. 'Route A - Test 2025-08-18'):\n> ")

    session_name = input(prompt).strip()

    # If blank, build a default name
    if not session_name:
        generated = str(uuid.uuid4())[:8]
        session_name = f"{'M ' if mock_mode else ''}Session {generated}"

    # For mock sessions, ensure leading "M " prefix
    if mock_mode and not session_name.startswith("M "):
        session_name = "M " + session_name

    print(f"\n📝 Session name set to: {session_name}")
    return mock_mode, session_name


async def main():
    """Main application entry point"""
    try:
        mock_mode, session_name = get_user_preferences()

        print("\n" + "-" * 70)
        print("🔧 STARTING TELEMETRY BRIDGE WITH DATABASE...")
        print("-" * 70)

        bridge = TelemetryBridgeWithDB(mock_mode=mock_mode,
                                      session_name=session_name)
        await bridge.run()

    except KeyboardInterrupt:
        logger.info("🛑 Bridge stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
    finally:
        logger.info("🏁 Application terminated")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bridge interrupted")
    except Exception as e:
        logger.error(f"❌ Application error: {e}")
