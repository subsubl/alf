#ifndef GPIO_AGENT_H
#define GPIO_AGENT_H

#include <stdbool.h>
#include <stddef.h>

/**
 * @brief Initialize GPIO agent and battery ADC channel.
 */
int gpio_agent_init(void);

/**
 * @brief Configure an allowlisted header GPIO pin dynamically.
 * @param pin GPIO number (allowlist: 15,16,17,18,21,33,34,35,36,37)
 * @param dir "in" or "out"
 * @param pull "none", "up", or "down"
 * @param initial_val 0 or 1 (for output)
 * @return 0 on success, -EACCES if reserved, negative errno otherwise.
 */
int gpio_agent_configure(int pin, const char *dir, const char *pull, int initial_val);

/**
 * @brief Set an allowlisted GPIO output level (auto-configures as output if needed).
 */
int gpio_agent_set(int pin, int val);

/**
 * @brief Read an allowlisted GPIO pin state (returns 0 or 1 in val_out).
 */
int gpio_agent_get(int pin, int *val_out);

/**
 * @brief Read battery voltage in millivolts (scaled for 1/3 voltage divider on GPIO 1).
 */
int gpio_agent_read_vbat_mv(void);

/**
 * @brief Get human-readable summary of last agent GPIO operation for the debug screen.
 */
void gpio_agent_get_status_str(char *buf, size_t len);

#endif /* GPIO_AGENT_H */
