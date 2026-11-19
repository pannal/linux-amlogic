#!/usr/bin/env python3
"""Check the hdmitx HDR10 pre-arm (signal PQ while the HDR10 file loads)."""
import argparse
import importlib.util
from pathlib import Path
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
TX = ROOT / "drivers/amlogic/media/vout/hdmitx/hdmi_tx_20"
main = (TX / "hdmi_tx_main.c").read_text()
mute = (TX / "hdmi_tx_video_mute.h").read_text()


def between(source, start, end):
    i = source.index(start)
    return source[i:source.index(end, i)]


# Source-level contracts that the compiled model below cannot see.
req = helpers.function(main, "hdmitx_set_drm_pkt_req")
arm = between(req, "case HDMITX_DRM_REQ_PREARM:", "case HDMITX_DRM_REQ_PREARM_CANCEL:")
assert arm.index("hdr_prearm_state != HDR_PREARM_IDLE ||\n\t\t    !hdmitx_hdr_prearm_ok(hdev)") < \
    arm.index("hdmitx_hdr_prearm_hold(hdev);"), "mute before eligibility"
assert "data = &hdr_prearm_pq;\n\t\torigin = HDMITX_HDR_ORIGIN_PLAIN_HDR10;" in arm
cancel = between(req, "case HDMITX_DRM_REQ_PREARM_CANCEL:", "default:")
assert "data = &hdr_prearm_sdr;" in cancel
assert cancel.index("hdr_prearm_state != HDR_PREARM_WAITING") < cancel.index("data = &hdr_prearm_sdr;")
assert req.index("spin_lock_irqsave(&hdev->edid_spinlock, flags);") < req.index("switch (req)") < \
    req.index("zero = hdmitx_hdr_native(hdev, data, origin, &exit);"), "request decided outside the lock"
assert "hdmitx_set_drm_pkt_req(data, origin, HDMITX_DRM_REQ_NORMAL);" in \
    helpers.function(main, "hdmitx_set_drm_pkt_from")
tail = between(req, "if (zero && hdev->hdmi_current_hdr_mode == 1) {", "/* No running or pending work will release the mute: undo it. */")
native = between(tail, "hdmitx_queue_hdr(hdev, true)) {", "return;")
assert "hdmitx_hdr_entry_hold(hdev);" in native, "native entry skips the legacy hold"
assert tail.count("hdmitx_hdr_entry_hold(hdev);") == 1
assert "hdr_prearm_entry_hold ?\n\t\t\t\t\t  HDR_PREARM_RESULT_ENTRY_HOLD :" in \
    helpers.function(main, "hdr_prearm_work_func")
store = between(main, "static ssize_t hdr_prearm_store(", "static DEVICE_ATTR(hdr_prearm,")
assert "kstrtouint(buf, 10, &value) || value > 1" in store and "hdr_prearm_settle_ms" not in store
assert "hdmitx_hdr_prearm_close(hdev);" in helpers.function(main, "hdmitx_close_hdr_work")
assert "cancel_delayed_work_sync(&hdr_prearm_work);" in helpers.function(main, "hdmitx_hdr_prearm_close")
assert main.count("device_create_file(dev, &dev_attr_hdr_prearm);") == 1
assert main.count("device_remove_file(dev, &dev_attr_hdr_prearm);") == 1
assert "video_mute_internal || video_mute_kodi || video_mute_prearm ||" in mute
for name, value in (("C_BT2020", 9), ("T_BT2020_10", 14), ("T_SMPTE_ST_2084", 16)):
    assert name in main
print("PASS: pre-arm decided under edid_spinlock, PLAIN_HDR10 entry, native entry hold, SDR cancel, teardown, mute owner")

