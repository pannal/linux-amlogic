#!/usr/bin/env python3
"""Check the default-off hdmitx same-transport refresh for attr "now"."""
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
video = (TX / "hdmi_tx_video.c").read_text()
hw = (TX / "hw/hdmi_tx_hw.c").read_text()


def between(source, start, end):
    i = source.index(start)
    return source[i:source.index(end, i)]


# Source contracts.
auto = helpers.function(main, "__set_disp_mode_auto_locked")
assert "return __set_disp_mode_auto_locked(false);" in helpers.function(main, "set_disp_mode_auto_locked")
store = between(main, "ssize_t store_attr(", "/*aud_mode attr*/")
assert store.count("__set_disp_mode_auto_locked(true);") == 2 and "set_disp_mode_auto_locked();" not in store
assert main.count("__set_disp_mode_auto_locked(true)") == 3, "only attr now and vout set_mode may refresh"
vout_set = helpers.function(main, "set_disp_mode_auto")
assert vout_set.index("mutex_lock(&setclk_mutex);") < vout_set.index("ret = __set_disp_mode_auto_locked(true);") < \
    vout_set.index("mutex_unlock(&setclk_mutex);"), "vout same-mode rewrite uses guarded fast path"
plug_reconnect = main.count("set_disp_mode_auto_locked();")
assert plug_reconnect >= 1, "locked HPD/reconnect callers keep full modeset"
# Disabled DV must not keep a stale DV EOTF forcing the tunnel colour format.
assert "bool dv_enabled = is_dolby_vision_enable();" in auto
assert "int effective_eotf = (xbmc_dv_non_ipt || !dv_enabled) ?\n\t\tEOTF_T_NULL : hdev->hdmi_current_eotf_type;" in auto
assert auto.index("const struct hdmitx_same_mode snap = hdmitx_same_mode;") < \
    auto.index("hdmitx_same_mode.valid = false;") < auto.index("hdev->ready = 0;")
assert "const bool was_ready = hdev->ready;" in auto
fast = between(auto, "same_mode_last = hdmitx_same_mode_mismatch(", "same_mode_modesets++;")
assert auto.index("if (xbmc_aml_linux_force_422) {") < auto.index("vic = HDMI_4k2k_smpte_24;") < \
    auto.index("req_cs = para->cs;") < auto.index("same_mode_last =") < \
    auto.index("link = hdmitx_hdr_pre_display_init(hdev, link);"), "decided before final parameters"
for banned in ("hdmitx_hdr_pre_display_init", "hdmitx_set_display(", "AVMUTE", "hdmitx_set_audio",
               "cedst", "setdispmode", "hdmitx_setup_gen ="):
    assert banned not in fast, banned
dvblock = between(fast, "if (!dv) {", "}")
assert "hdmitx_hw_same_mode_refresh(hdev);" in dvblock and "hdmitx_refresh_display_pkts(hdev, vic);" in dvblock
assert fast.index("para->cs = snap.cs;") < fast.index("if (!dv) {")
for need in ("hdev->ready = 1;", "hdmitx_hdr_commit(hdev, 0, link);",
             "hdmitx_same_mode_record(hdev, info, gen, link, req_cs, req_cd);", "return 0;"):
    assert need in fast, need
