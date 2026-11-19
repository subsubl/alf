#ifndef IMU_QMI8658_H
#define IMU_QMI8658_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    IMU_GESTURE_NONE = 0,
    IMU_GESTURE_SHAKE,
    IMU_GESTURE_TILT,
    IMU_GESTURE_PICKUP,
    IMU_GESTURE_DOUBLE_TAP,
} imu_gesture_t;

typedef struct {
    imu_gesture_t gest;
    float ax;
    float ay;
    float az;
    float temp_c;
} imu_gesture_event_t;

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
 * @brief Pop the next queued gesture event detected by the 50Hz IMU thread.
 * @return true if an event was popped into out, false if queue is empty.
 */
bool imu_qmi8658_pop_gesture(imu_gesture_event_t *out);

/**
 * @brief Get cached latest IMU state.
 */
void imu_qmi8658_get_latest(imu_data_t *out);

/**
 * @brief Convert gesture enum to static string.
 */
const char *imu_gesture_to_str(imu_gesture_t g);

#endif /* IMU_QMI8658_H */
