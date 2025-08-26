#include "gps.h"
#include "../config/config.h"
#include "../data_types/data_types.h"
#include "driver/uart.h"
#include "esp_log.h"
#include <string.h>
#include <stdlib.h>

static const char *TAG = "GPS";

namespace GPS {
    // A custom implementation of strsep to handle empty tokens correctly.
    char* strsep_custom(char** stringp, const char* delim) {
        if (stringp == NULL || *stringp == NULL) {
            return NULL;
        }
        char* start = *stringp;
        char* p = strpbrk(start, delim);
        if (p == NULL) {
            *stringp = NULL; // No more delimiters.
        } else {
            *p = '\0';
            *stringp = p + 1;
        }
        return start;
    }

    // Function to convert NMEA coordinate to decimal degrees
    float nmea_to_decimal(const char* nmea_coord, char direction) {
        ESP_LOGD(TAG, "nmea_to_decimal: Input coord='%s', direction='%c'", nmea_coord, direction);
        if (!nmea_coord || strlen(nmea_coord) < 3) {
            ESP_LOGW(TAG, "nmea_to_decimal: Invalid or empty coordinate string: %s", nmea_coord ? nmea_coord : "NULL");
            return 0.0;
        }

        const char* dot = strchr(nmea_coord, '.');
        if (!dot) {
            ESP_LOGW(TAG, "nmea_to_decimal: No dot found in coordinate: %s", nmea_coord);
            return 0.0;
        }

        int deg_len = (dot - nmea_coord) - 2;
        if (deg_len < 1) {
            ESP_LOGW(TAG, "nmea_to_decimal: Invalid coordinate format for %s", nmea_coord);
            return 0.0;
        }

        char deg_str[10];
        strncpy(deg_str, nmea_coord, deg_len);
        deg_str[deg_len] = '\0';
        
        float degrees = atof(deg_str);
        float minutes = atof(dot - 2);

        float decimal_degrees = degrees + (minutes / 60.0f);

        if (direction == 'S' || direction == 'W') {
            decimal_degrees = -decimal_degrees;
        }
        ESP_LOGD(TAG, "nmea_to_decimal: Converted %.6f", decimal_degrees);
        return decimal_degrees;
    }

    // Function to calculate NMEA checksum
    uint8_t calculate_nmea_checksum(const char* sentence) {
        uint8_t checksum = 0;
        for (int i = 1; sentence[i] != '*' && sentence[i] != '\0'; i++) {
            checksum ^= sentence[i];
        }
        return checksum;
    }

    // Function to validate NMEA sentence checksum
    bool validate_nmea_checksum(const char* sentence) {
        const char* asterisk = strchr(sentence, '*');
        if (!asterisk) return false;
        
        uint8_t received_checksum = (uint8_t)strtol(asterisk + 1, NULL, 16);
        uint8_t calculated_checksum = calculate_nmea_checksum(sentence);
        
        return received_checksum == calculated_checksum;
    }

    // Parse GPGGA sentence
    void parse_gpgga(char* sentence) {
        ESP_LOGD(TAG, "parse_gpgga: Parsing GPGGA sentence: %s", sentence);
        char* cursor = sentence;
        
        char *time, *lat_val, *lat_dir, *lon_val, *lon_dir, *fix_quality, *num_sats, *hdop, *altitude;

        strsep_custom(&cursor, ","); // Skip $GPGGA
        time = strsep_custom(&cursor, ",");
        lat_val = strsep_custom(&cursor, ",");
        lat_dir = strsep_custom(&cursor, ",");
        lon_val = strsep_custom(&cursor, ",");
        lon_dir = strsep_custom(&cursor, ",");
        fix_quality = strsep_custom(&cursor, ",");
        num_sats = strsep_custom(&cursor, ",");
        hdop = strsep_custom(&cursor, ",");
        altitude = strsep_custom(&cursor, ",");

        if (fix_quality && strlen(fix_quality) > 0) {
            g_sensor_data.gps_data.fix_valid = (atoi(fix_quality) > 0);
        } else {
            g_sensor_data.gps_data.fix_valid = false;
        }

        if (g_sensor_data.gps_data.fix_valid) {
            if (lat_val && lat_dir && *lat_dir != '\0') {
                g_sensor_data.gps_data.latitude = nmea_to_decimal(lat_val, *lat_dir);
            }
            if (lon_val && lon_dir && *lon_dir != '\0') {
                g_sensor_data.gps_data.longitude = nmea_to_decimal(lon_val, *lon_dir);
            }
            if (altitude && strlen(altitude) > 0) {
                g_sensor_data.gps_data.altitude = atof(altitude);
            }
            g_sensor_data.gps_valid = true; // Set gps_valid to true when a fix is obtained
        } else {
            g_sensor_data.gps_valid = false; // No fix, so GPS data is not valid
        }
    }

