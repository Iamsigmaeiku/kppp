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
constexpr float kDtMinS = 0.0005f;
constexpr float kDtMaxS = 0.050f;

constexpr float kQPos = 0.02f;
constexpr float kQVel = 0.8f;
constexpr float kQAtt = 1e-4f;
constexpr float kQBg = 1e-6f;
constexpr float kQBa = 1e-5f;

constexpr float kGyroVarTh = 0.02f * 0.02f;   // (rad/s)^2
constexpr float kAccelVarTh = 0.05f * 0.05f;  // g^2
constexpr float kPosSigmaFloorM = 0.5f;
constexpr float kVelSigmaFloor = 0.1f;

constexpr int kN = 15;

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

void quatNormalize(float q[4]) {
  const float n =
      std::sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3]);
  if (n < 1e-9f) {
    q[0] = 1;
    q[1] = q[2] = q[3] = 0;
    return;
  }
  q[0] /= n;
  q[1] /= n;
  q[2] /= n;
  q[3] /= n;
}

}  // namespace

GpsImuEskf::GpsImuEskf() { reset(); }

void GpsImuEskf::reset() {
  phase_ = EskfPhase::CALIBRATING;
  calib_start_us_ = 0;
  calib_n_ = 0;
  for (int i = 0; i < 3; i++) {
    bg_sum_[i] = 0;
    g_sum_[i] = 0;
    bg_[i] = 0;
    ba_[i] = 0;
    p_[i] = 0;
    v_[i] = 0;
  }
  q_[0] = 1;
  q_[1] = q_[2] = q_[3] = 0;
  has_origin_ = false;
  lat0_ = lon0_ = h0_ = 0;
  std::memset(dx_, 0, sizeof(dx_));
  matEye(P_);
  for (int i = 0; i < 3; i++) {
    P_[(kIp + i) * kN + (kIp + i)] = 25.0f;
    P_[(kIv + i) * kN + (kIv + i)] = 4.0f;
    P_[(kIth + i) * kN + (kIth + i)] = 0.25f;
    P_[(kIbg + i) * kN + (kIbg + i)] = 1e-3f;
    P_[(kIba + i) * kN + (kIba + i)] = 0.01f;
  }
  has_last_ = false;
  t_prev_us_ = 0;
  win_i_ = win_n_ = 0;
  stationary_ = false;
  zupt_count_ = 0;
  last_innov_pos_ = last_innov_vel_ = 0;
  gps_valid_ = false;
  last_gps_us_ = 0;
  std::memset(g_buf_, 0, sizeof(g_buf_));
  std::memset(a_buf_, 0, sizeof(a_buf_));
}

void GpsImuEskf::latLonToNed(float lat, float lon, float h, float &n, float &e,
                             float &d) const {
  const float cos_lat0 = std::cos(lat0_ * kDeg2Rad);
  n = (lat - lat0_) * kMPerDegLat;
  e = (lon - lon0_) * cos_lat0 * kMPerDegLonEq;
  d = (h0_ - h);  // NED down: above origin → negative d
}

void GpsImuEskf::nedToLatLon(float &lat, float &lon, float &h) const {
  const float cos_lat0 = std::cos(lat0_ * kDeg2Rad);
  lat = lat0_ + p_[0] / kMPerDegLat;
  lon = lon0_ + p_[1] / (kMPerDegLonEq * cos_lat0);
  h = h0_ - p_[2];
}

void GpsImuEskf::bodyToNed(const float vb[3], float vn[3]) const {
  // C_bn from quaternion (body → NED)
  const float w = q_[0], x = q_[1], y = q_[2], z = q_[3];
  const float r00 = 1 - 2 * (y * y + z * z);
  const float r01 = 2 * (x * y - z * w);
  const float r02 = 2 * (x * z + y * w);
  const float r10 = 2 * (x * y + z * w);
  const float r11 = 1 - 2 * (x * x + z * z);
  const float r12 = 2 * (y * z - x * w);
  const float r20 = 2 * (x * z - y * w);
  const float r21 = 2 * (y * z + x * w);
  const float r22 = 1 - 2 * (x * x + y * y);
  vn[0] = r00 * vb[0] + r01 * vb[1] + r02 * vb[2];
  vn[1] = r10 * vb[0] + r11 * vb[1] + r12 * vb[2];
  vn[2] = r20 * vb[0] + r21 * vb[1] + r22 * vb[2];
}

