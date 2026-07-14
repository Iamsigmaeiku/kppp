/**
 * GPS-aided dead reckoning (loosely-coupled complementary filter).
 * Pure C++ — caller supplies timestamps; no Arduino dependency.
 *
 * Local frame: x=east(m), y=north(m), heading=0 north / clockwise (NMEA course).
 * Integration: x += v*sin(h)*dt, y += v*cos(h)*dt
 */
#pragma once

#include <stdint.h>

#ifndef DR_DEBUG
#define DR_DEBUG 1
#endif

#ifndef DR_GYRO_BIAS_SAMPLES
#define DR_GYRO_BIAS_SAMPLES 200
#endif

#ifndef DR_GPS_FIX_MAX_AGE_MS
#define DR_GPS_FIX_MAX_AGE_MS 1500u
#endif

#ifndef DR_POS_BLEND
#define DR_POS_BLEND 0.4f
#endif

#ifndef DR_HDG_BLEND
#define DR_HDG_BLEND 0.3f
#endif

#ifndef DR_COURSE_MIN_SPEED_MPS
#define DR_COURSE_MIN_SPEED_MPS 2.0f
#endif

/** IMU body-x to vehicle-forward yaw offset (deg, CCW positive about up). */
#ifndef DR_IMU_MOUNT_YAW_DEG
#define DR_IMU_MOUNT_YAW_DEG 0.0f
#endif

enum class DrState : uint8_t { CALIBRATING = 0, RUNNING = 1 };

struct DrOutput {
  bool valid;
  float lat_dr;
  float lon_dr;
  float heading_deg;
  float speed_mps;
  DrState state;
  uint16_t bias_samples;  // while CALIBRATING: count so far
};

class DeadReckoner {
 public:
  DeadReckoner();

  void reset();

  /** Call only on a new GPS fix edge (updated_at changed), not every IMU tick. */
  void onGpsFix(uint32_t t_ms, float lat, float lon, float speed_mps,
                float course_deg, uint32_t sat_count);

  DrOutput tick(uint32_t t_ms, float ax_g, float ay_g, float az_g, float gx_dps,
                float gy_dps, float gz_dps);

  DrState state() const { return state_; }
  float gyroBiasDps() const { return gz_bias_dps_; }
  bool hasOrigin() const { return has_origin_; }

 private:
  void latLonToLocal(float lat, float lon, float &x, float &y) const;
  void localToLatLon(float &lat, float &lon) const;
  float axForwardG(float ax_g, float ay_g) const;
  DrOutput makeOutput(bool valid) const;

  DrState state_;
  uint16_t bias_count_;
  double gz_bias_sum_;
  float gz_bias_dps_;

  bool has_origin_;
  float lat0_;
  float lon0_;
  float x_m_;
  float y_m_;
  float heading_rad_;
  float v_mps_;
  float v_last_gps_;

  bool has_last_tick_;
  uint32_t t_prev_ms_;
  uint32_t last_fix_ms_;
  bool has_last_fix_;
};