PRELUDE = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
typedef unsigned int u32;
typedef unsigned long long u64;
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define DIV_ROUND_UP_ULL(n, d) (((n) + (d) - 1) / (d))
#define BIT(n) (1u << (n))
#define T_BT709 1
#define T_BT2020_10 14
#define T_SMPTE_ST_2084 16
#define C_BT709 1
#define C_BT2020 9
#define pr_info(...) ((void)0)
typedef struct { int v; } atomic_t;
static int atomic_read(atomic_t *a) { return a->v; }
struct work_struct { int unused; };
struct delayed_work { int unused; };
#define DECLARE_DELAYED_WORK(n, f) struct delayed_work n
static unsigned long jiffies, delay_set = ~0ul;
static unsigned long msecs_to_jiffies(unsigned int ms) { return ms; }
static unsigned int jiffies_to_msecs(unsigned long j) { return j; }
static int system_wq_obj, *system_wq = &system_wq_obj;
static void mod_delayed_work(int *wq, struct delayed_work *w, unsigned long d) { delay_set = d; }
enum hdmitx_video_mute_owner { HDMITX_MUTE_PREARM = 3 };
struct master_display_info_s { u32 features; };
struct hdmitx_hdr_bypass {
 bool enabled, initialized, closing, failed, legacy_demand, owed;
 unsigned char endpoint; int endpoint_gen; atomic_t transport_gen; unsigned int outstanding;
};
struct hdmitx_dev {
 struct hdmitx_hdr_bypass hdr_bypass; int hdr10plus_feature;
 struct { struct { unsigned int hdr_support; } hdr_info2; } rxcap;
 int hdmi_last_hdr_mode, hdmi_current_hdr_mode; int tx_aud_cfg;
 unsigned int hdr_transfer_feature, hdr_color_feature;
};
static int muted = -1, mute_deferred = -1, transport_ok = 1, dv_en, hdr_status_pos;
static bool hdr_bt2020_exit;
static int hdr_mute_frame = 20;
static unsigned int frame_us = 41708, audio_ops;
static unsigned int hdmitx_get_frame_duration(void) { return frame_us; }
static struct hdmitx_dev d;
static void hdmitx_audio_mute_op(unsigned int flag) { d.tx_aud_cfg = flag; audio_ops++; }
static bool hdmitx_hdr_transport_ok(struct hdmitx_dev *h) { return transport_ok; }
static int hdmitx_dv_en(void) { return dv_en; }
static void hdmitx_update_video_mute(struct hdmitx_dev *h, enum hdmitx_video_mute_owner o,
 bool m, bool d) { assert(o == HDMITX_MUTE_PREARM); muted = m; mute_deferred = d; }
"""
STATE = between(main, "enum {\n\tHDR_PREARM_IDLE,", "enum hdmitx_drm_req {")
FUNCS = "".join(helpers.function(main, n) for n in (
    "hdmitx_drm_is", "hdmitx_hdr_prearm_ok", "hdmitx_hdr_hold_ms",
    "hdmitx_hdr_prearm_hold", "hdmitx_hdr_prearm_release",
    "hdmitx_hdr_entry_hold", "hdmitx_hdr_prearm_packet"))
TEST = r"""
static void hdr_prearm_work_func(struct work_struct *w) {}
static struct master_display_info_s pq = {(9u << 16) | (16u << 8)},
 sdr2020 = {(9u << 16) | (14u << 8)}, sdr709 = {(1u << 16) | (1u << 8)};