void GpsImuEskf::quatIntegrate(float dt, const float w[3]) {
  // q_dot = 0.5 * q ⊗ ω
  const float qw = q_[0], qx = q_[1], qy = q_[2], qz = q_[3];
  const float wx = w[0], wy = w[1], wz = w[2];
  float dq[4];
  dq[0] = 0.5f * (-qx * wx - qy * wy - qz * wz);
  dq[1] = 0.5f * (qw * wx + qy * wz - qz * wy);
  dq[2] = 0.5f * (qw * wy - qx * wz + qz * wx);
  dq[3] = 0.5f * (qw * wz + qx * wy - qy * wx);
  q_[0] += dq[0] * dt;
  q_[1] += dq[1] * dt;
  q_[2] += dq[2] * dt;
  q_[3] += dq[3] * dt;
  quatNormalize(q_);
}

void GpsImuEskf::quatToEuler(float &yaw, float &pitch, float &roll) const {
  const float w = q_[0], x = q_[1], y = q_[2], z = q_[3];
  // ZYX yaw-pitch-roll, yaw from North
  const float sinr = 2 * (w * x + y * z);
  const float cosr = 1 - 2 * (x * x + y * y);
  roll = std::atan2(sinr, cosr);
  float sinp = 2 * (w * y - z * x);
  if (std::fabs(sinp) >= 1)
    pitch = std::copysign(kPi / 2, sinp);
  else
    pitch = std::asin(sinp);
  const float siny = 2 * (w * z + x * y);
  const float cosy = 1 - 2 * (y * y + z * z);
  yaw = std::atan2(siny, cosy);
}

void GpsImuEskf::setAttitudeFromGravity(float ax, float ay, float az) {
  // accel ≈ -g in body when static if NED and accel measures specific force?
  // ICM reports +1g when axis points up (typical). In NED, gravity vector in
  // nav is [0,0,+g] (down). Specific force f = a - g_body... For level:
  // roll/pitch from accel assuming stationary, yaw left at 0.
  const float an =
      std::sqrt(ax * ax + ay * ay + az * az);
  if (an < 0.1f) return;
  const float nx = ax / an;
  const float ny = ay / an;
  const float nz = az / an;
  // Assume accel reads +1g on axis opposite gravity when upright (Z up board).
  // Map: pitch/roll so that body -Z aligns with NED down if board flat Z-up.
  // Simplified: roll = atan2(ay, az), pitch = atan2(-ax, sqrt(ay^2+az^2))
  const float roll = std::atan2(ny, nz);
  const float pitch = std::atan2(-nx, std::sqrt(ny * ny + nz * nz));
  const float yaw = 0.0f;
  const float cr = std::cos(roll * 0.5f), sr = std::sin(roll * 0.5f);
  const float cp = std::cos(pitch * 0.5f), sp = std::sin(pitch * 0.5f);
  const float cy = std::cos(yaw * 0.5f), sy = std::sin(yaw * 0.5f);
  q_[0] = cr * cp * cy + sr * sp * sy;
  q_[1] = sr * cp * cy - cr * sp * sy;
  q_[2] = cr * sp * cy + sr * cp * sy;
  q_[3] = cr * cp * sy - sr * sp * cy;
  quatNormalize(q_);
}

void GpsImuEskf::updateStationary(const float gyro_rps[3],
                                  const float accel_g[3]) {
  for (int i = 0; i < 3; i++) {
    g_buf_[win_i_][i] = gyro_rps[i];
    a_buf_[win_i_][i] = accel_g[i];
  }
  win_i_ = (win_i_ + 1) % kWin;
  if (win_n_ < kWin) win_n_++;
  if (win_n_ < kWin / 2) {
    stationary_ = false;
    return;
  }
  float mg[3] = {0, 0, 0}, ma[3] = {0, 0, 0};
  for (int i = 0; i < win_n_; i++) {
    for (int j = 0; j < 3; j++) {
      mg[j] += g_buf_[i][j];
      ma[j] += a_buf_[i][j];
    }
  }
  const float inv = 1.0f / (float)win_n_;
  for (int j = 0; j < 3; j++) {
    mg[j] *= inv;
    ma[j] *= inv;
  }
  float vg = 0, va = 0;
  for (int i = 0; i < win_n_; i++) {
    for (int j = 0; j < 3; j++) {
      const float dg = g_buf_[i][j] - mg[j];
      const float da = a_buf_[i][j] - ma[j];
      vg += dg * dg;
      va += da * da;
    }
  }
  vg /= (float)(win_n_ * 3);
  va /= (float)(win_n_ * 3);
  stationary_ = (vg < kGyroVarTh && va < kAccelVarTh);
}

