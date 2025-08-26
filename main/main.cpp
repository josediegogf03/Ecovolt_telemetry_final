#include "nvs_flash.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "libs/gps/gps.h"
#include "libs/telemetry_system/telemetry_system.h"

static const char *TAG = "MAIN";

// Task wrapper for the C++ class
extern "C" void telemetry_task_wrapper(void *pvParameters) {
    auto* system = static_cast<TelemetrySystem*>(pvParameters);
    system->run();
    vTaskDelete(NULL);
}


//== ENTRY POINT ===================================================================
extern "C" void app_main(void) {
    ESP_LOGI(TAG, "This is a test message from app_main");
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    
    ESP_LOGI(TAG, "ESP32 Combined Telemetry System Starting...");
    
    xTaskCreate(GPS::gps_read_task, "gps_read_task", 4096, NULL, 5, NULL);
    ESP_LOGI(TAG, "GPS reader task started.");
    
    static TelemetrySystem telemetry_system;
    
    xTaskCreate(telemetry_task_wrapper, "telemetry_task", 8192, &telemetry_system, 5, NULL);
}