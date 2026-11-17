#include "ui_screens.h"
#include "ble_scanner.h"
#include "gpio_agent.h"
#include "imu_qmi8658.h"

#include <lvgl.h>
#include <lvgl_display.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/drivers/display.h>
#include <zephyr/kernel.h>

static ui_callbacks_t g_cbs;

/* Screen objects */
static lv_obj_t *scr_face;
static lv_obj_t *scr_settings;
static lv_obj_t *scr_debug;
static int current_screen_idx;

/* Face screen widgets */
static lv_obj_t *face_canvas;
static lv_obj_t *status_dot;
static lv_obj_t *status_lbl;
static lv_obj_t *vbat_lbl;
static lv_obj_t *sub_lbl;

/* Settings screen widgets */
static lv_obj_t *cfg_bright_lbl;
static lv_obj_t *cfg_vol_lbl;
static lv_obj_t *cfg_mic_lbl;
static lv_obj_t *cfg_rot_lbl;
static lv_obj_t *cfg_ble_sw;
static int cfg_bright = 100;
static int cfg_vol = 75;
static bool cfg_mic_vad = true;
static bool cfg_ble_en = true;

/* 4-Step Hardware Rotation: 0 = Auto 4-Way, 1 = Auto Flip 0/180, 2 = Lock Upright (180) */
static int rot_mode = 0;
static enum display_orientation current_orient = DISPLAY_ORIENTATION_ROTATED_180;
static enum display_orientation pending_orient = DISPLAY_ORIENTATION_ROTATED_180;
static int orient_stable_ticks = 0;

/* Debug screen widgets */
static lv_obj_t *dbg_link_lbl;
static lv_obj_t *dbg_imu_lbl;
static lv_obj_t *dbg_ble_lbl;
static lv_obj_t *dbg_gpio_lbl;
static lv_obj_t *dbg_sys_lbl;

/* Procedural face animation state */
static face_emotion_t g_emotion = FACE_IDLE;
static int target_mouth = 0;
static int current_mouth = 0;
static uint32_t anim_frame = 0;
static int pet_blush_frames = 0;
static float gaze_x = 0.0f;
static float gaze_y = 0.0f;
static float gaze_target_x = 0.0f;
static float gaze_target_y = 0.0f;
static bool mic_active = false;

/* Link status strings */
static char link_quixi[16] = "WAIT";
static char link_voip[16]  = "IDLE";
static char link_bt[16]    = "WAIT";
static int link_rtt_ms     = -1;

/* ============================================================================
 * Hardware 4-Step Display & Touch Orientation
 * ============================================================================ */

static int orient_to_deg(enum display_orientation o)
{
    switch (o) {
    case DISPLAY_ORIENTATION_ROTATED_90:  return 90;
    case DISPLAY_ORIENTATION_ROTATED_180: return 180;
    case DISPLAY_ORIENTATION_ROTATED_270: return 270;
    default:                              return 0;
    }
}

static void apply_hardware_orientation(enum display_orientation new_orient)
{
    const struct device *disp_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_display));
    if (!device_is_ready(disp_dev)) {
        return;
    }

    if (display_set_orientation(disp_dev, new_orient) == 0) {
        current_orient = new_orient;
        lv_disp_t *disp = lv_disp_get_default();
        if (disp && disp->driver && disp->driver->user_data) {
            struct lvgl_disp_data *disp_data = (struct lvgl_disp_data *)disp->driver->user_data;
            display_get_capabilities(disp_dev, &disp_data->cap);
        }
        lv_obj_t *act = lv_scr_act();
        if (act) {
            lv_obj_invalidate(act);
        }
    }
}

static void update_4step_rotation(void)
{
    enum display_orientation desired = current_orient;

    if (rot_mode == 2) {
        desired = DISPLAY_ORIENTATION_ROTATED_180;
    } else {
        imu_data_t imu;
        imu_qmi8658_get_latest(&imu);
        if (imu.ready) {
            float in_plane = sqrtf(imu.ax * imu.ax + imu.ay * imu.ay);
            if (in_plane > 3.0f) {
                float abs_x = fabsf(imu.ax);
                float abs_y = fabsf(imu.ay);

                if (rot_mode == 1) {
                    /* Flip 0 / 180 only */
                    if (abs_x > 2.5f) {
                        desired = (imu.ax > 0.0f) ? DISPLAY_ORIENTATION_ROTATED_180
                                                  : DISPLAY_ORIENTATION_NORMAL;
                    }
                } else {
                    /* Full 4-step with 1.35x dominance hysteresis */
                    if (abs_x > abs_y * 1.35f) {
                        desired = (imu.ax > 0.0f) ? DISPLAY_ORIENTATION_ROTATED_180
                                                  : DISPLAY_ORIENTATION_NORMAL;
                    } else if (abs_y > abs_x * 1.35f) {
                        desired = (imu.ay < 0.0f) ? DISPLAY_ORIENTATION_ROTATED_90
                                                  : DISPLAY_ORIENTATION_ROTATED_270;
                    }
                }
            }
        }
    }

    if (desired != current_orient) {
        if (desired == pending_orient) {
            orient_stable_ticks++;
            if (orient_stable_ticks >= 4) { /* ~120ms debounce */
                orient_stable_ticks = 0;
                apply_hardware_orientation(desired);
            }
        } else {
            pending_orient = desired;
            orient_stable_ticks = 1;
        }
    } else {
        pending_orient = current_orient;
        orient_stable_ticks = 0;
    }
}