static void reset(void) {
 memset(&d, 0, sizeof(d)); d.tx_aud_cfg = 1; hdr_mute_frame = 20; frame_us = 41708;
 hdr_prearm_state = HDR_PREARM_IDLE; hdr_prearm_audio = hdr_prearm_entry_hold = false;
 d.hdr_bypass.enabled = d.hdr_bypass.initialized = true;
 d.hdr_bypass.endpoint = 1; d.rxcap.hdr_info2.hdr_support = 0x4;
 d.hdmi_last_hdr_mode = d.hdmi_current_hdr_mode = 4;
 d.hdr_transfer_feature = T_BT2020_10; d.hdr_color_feature = C_BT2020;
 transport_ok = 1; dv_en = 0; hdr_status_pos = 0; hdr_bt2020_exit = false;
}
static void wait(unsigned long start, unsigned long now) {
 hdr_prearm_state = HDR_PREARM_WAITING; hdr_prearm_start = start; jiffies = now;
 hdr_prearm_sdr_repeats = 0; hdr_prearm_settle_ms = 600; muted = 1; delay_set = ~0ul;
}
int main(void) {
 reset(); assert(hdmitx_hdr_prearm_ok(&d));
#define NO(stmt) do { reset(); stmt; assert(!hdmitx_hdr_prearm_ok(&d)); } while (0)
 NO(d.hdr_bypass.enabled = false); NO(d.hdr_bypass.failed = true);
 NO(d.hdr_bypass.closing = true); NO(transport_ok = 0); NO(dv_en = 1);
 NO(d.hdr10plus_feature = 1); NO(hdr_status_pos = 4);
 NO(d.rxcap.hdr_info2.hdr_support = 0x8); NO(d.hdr_bypass.endpoint = 2);
 NO(d.hdr_bypass.endpoint_gen = 1); NO(d.hdr_bypass.outstanding = 1);
 NO(d.hdr_bypass.legacy_demand = true); NO(d.hdr_bypass.owed = true);
 NO(hdr_bt2020_exit = true); NO(d.hdmi_last_hdr_mode = 0);
 NO(d.hdmi_current_hdr_mode = 1);           /* already HDR: nothing to pre-arm */
 NO(d.hdr_transfer_feature = T_BT709);      /* BT.709 GUI would change AVI */
 NO(d.hdr_color_feature = C_BT709);
 /* Idle: packets pass through untouched. */
 hdr_prearm_state = HDR_PREARM_IDLE; muted = -1;
 assert(!hdmitx_hdr_prearm_packet(&d, &sdr2020) && muted == -1);
 /* HDR video 200 ms after arming: show it after the remaining 400 ms. */
 wait(1000, 1200);
 assert(!hdmitx_hdr_prearm_packet(&d, &pq));
 assert(hdr_prearm_state == HDR_PREARM_SETTLING && muted == 1 && delay_set == 400);
 assert(!hdmitx_hdr_prearm_packet(&d, &sdr2020) && hdr_prearm_state == HDR_PREARM_SETTLING);
 /* Late video: show it on the next work run. */
 wait(1000, 5000); assert(!hdmitx_hdr_prearm_packet(&d, &pq) && delay_set == 0);
 /* The held BT.2020 SDR output repeats a bounded number of times. */
 wait(0, 0);
 for (int i = 0; i < HDR_PREARM_SDR_REPEATS; ++i)
  assert(hdmitx_hdr_prearm_packet(&d, &sdr2020) && muted == 1);
 assert(!hdmitx_hdr_prearm_packet(&d, &sdr2020));
 assert(hdr_prearm_state == HDR_PREARM_IDLE && muted == 0 && mute_deferred == 1);
 assert(hdr_prearm_result == HDR_PREARM_RESULT_SDR_VIDEO);
 /* Anything else ends the pre-arm and is processed normally. */
 wait(0, 0); assert(!hdmitx_hdr_prearm_packet(&d, &sdr709));
 assert(hdr_prearm_state == HDR_PREARM_IDLE && hdr_prearm_result == HDR_PREARM_RESULT_OTHER_PKT);
 wait(0, 0); assert(!hdmitx_hdr_prearm_packet(&d, NULL) && muted == 0);
 struct master_display_info_s legacy = {pq.features | BIT(30)};
 wait(0, 0); assert(!hdmitx_hdr_prearm_packet(&d, &legacy) && hdr_prearm_state == HDR_PREARM_IDLE);
 /* The hold is the legacy hdr_mute_frame black of the current mode. */
 reset(); assert(hdmitx_hdr_hold_ms() == 835);
 frame_us = 16683; assert(hdmitx_hdr_hold_ms() == 334);
 hdr_mute_frame = 0; assert(hdmitx_hdr_hold_ms() == 0);
 hdr_mute_frame = 1000; assert(hdmitx_hdr_hold_ms() == HDR_PREARM_MAX_SETTLE_MS);
 hdr_mute_frame = -1; assert(hdmitx_hdr_hold_ms() == 0);
 /* Pre-arm hold: black and HDMI audio muted, both restored on release. */
 reset(); jiffies = 7; audio_ops = 0; hdmitx_hdr_prearm_hold(&d);
 assert(muted == 1 && mute_deferred == 0 && d.tx_aud_cfg == 0 && hdr_prearm_audio);
 assert(hdr_prearm_settle_ms == 835 && hdr_prearm_start == 7);
 hdr_prearm_state = HDR_PREARM_SETTLING; hdmitx_hdr_prearm_release(&d, HDR_PREARM_RESULT_VIDEO);
 assert(muted == 0 && d.tx_aud_cfg == 1 && !hdr_prearm_audio && audio_ops == 2);
 /* Audio someone else had off, or a zero hold, is left alone. */
 reset(); d.tx_aud_cfg = 2; audio_ops = 0; hdmitx_hdr_prearm_hold(&d);
 hdmitx_hdr_prearm_release(&d, HDR_PREARM_RESULT_CANCEL); assert(d.tx_aud_cfg == 2 && !audio_ops);
 reset(); hdr_mute_frame = 0; audio_ops = 0; hdmitx_hdr_prearm_hold(&d);
 assert(muted == 1 && !audio_ops && hdr_prearm_settle_ms == 0);
 hdmitx_hdr_prearm_release(&d, HDR_PREARM_RESULT_CANCEL); assert(muted == 0 && !audio_ops);
 /* A writer that changed audio while held keeps its setting. */
 reset(); hdmitx_hdr_prearm_hold(&d); d.tx_aud_cfg = 1; audio_ops = 0;
 hdmitx_hdr_prearm_release(&d, HDR_PREARM_RESULT_CANCEL); assert(!audio_ops);
 /* Native entry without a pre-arm: the same hold from the PQ packet. */
 reset(); delay_set = ~0ul; hdmitx_hdr_entry_hold(&d);
 assert(hdr_prearm_state == HDR_PREARM_SETTLING && hdr_prearm_entry_hold);
 assert(muted == 1 && d.tx_aud_cfg == 0 && delay_set == 835);
 hdmitx_hdr_prearm_release(&d, HDR_PREARM_RESULT_ENTRY_HOLD);
 assert(!hdr_prearm_entry_hold && muted == 0 && d.tx_aud_cfg == 1);
 /* ... but not on top of a pre-arm, nor when the legacy hold is off. */
 reset(); wait(0, 0); delay_set = ~0ul; hdmitx_hdr_entry_hold(&d);
 assert(hdr_prearm_state == HDR_PREARM_WAITING && !hdr_prearm_entry_hold && delay_set == ~0ul);
 reset(); hdr_mute_frame = 0; muted = -1; hdmitx_hdr_entry_hold(&d);
 assert(hdr_prearm_state == HDR_PREARM_IDLE && muted == -1);
 /* The synthetic packets are the endpoints the native path recognises. */
 assert(hdmitx_drm_is(&hdr_prearm_pq, T_SMPTE_ST_2084, C_BT2020));
 assert(hdmitx_drm_is(&hdr_prearm_sdr, T_BT2020_10, C_BT2020));
 return 0;
}
"""
SYNTH = between(main, "static struct master_display_info_s hdr_prearm_pq = {",
                "static void hdmitx_set_drm_pkt_req(")


def compile_run(directory, program, name, should_fail=False):
    source, binary = directory / (name + ".c"), directory / name
    source.write_text(program)
    built = subprocess.run(
        ["cc", "-std=gnu99", "-O1", "-Wall", "-Wextra", "-Werror",
         "-Wno-unused-parameter", "-Wno-unused-function", "-Wno-unused-variable",
         "-fsanitize=address,undefined", str(source), "-o", str(binary)])
    assert built.returncode == 0 or should_fail, name
    result = subprocess.run([str(binary)]) if built.returncode == 0 else built
    if should_fail:
        assert result.returncode != 0, (name, "negative control survived")
    else:
        assert result.returncode == 0, name


def program(funcs=FUNCS, state=STATE):
    return PRELUDE + state + funcs + SYNTH + TEST


with tempfile.TemporaryDirectory(prefix="hdr-prearm-") as directory:
    directory = Path(directory)
    compile_run(directory, program(), "prearm")
    print("PASS: eligibility (BT.2020 SDR endpoint only), settle timing, bounded SDR repeats, release")
    if args.negative_controls:
        for old, new, name in (
            ("hdr_prearm_sdr_repeats < HDR_PREARM_SDR_REPEATS) {", "1) {", "unbounded-swallow"),
            ("elapsed < settle ? settle - elapsed : 0", "0", "no-settle"),
            ("!hdr_bt2020_exit &&", "", "during-exit"),
            ("hdev->hdmi_last_hdr_mode == 4 && hdev->hdmi_current_hdr_mode == 4 &&", "", "from-hdr"),
            ("hdev->hdr_transfer_feature == T_BT2020_10 &&", "", "from-709"),
            ("hdmitx_update_video_mute(hdev, HDMITX_MUTE_PREARM, false, true);", "", "stays-black"),
            ("us = (u64)hdr_mute_frame * hdmitx_get_frame_duration();", "us = 600000;", "fixed-hold"),
            ("hdr_prearm_audio = hdr_prearm_settle_ms && hdev->tx_aud_cfg == 1;",
             "hdr_prearm_audio = false;", "audio-plays"),
            ("if (hdr_prearm_audio && hdev->tx_aud_cfg == 0)", "if (hdr_prearm_audio)", "audio-clobber"),
            ("if (hdr_prearm_state != HDR_PREARM_IDLE || hdr_mute_frame <= 0)",
             "if (hdr_mute_frame <= 0)", "hold-over-prearm"),
            ("hdr_prearm_entry_hold = true;", "", "entry-unmarked"),
        ):
            assert FUNCS.count(old) == 1, name
            compile_run(directory, program(FUNCS.replace(old, new)), name, True)
        print("PASS: repeat bound, settle, exit, HDR, BT.709, release, hold length, audio and entry-hold controls fail")
