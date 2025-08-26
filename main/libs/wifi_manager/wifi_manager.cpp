#include "wifi_manager.h"
#include "../config/config.h"
#include "esp_wifi.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "WIFI_MANAGER";

void WiFiManager::eventHandler(void* arg, esp_event_base_t event_base, int32_t event_id, void* event_data) {
    WiFiManager* instance = static_cast<WiFiManager*>(arg);
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(instance->event_group, CONNECTED_BIT);
        esp_wifi_connect();
        ESP_LOGI(TAG, "Retrying WiFi connection...");
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(instance->event_group, CONNECTED_BIT);
        ESP_LOGI(TAG, "WiFi Connected!");
    }
}

WiFiManager::WiFiManager() { 
    event_group = xEventGroupCreate(); 
}

WiFiManager::~WiFiManager() { 
    vEventGroupDelete(event_group); 
}

void WiFiManager::initialize() {
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &eventHandler, this, NULL);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &eventHandler, this, NULL);
    
    wifi_config_t wifi_config = {};
    strncpy((char*)wifi_config.sta.ssid, TelemetryConfig::WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strncpy((char*)wifi_config.sta.password, TelemetryConfig::WIFI_PASSWORD, sizeof(wifi_config.sta.password));
    
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
}

void WiFiManager::waitForConnection() {
    xEventGroupWaitBits(event_group, CONNECTED_BIT, pdFALSE, pdFALSE, portMAX_DELAY);
}