/* ============================================================================
 * Screen Navigation & Input Callbacks
 * ============================================================================ */

static void switch_to_screen(int idx)
{
    if (idx < 0) idx = 2;
    if (idx > 2) idx = 0;
    current_screen_idx = idx;

    if (idx == 0) {
        lv_scr_load_anim(scr_face, LV_SCR_LOAD_ANIM_FADE_ON, 120, 0, false);
    } else if (idx == 1) {
        lv_scr_load_anim(scr_settings, LV_SCR_LOAD_ANIM_FADE_ON, 120, 0, false);
    } else {
        lv_scr_load_anim(scr_debug, LV_SCR_LOAD_ANIM_FADE_ON, 120, 0, false);
    }
}

static void nav_prev_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    switch_to_screen(current_screen_idx - 1);
}

static void nav_next_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    switch_to_screen(current_screen_idx + 1);
}

static void screen_gesture_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_get_act());
    if (dir == LV_DIR_LEFT) {
        switch_to_screen(current_screen_idx + 1);
    } else if (dir == LV_DIR_RIGHT) {
        switch_to_screen(current_screen_idx - 1);
    }
}

static void add_nav_header(lv_obj_t *parent, const char *title)
{
    lv_obj_t *btn_prev = lv_btn_create(parent);
    lv_obj_set_size(btn_prev, 28, 24);
    lv_obj_align(btn_prev, LV_ALIGN_TOP_MID, -54, 16);
    lv_obj_set_style_radius(btn_prev, 12, 0);
    lv_obj_set_style_bg_color(btn_prev, lv_color_hex(0x1E293B), 0);
    lv_obj_set_style_pad_all(btn_prev, 0, 0);
    lv_obj_add_event_cb(btn_prev, nav_prev_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lbl_p = lv_label_create(btn_prev);
    lv_label_set_text(lbl_p, "<");
    lv_obj_center(lbl_p);

    lv_obj_t *btn_next = lv_btn_create(parent);
    lv_obj_set_size(btn_next, 28, 24);
    lv_obj_align(btn_next, LV_ALIGN_TOP_MID, 54, 16);
    lv_obj_set_style_radius(btn_next, 12, 0);
    lv_obj_set_style_bg_color(btn_next, lv_color_hex(0x1E293B), 0);
    lv_obj_set_style_pad_all(btn_next, 0, 0);
    lv_obj_add_event_cb(btn_next, nav_next_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lbl_n = lv_label_create(btn_next);
    lv_label_set_text(lbl_n, ">");
    lv_obj_center(lbl_n);

    if (title != NULL) {
        lv_obj_t *hdr = lv_label_create(parent);
        lv_label_set_text(hdr, title);
        lv_obj_set_style_text_color(hdr, lv_color_hex(0x38BDF8), 0);
        lv_obj_set_style_text_font(hdr, &lv_font_montserrat_14, 0);
        lv_obj_align(hdr, LV_ALIGN_TOP_MID, 0, 19);
    }

    lv_obj_add_event_cb(parent, screen_gesture_cb, LV_EVENT_GESTURE, NULL);
    lv_obj_clear_flag(parent, LV_OBJ_FLAG_GESTURE_BUBBLE);
}

static void face_tap_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    pet_blush_frames = 60;
    if (g_cbs.on_face_tap) {
        g_cbs.on_face_tap(g_emotion == FACE_SPEAKING);
    }
}

static void emit_cfg(void)
{
    if (g_cbs.on_cfg_change) {
        g_cbs.on_cfg_change(cfg_bright, cfg_vol, cfg_mic_vad ? "vad" : "ptt", cfg_ble_en ? 1 : 0);
    }
}

static void bright_cycle_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if (cfg_bright == 100) cfg_bright = 25;
    else if (cfg_bright == 25) cfg_bright = 50;
    else cfg_bright = 100;

    char buf[24];
    snprintf(buf, sizeof(buf), "Bright: %d%%", cfg_bright);
    lv_label_set_text(cfg_bright_lbl, buf);
    emit_cfg();
}

static void vol_down_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    cfg_vol -= 15;
    if (cfg_vol < 0) cfg_vol = 0;
    char buf[16];
    snprintf(buf, sizeof(buf), "Vol:%d%%", cfg_vol);
    lv_label_set_text(cfg_vol_lbl, buf);
    emit_cfg();
}

static void vol_up_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    cfg_vol += 15;
    if (cfg_vol > 100) cfg_vol = 100;
    char buf[16];
    snprintf(buf, sizeof(buf), "Vol:%d%%", cfg_vol);
    lv_label_set_text(cfg_vol_lbl, buf);
    emit_cfg();
}

static void mic_mode_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    cfg_mic_vad = !cfg_mic_vad;
    lv_label_set_text(cfg_mic_lbl, cfg_mic_vad ? "Mic: Auto VAD" : "Mic: Tap PTT");
    emit_cfg();
}

