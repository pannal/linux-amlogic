#!/usr/bin/env python3
"""Exercise production HDMI attribute handling with a host modeset stub.

No kernel build, DV transport emulation or hardware verification is performed.
"""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
root = parser.parse_args().source_root
main = (root / 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hdmi_tx_main.c').read_text()
common = (root / 'drivers/amlogic/media/vout/hdmitx/hdmi_common/hdmi_parameters.c').read_text()
strings = (root / 'lib/string.c').read_text()


def function(source, signature):
    start = source.index(signature)
    end = source.index('\n}', start) + 2
    return source[start:end] + '\n'


tables = common[common.index('static struct parse_cd parse_cd_[]'):
                common.index('const char *hdmi_get_str_cd')]
production = function(strings, 'bool sysfs_streq(') + tables
production += function(common, 'static void hdmi_parse_attr(')
store = function(main, 'ssize_t store_attr(')
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
enum { COLORSPACE_RGB444, COLORSPACE_YUV422, COLORSPACE_YUV444, COLORSPACE_YUV420 };
enum { COLORDEPTH_24B=4, COLORDEPTH_30B, COLORDEPTH_36B, COLORDEPTH_48B };
enum { COLORRANGE_LIM, COLORRANGE_FUL };
struct parse_cd { int cd; const char *name; };
struct parse_cs { int cs; const char *name; };
struct parse_cr { int cr; const char *name; };
struct hdmi_format_para { int cs, cd, cr; };
struct device { int unused; };
struct device_attribute { int unused; };
static struct hdmi_format_para para;
static struct { char fmt_attr[16]; struct hdmi_format_para *para; } hdmitx_device = { .para=&para };
static int setclk_mutex, lock_depth, modesets, checks, failures;
static int output_cs, output_cd;
static char applied_attr[16];
#define CHECK(x) do { checks++; if (!(x)) { fprintf(stderr,"line %d: %s\n",__LINE__,#x); failures++; } } while (0)
static void mutex_lock(int *lock) { (void)lock; assert(lock_depth++ == 0); }
static void mutex_unlock(int *lock) { (void)lock; assert(--lock_depth == 0); }
'''
modeset = r'''
/* Exercise the real parser, stopping before EOTF, bandwidth and hardware policy. */
static int set_disp_mode_auto_locked(void) {
    assert(lock_depth == 1);
    modesets++;
    memcpy(applied_attr, hdmitx_device.fmt_attr, sizeof(applied_attr));
    hdmi_parse_attr(&para, hdmitx_device.fmt_attr);
    output_cs = para.cs;
    output_cd = para.cd;
    return 0;
}
'''
cases = r'''
static void write_attr(const char *value) {
    CHECK(store_attr(NULL, NULL, value, strlen(value)) == (ssize_t)strlen(value));
    CHECK(lock_depth == 0);
}
int main(void) {
    const char *formats[] = {"rgb", "422", "444", "420"};
    int expected[] = {COLORSPACE_RGB444, COLORSPACE_YUV422, COLORSPACE_YUV444, COLORSPACE_YUV420};
    const char *depths[] = {"8bit", "10bit", "12bit", "16bit"};
    char value[32], saved[16];
    int cs, depth, comma, repeat;
    for (cs=0; cs<4; cs++) for (depth=0; depth<4; depth++) for (comma=0; comma<2; comma++) {
        snprintf(value, sizeof(value), "%s%s,%s", comma ? "," : "", formats[cs], depths[depth]);
        modesets=0;
        write_attr(value);
        CHECK(para.cs == expected[cs]);
        CHECK(modesets == 0);
        memcpy(saved, hdmitx_device.fmt_attr, sizeof(saved));
        for (repeat=0; repeat<3; repeat++) {
            write_attr(repeat == 1 ? "now\n" : "now");
            CHECK(output_cs == expected[cs]);
            CHECK(output_cd == COLORDEPTH_24B + depth);
            CHECK(!memcmp(saved, hdmitx_device.fmt_attr, sizeof(saved)));
            CHECK(!memcmp(saved, applied_attr, sizeof(saved)));
        }
        CHECK(modesets == 3);
        snprintf(value, sizeof(value), "%s%s,%s,now", comma ? "," : "", formats[cs], depths[depth]);
        write_attr(value);
        CHECK(modesets == 4);
        CHECK(output_cs == expected[cs]);
        CHECK(output_cd == COLORDEPTH_24B + depth);
        CHECK(strstr(applied_attr, "now") == NULL);
        CHECK(strstr(hdmitx_device.fmt_attr, "now") == NULL);
    }
    /* All-Auto must be able to clear a previous explicit setting. */
    write_attr("444,12bit,now");
    write_attr(",now\n");
    CHECK(output_cs == COLORSPACE_YUV422);
    CHECK(output_cd == COLORDEPTH_24B);
    CHECK(strstr(hdmitx_device.fmt_attr, "444") == NULL);
    write_attr("now");
    CHECK(output_cs == COLORSPACE_YUV422);
    write_attr("rgb,16bit,now");
    write_attr(",");
    CHECK(para.cs == COLORSPACE_YUV422);
    write_attr("now");
    CHECK(output_cs == COLORSPACE_YUV422 && output_cd == COLORDEPTH_24B);
    /* Depth-only selection keeps Auto chroma, including on refresh. */
    write_attr(",10bit,now");
    write_attr("now\n");
    CHECK(output_cs == COLORSPACE_YUV422 && output_cd == COLORDEPTH_30B);
    /* Explicit chroma with Auto depth uses the native and VS10 spellings. */
    for (cs=0; cs<4; cs++) {
        snprintf(value, sizeof(value), ",%s,", formats[cs]);
        write_attr(value);
        CHECK(para.cs == expected[cs]);
        write_attr("now\n");
        CHECK(output_cs == expected[cs] && output_cd == COLORDEPTH_24B);
        snprintf(value, sizeof(value), "%s,now\n", formats[cs]);
        write_attr(value);
        CHECK(output_cs == expected[cs] && output_cd == COLORDEPTH_24B);
    }
    /* Retain existing bounded-copy behavior on long input. */
    write_attr("444,12bit,limit,xxxxxxxxxxxxxxxx");
    CHECK(hdmitx_device.fmt_attr[15] == '\0');
    CHECK(para.cs == COLORSPACE_YUV444);
    printf("%d attribute checks, %d failures\n", checks, failures);
    return failures != 0;
}
'''
with tempfile.TemporaryDirectory(prefix='hdmitx-attr-') as tmp:
    src = Path(tmp) / 'test.c'
    binary = Path(tmp) / 'test'
    src.write_text(preamble + production + modeset + store + cases)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu99', '-Wall', '-Wextra',
                    '-Wno-unused-parameter', '-Wno-sign-compare',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    '-o', str(binary), str(src)], check=True)
    subprocess.run([str(binary)], check=True)
