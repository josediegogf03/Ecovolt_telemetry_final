#pragma once

#include "esp_event.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

class WiFiManager {
private:
    EventGroupHandle_t event_group;
    static constexpr int CONNECTED_BIT = BIT0;
    
    static void eventHandler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data);
    
public:
    WiFiManager();
    ~WiFiManager();
    
    void initialize();
    void waitForConnection();
};