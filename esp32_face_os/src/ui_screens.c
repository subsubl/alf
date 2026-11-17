#include "ui_screens.h"
#include "ble_scanner.h"
#include "gpio_agent.h"
#include "imu_qmi8658.h"

#include <lvgl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <zephyr/kernel.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static ui_callbacks_t g_cbs;

/* Screen objects */
static lv_obj_t *scr_face;
static lv_obj_t *scr_settings;
static lv_obj_t *scr_debug;
static int current_screen_idx;

/* Orbital widget registry for smooth 360-deg rotation around (120, 120) */
typedef struct {
    lv_obj_t *obj;
    int screen_idx;
    float base_x; /* Offset from (120, 120) at 0 deg */
    float base_y;
    lv_coord_t w;
    lv_coord_t h;
} orbital_widget_t;

#define MAX_ORBITAL_WIDGETS 28
static orbital_widget_t orbit_widgets[MAX_ORBITAL_WIDGETS];
static int orbit_count;

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

/* Rotation modes: 0 = Auto 360, 1 = Auto 180 flip, 2 = Locked 0 */
static int rot_mode = 0;
static float g_rot_rad = 0.0f;
static int16_t g_rot_tenths_deg = 0;
static int16_t last_widget_angle_tenths = -9999;

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
 * 360-Degree Geometry & Orbital Widget Transform Helpers
 * ============================================================================ */

static void register_orbital(lv_obj_t *obj, int scr_idx, float off_x, float off_y,
                             lv_coord_t w, lv_coord_t h)
{
    if (orbit_count >= MAX_ORBITAL_WIDGETS) {
        return;
    }
    lv_obj_set_size(obj, w, h);
    lv_obj_set_style_transform_pivot_x(obj, w / 2, 0);
    lv_obj_set_style_transform_pivot_y(obj, h / 2, 0);
    lv_obj_set_pos(obj, (lv_coord_t)lroundf(120.0f + off_x - w * 0.5f),
                   (lv_coord_t)lroundf(120.0f + off_y - h * 0.5f));

    orbit_widgets[orbit_count++] = (orbital_widget_t){
        .obj = obj,
        .screen_idx = scr_idx,
        .base_x = off_x,
        .base_y = off_y,
        .w = w,
        .h = h,
    };
}

static void apply_orbital_rotation(int scr_idx, float rad, int16_t angle_tenths)
{
    float c = cosf(rad);
    float s = sinf(rad);

    for (int i = 0; i < orbit_count; i++) {
        if (orbit_widgets[i].screen_idx != scr_idx) {
            continue;
        }
        orbital_widget_t *ow = &orbit_widgets[i];
        float cx = 120.0f + ow->base_x * c - ow->base_y * s;
        float cy = 120.0f + ow->base_x * s + ow->base_y * c;
        lv_obj_set_pos(ow->obj,
                       (lv_coord_t)lroundf(cx - ow->w * 0.5f),
                       (lv_coord_t)lroundf(cy - ow->h * 0.5f));
        lv_obj_set_style_transform_angle(ow->obj, angle_tenths, 0);
    }
}

static void rot_pt(float lx, float ly, lv_point_t *out)
{
    float c = cosf(g_rot_rad);
    float s = sinf(g_rot_rad);
    out->x = (lv_coord_t)lroundf(120.0f + lx * c - ly * s);
    out->y = (lv_coord_t)lroundf(120.0f + lx * s + ly * c);
}

static uint16_t wrap_deg_360(int deg)
{
    int m = deg % 360;
    if (m < 0) m += 360;
    return (uint16_t)m;
}

/* ============================================================================
 * Screen Navigation & Input Callbacks
 * ============================================================================ */

