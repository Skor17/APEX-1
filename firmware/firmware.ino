/*
 * APEX-1 flight computer — ESP32 firmware
 *
 * Reads the BMP280 (barometer) and MPU6050 (IMU) over I2C and streams telemetry
 * over UDP/Wi-Fi in the exact JSON contract defined by protocol.py (port 5551).
 * It is a drop-in replacement for simulator.py: the GCS (gcs.py) needs no changes.
 *
 * Wiring (ESP32 dev board):
 *   BMP280  SDA -> GPIO 21 (I2C_SDA)    SCL -> GPIO 22 (I2C_SCL)
 *   MPU6050 SDA -> GPIO 21              SCL -> GPIO 22
 *   (both share the I2C bus; VCC -> 3V3, GND -> GND)
 *   Launch button: GPIO 4 (LAUNCH_PIN) -> GND  (internal pull-up)
 *
 * Setup:
 *   1. Arduino IDE: install the "esp32" board package; select your ESP32 model.
 *   2. Install libraries via Library Manager: "Adafruit BMP280" and
 *      "Adafruit MPU6050" (both by Adafruit).
 *   3. Edit config.h: Wi-Fi SSID/password and the GCS LAN IP.
 *   4. Upload, then open the Serial Monitor at 115200.
 *   5. Press the launch button (or type 'L') to launch.
 *
 * Known limitations (see README roadmap):
 *   - Velocity comes from integrating IMU acceleration, which drifts over time.
 *     Sensor fusion (complementary/Kalman filter) is planned.
 *   - Altitude is barometric (~8 m noise from 1 hPa pressure noise), so landing
 *     detection is approximate. A physical touchdown switch would make it exact.
 *   - Mount the IMU so axis VERT_AXIS (default Z) points up (toward the nose)
 *     when the rocket is upright.
 */
#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include "config.h"
#include "Sensors.h"
#include "FlightState.h"

Sensors     sensors;
FlightState flight;
WiFiUDP     udp;

// Inverse international barometric formula (troposphere) — matches simulator.py
// so the reported altitude is on the same scale the GCS/HUD expects.
static float baroAltitude(float p_hpa) {
  return (288.15f / 0.0065f) * (1.0f - powf(p_hpa / 1013.25f, 1.0f / 5.2558f));
}

// Launch on a button falling edge or a serial 'L'/'l'.
static void handleLaunch(float alt_baro) {
  static bool btn_prev = HIGH;
  bool btn = digitalRead(LAUNCH_PIN) == LOW;  // active-low, internal pull-up
  bool btn_falling = (btn_prev == HIGH && btn == LOW);
  btn_prev = btn;

  bool ser = false;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == 'L' || c == 'l') ser = true;
  }

  if ((btn_falling || ser) && (!flight.launched || flight.landed)) {
    flight.launch(alt_baro);
    Serial.print(F("LAUNCH (flight "));
    Serial.print(flight.flight);
    Serial.println(F(")"));
  }
}

// Emit one telemetry frame — field names/order match protocol.TelemetryFrame.
static void sendTelemetry(const SensorReadings& r) {
  char buf[200];
  snprintf(buf, sizeof(buf),
           "{\"t\":%.3f,\"flight\":%d,\"status\":\"%s\","
           "\"altitude\":%.3f,\"velocity\":%.3f,\"accel\":%.3f,"
           "\"pressure\":%.3f,\"temperature\":%.3f}",
           flight.t, flight.flight, flight.status(),
           flight.altitude, flight.velocity, flight.accel,
           r.pressure_hpa, r.temperature_c);
  udp.beginPacket(GCS_IP, GCS_PORT);
  udp.write((const uint8_t*)buf, strlen(buf));
  udp.endPacket();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println(F("\n=== APEX-1 flight computer ==="));

  pinMode(LAUNCH_PIN, INPUT_PULLUP);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print(F("Wi-Fi connecting"));
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(250);
    Serial.print(F("."));
  }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println(F("Wi-Fi failed — check config.h. Halting."));
    while (true) delay(1000);
  }
  Serial.print(F("Connected, IP "));
  Serial.println(WiFi.localIP().toString());

  if (!sensors.begin()) {
    Serial.println(F("Sensor init failed — check I2C wiring. Halting."));
    while (true) delay(1000);
  }
  Serial.println(F("Sensors OK (BMP280 + MPU6050)"));

  udp.begin(LOCAL_UDP_PORT);
  Serial.print(F("Streaming telemetry to "));
  Serial.print(GCS_IP);
  Serial.print(F(":"));
  Serial.println(GCS_PORT);

  flight.begin();
}

void loop() {
  static uint32_t last = 0;
  const uint32_t period = 1000 / TELEMETRY_HZ;
  uint32_t now = millis();
  if (now - last < period) return;  // pace the loop to the telemetry rate
  last = now;

  SensorReadings r;
  sensors.read(r);
  float alt_baro = baroAltitude(r.pressure_hpa);

  handleLaunch(alt_baro);
  flight.step(alt_baro, r.accel[VERT_AXIS]);
  sendTelemetry(r);
}
