#pragma once
#include <stdint.h>
// APEX-1 flight computer — configuration.
// Edit the Wi-Fi and GCS address before flashing. Everything else has sane
// defaults that match the simulator's flight profile (see simulator.py).

// --- Wi-Fi / link ---------------------------------------------------------
// The vehicle joins this network and streams UDP telemetry to the GCS.
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";

// Ground station LAN IP (the machine running gcs.py). Must be on the same
// network as the vehicle. Port must match protocol.py PORT.
const char*    GCS_IP   = "192.168.1.100";
const uint16_t GCS_PORT = 5551;

// Local UDP source port the vehicle sends from (arbitrary; not the GCS port).
const uint16_t LOCAL_UDP_PORT = 5550;

// --- I2C bus (BMP280 + MPU6050 share it) ----------------------------------
const int    I2C_SDA      = 21;
const int    I2C_SCL      = 22;
const uint8_t BMP280_ADDR = 0x76;   // 0x77 if the breakout's SDO is tied high

// --- Launch input ---------------------------------------------------------
// Button from this GPIO to GND (internal pull-up used). A falling edge
// launches. Serial 'L'/'l' also launches, for bench testing without a button.
const int LAUNCH_PIN = 4;

// --- Flight / telemetry ---------------------------------------------------
const float BOOST_TIME       = 2.0;   // engine burn duration [s]
const float CHUTE_DEPLOY_ALT = 50.0;  // parachute deploy altitude, AGL [m]
const float TELEMETRY_HZ     = 50.0;  // sensor sample + telemetry rate
const float GRAVITY          = 9.81;  // [m/s^2]

// IMU axis that points UP (toward the nose) when the rocket is upright:
// 0 = X, 1 = Y, 2 = Z. Gravity compensation in FlightState assumes this axis
// reads +g at rest, so set it to the physically-up axis for your mount.
const int VERT_AXIS = 2;