    // Parse GPRMC sentence
    void parse_gprmc(char* sentence) {
        ESP_LOGD(TAG, "parse_gprmc: Parsing GPRMC sentence: %s", sentence);
        char* cursor = sentence;

        char *time, *status, *lat_val, *lat_dir, *lon_val, *lon_dir, *speed_knots;

        strsep_custom(&cursor, ","); // Skip $GPRMC
        time = strsep_custom(&cursor, ",");
        status = strsep_custom(&cursor, ",");
        lat_val = strsep_custom(&cursor, ",");
        lat_dir = strsep_custom(&cursor, ",");
        lon_val = strsep_custom(&cursor, ",");
        lon_dir = strsep_custom(&cursor, ",");
        speed_knots = strsep_custom(&cursor, ",");

        if (status && *status == 'A') {
            g_sensor_data.gps_data.fix_valid = true;
            if (lat_val && lat_dir && *lat_dir != '\0') {
                g_sensor_data.gps_data.latitude = nmea_to_decimal(lat_val, *lat_dir);
            }
            if (lon_val && lon_dir && *lon_dir != '\0') {
                g_sensor_data.gps_data.longitude = nmea_to_decimal(lon_val, *lon_dir);
            }
            if (speed_knots && strlen(speed_knots) > 0) {
                g_sensor_data.gps_data.speed_kmh = atof(speed_knots) * 1.852f;
            }
            g_sensor_data.gps_valid = true; // Set gps_valid to true when a fix is obtained
        } else {
            g_sensor_data.gps_data.fix_valid = false;
            g_sensor_data.gps_valid = false; // No fix, so GPS data is not valid
        }
    }

    // Parse NMEA sentence
    void parse_nmea_sentence(char* sentence) {
        ESP_LOGI(TAG, "parse_nmea_sentence: Received sentence: %s", sentence);
        if (!validate_nmea_checksum(sentence)) {
            ESP_LOGW(TAG, "parse_nmea_sentence: Invalid checksum for: %s", sentence);
            return;
        }
        ESP_LOGD(TAG, "parse_nmea_sentence: Checksum valid.");
        
        char* sentence_copy = strdup(sentence);
        if (!sentence_copy) {
            ESP_LOGE(TAG, "Failed to allocate memory for sentence copy");
            return;
        }
        
        if (strncmp(sentence_copy, "$GNGGA", 6) == 0 || strncmp(sentence_copy, "$GPGGA", 6) == 0) {
            parse_gpgga(sentence_copy);
        } else if (strncmp(sentence_copy, "$GNRMC", 6) == 0 || strncmp(sentence_copy, "$GPRMC", 6) == 0) {
            parse_gprmc(sentence_copy);
        }
        free(sentence_copy);
    }

    void gps_read_task(void* parameter) {
        uart_config_t uart_config = {
            .baud_rate = HardwareConfig::GPS_UART_BAUD_RATE,
            .data_bits = UART_DATA_8_BITS,
            .parity = UART_PARITY_DISABLE,
            .stop_bits = UART_STOP_BITS_1,
            .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
            .source_clk = UART_SCLK_DEFAULT,
        };
        ESP_ERROR_CHECK(uart_driver_install(HardwareConfig::GPS_UART_NUM, 1024 * 2, 0, 0, NULL, 0));
        ESP_ERROR_CHECK(uart_param_config(HardwareConfig::GPS_UART_NUM, &uart_config));
        ESP_ERROR_CHECK(uart_set_pin(HardwareConfig::GPS_UART_NUM, HardwareConfig::GPS_UART_TX_PIN, HardwareConfig::GPS_UART_RX_PIN, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));

        uint8_t* buffer = (uint8_t*) malloc(1024);
        char nmea_sentence[256];
        int sentence_idx = 0;
        bool in_sentence = false;
        
        ESP_LOGI(TAG, "GPS: Starting UART read loop.");
        while (1) {
            int len = uart_read_bytes(HardwareConfig::GPS_UART_NUM, buffer, 1023, 20 / portTICK_PERIOD_MS);
            if (len > 0) {
                ESP_LOGI(TAG, "GPS: Received %d bytes from UART", len);
                // Null-terminate the buffer for safe printing, if it's text
                buffer[len] = '\0'; 
                // ESP_LOGV(TAG, "GPS: Raw data: %s", (char*)buffer); // This can print non-ASCII chars and crash the monitor
                for (int i = 0; i < len; i++) {
                    char c = buffer[i];
                    if (c == '$') {
                        in_sentence = true;
                        sentence_idx = 0;
                        nmea_sentence[sentence_idx++] = c;
                    } else if (in_sentence) {
                        if (c == '\r' || c == '\n') {
                            if (sentence_idx > 5) { // Basic validation for a meaningful sentence
                                nmea_sentence[sentence_idx] = '\0';
                                parse_nmea_sentence(nmea_sentence);
                            }
                            in_sentence = false;
                        } else if (sentence_idx < 255) { // Leave space for null terminator
                            nmea_sentence[sentence_idx++] = c;
                        }
                    }
                }
            }
            vTaskDelay(100 / portTICK_PERIOD_MS);
        }
        free(buffer);
    }
}