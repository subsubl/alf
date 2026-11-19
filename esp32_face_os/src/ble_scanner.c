#include "ble_scanner.h"

#include <stdio.h>
#include <string.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(ble_scanner, LOG_LEVEL_WRN);

#define BLE_STALE_MS 30000

static struct k_mutex ble_mutex;
static ble_device_entry_t dev_table[BLE_MAX_DEVICES];
static bool bt_initialized;
static bool scan_enabled;

static void sanitize_name(const uint8_t *src, size_t len, char *dst, size_t dst_size)
{
    size_t j = 0;
    for (size_t i = 0; i < len && j + 1 < dst_size; i++) {
        char c = (char)src[i];
        if (c >= 0x20 && c <= 0x7E && c != '"' && c != '\\') {
            dst[j++] = c;
        }
    }
    dst[j] = '\0';
}

static bool parse_ad_name_cb(struct bt_data *data, void *user_data)
{
    char *name_buf = (char *)user_data;
    if (data->type == BT_DATA_NAME_COMPLETE || data->type == BT_DATA_NAME_SHORTENED) {
        sanitize_name(data->data, data->data_len, name_buf, 32);
        return false; /* stop parsing */
    }
    return true;
}

static void prune_stale_locked(int64_t now)
{
    for (int i = 0; i < BLE_MAX_DEVICES; i++) {
        if (dev_table[i].active && (now - dev_table[i].last_seen_ms) > BLE_STALE_MS) {
            dev_table[i].active = false;
        }
    }
}

static void scan_cb(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
                    struct net_buf_simple *ad)
{
    ARG_UNUSED(type);

    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
             addr->a.val[5], addr->a.val[4], addr->a.val[3],
             addr->a.val[2], addr->a.val[1], addr->a.val[0]);

    char parsed_name[32] = {0};
    if (ad != NULL && ad->len > 0) {
        bt_data_parse(ad, parse_ad_name_cb, parsed_name);
    }

    int64_t now = k_uptime_get();

    k_mutex_lock(&ble_mutex, K_FOREVER);
    prune_stale_locked(now);

    int empty_idx = -1;
    int oldest_idx = 0;
    int64_t oldest_ts = INT64_MAX;
    int match_idx = -1;

    for (int i = 0; i < BLE_MAX_DEVICES; i++) {
        if (dev_table[i].active) {
            if (strcmp(dev_table[i].mac, mac_str) == 0 &&
                dev_table[i].addr_type == addr->type) {
                match_idx = i;
                break;
            }
            if (dev_table[i].last_seen_ms < oldest_ts) {
                oldest_ts = dev_table[i].last_seen_ms;
                oldest_idx = i;
            }
        } else if (empty_idx < 0) {
            empty_idx = i;
        }
    }

    int target = (match_idx >= 0) ? match_idx : ((empty_idx >= 0) ? empty_idx : oldest_idx);
    dev_table[target].active = true;
    dev_table[target].addr_type = addr->type;
    strncpy(dev_table[target].mac, mac_str, sizeof(dev_table[target].mac) - 1);
    dev_table[target].mac[sizeof(dev_table[target].mac) - 1] = '\0';
    dev_table[target].rssi = rssi;
    dev_table[target].last_seen_ms = now;

    if (parsed_name[0] != '\0') {
        strncpy(dev_table[target].name, parsed_name, sizeof(dev_table[target].name) - 1);
        dev_table[target].name[sizeof(dev_table[target].name) - 1] = '\0';
    } else if (match_idx < 0) {
        dev_table[target].name[0] = '\0';
    }

    k_mutex_unlock(&ble_mutex);
}

int ble_scanner_init(void)
{
    k_mutex_init(&ble_mutex);
    memset(dev_table, 0, sizeof(dev_table));

    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("bt_enable failed (%d)", err);
        return err;
    }
    bt_initialized = true;
    return ble_scanner_set_enabled(true);
}