static void rot_mode_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    rot_mode = (rot_mode + 1) % 3;
    if (rot_mode == 0) {
        lv_label_set_text(cfg_rot_lbl, "Rot: Auto 4-Way");
    } else if (rot_mode == 1) {
        lv_label_set_text(cfg_rot_lbl, "Rot: Flip 180");
    } else {
        lv_label_set_text(cfg_rot_lbl, "Rot: Lock Up");
    }
}

static void ble_switch_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    cfg_ble_en = lv_obj_has_state(cfg_ble_sw, LV_STATE_CHECKED);
    ble_scanner_set_enabled(cfg_ble_en);
    emit_cfg();
}

static void dbg_ping_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if (g_cbs.on_dbg_ping) {
        g_cbs.on_dbg_ping();
    }
}

static void dbg_ble_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    if (g_cbs.on_dbg_ble_refresh) {
        g_cbs.on_dbg_ble_refresh();
    }
}

/* ============================================================================
 * Fast Native-Speed Procedural Vector Companion Face
 * ============================================================================ */

static inline lv_point_t pt(float lx, float ly)
{
    return (lv_point_t){
        .x = (lv_coord_t)lroundf(120.0f + lx),
        .y = (lv_coord_t)lroundf(114.0f + ly),
    };
}

static void draw_filled_circle(lv_draw_ctx_t *draw_ctx, const lv_point_t *c, int r,
                               lv_color_t color, lv_opa_t opa)
{
    if (r <= 0) return;
    lv_draw_rect_dsc_t dsc;
    lv_draw_rect_dsc_init(&dsc);
    dsc.bg_color = color;
    dsc.bg_opa = opa;
    dsc.radius = LV_RADIUS_CIRCLE;
    dsc.border_width = 0;

    lv_area_t area = {
        .x1 = c->x - r,
        .y1 = c->y - r,
        .x2 = c->x + r,
        .y2 = c->y + r,
    };
    lv_draw_rect(draw_ctx, &dsc, &area);
}

static void draw_face_line(lv_draw_ctx_t *draw_ctx, float x1, float y1, float x2, float y2,
                           lv_coord_t width, lv_color_t color, lv_opa_t opa)
{
    lv_point_t p1 = pt(x1, y1);
    lv_point_t p2 = pt(x2, y2);

    lv_draw_line_dsc_t dsc;
    lv_draw_line_dsc_init(&dsc);
    dsc.color = color;
    dsc.width = width;
    dsc.opa = opa;
    dsc.round_start = 1;
    dsc.round_end = 1;
    lv_draw_line(draw_ctx, &dsc, &p1, &p2);
}

static void draw_single_eye(lv_draw_ctx_t *draw_ctx, float ex, float ey,
                            lv_color_t iris_col, bool blink_now)
{
    if (g_emotion == FACE_HAPPY) {
        lv_point_t ec = pt(ex, ey + 6.0f);
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.color = iris_col;
        arc.width = 6;
        arc.rounded = 1;
        lv_draw_arc(draw_ctx, &arc, &ec, 17, 205, 335);
        return;
    }

    if (g_emotion == FACE_SLEEPING || blink_now) {
        lv_point_t ec = pt(ex, ey - 2.0f);
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.color = (g_emotion == FACE_SLEEPING) ? lv_color_hex(0x64748B) : iris_col;
        arc.width = 4;
        arc.rounded = 1;
        lv_draw_arc(draw_ctx, &arc, &ec, 16, 25, 155);
        return;
    }

    int eye_r = 19;
    if (g_emotion == FACE_LISTENING) eye_r = 21;
    else if (g_emotion == FACE_SURPRISED) eye_r = 22;
    else if (g_emotion == FACE_SAD) eye_r = 16;

    lv_point_t eye_c = pt(ex, ey);
    draw_filled_circle(draw_ctx, &eye_c, eye_r + 4, iris_col, LV_OPA_20);
    draw_filled_circle(draw_ctx, &eye_c, eye_r, iris_col, LV_OPA_COVER);

    lv_point_t pupil_c = pt(ex + gaze_x, ey + gaze_y);
    draw_filled_circle(draw_ctx, &pupil_c, eye_r - 7, lv_color_hex(0x0B1220), LV_OPA_90);

    lv_point_t glint1 = pt(ex + gaze_x - 6.0f, ey + gaze_y - 7.0f);
    lv_point_t glint2 = pt(ex + gaze_x + 5.0f, ey + gaze_y + 5.0f);
    draw_filled_circle(draw_ctx, &glint1, 5, lv_color_white(), LV_OPA_COVER);
    draw_filled_circle(draw_ctx, &glint2, 2, lv_color_white(), LV_OPA_90);

    if (g_emotion == FACE_SAD) {
        float dir = (ex < 0) ? -1.0f : 1.0f;
        draw_face_line(draw_ctx, ex - 18.0f * dir, ey - 10.0f, ex + 18.0f * dir, ey - 18.0f,
                       8, lv_color_hex(0x0B0F17), LV_OPA_COVER);
    } else if (g_emotion == FACE_ALERT) {
        float dir = (ex < 0) ? 1.0f : -1.0f;
        draw_face_line(draw_ctx, ex - 18.0f * dir, ey - 16.0f, ex + 18.0f * dir, ey - 9.0f,
                       8, lv_color_hex(0x0B0F17), LV_OPA_COVER);
    }
}

