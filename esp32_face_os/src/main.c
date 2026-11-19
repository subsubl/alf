/*
 * Companion Face OS Front for Waveshare ESP32-S3-Touch-LCD-1.28-B
 * Built on Zephyr RTOS v3.7 + LVGL v8.4
 */

#include <stdio.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/display.h>
#include <zephyr/kernel.h>
#include <lvgl.h>

#include "ble_scanner.h"
#include "gpio_agent.h"
#include "imu_qmi8658.h"
#include "serial_proto.h"
#include "ui_screens.h"

static void on_face_tap_cb(bool currently_speaking)
{
    if (currently_speaking) {
        serial_send_json("{\"t\":\"evt\",\"src\":\"touch\",\"act\":\"stop_speech\"}");
        ui_set_emotion(FACE_IDLE);
    } else {
        serial_send_json("{\"t\":\"evt\",\"src\":\"touch\",\"act\":\"ptt_toggle\"}");
    }
}

static void on_cfg_change_cb(int bright, int vol, const char *mic_mode, int ble_en, int sleep_s)
{
    char buf[160];
    snprintf(buf, sizeof(buf),
             "{\"t\":\"evt\",\"src\":\"cfg\",\"bright\":%d,\"vol\":%d,\"mode\":\"%s\",\"ble_en\":%d,\"sleep_s\":%d}",
             bright, vol, mic_mode, ble_en, sleep_s);
    serial_send_json(buf);
}

static void on_dbg_test_call_cb(void)
{
    serial_send_json("{\"t\":\"evt\",\"src\":\"dbg\",\"act\":\"test_call\"}");
}

static void on_dbg_ble_refresh_cb(void)
{
    serial_send_ble_evt();
}

int main(void)
{
    /* 1. Initialize Display & LVGL UI (PWM backlight GPIO 2 initialized in ui_screens_init) */
    const struct device *display_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
    if (!device_is_ready(display_dev)) {
        return 0;
    }

    ui_callbacks_t ui_cbs = {
        .on_face_tap = on_face_tap_cb,
        .on_cfg_change = on_cfg_change_cb,
        .on_dbg_test_call = on_dbg_test_call_cb,
        .on_dbg_ble_refresh = on_dbg_ble_refresh_cb,
    };
    ui_screens_init(&ui_cbs);
    lv_task_handler();
    display_blanking_off(display_dev);

    /* 2. Initialize Subsystems (IMU launches dedicated 50Hz thread) */
    gpio_agent_init();
    imu_qmi8658_init();
    ble_scanner_init();
    serial_proto_init();

    serial_send_json("{\"t\":\"evt\",\"src\":\"sys\",\"act\":\"boot\",\"ver\":\"1.0\"}");

    uint32_t loop_tick = 0;

    while (1) {
        /* Drain UART NDJSON commands immediately */
        serial_proto_poll();

        /* Drain 50Hz IMU gesture events */
        imu_gesture_event_t imu_ev;
        while (imu_qmi8658_pop_gesture(&imu_ev)) {
            char ev[160];
            snprintf(ev, sizeof(ev),
                     "{\"t\":\"evt\",\"src\":\"imu\",\"gest\":\"%s\",\"ax\":%.2f,\"ay\":%.2f,\"az\":%.2f,\"temp\":%.1f}",
                     imu_gesture_to_str(imu_ev.gest),
                     (double)imu_ev.ax, (double)imu_ev.ay, (double)imu_ev.az,
                     (double)imu_ev.temp_c);
            serial_send_json(ev);
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
