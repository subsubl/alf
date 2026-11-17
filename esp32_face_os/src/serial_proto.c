#include "serial_proto.h"
#include "ble_scanner.h"
#include "gpio_agent.h"
#include "imu_qmi8658.h"
#include "ui_screens.h"

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>

#define RX_LINE_MAX 384

K_MSGQ_DEFINE(rx_msgq, RX_LINE_MAX, 8, 4);

static const struct device *uart_dev;
static struct k_mutex tx_mutex;
static char isr_line_buf[RX_LINE_MAX];
static size_t isr_line_len;

static void uart_isr_cb(const struct device *dev, void *user_data)
{
    ARG_UNUSED(user_data);

    if (!uart_irq_update(dev)) {
        return;
    }

    while (uart_irq_rx_ready(dev)) {
        uint8_t c;
        if (uart_fifo_read(dev, &c, 1) <= 0) {
            break;
        }

        if (c == '\n' || c == '\r') {
            if (isr_line_len > 0) {
                isr_line_buf[isr_line_len] = '\0';
                if (isr_line_buf[0] == '{') {
                    k_msgq_put(&rx_msgq, isr_line_buf, K_NO_WAIT);
                }
                isr_line_len = 0;
            }
        } else {
            if (isr_line_len < RX_LINE_MAX - 1) {
                isr_line_buf[isr_line_len++] = (char)c;
            } else {
                /* Line overflow: reset to wait for next clean JSON line */
                isr_line_len = 0;
            }
        }
    }
}

int serial_proto_init(void)
{
    k_mutex_init(&tx_mutex);
    isr_line_len = 0;

    uart_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));
    if (!device_is_ready(uart_dev)) {
        return -ENODEV;
    }

    int err = uart_irq_callback_user_data_set(uart_dev, uart_isr_cb, NULL);
    if (err < 0) {
        return err;
    }
    uart_irq_rx_enable(uart_dev);
    return 0;
}

void serial_send_json(const char *json_line)
{
    if (!uart_dev || !json_line) {
        return;
    }
    k_mutex_lock(&tx_mutex, K_FOREVER);
    for (size_t i = 0; json_line[i] != '\0'; i++) {
        uart_poll_out(uart_dev, (unsigned char)json_line[i]);
    }
    uart_poll_out(uart_dev, '\n');
    k_mutex_unlock(&tx_mutex);
}

/* Lightweight JSON field extractors */
static const char *find_json_key(const char *json, const char *key)
{
    char pattern[40];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = json;
    while ((p = strstr(p, pattern)) != NULL) {
        const char *after = p + strlen(pattern);
        while (*after == ' ' || *after == '\t') {
            after++;
        }
        if (*after == ':') {
            after++;
            while (*after == ' ' || *after == '\t') {
                after++;
            }
            return after;
        }
        p = after;
    }
    return NULL;
}

static bool json_get_str(const char *json, const char *key, char *out, size_t out_len)
{
    if (!out || out_len == 0) return false;
    out[0] = '\0';

    const char *val = find_json_key(json, key);
    if (!val || *val != '"') {
        return false;
    }
    val++;

    size_t i = 0;
    while (*val != '\0' && *val != '"' && i + 1 < out_len) {
        if (*val == '\\' && *(val + 1) != '\0') {
            val++;
        }
        out[i++] = *val++;
    }
    out[i] = '\0';
    return true;
}

static int json_get_int(const char *json, const char *key, int default_val)
{
    const char *val = find_json_key(json, key);
    if (!val) {
        return default_val;
    }
    if (*val == '-' || isdigit((unsigned char)*val)) {
        return (int)strtol(val, NULL, 10);
    }
    return default_val;
}

