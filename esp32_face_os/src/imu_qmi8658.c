#include "imu_qmi8658.h"

#include <math.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>

LOG_MODULE_REGISTER(imu_qmi8658, LOG_LEVEL_WRN);

#define QMI8658_ADDR_PRIMARY   0x6B
#define QMI8658_ADDR_ALT       0x6A

#define QMI8658_REG_WHO_AM_I   0x00
#define QMI8658_REG_CTRL1      0x02
#define QMI8658_REG_CTRL2      0x03
#define QMI8658_REG_CTRL3      0x04
#define QMI8658_REG_CTRL7      0x08
#define QMI8658_REG_TEMP_L     0x33

#define QMI8658_WHO_AM_I_VAL   0x05

#define GESTURE_COOLDOWN_MS    800
#define IMU_THREAD_STACK_SIZE  2048
#define IMU_THREAD_PRIO        K_PRIO_PREEMPT(2)

typedef enum {
    TAP_IDLE = 0,
    TAP_FIRST_ACTIVE,
    TAP_WAIT_SECOND,
    TAP_SECOND_ACTIVE,
} tap_state_t;

static const struct device *i2c_dev;
static struct k_mutex imu_mutex;
static imu_data_t g_imu;
static int shake_score;
static bool was_tilted;
static bool lp_init;
static float lp_ax, lp_ay, lp_az;
static int lp_freeze_ticks;
static bool prev_init;
static float prev_ax, prev_ay, prev_az;
static int rest_flat_ticks;
static bool was_resting_flat;
static int pickup_ticks;
static int pickup_drop_ticks;
static bool pickup_ref_set;
static bool pickup_active;
static bool pickup_dipped;
static float pickup_ref_ax, pickup_ref_ay;
static float pickup_dir_ax, pickup_dir_ay;
static tap_state_t tap_state;
static int64_t tap1_ms;
static int64_t tap2_ms;
static int tap_quiet_ticks;
static int tap2_steady_ticks;
static float tap1_base_ax, tap1_base_ay, tap1_base_az;
static float tap2_base_ax, tap2_base_ay, tap2_base_az;
static int64_t last_shake_ms;

K_MSGQ_DEFINE(imu_evt_msgq, sizeof(imu_gesture_event_t), 8, 4);
K_THREAD_STACK_DEFINE(imu_thread_stack, IMU_THREAD_STACK_SIZE);
static struct k_thread imu_thread_data;
static void imu_thread_fn(void *p1, void *p2, void *p3);

static int qmi_write_reg(uint8_t addr, uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = { reg, val };
    return i2c_write(i2c_dev, buf, sizeof(buf), addr);
}

static int qmi_read_regs(uint8_t addr, uint8_t reg, uint8_t *dst, size_t len)
{
    return i2c_write_read(i2c_dev, addr, &reg, 1, dst, len);
}

static int try_probe_addr(uint8_t addr)
{
    uint8_t who = 0;
    int ret = qmi_read_regs(addr, QMI8658_REG_WHO_AM_I, &who, 1);
    if (ret < 0) {
        return ret;
    }
    if (who != QMI8658_WHO_AM_I_VAL) {
        return -ENODEV;
    }
    return 0;
}

