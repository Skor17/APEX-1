#pragma once
// Flight state machine + vertical physics, driven by real sensor samples.
// Mirrors simulator.py's FlightSimulator so the on-wire status sequence and
// units match what the GCS/HUD expect.
#include <Arduino.h>
#include "config.h"

// Status strings — MUST stay in sync with protocol.py (the GCS matches on them).
static const char* STATUS_PRE_LAUNCH = "PRE-LAUNCH";
static const char* STATUS_BOOST      = "BOOST";
static const char* STATUS_ASCENT     = "ASCENT";
static const char* STATUS_DESCENT    = "DESCENT";
static const char* STATUS_PARACHUTE  = "PARACHUTE";
static const char* STATUS_LANDED     = "LANDED";

class FlightState {
 public:
  float t = 0.0;         // mission elapsed time [s]
  int   flight = 0;      // flight number (1-based, increments per launch)
  float altitude = 0.0;  // altitude above launch pad [m]
  float velocity = 0.0;  // vertical velocity [m/s], +up
  float accel = 0.0;     // proper acceleration [m/s^2]
  bool  launched = false;
  bool  landed = false;
  bool  chute = false;

  void begin() { flight = 0; }

  // Trigger a (re)launch. alt_baro is the current barometric altitude [m],
  // captured as the on-pad reference so reported altitude starts at ~0.
  void launch(float alt_baro) {
    flight++;
    alt0 = alt_baro;
    t = 0.0f; altitude = 0.0f; velocity = 0.0f; accel = 0.0f;
    chute = false; landed = false; launched = true;
    land_timer = 0.0f;
    last_ms = millis();
  }

  // Advance one sample. alt_baro [m]; accel_meas = raw IMU specific force on
  // the vertical (up) axis [m/s^2].
  void step(float alt_baro, float accel_meas) {
    if (!launched || landed) return;
    unsigned long now = millis();
    float dt = (now - last_ms) / 1000.0f;
    last_ms = now;
    if (dt <= 0.0f) return;

    // Proper acceleration = specific force minus gravity (axis points up, so
    // it reads +g at rest). Integrating this gives vertical velocity.
    accel = accel_meas - GRAVITY;
    velocity += accel * dt;   // raw integration drifts; sensor fusion is future work
    altitude = alt_baro - alt0;
    t += dt;

    // Parachute: descending and at/below the deploy altitude.
    if (!chute && velocity < 0.0f && altitude <= CHUTE_DEPLOY_ALT) {
      chute = true;
    }
    // Landing: back at pad level and not ascending, sustained briefly to ride
    // out barometric noise. Only considered after the burn is done.
    if (t > BOOST_TIME) {
      if (altitude <= 0.0f && velocity <= 0.0f) {
        land_timer += dt;
        if (land_timer >= 0.3f) landed = true;
      } else {
        land_timer = 0.0f;
      }
    }
  }

  const char* status() const {
    if (!launched)        return STATUS_PRE_LAUNCH;
    if (landed)           return STATUS_LANDED;
    if (t < BOOST_TIME)   return STATUS_BOOST;
    if (chute)            return STATUS_PARACHUTE;
    if (velocity > 0.0f)  return STATUS_ASCENT;
    return STATUS_DESCENT;
  }

 private:
  float         alt0 = 0.0;     // barometric altitude at launch [m]
  float         land_timer = 0.0;
  unsigned long last_ms = 0;
};