assert "if (ret >= 0)\n\t\thdmitx_same_mode_record(hdev, info, gen, link, req_cs, req_cd);" in auto
# Only a successful, displaying full modeset rebuilds transport trust, before its commit.
assert main.count("hdmitx_hdr_transport_rebuilt(hdev);") == 1
assert "if (ret >= 0 && hdev->cur_VIC != HDMI_Unknown)\n\t\thdmitx_hdr_transport_rebuilt(hdev);\n\thdmitx_hdr_commit(hdev, ret, link);" in auto
assert "hdmitx_hdr_transport_rebuilt" not in fast
# Refresh AVI/packet RMWs are serialised with the vsync DRM writer.
assert "spin_lock_irqsave(&hdev->edid_spinlock, flags);\n\t\t\thdmitx_hw_same_mode_refresh(hdev);\n\t\t\thdmitx_refresh_display_pkts(hdev, vic);\n\t\t\tspin_unlock_irqrestore(&hdev->edid_spinlock, flags);" in dvblock
assert auto.index("hdmitx_hdr_commit(hdev, ret, link);") < auto.rindex("hdmitx_same_mode_record(")
assert "static bool seamless_mode_refresh;" in main, "default off"
# HPD plugin: modeset is recordable only inside the locked handler, before hpd_state publish.
plug = helpers.function(main, "hdmitx_hpd_plugin_handler")
assert plug.index("mutex_lock(&setclk_mutex);") < \
    plug.index("hdmitx_same_mode_plugin = true;\n\tset_disp_mode_auto_locked();\n\thdmitx_same_mode_plugin = false;") < \
    plug.index("hdev->hpd_state = 1;") < plug.index("mutex_unlock(&setclk_mutex);")
assert main.count("hdmitx_same_mode_plugin = true;") == 1
keep_mute = between(plug, "/* keep avmute & clear pkt", "hdmitx_set_hdr10plus_pkt(0, NULL);")
assert "hdmitx_same_mode.valid = false;" in keep_mute, "kept AVMUTE needs a full modeset"
assert main.count("device_create_file(dev, &dev_attr_seamless_mode_refresh);") == 1
assert main.count("device_remove_file(dev, &dev_attr_seamless_mode_refresh_state);") == 1
# hw_gen accounting: every untrusted control bumps it; packet-only AVI fields do not.
unt = helpers.function(main, "hdmitx_hdr_transport_untrusted")
assert "hdmitx_same_mode_hw_changed();" in unt
assert "hdmitx_same_mode_hw_changed" not in helpers.function(main, "hdmitx_hdr_packet_untrusted")
assert "hdmitx_same_mode_hw_changed();" in helpers.function(hw, "hdmitx_set_dispmode")
cfg = helpers.function(hw, "hdmitx_cntl_config")
assert "cmd == CONF_AVI_YQ01)\n\t\thdmitx_hdr_packet_untrusted(hdev, HDMITX_HDR_PKT_AVI);" in cfg
assert "CONF_CLR_AVI_PACKET) {\n\t\thdmitx_same_mode_hw_changed();" in cfg
setpkt = helpers.function(hw, "hdmitx_set_packet")
assert "hdmitx_hdr_packet_untrusted(get_hdmitx_device(),\n\t\t\tHDMITX_HDR_PKT_VSIF);\n\telse if (type == HDMI_PACKET_VEND)\n\t\thdmitx_hdr_packet_clean(" in setpkt
assert setpkt.index("hdmitx_hdr_packet_clean(") < setpkt.index("switch (type)")
assert hw.count("hdmitx_hdr_transport_untrusted(hdev);") >= 4
# Trust restoration: only full AVI rewrite and VSIF-off clean their class.
avi = helpers.function(hw, "hdmitx_config_avi")
assert avi.rstrip().endswith("hdmitx_hdr_packet_clean(hdev, HDMITX_HDR_PKT_AVI);\n}")
assert hw.count("hdmitx_hdr_packet_clean(hdev, HDMITX_HDR_PKT_AVI);") == 1
en = helpers.function(hw, "hdmitx_config_pkt_enables")
three_d = between(en, "if (hdev->flag_3dfp", "} else {")
assert "packet_clean" not in three_d, "3D VSIF is transmitted; must not clean"
assert "PACKET_TX_EN, 0, 4, 1);\n\t\t/* VSIF no longer transmitted, whatever its registers hold. */\n\t\thdmitx_hdr_packet_clean(hdev, HDMITX_HDR_PKT_VSIF);" in en
assert hw.count("hdmitx_hdr_packet_clean(") == 3
assert fast.index("hdmitx_hw_same_mode_refresh(hdev);") < fast.index("hdmitx_hdr_commit(hdev, 0, link);")
# Full modeset and refresh share the same AVI/packet code.
h20 = helpers.function(hw, "config_hdmi20_tx")
assert "hdmitx_config_avi(hdev, hdev->cur_video_param->VIC" in h20 or "hdmitx_config_avi(hdev," in h20
assert "hdmitx_config_pkt_enables(hdev);" in h20
ref = helpers.function(hw, "hdmitx_hw_same_mode_refresh")
assert "hdmitx_config_avi(hdev," in ref and "hdmitx_config_pkt_enables(hdev);" in ref
disp = helpers.function(video, "hdmitx_set_display")
assert "hdmitx_set_display_pkts(hdev, VideoCode);" in disp and "hdmi_set_vend_spec_infofram" not in disp
assert "hdmitx_set_display_pkts(hdev, vic);\n\thdmitx_set_spd_info(hdev);" in \
    helpers.function(video, "hdmitx_refresh_display_pkts")