int imu_qmi8658_init(void)
{
    k_mutex_init(&imu_mutex);
    memset(&g_imu, 0, sizeof(g_imu));
    shake_score = 0;
    was_tilted = false;
    lp_init = false;
    lp_ax = lp_ay = lp_az = 0.0f;
    lp_freeze_ticks = 0;
    prev_init = false;
    prev_ax = prev_ay = prev_az = 0.0f;
    rest_flat_ticks = 0;
    was_resting_flat = false;
    pickup_ticks = 0;
    pickup_drop_ticks = 0;
    pickup_ref_set = false;
    pickup_active = false;
    pickup_dipped = false;
    pickup_ref_ax = pickup_ref_ay = 0.0f;
    pickup_dir_ax = pickup_dir_ay = 0.0f;
    tap_state = TAP_IDLE;
    tap1_ms = 0;
    tap2_ms = 0;
    tap_quiet_ticks = 0;
    tap2_steady_ticks = 0;
    tap1_base_ax = tap1_base_ay = tap1_base_az = 0.0f;
    tap2_base_ax = tap2_base_ay = tap2_base_az = 0.0f;
    last_shake_ms = -1000;

    i2c_dev = DEVICE_DT_GET(DT_NODELABEL(i2c1));
    if (!device_is_ready(i2c_dev)) {
        LOG_ERR("i2c1 not ready");
        return -ENODEV;
    }

    uint8_t addr = QMI8658_ADDR_PRIMARY;
    if (try_probe_addr(addr) != 0) {
        addr = QMI8658_ADDR_ALT;
        if (try_probe_addr(addr) != 0) {
            LOG_ERR("QMI8658 not found at 0x6B or 0x6A");
            return -ENODEV;
        }
    }

    /* CTRL1: auto-increment address, little endian */
    if (qmi_write_reg(addr, QMI8658_REG_CTRL1, 0x40) < 0) return -EIO;
    /* CTRL2: Accel +/-8g (0x2_), 250Hz (0x_5) */
    if (qmi_write_reg(addr, QMI8658_REG_CTRL2, 0x25) < 0) return -EIO;
    /* CTRL3: Gyro +/-512dps (0x5_), 250Hz (0x_5) */
    if (qmi_write_reg(addr, QMI8658_REG_CTRL3, 0x55) < 0) return -EIO;
    /* CTRL7: Enable Accel + Gyro */
    if (qmi_write_reg(addr, QMI8658_REG_CTRL7, 0x03) < 0) return -EIO;

    k_mutex_lock(&imu_mutex, K_FOREVER);
    g_imu.ready = true;
    g_imu.addr = addr;
    g_imu.last_gesture = IMU_GESTURE_NONE;
    k_mutex_unlock(&imu_mutex);

    k_thread_create(&imu_thread_data, imu_thread_stack,
                    K_THREAD_STACK_SIZEOF(imu_thread_stack),
                    imu_thread_fn, NULL, NULL, NULL,
                    IMU_THREAD_PRIO, 0, K_NO_WAIT);
    k_thread_name_set(&imu_thread_data, "imu_50hz");

    return 0;
}

