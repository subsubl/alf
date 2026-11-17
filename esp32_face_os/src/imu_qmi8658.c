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

static const struct device *i2c_dev;
static struct k_mutex imu_mutex;
static imu_data_t g_imu;
static int shake_streak;
static bool was_tilted;

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
    shake_streak = 0;
    was_tilted = false;

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

    return 0;
}

imu_gesture_t imu_qmi8658_poll(imu_data_t *out)
{
    imu_gesture_t triggered = IMU_GESTURE_NONE;

    if (!g_imu.ready || i2c_dev == NULL) {
        if (out) {
            imu_qmi8658_get_latest(out);
        }
        return IMU_GESTURE_NONE;
    }

    uint8_t raw[14];
    if (qmi_read_regs(g_imu.addr, QMI8658_REG_TEMP_L, raw, sizeof(raw)) < 0) {
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
    float l1_mag = fabsf(ax) + fabsf(ay) + fabsf(az);
    bool is_tilted = (fabsf(ax) > 6.5f) || (fabsf(ay) > 6.5f);

    if (l1_mag > 22.0f) {
        shake_streak++;
    } else {
        shake_streak = 0;
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
        if (shake_streak >= 3) {
            triggered = IMU_GESTURE_SHAKE;
            g_imu.last_gesture = IMU_GESTURE_SHAKE;
            g_imu.last_gesture_ms = now;
            shake_streak = 0;
        } else if (is_tilted && !was_tilted) {
            triggered = IMU_GESTURE_TILT;
            g_imu.last_gesture = IMU_GESTURE_TILT;
            g_imu.last_gesture_ms = now;
        }
    }
    was_tilted = is_tilted;

    if (out) {
        *out = g_imu;
    }
    k_mutex_unlock(&imu_mutex);

    return triggered;
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
    default:
        return "none";
    }
}
