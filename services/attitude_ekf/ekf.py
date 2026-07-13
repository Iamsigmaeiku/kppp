import numpy as np


class AttitudeEKF:
    def __init__(self):
        self.x = np.array([0.0, 0.0])  # [roll, pitch] in rad
        self.P = np.eye(2) * 0.1
        self.Q = np.eye(2) * 0.001  # process noise
        self.R = np.eye(2) * 0.03  # measurement noise (accel-derived angle)

    def predict(self, gyro_x_dps: float, gyro_y_dps: float, dt: float):
        gx_rad = np.radians(gyro_x_dps)
        gy_rad = np.radians(gyro_y_dps)
        self.x[0] += gx_rad * dt
        self.x[1] += gy_rad * dt
        self.P += self.Q

    def update(self, accel_x: float, accel_y: float, accel_z: float):
        roll_meas = np.arctan2(accel_y, accel_z)
        pitch_meas = np.arctan2(-accel_x, np.sqrt(accel_y**2 + accel_z**2))
        z = np.array([roll_meas, pitch_meas])

        y = z - self.x
        S = self.P + self.R
        K = self.P @ np.linalg.inv(S)
        self.x = self.x + K @ y
        self.P = (np.eye(2) - K) @ self.P

        return self.x  # [roll, pitch] in rad
