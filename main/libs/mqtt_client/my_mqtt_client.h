#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_crt_bundle.h"
#include "../telemetry_data/telemetry_data.h"
#include "mqtt_client.h" // Use the public header for the MQTT component

class MQTTClient {
private:
    esp_mqtt_client_handle_t client;
    EventGroupHandle_t event_group;
    static constexpr int CONNECTED_BIT = BIT0;
    
    std::string generateClientId();
    static void mqttEventHandler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data);
    
public:
    MQTTClient();
    ~MQTTClient();
    
    bool initialize();
    bool waitForConnection(uint32_t timeout_ms = 30000);
    bool publish(const TelemetryData& telemetry);
};