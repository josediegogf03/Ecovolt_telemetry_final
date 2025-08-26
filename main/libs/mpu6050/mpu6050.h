#pragma once

#include "driver/i2c_master.h"
#include "esp_err.h"
#include "../data_types/data_types.h"

class MPU6050 {
private:
    float accel_scale = 16384.0f; // For +/- 2g
    float gyro_scale = 131.0f;    // For +/- 250 deg/s
    i2c_master_bus_handle_t bus_handle = NULL;
    i2c_master_dev_handle_t dev_handle = NULL;
    
    esp_err_t i2c_write_byte(uint8_t reg, uint8_t data);
    esp_err_t i2c_read_bytes(uint8_t reg, uint8_t *data, size_t len);

public:
    esp_err_t init();
    ~MPU6050();
    esp_err_t read_data(mpu6050_data_t *data);
};