static void draw_eyebrows(lv_draw_ctx_t *draw_ctx, lv_color_t brow_col)
{
    if (g_emotion == FACE_SLEEPING) return;

    float ly_in = -38.0f, ly_out = -36.0f;
    float ry_in = -38.0f, ry_out = -36.0f;

    switch (g_emotion) {
    case FACE_HAPPY:
    case FACE_LISTENING:
        ly_in = -43.0f; ly_out = -39.0f;
        ry_in = -43.0f; ry_out = -39.0f;
        break;
    case FACE_SURPRISED:
        ly_in = -48.0f; ly_out = -44.0f;
        ry_in = -48.0f; ry_out = -44.0f;
        break;
    case FACE_THINKING:
        ly_in = -37.0f; ly_out = -36.0f;
        ry_in = -46.0f; ry_out = -43.0f;
        break;
    case FACE_SAD:
        ly_in = -43.0f; ly_out = -34.0f;
        ry_in = -43.0f; ry_out = -34.0f;
        break;
    case FACE_ALERT:
        ly_in = -32.0f; ly_out = -44.0f;
        ry_in = -32.0f; ry_out = -44.0f;
        break;
    default:
        break;
    }

    draw_face_line(draw_ctx, -50.0f, ly_out, -24.0f, ly_in, 4, brow_col, LV_OPA_90);
    draw_face_line(draw_ctx, 24.0f, ry_in, 50.0f, ry_out, 4, brow_col, LV_OPA_90);
}

static void draw_mouth(lv_draw_ctx_t *draw_ctx, lv_color_t accent_col)
{
    if (g_emotion == FACE_SPEAKING || current_mouth > 6) {
        int open_r = 9 + (current_mouth * 11) / 100;
        lv_point_t mc = pt(0.0f, 24.0f);
        draw_filled_circle(draw_ctx, &mc, open_r + 3, accent_col, LV_OPA_COVER);
        draw_filled_circle(draw_ctx, &mc, open_r, lv_color_hex(0x0F172A), LV_OPA_COVER);

        if (open_r >= 10) {
            lv_point_t tc = pt(0.0f, 24.0f + open_r * 0.42f);
            draw_filled_circle(draw_ctx, &tc, open_r / 2, lv_color_hex(0xFB7185), LV_OPA_90);
        }
        return;
    }

    if (g_emotion == FACE_SURPRISED) {
        lv_point_t mc = pt(0.0f, 25.0f);
        draw_filled_circle(draw_ctx, &mc, 12, accent_col, LV_OPA_COVER);
        draw_filled_circle(draw_ctx, &mc, 8, lv_color_hex(0x0F172A), LV_OPA_COVER);
        return;
    }

    if (g_emotion == FACE_SAD) {
        lv_point_t mc = pt(0.0f, 38.0f);
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.color = accent_col;
        arc.width = 4;
        arc.rounded = 1;
        lv_draw_arc(draw_ctx, &arc, &mc, 18, 215, 325);
        return;
    }

    if (g_emotion == FACE_THINKING) {
        draw_face_line(draw_ctx, -12.0f, 25.0f, 12.0f, 22.0f, 4, accent_col, LV_OPA_COVER);
        return;
    }

    if (g_emotion == FACE_SLEEPING) {
        lv_point_t mc = pt(0.0f, 24.0f);
        draw_filled_circle(draw_ctx, &mc, 5, lv_color_hex(0x64748B), LV_OPA_80);
        return;
    }

    lv_point_t mc = pt(0.0f, 12.0f);
    lv_draw_arc_dsc_t arc;
    lv_draw_arc_dsc_init(&arc);
    arc.color = accent_col;
    arc.width = (g_emotion == FACE_HAPPY) ? 5 : 4;
    arc.rounded = 1;
    uint16_t span = (g_emotion == FACE_HAPPY) ? 110 : 76;
    lv_draw_arc(draw_ctx, &arc, &mc, 20, 90 - span / 2, 90 + span / 2);
}

static void draw_extra_fx(lv_draw_ctx_t *draw_ctx, lv_color_t accent_col)
{
    if (g_emotion == FACE_THINKING) {
        for (int i = 0; i < 3; i++) {
            float phase = (anim_frame * 0.16f) + i * 0.8f;
            lv_point_t dp = pt(-16.0f + i * 16.0f, -54.0f + sinf(phase) * 4.0f);
            draw_filled_circle(draw_ctx, &dp, 3, accent_col, LV_OPA_90);
        }
    }

    if (g_emotion == FACE_SLEEPING) {
        float drift = (float)(anim_frame % 45) * 0.45f;
        float zx = 46.0f;
        float zy = -32.0f - drift;
        float sz = 6.0f;
        lv_opa_t opa = (anim_frame % 45 > 35) ? LV_OPA_40 : LV_OPA_90;
        draw_face_line(draw_ctx, zx - sz, zy - sz, zx + sz, zy - sz, 2, lv_color_hex(0x94A3B8), opa);
        draw_face_line(draw_ctx, zx + sz, zy - sz, zx - sz, zy + sz, 2, lv_color_hex(0x94A3B8), opa);
        draw_face_line(draw_ctx, zx - sz, zy + sz, zx + sz, zy + sz, 2, lv_color_hex(0x94A3B8), opa);
    }
}