static imu_gesture_t imu_qmi8658_poll(imu_data_t *out)
{
    imu_gesture_t triggered = IMU_GESTURE_NONE;
    bool ready = false;
    uint8_t addr = 0;

    k_mutex_lock(&imu_mutex, K_FOREVER);
    ready = g_imu.ready;
    addr = g_imu.addr;
    k_mutex_unlock(&imu_mutex);

    if (!ready || i2c_dev == NULL) {
        if (out) {
            imu_qmi8658_get_latest(out);
        }
        return IMU_GESTURE_NONE;
    }

    uint8_t raw[14];
    if (qmi_read_regs(addr, QMI8658_REG_TEMP_L, raw, sizeof(raw)) < 0) {
        if (out) {
            imu_qmi8658_get_latest(out);
        }
        return IMU_GESTURE_NONE;
    }

    int16_t raw_temp = (int16_t)sys_get_le16(&raw[0]);
    int16_t raw_ax   = (int16_t)sys_get_le16(&raw[2]);
    int16_t raw_ay   = (int16_t)sys_get_le16(&raw[4]);
    int16_t raw_az   = (int16_t)sys_get_le16(&raw[6]);
    int16_t raw_gx   = (int16_t)sys_get_le16(&raw[8]);
    int16_t raw_gy   = (int16_t)sys_get_le16(&raw[10]);
    int16_t raw_gz   = (int16_t)sys_get_le16(&raw[12]);

    float ax = ((float)raw_ax) * (9.80665f / 4096.0f);
    float ay = ((float)raw_ay) * (9.80665f / 4096.0f);
    float az = ((float)raw_az) * (9.80665f / 4096.0f);
    float gx = ((float)raw_gx) / 64.0f;
    float gy = ((float)raw_gy) / 64.0f;
    float gz = ((float)raw_gz) / 64.0f;
    float temp_c = ((float)raw_temp) / 256.0f;

    int64_t now = k_uptime_get();

    if (!prev_init) {
        prev_ax = ax;
        prev_ay = ay;
        prev_az = az;
        prev_init = true;
    }
    float jx = ax - prev_ax;
    float jy = ay - prev_ay;
    float jz = az - prev_az;
    float jerk = sqrtf(jx * jx + jy * jy + jz * jz);
    prev_ax = ax;
    prev_ay = ay;
    prev_az = az;

    float a_mag = sqrtf(ax * ax + ay * ay + az * az);

    if (!lp_init) {
        lp_ax = ax;
        lp_ay = ay;
        lp_az = az;
        lp_init = true;
        lp_freeze_ticks = 0;
    }

    float dx = ax - lp_ax;
    float dy = ay - lp_ay;
    float dz = az - lp_az;
    float dyn_mag = sqrtf(dx * dx + dy * dy + dz * dz);

    /* Freeze low-pass gravity adaptation on sharp tap impulses (up to 15 ticks = 300ms),
     * while allowing steady 1g orientation changes to track gravity smoothly. */
    bool steady_1g = (jerk < 3.0f) && (fabsf(a_mag - 9.80665f) < 1.5f);
    if (dyn_mag <= 2.2f || steady_1g || lp_freeze_ticks >= 15) {
        lp_ax += 0.15f * (ax - lp_ax);
        lp_ay += 0.15f * (ay - lp_ay);
        lp_az += 0.15f * (az - lp_az);
        dx = ax - lp_ax;
        dy = ay - lp_ay;
        dz = az - lp_az;
        dyn_mag = sqrtf(dx * dx + dy * dy + dz * dz);
        lp_freeze_ticks = 0;
    } else {
        lp_freeze_ticks++;
    }

    float in_plane = sqrtf(ax * ax + ay * ay);
    float gyro_mag = fabsf(gx) + fabsf(gy) + fabsf(gz);
    bool is_tilted = (fabsf(ax) > 6.5f) || (fabsf(ay) > 6.5f);

    if (dyn_mag > 7.0f && !steady_1g) {
        shake_score += 2;
        if (shake_score > 16) {
            shake_score = 16;
        }
    } else if (shake_score > 0) {
        shake_score--;
    }

    if (was_resting_flat) {
        if (in_plane > 5.5f) {
            pickup_active = true;
            pickup_drop_ticks = 0;
            if (!pickup_ref_set || pickup_dipped) {
                pickup_ref_ax = ax;
                pickup_ref_ay = ay;
                pickup_ref_set = true;
                pickup_dipped = false;
            } else if ((shake_score >= 10) ||
                       (shake_score >= 4 && dyn_mag > 7.0f && a_mag > 12.0f &&
                        (ax * pickup_ref_ax + ay * pickup_ref_ay) < -15.0f)) {
                /* High-energy shake or lateral reversal: not a pickup/twist */
                was_resting_flat = false;
                rest_flat_ticks = 0;
                pickup_ticks = 0;
                pickup_drop_ticks = 0;
                pickup_ref_set = false;
                pickup_active = false;
                pickup_dipped = false;
            }
        } else {
            bool clear_flat = false;
            if (shake_score >= 4) {
                clear_flat = true;
            } else if (pickup_active) {
                if (in_plane < 3.5f) {
                    clear_flat = true;
                } else if (in_plane <= 4.5f) {
                    pickup_drop_ticks++;
                    pickup_dipped = true;
                    if (pickup_drop_ticks >= 2) {
                        clear_flat = true;
                    }
                } else {
                    pickup_drop_ticks = 0;
                }
            }
            if (clear_flat) {
                was_resting_flat = false;
                rest_flat_ticks = 0;
                pickup_ticks = 0;
                pickup_drop_ticks = 0;
                pickup_ref_set = false;
                pickup_active = false;
                pickup_dipped = false;
            }
        }
    }

    if (shake_score >= 10 || (now - last_shake_ms) <= 500) {
        tap_state = TAP_IDLE;
    }

    if (in_plane < 3.0f && dyn_mag < 1.5f && gyro_mag < 30.0f) {
        if (rest_flat_ticks < 25) {
            rest_flat_ticks++;
        }
        if (rest_flat_ticks >= 15) {
            was_resting_flat = true;
            pickup_ref_set = false;
            pickup_drop_ticks = 0;
            pickup_active = false;
            pickup_dipped = false;
        }
    } else {
        rest_flat_ticks = 0;
    }

    bool dbl_tap_fired = false;
    switch (tap_state) {
    case TAP_IDLE:
        if (dyn_mag > 4.0f && gyro_mag < 220.0f &&
            (now - last_shake_ms) > 500 && shake_score < 8) {
            tap1_ms = now;
            tap1_base_ax = lp_ax;
            tap1_base_ay = lp_ay;
            tap1_base_az = lp_az;
            tap_quiet_ticks = 0;
            tap_state = TAP_FIRST_ACTIVE;
        }
        break;
    case TAP_FIRST_ACTIVE:
        if ((now - tap1_ms) > 140) {
            tap_state = TAP_IDLE;
        } else if (dyn_mag < 2.2f) {
            float bx = ax - tap1_base_ax;
            float by = ay - tap1_base_ay;
            float bz = az - tap1_base_az;
            float bdiff = sqrtf(bx * bx + by * by + bz * bz);
            if (bdiff > 3.5f) {
                tap_state = TAP_IDLE;
            } else {
                tap_quiet_ticks = 1;
                tap2_base_ax = lp_ax;
                tap2_base_ay = lp_ay;
                tap2_base_az = lp_az;
                tap_state = TAP_WAIT_SECOND;
            }
        }
        break;
    case TAP_WAIT_SECOND: {
        int64_t dt = now - tap1_ms;
        if (dt > 450) {
            tap_state = TAP_IDLE;
        } else if (dyn_mag < 2.2f) {
            tap_quiet_ticks++;
            tap2_base_ax = lp_ax;
            tap2_base_ay = lp_ay;
            tap2_base_az = lp_az;
        } else if (dt < 120) {
            if (dyn_mag > 4.0f) {
                tap_state = TAP_IDLE;
            } else {
                tap_quiet_ticks = 0;
            }
        } else if (dyn_mag > 4.0f && gyro_mag < 220.0f) {
            if (tap_quiet_ticks >= 2) {
                tap2_ms = now;
                tap2_steady_ticks = 0;
                tap_quiet_ticks = 0;
                tap_state = TAP_SECOND_ACTIVE;
            } else {
                tap_state = TAP_IDLE;
            }
        }
        break;
    }
    case TAP_SECOND_ACTIVE: {
        int64_t dt2 = now - tap2_ms;
        float bx = ax - tap2_base_ax;
        float by = ay - tap2_base_ay;
        float bz = az - tap2_base_az;
        float bdiff = sqrtf(bx * bx + by * by + bz * bz);
        if (dt2 > 120) {
            tap_state = TAP_IDLE;
        } else if (steady_1g && bdiff > 3.5f) {
            tap2_steady_ticks++;
            if (tap2_steady_ticks >= 4) {
                tap_state = TAP_IDLE;
            }
        } else if (dyn_mag < 2.2f) {
            if (bdiff > 3.5f) {
                tap_state = TAP_IDLE;
            } else {
                tap_quiet_ticks++;
                if (tap_quiet_ticks >= 3 && shake_score < 10) {
                    dbl_tap_fired = true;
                    tap_state = TAP_IDLE;
                }
            }
        } else {
            if (tap_quiet_ticks > 0 && dyn_mag > 3.5f) {
                tap_state = TAP_IDLE;
            } else {
                tap_quiet_ticks = 0;
            }
        }
        break;
    }
    }

    bool settled = steady_1g || (dyn_mag <= 7.0f);
    bool flat_lateral = (fabsf(az) > 8.5f);
    if (was_resting_flat && in_plane > 5.5f && settled && shake_score < 4 && !flat_lateral) {
        if (pickup_ticks == 0) {
            pickup_dir_ax = ax;
            pickup_dir_ay = ay;
            pickup_ticks = 1;
        } else {
            float dot = ax * pickup_dir_ax + ay * pickup_dir_ay;
            float dir_m2 = pickup_dir_ax * pickup_dir_ax + pickup_dir_ay * pickup_dir_ay;
            float cur_m2 = ax * ax + ay * ay;
            if (dot <= 0.0f || (dot * dot) < 0.88f * dir_m2 * cur_m2) {
                pickup_dir_ax = ax;
                pickup_dir_ay = ay;
                pickup_ticks = 1;
            } else {
                pickup_ticks++;
            }
        }
    } else {
        pickup_ticks = 0;
    }

    k_mutex_lock(&imu_mutex, K_FOREVER);
    g_imu.ax = ax;
    g_imu.ay = ay;
    g_imu.az = az;
    g_imu.gx = gx;
    g_imu.gy = gy;
    g_imu.gz = gz;
    g_imu.temp_c = temp_c;

    if ((now - g_imu.last_gesture_ms) >= GESTURE_COOLDOWN_MS) {
        if (shake_score >= 10 && (!was_resting_flat || in_plane <= 5.5f)) {
            triggered = IMU_GESTURE_SHAKE;
            g_imu.last_gesture = IMU_GESTURE_SHAKE;
            g_imu.last_gesture_ms = now;
            shake_score = 0;
            last_shake_ms = now;
            tap_state = TAP_IDLE;
            was_resting_flat = false;
            rest_flat_ticks = 0;
            pickup_ticks = 0;
            pickup_drop_ticks = 0;
            pickup_ref_set = false;
            pickup_active = false;
            pickup_dipped = false;
        } else if (shake_score < 4 && was_resting_flat && pickup_ticks >= 3) {
            triggered = IMU_GESTURE_PICKUP;
            g_imu.last_gesture = IMU_GESTURE_PICKUP;
            g_imu.last_gesture_ms = now;
            was_resting_flat = false;
            rest_flat_ticks = 0;
            pickup_ticks = 0;
            pickup_drop_ticks = 0;
            pickup_ref_set = false;
            pickup_active = false;
            pickup_dipped = false;
        } else if (dbl_tap_fired) {
            triggered = IMU_GESTURE_DOUBLE_TAP;
            g_imu.last_gesture = IMU_GESTURE_DOUBLE_TAP;
            g_imu.last_gesture_ms = now;
        } else if (shake_score < 4 && is_tilted && !was_tilted &&
                   settled && !was_resting_flat && !flat_lateral) {
            triggered = IMU_GESTURE_TILT;
            g_imu.last_gesture = IMU_GESTURE_TILT;
            g_imu.last_gesture_ms = now;
        }
    }

    if (pickup_ticks >= 3) {
        was_resting_flat = false;
        rest_flat_ticks = 0;
        pickup_ticks = 0;
        pickup_drop_ticks = 0;
        pickup_ref_set = false;
        pickup_active = false;
        pickup_dipped = false;
    }
    was_tilted = is_tilted;

    if (out) {
        *out = g_imu;
    }
    k_mutex_unlock(&imu_mutex);

    return triggered;
}

