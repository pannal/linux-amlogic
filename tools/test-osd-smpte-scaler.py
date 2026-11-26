#!/usr/bin/env python3
"""Exercise the fbdev scaler register writer across native SMPTE and scaled modes."""
from pathlib import Path
import os
import runpy
import subprocess
import tempfile

TOOLS = Path(__file__).resolve().parent
OSD = TOOLS.parent / "drivers/amlogic/media/osd"
function = runpy.run_path(str(TOOLS / "test-osd-rdma-recovery.py"))["function"]

SUPPORT = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef uint64_t u64;
typedef uint32_t u32;
typedef int32_t s32;
typedef uint16_t u16;
typedef uint8_t u8;
enum { OSD1, OSD2 };
enum { VIU1, OSD_NORMAL, OSD_HIGH_ONE, SC_4K2K, SC_DOUBLE };
enum { __MESON_CPU_MAJOR_ID_G12B = 0x29 };
#define NEW_PPS_PHASE
#define do_div(n,d) ((n) /= (d))
#define VSYNCOSD_WR_MPEG_REG(field,value) ((field) = (value))
#define VSYNCOSD_WR_MPEG_REG_BITS(field,value,start,len) \
    ((field) = ((field) & ~(((1U << (len)) - 1) << (start))) | \
               (((value) & ((1U << (len)) - 1)) << (start)))
#define osd_setting_blending_scope(index) ((void)0)
#define vpp_blend_setting_default(index) ((void)0)
#define remove_from_update_list(index, item) ((void)0)
struct hw_osd_reg_s {
    u32 osd_sc_ctrl0, osd_hsc_ctrl0, osd_vsc_ctrl0;
    u32 osd_sci_wh_m1, osd_sco_h_start_end, osd_sco_v_start_end;
    u32 osd_hsc_phase_step, osd_hsc_init_phase, osd_vsc_phase_step, osd_vsc_init_phase;
};
static struct hw_osd_reg_s hw_osd_reg_array[2];
struct rect { int x_start, x_end, y_start, y_end; };
static struct {
    struct { int cpu_id, osd_ver; bool osd0_sc_independ; } osd_meson_dev;
    struct rect free_src_data[2], free_dst_data[2];
    struct { bool h_enable, v_enable; } free_scale[2];
    u32 free_scale_enable[2], free_scale_mode[2], sc_filter_workaround[2];
    u32 scale_workaround, workaround_line, vinfo_width[1], vinfo_height[1];
    bool hwc_enable[1], osd_display_fb[1], field_out_en[1];
} osd_hw;
static bool rev_b;
static inline bool is_meson_rev_b(void) { return rev_b; }
static u32 get_output_device_id(u32 index) { return VIU1; }
"""

CHECKS = r"""
static void setup(u32 i, int src_w, int src_h, int dst_w, int dst_h)
{
    osd_hw.free_src_data[i] = (struct rect){0, src_w - 1, 0, src_h - 1};
    osd_hw.free_dst_data[i] = (struct rect){0, dst_w - 1, 0, dst_h - 1};
    osd_hw.vinfo_width[VIU1] = dst_w;
    osd_hw.vinfo_height[VIU1] = dst_h;
    osd_hw.free_scale_enable[i] = 0x10001;
    osd_hw.free_scale_mode[i] = 1;
    osd_hw.free_scale[i].h_enable = osd_hw.free_scale[i].v_enable = true;
}
static void check_filters(u32 i, bool expected)
{
    osd_update_disp_freescale_enable(i);
    const struct hw_osd_reg_s *reg = &hw_osd_reg_array[i];
    assert(!!(reg->osd_hsc_ctrl0 & (1U << 22)) == expected);
    assert(!!(reg->osd_vsc_ctrl0 & (1U << 24)) == expected);
    assert(reg->osd_sc_ctrl0 == 0xc);
}
int main(void)
{
    osd_hw.osd_meson_dev.osd_ver = OSD_HIGH_ONE;
    osd_hw.osd_meson_dev.cpu_id = __MESON_CPU_MAJOR_ID_G12B;
    osd_hw.scale_workaround = SC_4K2K;
    osd_hw.hwc_enable[VIU1] = osd_hw.osd_display_fb[VIU1] = true;
    setup(OSD1, 4096, 2160, 4096, 2160);

    /* Use the ordinary control words as a reference for every other bit. */
    rev_b = false;
    check_filters(OSD1, true);
    struct hw_osd_reg_s expected = hw_osd_reg_array[OSD1];
    expected.osd_hsc_ctrl0 &= ~(1U << 22);
    expected.osd_vsc_ctrl0 &= ~(1U << 24);
    rev_b = true;
    check_filters(OSD1, false);
    assert(!memcmp(&expected, &hw_osd_reg_array[OSD1], sizeof(expected)));
    assert(expected.osd_sci_wh_m1 == (4095U << 16 | 2159));
    assert(expected.osd_sco_h_start_end == 4095 && expected.osd_sco_v_start_end == 2159);
    assert(expected.osd_hsc_phase_step == 1U << 24 && expected.osd_vsc_phase_step == 1U << 24);

    setup(OSD1, 1920, 1080, 4096, 2160);
    check_filters(OSD1, true);
    assert(hw_osd_reg_array[OSD1].osd_hsc_phase_step == ((u64)1920 << 24) / 4096);
    setup(OSD1, 3840, 2160, 4096, 2160);
    check_filters(OSD1, true);
    setup(OSD1, 4096, 2025, 4096, 2160);
    check_filters(OSD1, true);
    setup(OSD1, 3840, 2160, 3840, 2160);
    check_filters(OSD1, true);
    setup(OSD1, 4096, 2160, 4096, 2160);
    check_filters(OSD1, false);
    osd_hw.free_dst_data[OSD1].x_start = 8;
    check_filters(OSD1, true);
    assert(hw_osd_reg_array[OSD1].osd_sco_h_start_end == (8U << 16 | 4095));
    osd_hw.free_dst_data[OSD1].x_start = 0;
    osd_hw.free_dst_data[OSD1].y_start = 1;
    osd_hw.free_dst_data[OSD1].y_end++;
    check_filters(OSD1, true);
    setup(OSD1, 4096, 2160, 4096, 2160);
    osd_hw.field_out_en[VIU1] = true;
    check_filters(OSD1, true);
    osd_hw.field_out_en[VIU1] = false;
    setup(OSD2, 4096, 2160, 4096, 2160);
    check_filters(OSD2, true);
    osd_hw.osd_display_fb[VIU1] = false;
    check_filters(OSD1, true);
    osd_hw.osd_display_fb[VIU1] = true;
    osd_hw.osd_meson_dev.cpu_id++;
    check_filters(OSD1, true);
}
"""

def main():
    source = (OSD / "osd_hw.c").read_text()
    phase_types = source[source.index("#define OSD_ZOOM_BITS"):source.index("#define COEFF_NORM")]
    functions = [function(source, signature) for signature in (
        "static void f2v_get_vertical_phase(",
        "static u32 is_scaler_interlaced(",
        "static void osd_update_disp_freescale_enable(u32 index)\n{",
    )]
    with tempfile.TemporaryDirectory() as tmp:
        src, exe = Path(tmp) / "test.c", Path(tmp) / "test"
        src.write_text("\n".join([SUPPORT, phase_types, *functions, CHECKS]))
        subprocess.run([os.environ.get("CC", "cc"), "-std=c11", "-Wall", "-Werror",
                        str(src), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], cwd=tmp, check=True)
    print("OSD native SMPTE and scaled-mode register checks passed")

if __name__ == "__main__":
    main()
