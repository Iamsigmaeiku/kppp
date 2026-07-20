#include "GpsImuEskf.h"

#include <cmath>
#include <cstdio>
#include <cstring>

namespace {

constexpr float kPi = 3.14159265358979323846f;
constexpr float kDeg2Rad = kPi / 180.0f;
constexpr float kRad2Deg = 180.0f / kPi;
constexpr float kG = 9.80665f;
constexpr float kMPerDegLat = 110540.0f;
constexpr float kMPerDegLonEq = 111320.0f;
constexpr float kDtMinS = 0.001f;
constexpr float kDtMaxS = 0.100f;
constexpr uint32_t kMinSats = 4;

// Process noise (tuning knobs)
constexpr float kQPos = 0.01f;     // m^2 / s  (via F)
constexpr float kQVel = 0.5f;      // (m/s)^2
constexpr float kQYaw = 0.01f;     // rad^2
constexpr float kQBgz = 0.001f;    // (dps)^2
constexpr float kQBa = 1e-5f;      // g^2
constexpr float kVelSigmaBase = 0.8f;
constexpr float kZvuSpeedMps = 0.4f;
// GPS course→yaw 量測噪音：低速時 course 本身雜訊很大，用 1/speed 放大 sigma。
constexpr float kYawSigmaBaseRad = 0.15f;       // ~8.6°
constexpr float kYawSigmaSpeedGain = 0.6f;      // rad·(m/s)

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

constexpr int kN = 8;

void matEye(float *A) {
  std::memset(A, 0, sizeof(float) * kN * kN);
  for (int i = 0; i < kN; i++) A[i * kN + i] = 1.0f;
}

void matMul(const float *A, const float *B, float *C) {
  for (int i = 0; i < kN; i++) {
    for (int j = 0; j < kN; j++) {
      float s = 0.0f;
      for (int k = 0; k < kN; k++) s += A[i * kN + k] * B[k * kN + j];
      C[i * kN + j] = s;
    }
  }
}

// Scratch mats in BSS — avoid blowing sampleTask stack
float gTmp[kN * kN];
float gF[kN * kN];
float gPn[kN * kN];
float gKH[kN * kN];
float gIKH[kN * kN];

void matMulABAt(const float *A, const float *B, float *Out) {
  matMul(A, B, gTmp);
  for (int i = 0; i < kN; i++) {
    for (int j = 0; j < kN; j++) {
      float s = 0.0f;
      for (int k = 0; k < kN; k++) s += gTmp[i * kN + k] * A[j * kN + k];
      Out[i * kN + j] = s;
    }
  }
}

void matAddDiag(float *P, int i, float v) { P[i * kN + i] += v; }

}  // namespace

GpsImuEskf::GpsImuEskf() { reset(); }

void GpsImuEskf::reset() {
  state_ = EskfState::CALIBRATING;
  bias_count_ = 0;
  gz_bias_sum_ = 0.0;
  b_gz_ = 0.0f;
  b_ax_ = 0.0f;
  b_ay_ = 0.0f;
  has_origin_ = false;
  lat0_ = 0.0f;
  lon0_ = 0.0f;
  x_m_ = 0.0f;
  y_m_ = 0.0f;
  vx_ = 0.0f;
  vy_ = 0.0f;
  yaw_rad_ = 0.0f;
  std::memset(dx_, 0, sizeof(dx_));
  matEye(P_);
  // Initial uncertainty
  P_[kIx * kN + kIx] = 25.0f;
  P_[kIy * kN + kIy] = 25.0f;
  P_[kIvx * kN + kIvx] = 4.0f;
  P_[kIvy * kN + kIvy] = 4.0f;
  P_[kIyaw * kN + kIyaw] = 0.25f;
  P_[kIbgz * kN + kIbgz] = 1.0f;
  P_[kIbax * kN + kIbax] = 0.01f;
  P_[kIbay * kN + kIbay] = 0.01f;
  has_last_tick_ = false;
  t_prev_ms_ = 0;
  last_fix_ms_ = 0;
  has_last_fix_ = false;
  last_gps_speed_ = 0.0f;
}