static void handle_json_line(const char *line)
{
    char type[24] = {0};
    char id[32] = {0};
    if (!json_get_str(line, "t", type, sizeof(type))) {
        return;
    }
    json_get_str(line, "id", id, sizeof(id));

    if (strcmp(type, "ping") == 0) {
        char resp[96];
        snprintf(resp, sizeof(resp), "{\"t\":\"res\",\"id\":\"%s\",\"ok\":1,\"up\":%lld}",
                 id, k_uptime_get());
        serial_send_json(resp);
    } else if (strcmp(type, "state") == 0) {
        char emo[24] = {0};
        char sub[192] = {0};
        if (json_get_str(line, "emo", emo, sizeof(emo))) {
            ui_set_emotion_str(emo);
        }
        if (json_get_str(line, "sub", sub, sizeof(sub))) {
            ui_set_subtitle(sub);
        }
        int mic = json_get_int(line, "mic", -1);
        if (mic >= 0) {
            ui_set_mic_active(mic != 0);
        }
    } else if (strcmp(type, "sync") == 0) {
        char emo[24] = {0};
        char sub[192] = {0};
        if (json_get_str(line, "emo", emo, sizeof(emo))) {
            ui_set_emotion_str(emo);
        }
        if (json_get_str(line, "sub", sub, sizeof(sub))) {
            ui_set_subtitle(sub);
        }
        int mouth = json_get_int(line, "mouth", -1);
        if (mouth >= 0) {
            ui_set_mouth(mouth);
        }
    } else if (strcmp(type, "mouth") == 0) {
        int v = json_get_int(line, "v", -1);
        if (v >= 0) {
            ui_set_mouth(v);
        }
    } else if (strcmp(type, "dbg") == 0) {
        char quixi[16] = {0};
        char voip[16] = {0};
        char bt_spk[16] = {0};
        json_get_str(line, "quixi", quixi, sizeof(quixi));
        json_get_str(line, "voip", voip, sizeof(voip));
        json_get_str(line, "bt_spk", bt_spk, sizeof(bt_spk));
        int rtt = json_get_int(line, "srv_ms", -1);
        ui_set_link_status(quixi[0] ? quixi : NULL,
                           voip[0] ? voip : NULL,
                           rtt,
                           bt_spk[0] ? bt_spk : NULL);
    } else if (strcmp(type, "gpio_cfg") == 0) {
        int pin = json_get_int(line, "pin", -1);
        char dir[12] = "in";
        char pull[12] = "none";
        json_get_str(line, "dir", dir, sizeof(dir));
        json_get_str(line, "pull", pull, sizeof(pull));
        int val = json_get_int(line, "val", 0);

        int err = gpio_agent_configure(pin, dir, pull, val);
        char resp[128];
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":%d,\"pin\":%d,\"err\":%d}",
                 id, (err == 0) ? 1 : 0, pin, err);
        serial_send_json(resp);
    } else if (strcmp(type, "gpio_set") == 0) {
        int pin = json_get_int(line, "pin", -1);
        int val = json_get_int(line, "val", 0);
        int err = gpio_agent_set(pin, val);
        char resp[128];
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":%d,\"pin\":%d,\"val\":%d,\"err\":%d}",
                 id, (err == 0) ? 1 : 0, pin, val ? 1 : 0, err);
        serial_send_json(resp);
    } else if (strcmp(type, "gpio_get") == 0) {
        int pin = json_get_int(line, "pin", -1);
        int val = 0;
        int err = (pin >= 0) ? gpio_agent_get(pin, &val) : 0;
        int vbat = gpio_agent_read_vbat_mv();
        char resp[160];
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":%d,\"pin\":%d,\"val\":%d,\"vbat_mv\":%d,\"err\":%d}",
                 id, (err == 0) ? 1 : 0, pin, val, vbat, err);
        serial_send_json(resp);
    } else if (strcmp(type, "ble_get") == 0) {
        static char ble_arr[768];
        static char resp[896];
        ble_scanner_format_json(ble_arr, sizeof(ble_arr));
        int cnt = ble_scanner_get_count();
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":1,\"cnt\":%d,\"ble\":%s}",
                 id, cnt, ble_arr);
        serial_send_json(resp);
    } else if (strcmp(type, "imu_get") == 0) {
        imu_data_t imu;
        imu_qmi8658_get_latest(&imu);
        char resp[256];
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":%d,\"ax\":%.2f,\"ay\":%.2f,\"az\":%.2f,"
                 "\"gx\":%.1f,\"gy\":%.1f,\"gz\":%.1f,\"temp\":%.1f,\"gest\":\"%s\",\"angle\":%d}",
                 id, imu.ready ? 1 : 0,
                 (double)imu.ax, (double)imu.ay, (double)imu.az,
                 (double)imu.gx, (double)imu.gy, (double)imu.gz,
                 (double)imu.temp_c, imu_gesture_to_str(imu.last_gesture),
                 ui_screens_get_angle_deg());
        serial_send_json(resp);
    }
}

void serial_proto_poll(void)
{
    char line[RX_LINE_MAX];
    while (k_msgq_get(&rx_msgq, line, K_NO_WAIT) == 0) {
        handle_json_line(line);
    }
}