print("PASS: attr-now/vout same-mode only, DV-off EOTF gate, default off, no PHY/timing/AVMUTE/audio on refresh, shared packet code, hw_gen accounting")

PRELUDE = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
typedef struct { int counter; } atomic_t;
#define atomic_read(a) ((a)->counter)
#define READ_ONCE(x) (x)
enum hdmi_vic { HDMI_Unknown = 0 };
struct hdmi_format_para { unsigned int cs, cd, cr; };
struct hdmitx_vidpara { enum hdmi_vic VIC; };
struct vinfo_s { unsigned int sync_duration_num, sync_duration_den, video_clk; };
struct hdmitx_dev {
	int hpd_state; enum hdmi_vic cur_VIC; struct hdmitx_vidpara *cur_video_param;
	unsigned int frac_rate_policy, phy_idx; int flag_3dfp, flag_3dtb, flag_3dss;
	struct hdmi_format_para *para; struct { atomic_t link_gen; } hdr_bypass;
};
static atomic_t hdmitx_same_mode_hw_gen;
static unsigned int hdmitx_setup_gen;
"""

BODY = r"""
static struct hdmi_format_para P = { 1, 6, 0 }, Q = { 1, 6, 0 };
static struct hdmitx_vidpara V = { 32 };
static struct vinfo_s I = { 24000, 1000, 74250 };
static struct hdmitx_dev D;

static void reset(void)
{
	D = (struct hdmitx_dev){ 1, 32, &V, 1, 0, 0, 0, 0, &P };
	D.hdr_bypass.link_gen.counter = 7;
	hdmitx_same_mode_hw_gen.counter = 3;
	hdmitx_setup_gen = 5;
	seamless_mode_refresh = true;
	P = (struct hdmi_format_para){ 1, 6, 0 };
	I = (struct vinfo_s){ 24000, 1000, 74250 };
	hdmitx_same_mode.valid = false;
	hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6);
}

static unsigned int check(bool ok, bool ready, enum hdmi_vic vic)
{
	struct hdmitx_same_mode s = hdmitx_same_mode;
	return hdmitx_same_mode_mismatch(&D, &s, ok, ready, &P, vic, &I, 5);
}

