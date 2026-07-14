#include "DeadReckoner.h"

#include <cmath>
#include <cstdio>

namespace {

constexpr float kPi = 3.14159265358979323846f;
constexpr float kDeg2Rad = kPi / 180.0f;
constexpr float kRad2Deg = 180.0f / kPi;
constexpr float kG = 9.80665f;
constexpr float kMPerDegLat = 110540.0f;
constexpr float kMPerDegLonEq = 111320.0f;
constexpr float kDtMinS = 0.001f;
constexpr float kDtMaxS = 0.100f;
constexpr float kVClampAbsWhenSlow = 0.5f;
constexpr uint32_t kMinSats = 4;

float wrapPi(float a) {
  while (a > kPi) a -= 2.0f * kPi;
  while (a < -kPi) a += 2.0f * kPi;
  return a;
}

float wrap360(float deg) {
  while (deg < 0.0f) deg += 360.0f;
  while (deg >= 360.0f) deg -= 360.0f;
  return deg;
}

bool finiteF(float v) { return std::isfinite(v); }

}  // namespace

DeadReckoner::DeadReckoner() { reset(); }

void DeadReckoner::reset() {
  state_ = DrState::CALIBRATING;
  bias_count_ = 0;
  gz_bias_sum_ = 0.0;
  gz_bias_dps_ = 0.0f;
  has_origin_ = false;
  lat0_ = 0.0f;
  lon0_ = 0.0f;
  x_m_ = 0.0f;
  y_m_ = 0.0f;
  heading_rad_ = 0.0f;
  v_mps_ = 0.0f;
  v_last_gps_ = 0.0f;
  has_last_tick_ = false;
  t_prev_ms_ = 0;
  last_fix_ms_ = 0;
  has_last_fix_ = false;
}

void DeadReckoner::latLonToLocal(float lat, float lon, float &x, float &y) const {
  const float cos_lat0 = std::cos(lat0_ * kDeg2Rad);
  x = (lon - lon0_) * cos_lat0 * kMPerDegLonEq;
  y = (lat - lat0_) * kMPerDegLat;
}

void DeadReckoner::localToLatLon(float &lat, float &lon) const {
  const float cos_lat0 = std::cos(lat0_ * kDeg2Rad);
  lat = lat0_ + y_m_ / kMPerDegLat;
  lon = lon0_ + x_m_ / (kMPerDegLonEq * cos_lat0);
}

float DeadReckoner::axForwardG(float ax_g, float ay_g) const {
  const float yaw = DR_IMU_MOUNT_YAW_DEG * kDeg2Rad;
  // Rotate body accel into vehicle frame; take forward (x) component.
  return ax_g * std::cos(yaw) + ay_g * std::sin(yaw);
}

DrOutput DeadReckoner::makeOutput(bool valid) const {
  DrOutput o{};
  o.valid = valid;
  o.state = state_;
  o.bias_samples = bias_count_;
  o.speed_mps = v_mps_;
  o.heading_deg = wrap360(heading_rad_ * kRad2Deg);
  if (valid && has_origin_) {
    localToLatLon(o.lat_dr, o.lon_dr);
  } else {
    o.lat_dr = 0.0f;
    o.lon_dr = 0.0f;
  }
  return o;
}