int ble_scanner_set_enabled(bool enable)
{
    if (!bt_initialized) {
        return -ENODEV;
    }
    if (enable == scan_enabled) {
        return 0;
    }

    if (enable) {
        struct bt_le_scan_param scan_param = {
            .type = BT_LE_SCAN_TYPE_PASSIVE,
            .options = BT_LE_SCAN_OPT_NONE,
            .interval = BT_GAP_SCAN_FAST_INTERVAL,
            .window = BT_GAP_SCAN_FAST_WINDOW,
        };
        int err = bt_le_scan_start(&scan_param, scan_cb);
        if (err) {
            LOG_ERR("bt_le_scan_start failed (%d)", err);
            return err;
        }
        scan_enabled = true;
    } else {
        int err = bt_le_scan_stop();
        if (err) {
            LOG_ERR("bt_le_scan_stop failed (%d)", err);
            return err;
        }
        scan_enabled = false;
    }

    return 0;
}

bool ble_scanner_is_enabled(void)
{
    return scan_enabled;
}

int ble_scanner_get_count(void)
{
    int count = 0;
    int64_t now = k_uptime_get();

    k_mutex_lock(&ble_mutex, K_FOREVER);
    prune_stale_locked(now);
    for (int i = 0; i < BLE_MAX_DEVICES; i++) {
        if (dev_table[i].active) {
            count++;
        }
    }
    k_mutex_unlock(&ble_mutex);

    return count;
}

bool ble_scanner_get_strongest(char *mac_out, size_t mac_len,
                               char *name_out, size_t name_len,
                               int8_t *rssi_out)
{
    int64_t now = k_uptime_get();
    int best_idx = -1;
    int8_t best_rssi = INT8_MIN;

    k_mutex_lock(&ble_mutex, K_FOREVER);
    prune_stale_locked(now);
    for (int i = 0; i < BLE_MAX_DEVICES; i++) {
        if (dev_table[i].active && (best_idx < 0 || dev_table[i].rssi > best_rssi)) {
            best_rssi = dev_table[i].rssi;
            best_idx = i;
        }
    }

    if (best_idx >= 0) {
        if (mac_out && mac_len > 0) {
            strncpy(mac_out, dev_table[best_idx].mac, mac_len - 1);
            mac_out[mac_len - 1] = '\0';
        }
        if (name_out && name_len > 0) {
            strncpy(name_out, dev_table[best_idx].name, name_len - 1);
            name_out[name_len - 1] = '\0';
        }
        if (rssi_out) {
            *rssi_out = best_rssi;
        }
    }
    k_mutex_unlock(&ble_mutex);

    return best_idx >= 0;
}

int ble_scanner_format_json_ex(char *buf, size_t len, int *cnt_out)
{
    if (!buf || len < 3) {
        return -EINVAL;
    }

    int64_t now = k_uptime_get();
    size_t pos = 0;
    buf[pos++] = '[';
    bool first = true;
    int count = 0;

    k_mutex_lock(&ble_mutex, K_FOREVER);
    prune_stale_locked(now);

    for (int i = 0; i < BLE_MAX_DEVICES; i++) {
        if (!dev_table[i].active) {
            continue;
        }
        char entry[128];
        int n = snprintf(entry, sizeof(entry),
                         "%s{\"mac\":\"%s\",\"type\":%u,\"name\":\"%s\",\"rssi\":%d}",
                         first ? "" : ",",
                         dev_table[i].mac,
                         (unsigned int)dev_table[i].addr_type,
                         dev_table[i].name[0] ? dev_table[i].name : "?",
                         (int)dev_table[i].rssi);
        if (n < 0 || (size_t)n >= sizeof(entry) || pos + (size_t)n + 2 > len) {
            break;
        }
        memcpy(buf + pos, entry, (size_t)n);
        pos += (size_t)n;
        first = false;
        count++;
    }
    k_mutex_unlock(&ble_mutex);

    if (cnt_out) {
        *cnt_out = count;
    }
    buf[pos++] = ']';
    buf[pos] = '\0';
    return (int)pos;
}

int ble_scanner_format_json(char *buf, size_t len)
{
    return ble_scanner_format_json_ex(buf, len, NULL);
}