void GpsImuEskf::injectError() {
  p_[0] += dx_[kIp];
  p_[1] += dx_[kIp + 1];
  p_[2] += dx_[kIp + 2];
  v_[0] += dx_[kIv];
  v_[1] += dx_[kIv + 1];
  v_[2] += dx_[kIv + 2];
  // small-angle: q ← δθ ⊗ q
  const float dth[3] = {dx_[kIth], dx_[kIth + 1], dx_[kIth + 2]};
  float dq[4] = {1.0f, 0.5f * dth[0], 0.5f * dth[1], 0.5f * dth[2]};
  quatNormalize(dq);
  const float qw = q_[0], qx = q_[1], qy = q_[2], qz = q_[3];
  q_[0] = dq[0] * qw - dq[1] * qx - dq[2] * qy - dq[3] * qz;
  q_[1] = dq[0] * qx + dq[1] * qw + dq[2] * qz - dq[3] * qy;
  q_[2] = dq[0] * qy - dq[1] * qz + dq[2] * qw + dq[3] * qx;
  q_[3] = dq[0] * qz + dq[1] * qy - dq[2] * qx + dq[3] * qw;
  quatNormalize(q_);
  for (int i = 0; i < 3; i++) {
    bg_[i] += dx_[kIbg + i];
    ba_[i] += dx_[kIba + i];
  }
  std::memset(dx_, 0, sizeof(dx_));
}

void GpsImuEskf::predict(float dt, const float f_body[3], const float w_body[3]) {
  // nominal
  float f_ned[3];
  bodyToNed(f_body, f_ned);
  f_ned[2] += kG;  // gravity in NED down
  v_[0] += f_ned[0] * dt;
  v_[1] += f_ned[1] * dt;
  v_[2] += f_ned[2] * dt;
  p_[0] += v_[0] * dt;
  p_[1] += v_[1] * dt;
  p_[2] += v_[2] * dt;
  quatIntegrate(dt, w_body);

  // F ≈ I + Fc dt
  matEye(gF);
  // δp ← δv
  for (int i = 0; i < 3; i++) gF[(kIp + i) * kN + (kIv + i)] = dt;
  // δv ← -[f_ned×] δθ - C_bn δba
  // skew(f) * dth
  const float fx = f_ned[0], fy = f_ned[1], fz = f_ned[2];
  gF[(kIv + 0) * kN + (kIth + 1)] = fz * dt;
  gF[(kIv + 0) * kN + (kIth + 2)] = -fy * dt;
  gF[(kIv + 1) * kN + (kIth + 0)] = -fz * dt;
  gF[(kIv + 1) * kN + (kIth + 2)] = fx * dt;
  gF[(kIv + 2) * kN + (kIth + 0)] = fy * dt;
  gF[(kIv + 2) * kN + (kIth + 1)] = -fx * dt;
  // C_bn columns for ba (approx identity * dt scaled by g for ba in g-units)
  // f_body already in m/s^2; ba stored in g → convert
  float C[9];
  {
    const float w = q_[0], x = q_[1], y = q_[2], z = q_[3];
    C[0] = 1 - 2 * (y * y + z * z);
    C[1] = 2 * (x * y - z * w);
    C[2] = 2 * (x * z + y * w);
    C[3] = 2 * (x * y + z * w);
    C[4] = 1 - 2 * (x * x + z * z);
    C[5] = 2 * (y * z - x * w);
    C[6] = 2 * (x * z - y * w);
    C[7] = 2 * (y * z + x * w);
    C[8] = 1 - 2 * (x * x + y * y);
  }
  for (int i = 0; i < 3; i++) {
    for (int j = 0; j < 3; j++) {
      gF[(kIv + i) * kN + (kIba + j)] = -C[i * 3 + j] * kG * dt;
    }
  }
  // δθ ← -δbg
  for (int i = 0; i < 3; i++) gF[(kIth + i) * kN + (kIbg + i)] = -dt;

  matMulABAt(gF, P_, gPn);
  std::memcpy(P_, gPn, sizeof(float) * kN * kN);
  for (int i = 0; i < 3; i++) {
    matAddDiag(P_, kIp + i, kQPos * dt);
    matAddDiag(P_, kIv + i, kQVel * dt);
    matAddDiag(P_, kIth + i, kQAtt * dt);
    matAddDiag(P_, kIbg + i, kQBg * dt);
    matAddDiag(P_, kIba + i, kQBa * dt);
  }
}