int main(void)
{
	reset();
	assert(hdmitx_same_mode.valid && !check(true, true, 32));
	assert(check(false, true, 32) == 1u << 1);
	assert(check(true, false, 32) == 1u << 3);
	seamless_mode_refresh = false; assert(check(true, true, 32) == 1u << 0); reset();
	D.hpd_state = 0; assert(check(true, true, 32) & 1u << 4); reset();
	hdmitx_same_mode_hw_gen.counter++; assert(check(true, true, 32) == 1u << 5); reset();
	D.hdr_bypass.link_gen.counter++; assert(check(true, true, 32) == 1u << 6); reset();
	hdmitx_setup_gen++; assert(check(true, true, 32) == 1u << 7); reset();
	assert(check(true, true, 16) == (1u << 8 | 1u << 9));
	V.VIC = 16; assert(check(true, true, 32) & 1u << 9); V.VIC = 32; reset();
	{ struct hdmi_format_para *p = D.para; D.para = &Q;
	  hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6); D.para = p;
	  assert(check(true, true, 32) == 1u << 10); reset(); }
	D.frac_rate_policy = 0; assert(check(true, true, 32) == 1u << 11); reset();
	D.phy_idx = 1; assert(check(true, true, 32) == 1u << 11); reset();
	P.cd = 7; assert(check(true, true, 32) == 1u << 12); reset();
	P.cs = 2; assert(check(true, true, 32) == 1u << 12); reset();
	P.cr = 1; assert(check(true, true, 32) == 1u << 12); reset();
	I.video_clk = 74176; assert(check(true, true, 32) == 1u << 13); reset();
	I.sync_duration_num = 23976; assert(check(true, true, 32) == 1u << 13); reset();
	D.flag_3dtb = 1; assert(check(true, true, 32) & 1u << 14); reset();
	/* record refuses unsafe states */
	hdmitx_same_mode.valid = false; D.hdr_bypass.link_gen.counter++;
	hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6); assert(!hdmitx_same_mode.valid); reset();
	hdmitx_same_mode.valid = false; D.cur_VIC = HDMI_Unknown;
	hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6); assert(!hdmitx_same_mode.valid); reset();
	hdmitx_same_mode.valid = false; D.flag_3dfp = 1;
	hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6); assert(!hdmitx_same_mode.valid); reset();
	/* plugin modeset records before hpd_state is published; nothing else does */
	hdmitx_same_mode.valid = false; D.hpd_state = 0;
	hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6); assert(!hdmitx_same_mode.valid);
	hdmitx_same_mode_plugin = true;
	hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6); hdmitx_same_mode_plugin = false;
	assert(hdmitx_same_mode.valid && check(true, true, 32) == 1u << 4);
	D.hpd_state = 1; assert(!check(true, true, 32)); reset();
	/* applied cs (RGB-only override) is kept separately from the request */
	P.cs = 0; hdmitx_same_mode_record(&D, &I, 5, 7, 1, 6); P.cs = 1;
	assert(!check(true, true, 32) && hdmitx_same_mode.cs == 0);
	puts("PASS: compiled mismatch/record model");
	return 0;
}
"""


def model(source):
    struct = between(source, "struct hdmitx_same_mode {", "};") + "};\n"
    statics = between(source, "static bool seamless_mode_refresh;",
                      "static unsigned int hdmitx_same_mode_mismatch(")
    return (PRELUDE + struct + statics +
            helpers.function(source, "hdmitx_same_mode_mismatch") + "\n" +
            helpers.function(source, "hdmitx_same_mode_record") + "\n" + BODY)


def run(source):
    with tempfile.TemporaryDirectory() as d:
        c, exe = Path(d) / "m.c", Path(d) / "m"
        c.write_text(model(source))
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", "-Wno-unused-function", "-Wno-unused-variable",
                        "-o", str(exe), str(c)], check=True)
        return subprocess.run([str(exe)], capture_output=True, text=True)


r = run(main)
sys.stdout.write(r.stdout)
assert r.returncode == 0, r.stderr

TRUST_PRELUDE = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
typedef struct { int counter; } atomic_t;
#define atomic_read(a) ((a)->counter)
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
static int atomic_inc_return(atomic_t *a) { return ++a->counter; }
#define atomic_inc(a) ((void)atomic_inc_return(a))
#define DEFINE_SPINLOCK(n) int n
static int held;
#define spin_lock_irqsave(l, f) do { (void)(l); (f) = 0; assert(!held); held = 1; } while (0)
#define spin_unlock_irqrestore(l, f) do { (void)(l); (void)(f); assert(held); held = 0; } while (0)
#define pr_warn(...) ((void)0)
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
#define HDMITX_HDR_PKT_VSIF 0x1
#define HDMITX_HDR_PKT_AVI 0x2
#define HDMITX_HDR_PKT_ALL 0x3
struct hdmitx_dev { struct { bool untrusted, enabled; atomic_t transport_gen, link_gen; } hdr_bypass; };
"""

