#include "telemetry_system.h"
#include "../config/config.h"
#include "../data_types/data_types.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include <sys/time.h>
#include <cmath>

static const char *TAG = "TELEMETRY_SYSTEM";

std::string getISOTimestamp() {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    time_t now = tv.tv_sec;
    struct tm* ptm = gmtime(&now);
    char buf[30];
    strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", ptm);
    std::string result(buf);
    result += "." + std::to_string(tv.tv_usec / 1000) + "Z";
    return result;
}

void TelemetrySystem::run() {
    if (mpu.init() != ESP_OK) {
        ESP_LOGE(TAG, "MPU6050 initialization failed! Running without MPU data.");
    }
    
    wifi_manager.initialize();
    wifi_manager.waitForConnection();
    
    mqtt_client.initialize();
    if (!mqtt_client.waitForConnection()) {
        ESP_LOGE(TAG, "MQTT connection timeout! Will attempt to publish anyway.");
    }
    
    ESP_LOGI(TAG, "System initialized. Starting main telemetry loop...");
    
    start_time_us = esp_timer_get_time();

    while (true) {
        g_sensor_data.mpu_valid = (mpu.read_data(&g_sensor_data.mpu_data) == ESP_OK);
        
        TelemetryData telemetry;
        float voltage, current;
        adc_reader.read_voltage_and_current(voltage, current);
        
        float speed_ms = g_sensor_data.gps_valid ? (g_sensor_data.gps_data.speed_kmh / 3.6f) : 0.0f;
        float power = voltage * current;
        
        float time_delta_s = TelemetryConfig::PUBLISH_INTERVAL / 1000.0f;
        cumulative_energy += power * time_delta_s;
        cumulative_distance += speed_ms * time_delta_s;

        if (g_sensor_data.mpu_valid) {
            vehicle_heading += g_sensor_data.mpu_data.gyro_z * time_delta_s;
        }
        
        float total_acceleration = 0.0f;
        if (g_sensor_data.mpu_valid) {
            const auto& a = g_sensor_data.mpu_data;
            total_acceleration = std::sqrt(a.accel_x*a.accel_x + a.accel_y*a.accel_y + a.accel_z*a.accel_z);
        }

        message_count++;
        telemetry.data.timestamp = getISOTimestamp();
        telemetry.data.message_id = message_count;
        telemetry.data.uptime_seconds = (esp_timer_get_time() - start_time_us) / 1000000.0f;

        telemetry.data.speed_ms = speed_ms;
        telemetry.data.latitude = g_sensor_data.gps_data.latitude;
        telemetry.data.longitude = g_sensor_data.gps_data.longitude;
        telemetry.data.altitude = g_sensor_data.gps_data.altitude;
        telemetry.data.accel_x = g_sensor_data.mpu_data.accel_x;
        telemetry.data.accel_y = g_sensor_data.mpu_data.accel_y;
        telemetry.data.accel_z = g_sensor_data.mpu_data.accel_z;
        telemetry.data.gyro_x = g_sensor_data.mpu_data.gyro_x;
        telemetry.data.gyro_y = g_sensor_data.mpu_data.gyro_y;
        telemetry.data.gyro_z = g_sensor_data.mpu_data.gyro_z;
        telemetry.data.total_acceleration = total_acceleration;
        telemetry.data.vehicle_heading = vehicle_heading;
        
        telemetry.data.voltage_v = voltage;
        telemetry.data.current_a = current;
        telemetry.data.power_w = power;
        telemetry.data.energy_j = cumulative_energy;
        telemetry.data.distance_m = cumulative_distance;

        if (mqtt_client.publish(telemetry)) {
            ESP_LOGI(TAG, "Message %d sent. Speed: %.2f m/s, Lat: %.4f, Lon: %.4f, Alt: %.2f",
                telemetry.data.message_id,
                telemetry.data.speed_ms,
                telemetry.data.latitude,
                telemetry.data.longitude,
                telemetry.data.altitude);
        } else {
            ESP_LOGE(TAG, "Failed to publish message %d", telemetry.data.message_id);
        }
        
        vTaskDelay(pdMS_TO_TICKS(TelemetryConfig::PUBLISH_INTERVAL));
    }
}