static void switch_to_screen(int idx)
{
    if (idx < 0) idx = 2;
    if (idx > 2) idx = 0;
    current_screen_idx = idx;

    apply_orbital_rotation(current_screen_idx, g_rot_rad, g_rot_tenths_deg);

    if (idx == 0) {
        lv_scr_load_anim(scr_face, LV_SCR_LOAD_ANIM_FADE_ON, 150, 0, false);
    } else if (idx == 1) {
        lv_scr_load_anim(scr_settings, LV_SCR_LOAD_ANIM_FADE_ON, 150, 0, false);
    } else {
        lv_scr_load_anim(scr_debug, LV_SCR_LOAD_ANIM_FADE_ON, 150, 0, false);
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
    /* Map physical left/right swipe in screen coordinates */
    if (dir == LV_DIR_LEFT || dir == LV_DIR_TOP) {
        switch_to_screen(current_screen_idx + 1);
    } else if (dir == LV_DIR_RIGHT || dir == LV_DIR_BOTTOM) {
        switch_to_screen(current_screen_idx - 1);
    }
}

static void add_nav_header(lv_obj_t *parent, int scr_idx, const char *title)
{
    lv_obj_t *btn_prev = lv_btn_create(parent);
    lv_obj_set_style_radius(btn_prev, 12, 0);
    lv_obj_set_style_bg_color(btn_prev, lv_color_hex(0x1E293B), 0);
    lv_obj_set_style_pad_all(btn_prev, 0, 0);
    lv_obj_add_event_cb(btn_prev, nav_prev_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lbl_p = lv_label_create(btn_prev);
    lv_label_set_text(lbl_p, "<");
    lv_obj_center(lbl_p);
    register_orbital(btn_prev, scr_idx, -54.0f, -92.0f, 28, 24);

    lv_obj_t *btn_next = lv_btn_create(parent);
    lv_obj_set_style_radius(btn_next, 12, 0);
    lv_obj_set_style_bg_color(btn_next, lv_color_hex(0x1E293B), 0);
    lv_obj_set_style_pad_all(btn_next, 0, 0);
    lv_obj_add_event_cb(btn_next, nav_next_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lbl_n = lv_label_create(btn_next);
    lv_label_set_text(lbl_n, ">");
    lv_obj_center(lbl_n);
    register_orbital(btn_next, scr_idx, 54.0f, -92.0f, 28, 24);

    if (title != NULL) {
        lv_obj_t *hdr = lv_label_create(parent);
        lv_label_set_text(hdr, title);
        lv_obj_set_style_text_color(hdr, lv_color_hex(0x38BDF8), 0);
        lv_obj_set_style_text_font(hdr, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_align(hdr, LV_TEXT_ALIGN_CENTER, 0);
        register_orbital(hdr, scr_idx, 0.0f, -92.0f, 76, 18);
    }

    lv_obj_add_event_cb(parent, screen_gesture_cb, LV_EVENT_GESTURE, NULL);
    lv_obj_clear_flag(parent, LV_OBJ_FLAG_GESTURE_BUBBLE);
}

static void face_tap_cb(lv_event_t *e)
{
    LV_UNUSED(e);
    pet_blush_frames = 60; /* ~2s happy blush reaction on tap */
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
        lv_label_set_text(cfg_rot_lbl, "Rot: Auto 360");
    } else if (rot_mode == 1) {
        lv_label_set_text(cfg_rot_lbl, "Rot: Flip 180");
    } else {
        lv_label_set_text(cfg_rot_lbl, "Rot: Lock 0");
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
 * Procedural 360-Degree Vector Companion Face Renderer
 * ============================================================================ */

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

static void draw_rot_line(lv_draw_ctx_t *draw_ctx, float x1, float y1, float x2, float y2,
                          lv_coord_t width, lv_color_t color, lv_opa_t opa)
{
    lv_point_t p1, p2;
    rot_pt(x1, y1, &p1);
    rot_pt(x2, y2, &p2);

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
                            int rot_deg, lv_color_t iris_col, bool blink_now)
{
    /* Happy / Sleeping crescent eyes */
    if (g_emotion == FACE_HAPPY) {
        lv_point_t ec;
        rot_pt(ex, ey + 6.0f, &ec);
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.color = iris_col;
        arc.width = 6;
        arc.rounded = 1;
        uint16_t a0 = wrap_deg_360(205 + rot_deg);
        uint16_t a1 = wrap_deg_360(335 + rot_deg);
        lv_draw_arc(draw_ctx, &arc, &ec, 17, a0, a1);
        return;
    }

    if (g_emotion == FACE_SLEEPING || blink_now) {
        lv_point_t ec;
        rot_pt(ex, ey - 2.0f, &ec);
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.color = (g_emotion == FACE_SLEEPING) ? lv_color_hex(0x64748B) : iris_col;
        arc.width = 4;
        arc.rounded = 1;
        uint16_t a0 = wrap_deg_360(25 + rot_deg);
        uint16_t a1 = wrap_deg_360(155 + rot_deg);
        lv_draw_arc(draw_ctx, &arc, &ec, 16, a0, a1);
        return;
    }

    int eye_r = 19;
    if (g_emotion == FACE_LISTENING) eye_r = 21;
    else if (g_emotion == FACE_SURPRISED) eye_r = 22;
    else if (g_emotion == FACE_SAD) eye_r = 16;

    lv_point_t eye_c;
    rot_pt(ex, ey, &eye_c);

    /* Outer glow halo */
    draw_filled_circle(draw_ctx, &eye_c, eye_r + 5, iris_col, LV_OPA_20);
    /* Main eye body */
    draw_filled_circle(draw_ctx, &eye_c, eye_r, iris_col, LV_OPA_COVER);

    /* Inner deep pupil shifted by gaze */
    lv_point_t pupil_c;
    rot_pt(ex + gaze_x, ey + gaze_y, &pupil_c);
    draw_filled_circle(draw_ctx, &pupil_c, eye_r - 7, lv_color_hex(0x0B1220), LV_OPA_90);

    /* Dual 3D specular catchlights (rotate cleanly in eye-local space) */
    lv_point_t glint1, glint2;
    rot_pt(ex + gaze_x - 6.0f, ey + gaze_y - 7.0f, &glint1);
    rot_pt(ex + gaze_x + 5.0f, ey + gaze_y + 5.0f, &glint2);
    draw_filled_circle(draw_ctx, &glint1, 5, lv_color_white(), LV_OPA_COVER);
    draw_filled_circle(draw_ctx, &glint2, 2, lv_color_white(), LV_OPA_90);

    /* Sad / Alert upper eyelid slant mask */
    if (g_emotion == FACE_SAD) {
        float dir = (ex < 0) ? -1.0f : 1.0f;
        draw_rot_line(draw_ctx, ex - 18.0f * dir, ey - 10.0f, ex + 18.0f * dir, ey - 18.0f,
                      8, lv_color_hex(0x0B0F17), LV_OPA_COVER);
    } else if (g_emotion == FACE_ALERT) {
        float dir = (ex < 0) ? 1.0f : -1.0f;
        draw_rot_line(draw_ctx, ex - 18.0f * dir, ey - 16.0f, ex + 18.0f * dir, ey - 9.0f,
                      8, lv_color_hex(0x0B0F17), LV_OPA_COVER);
    }
}

static void draw_eyebrows(lv_draw_ctx_t *draw_ctx, lv_color_t brow_col)
{
    if (g_emotion == FACE_SLEEPING) {
        return;
    }

    float ly_in = -38.0f;
    float ly_out = -36.0f;
    float ry_in = -38.0f;
    float ry_out = -36.0f;

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
        /* Curious asymmetric brow */
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

    draw_rot_line(draw_ctx, -50.0f, ly_out, -24.0f, ly_in, 4, brow_col, LV_OPA_90);
    draw_rot_line(draw_ctx, 24.0f, ry_in, 50.0f, ry_out, 4, brow_col, LV_OPA_90);
}

static void draw_mouth(lv_draw_ctx_t *draw_ctx, int rot_deg, lv_color_t accent_col)
{
    if (g_emotion == FACE_SPEAKING || current_mouth > 6) {
        /* Multi-layered dynamic viseme mouth */
        int open_r = 9 + (current_mouth * 11) / 100;
        lv_point_t mc;
        rot_pt(0.0f, 24.0f, &mc);

        /* Outer lip glow + dark mouth cavity */
        draw_filled_circle(draw_ctx, &mc, open_r + 3, accent_col, LV_OPA_COVER);
        draw_filled_circle(draw_ctx, &mc, open_r, lv_color_hex(0x0F172A), LV_OPA_COVER);

        /* Warm pink tongue highlight inside lower cavity */
        if (open_r >= 10) {
            lv_point_t tc;
            rot_pt(0.0f, 24.0f + open_r * 0.42f, &tc);
            draw_filled_circle(draw_ctx, &tc, open_r / 2, lv_color_hex(0xFB7185), LV_OPA_90);
        }
        return;
    }

    if (g_emotion == FACE_SURPRISED) {
        lv_point_t mc;
        rot_pt(0.0f, 25.0f, &mc);
        draw_filled_circle(draw_ctx, &mc, 12, accent_col, LV_OPA_COVER);
        draw_filled_circle(draw_ctx, &mc, 8, lv_color_hex(0x0F172A), LV_OPA_COVER);
        return;
    }

    if (g_emotion == FACE_SAD) {
        lv_point_t mc;
        rot_pt(0.0f, 38.0f, &mc);
        lv_draw_arc_dsc_t arc;
        lv_draw_arc_dsc_init(&arc);
        arc.color = accent_col;
        arc.width = 4;
        arc.rounded = 1;
        lv_draw_arc(draw_ctx, &arc, &mc, 18, wrap_deg_360(215 + rot_deg), wrap_deg_360(325 + rot_deg));
        return;
    }

    if (g_emotion == FACE_THINKING) {
        draw_rot_line(draw_ctx, -12.0f, 25.0f, 12.0f, 22.0f, 4, accent_col, LV_OPA_COVER);
        return;
    }

    if (g_emotion == FACE_SLEEPING) {
        lv_point_t mc;
        rot_pt(0.0f, 24.0f, &mc);
        draw_filled_circle(draw_ctx, &mc, 5, lv_color_hex(0x64748B), LV_OPA_80);
        return;
    }

    /* Warm smile arc for IDLE / HAPPY / LISTENING */
    lv_point_t mc;
    rot_pt(0.0f, 12.0f, &mc);
    lv_draw_arc_dsc_t arc;
    lv_draw_arc_dsc_init(&arc);
    arc.color = accent_col;
    arc.width = (g_emotion == FACE_HAPPY) ? 5 : 4;
    arc.rounded = 1;
    uint16_t span = (g_emotion == FACE_HAPPY) ? 110 : 76;
    lv_draw_arc(draw_ctx, &arc, &mc, 20,
                wrap_deg_360(90 - span / 2 + rot_deg),
                wrap_deg_360(90 + span / 2 + rot_deg));
}

static void draw_extra_fx(lv_draw_ctx_t *draw_ctx, int rot_deg, lv_color_t accent_col)
{
    /* Animated orbiting dots when THINKING */
    if (g_emotion == FACE_THINKING) {
        for (int i = 0; i < 3; i++) {
            float phase = (anim_frame * 0.14f) + i * 0.8f;
            float dx = -16.0f + i * 16.0f;
            float dy = -56.0f + sinf(phase) * 4.0f;
            lv_point_t dp;
            rot_pt(dx, dy, &dp);
            draw_filled_circle(draw_ctx, &dp, 3, accent_col, LV_OPA_90);
        }
    }

    /* Animated floating 'Z' glyphs when SLEEPING */
    if (g_emotion == FACE_SLEEPING) {
        float drift = (float)(anim_frame % 45) * 0.45f;
        float zx = 46.0f;
        float zy = -32.0f - drift;
        float sz = 6.0f;
        lv_opa_t opa = (anim_frame % 45 > 35) ? LV_OPA_40 : LV_OPA_90;
        draw_rot_line(draw_ctx, zx - sz, zy - sz, zx + sz, zy - sz, 2, lv_color_hex(0x94A3B8), opa);
        draw_rot_line(draw_ctx, zx + sz, zy - sz, zx - sz, zy + sz, 2, lv_color_hex(0x94A3B8), opa);
        draw_rot_line(draw_ctx, zx - sz, zy + sz, zx + sz, zy + sz, 2, lv_color_hex(0x94A3B8), opa);
    }

    /* Subtle horizon compass arc along top bezel */
    lv_point_t center = { .x = 120, .y = 120 };
    lv_draw_arc_dsc_t ring_dsc;
    lv_draw_arc_dsc_init(&ring_dsc);
    ring_dsc.color = lv_color_hex(0x1E293B);
    ring_dsc.width = 2;
    lv_draw_arc(draw_ctx, &ring_dsc, &center, 115, 0, 359);

    ring_dsc.color = accent_col;
    ring_dsc.width = 3;
    ring_dsc.rounded = 1;
    ring_dsc.opa = LV_OPA_70;
    lv_draw_arc(draw_ctx, &ring_dsc, &center, 115,
                wrap_deg_360(252 + rot_deg), wrap_deg_360(288 + rot_deg));
}

static void face_draw_event_cb(lv_event_t *e)
{
    lv_draw_ctx_t *draw_ctx = lv_event_get_draw_ctx(e);
    if (!draw_ctx) return;

    int rot_deg = (int)lroundf(g_rot_rad * (180.0f / (float)M_PI));
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

    draw_extra_fx(draw_ctx, rot_deg, iris_col);

    /* Blush cheeks when happy, speaking, or petted */
    if (g_emotion == FACE_HAPPY || g_emotion == FACE_SPEAKING || pet_blush_frames > 0) {
        lv_point_t b_l, b_r;
        rot_pt(-54.0f, 14.0f, &b_l);
        rot_pt(54.0f, 14.0f, &b_r);
        draw_filled_circle(draw_ctx, &b_l, 9, lv_color_hex(0xFB7185), LV_OPA_40);
        draw_filled_circle(draw_ctx, &b_r, 9, lv_color_hex(0xFB7185), LV_OPA_40);
    }

    draw_single_eye(draw_ctx, -38.0f, -14.0f, rot_deg, iris_col, blink_now);
    draw_single_eye(draw_ctx, 38.0f, -14.0f, rot_deg, iris_col, blink_now);
    draw_eyebrows(draw_ctx, iris_col);
    draw_mouth(draw_ctx, rot_deg, iris_col);
}

/* ============================================================================
 * Screen Builders
 * ============================================================================ */

static void build_face_screen(void)
{
    scr_face = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_face, lv_color_hex(0x0B0F17), 0);

    /* Fullscreen procedural vector canvas */
    face_canvas = lv_obj_create(scr_face);
    lv_obj_set_size(face_canvas, 240, 240);
    lv_obj_set_pos(face_canvas, 0, 0);
    lv_obj_set_style_bg_opa(face_canvas, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(face_canvas, 0, 0);
    lv_obj_set_style_pad_all(face_canvas, 0, 0);
    lv_obj_add_event_cb(face_canvas, face_draw_event_cb, LV_EVENT_DRAW_MAIN, NULL);
    lv_obj_add_event_cb(face_canvas, face_tap_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_add_event_cb(face_canvas, screen_gesture_cb, LV_EVENT_GESTURE, NULL);

    add_nav_header(scr_face, 0, NULL);

    /* Top status dot + text (orbital around center) */
    status_dot = lv_obj_create(scr_face);
    lv_obj_set_style_radius(status_dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_border_width(status_dot, 0, 0);
    lv_obj_set_style_bg_color(status_dot, lv_color_hex(0xF59E0B), 0);
    register_orbital(status_dot, 0, -24.0f, -94.0f, 8, 8);

    status_lbl = lv_label_create(scr_face);
    lv_label_set_text(status_lbl, "WAIT");
    lv_obj_set_style_text_color(status_lbl, lv_color_hex(0x94A3B8), 0);
    lv_obj_set_style_text_font(status_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(status_lbl, LV_TEXT_ALIGN_CENTER, 0);
    register_orbital(status_lbl, 0, 8.0f, -94.0f, 48, 16);

    vbat_lbl = lv_label_create(scr_face);
    lv_label_set_text(vbat_lbl, "USB");
    lv_obj_set_style_text_color(vbat_lbl, lv_color_hex(0x64748B), 0);
    lv_obj_set_style_text_font(vbat_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(vbat_lbl, LV_TEXT_ALIGN_CENTER, 0);
    register_orbital(vbat_lbl, 0, 0.0f, -76.0f, 48, 16);

    /* Real-time Synced Subtitle Overlay (orbital at bottom) */
    sub_lbl = lv_label_create(scr_face);
    lv_label_set_long_mode(sub_lbl, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(sub_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(sub_lbl, lv_color_hex(0xE2E8F0), 0);
    lv_obj_set_style_text_font(sub_lbl, &lv_font_montserrat_12, 0);
    lv_label_set_text(sub_lbl, "Tap face to speak");
    register_orbital(sub_lbl, 0, 0.0f, 68.0f, 164, 34);
}

static void build_settings_screen(void)
{
    scr_settings = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_settings, lv_color_hex(0x0F172A), 0);
    add_nav_header(scr_settings, 1, "SETTINGS");

    /* Brightness cycle button */
    lv_obj_t *btn_bright = lv_btn_create(scr_settings);
    lv_obj_set_style_radius(btn_bright, 10, 0);
    lv_obj_set_style_bg_color(btn_bright, lv_color_hex(0x1E293B), 0);
    lv_obj_add_event_cb(btn_bright, bright_cycle_cb, LV_EVENT_CLICKED, NULL);
    cfg_bright_lbl = lv_label_create(btn_bright);
    lv_label_set_text(cfg_bright_lbl, "Bright: 100%");
    lv_obj_set_style_text_font(cfg_bright_lbl, &lv_font_montserrat_12, 0);
    lv_obj_center(cfg_bright_lbl);
    register_orbital(btn_bright, 1, 0.0f, -50.0f, 136, 26);

    /* Volume row */
    lv_obj_t *btn_vdn = lv_btn_create(scr_settings);
    lv_obj_set_style_bg_color(btn_vdn, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(btn_vdn, vol_down_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l_dn = lv_label_create(btn_vdn);
    lv_label_set_text(l_dn, "-");
    lv_obj_center(l_dn);
    register_orbital(btn_vdn, 1, -52.0f, -16.0f, 34, 26);

    cfg_vol_lbl = lv_label_create(scr_settings);
    lv_label_set_text(cfg_vol_lbl, "Vol:75%");
    lv_obj_set_style_text_color(cfg_vol_lbl, lv_color_hex(0xF8FAFC), 0);
    lv_obj_set_style_text_font(cfg_vol_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(cfg_vol_lbl, LV_TEXT_ALIGN_CENTER, 0);
    register_orbital(cfg_vol_lbl, 1, 0.0f, -16.0f, 62, 18);

    lv_obj_t *btn_vup = lv_btn_create(scr_settings);
    lv_obj_set_style_bg_color(btn_vup, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(btn_vup, vol_up_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l_up = lv_label_create(btn_vup);
    lv_label_set_text(l_up, "+");
    lv_obj_center(l_up);
    register_orbital(btn_vup, 1, 52.0f, -16.0f, 34, 26);

    /* Mic Mode button */
    lv_obj_t *btn_mic = lv_btn_create(scr_settings);
    lv_obj_set_style_radius(btn_mic, 10, 0);
    lv_obj_set_style_bg_color(btn_mic, lv_color_hex(0x0284C7), 0);
    lv_obj_add_event_cb(btn_mic, mic_mode_cb, LV_EVENT_CLICKED, NULL);
    cfg_mic_lbl = lv_label_create(btn_mic);
    lv_label_set_text(cfg_mic_lbl, "Mic: Auto VAD");
    lv_obj_set_style_text_font(cfg_mic_lbl, &lv_font_montserrat_12, 0);
    lv_obj_center(cfg_mic_lbl);
    register_orbital(btn_mic, 1, 0.0f, 18.0f, 136, 26);

    /* Rotation Mode button */
    lv_obj_t *btn_rot = lv_btn_create(scr_settings);
    lv_obj_set_style_radius(btn_rot, 10, 0);
    lv_obj_set_style_bg_color(btn_rot, lv_color_hex(0x4F46E5), 0);
    lv_obj_add_event_cb(btn_rot, rot_mode_cb, LV_EVENT_CLICKED, NULL);
    cfg_rot_lbl = lv_label_create(btn_rot);
    lv_label_set_text(cfg_rot_lbl, "Rot: Auto 360");
    lv_obj_set_style_text_font(cfg_rot_lbl, &lv_font_montserrat_12, 0);
    lv_obj_center(cfg_rot_lbl);
    register_orbital(btn_rot, 1, 0.0f, 50.0f, 136, 26);

    /* BLE Radar switch */
    lv_obj_t *ble_lbl = lv_label_create(scr_settings);
    lv_label_set_text(ble_lbl, "BLE");
    lv_obj_set_style_text_color(ble_lbl, lv_color_hex(0xCBD5E1), 0);
    lv_obj_set_style_text_font(ble_lbl, &lv_font_montserrat_12, 0);
    register_orbital(ble_lbl, 1, -28.0f, 82.0f, 34, 18);

    cfg_ble_sw = lv_switch_create(scr_settings);
    lv_obj_add_state(cfg_ble_sw, LV_STATE_CHECKED);
    lv_obj_add_event_cb(cfg_ble_sw, ble_switch_cb, LV_EVENT_VALUE_CHANGED, NULL);
    register_orbital(cfg_ble_sw, 1, 22.0f, 82.0f, 44, 22);
}

static void build_debug_screen(void)
{
    scr_debug = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_debug, lv_color_hex(0x090D16), 0);
    add_nav_header(scr_debug, 2, "DEBUG");

    dbg_link_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_link_lbl, lv_color_hex(0x34D399), 0);
    lv_obj_set_style_text_font(dbg_link_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(dbg_link_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(dbg_link_lbl, "Q:WAIT V:IDLE");
    register_orbital(dbg_link_lbl, 2, 0.0f, -56.0f, 168, 18);

    dbg_imu_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_imu_lbl, lv_color_hex(0xE2E8F0), 0);
    lv_obj_set_style_text_font(dbg_imu_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(dbg_imu_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(dbg_imu_lbl, "IMU: waiting...");
    register_orbital(dbg_imu_lbl, 2, 0.0f, -32.0f, 184, 18);

    dbg_ble_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_ble_lbl, lv_color_hex(0x93C5FD), 0);
    lv_obj_set_style_text_font(dbg_ble_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(dbg_ble_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(dbg_ble_lbl, "BLE: 0 devs");
    register_orbital(dbg_ble_lbl, 2, 0.0f, -8.0f, 184, 18);

    dbg_gpio_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_gpio_lbl, lv_color_hex(0xFBBF24), 0);
    lv_obj_set_style_text_font(dbg_gpio_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(dbg_gpio_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(dbg_gpio_lbl, "GPIO: idle");
    register_orbital(dbg_gpio_lbl, 2, 0.0f, 16.0f, 168, 18);

    dbg_sys_lbl = lv_label_create(scr_debug);
    lv_obj_set_style_text_color(dbg_sys_lbl, lv_color_hex(0x94A3B8), 0);
    lv_obj_set_style_text_font(dbg_sys_lbl, &lv_font_montserrat_12, 0);
    lv_obj_set_style_text_align(dbg_sys_lbl, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text(dbg_sys_lbl, "Rot:0 deg");
    register_orbital(dbg_sys_lbl, 2, 0.0f, 40.0f, 168, 18);

    lv_obj_t *btn_ping = lv_btn_create(scr_debug);
    lv_obj_set_style_bg_color(btn_ping, lv_color_hex(0x2563EB), 0);
    lv_obj_add_event_cb(btn_ping, dbg_ping_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lp = lv_label_create(btn_ping);
    lv_label_set_text(lp, "PING");
    lv_obj_set_style_text_font(lp, &lv_font_montserrat_12, 0);
    lv_obj_center(lp);
    register_orbital(btn_ping, 2, -36.0f, 74.0f, 62, 26);

    lv_obj_t *btn_ble = lv_btn_create(scr_debug);
    lv_obj_set_style_bg_color(btn_ble, lv_color_hex(0x0D9488), 0);
    lv_obj_add_event_cb(btn_ble, dbg_ble_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lb = lv_label_create(btn_ble);
    lv_label_set_text(lb, "BLE");
    lv_obj_set_style_text_font(lb, &lv_font_montserrat_12, 0);
    lv_obj_center(lb);
    register_orbital(btn_ble, 2, 36.0f, 74.0f, 62, 26);
}

/* ============================================================================
 * Public API
 * ============================================================================ */

void ui_screens_init(const ui_callbacks_t *cbs)
{
    if (cbs) {
        g_cbs = *cbs;
    }
    orbit_count = 0;

    build_face_screen();
    build_settings_screen();
    build_debug_screen();

    current_screen_idx = 0;
    apply_orbital_rotation(0, 0.0f, 0);
    lv_scr_load(scr_face);
}

static void update_smooth_rotation(void)
{
    float target_rad = g_rot_rad;

    if (rot_mode == 2) {
        target_rad = 0.0f;
    } else {
        imu_data_t imu;
        imu_qmi8658_get_latest(&imu);
        if (imu.ready) {
            float in_plane = sqrtf(imu.ax * imu.ax + imu.ay * imu.ay);
            if (in_plane > 2.0f) {
                float raw_rad = atan2f(imu.ay, -imu.ax);
                if (rot_mode == 1) {
                    /* Snap to 0 or 180 deg */
                    target_rad = (fabsf(raw_rad) > (float)(M_PI * 0.5)) ? (float)M_PI : 0.0f;
                } else {
                    target_rad = raw_rad;
                }
            }
        }
    }

    float diff = target_rad - g_rot_rad;
    while (diff > (float)M_PI)  diff -= (float)(2.0 * M_PI);
    while (diff < -(float)M_PI) diff += (float)(2.0 * M_PI);

    if (fabsf(diff) > 0.01f) {
        g_rot_rad += diff * 0.22f;
        while (g_rot_rad > (float)M_PI)  g_rot_rad -= (float)(2.0 * M_PI);
        while (g_rot_rad < -(float)M_PI) g_rot_rad += (float)(2.0 * M_PI);
    }

    int tenths = (int)lroundf(g_rot_rad * (1800.0f / (float)M_PI));
    tenths = ((tenths % 3600) + 3600) % 3600;
    g_rot_tenths_deg = (int16_t)tenths;

    if (abs(g_rot_tenths_deg - last_widget_angle_tenths) >= 15) {
        last_widget_angle_tenths = g_rot_tenths_deg;
        apply_orbital_rotation(current_screen_idx, g_rot_rad, g_rot_tenths_deg);
    }
}

void ui_screens_tick_anim(void)
{
    anim_frame++;
    if (pet_blush_frames > 0) {
        pet_blush_frames--;
    }

    /* Update continuous 360-degree gravity rotation */
    update_smooth_rotation();

    /* Smooth mouth viseme interpolation */
    if (g_emotion == FACE_SPEAKING) {
        current_mouth += (target_mouth - current_mouth) / 2;
        if (current_mouth == 0 && target_mouth > 0) {
            current_mouth = target_mouth;
        }
    } else {
        target_mouth = 0;
        current_mouth = 0;
    }

    /* Subtle organic idle gaze wander */
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

    /* Trigger procedural redraw on Face screen */
    if (current_screen_idx == 0 && face_canvas != NULL) {
        lv_obj_invalidate(face_canvas);
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
    return (int)(g_rot_tenths_deg / 10);
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
