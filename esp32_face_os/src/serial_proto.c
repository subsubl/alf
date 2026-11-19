#include "serial_proto.h"
#include "ble_scanner.h"
#include "gpio_agent.h"
#include "imu_qmi8658.h"
#include "ui_screens.h"

#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>

#define RX_LINE_MAX 512

K_MSGQ_DEFINE(rx_msgq, RX_LINE_MAX, 16, 4);

static const struct device *uart_dev;
static struct k_mutex tx_mutex;
static char isr_line_buf[RX_LINE_MAX];
static size_t isr_line_len;
static bool isr_overflow;
static char ble_arr_buf[3584];
static char ble_resp_buf[4096];

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
            if (isr_overflow) {
                isr_overflow = false;
                isr_line_len = 0;
            } else if (isr_line_len > 0) {
                size_t start = 0;
                size_t end = isr_line_len;
                while (start < end && (isr_line_buf[start] == ' ' || isr_line_buf[start] == '\t')) {
                    start++;
                }
                while (end > start && (isr_line_buf[end - 1] == ' ' || isr_line_buf[end - 1] == '\t')) {
                    end--;
                }
                if (end - start >= 2 && isr_line_buf[start] == '{' && isr_line_buf[end - 1] == '}') {
                    isr_line_buf[isr_line_len] = '\0';
                    k_msgq_put(&rx_msgq, isr_line_buf, K_NO_WAIT);
                }
                isr_line_len = 0;
            }
        } else if (!isr_overflow) {
            if (isr_line_len < RX_LINE_MAX - 1) {
                isr_line_buf[isr_line_len++] = (char)c;
            } else {
                /* Line overflow: discard remainder until newline */
                isr_overflow = true;
                isr_line_len = 0;
            }
        }
    }
}

int serial_proto_init(void)
{
    k_mutex_init(&tx_mutex);
    isr_line_len = 0;
    isr_overflow = false;

    uart_dev = DEVICE_DT_GET(DT_NODELABEL(uart0));
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

/* Depth- and escape-aware key-level JSON scanner (top-level object depth 1) */
static const char *skip_json_ws(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
        p++;
    }
    return p;
}

static const char *skip_json_string(const char *p)
{
    if (*p != '"') {
        return NULL;
    }
    p++;
    while (*p != '\0') {
        if (*p == '\\') {
            p++;
            if (*p == '\0') {
                return NULL;
            }
            p++;
        } else if (*p == '"') {
            return p + 1;
        } else {
            p++;
        }
    }
    return NULL;
}

static const char *skip_json_value(const char *p)
{
    p = skip_json_ws(p);
    if (*p == '"') {
        return skip_json_string(p);
    }
    if (*p == '{' || *p == '[') {
        int depth = 0;
        while (*p != '\0') {
            if (*p == '"') {
                p = skip_json_string(p);
                if (!p) {
                    return NULL;
                }
                continue;
            }
            if (*p == '{' || *p == '[') {
                depth++;
            } else if (*p == '}' || *p == ']') {
                depth--;
                if (depth == 0) {
                    return p + 1;
                }
            }
            p++;
        }
        return NULL;
    }
    while (*p != '\0' && *p != ',' && *p != '}') {
        p++;
    }
    return p;
}

static const char *find_json_key(const char *json, const char *key)
{
    if (!json || !key) {
        return NULL;
    }
    size_t key_len = strlen(key);
    const char *p = skip_json_ws(json);
    if (*p != '{') {
        return NULL;
    }
    p++;

    while (*p != '\0') {
        p = skip_json_ws(p);
        if (*p == ',') {
            p++;
            p = skip_json_ws(p);
        }
        if (*p == '}' || *p == '\0') {
            return NULL;
        }
        if (*p != '"') {
            return NULL;
        }

        const char *k_start = p + 1;
        const char *after_key = skip_json_string(p);
        if (!after_key) {
            return NULL;
        }
        size_t span_len = (size_t)((after_key - 1) - k_start);
        bool key_match = (span_len == key_len && strncmp(k_start, key, key_len) == 0);

        p = skip_json_ws(after_key);
        if (*p != ':') {
            return NULL;
        }
        p = skip_json_ws(p + 1);
        if (key_match) {
            return p;
        }
        p = skip_json_value(p);
        if (!p) {
            return NULL;
        }
    }
    return NULL;
}

static void sanitize_json_id(char *id)
{
    if (!id) return;
    size_t w = 0;
    for (size_t r = 0; id[r] != '\0'; r++) {
        unsigned char c = (unsigned char)id[r];
        if (c >= 0x20 && c <= 0x7E && c != '"' && c != '\\') {
            id[w++] = (char)c;
        }
    }
    id[w] = '\0';
}