void GpsImuEskf::correctPosVel(const float p_meas[3], const float v_meas[3],
                               const float r_pos[3], const float r_vel[3]) {
  const float z[6] = {p_meas[0], p_meas[1], p_meas[2],
                      v_meas[0], v_meas[1], v_meas[2]};
  const float R[6] = {r_pos[0], r_pos[1], r_pos[2],
                      r_vel[0], r_vel[1], r_vel[2]};
  const int idx[6] = {kIp, kIp + 1, kIp + 2, kIv, kIv + 1, kIv + 2};

  float innov_p = 0, innov_v = 0;
  for (int m = 0; m < 6; m++) {
    const float pred =
        (m < 3) ? p_[m] : v_[m - 3];
    const int ix = idx[m];
    const float innov = z[m] - pred;
    if (m < 3)
      innov_p += innov * innov;
    else
      innov_v += innov * innov;
    const float r = R[m];
    if (r < 1e-8f) continue;
    const float s = P_[ix * kN + ix] + r;
    if (s < 1e-9f) continue;
    const float inv_s = 1.0f / s;
    float K[kN];
    for (int i = 0; i < kN; i++) K[i] = P_[i * kN + ix] * inv_s;
    for (int i = 0; i < kN; i++) dx_[i] += K[i] * innov;
    std::memset(gKH, 0, sizeof(gKH));
    for (int i = 0; i < kN; i++) gKH[i * kN + ix] = K[i];
    matEye(gIKH);
    for (int i = 0; i < kN * kN; i++) gIKH[i] -= gKH[i];
    matMul(gIKH, P_, gPn);
    std::memcpy(P_, gPn, sizeof(float) * kN * kN);
    injectError();
  }
  last_innov_pos_ = std::sqrt(innov_p);
  last_innov_vel_ = std::sqrt(innov_v);
}

void GpsImuEskf::correctYaw(float yaw_meas_rad, float sigma_rad) {
  float yaw, pitch, roll;
  quatToEuler(yaw, pitch, roll);
  const float r = sigma_rad * sigma_rad;
  if (r < 1e-9f) return;
  const float innov = wrapPi(yaw_meas_rad - yaw);
  const int ix = kIth + 2;  // yaw about Down
  const float s = P_[ix * kN + ix] + r;
  if (s < 1e-9f) return;
  const float inv_s = 1.0f / s;
  float K[kN];
  for (int i = 0; i < kN; i++) K[i] = P_[i * kN + ix] * inv_s;
  for (int i = 0; i < kN; i++) dx_[i] += K[i] * innov;
  std::memset(gKH, 0, sizeof(gKH));
  for (int i = 0; i < kN; i++) gKH[i * kN + ix] = K[i];
  matEye(gIKH);
  for (int i = 0; i < kN * kN; i++) gIKH[i] -= gKH[i];
  matMul(gIKH, P_, gPn);
  std::memcpy(P_, gPn, sizeof(float) * kN * kN);
  injectError();
}

