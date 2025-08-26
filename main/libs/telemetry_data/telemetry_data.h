#pragma once

#include <memory>
#include <string>
#include "cJSON.h"

class TelemetryData {
public:
    struct SensorData {
        float speed_ms;
        float voltage_v;
        float current_a;
        float power_w;
        float energy_j;
        float distance_m;
        double latitude;
        double longitude;
        float altitude; // Added altitude
        float gyro_x, gyro_y, gyro_z;
        float accel_x, accel_y, accel_z;
        float vehicle_heading;
        float total_acceleration;
        int message_id;
        float uptime_seconds;
        std::string timestamp;
    };

    SensorData data;

    std::unique_ptr<cJSON, decltype(&cJSON_Delete)> toJSON() const;
};