static void imu_thread_fn(void *p1, void *p2, void *p3)
{
    ARG_UNUSED(p1);
    ARG_UNUSED(p2);
    ARG_UNUSED(p3);

    int64_t next_ms = k_uptime_get();
    while (1) {
        imu_data_t snap;
        imu_gesture_t gest = imu_qmi8658_poll(&snap);
        if (gest != IMU_GESTURE_NONE) {
            imu_gesture_event_t ev = {
                .gest = gest,
                .ax = snap.ax,
                .ay = snap.ay,
                .az = snap.az,
                .temp_c = snap.temp_c,
            };
            k_msgq_put(&imu_evt_msgq, &ev, K_NO_WAIT);
        }

        next_ms += 20;
        int64_t rem = next_ms - k_uptime_get();
        if (rem > 0) {
            k_msleep((int32_t)rem);
        } else if (rem < -100) {
            next_ms = k_uptime_get();
        }
    }
}

bool imu_qmi8658_pop_gesture(imu_gesture_event_t *out)
{
    if (!out) return false;
    return k_msgq_get(&imu_evt_msgq, out, K_NO_WAIT) == 0;
}

void imu_qmi8658_get_latest(imu_data_t *out)
{
    if (!out) return;
    k_mutex_lock(&imu_mutex, K_FOREVER);
    *out = g_imu;
    k_mutex_unlock(&imu_mutex);
}

const char *imu_gesture_to_str(imu_gesture_t g)
{
    switch (g) {
    case IMU_GESTURE_SHAKE:
        return "shake";
    case IMU_GESTURE_TILT:
        return "tilt";
    case IMU_GESTURE_PICKUP:
        return "pickup";
    case IMU_GESTURE_DOUBLE_TAP:
        return "double_tap";
    default:
        return "none";
    }
}
