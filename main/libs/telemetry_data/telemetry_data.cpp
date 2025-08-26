#include "telemetry_data.h"
#include <cmath>

std::unique_ptr<cJSON, decltype(&cJSON_Delete)> TelemetryData::toJSON() const {
    auto json = std::unique_ptr<cJSON, decltype(&cJSON_Delete)>(cJSON_CreateObject(), cJSON_Delete);
    
    cJSON_AddStringToObject(json.get(), "timestamp", data.timestamp.c_str());
    cJSON_AddNumberToObject(json.get(), "speed_ms", std::round(data.speed_ms * 100) / 100);
    cJSON_AddNumberToObject(json.get(), "voltage_v", std::round(data.voltage_v * 100) / 100);
    cJSON_AddNumberToObject(json.get(), "current_a", std::round(data.current_a * 100) / 100);
    cJSON_AddNumberToObject(json.get(), "power_w", std::round(data.power_w * 100) / 100);
    cJSON_AddNumberToObject(json.get(), "energy_j", std::round(data.energy_j * 100) / 100);
    cJSON_AddNumberToObject(json.get(), "distance_m", std::round(data.distance_m * 100) / 100);
    cJSON_AddNumberToObject(json.get(), "latitude", std::round(data.latitude * 1000000) / 1000000);
    cJSON_AddNumberToObject(json.get(), "longitude", std::round(data.longitude * 1000000) / 1000000);
    cJSON_AddNumberToObject(json.get(), "altitude", std::round(data.altitude * 100) / 100); // Added altitude
    cJSON_AddNumberToObject(json.get(), "gyro_x", std::round(data.gyro_x * 1000) / 1000);
    cJSON_AddNumberToObject(json.get(), "gyro_y", std::round(data.gyro_y * 1000) / 1000);
    cJSON_AddNumberToObject(json.get(), "gyro_z", std::round(data.gyro_z * 1000) / 1000);
    cJSON_AddNumberToObject(json.get(), "accel_x", std::round(data.accel_x * 1000) / 1000);
    cJSON_AddNumberToObject(json.get(), "accel_y", std::round(data.accel_y * 1000) / 1000);
    cJSON_AddNumberToObject(json.get(), "accel_z", std::round(data.accel_z * 1000) / 1000);
    cJSON_AddNumberToObject(json.get(), "vehicle_heading", std::round(std::fmod(data.vehicle_heading, 360.0f) * 100) / 100);
    cJSON_AddNumberToObject(json.get(), "total_acceleration", std::round(data.total_acceleration * 1000) / 1000);
    cJSON_AddNumberToObject(json.get(), "message_id", data.message_id);
    cJSON_AddNumberToObject(json.get(), "uptime_seconds", std::round(data.uptime_seconds * 100) / 100);
    
    return json;
}
