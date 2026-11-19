#ifndef UI_SCREENS_H
#define UI_SCREENS_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    FACE_IDLE = 0,
    FACE_LISTENING,
    FACE_THINKING,
    FACE_SPEAKING,
    FACE_HAPPY,
    FACE_SAD,
    FACE_SURPRISED,
    FACE_SLEEPING,
    FACE_ALERT,
} face_emotion_t;

typedef struct {
    void (*on_face_tap)(bool currently_speaking);
    void (*on_cfg_change)(int bright, int vol, const char *mic_mode, int ble_en, int sleep_s);
    void (*on_dbg_test_call)(void);
    void (*on_dbg_ble_refresh)(void);
} ui_callbacks_t;

/**
 * @brief Initialize all 3 LVGL screens (Face, Settings, Debug) and load Home screen.
 */
void ui_screens_init(const ui_callbacks_t *cbs);

/**
 * @brief Periodic ~33FPS UI animation & 360-degree gravity rotation tick.
 */
void ui_screens_tick_anim(void);

/**
 * @brief Refresh telemetry labels on the Debug screen and Home battery/status.
 */
void ui_screens_update_telemetry(void);

/**
 * @brief Get current smoothed UI rotation angle in degrees (0..359).
 */
int ui_screens_get_angle_deg(void);

/**
 * @brief Set companion emotional expression by enum or string.
 */
void ui_set_emotion(face_emotion_t emo);
void ui_set_emotion_str(const char *emo_str);

/**
 * @brief Update bottom live subtitle box on the Face screen.
 */
void ui_set_subtitle(const char *text);

/**
 * @brief Update real-time mouth openness (0..100) for lip-sync viseme animation.
 */
void ui_set_mouth(int value);

/**
 * @brief Update QuIXI / VoIP / BT link status shown on Home & Debug screens.
 */
void ui_set_link_status(const char *quixi, const char *voip, int rtt_ms, const char *bt_spk);

/**
 * @brief Update current mic active state (listening indicator).
 */
void ui_set_mic_active(bool active);

/**
 * @brief Host-callable non-echoing config setters (update PWM / UI without emitting evt.cfg).
 */
void ui_set_brightness(int pct);
int ui_get_brightness(void);
void ui_set_volume(int vol);
void ui_set_mic_mode_str(const char *mode);
void ui_set_ble_enabled_ui(bool enabled);
void ui_set_sleep_timeout_s(int sleep_s);

#endif /* UI_SCREENS_H */