static void face_draw_event_cb(lv_event_t *e)
{
    lv_draw_ctx_t *draw_ctx = lv_event_get_draw_ctx(e);
    if (!draw_ctx) return;

    bool blink_now = (g_emotion != FACE_SLEEPING && g_emotion != FACE_THINKING &&
                      g_emotion != FACE_HAPPY) && ((anim_frame % 115) >= 110);

    lv_color_t iris_col = lv_color_hex(0x38BDF8);
    switch (g_emotion) {
    case FACE_LISTENING: iris_col = lv_color_hex(0x22C55E); break;
    case FACE_THINKING:  iris_col = lv_color_hex(0xA855F7); break;
    case FACE_SPEAKING:  iris_col = lv_color_hex(0x38BDF8); break;
    case FACE_HAPPY:     iris_col = lv_color_hex(0xFACC15); break;
    case FACE_SAD:       iris_col = lv_color_hex(0x60A5FA); break;
    case FACE_SURPRISED: iris_col = lv_color_hex(0xFB923C); break;
    case FACE_SLEEPING:  iris_col = lv_color_hex(0x475569); break;
    case FACE_ALERT:     iris_col = lv_color_hex(0xEF4444); break;
    default: break;
    }

    draw_extra_fx(draw_ctx, iris_col);

    if (g_emotion == FACE_HAPPY || g_emotion == FACE_SPEAKING || pet_blush_frames > 0) {
        lv_point_t b_l = pt(-54.0f, 14.0f);
        lv_point_t b_r = pt(54.0f, 14.0f);
        draw_filled_circle(draw_ctx, &b_l, 9, lv_color_hex(0xFB7185), LV_OPA_40);
        draw_filled_circle(draw_ctx, &b_r, 9, lv_color_hex(0xFB7185), LV_OPA_40);
    }

    draw_single_eye(draw_ctx, -38.0f, -14.0f, iris_col, blink_now);
    draw_single_eye(draw_ctx, 38.0f, -14.0f, iris_col, blink_now);
    draw_eyebrows(draw_ctx, iris_col);
    draw_mouth(draw_ctx, iris_col);
}

/* ============================================================================
 * Screen Builders
 * ============================================================================ */