void GpsImuEskf::zeroVelocityUpdate() {
  const float z[3] = {0, 0, 0};
  const float r[3] = {0.05f * 0.05f, 0.05f * 0.05f, 0.05f * 0.05f};
  for (int m = 0; m < 3; m++) {
    const int ix = kIv + m;
    const float innov = z[m] - v_[m];
    const float s = P_[ix * kN + ix] + r[m];
    if (s < 1e-9f) continue;
    const float inv_s = 1.0f / s;
    float K[kN];
    for (int i = 0; i < kN; i++) K[i] = P_[i * kN + ix] * inv_s;
    for (int i = 0; i < kN; i++) dx_[i] += K[i] * innov;
    std::memset(gKH, 0, sizeof(gKH));
    for (int i = 0; i < kN; i++) gKH[i * kN + ix] = K[i];
    matEye(gIKH);
    for (int i = 0; i < kN * kN; i++) gIKH[i] -= gKH[i];
    matMul(gIKH, P_, gPn);
    std::memcpy(P_, gPn, sizeof(float) * kN * kN);
    injectError();
  }
  zupt_count_++;
}

void GpsImuEskf::onImu(const EskfImuIn &imu) {
  if (!finiteF(imu.ax_g) || !finiteF(imu.gx_rps)) return;

  const float gyro[3] = {imu.gx_rps, imu.gy_rps, imu.gz_rps};
  const float accel[3] = {imu.ax_g, imu.ay_g, imu.az_g};
  updateStationary(gyro, accel);

  if (phase_ == EskfPhase::CALIBRATING) {
    if (calib_n_ == 0) calib_start_us_ = imu.ts_us;
    for (int i = 0; i < 3; i++) {
      bg_sum_[i] += gyro[i];
      g_sum_[i] += accel[i];
    }
    calib_n_++;
    const uint32_t elapsed = imu.ts_us - calib_start_us_;
    if (elapsed >= ESKF_CALIB_MS * 1000u && calib_n_ > 10) {
      for (int i = 0; i < 3; i++) {
        bg_[i] = (float)(bg_sum_[i] / (double)calib_n_);
      }
      const float ax = (float)(g_sum_[0] / (double)calib_n_);
      const float ay = (float)(g_sum_[1] / (double)calib_n_);
      const float az = (float)(g_sum_[2] / (double)calib_n_);
      setAttitudeFromGravity(ax, ay, az);
      phase_ = EskfPhase::WAIT_GPS;
#if ESKF_DEBUG
      std::printf("[eskf] calib done n=%u bg=%.4f,%.4f,%.4f\n",
                  (unsigned)calib_n_, bg_[0], bg_[1], bg_[2]);
#endif
    }
    has_last_ = true;
    t_prev_us_ = imu.ts_us;
    return;
  }

  float dt = 0.01f;
  if (has_last_) {
    dt = (float)(imu.ts_us - t_prev_us_) * 1e-6f;
    if (dt < kDtMinS) dt = kDtMinS;
    if (dt > kDtMaxS) dt = kDtMaxS;
  }
  has_last_ = true;
  t_prev_us_ = imu.ts_us;

  if (phase_ != EskfPhase::RUNNING) return;

  float w[3], f[3];
  for (int i = 0; i < 3; i++) {
    w[i] = gyro[i] - bg_[i];
    f[i] = (accel[i] - ba_[i]) * kG;
  }
  predict(dt, f, w);

  if (stationary_) zeroVelocityUpdate();
}

