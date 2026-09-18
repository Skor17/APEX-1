#pragma once
// Thin wrapper over the BMP280 (barometer) and MPU6050 (IMU).
// Uses the Adafruit libraries; both sensors share the I2C bus.
#include <Wire.h>
#include <Adafruit_BMP280.h>
#include <Adafruit_MPU6050.h>
#include "config.h"

struct SensorReadings {
  float pressure_hpa;  // barometric pressure [hPa]
  float temperature_c; // temperature [deg C]
  float accel[3];      // specific force [m/s^2], X/Y/Z
};

class Sensors {
 public:
  bool begin() {
    Wire.begin(I2C_SDA, I2C_SCL);

    // BMP280 address depends on the breakout's SDO pin; try both.
    if (!bmp.begin(BMP280_ADDR) && !bmp.begin(0x77)) {
      Serial.println(F("BMP280 not found on I2C"));
      return false;
    }

    // MPU6050 auto-detects its address (0x68/0x69).
    if (!mpu.begin()) {
      Serial.println(F("MPU6050 not found on I2C"));
      return false;
    }
    mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu.setFilterBandwidth(MPU6050_BAND_96_HZ);
    return true;
  }

  void read(SensorReadings& r) {
    r.pressure_hpa  = bmp.readPressure() / 100.0f;  // Pa -> hPa
    r.temperature_c = bmp.readTemperature();
    mpu.getAcceleration(&r.accel[0], &r.accel[1], &r.accel[2]);
  }

 private:
  Adafruit_BMP280  bmp;
  Adafruit_MPU6050 mpu;
};
