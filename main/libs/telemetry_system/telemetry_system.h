#pragma once

#include "../wifi_manager/wifi_manager.h"
#include "../mqtt_client/my_mqtt_client.h"
#include "../adc_reader/adc_reader.h"
#include "../mpu6050/mpu6050.h"

class TelemetrySystem {
private:
    WiFiManager wifi_manager;
    MQTTClient mqtt_client;
    ADCReader adc_reader;
    MPU6050 mpu;

    float cumulative_energy = 0.0f;
    float cumulative_distance = 0.0f;
    float vehicle_heading = 0.0f;
    int message_count = 0;
    int64_t start_time_us;
    
public:
    void run();
};