void DeadReckoner::onGpsFix(uint32_t t_ms, float lat, float lon, float speed_mps,
                            float course_deg, uint32_t sat_count) {
  if (sat_count < kMinSats) return;
  if (!finiteF(lat) || !finiteF(lon)) return;

  const float speed = finiteF(speed_mps) ? speed_mps : 0.0f;

  if (!has_origin_) {
    lat0_ = lat;
    lon0_ = lon;
    x_m_ = 0.0f;
    y_m_ = 0.0f;
    has_origin_ = true;
    v_last_gps_ = speed;
    v_mps_ = speed;
    if (speed > DR_COURSE_MIN_SPEED_MPS && finiteF(course_deg)) {
      heading_rad_ = course_deg * kDeg2Rad;
    }
    last_fix_ms_ = t_ms;
    has_last_fix_ = true;
#if DR_DEBUG
    std::printf("[dr] origin lat=%.7f lon=%.7f v=%.2f\n", lat, lon, speed);
#endif
    return;
  }

  float x_gps = 0.0f;
  float y_gps = 0.0f;
  latLonToLocal(lat, lon, x_gps, y_gps);

  const float x_before = x_m_;
  const float y_before = y_m_;

  v_last_gps_ = speed;
  v_mps_ = speed;  // hard reset each GPS period

  if (speed > DR_COURSE_MIN_SPEED_MPS && finiteF(course_deg)) {
    const float h_gps = course_deg * kDeg2Rad;
    heading_rad_ = wrapPi(heading_rad_ + DR_HDG_BLEND * wrapPi(h_gps - heading_rad_));
  }

  x_m_ = (1.0f - DR_POS_BLEND) * x_m_ + DR_POS_BLEND * x_gps;
  y_m_ = (1.0f - DR_POS_BLEND) * y_m_ + DR_POS_BLEND * y_gps;

  last_fix_ms_ = t_ms;
  has_last_fix_ = true;

#if DR_DEBUG
  std::printf(
      "[dr] gps_fix x: %.2f -> %.2f (d=%+.2f)  y: %.2f -> %.2f (d=%+.2f)  "
      "v=%.2f hdg=%.1f sats=%u\n",
      x_before, x_m_, x_m_ - x_before, y_before, y_m_, y_m_ - y_before, v_mps_,
      wrap360(heading_rad_ * kRad2Deg), (unsigned)sat_count);
#else
  (void)x_before;
  (void)y_before;
#endif
}

DrOutput DeadReckoner::tick(uint32_t t_ms, float ax_g, float ay_g, float /*az_g*/,
                            float /*gx_dps*/, float /*gy_dps*/, float gz_dps) {
  if (state_ == DrState::CALIBRATING) {
    if (finiteF(gz_dps)) {
      gz_bias_sum_ += static_cast<double>(gz_dps);
      bias_count_++;
    }
    if (bias_count_ >= DR_GYRO_BIAS_SAMPLES) {
      gz_bias_dps_ =
          static_cast<float>(gz_bias_sum_ / static_cast<double>(bias_count_));
      state_ = DrState::RUNNING;
#if DR_DEBUG
      std::printf("[dr] CALIBRATING done bias_gz=%.4f dps (n=%u)\n", gz_bias_dps_,
                  (unsigned)bias_count_);
#endif
    }
    has_last_tick_ = true;
    t_prev_ms_ = t_ms;
    return makeOutput(false);
  }

  // RUNNING
  float dt = 0.0f;
  if (has_last_tick_) {
    dt = static_cast<float>(t_ms - t_prev_ms_) * 0.001f;
    if (dt < kDtMinS) dt = kDtMinS;
    if (dt > kDtMaxS) dt = kDtMaxS;
  }
  has_last_tick_ = true;
  t_prev_ms_ = t_ms;

  if (!has_origin_) {
    return makeOutput(false);
  }

  const float gz_cal = (finiteF(gz_dps) ? gz_dps : gz_bias_dps_) - gz_bias_dps_;
  heading_rad_ = wrapPi(heading_rad_ + (gz_cal * kDeg2Rad) * dt);

  const bool fix_fresh =
      has_last_fix_ && (t_ms - last_fix_ms_ < DR_GPS_FIX_MAX_AGE_MS);

  if (fix_fresh) {
    const float dt_fix = static_cast<float>(t_ms - last_fix_ms_) * 0.001f;
    const float ax_fwd = axForwardG(ax_g, ay_g);
    float v_corr = v_last_gps_ + ax_fwd * kG * dt_fix;

    const float vmax_delta =
        (std::fabs(v_last_gps_) > 1e-3f) ? (0.3f * std::fabs(v_last_gps_))
                                         : kVClampAbsWhenSlow;
    if (v_corr > v_last_gps_ + vmax_delta) v_corr = v_last_gps_ + vmax_delta;
    if (v_corr < v_last_gps_ - vmax_delta) v_corr = v_last_gps_ - vmax_delta;
    if (v_corr < 0.0f) v_corr = 0.0f;
    v_mps_ = v_corr;
  }
  // else: freeze v_mps_

  x_m_ += v_mps_ * std::sin(heading_rad_) * dt;
  y_m_ += v_mps_ * std::cos(heading_rad_) * dt;

  return makeOutput(true);
}