void GpsImuEskf::latLonToLocal(float lat, float lon, float &x, float &y) const {
  const float cos_lat0 = std::cos(lat0_ * kDeg2Rad);
  x = (lon - lon0_) * cos_lat0 * kMPerDegLonEq;
  y = (lat - lat0_) * kMPerDegLat;
}

void GpsImuEskf::localToLatLon(float &lat, float &lon) const {
  const float cos_lat0 = std::cos(lat0_ * kDeg2Rad);
  lat = lat0_ + y_m_ / kMPerDegLat;
  lon = lon0_ + x_m_ / (kMPerDegLonEq * cos_lat0);
}

void GpsImuEskf::bodyHorizG(float ax_g, float az_g, float &a_fwd,
                            float &a_lat) const {
  const float yaw = DR_IMU_MOUNT_YAW_DEG * kDeg2Rad;
  // Board: X≈lon, Z≈lat after gravity on +Y removed by caller/bias
  a_fwd = ax_g * std::cos(yaw) + az_g * std::sin(yaw);
  a_lat = -ax_g * std::sin(yaw) + az_g * std::cos(yaw);
}

EskfOutput GpsImuEskf::makeOutput(bool valid) const {
  EskfOutput o{};
  o.valid = valid;
  o.state = state_;
  o.bias_samples = bias_count_;
  o.speed_mps = std::sqrt(vx_ * vx_ + vy_ * vy_);
  o.heading_deg = wrap360(yaw_rad_ * kRad2Deg);
  if (valid && has_origin_) {
    localToLatLon(o.lat_dr, o.lon_dr);
  } else {
    o.lat_dr = 0.0f;
    o.lon_dr = 0.0f;
  }
  return o;
}

void GpsImuEskf::predictCov(float dt, float a_fwd_mps2, float a_lat_mps2) {
  // Discrete F ≈ I + F_c * dt
  // pos <- vel; vel <- yaw (cross with accel); yaw <- -b_gz (dps→rad)
  matEye(gF);
  gF[kIx * kN + kIvx] = dt;
  gF[kIy * kN + kIvy] = dt;

  const float s = std::sin(yaw_rad_);
  const float c = std::cos(yaw_rad_);
  // a_e = a_fwd*s + a_lat*c; a_n = a_fwd*c - a_lat*s
  // da_e/dyaw = a_fwd*c - a_lat*s; da_n/dyaw = -a_fwd*s - a_lat*c
  gF[kIvx * kN + kIyaw] = (a_fwd_mps2 * c - a_lat_mps2 * s) * dt;
  gF[kIvy * kN + kIyaw] = (-a_fwd_mps2 * s - a_lat_mps2 * c) * dt;
  gF[kIvx * kN + kIbax] = -s * kG * dt;  // b_ax along fwd (approx)
  gF[kIvy * kN + kIbax] = -c * kG * dt;
  gF[kIvx * kN + kIbay] = -c * kG * dt;  // b_ay along lat
  gF[kIvy * kN + kIbay] = s * kG * dt;
  gF[kIyaw * kN + kIbgz] = -kDeg2Rad * dt;

  matMulABAt(gF, P_, gPn);
  std::memcpy(P_, gPn, sizeof(float) * kN * kN);

  matAddDiag(P_, kIx, kQPos * dt);
  matAddDiag(P_, kIy, kQPos * dt);
  matAddDiag(P_, kIvx, kQVel * dt);
  matAddDiag(P_, kIvy, kQVel * dt);
  matAddDiag(P_, kIyaw, kQYaw * dt);
  matAddDiag(P_, kIbgz, kQBgz * dt);
  matAddDiag(P_, kIbax, kQBa * dt);
  matAddDiag(P_, kIbay, kQBa * dt);
}