static void build_face_screen(void)
{
    scr_face = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_face, lv_color_hex(0x0B0F17), 0);

    /* Procedural face canvas covering central face region */
    face_canvas = lv_obj_create(scr_face);
    lv_obj_set_size(face_canvas, 240, 240);
    lv_obj_set_pos(face_canvas, 0, 0);
    lv_obj_set_style_bg_opa(face_canvas, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(face_canvas, 0, 0);
    lv_obj_set_style_pad_all(face_canvas, 0, 0);
    lv_obj_add_event_cb(face_canvas, face_draw_event_cb, LV_EVENT_DRAW_MAIN, NULL);
    lv_obj_add_event_cb(face_canvas, face_tap_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_add_event_cb(face_canvas, screen_gesture_cb, LV_EVENT_GESTURE, NULL);

    add_nav_header(scr_face, NULL);

    status_dot = lv_obj_create(scr_face);
    lv_obj_set_size(status_dot, 8, 8);
    lv_obj_set_style_radius(status_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(status_dot, 0, 0);
    lv_obj_set_style_bg_color(status_dot, lv_color_hex(0xF59E0B), 0);
    lv_obj_align(status_dot, LV_ALIGN_TOP_MID, -22, 24);

    status_lbl = lv_label_create(scr_face);
    lv_label_set_text(status_lbl, "WAIT");
    lv_obj_set_style_text_color(status_lbl, lv_color_hex(0x94A3B8), 0);
    lv_obj_set_style_text_font(status_lbl, &lv_font_montserrat_12, 0);
    lv_obj_align(status_lbl, LV_ALIGN_TOP_MID, 8, 21);

    vbat_lbl = lv_label_create(scr_face);
    lv_label_set_text(vbat_lbl, "USB");
    lv_obj_set_style_text_color(vbat_lbl, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(vbat_lbl, &lv_font_montserrat_12, 0);
    lv_obj_align(vbat_lbl, LV_ALIGN_TOP_MID, 0, 37);

    sub_lbl = lv_label_create(scr_face);
    lv_obj_set_width(sub_lbl, 168);
    lv_label_set_long_mode(sub_lbl, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(sub_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(sub_lbl, lv_color_hex(0xE2E8F0), 0);
    lv_obj_set_style_text_font(sub_lbl, &lv_font_montserrat_12, 0);
    lv_label_set_text(sub_lbl, "Tap face to speak");
    lv_obj_align(sub_lbl, LV_ALIGN_BOTTOM_MID, 0, -24);
}

static void build_settings_screen(void)
{
    scr_settings = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_settings, lv_color_hex(0x0F172A), 0);
    add_nav_header(scr_settings, "SETTINGS");

    lv_obj_t *btn_bright = lv_btn_create(scr_settings);
    lv_obj_set_size(btn_bright, 138, 26);
    lv_obj_align(btn_bright, LV_ALIGN_CENTER, 0, -50);
    lv_obj_set_style_radius(btn_bright, 10, 0);
    lv_obj_set_style_bg_color(btn_bright, lv_color_hex(0x1E293B), 0);
    lv_obj_add_event_cb(btn_bright, bright_cycle_cb, LV_EVENT_CLICKED, NULL);
    cfg_bright_lbl = lv_label_create(btn_bright);
    lv_label_set_text(cfg_bright_lbl, "Bright: 100%");
    lv_obj_set_style_text_font(cfg_bright_lbl, &lv_font_montserrat_12, 0);
    lv_obj_center(cfg_bright_lbl);

    lv_obj_t *btn_vdn = lv_btn_create(scr_settings);
    lv_obj_set_size(btn_vdn, 34, 26);
    lv_obj_align(btn_vdn, LV_ALIGN_CENTER, -52, -16);
    lv_obj_set_style_bg_color(btn_vdn, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(btn_vdn, vol_down_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l_dn = lv_label_create(btn_vdn);
    lv_label_set_text(l_dn, "-");
    lv_obj_center(l_dn);

    cfg_vol_lbl = lv_label_create(scr_settings);
    lv_label_set_text(cfg_vol_lbl, "Vol:75%");
    lv_obj_set_style_text_color(cfg_vol_lbl, lv_color_hex(0xF8FAFC), 0);
    lv_obj_set_style_text_font(cfg_vol_lbl, &lv_font_montserrat_12, 0);
    lv_obj_align(cfg_vol_lbl, LV_ALIGN_CENTER, 0, -16);

    lv_obj_t *btn_vup = lv_btn_create(scr_settings);
    lv_obj_set_size(btn_vup, 34, 26);
    lv_obj_align(btn_vup, LV_ALIGN_CENTER, 52, -16);
    lv_obj_set_style_bg_color(btn_vup, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(btn_vup, vol_up_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l_up = lv_label_create(btn_vup);
    lv_label_set_text(l_up, "+");
    lv_obj_center(l_up);

    lv_obj_t *btn_mic = lv_btn_create(scr_settings);
    lv_obj_set_size(btn_mic, 138, 26);
    lv_obj_align(btn_mic, LV_ALIGN_CENTER, 0, 18);
    lv_obj_set_style_radius(btn_mic, 10, 0);
    lv_obj_set_style_bg_color(btn_mic, lv_color_hex(0x0284C7), 0);
    lv_obj_add_event_cb(btn_mic, mic_mode_cb, LV_EVENT_CLICKED, NULL);
    cfg_mic_lbl = lv_label_create(btn_mic);
    lv_label_set_text(cfg_mic_lbl, "Mic: Auto VAD");
    lv_obj_set_style_text_font(cfg_mic_lbl, &lv_font_montserrat_12, 0);
    lv_obj_center(cfg_mic_lbl);

    lv_obj_t *btn_rot = lv_btn_create(scr_settings);
    lv_obj_set_size(btn_rot, 138, 26);
    lv_obj_align(btn_rot, LV_ALIGN_CENTER, 0, 50);
    lv_obj_set_style_radius(btn_rot, 10, 0);
    lv_obj_set_style_bg_color(btn_rot, lv_color_hex(0x4F46E5), 0);
    lv_obj_add_event_cb(btn_rot, rot_mode_cb, LV_EVENT_CLICKED, NULL);
    cfg_rot_lbl = lv_label_create(btn_rot);
    lv_label_set_text(cfg_rot_lbl, "Rot: Auto 4-Way");
    lv_obj_set_style_text_font(cfg_rot_lbl, &lv_font_montserrat_12, 0);
    lv_obj_center(cfg_rot_lbl);

    lv_obj_t *ble_lbl = lv_label_create(scr_settings);
    lv_label_set_text(ble_lbl, "BLE");
    lv_obj_set_style_text_color(ble_lbl, lv_color_hex(0xCBD5E1), 0);
    lv_obj_set_style_text_font(ble_lbl, &lv_font_montserrat_12, 0);
    lv_obj_align(ble_lbl, LV_ALIGN_CENTER, -28, 82);

    cfg_ble_sw = lv_switch_create(scr_settings);
    lv_obj_set_size(cfg_ble_sw, 44, 22);
    lv_obj_align(cfg_ble_sw, LV_ALIGN_CENTER, 22, 82);
    lv_obj_add_state(cfg_ble_sw, LV_STATE_CHECKED);
    lv_obj_add_event_cb(cfg_ble_sw, ble_switch_cb, LV_EVENT_VALUE_CHANGED, NULL);
}

static void build_debug_screen(void)
{
    scr_debug = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_debug, lv_color_hex(0x090D16), 0);
    add_nav_header(scr_debug, "DEBUG");

    dbg_link_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_link_lbl, lv_color_hex(0x34D399), 0);
    lv_obj_set_style_text_font(dbg_link_lbl, &lv_font_montserrat_12, 0);
    lv_label_set_text(dbg_link_lbl, "Q:WAIT V:IDLE");
    lv_obj_align(dbg_link_lbl, LV_ALIGN_TOP_MID, 0, 56);

    dbg_imu_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_imu_lbl, lv_color_hex(0xE2E8F0), 0);
    lv_obj_set_style_text_font(dbg_imu_lbl, &lv_font_montserrat_12, 0);
    lv_label_set_text(dbg_imu_lbl, "IMU: waiting...");
    lv_obj_align(dbg_imu_lbl, LV_ALIGN_TOP_MID, 0, 80);

    dbg_ble_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_ble_lbl, lv_color_hex(0x93C5FD), 0);
    lv_obj_set_style_text_font(dbg_ble_lbl, &lv_font_montserrat_12, 0);
    lv_label_set_text(dbg_ble_lbl, "BLE: 0 devs");
    lv_obj_align(dbg_ble_lbl, LV_ALIGN_TOP_MID, 0, 104);

    dbg_gpio_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_gpio_lbl, lv_color_hex(0xFBBF24), 0);
    lv_obj_set_style_text_font(dbg_gpio_lbl, &lv_font_montserrat_12, 0);
    lv_label_set_text(dbg_gpio_lbl, "GPIO: idle");
    lv_obj_align(dbg_gpio_lbl, LV_ALIGN_TOP_MID, 0, 128);

    dbg_sys_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_sys_lbl, lv_color_hex(0x94A3B8), 0);
    lv_obj_set_style_text_font(dbg_sys_lbl, &lv_font_montserrat_12, 0);
    lv_label_set_text(dbg_sys_lbl, "Rot:180 deg");
    lv_obj_align(dbg_sys_lbl, LV_ALIGN_TOP_MID, 0, 152);

    lv_obj_t *btn_ping = lv_btn_create(scr_debug);
    lv_obj_set_size(btn_ping, 62, 26);
    lv_obj_align(btn_ping, LV_ALIGN_BOTTOM_MID, -36, -28);
    lv_obj_set_style_bg_color(btn_ping, lv_color_hex(0x2563EB), 0);
    lv_obj_add_event_cb(btn_ping, dbg_ping_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lp = lv_label_create(btn_ping);
    lv_label_set_text(lp, "PING");
    lv_obj_set_style_text_font(lp, &lv_font_montserrat_12, 0);
    lv_obj_center(lp);

    lv_obj_t *btn_ble = lv_btn_create(scr_debug);
    lv_obj_set_size(btn_ble, 62, 26);
    lv_obj_align(btn_ble, LV_ALIGN_BOTTOM_MID, 36, -28);
    lv_obj_set_style_bg_color(btn_ble, lv_color_hex(0x0D9488), 0);
    lv_obj_add_event_cb(btn_ble, dbg_ble_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lb = lv_label_create(btn_ble);
    lv_label_set_text(lb, "BLE");
    lv_obj_set_style_text_font(lb, &lv_font_montserrat_12, 0);
    lv_obj_center(lb);
}

/* ============================================================================
 * Public API
 * ============================================================================ */

void ui_screens_init(const ui_callbacks_t *cbs)
{
    if (cbs) {
        g_cbs = *cbs;
    }

    build_face_screen();
    build_settings_screen();
    build_debug_screen();

    current_screen_idx = 0;
    apply_hardware_orientation(DISPLAY_ORIENTATION_ROTATED_180);
    lv_scr_load(scr_face);
}

void ui_screens_tick_anim(void)
{
    anim_frame++;
    if (pet_blush_frames > 0) {
        pet_blush_frames--;
    }

    update_4step_rotation();

    if (g_emotion == FACE_SPEAKING) {
        current_mouth += (target_mouth - current_mouth) / 2;
        if (current_mouth == 0 && target_mouth > 0) {
            current_mouth = target_mouth;
        }
    } else {
        target_mouth = 0;
        current_mouth = 0;
    }

    if ((anim_frame % 75) == 0) {
        if (g_emotion == FACE_IDLE || g_emotion == FACE_HAPPY) {
            int phase = (int)((anim_frame / 75) % 5);
            static const float gx_tab[5] = { 0.0f, -3.5f, 3.5f, 0.0f, 2.0f };
            static const float gy_tab[5] = { 0.0f, -1.0f, -1.0f, -2.5f, 1.5f };
            gaze_target_x = gx_tab[phase];
            gaze_target_y = gy_tab[phase];
        } else if (g_emotion == FACE_THINKING) {
            gaze_target_x = 4.0f;
            gaze_target_y = -4.0f;
        } else {
            gaze_target_x = 0.0f;
            gaze_target_y = 0.0f;
        }
    }
    gaze_x += (gaze_target_x - gaze_x) * 0.18f;
    gaze_y += (gaze_target_y - gaze_y) * 0.18f;

    /* Invalidate only the central face bounding box for fast SPI updates */
    if (current_screen_idx == 0 && face_canvas != NULL) {
        static const lv_area_t face_dirty = { .x1 = 35, .y1 = 44, .x2 = 205, .y2 = 166 };
        lv_obj_invalidate_area(face_canvas, &face_dirty);
    }
}

void ui_screens_update_telemetry(void)
{
    int vbat_mv = gpio_agent_read_vbat_mv();
    char vbuf[16];
    if (vbat_mv > 500) {
        snprintf(vbuf, sizeof(vbuf), "%d.%02dV", vbat_mv / 1000, (vbat_mv % 1000) / 10);
    } else {
        snprintf(vbuf, sizeof(vbuf), "USB");
    }
    lv_label_set_text(vbat_lbl, vbuf);

    if (current_screen_idx == 2) {
        char buf[64];

        if (link_rtt_ms >= 0) {
            snprintf(buf, sizeof(buf), "Q:%s V:%s %dms", link_quixi, link_voip, link_rtt_ms);
        } else {
            snprintf(buf, sizeof(buf), "Q:%s V:%s BT:%s", link_quixi, link_voip, link_bt);
        }
        lv_label_set_text(dbg_link_lbl, buf);

        imu_data_t imu;
        imu_qmi8658_get_latest(&imu);
        if (imu.ready) {
            snprintf(buf, sizeof(buf), "%.1f,%.1f,%.1f [%s]",
                     (double)imu.ax, (double)imu.ay, (double)imu.az,
                     imu_gesture_to_str(imu.last_gesture));
        } else {
            snprintf(buf, sizeof(buf), "IMU: offline");
        }
        lv_label_set_text(dbg_imu_lbl, buf);

        char mac[18] = {0};
        char name[20] = {0};
        int8_t rssi = 0;
        int cnt = ble_scanner_get_count();
        if (ble_scanner_get_strongest(mac, sizeof(mac), name, sizeof(name), &rssi)) {
            snprintf(buf, sizeof(buf), "BLE:%d %s %ddBm", cnt, name[0] ? name : mac, (int)rssi);
        } else {
            snprintf(buf, sizeof(buf), "BLE:%d scanning...", cnt);
        }
        lv_label_set_text(dbg_ble_lbl, buf);

        char gbuf[40];
        gpio_agent_get_status_str(gbuf, sizeof(gbuf));
        lv_label_set_text(dbg_gpio_lbl, gbuf);

        snprintf(buf, sizeof(buf), "%dmV | %ddeg | %llds",
                 vbat_mv, ui_screens_get_angle_deg(), k_uptime_get() / 1000);
        lv_label_set_text(dbg_sys_lbl, buf);
    }
}

int ui_screens_get_angle_deg(void)
{
    return orient_to_deg(current_orient);
}

void ui_set_emotion(face_emotion_t emo)
{
    g_emotion = emo;
}

void ui_set_emotion_str(const char *emo_str)
{
    if (!emo_str) return;
    if (strcasecmp(emo_str, "LISTENING") == 0) g_emotion = FACE_LISTENING;
    else if (strcasecmp(emo_str, "THINKING") == 0) g_emotion = FACE_THINKING;
    else if (strcasecmp(emo_str, "SPEAKING") == 0) g_emotion = FACE_SPEAKING;
    else if (strcasecmp(emo_str, "HAPPY") == 0) g_emotion = FACE_HAPPY;
    else if (strcasecmp(emo_str, "SAD") == 0) g_emotion = FACE_SAD;
    else if (strcasecmp(emo_str, "SURPRISED") == 0) g_emotion = FACE_SURPRISED;
    else if (strcasecmp(emo_str, "SLEEPING") == 0) g_emotion = FACE_SLEEPING;
    else if (strcasecmp(emo_str, "ALERT") == 0) g_emotion = FACE_ALERT;
    else g_emotion = FACE_IDLE;
}

void ui_set_subtitle(const char *text)
{
    if (!text) return;
    lv_label_set_text(sub_lbl, text);
}

void ui_set_mouth(int value)
{
    if (value < 0) value = 0;
    if (value > 100) value = 100;
    target_mouth = value;
    if (value > 0 && g_emotion != FACE_SPEAKING) {
        g_emotion = FACE_SPEAKING;
    }
}

void ui_set_link_status(const char *quixi, const char *voip, int rtt_ms, const char *bt_spk)
{
    if (quixi && quixi[0]) {
        strncpy(link_quixi, quixi, sizeof(link_quixi) - 1);
        link_quixi[sizeof(link_quixi) - 1] = '\0';
        lv_label_set_text(status_lbl, link_quixi);

        if (strcasecmp(link_quixi, "ONLINE") == 0 || strcasecmp(link_quixi, "OK") == 0) {
            lv_obj_set_style_bg_color(status_dot, lv_color_hex(0x22C55E), 0);
        } else if (strcasecmp(link_quixi, "OFFLINE") == 0) {
            lv_obj_set_style_bg_color(status_dot, lv_color_hex(0xEF4444), 0);
        } else {
            lv_obj_set_style_bg_color(status_dot, lv_color_hex(0xF59E0B), 0);
        }
    }
    if (voip && voip[0]) {
        strncpy(link_voip, voip, sizeof(link_voip) - 1);
        link_voip[sizeof(link_voip) - 1] = '\0';
    }
    if (bt_spk && bt_spk[0]) {
        strncpy(link_bt, bt_spk, sizeof(link_bt) - 1);
        link_bt[sizeof(link_bt) - 1] = '\0';
    }
    if (rtt_ms >= 0) {
        link_rtt_ms = rtt_ms;
    }
}

void ui_set_mic_active(bool active)
{
    mic_active = active;
    if (active) {
        g_emotion = FACE_LISTENING;
    }
}
