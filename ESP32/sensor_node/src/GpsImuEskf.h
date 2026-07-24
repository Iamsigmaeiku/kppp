/**
 * 15-state loosely-coupled GPS+IMU ESKF in NED.
 * Error: δp(3) δv(3) δθ(3) δbg(3) δba(3)
 * Ported/extended from the prior 8-state GpsImuEskf.
 * Pure C++ — no Arduino / Eigen.
 */
#pragma once

#include <stdint.h>

#ifndef ESKF_DEBUG
#define ESKF_DEBUG 1
#endif

#ifndef ESKF_COURSE_MIN_SPEED_MPS
#define ESKF_COURSE_MIN_SPEED_MPS 1.5f
#endif

#ifndef ESKF_OUTLIER_GATE_SIGMA
#define ESKF_OUTLIER_GATE_SIGMA 5.0f
#endif

#ifndef ESKF_CALIB_MS
#define ESKF_CALIB_MS 2000u
#endif

#ifndef ESKF_NIS_GATE_ENABLE
#define ESKF_NIS_GATE_ENABLE 1
#endif

enum class EskfPhase : uint8_t {
  CALIBRATING = 0,
  WAIT_GPS = 1,
  RUNNING = 2,
};

struct EskfImuIn {
  uint32_t ts_us;
  float ax_g, ay_g, az_g;
  float gx_rps, gy_rps, gz_rps;  // rad/s, raw (bias subtracted inside)
};

struct EskfGpsIn {
  float lat_deg, lon_deg;
  float height_m;
  float vn, ve, vd;  // m/s
  float g_speed;     // m/s
  float head_mot_deg;
  float h_acc_m, v_acc_m, s_acc_mps;
  uint8_t fix_type;
  uint8_t num_sv;
};

struct EskfOutput {
  bool valid;
  EskfPhase phase;
  float lat_deg, lon_deg;
  float height_m;
  float vn, ve, vd;
  float yaw_deg, pitch_deg, roll_deg;
  float pos_std_m;
  bool zupt_active;
  bool gps_valid;
  float innov_pos_m;
  float innov_vel_mps;
  float bg[3];
  float ba[3];
  uint32_t zupt_count;
};

class GpsImuEskf {
 public:
  GpsImuEskf();

  void reset();
  void onImu(const EskfImuIn &imu);
  void onGps(const EskfGpsIn &gps);
  EskfOutput output() const;

  EskfPhase phase() const { return phase_; }

 private:
  static constexpr int kN = 15;
  static constexpr int kIp = 0;
  static constexpr int kIv = 3;
  static constexpr int kIth = 6;
  static constexpr int kIbg = 9;
  static constexpr int kIba = 12;

  void predict(float dt, const float f_body[3], const float w_body[3]);
  void correctPosVel(const float p_meas[3], const float v_meas[3],
                     const float r_pos[3], const float r_vel[3]);
  void correctYaw(float yaw_meas_rad, float sigma_rad);
  void zeroVelocityUpdate();
  void injectError();
  void updateStationary(const float gyro_rps[3], const float accel_g[3]);
  void latLonToNed(float lat, float lon, float h, float &n, float &e,
                   float &d) const;
  void nedToLatLon(float &lat, float &lon, float &h) const;
  void quatIntegrate(float dt, const float w[3]);
  void quatToEuler(float &yaw, float &pitch, float &roll) const;
  void bodyToNed(const float vb[3], float vn[3]) const;
  void setAttitudeFromGravity(float ax, float ay, float az);
  void stabilizeCovariance();

  EskfPhase phase_;
  uint32_t calib_start_us_;
  double bg_sum_[3];
  double g_sum_[3];
  uint32_t calib_n_;

  bool has_origin_;
  float lat0_, lon0_, h0_;
  float p_[3];
  float v_[3];
  float q_[4];  // wxyz
  float bg_[3];
  float ba_[3];

  float dx_[kN];
  float P_[kN * kN];

  bool has_last_;
  uint32_t t_prev_us_;

  // stationary detector
  static constexpr int kWin = 32;
  float g_buf_[kWin][3];
  float a_buf_[kWin][3];
  int win_i_;
  int win_n_;
  bool stationary_;
  uint32_t zupt_count_;

  float last_innov_pos_;
  float last_innov_vel_;
  bool gps_valid_;
  uint32_t last_gps_us_;
};