void GpsImuEskf::injectError() {
  x_m_ += dx_[kIx];
  y_m_ += dx_[kIy];
  vx_ += dx_[kIvx];
  vy_ += dx_[kIvy];
  yaw_rad_ = wrapPi(yaw_rad_ + dx_[kIyaw]);
  b_gz_ += dx_[kIbgz];
  b_ax_ += dx_[kIbax];
  b_ay_ += dx_[kIbay];
  std::memset(dx_, 0, sizeof(dx_));
}

void GpsImuEskf::correctPosition(float x_gps, float y_gps, float sigma_m) {
  // z = [x, y]; H picks pos; R = sigma^2 I
  const float innov_x = x_gps - x_m_;
  const float innov_y = y_gps - y_m_;
  const float r = sigma_m * sigma_m;
  if (r < 1e-6f) return;

  // S = H P H' + R  (2x2); H = [[1,0,...],[0,1,...]]
  const float s00 = P_[kIx * kN + kIx] + r;
  const float s01 = P_[kIx * kN + kIy];
  const float s10 = P_[kIy * kN + kIx];
  const float s11 = P_[kIy * kN + kIy] + r;
  const float det = s00 * s11 - s01 * s10;
  if (std::fabs(det) < 1e-9f) return;
  const float inv00 = s11 / det;
  const float inv01 = -s01 / det;
  const float inv10 = -s10 / det;
  const float inv11 = s00 / det;

  // K = P H' S^{-1}  → columns for x,y measurements
  float Kx[kN], Ky[kN];
  for (int i = 0; i < kN; i++) {
    const float pix = P_[i * kN + kIx];
    const float piy = P_[i * kN + kIy];
    Kx[i] = pix * inv00 + piy * inv10;
    Ky[i] = pix * inv01 + piy * inv11;
  }

  for (int i = 0; i < kN; i++) {
    dx_[i] += Kx[i] * innov_x + Ky[i] * innov_y;
  }

  // P = (I - K H) P
  std::memset(gKH, 0, sizeof(gKH));
  for (int i = 0; i < kN; i++) {
    gKH[i * kN + kIx] = Kx[i];
    gKH[i * kN + kIy] = Ky[i];
  }
  matEye(gIKH);
  for (int i = 0; i < kN * kN; i++) gIKH[i] -= gKH[i];
  matMul(gIKH, P_, gPn);
  std::memcpy(P_, gPn, sizeof(float) * kN * kN);

  injectError();
}

void GpsImuEskf::correctVelocity(float vx_gps, float vy_gps, float sigma_mps) {
  const float innov_x = vx_gps - vx_;
  const float innov_y = vy_gps - vy_;
  const float r = sigma_mps * sigma_mps;
  if (r < 1e-6f) return;

  const float s00 = P_[kIvx * kN + kIvx] + r;
  const float s01 = P_[kIvx * kN + kIvy];
  const float s10 = P_[kIvy * kN + kIvx];
  const float s11 = P_[kIvy * kN + kIvy] + r;
  const float det = s00 * s11 - s01 * s10;
  if (std::fabs(det) < 1e-9f) return;
  const float inv00 = s11 / det;
  const float inv01 = -s01 / det;
  const float inv10 = -s10 / det;
  const float inv11 = s00 / det;

  float Kx[kN], Ky[kN];
  for (int i = 0; i < kN; i++) {
    const float pix = P_[i * kN + kIvx];
    const float piy = P_[i * kN + kIvy];
    Kx[i] = pix * inv00 + piy * inv10;
    Ky[i] = pix * inv01 + piy * inv11;
  }
  for (int i = 0; i < kN; i++) {
    dx_[i] += Kx[i] * innov_x + Ky[i] * innov_y;
  }

  std::memset(gKH, 0, sizeof(gKH));
  for (int i = 0; i < kN; i++) {
    gKH[i * kN + kIvx] = Kx[i];
    gKH[i * kN + kIvy] = Ky[i];
  }
  matEye(gIKH);
  for (int i = 0; i < kN * kN; i++) gIKH[i] -= gKH[i];
  matMul(gIKH, P_, gPn);
  std::memcpy(P_, gPn, sizeof(float) * kN * kN);

  injectError();
}

