#pragma once

// MPU6050 raw data structure
typedef struct {
    float accel_x, accel_y, accel_z;
    float gyro_x, gyro_y, gyro_z;
} mpu6050_data_t;

// GPS raw data structure
typedef struct {
    float latitude;
    float longitude;
    float altitude;
    float speed_kmh;
    bool fix_valid;
} gps_data_t;

// Combined sensor data structure to hold latest values
typedef struct {
    mpu6050_data_t mpu_data;
    gps_data_t gps_data;
    bool mpu_valid = false;
    bool gps_valid = false;
} combined_sensor_data_t;

// Global instance to be accessed by different tasks
extern combined_sensor_data_t g_sensor_data;