TRUST_BODY = r"""
static struct hdmitx_dev D;
#define U (D.hdr_bypass.untrusted)
#define G (D.hdr_bypass.transport_gen.counter)
static void reset(void)
{
	D = (struct hdmitx_dev){0};
	hdmitx_hdr_sticky_untrusted = false;
	hdmitx_hdr_sticky_permanent = false;
	hdmitx_hdr_dirty_pkts = 0;
	hdmitx_same_mode_hw_gen.counter = 0;
}

int main(void)
{
	int g;

	/* DV VSIF dirties only VSIF; a VSIF-off rewrite restores trust. */
	reset(); hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_VSIF);
	assert(U && G == 1 && hdmitx_same_mode_hw_gen.counter == 0);
	g = G; hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_VSIF);
	assert(!U && G == g && !held);
	/* Both classes must be rewritten. */
	reset(); hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_VSIF);
	hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_AVI);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_VSIF); assert(U);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_AVI); assert(!U);
	/* Transport-class untrust is sticky. */
	reset(); hdmitx_hdr_transport_untrusted(&D);
	assert(U && hdmitx_same_mode_hw_gen.counter == 1);
	hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_AVI);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_AVI); assert(U);
	/* Clean of a class that was never dirty does not lift sticky. */
	reset(); hdmitx_hdr_transport_untrusted(&D);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_ALL); assert(U);
	/* Generation overflow is sticky. */
	reset(); D.hdr_bypass.transport_gen.counter = -5;
	hdmitx_hdr_transport_changed(&D); assert(U && hdmitx_hdr_sticky_untrusted);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_ALL); assert(U);
	reset(); D.hdr_bypass.link_gen.counter = -5;
	hdmitx_hdr_link_changed(&D); assert(U && hdmitx_hdr_sticky_untrusted);
	/* Re-dirty after clean is untrusted again. */
	reset(); hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_VSIF);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_VSIF);
	hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_VSIF); assert(U);
	/* Unknown class bits never become a hidden dirty bit. */
	reset(); hdmitx_hdr_packet_untrusted(&D, 0x80 | HDMITX_HDR_PKT_AVI);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_AVI); assert(!U);
	/* A full modeset rebuild lifts transport-class sticky untrust. */
	reset(); hdmitx_hdr_transport_untrusted(&D);
	hdmitx_hdr_transport_rebuilt(&D); assert(!U && !hdmitx_hdr_sticky_untrusted);
	/* ...but not packets still dirty. */
	reset(); hdmitx_hdr_transport_untrusted(&D);
	hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_AVI);
	hdmitx_hdr_transport_rebuilt(&D); assert(U);
	hdmitx_hdr_packet_clean(&D, HDMITX_HDR_PKT_AVI); assert(!U);
	/* Debug register access and generation overflow survive a rebuild. */
	reset(); hdmitx_hdr_debug_untrusted(&D);
	assert(U && hdmitx_same_mode_hw_gen.counter == 1);
	hdmitx_hdr_transport_rebuilt(&D); assert(U);
	reset(); D.hdr_bypass.transport_gen.counter = -5;
	hdmitx_hdr_transport_changed(&D); hdmitx_hdr_transport_rebuilt(&D); assert(U);
	/* Rebuild with nothing sticky never invents trust over dirty packets. */
	reset(); hdmitx_hdr_packet_untrusted(&D, HDMITX_HDR_PKT_VSIF);
	hdmitx_hdr_transport_rebuilt(&D); assert(U && !held);
	puts("PASS: compiled packet trust model (incl. transport rebuild)");
	return 0;
}
"""


def trust_model(source):
    statics = between(source, "static DEFINE_SPINLOCK(hdmitx_hdr_trust_lock);",
                      "static void hdmitx_hdr_set_sticky(")
    hw_gen = "static atomic_t hdmitx_same_mode_hw_gen;\n"
    names = ("hdmitx_hdr_set_sticky", "hdmitx_hdr_transport_changed", "hdmitx_hdr_link_changed",
             "hdmitx_same_mode_hw_changed", "hdmitx_hdr_mark_untrusted",
             "hdmitx_hdr_transport_untrusted", "hdmitx_hdr_debug_untrusted",
             "hdmitx_hdr_packet_untrusted", "hdmitx_hdr_packet_clean",
             "hdmitx_hdr_transport_rebuilt")
    return (TRUST_PRELUDE + statics + hw_gen +
            "\n".join(helpers.function(source, n) for n in names) + TRUST_BODY)