void GpsImuEskf::correctYaw(float yaw_gps_rad, float sigma_rad) {
  // 純量量測（H 只挑 yaw 這一維），流程跟 correctPosition/correctVelocity
  // 一致：K = P Hᵀ S⁻¹、P = (I-KH)P，取代原本繞過協方差矩陣的 ad-hoc 混合。
  const float r = sigma_rad * sigma_rad;
  if (r < 1e-9f) return;

  const float innov = wrapPi(yaw_gps_rad - yaw_rad_);
  const float s = P_[kIyaw * kN + kIyaw] + r;
  if (s < 1e-9f) return;
  const float inv_s = 1.0f / s;

  float K[kN];
  for (int i = 0; i < kN; i++) {
    K[i] = P_[i * kN + kIyaw] * inv_s;
  }
  for (int i = 0; i < kN; i++) {
    dx_[i] += K[i] * innov;
  }

  std::memset(gKH, 0, sizeof(gKH));
  for (int i = 0; i < kN; i++) {
    gKH[i * kN + kIyaw] = K[i];
  }
  matEye(gIKH);
  for (int i = 0; i < kN * kN; i++) gIKH[i] -= gKH[i];
  matMul(gIKH, P_, gPn);
  std::memcpy(P_, gPn, sizeof(float) * kN * kN);

  injectError();
}

void GpsImuEskf::zeroVelocityUpdate() {
  correctVelocity(0.0f, 0.0f, 0.3f);
}

void GpsImuEskf::onGpsFix(uint32_t t_ms, float lat, float lon, float speed_mps,
                          float course_deg, float hdop, uint32_t sat_count) {
  if (sat_count < kMinSats) return;
  if (!finiteF(lat) || !finiteF(lon)) return;

  const float speed = finiteF(speed_mps) ? speed_mps : 0.0f;
  last_gps_speed_ = speed;

  if (!has_origin_) {
    lat0_ = lat;
    lon0_ = lon;
    x_m_ = 0.0f;
    y_m_ = 0.0f;
    has_origin_ = true;
    if (speed > ESKF_COURSE_MIN_SPEED_MPS && finiteF(course_deg)) {
      yaw_rad_ = course_deg * kDeg2Rad;
      vx_ = speed * std::sin(yaw_rad_);
      vy_ = speed * std::cos(yaw_rad_);
    } else {
      vx_ = 0.0f;
      vy_ = 0.0f;
    }
    last_fix_ms_ = t_ms;
    has_last_fix_ = true;
#if ESKF_DEBUG
    std::printf("[eskf] origin lat=%.7f lon=%.7f v=%.2f\n", lat, lon, speed);
#endif
    return;
  }

  if (state_ != EskfState::RUNNING) {
    last_fix_ms_ = t_ms;
    has_last_fix_ = true;
    return;
  }

  float x_gps = 0.0f, y_gps = 0.0f;
  latLonToLocal(lat, lon, x_gps, y_gps);

  float sigma = ESKF_POS_SIGMA_MIN_M;
  if (finiteF(hdop) && hdop > 0.0f) {
    const float from_hdop = hdop * ESKF_HDOP_SCALE;
    if (from_hdop > sigma) sigma = from_hdop;
  }

  const float innov =
      std::sqrt((x_gps - x_m_) * (x_gps - x_m_) + (y_gps - y_m_) * (y_gps - y_m_));
  const float gate = ESKF_OUTLIER_GATE_SIGMA * sigma;
  if (innov > gate && speed < ESKF_COURSE_MIN_SPEED_MPS) {
#if ESKF_DEBUG
    std::printf("[eskf] GPS outlier skip innov=%.2f gate=%.2f\n", innov, gate);
#endif
    last_fix_ms_ = t_ms;
    has_last_fix_ = true;
    return;
  }

  correctPosition(x_gps, y_gps, sigma);

  if (speed > ESKF_COURSE_MIN_SPEED_MPS && finiteF(course_deg)) {
    const float h = course_deg * kDeg2Rad;
    const float vx_g = speed * std::sin(h);
    const float vy_g = speed * std::cos(h);
    const float sig_v = kVelSigmaBase + 0.05f * speed;
    correctVelocity(vx_g, vy_g, sig_v);

    const float sig_yaw = kYawSigmaBaseRad + kYawSigmaSpeedGain / speed;
    correctYaw(h, sig_yaw);
  }

  last_fix_ms_ = t_ms;
  has_last_fix_ = true;

#if ESKF_DEBUG
  std::printf("[eskf] gps_fix x=%.2f y=%.2f v=%.2f hdg=%.1f sig=%.2f sats=%u\n",
              x_m_, y_m_, speed, wrap360(yaw_rad_ * kRad2Deg), sigma,
              (unsigned)sat_count);
#endif
}

