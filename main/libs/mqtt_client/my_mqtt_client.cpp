#include "my_mqtt_client.h"
#include "../config/config.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_crt_bundle.h"
#include "mqtt_client.h" // Use the public header for the MQTT component
#include <sstream>
#include <iomanip>

static const char *TAG = "MQTT_CLIENT";

std::string MQTTClient::generateClientId() {
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);
    std::ostringstream oss;
    oss << TelemetryConfig::ABLY_CLIENT_ID_PREFIX << std::hex << std::setfill('0');
    for (int i = 0; i < 6; ++i) oss << std::setw(2) << static_cast<unsigned>(mac[i]);
    return oss.str();
}

void MQTTClient::mqttEventHandler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    auto* instance = static_cast<MQTTClient*>(handler_args);
    esp_mqtt_event_handle_t event = static_cast<esp_mqtt_event_handle_t>(event_data);
    if (event->event_id == MQTT_EVENT_CONNECTED) {
        ESP_LOGI(TAG, "MQTT_EVENT_CONNECTED");
        xEventGroupSetBits(instance->event_group, CONNECTED_BIT);
    } else if (event->event_id == MQTT_EVENT_DISCONNECTED) {
        ESP_LOGW(TAG, "MQTT_EVENT_DISCONNECTED");
        xEventGroupClearBits(instance->event_group, CONNECTED_BIT);
    } else if (event->event_id == MQTT_EVENT_ERROR) {
        ESP_LOGE(TAG, "MQTT_EVENT_ERROR");
    }
}

MQTTClient::MQTTClient() : client(nullptr) { 
    event_group = xEventGroupCreate(); 
}

MQTTClient::~MQTTClient() { 
    if (client) esp_mqtt_client_destroy(client); 
    vEventGroupDelete(event_group); 
}

bool MQTTClient::initialize() {
    std::string client_id = generateClientId();
    ESP_LOGI(TAG, "Using MQTT client ID: %s", client_id.c_str());
    
    esp_mqtt_client_config_t mqtt_cfg = {};
    mqtt_cfg.broker.address.hostname = TelemetryConfig::MQTT_BROKER_HOST;
    mqtt_cfg.broker.address.port = TelemetryConfig::MQTT_BROKER_PORT;
    mqtt_cfg.broker.address.transport = MQTT_TRANSPORT_OVER_SSL;
    mqtt_cfg.broker.verification.crt_bundle_attach = esp_crt_bundle_attach;
    mqtt_cfg.credentials.username = TelemetryConfig::MQTT_USERNAME;
    mqtt_cfg.credentials.client_id = client_id.c_str();
    
    client = esp_mqtt_client_init(&mqtt_cfg);
    if (!client) return false;
    
    esp_mqtt_client_register_event(client, (esp_mqtt_event_id_t)ESP_EVENT_ANY_ID, (esp_event_handler_t)mqttEventHandler, this);
    return esp_mqtt_client_start(client) == ESP_OK;
}

bool MQTTClient::waitForConnection(uint32_t timeout_ms) {
    EventBits_t bits = xEventGroupWaitBits(event_group, CONNECTED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms));
    return (bits & CONNECTED_BIT) != 0;
}

bool MQTTClient::publish(const TelemetryData& telemetry) {
    if (!(xEventGroupGetBits(event_group) & CONNECTED_BIT)) {
        ESP_LOGW(TAG, "MQTT not connected, skipping publish");
        return false;
    }
    
    auto json = telemetry.toJSON();
    std::unique_ptr<char, decltype(&free)> json_string(cJSON_PrintUnformatted(json.get()), free);
    if (!json_string) return false;
    
    int msg_id = esp_mqtt_client_publish(client, TelemetryConfig::ABLY_CHANNEL, json_string.get(), 0, 1, 0);
    return msg_id >= 0;
}
