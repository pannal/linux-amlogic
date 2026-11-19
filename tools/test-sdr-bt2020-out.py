#!/usr/bin/env python3
"""Check the opt-in amvecm sdr_bt2020_out (SDR transfer, BT.2020 colour) path."""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--negative-controls", action="store_true")
args = parser.parse_args()
spec = importlib.util.spec_from_file_location(
    "kernel_test", ROOT / "tools/test-pcm-native-layouts.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
VECM = ROOT / "drivers/amlogic/media/enhancement/amvecm"
hdr2 = (VECM / "set_hdr2_v0.c").read_text()
amcsc = (VECM / "amcsc.c").read_text()
amcsc_h = (VECM / "amcsc.h").read_text()
pip = (VECM / "amcsc_pip.c").read_text()


def array(source, name):
    body = re.search(r"\b%s\[[^\]]*\]\s*=\s*\{([^}]*)\}" % name, source).group(1)
    return [int(v) for v in re.findall(r"-?\d+", body)]


def mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def inv(m):
    a, b, c = m[0]; d, e, f = m[1]; g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def rgb_to_xyz(primaries, white=(0.3127, 0.3290)):
    cols = [[x / y, 1.0, (1 - x - y) / y] for x, y in primaries]
    p = [[cols[j][i] for j in range(3)] for i in range(3)]
    w = [white[0] / white[1], 1.0, (1 - white[0] - white[1]) / white[1]]
    s = [sum(row[k] * w[k] for k in range(3)) for row in inv(p)]
    return [[p[i][j] * s[j] for j in range(3)] for i in range(3)]


def check_matrices(source):
    want = mul(inv(rgb_to_xyz(((0.708, 0.292), (0.170, 0.797), (0.131, 0.046)))),
               rgb_to_xyz(((0.64, 0.33), (0.30, 0.60), (0.15, 0.06))))
    got = array(source, "sdr_709_2020_1024")
    assert len(got) == 9
    for r in range(3):
        # 1024 == 1.0 (SDR_GMT_CONVERT gamut shift 11, EOTF at 1/2): white stays white.
        assert sum(got[r * 3:r * 3 + 3]) == 1024, ("row sum", r)
        for c in range(3):
            assert abs(got[r * 3 + c] - 1024 * want[r][c]) < 1.0, (r, c, got, want)
    # Output encoder: BT.2020 NCL limited range (876/896 of 1024).
    ncl = array(source, "rgb2ycbcr_ncl2020")[:9]
    kr, kb = 0.2627, 0.0593
    kg = 1 - kr - kb
    ref = [kr * 876, kg * 876, kb * 876,
           -kr / (2 - 2 * kb) * 896, -kg / (2 - 2 * kb) * 896, 448,
           448, -kg / (2 - 2 * kr) * 896, -kb / (2 - 2 * kr) * 896]
    assert all(abs(a - b) < 1.0 for a, b in zip(ncl, ref)), (ncl, ref)


check_matrices(hdr2)
procs = dict((m.group(1), int(m.group(2))) for m in re.finditer(
    r"^#define\s+(PROC_\w+)\s+(\d+)", amcsc_h, re.M))
sdr_modes = [n for n in procs if n.startswith("PROC_SDR_TO_")] + ["PROC_BYPASS"]
assert procs["PROC_SDR_TO_BT2020"] not in [procs[n] for n in sdr_modes
                                           if n != "PROC_SDR_TO_BT2020"] + [procs["PROC_OFF"]]
assert "osd_pq_applied = osd_pq;\n\t\tsdr_bt2020_applied = READ_ONCE(sdr_bt2020_policy);" in hdr2
# Video off (GUI): adaptive policy must keep the HDR module for BT.2020 SDR.
assert "if (get_hdr_policy() == 1 &&\n\t\t\t\t    !sdr_bt2020_video_off(vinfo)) {" in amcsc
assert "return sdr_bt2020_eligible(vinfo, HDRTYPE_SDR);" in pip
# A new video's first (still-bypassed) pass must not drop the GUI to BT.709.
assert ("if (get_hdr_module_status(vd_path) == HDR_MODULE_BYPASS &&\n\t\t    cur_hdr_policy == 1 &&\n"
        "\t\t    sdr_bt2020_eligible(vinfo, HDRTYPE_SDR))\n\t\t\ttarget_format[vd_path] = BT2020;\n"
        "\t} else if (cur_hdr_policy == 0) {") in pip
print("PASS: 709->2020 gamut (1024 units, unit row sums), NCL2020 encoder, distinct PROC id, OSD ack")

PRELUDE = r"""
#include <assert.h>
#include <stdbool.h>
#include <string.h>
#include <stdint.h>
typedef uint32_t u32;
#define READ_ONCE(v) (v)
"""
eligible = PRELUDE + r"""
enum hdr_type_e { HDRTYPE_NONE, HDRTYPE_SDR, HDRTYPE_HDR10, HDRTYPE_SDR2020 };
enum { UNKNOWN_FMT = 0, BT2020_PQ = 3 };
#define HDR_SUPPORT (1 << 2)
#define BT2020_SUPPORT (1 << 5)
enum color_fmt { COLOR_FMT_RGB444, COLOR_FMT_YUV422, COLOR_FMT_YUV444 };
struct vinfo_s { int caps; enum color_fmt viu_color_fmt; };
static u32 sdr_bt2020_policy = 1;
static int force_output = UNKNOWN_FMT, gamut_conv_enable;
static int get_force_output(void) { return force_output; }
static int sink_hdr_support(struct vinfo_s *v) { return v->caps; }
""" + helpers.function(pip, "sdr_bt2020_eligible") + r"""
int main(void) {
 struct vinfo_s v = {HDR_SUPPORT | BT2020_SUPPORT, COLOR_FMT_YUV422};
 assert(sdr_bt2020_eligible(&v, HDRTYPE_SDR));
 assert(!sdr_bt2020_eligible(&v, HDRTYPE_SDR2020));
 assert(!sdr_bt2020_eligible(&v, HDRTYPE_HDR10));
 v.caps = HDR_SUPPORT; assert(!sdr_bt2020_eligible(&v, HDRTYPE_SDR));
 v.caps = BT2020_SUPPORT; assert(!sdr_bt2020_eligible(&v, HDRTYPE_SDR));
 v.caps |= HDR_SUPPORT; v.viu_color_fmt = COLOR_FMT_RGB444;
 assert(!sdr_bt2020_eligible(&v, HDRTYPE_SDR));
 v.viu_color_fmt = COLOR_FMT_YUV444; force_output = BT2020_PQ;
 assert(!sdr_bt2020_eligible(&v, HDRTYPE_SDR));
 force_output = UNKNOWN_FMT; gamut_conv_enable = 1;
 assert(!sdr_bt2020_eligible(&v, HDRTYPE_SDR));
 gamut_conv_enable = 0; sdr_bt2020_policy = 0;
 assert(!sdr_bt2020_eligible(&v, HDRTYPE_SDR));
 return 0;
}
"""
pending = PRELUDE + r"""
enum vd_path_e { VD1_PATH, VD2_PATH, VD_PATH_MAX };
static u32 osd_pq_passthrough, osd_pq_applied, sdr_bt2020_out, sdr_bt2020_applied;
""" + "#define OSD_PQ_TRIES 4\nstatic u32 osd_pq_target, sdr_bt2020_target;\n" \
    "static int osd_pq_tries[VD_PATH_MAX];\n" + \
    helpers.function(amcsc, "osd_pq_pending") + r"""
int main(void) {
 assert(!osd_pq_pending(VD1_PATH));
 sdr_bt2020_out = 1;                      /* knob on: OSD1 must be reprogrammed */
 for (int i = 0; i < 4; ++i) { assert(osd_pq_pending(VD1_PATH)); ++osd_pq_tries[0]; }
 assert(!osd_pq_pending(VD1_PATH));       /* bounded when OSD1 never acks */
 sdr_bt2020_out = 0; assert(osd_pq_pending(VD1_PATH) == 0);
 sdr_bt2020_out = 7; assert(osd_pq_pending(VD1_PATH)); /* tries reset on change */
 sdr_bt2020_applied = 1; assert(!osd_pq_pending(VD1_PATH));
 sdr_bt2020_out = 0; assert(osd_pq_pending(VD1_PATH));
 sdr_bt2020_applied = 0; assert(!osd_pq_pending(VD1_PATH));
 return 0;
}
"""


def compile_run(program, name, should_fail=False):
    source, binary = directory / (name + ".c"), directory / name
    source.write_text(program)
    built = subprocess.run(
        ["cc", "-std=gnu99", "-O1", "-Wall", "-Wextra", "-Werror",
         "-Wno-unused-parameter", "-Wno-unused-function",
         "-fsanitize=address,undefined", str(source), "-o", str(binary)])
    assert built.returncode == 0 or should_fail, name
    result = subprocess.run([str(binary)]) if built.returncode == 0 else built
    if should_fail:
        assert result.returncode != 0, (name, "negative control survived")
    else:
        assert result.returncode == 0, name


with tempfile.TemporaryDirectory(prefix="sdr-bt2020-") as directory:
    directory = Path(directory)
    compile_run(eligible, "eligible")
    compile_run(pending, "pending")
    print("PASS: eligibility gates (SDR only, HDR+BT2020 sink, YCbCr, unforced) and OSD1 reprogramming")
    if args.negative_controls:
        try:
            check_matrices(hdr2.replace("643, 337, 44", "627, 329, 43"))
        except (AssertionError, AttributeError):
            pass
        else:
            raise AssertionError("unnormalised gamut survived")
        try:
            check_matrices(re.sub(r"(sdr_709_2020_1024\[9\] = \{)[^}]*\}",
                                  r"\g<1>1024, 0, 0, 0, 1024, 0, 0, 0, 1024}", hdr2))
        except AssertionError:
            pass
        else:
            raise AssertionError("identity gamut survived")
        for old, new, name in (
            ("(sink_hdr_support(vinfo) & BT2020_SUPPORT) &&", "", "no-bt2020-cap"),
            ("vinfo->viu_color_fmt != COLOR_FMT_RGB444 &&", "", "rgb-allowed"),
            ("source == HDRTYPE_SDR &&", "", "any-source"),
        ):
            assert eligible.count(old) == 1, name
            compile_run(eligible.replace(old, new), name, True)
        old = "bt2020 != !!READ_ONCE(sdr_bt2020_applied)"
        assert pending.count(old) == 1
        compile_run(pending.replace(old, "0"), "no-osd-reprogram", True)
        print("PASS: gamut, eligibility and OSD reprogramming controls fail")
