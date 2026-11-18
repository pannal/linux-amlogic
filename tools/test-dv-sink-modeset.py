#!/usr/bin/env python3
"""Exercise the Dolby Vision driver's sink reads during a transient modeset."""
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / 'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c'
VINFO = ROOT / 'include/linux/amlogic/media/vout/vinfo.h'


def between(source, start, end):
    first = source.index(start)
    return source[first:source.index(end, first)]


def program(driver, vinfo):
    loader_start = driver.index('static inline void set_dolby_vsvdb(')
    loader_end = driver.index('\n}', driver.index('xbmc_dv_vsvdb_inject_num += 1;', loader_start)) + 2
    parts = [r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define READ_ONCE(x) (x)
#define min_t(type, a, b) ((type)(a) < (type)(b) ? (type)(a) : (type)(b))
#define simple_strtoul strtoul
#define dump_buffer(...) ((void)0)
#define pr_info(...) ((void)0)
""", between(vinfo, 'enum block_type {', 'struct vout_device_s {'), r"""
struct vout_device_s { const struct dv_info *dv_info; };
struct vinfo_s {
    struct vout_device_s *vout_device;
    struct { unsigned int lumi_min, lumi_max; } hdr_info;
};
struct vframe_s { unsigned int width, height, duration; };
enum signal_format_enum { FORMAT_DOVI, FORMAT_HDR10, FORMAT_SDR, FORMAT_DOVI_LL };
enum { DV_SUPPORT_SHF = 4, DV_SUPPORT = 1 << DV_SUPPORT_SHF, HDR_SUPPORT = 1 };
enum { FLAG_DISABLE_DOVI_OUT = 1, FLAG_USE_SINK_MIN_MAX = 2 };
static unsigned int capabilities, dolby_vision_flags = FLAG_USE_SINK_MIN_MAX;
static unsigned int sink_hdr_support(const struct vinfo_s *vinfo) { return capabilities; }
static unsigned int dolby_vision_target_max[3][3], dolby_vision_default_max[3][3];
static unsigned int dolby_vision_target_min, graphic_max, graphic_min;
static struct { u8 vsvdb_tbl[32]; u32 vsvdb_len, vsvdb_changed; } new_dovi_setting;
static unsigned int xbmc_dv_type, xbmc_dv_vp, xbmc_dv_vsvdb_inject_num;
static unsigned int xbmc_dv_vsvdb_source_lum_limit_num, xbmc_dv_hdr10_for_dv_ll_inject_num;
static unsigned int dolby_vision_dolby_vsvdb_source_lum_limit, xbmc_dv_md_source_max_pq;
static bool xbmc_dv_vsvdb_inject, xbmc_dv_hdr10_for_dv_ll, use_new, ll;
static char *xbmc_dv_vsvdb_payload = "27020304050607";
static u8 md_buf[1][68];
static int current_id;
static bool is_dv_ll(void) { return ll; }
static void set_hdr10_data_for_dv_ll(void) { xbmc_dv_hdr10_for_dv_ll_inject_num++; }
"""]
    parts.extend([
        between(driver, 'static const struct dv_info *sink_dv_info(', 'static int sink_support_hdr('),
        between(driver, 'static const u16 min_direct_to_pq_lut', 'static const u16 xbmc_max_direct_to_nits_lut['),
        between(driver, 'static const u16 xbmc_max_direct_to_pq_lut_2', 'static const u16 xbmc_max_direct_to_nits_lut_2_adj'),
        between(driver, 'static inline u8 xbmc_find_closest_lut_index', 'static inline void set_dolby_vsvdb_source_cs'),
        between(driver, 'static inline void extract_dolby_vsvdb_source_lum', 'static inline void set_hdr10_data_for_dv_ll'),
        driver[loader_start:loader_end],
        'static void frame(struct vinfo_s *vinfo, enum signal_format_enum src_format) {',
        between(driver, '\tconst struct dv_info *dv_info;\n\tbool vsvdb_ready', '\tstruct vframe_s *el_vf;'),
        'u32 target_lumin_max = 0;',
        between(driver, '\tdv_info = sink_dv_info(vinfo);\n\tif (dolby_vision_flags', '\tif (is_osd_off)'),
        between(driver, '\tif (xbmc_dv_vsvdb_inject_num < 24)', '\t/* check video/graphics priority'),
        '}', CASES])
    return '\n'.join(parts)


CASES = r"""
int main(void)
{
    struct dv_info sink = {.ieeeoui = 0x00d046, .block_flag = CORRECT, .ver = 1,
        .length = 11, .tmaxLUM = 8, .tminLUM = 127,
        .rawdata = {0xeb, 1, 0x46, 0xd0, 0, 0x27, 0xfe, 0x02, 9, 8, 7, 6}};
    const u8 injected[] = {0xeb, 1, 0x46, 0xd0, 0, 0x27, 2, 3, 4, 5, 6, 7};
    struct vout_device_s device = {&sink}, empty_device = {0};
    struct vinfo_s hdmi = {&device}, null_mode = {0}, no_descriptor = {&empty_device};
    struct vframe_s uhd = {3840, 2160, 1600};
    struct vinfo_s *missing[] = {NULL, &null_mode, &no_descriptor};

    /* Display-led uses a capable sink's block even with the injection toggle. */
    xbmc_dv_vsvdb_inject = true;
    frame(&hdmi, FORMAT_DOVI);
    assert(new_dovi_setting.vsvdb_len == 12 && !memcmp(new_dovi_setting.vsvdb_tbl, sink.rawdata, 12));
    assert(graphic_max == 500 && graphic_min == 10000 && dolby_vision_target_max[2][1] == 500);

    /* Leave one load and clamp for the replacement sink. */
    xbmc_dv_type = 1;
    xbmc_dv_vsvdb_inject = false;
    xbmc_dv_vsvdb_inject_num = xbmc_dv_vsvdb_source_lum_limit_num = 23;
    xbmc_dv_hdr10_for_dv_ll = ll = true;
    new_dovi_setting.vsvdb_changed = 0;
    capabilities = DV_SUPPORT | HDR_SUPPORT;
    for (unsigned int i = 0; i < 30; ++i) {
        struct vinfo_s *v = missing[i % 3];
        frame(v, FORMAT_DOVI);
        assert(sink_support_dolby_vision(v, &uhd));
        assert(!memcmp(new_dovi_setting.vsvdb_tbl, sink.rawdata, 12));
        assert(!new_dovi_setting.vsvdb_changed && new_dovi_setting.vsvdb_len == 12);
        assert(xbmc_dv_vsvdb_inject_num == 23 && xbmc_dv_vsvdb_source_lum_limit_num == 23);
        assert(!xbmc_dv_hdr10_for_dv_ll_inject_num && dolby_vision_target_max[2][1] == 500);
    }
    assert(!sink_support_dolby_vision(&hdmi, &uhd));
    sink.sup_2160p60hz = 1;
    assert(sink_support_dolby_vision(&hdmi, &uhd));
    sink.rawdata[11] = 0x77;
    md_buf[0][66] = 0x0d;
    md_buf[0][67] = 0x3c; /* 3388 PQ, selects the driver's 3377 entry. */
    frame(&hdmi, FORMAT_DOVI);
    assert(new_dovi_setting.vsvdb_changed && new_dovi_setting.vsvdb_tbl[11] == 0x77);
    assert(new_dovi_setting.vsvdb_tbl[6] == 74 && new_dovi_setting.vsvdb_tbl[7] == 0);
    assert(xbmc_dv_vsvdb_inject_num == 24 && xbmc_dv_vsvdb_source_lum_limit_num == 24);
    assert(xbmc_dv_hdr10_for_dv_ll_inject_num == 1);

    ll = false;
    xbmc_dv_vsvdb_inject_num = 0;
    xbmc_dv_type = 0;
    xbmc_dv_vsvdb_inject = true;
    frame(&null_mode, FORMAT_SDR);
    assert(!xbmc_dv_vsvdb_inject_num && new_dovi_setting.vsvdb_tbl[11] == 0x77);
    sink.ieeeoui = 0;
    hdmi.hdr_info.lumi_max = no_descriptor.hdr_info.lumi_max = 64;
    frame(&hdmi, FORMAT_SDR);
    assert(graphic_max == 200 && dolby_vision_target_max[2][1] == 200);
    assert(xbmc_dv_vsvdb_inject_num == 1 && !memcmp(new_dovi_setting.vsvdb_tbl, injected, 12));

    xbmc_dv_type = 1;
    xbmc_dv_vsvdb_payload = "270203040506aa";
    no_descriptor.hdr_info.lumi_max = 96;
    frame(&no_descriptor, FORMAT_SDR);
    assert(graphic_max == 400 && dolby_vision_target_max[2][1] == 400);
    assert(xbmc_dv_vsvdb_inject_num == 2 && new_dovi_setting.vsvdb_tbl[11] == 0xaa);
    xbmc_dv_type = 0;
    xbmc_dv_vsvdb_inject = false;
    xbmc_dv_vp = 1;
    xbmc_dv_vsvdb_payload = "270203040506bb";
    frame(NULL, FORMAT_SDR);
    assert(xbmc_dv_vsvdb_inject_num == 3 && new_dovi_setting.vsvdb_tbl[11] == 0xbb);

    /* A rejected override leaves a usable native block eligible for clamping. */
    xbmc_dv_type = 1;
    xbmc_dv_vp = 0;
    xbmc_dv_vsvdb_inject = ll = true;
    xbmc_dv_vsvdb_payload = "";
    xbmc_dv_vsvdb_source_lum_limit_num = 23;
    frame(&no_descriptor, FORMAT_DOVI);
    assert(xbmc_dv_vsvdb_inject_num == 3 && xbmc_dv_vsvdb_source_lum_limit_num == 23);
    assert(new_dovi_setting.vsvdb_tbl[11] == 0xbb);
    sink.ieeeoui = 0x00d046;
    frame(&hdmi, FORMAT_DOVI);
    assert(xbmc_dv_vsvdb_inject_num == 3 && xbmc_dv_vsvdb_source_lum_limit_num == 24);
    assert(new_dovi_setting.vsvdb_tbl[11] == 0x77 && new_dovi_setting.vsvdb_tbl[6] == 74);
    xbmc_dv_vsvdb_payload = "27020304050607";
    frame(&hdmi, FORMAT_SDR);
    assert(xbmc_dv_vsvdb_inject_num == 4 && !memcmp(new_dovi_setting.vsvdb_tbl, injected, 12));
}
"""

if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / 'sink.c'
        binary = Path(temp) / 'sink'
        source.write_text(program(DRIVER.read_text(), VINFO.read_text()))
        subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Werror', str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print('PASS: sink modeset, VSVDB recovery, injection, luminance and forced DV')
