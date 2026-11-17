/*
 * Companion Face OS Front for Waveshare ESP32-S3-Touch-LCD-1.28-B
 * Built on Zephyr RTOS v3.7 + LVGL v8.4
 */

#include <stdio.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/display.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <lvgl.h>

#include "ble_scanner.h"
#include "gpio_agent.h"
#include "imu_qmi8658.h"
#include "serial_proto.h"
#include "ui_screens.h"

static const struct gpio_dt_spec backlight = GPIO_DT_SPEC_GET(DT_ALIAS(backlight), gpios);

static void on_face_tap_cb(bool currently_speaking)
{
    if (currently_speaking) {
        serial_send_json("{\"t\":\"evt\",\"src\":\"touch\",\"act\":\"stop_speech\"}");
        ui_set_emotion(FACE_IDLE);
    } else {
        serial_send_json("{\"t\":\"evt\",\"src\":\"touch\",\"act\":\"ptt_toggle\"}");
    }
}

static void on_cfg_change_cb(int bright, int vol, const char *mic_mode, int ble_en)
{
    char buf[128];
    snprintf(buf, sizeof(buf),
             "{\"t\":\"evt\",\"src\":\"cfg\",\"bright\":%d,\"vol\":%d,\"mode\":\"%s\",\"ble_en\":%d}",
             bright, vol, mic_mode, ble_en);
    serial_send_json(buf);
}

static void on_dbg_ping_cb(void)
{
    serial_send_json("{\"t\":\"evt\",\"src\":\"dbg\",\"act\":\"ping\"}");
}

static void on_dbg_ble_refresh_cb(void)
{
    static char ble_arr[768];
    static char resp[896];
    ble_scanner_format_json(ble_arr, sizeof(ble_arr));
    snprintf(resp, sizeof(resp), "{\"t\":\"evt\",\"src\":\"ble\",\"ble\":%s}", ble_arr);
    serial_send_json(resp);
}

int main(void)
{
    /* 1. Turn on LCD Backlight directly via GPIO 2 */
    if (gpio_is_ready_dt(&backlight)) {
        gpio_pin_configure_dt(&backlight, GPIO_OUTPUT_ACTIVE);
        gpio_pin_set_dt(&backlight, 1);
    }

    /* 2. Initialize Display & LVGL UI */
    const struct device *display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
    if (!device_is_ready(display_dev)) {
        return 0;
    }

    ui_callbacks_t ui_cbs = {
        .on_face_tap = on_face_tap_cb,
        .on_cfg_change = on_cfg_change_cb,
        .on_dbg_ping = on_dbg_ping_cb,
        .on_dbg_ble_refresh = on_dbg_ble_refresh_cb,
    };
    ui_screens_init(&ui_cbs);
    lv_task_handler();
    display_blanking_off(display_dev);

    /* 3. Initialize Subsystems */
    gpio_agent_init();
    imu_qmi8658_init();
    ble_scanner_init();
    serial_proto_init();

    serial_send_json("{\"t\":\"evt\",\"src\":\"sys\",\"act\":\"boot\",\"ver\":\"1.0\"}");

    uint32_t loop_tick = 0;

    while (1) {
        /* Drain UART NDJSON commands immediately */
        serial_proto_poll();

        /* Poll IMU at 25Hz (every 40ms) */
        if ((loop_tick % 4) == 0) {
            imu_data_t imu;
            imu_gesture_t gest = imu_qmi8658_poll(&imu);
            if (gest != IMU_GESTURE_NONE) {
                char ev[160];
                snprintf(ev, sizeof(ev),
                         "{\"t\":\"evt\",\"src\":\"imu\",\"gest\":\"%s\",\"ax\":%.2f,\"ay\":%.2f,\"az\":%.2f,\"temp\":%.1f}",
                         imu_gesture_to_str(gest),
                         (double)imu.ax, (double)imu.ay, (double)imu.az,
                         (double)imu.temp_c);
                serial_send_json(ev);
            }
        }

        /* Animate procedural face at ~33 FPS (every 30ms) */
        if ((loop_tick % 3) == 0) {
            ui_screens_tick_anim();
        }

        /* Update Debug & Battery telemetry every 500ms */
        if ((loop_tick % 50) == 0) {
            ui_screens_update_telemetry();
        }

        /* Periodic 10-second heartbeat */
        if ((loop_tick % 1000) == 0 && loop_tick > 0) {
            char hb[96];
            snprintf(hb, sizeof(hb), "{\"t\":\"evt\",\"src\":\"sys\",\"act\":\"hb\",\"up\":%lld}",
                     k_uptime_get());
            serial_send_json(hb);
        }

        lv_task_handler();
        loop_tick++;
        k_sleep(K_MSEC(10));
    }

    return 0;
}