static size_t utf8_trim_incomplete_tail(const char *buf, size_t len)
{
    if (len == 0) {
        return 0;
    }
    size_t pos = len;
    while (pos > 0 && (((unsigned char)buf[pos - 1] & 0xC0U) == 0x80U) && (len - pos) < 3U) {
        pos--;
    }
    if (pos == 0) {
        return 0;
    }
    unsigned char lead = (unsigned char)buf[pos - 1];
    size_t expected = 1;
    if ((lead & 0x80U) == 0x00U) {
        expected = 1;
    } else if ((lead & 0xE0U) == 0xC0U) {
        expected = 2;
    } else if ((lead & 0xF0U) == 0xE0U) {
        expected = 3;
    } else if ((lead & 0xF8U) == 0xF0U) {
        expected = 4;
    } else {
        return pos - 1;
    }
    size_t actual = len - (pos - 1);
    if (actual < expected) {
        return pos - 1;
    }
    return len;
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
        if (*val == '\\') {
            val++;
            if (*val == '\0') {
                break;
            }
            switch (*val) {
            case '"':  out[i++] = '"';  break;
            case '\\': out[i++] = '\\'; break;
            case '/':  out[i++] = '/';  break;
            case 'n':  out[i++] = '\n'; break;
            case 'r':  out[i++] = ' ';  break;
            case 't':  out[i++] = '\t'; break;
            default:   out[i++] = *val; break;
            }
            val++;
        } else {
            out[i++] = *val++;
        }
    }
    bool truncated = (*val != '\0' && *val != '"');
    while (*val != '\0' && *val != '"') {
        if (*val == '\\') {
            val++;
            if (*val == '\0') {
                break;
            }
        }
        val++;
    }
    if (*val != '"') {
        out[0] = '\0';
        return false;
    }
    if (truncated) {
        i = utf8_trim_incomplete_tail(out, i);
    }
    out[i] = '\0';
    return true;
}

static bool json_get_int_ex(const char *json, const char *key, int *out_val)
{
    const char *val = find_json_key(json, key);
    if (!val) {
        return false;
    }
    if (*val == '-' || isdigit((unsigned char)*val)) {
        char *end = NULL;
        errno = 0;
        long v = strtol(val, &end, 10);
        if (errno != ERANGE && end != val && v >= INT_MIN && v <= INT_MAX) {
            while (*end == ' ' || *end == '\t') {
                end++;
            }
            if (*end == ',' || *end == '}' || *end == '\0') {
                if (out_val) {
                    *out_val = (int)v;
                }
                return true;
            }
        }
    }
    return false;
}

static int json_get_int(const char *json, const char *key, int default_val)
{
    int v = default_val;
    json_get_int_ex(json, key, &v);
    return v;
}

