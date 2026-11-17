#ifndef IMU_QMI8658_H
#define IMU_QMI8658_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    IMU_GESTURE_NONE = 0,
    IMU_GESTURE_SHAKE,
    IMU_GESTURE_TILT,
} imu_gesture_t;

typedef struct {
    bool ready;
    uint8_t addr;
    float ax;
    float ay;
    float az;
    float gx;
    float gy;
    float gz;
    float temp_c;
    imu_gesture_t last_gesture;
    int64_t last_gesture_ms;
} imu_data_t;

/**
 * @brief Initialize the QMI8658 6-axis IMU over i2c1.
 * @return 0 on success, negative errno on failure.
 */
int imu_qmi8658_init(void);

/**
 * @brief Poll current IMU readings and detect motion gestures.
 * @param out Optional pointer to copy current IMU state into.
 * @return Newly detected gesture during this poll (or IMU_GESTURE_NONE).
 */
imu_gesture_t imu_qmi8658_poll(imu_data_t *out);

/**
 * @brief Get cached latest IMU state.
 */
void imu_qmi8658_get_latest(imu_data_t *out);

/**
 * @brief Convert gesture enum to static string.
 */
const char *imu_gesture_to_str(imu_gesture_t g);

#endif /* IMU_QMI8658_H */
