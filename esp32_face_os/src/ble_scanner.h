#ifndef BLE_SCANNER_H
#define BLE_SCANNER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BLE_MAX_DEVICES 16

typedef struct {
    bool active;
    char mac[18];
    char name[20];
    int8_t rssi;
    int64_t last_seen_ms;
} ble_device_entry_t;

/**
 * @brief Initialize Zephyr BLE stack and start passive observer scanning.
 * @return 0 on success, negative errno on failure.
 */
int ble_scanner_init(void);

/**
 * @brief Enable or pause background BLE passive scanning.
 */
int ble_scanner_set_enabled(bool enable);

/**
 * @brief Check whether BLE scanning is currently active.
 */
bool ble_scanner_is_enabled(void);

/**
 * @brief Return count of active devices seen within the last 30 seconds.
 */
int ble_scanner_get_count(void);

/**
 * @brief Retrieve strongest currently visible device (if any).
 * @return true if at least one active device exists.
 */
bool ble_scanner_get_strongest(char *mac_out, size_t mac_len,
                               char *name_out, size_t name_len,
                               int8_t *rssi_out);

/**
 * @brief Format active BLE devices as a JSON array into buf: [{"mac":"...","name":"...","rssi":-50},...]
 */
int ble_scanner_format_json(char *buf, size_t len);

#endif /* BLE_SCANNER_H */
