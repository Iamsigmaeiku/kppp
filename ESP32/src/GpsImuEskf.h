/**
 * 2D ground-vehicle ESKF (loosely coupled GPS + IMU).
 * Inspired by zm0612/eskf-gps-imu-fusion Predict/Correct + HDOP-R / velocity update.
 *
 * Nominal / error state (8):
 *   x_e, y_n [m], vx, vy [m/s], yaw [rad NMEA], b_gz [dps], b_ax, b_ay [g]
 *
 * Board assumption (same as prior DR): gravity mostly on +Y → horizontal = X/Z.
 * Local frame: x=east, y=north; heading 0=north, clockwise (NMEA course).
 *
 * Pure C++ — no Arduino / Eigen.
 */
#pragma once

#include <stdint.h>

#ifndef ESKF_DEBUG
#define ESKF_DEBUG 1
#endif

#ifndef ESKF_GYRO_BIAS_SAMPLES
#define ESKF_GYRO_BIAS_SAMPLES 200
#endif

#ifndef ESKF_COURSE_MIN_SPEED_MPS
#define ESKF_COURSE_MIN_SPEED_MPS 2.0f
#endif

#ifndef ESKF_HDOP_SCALE
#define ESKF_HDOP_SCALE 1.2f
#endif

#ifndef ESKF_POS_SIGMA_MIN_M
#define ESKF_POS_SIGMA_MIN_M 1.5f
#endif

#ifndef ESKF_OUTLIER_GATE_SIGMA
#define ESKF_OUTLIER_GATE_SIGMA 3.0f
#endif

/** IMU body-x to vehicle-forward yaw offset (deg, CCW about up). */
#ifndef DR_IMU_MOUNT_YAW_DEG
#define DR_IMU_MOUNT_YAW_DEG 0.0f
#endif

enum class EskfState : uint8_t { CALIBRATING = 0, RUNNING = 1 };

struct EskfOutput {
  bool valid;
  float lat_dr;
  float lon_dr;
  float heading_deg;
  float speed_mps;
  EskfState state;
  uint16_t bias_samples;
};

class GpsImuEskf {
 public:
  GpsImuEskf();

  void reset();

  /** New GPS fix edge only (updated_at changed). */
  void onGpsFix(uint32_t t_ms, float lat, float lon, float speed_mps,
                float course_deg, float hdop, uint32_t sat_count);

  EskfOutput tick(uint32_t t_ms, float ax_g, float ay_g, float az_g,
                  float gx_dps, float gy_dps, float gz_dps);

  EskfState state() const { return state_; }
  float gyroBiasDps() const { return b_gz_; }
  bool hasOrigin() const { return has_origin_; }

 private:
  static constexpr int kDim = 8;
  static constexpr int kIx = 0;
  static constexpr int kIy = 1;
  static constexpr int kIvx = 2;
  static constexpr int kIvy = 3;
  static constexpr int kIyaw = 4;
  static constexpr int kIbgz = 5;
  static constexpr int kIbax = 6;
  static constexpr int kIbay = 7;

  void latLonToLocal(float lat, float lon, float &x, float &y) const;
  void localToLatLon(float &lat, float &lon) const;
  void bodyHorizG(float ax_g, float az_g, float &a_fwd, float &a_lat) const;
  void predictCov(float dt, float a_fwd_mps2, float a_lat_mps2);
  void correctPosition(float x_gps, float y_gps, float sigma_m);
  void correctVelocity(float vx_gps, float vy_gps, float sigma_mps);
  void correctYaw(float yaw_gps_rad, float sigma_rad);
  void injectError();
  void zeroVelocityUpdate();
  EskfOutput makeOutput(bool valid) const;

  EskfState state_;
  uint16_t bias_count_;
  double gz_bias_sum_;
  float b_gz_;
  float b_ax_;
  float b_ay_;

  bool has_origin_;
  float lat0_;
  float lon0_;
  float x_m_;
  float y_m_;
  float vx_;
  float vy_;
  float yaw_rad_;

  float dx_[kDim];
  float P_[kDim * kDim];

  bool has_last_tick_;
  uint32_t t_prev_ms_;
  uint32_t last_fix_ms_;
  bool has_last_fix_;
  float last_gps_speed_;
};
