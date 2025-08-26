#include "config.h"

namespace TelemetryConfig {
    const char* WIFI_SSID = "Nospasa";
    const char* WIFI_PASSWORD = "Canada031106";
    const char* ABLY_API_KEY = "ja_fwQ.K6CTEw:F-aWFMdJXPCv9MvxhYztCGna3XdRJZVgA0qm9pMfDOQ";
    const char* ABLY_CLIENT_ID_PREFIX = "esp32_telemetry_";
    const char* ABLY_CHANNEL = "EcoTele";
    const uint32_t PUBLISH_INTERVAL = 500; // Publish every 0.5 seconds
    
    const char* MQTT_BROKER_HOST = "mqtt.ably.io";
    const int MQTT_BROKER_PORT = 8883; // SSL port
    const char* MQTT_USERNAME = ABLY_API_KEY;
    const char* MQTT_PASSWORD = "";
}

namespace HardwareConfig {
    const uint8_t MPU6050_ADDR = 0x68;
    const gpio_num_t I2C_MASTER_SCL_IO = GPIO_NUM_9;
    const gpio_num_t I2C_MASTER_SDA_IO = GPIO_NUM_8;
    const uint32_t I2C_MASTER_FREQ_HZ = 100000;
    
    const uart_port_t GPS_UART_NUM = UART_NUM_1;
    const int GPS_UART_BAUD_RATE = 9600;
    const gpio_num_t GPS_UART_RX_PIN = GPIO_NUM_20;
    const gpio_num_t GPS_UART_TX_PIN = GPIO_NUM_21;
}
