#pragma once

#include "driver/gpio.h"
#include "driver/uart.h"

//== CONFIGURATION =================================================================
// WiFi, MQTT, and application configuration
namespace TelemetryConfig {
    extern const char* WIFI_SSID;
    extern const char* WIFI_PASSWORD;
    extern const char* ABLY_API_KEY;
    extern const char* ABLY_CLIENT_ID_PREFIX;
    extern const char* ABLY_CHANNEL;
    extern const uint32_t PUBLISH_INTERVAL;
    
    // Ably MQTT configuration - SSL version
    extern const char* MQTT_BROKER_HOST;
    extern const int MQTT_BROKER_PORT;
    extern const char* MQTT_USERNAME;
    extern const char* MQTT_PASSWORD;
}

// Hardware configuration
namespace HardwareConfig {
    // MPU6050 I2C configuration
    extern const uint8_t MPU6050_ADDR;
    extern const gpio_num_t I2C_MASTER_SCL_IO;
    extern const gpio_num_t I2C_MASTER_SDA_IO;
    extern const uint32_t I2C_MASTER_FREQ_HZ;
    
    // GPS UART configuration
    extern const uart_port_t GPS_UART_NUM;
    extern const int GPS_UART_BAUD_RATE;
    extern const gpio_num_t GPS_UART_RX_PIN;
    extern const gpio_num_t GPS_UART_TX_PIN;
}