#include "gpio_agent.h"

#include <stdio.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(gpio_agent, LOG_LEVEL_WRN);

static const int allowed_pins[] = { 15, 16, 17, 18, 21, 33, 34, 35, 36, 37 };
#define NUM_ALLOWED_PINS (sizeof(allowed_pins) / sizeof(allowed_pins[0]))

static const struct device *gpio0_dev;
static const struct device *gpio1_dev;
static bool pin_configured_out[NUM_ALLOWED_PINS];
static bool pin_configured_in[NUM_ALLOWED_PINS];
static char last_gpio_status[40] = "GPIO: idle";
static struct k_mutex gpio_mutex;

#if DT_NODE_EXISTS(DT_PATH(zephyr_user)) && DT_NODE_HAS_PROP(DT_PATH(zephyr_user), io_channels)
static const struct adc_dt_spec vbat_adc = ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), 0);
static bool adc_ready;
#else
static bool adc_ready;
#endif

static int resolve_pin(int pin, const struct device **port_out, gpio_pin_t *pin_out, int *slot_out)
{
    for (int i = 0; i < (int)NUM_ALLOWED_PINS; i++) {
        if (allowed_pins[i] == pin) {
            if (pin < 32) {
                if (!gpio0_dev || !device_is_ready(gpio0_dev)) {
                    return -ENODEV;
                }
                *port_out = gpio0_dev;
                *pin_out = (gpio_pin_t)pin;
            } else {
                if (!gpio1_dev || !device_is_ready(gpio1_dev)) {
                    return -ENODEV;
                }
                *port_out = gpio1_dev;
                *pin_out = (gpio_pin_t)(pin - 32);
            }
            if (slot_out) {
                *slot_out = i;
            }
            return 0;
        }
    }
    return -EACCES;
}

int gpio_agent_init(void)
{
    k_mutex_init(&gpio_mutex);
    memset(pin_configured_out, 0, sizeof(pin_configured_out));
    memset(pin_configured_in, 0, sizeof(pin_configured_in));

    gpio0_dev = DEVICE_DT_GET(DT_NODELABEL(gpio0));
    gpio1_dev = DEVICE_DT_GET(DT_NODELABEL(gpio1));

#if DT_NODE_EXISTS(DT_PATH(zephyr_user)) && DT_NODE_HAS_PROP(DT_PATH(zephyr_user), io_channels)
    if (adc_is_ready_dt(&vbat_adc)) {
        int err = adc_channel_setup_dt(&vbat_adc);
        if (err == 0) {
            adc_ready = true;
        } else {
            LOG_WRN("adc_channel_setup_dt failed: %d", err);
        }
    }
#endif

    return 0;
}

int gpio_agent_configure(int pin, const char *dir, const char *pull, int initial_val)
{
    const struct device *port = NULL;
    gpio_pin_t hw_pin = 0;
    int slot = -1;

    int err = resolve_pin(pin, &port, &hw_pin, &slot);
    if (err != 0) {
        k_mutex_lock(&gpio_mutex, K_FOREVER);
        snprintf(last_gpio_status, sizeof(last_gpio_status), "GPIO %d: DENIED", pin);
        k_mutex_unlock(&gpio_mutex);
        return err;
    }

    gpio_flags_t flags = 0;
    bool is_out = (dir != NULL && strcmp(dir, "out") == 0);

    if (is_out) {
        flags = GPIO_INPUT | (initial_val ? GPIO_OUTPUT_HIGH : GPIO_OUTPUT_LOW);
    } else {
        flags = GPIO_INPUT;
        if (pull != NULL) {
            if (strcmp(pull, "up") == 0) {
                flags |= GPIO_PULL_UP;
            } else if (strcmp(pull, "down") == 0) {
                flags |= GPIO_PULL_DOWN;
            }
        }
    }

    err = gpio_pin_configure(port, hw_pin, flags);
    k_mutex_lock(&gpio_mutex, K_FOREVER);
    if (err == 0) {
        pin_configured_out[slot] = is_out;
        pin_configured_in[slot] = !is_out;
        snprintf(last_gpio_status, sizeof(last_gpio_status), "P%d %s=%d",
                 pin, is_out ? "OUT" : "IN", is_out ? (initial_val ? 1 : 0) : 0);
    } else {
        snprintf(last_gpio_status, sizeof(last_gpio_status), "P%d ERR %d", pin, err);
    }
    k_mutex_unlock(&gpio_mutex);

    return err;
}

int gpio_agent_set(int pin, int val)
{
    const struct device *port = NULL;
    gpio_pin_t hw_pin = 0;
    int slot = -1;

    int err = resolve_pin(pin, &port, &hw_pin, &slot);
    if (err != 0) {
        return err;
    }

    if (!pin_configured_out[slot]) {
        return gpio_agent_configure(pin, "out", "none", val ? 1 : 0);
    }

    err = gpio_pin_set(port, hw_pin, val ? 1 : 0);
    k_mutex_lock(&gpio_mutex, K_FOREVER);
    if (err == 0) {
        snprintf(last_gpio_status, sizeof(last_gpio_status), "P%d OUT=%d", pin, val ? 1 : 0);
    }
    k_mutex_unlock(&gpio_mutex);

    return err;
}

int gpio_agent_get(int pin, int *val_out)
{
    const struct device *port = NULL;
    gpio_pin_t hw_pin = 0;
    int slot = -1;

    int err = resolve_pin(pin, &port, &hw_pin, &slot);
    if (err != 0) {
        return err;
    }

    if (!pin_configured_out[slot] && !pin_configured_in[slot]) {
        err = gpio_agent_configure(pin, "in", "none", 0);
        if (err != 0) {
            return err;
        }
    }

    int val = gpio_pin_get(port, hw_pin);
    if (val < 0) {
        return val;
    }

    if (val_out) {
        *val_out = val;
    }

    k_mutex_lock(&gpio_mutex, K_FOREVER);
    snprintf(last_gpio_status, sizeof(last_gpio_status), "P%d READ=%d", pin, val);
    k_mutex_unlock(&gpio_mutex);

    return 0;
}

int gpio_agent_read_vbat_mv(void)
{
#if DT_NODE_EXISTS(DT_PATH(zephyr_user)) && DT_NODE_HAS_PROP(DT_PATH(zephyr_user), io_channels)
    if (!adc_ready) {
        return 0;
    }

    int16_t buf = 0;
    struct adc_sequence seq = {
        .buffer = &buf,
        .buffer_size = sizeof(buf),
    };

    if (adc_sequence_init_dt(&vbat_adc, &seq) < 0) {
        return 0;
    }
    if (adc_read_dt(&vbat_adc, &seq) < 0) {
        return 0;
    }

    int32_t mv = buf;
    if (adc_raw_to_millivolts_dt(&vbat_adc, &mv) < 0) {
        /* Fallback for 12-bit ADC with ~3.1V full scale at 11dB/1_4 gain */
        mv = ((int32_t)buf * 3100) / 4095;
    }

    /* Waveshare 200k + 100k resistor divider scales Vbat by 1/3 */
    return (int)(mv * 3);
#else
    return 0;
#endif
}

void gpio_agent_get_status_str(char *buf, size_t len)
{
    if (!buf || len == 0) return;
    k_mutex_lock(&gpio_mutex, K_FOREVER);
    strncpy(buf, last_gpio_status, len - 1);
    buf[len - 1] = '\0';
    k_mutex_unlock(&gpio_mutex);
}