def run_trust(source):
    with tempfile.TemporaryDirectory() as d:
        c, exe = Path(d) / "t.c", Path(d) / "t"
        c.write_text(trust_model(source))
        cc = subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", "-Wno-unused-function",
                             "-o", str(exe), str(c)], capture_output=True, text=True)
        assert cc.returncode == 0, cc.stderr
        return subprocess.run([str(exe)], capture_output=True, text=True)


r = run_trust(main)
sys.stdout.write(r.stdout)
assert r.returncode == 0, r.stderr

if args.negative_controls:
    mutants = {
        "hw_gen ignored": ("(s->hw_gen != atomic_read(&hdmitx_same_mode_hw_gen)) << 5", "0 << 5"),
        "cd ignored": ("s->req_cd != para->cd ||", ""),
        "clock ignored": ("s->clock != info->video_clk) << 13", "0) << 13"),
        "link not checked on record": ("link != atomic_read(&hdev->hdr_bypass.link_gen) ||", ""),
        "record ignores hpd": ("(!hdev->hpd_state && !hdmitx_same_mode_plugin) ||", ""),
        "plugin modeset not recordable": ("(!hdev->hpd_state && !hdmitx_same_mode_plugin) ||",
                                          "!hdev->hpd_state ||"),
    }
    for name, (old, new) in mutants.items():
        assert old in main, name
        r = run(main.replace(old, new, 1))
        assert r.returncode != 0, f"mutant survived: {name}"
        print(f"negative control killed: {name}")
    trust_mutants = {
        "clean ignores sticky": ("!hdmitx_hdr_sticky_untrusted &&\n", "\n"),
        "clean clears every class": ("hdmitx_hdr_dirty_pkts &= ~pkt;", "hdmitx_hdr_dirty_pkts = 0;"),
        "packet untrust made silent": ("hdmitx_hdr_dirty_pkts |= pkts;", "(void)pkts;"),
        "rebuild ignores permanent": ("if (hdmitx_hdr_sticky_untrusted && !hdmitx_hdr_sticky_permanent) {",
                                      "if (hdmitx_hdr_sticky_untrusted) {"),
        "rebuild ignores dirty packets": ("if (!hdmitx_hdr_dirty_pkts &&\n\t\t    READ_ONCE(hdev->hdr_bypass.untrusted)) {",
                                          "if (READ_ONCE(hdev->hdr_bypass.untrusted)) {"),
        "rebuild is a no-op": ("\t\thdmitx_hdr_sticky_untrusted = false;\n\t\tif (!hdmitx_hdr_dirty_pkts",
                               "\t\tif (!hdmitx_hdr_dirty_pkts"),
        "debug untrust not permanent": ("\thdmitx_hdr_sticky_permanent = true;\n\tspin_unlock_irqrestore(&hdmitx_hdr_trust_lock, flags);\n\thdmitx_same_mode_hw_changed();",
                                        "\tspin_unlock_irqrestore(&hdmitx_hdr_trust_lock, flags);\n\thdmitx_same_mode_hw_changed();"),
        "packet bumps hw_gen": ("\thdmitx_hdr_mark_untrusted(hdev, __builtin_return_address(0),\n\t\tpkt",
                                "\thdmitx_same_mode_hw_changed();\n\thdmitx_hdr_mark_untrusted(hdev, __builtin_return_address(0),\n\t\tpkt"),
    }
    for name, (old, new) in trust_mutants.items():
        assert old in main, name
        r = run_trust(main.replace(old, new, 1))
        assert r.returncode != 0, f"mutant survived: {name}"
        print(f"negative control killed: {name}")