EskfOutput GpsImuEskf::tick(uint32_t t_ms, float ax_g, float /*ay_g*/, float az_g,
                            float /*gx_dps*/, float /*gy_dps*/, float gz_dps) {
  if (state_ == EskfState::CALIBRATING) {
    if (finiteF(gz_dps)) {
      gz_bias_sum_ += static_cast<double>(gz_dps);
      bias_count_++;
    }
    // Accumulate accel bias estimate (expect ~0 on horizontal axes at rest)
    if (finiteF(ax_g) && finiteF(az_g) && bias_count_ > 0) {
      const float n = static_cast<float>(bias_count_);
      b_ax_ += (ax_g - b_ax_) / n;
      b_ay_ += (az_g - b_ay_) / n;  // stored as "bay" = body Z horiz
    }
    if (bias_count_ >= ESKF_GYRO_BIAS_SAMPLES) {
      b_gz_ = static_cast<float>(gz_bias_sum_ / static_cast<double>(bias_count_));
      state_ = EskfState::RUNNING;
#if ESKF_DEBUG
      std::printf("[eskf] CALIBRATING done bias_gz=%.4f dps b_ax=%.4f b_az=%.4f\n",
                  b_gz_, b_ax_, b_ay_);
#endif
    }
    has_last_tick_ = true;
    t_prev_ms_ = t_ms;
    return makeOutput(false);
  }

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

  const float gz_cal = (finiteF(gz_dps) ? gz_dps : b_gz_) - b_gz_;
  yaw_rad_ = wrapPi(yaw_rad_ + (gz_cal * kDeg2Rad) * dt);

  float a_fwd_g = 0.0f, a_lat_g = 0.0f;
  bodyHorizG((finiteF(ax_g) ? ax_g : 0.0f) - b_ax_,
             (finiteF(az_g) ? az_g : 0.0f) - b_ay_, a_fwd_g, a_lat_g);
  const float a_fwd = a_fwd_g * kG;
  const float a_lat = a_lat_g * kG;
  const float s = std::sin(yaw_rad_);
  const float c = std::cos(yaw_rad_);
  const float a_e = a_fwd * s + a_lat * c;
  const float a_n = a_fwd * c - a_lat * s;

  vx_ += a_e * dt;
  vy_ += a_n * dt;
  x_m_ += vx_ * dt;
  y_m_ += vy_ * dt;

  predictCov(dt, a_fwd, a_lat);

  // ZVU when GPS says nearly stopped
  if (has_last_fix_ && last_gps_speed_ < kZvuSpeedMps &&
      (t_ms - last_fix_ms_) < 2000u) {
    const float spd = std::sqrt(vx_ * vx_ + vy_ * vy_);
    if (spd < 1.5f) zeroVelocityUpdate();
  }

  return makeOutput(true);
}