void GpsImuEskf::onGps(const EskfGpsIn &gps) {
  if (gps.fix_type < 3) {
    gps_valid_ = false;
    return;
  }
  if (!finiteF(gps.lat_deg) || !finiteF(gps.lon_deg)) return;
  gps_valid_ = true;

  if (phase_ == EskfPhase::CALIBRATING) return;

  if (!has_origin_) {
    lat0_ = gps.lat_deg;
    lon0_ = gps.lon_deg;
    h0_ = gps.height_m;
    p_[0] = p_[1] = p_[2] = 0;
    v_[0] = gps.vn;
    v_[1] = gps.ve;
    v_[2] = gps.vd;
    has_origin_ = true;
    if (gps.g_speed > ESKF_COURSE_MIN_SPEED_MPS && finiteF(gps.head_mot_deg)) {
      // set yaw from headMot, keep roll/pitch
      float yaw, pitch, roll;
      quatToEuler(yaw, pitch, roll);
      yaw = gps.head_mot_deg * kDeg2Rad;
      const float cr = std::cos(roll * 0.5f), sr = std::sin(roll * 0.5f);
      const float cp = std::cos(pitch * 0.5f), sp = std::sin(pitch * 0.5f);
      const float cy = std::cos(yaw * 0.5f), sy = std::sin(yaw * 0.5f);
      q_[0] = cr * cp * cy + sr * sp * sy;
      q_[1] = sr * cp * cy - cr * sp * sy;
      q_[2] = cr * sp * cy + sr * cp * sy;
      q_[3] = cr * cp * sy - sr * sp * cy;
      quatNormalize(q_);
    }
    phase_ = EskfPhase::RUNNING;
#if ESKF_DEBUG
    std::printf("[eskf] origin lat=%.7f lon=%.7f fix=%u\n", lat0_, lon0_,
                gps.fix_type);
#endif
    return;
  }

  if (phase_ != EskfPhase::RUNNING) return;

  float pn, pe, pd;
  latLonToNed(gps.lat_deg, gps.lon_deg, gps.height_m, pn, pe, pd);
  const float p_meas[3] = {pn, pe, pd};
  const float v_meas[3] = {gps.vn, gps.ve, gps.vd};

  float h_acc = gps.h_acc_m;
  float v_acc = gps.v_acc_m;
  float s_acc = gps.s_acc_mps;
  if (!(h_acc > 0)) h_acc = 5.0f;
  if (!(v_acc > 0)) v_acc = 8.0f;
  if (!(s_acc > 0)) s_acc = 1.0f;
  if (h_acc < kPosSigmaFloorM) h_acc = kPosSigmaFloorM;
  if (v_acc < kPosSigmaFloorM) v_acc = kPosSigmaFloorM;
  if (s_acc < kVelSigmaFloor) s_acc = kVelSigmaFloor;

  const float innov =
      std::sqrt((pn - p_[0]) * (pn - p_[0]) + (pe - p_[1]) * (pe - p_[1]));
  const float gate = ESKF_OUTLIER_GATE_SIGMA * h_acc;
  if (innov > gate && gps.g_speed < ESKF_COURSE_MIN_SPEED_MPS) {
#if ESKF_DEBUG
    std::printf("[eskf] GPS outlier innov=%.2f gate=%.2f\n", innov, gate);
#endif
    return;
  }

  const float r_pos[3] = {h_acc * h_acc, h_acc * h_acc, v_acc * v_acc};
  const float r_vel[3] = {s_acc * s_acc, s_acc * s_acc, s_acc * s_acc * 4.0f};
  correctPosVel(p_meas, v_meas, r_pos, r_vel);

  if (gps.g_speed > ESKF_COURSE_MIN_SPEED_MPS && finiteF(gps.head_mot_deg)) {
    const float sig_yaw = 0.15f + 0.6f / gps.g_speed;
    correctYaw(gps.head_mot_deg * kDeg2Rad, sig_yaw);
  }
}

EskfOutput GpsImuEskf::output() const {
  EskfOutput o{};
  o.phase = phase_;
  o.zupt_active = stationary_;
  o.gps_valid = gps_valid_;
  o.innov_pos_m = last_innov_pos_;
  o.innov_vel_mps = last_innov_vel_;
  o.zupt_count = zupt_count_;
  for (int i = 0; i < 3; i++) {
    o.bg[i] = bg_[i];
    o.ba[i] = ba_[i];
  }
  o.valid = (phase_ == EskfPhase::RUNNING && has_origin_);
  o.vn = v_[0];
  o.ve = v_[1];
  o.vd = v_[2];
  float yaw, pitch, roll;
  quatToEuler(yaw, pitch, roll);
  o.yaw_deg = wrap360(yaw * kRad2Deg);
  o.pitch_deg = pitch * kRad2Deg;
  o.roll_deg = roll * kRad2Deg;
  const float pn =
      std::sqrt(P_[kIp * kN + kIp] + P_[(kIp + 1) * kN + (kIp + 1)]);
  o.pos_std_m = pn;
  if (o.valid) {
    float lat, lon, h;
    // const cast for nedToLatLon — need non-const locals
    GpsImuEskf *self = const_cast<GpsImuEskf *>(this);
    self->nedToLatLon(lat, lon, h);
    o.lat_deg = lat;
    o.lon_deg = lon;
    o.height_m = h;
  }
  return o;
}
