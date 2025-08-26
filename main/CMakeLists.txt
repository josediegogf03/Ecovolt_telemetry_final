# Register the "main" component with the build system.
idf_component_register(
    # List all source files for the component.
    SRCS 
        "main.cpp"
        "libs/config/config.cpp"
        "libs/data_types/data_types.cpp"
        "libs/gps/gps.cpp"
        "libs/mpu6050/mpu6050.cpp"
        "libs/mqtt_client/my_mqtt_client.cpp"
        "libs/adc_reader/adc_reader.cpp"
        "libs/telemetry_data/telemetry_data.cpp"
        "libs/telemetry_system/telemetry_system.cpp"
        "libs/wifi_manager/wifi_manager.cpp"

    # List of components that this component depends on.
    PRIV_REQUIRES
        json
        mqtt
        nvs_flash
        esp_wifi
        esp_event
        esp_netif
        esp_system
        driver
        freertos
        log
        mbedtls
        esp_adc
        esp_timer
)

# Set this component to be treated as C++
set(COMPONENT_ADD_INCLUDEDIRS . "libs")
set(COMPONENT_SRCS ${COMPONENT_SRCS})
set_property(SOURCE ${COMPONENT_SRCS} PROPERTY LANGUAGE CXX)
