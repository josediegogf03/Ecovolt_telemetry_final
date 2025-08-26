#include "adc_reader.h"
#include "esp_log.h"
#include "driver/adc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define ADC_SAMPLES 100

static const char *TAG = "ADC_READER";

// TODO: These values needs to be calibrated
#define R1      1000000.0f // Ohms
#define R2      56000.0f  // Ohms
#define SHUNT_RESISTOR 0.02f // Ohms

ADCReader::ADCReader() {
    configure_adc();
    data_mutex = xSemaphoreCreateMutex();
    init_timer();
}

ADCReader::~ADCReader() {
    esp_timer_delete(periodic_timer);
    vSemaphoreDelete(data_mutex);
}

void ADCReader::configure_adc() {
    adc1_config_width(ADC_WIDTH_BIT_12);
    adc1_config_channel_atten(voltage_channel, ADC_ATTEN_DB_11);
    adc1_config_channel_atten(current_channel, ADC_ATTEN_DB_11);
    esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12, 0, &adc_chars);
    ESP_LOGI(TAG, "ADC configured.");
}

void ADCReader::init_timer() {
    const esp_timer_create_args_t periodic_timer_args = {
            .callback = &adc_timer_callback,
            .arg = this,
            .name = "adc_timer"
    };
    ESP_ERROR_CHECK(esp_timer_create(&periodic_timer_args, &periodic_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(periodic_timer, 500000)); // 100 milliseconds
}

void ADCReader::adc_timer_callback(void* arg) {
    ADCReader* reader = static_cast<ADCReader*>(arg);

    uint32_t adc_reading_i = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) {
        adc_reading_i += adc1_get_raw(reader->current_channel);
    }
    adc_reading_i /= ADC_SAMPLES;
    reader->raw_current_reading = adc_reading_i;

    // Convert raw ADC reading to millivolts
    uint32_t current_sense_mv = esp_adc_cal_raw_to_voltage(adc_reading_i, &reader->adc_chars);

    // INA240A1 Gain and Vref
    const float gain = 20.0f;
    const float vref_v = 1.65f; // VCC / 2 for bidirectional measurement (3.3V / 2)

    // Calculate current using the INA240 formula: I = (Vout - Vref) / (Gain * Rshunt)
    // Vout is converted from mV to V by dividing by 1000.0
    float current = ((current_sense_mv / 1000.0f) - vref_v) / (gain * SHUNT_RESISTOR); // Current in A

    if (xSemaphoreTake(reader->data_mutex, portMAX_DELAY) == pdTRUE) {
        reader->cumulative_current += current;
        reader->current_reading_count++;
        xSemaphoreGive(reader->data_mutex);
    }
}

void ADCReader::read_voltage_and_current(float& voltage, float& current) {
    uint32_t adc_reading_v = 0;
    for (int i = 0; i < ADC_SAMPLES; i++) {
        adc_reading_v += adc1_get_raw(voltage_channel);
    }
    adc_reading_v /= ADC_SAMPLES;
    uint32_t voltage_sense_mv = esp_adc_cal_raw_to_voltage(adc_reading_v, &adc_chars);
    voltage = voltage_sense_mv * ((R1 + R2) / R2) / 1000.0f; // Voltage in V

    if (xSemaphoreTake(data_mutex, portMAX_DELAY) == pdTRUE) {
        if (current_reading_count > 0) {
            current = cumulative_current / current_reading_count;
        } else {
            current = 0.0f;
        }
        cumulative_current = 0.0f;
        current_reading_count = 0;
        xSemaphoreGive(data_mutex);
    }
    ESP_LOGI(TAG, "Voltage ADC Raw: %d, Converted Voltage: %.2f V", (int)adc_reading_v, voltage);
    ESP_LOGI(TAG, "Current ADC Raw: %d, Converted Current: %.2f A", (int)raw_current_reading, current);
}