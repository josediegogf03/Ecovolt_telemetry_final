#include "mpu6050.h"
#include "../config/config.h"
#include "esp_log.h"

static const char *TAG = "MPU6050";

esp_err_t MPU6050::i2c_write_byte(uint8_t reg, uint8_t data) {
    uint8_t buf[2] = {reg, data};
    return i2c_master_transmit(dev_handle, buf, 2, pdMS_TO_TICKS(1000));
}

esp_err_t MPU6050::i2c_read_bytes(uint8_t reg, uint8_t *data, size_t len) {
    return i2c_master_transmit_receive(dev_handle, &reg, 1, data, len, pdMS_TO_TICKS(1000));
}

esp_err_t MPU6050::init() {
    i2c_master_bus_config_t i2c_mst_config = {};
    i2c_mst_config.clk_source = I2C_CLK_SRC_DEFAULT;
    i2c_mst_config.i2c_port = I2C_NUM_0;
    i2c_mst_config.scl_io_num = HardwareConfig::I2C_MASTER_SCL_IO;
    i2c_mst_config.sda_io_num = HardwareConfig::I2C_MASTER_SDA_IO;
    i2c_mst_config.glitch_ignore_cnt = 7;
    i2c_mst_config.flags.enable_internal_pullup = true;
    
    esp_err_t ret = i2c_new_master_bus(&i2c_mst_config, &bus_handle);
    if (ret != ESP_OK) return ret;
    
    i2c_device_config_t dev_cfg = {};
    dev_cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    dev_cfg.device_address = HardwareConfig::MPU6050_ADDR;
    dev_cfg.scl_speed_hz = HardwareConfig::I2C_MASTER_FREQ_HZ;
    
    ret = i2c_master_bus_add_device(bus_handle, &dev_cfg, &dev_handle);
    if (ret != ESP_OK) return ret;
    
    // Wake up, set gyro/accel ranges
    i2c_write_byte(0x6B, 0x00); // PWR_MGMT_1: Wake up
    i2c_write_byte(0x1B, 0x00); // GYRO_CONFIG: 
    i2c_write_byte(0x1C, 0x00); // ACCEL_CONFIG: 
    
    ESP_LOGI(TAG, "MPU6050 initialized successfully.");
    return ESP_OK;
}

MPU6050::~MPU6050() {
    if (dev_handle) i2c_master_bus_rm_device(dev_handle);
    if (bus_handle) i2c_del_master_bus(bus_handle);
}

esp_err_t MPU6050::read_data(mpu6050_data_t *data) {
    uint8_t raw[14];
    esp_err_t ret = i2c_read_bytes(0x3B, raw, 14); // Start from ACCEL_XOUT_H
    if (ret != ESP_OK) return ret;
    
    int16_t ax = (raw[0] << 8) | raw[1];
    int16_t ay = (raw[2] << 8) | raw[3];
    int16_t az = (raw[4] << 8) | raw[5];
    int16_t gx = (raw[8] << 8) | raw[9];
    int16_t gy = (raw[10] << 8) | raw[11];
    int16_t gz = (raw[12] << 8) | raw[13];
    
    data->accel_x = (ax / accel_scale) * 9.81f;
    data->accel_y = (ay / accel_scale) * 9.81f;
    data->accel_z = (az / accel_scale) * 9.81f;
    data->gyro_x = gx / gyro_scale;
    data->gyro_y = gy / gyro_scale;
    data->gyro_z = gz / gyro_scale;
    
    return ESP_OK;
}
