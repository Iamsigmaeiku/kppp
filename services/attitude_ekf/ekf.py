import numpy as np


class AttitudeEKF:
    """4-state attitude EKF: [roll, pitch (rad), bias_gx, bias_gy (dps)].

    Gyro bias is estimated online — it isn't directly measured, but becomes
    observable over time through the P cross-covariance terms once repeated
    accel corrections constrain roll/pitch drift. Measurement noise R is
    scaled up when |accel| deviates from 1g, since the accel-derived tilt
    is only trustworthy when the vehicle is close to static (not
    accelerating/braking/cornering).
    """

    def __init__(self) -> None:
        self.x = np.array([0.0, 0.0, 0.0, 0.0])
        self.P = np.diag([0.1, 0.1, 0.05, 0.05])
        self.q_angle = 0.001  # rad^2/s process noise on roll/pitch
        self.q_bias = 1e-3  # (dps)^2/s gyro-bias random-walk noise
        self.r_base = np.diag([0.03, 0.03])
        self.r_gain = 10.0  # adaptive-R sensitivity to |accel - 1g|

    def predict(self, gyro_x_dps: float, gyro_y_dps: float, dt: float) -> None:
        if dt <= 0.0:
            return
        gx = gyro_x_dps - self.x[2]
        gy = gyro_y_dps - self.x[3]
        self.x[0] += np.radians(gx) * dt
        self.x[1] += np.radians(gy) * dt

        f = np.eye(4)
        f[0, 2] = -np.radians(dt)
        f[1, 3] = -np.radians(dt)
        q = np.diag(
            [self.q_angle * dt, self.q_angle * dt, self.q_bias * dt, self.q_bias * dt]
        )
        self.P = f @ self.P @ f.T + q

    def update(self, accel_x: float, accel_y: float, accel_z: float):
        # 本專案 IMU 安裝方式重力在 +Y（見 ESP32/src/main.cpp 的
        # 「此安裝重力在 +Y（直立≈1g）」），不是慣用的 z-up 慣例；
        # roll/pitch 公式要用 Y 當「上」，X 當前後、Z 當左右來算，
        # 否則靜止時就會算出 roll≈90° 這種明顯錯誤的水平角。
        roll_meas = np.arctan2(accel_z, accel_y)
        pitch_meas = np.arctan2(-accel_x, np.sqrt(accel_y**2 + accel_z**2))
        z = np.array([roll_meas, pitch_meas])

        h = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
        y = z - h @ self.x
        y = (y + np.pi) % (2 * np.pi) - np.pi  # wrap innovation to [-pi, pi]

        accel_mag = np.sqrt(accel_x**2 + accel_y**2 + accel_z**2)
        dyn = accel_mag - 1.0
        r = self.r_base * (1.0 + self.r_gain * dyn * dyn)

        s = h @ self.P @ h.T + r
        k = self.P @ h.T @ np.linalg.inv(s)
        self.x = self.x + k @ y
        self.P = (np.eye(4) - k @ h) @ self.P

        return self.x[0], self.x[1]  # roll, pitch [rad]