static void handle_json_line(const char *line)
{
    if (!line) {
        return;
    }
    const char *start = skip_json_ws(line);
    size_t len = strlen(start);
    while (len > 0 && (start[len - 1] == ' ' || start[len - 1] == '\t' ||
                       start[len - 1] == '\r' || start[len - 1] == '\n')) {
        len--;
    }
    if (len < 2 || start[0] != '{' || start[len - 1] != '}') {
        return;
    }

    char type[24] = {0};
    char id[32] = {0};
    if (!json_get_str(line, "t", type, sizeof(type))) {
        return;
    }
    json_get_str(line, "id", id, sizeof(id));
    sanitize_json_id(id);

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
        int mic = -1;
        if (json_get_int_ex(line, "mic", &mic) && mic >= 0) {
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
        int mouth = -1;
        if (json_get_int_ex(line, "mouth", &mouth) && mouth >= 0) {
            ui_set_mouth(mouth);
        }
    } else if (strcmp(type, "mouth") == 0) {
        int v = -1;
        if (json_get_int_ex(line, "v", &v) && v >= 0) {
            ui_set_mouth(v);
        }
    } else if (strcmp(type, "dbg") == 0) {
        char quixi[16] = {0};
        char voip[16] = {0};
        char bt_spk[16] = {0};
        json_get_str(line, "quixi", quixi, sizeof(quixi));
        json_get_str(line, "voip", voip, sizeof(voip));
        json_get_str(line, "bt_spk", bt_spk, sizeof(bt_spk));
        int rtt = INT_MIN;
        if (json_get_int_ex(line, "srv_ms", &rtt)) {
            if (rtt < 0) {
                rtt = -1;
            }
        }
        ui_set_link_status(quixi[0] ? quixi : NULL,
                           voip[0] ? voip : NULL,
                           rtt,
                           bt_spk[0] ? bt_spk : NULL);
    } else if (strcmp(type, "cfg") == 0) {
        int bright = -1;
        int vol = -1;
        int ble_en = -1;
        int sleep_s = -1;
        char mode[16] = {0};

        if (json_get_int_ex(line, "bright", &bright)) {
            ui_set_brightness(bright);
        }
        if (json_get_int_ex(line, "vol", &vol)) {
            ui_set_volume(vol);
        }
        if (json_get_str(line, "mode", mode, sizeof(mode))) {
            ui_set_mic_mode_str(mode);
        }
        if (json_get_int_ex(line, "ble_en", &ble_en)) {
            ui_set_ble_enabled_ui(ble_en != 0);
        }
        if (json_get_int_ex(line, "sleep_s", &sleep_s)) {
            ui_set_sleep_timeout_s(sleep_s);
        }
        if (id[0] != '\0') {
            char resp[128];
            snprintf(resp, sizeof(resp),
                     "{\"t\":\"res\",\"id\":\"%s\",\"ok\":1,\"bright\":%d}",
                     id, ui_get_brightness());
            serial_send_json(resp);
        }
    } else if (strcmp(type, "gpio_cfg") == 0) {
        int pin = -1;
        int err = 0;
        if (!json_get_int_ex(line, "pin", &pin) || pin < 0) {
            err = -EINVAL;
        } else {
            char dir[12] = "in";
            char pull[12] = "none";
            char tmp_dir[12];
            char tmp_pull[12];
            if (json_get_str(line, "dir", tmp_dir, sizeof(tmp_dir))) {
                snprintf(dir, sizeof(dir), "%s", tmp_dir);
            } else if (find_json_key(line, "dir") != NULL) {
                dir[0] = '\0';
            }
            if (json_get_str(line, "pull", tmp_pull, sizeof(tmp_pull))) {
                snprintf(pull, sizeof(pull), "%s", tmp_pull);
            } else if (find_json_key(line, "pull") != NULL) {
                pull[0] = '\0';
            }
            int val = json_get_int(line, "val", 0);
            err = gpio_agent_configure(pin, dir, pull, val);
        }
        char resp[128];
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":%d,\"pin\":%d,\"err\":%d}",
                 id, (err == 0) ? 1 : 0, pin, err);
        serial_send_json(resp);
    } else if (strcmp(type, "gpio_set") == 0) {
        int pin = -1;
        int val = 0;
        int err = 0;
        if (!json_get_int_ex(line, "pin", &pin) || pin < 0 || !json_get_int_ex(line, "val", &val)) {
            err = -EINVAL;
        } else {
            err = gpio_agent_set(pin, val);
        }
        char resp[128];
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":%d,\"pin\":%d,\"val\":%d,\"err\":%d}",
                 id, (err == 0) ? 1 : 0, pin, val ? 1 : 0, err);
        serial_send_json(resp);
    } else if (strcmp(type, "gpio_get") == 0) {
        int pin = -1;
        int val = 0;
        int err = 0;
        if (!json_get_int_ex(line, "pin", &pin) || pin < 0) {
            err = -EINVAL;
        } else {
            err = gpio_agent_get(pin, &val);
        }
        int vbat = gpio_agent_read_vbat_mv();
        char resp[160];
        snprintf(resp, sizeof(resp),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":%d,\"pin\":%d,\"val\":%d,\"vbat_mv\":%d,\"err\":%d}",
                 id, (err == 0) ? 1 : 0, pin, val, vbat, err);
        serial_send_json(resp);
    } else if (strcmp(type, "ble_get") == 0) {
        int cnt = 0;
        ble_scanner_format_json_ex(ble_arr_buf, sizeof(ble_arr_buf), &cnt);
        snprintf(ble_resp_buf, sizeof(ble_resp_buf),
                 "{\"t\":\"res\",\"id\":\"%s\",\"ok\":1,\"cnt\":%d,\"ble\":%s}",
                 id, cnt, ble_arr_buf);
        serial_send_json(ble_resp_buf);
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

void serial_send_ble_evt(void)
{
    ble_scanner_format_json_ex(ble_arr_buf, sizeof(ble_arr_buf), NULL);
    snprintf(ble_resp_buf, sizeof(ble_resp_buf),
             "{\"t\":\"evt\",\"src\":\"ble\",\"ble\":%s}", ble_arr_buf);
    serial_send_json(ble_resp_buf);
}

void serial_proto_poll(void)
{
    static char line[RX_LINE_MAX];
    while (k_msgq_get(&rx_msgq, line, K_NO_WAIT) == 0) {
        handle_json_line(line);
    }
}
