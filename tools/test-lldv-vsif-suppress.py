#!/usr/bin/env python3
"""Check opt-in LLDV VSIF suppression and AVI 4K VIC declaration in the real
hdmitx packet writers, compiled against mocked registers.  Default-off output
must match the unmodified driver."""
import pathlib
import re
import subprocess
import sys
import tempfile


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"',
                    lambda m: " " * len(m[0]), source, flags=re.S)
    opening = masked.index("{", start)
    depth = 0
    for index in range(opening, len(masked)):
        depth += (masked[index] == "{") - (masked[index] == "}")
        if not depth:
            return source[start:index + 1]
    raise ValueError("Unterminated source function")


root = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
        else pathlib.Path(__file__).resolve().parents[1])
folder = root / "drivers/amlogic/media/vout/hdmitx/hdmi_tx_20"
writer = function((folder / "hw/hdmi_tx_hw.c").read_text(),
                  "static void hdmitx_set_packet(int type, unsigned char *DB, unsigned char *HB)\n")
hw_source = (folder / "hw/hdmi_tx_hw.c").read_text()
modern = (function(hw_source,
                   "static bool hdmitx_use_avi_4k_vic(unsigned char *DB, unsigned char *HB)\n")
          if "static bool hdmitx_use_avi_4k_vic" in hw_source else "")
data_writer = function(hw_source,
                       "static void hdmitx_set_datapacket(int type, unsigned char *DB,\n\tunsigned char *HB)\n")
main_source = (folder / "hdmi_tx_main.c").read_text()
start = main_source.index("/*ver0 and ver1_15")
opening = main_source.index("if (", start) + 4
closing = main_source.index(" {", opening)
condition = main_source[opening:closing].rstrip()
assert condition.endswith(")")
condition = condition[:-1]
video_source = (folder / "hdmi_tx_video.c").read_text()
fill_vsif = function(video_source,
                     "static void fill_hdmi4k_vsif_data(enum hdmi_vic vic, unsigned char *DB,\n")
construct_vsif = function(video_source,
                          "int hdmitx_construct_vsif(struct hdmitx_dev *hdev, enum vsif_type type,\n")
suppress = "static bool lldv_vsif_suppress" in hw_source
suppress_code = ""
if suppress:
    state = hw_source[hw_source.index("static bool lldv_vsif_suppress"):
                      hw_source.index("static bool hdmitx_vsif_en(void)")]
    ieee = hw_source[hw_source.index("#define GET_IEEEOUI()"):]
    ieee = ieee[:ieee.index("<< 16)") + len("<< 16)")]
    suppress_code = "\n".join([
        state, function(hw_source, "static bool hdmitx_vsif_en(void)\n"), ieee,
        function(hw_source, "enum hdmi_tf_type hdmitx_get_cur_dv_st(void)\n"),
        function(hw_source, "static bool hdmitx_is_lldv_vsif(unsigned char *DB, unsigned char *HB)\n"),
        function(hw_source, "static unsigned int hdmitx_lldv_avi_vic(struct hdmitx_dev *hdev)\n"),
        function(hw_source, "static bool hdmitx_lldv_vsif_take(unsigned char *DB, unsigned char *HB)\n")])
    tail_code = "\n".join([
        function(hw_source, "static void hdmitx_disable_packet(int type)\n"),
        function(hw_source, "static int lldv_vsif_suppress_set(const char *val,\n")])

registers = sorted(set(re.findall(r"\bHDMITX_DWC_[A-Z0-9_]+\b",
                                  writer + modern + data_writer + suppress_code +
                                  (tail_code if suppress else ""))) |
                   {"HDMITX_DWC_FC_AVICONF1", "HDMITX_DWC_FC_AVIVID"})
packets = sorted(set(re.findall(r"\bHDMI_PACKET_[A-Z0-9_]+\b", writer)) |
                 {"HDMI_AUDIO_INFO", "HDMI_SOURCE_DESCRIPTION"})
defines = "\n".join(
    [f"#define {name} {128 * (i + 1)}" for i, name in enumerate(registers)] +
    [f"#define {name} {i + 1}" for i, name in enumerate(packets)])
code = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define HF_IEEEOUI 0xc45dd8
#define HDMI_3840x2160p30_16x9 95
#define HDMI_3840x2160p25_16x9 94
#define HDMI_3840x2160p24_16x9 93
#define HDMI_4096x2160p24_256x135 98
#define HDMI_IEEEOUI 0x000c03
#define DOVI_IEEEOUI 0x00d046
#define HDR10PLUS_IEEEOUI 0x90848b
#define HDMITX_VIC_MASK 0xff
#define HDMITX_VESA_OFFSET 0x300
#define HDMITX_HDR_PKT_VSIF 1
#define HDMITX_HDR_PKT_AVI 2
#define T3D_FRAME_PACKING 0
#define HDMI_Unknown 0
#define HDMI_4k2k_30 HDMI_3840x2160p30_16x9
#define HDMI_4k2k_25 HDMI_3840x2160p25_16x9
#define HDMI_4k2k_24 HDMI_3840x2160p24_16x9
#define HDMI_4k2k_smpte_24 HDMI_4096x2160p24_256x135
#define GET_OUI_BYTE0(oui) (oui & 0xff)
#define GET_OUI_BYTE1(oui) ((oui >> 8) & 0xff)
#define GET_OUI_BYTE2(oui) ((oui >> 16) & 0xff)
enum hdmi_vic { HDMI_VIC_PLACEHOLDER };
enum vsif_type { VT_DEFAULT, VT_HDMI14_4K, VT_ALLM, VT_MAX };
enum { EOTF_T_SDR, EOTF_T_DOLBYVISION, EOTF_T_LL_MODE };
struct dv_info { int ver, length, low_latency; };
struct hdmitx_vidpara { unsigned int VIC; };
struct hdmitx_dev { unsigned int cur_VIC; struct hdmitx_vidpara *cur_video_param;
    int edid_spinlock;
    struct { void (*setdatapacket)(int, unsigned char *, unsigned char *); } hwop;
    struct { struct dv_info dv_info; unsigned int hf_ieeeoui, VIC_count, VIC[64];
} rxcap; };
enum hdmi_tf_type { HDMI_NONE = 0, HDMI_DV_VSIF_STD = 0x41, HDMI_DV_VSIF_LL = 0x42 };
struct kernel_param { void *arg; };
#define pr_info(...) ((void)0)
#define spin_lock_irqsave(l, f) ((void)(l), (f) = 0)
#define spin_unlock_irqrestore(l, f) ((void)(l), (void)(f))
static int __attribute__((unused)) param_set_bool(const char *val, const struct kernel_param *kp) {
    if (strcmp(val, "0") && strcmp(val, "1")) return -22;
    *(bool *)kp->arg = val[0] == '1'; return 0;
}
static struct hdmitx_dev device;
static unsigned int regs[32768], dirty, avivid_zero_writes;
static struct hdmitx_dev *get_hdmitx_device(void) { return &device; }
static unsigned int __attribute__((unused)) hdmitx_rd_reg(unsigned int r) { return regs[r]; }
static unsigned int __attribute__((unused)) hdmitx_get_bit(unsigned int r, int b) {
    return (regs[r] >> b) & 1;
}
static void hdmitx_wr_reg(unsigned int r, unsigned int v);
static void __attribute__((unused)) hdmitx_set_avi_vic(enum hdmi_vic v) {
    hdmitx_wr_reg(HDMITX_DWC_FC_AVIVID, v);
}
static bool is_hdmi14_4k(enum hdmi_vic v) {
    return v == 93 || v == 94 || v == 95 || v == 98;
}
static void hdmitx_wr_reg(unsigned int r, unsigned int v) {
    if (r == HDMITX_DWC_FC_AVIVID && !v) avivid_zero_writes++;
    regs[r] = v;
}
static void hdmitx_set_reg_bits(unsigned int r, unsigned int v, int p, int n) {
    unsigned int mask = ((1u << n) - 1) << p;
    regs[r] = (regs[r] & ~mask) | ((v << p) & mask);
}
static void hdmitx_hdr_packet_untrusted(struct hdmitx_dev *d, unsigned int m) {
    (void)d; dirty |= m;
}
static void hdmitx_hdr_packet_clean(struct hdmitx_dev *d, unsigned int m) {
    (void)d; dirty &= ~m;
}
static void hdmitx_hdr10_limit_packet(unsigned char *out, unsigned char *db,
                                    unsigned char *hb) {
    (void)hb; memcpy(out, db, 26);
}
"""
code = defines + "\n" + code + "\nstatic void hdmitx_set_packet(int, unsigned char *, unsigned char *);\n"
code += suppress_code + "\n" + modern + "\n" + writer + "\n" + data_writer
code += "\n" + fill_vsif + "\n" + construct_vsif + "\n"
code += "\nstatic int uses_legacy(int type, struct hdmitx_dev *hdev) { return (" + condition + "); }\n"
if suppress:
    code += tail_code + r"""
static void lldv_reset(unsigned int vic) {
    memset(regs, 0, sizeof(regs)); dirty = 0;
    device.cur_VIC = vic; device.cur_video_param = NULL;
    device.rxcap.hf_ieeeoui = HF_IEEEOUI; device.rxcap.VIC_count = 1;
    device.rxcap.VIC[0] = vic; regs[HDMITX_DWC_FC_AVIVID] = vic;
    regs[HDMITX_DWC_FC_AVICONF0] = 1; regs[HDMITX_DWC_FC_PACKET_TX_EN] = 0x10;
    lldv_vsif_suppress = true; lldv_vsif_suppressed = false; lldv_vsif_saved = false;
}
static int vsif_sent(void) {
    return (regs[HDMITX_DWC_FC_DATAUTO0] & 8) && regs[HDMITX_DWC_FC_VSDSIZE];
}
static void check(int ok, const char *name);
static void lldv_suppression_tests(void) {
    unsigned char dolby[27] = {0x46, 0xd0, 0, 3, 0x11, 0x22};
    unsigned char hf[27] = {0xd8, 0x5d, 0xc4, 1, 0};
    unsigned char h10p[27] = {0x8b, 0x84, 0x90, 1, 2};
    unsigned char hb[3] = {0x81, 1, 0x1b};
    struct kernel_param kp = {&lldv_vsif_suppress};

    lldv_reset(93);
    regs[HDMITX_DWC_FC_DATAUTO0] = 8; regs[HDMITX_DWC_FC_VSDSIZE] = 0x1b;
    regs[HDMITX_DWC_FC_VSDIEEEID0] = 0x46; regs[HDMITX_DWC_FC_VSDIEEEID1] = 0xd0;
    regs[HDMITX_DWC_FC_VSDPAYLOAD0] = 3;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(!vsif_sent() && !(regs[HDMITX_DWC_FC_DATAUTO0] & 8) &&
          !regs[HDMITX_DWC_FC_VSDIEEEID0] &&
          !regs[HDMITX_DWC_FC_VSDPAYLOAD0] && regs[HDMITX_DWC_FC_AVIVID] == 93,
          "suppressed LLDV clears stale Dolby auto-send and keeps AVI VIC 93");
    check(hdmitx_get_cur_dv_st() == HDMI_DV_VSIF_LL,
          "suppressed LLDV still reports DV-LL state for bookkeeping");
    check(dirty & HDMITX_HDR_PKT_VSIF, "suppressed LLDV keeps Dolby VSIF HDR-trust handling");
    lldv_vsif_suppress_set("0", &kp);
    check(vsif_sent() && regs[HDMITX_DWC_FC_VSDSIZE] == 0x1b &&
          regs[HDMITX_DWC_FC_VSDIEEEID0] == 0x46 && regs[HDMITX_DWC_FC_VSDPAYLOAD0] == 3 &&
          regs[HDMITX_DWC_FC_VSDPAYLOAD1] == 0x11 && regs[HDMITX_DWC_FC_AVIVID] == 93,
          "runtime off immediately restores the ordinary LLDV Dolby VSIF");
    check(hdmitx_get_cur_dv_st() == HDMI_DV_VSIF_LL, "restored LLDV reports DV-LL");
    lldv_vsif_suppress_set("1", &kp);
    check(!vsif_sent() && hdmitx_get_cur_dv_st() == HDMI_DV_VSIF_LL,
          "runtime on immediately suppresses the active LLDV VSIF");
    hdmitx_set_packet(HDMI_PACKET_VEND, NULL, NULL);
    check(!vsif_sent() && hdmitx_get_cur_dv_st() == HDMI_NONE,
          "DV exit clears the suppressed DV state");
    lldv_vsif_suppress_set("0", &kp);
    check(!vsif_sent(), "runtime off after DV exit does not resurrect a stale VSIF");

    lldv_reset(93); lldv_vsif_suppress = false;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(vsif_sent() && regs[HDMITX_DWC_FC_VSDIEEEID0] == 0x46,
          "suppression off keeps ordinary LLDV signaling");
    lldv_vsif_suppress_set("1", &kp);
    check(!vsif_sent() && hdmitx_get_cur_dv_st() == HDMI_DV_VSIF_LL,
          "runtime on applies to an already-sent LLDV VSIF");

    lldv_reset(93); dolby[3] = 2;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(vsif_sent() && regs[HDMITX_DWC_FC_VSDPAYLOAD0] == 2,
          "TV-led Dolby VSIF is never suppressed");
    dolby[3] = 1;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(vsif_sent() && regs[HDMITX_DWC_FC_VSDPAYLOAD0] == 1,
          "Dolby VSIF without the DV-signal bit is not suppressed");
    dolby[3] = 3;

    lldv_reset(93);
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    hdmitx_set_packet(HDMI_PACKET_VEND, h10p, hb);
    check(vsif_sent() && regs[HDMITX_DWC_FC_VSDIEEEID0] == 0x8b &&
          hdmitx_get_cur_dv_st() == HDMI_NONE,
          "HDR10+ VSIF is untouched and ends suppressed DV state");
    hb[2] = 5;
    hdmitx_set_packet(HDMI_PACKET_VEND, hf, hb);
    check(vsif_sent() && regs[HDMITX_DWC_FC_VSDIEEEID0] == 0xd8,
          "HDMI-Forum VSIF is untouched");
    hb[2] = 0x1b;

    lldv_reset(93); device.rxcap.hf_ieeeoui = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(vsif_sent(), "legacy HDMI 1.4 4K sink keeps the LLDV VSIF");
    lldv_reset(93); device.rxcap.VIC[0] = 94;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(vsif_sent(), "sink not advertising the CTA 4K VIC keeps the LLDV VSIF");
    lldv_reset(HDMITX_VESA_OFFSET + 16);
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(vsif_sent(), "VESA mode keeps the LLDV VSIF");
    lldv_reset(0);
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(vsif_sent(), "unknown mode keeps the LLDV VSIF");
    lldv_reset(16); device.rxcap.hf_ieeeoui = 0; regs[HDMITX_DWC_FC_AVIVID] = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(!vsif_sent() && regs[HDMITX_DWC_FC_AVIVID] == 16 &&
          (dirty & HDMITX_HDR_PKT_AVI),
          "non-legacy CTA timing is declared in AVI before suppression");

    lldv_reset(93);
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    hdmitx_disable_packet(HDMI_PACKET_VEND);
    check(hdmitx_get_cur_dv_st() == HDMI_NONE, "disable_packet ends suppressed DV state");
    lldv_reset(93);
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    hdmitx_set_datapacket(HDMI_PACKET_VEND, NULL, NULL);
    check(hdmitx_get_cur_dv_st() == HDMI_NONE, "generic writer ends suppressed DV state");
    regs[HDMITX_DWC_FC_AVICONF0] = 0;
    lldv_reset(93); regs[HDMITX_DWC_FC_AVICONF0] = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(hdmitx_get_cur_dv_st() == HDMI_DV_VSIF_STD,
          "suppressed RGB tunnel reports the same state as the unsuppressed readback");
    puts("PASS: opt-in LLDV Dolby VSIF suppression, runtime toggle, bookkeeping and guards");
}
"""
code += r"""
#define LLDV_TESTS() """ + ("lldv_suppression_tests()" if suppress else "((void)0)") + r"""
static void check(int ok, const char *name) {
    if (!ok) { fprintf(stderr, "FAIL: %s\n", name); exit(1); }
}
static void default_off_tests(void) {
    unsigned char legacy[27] = {3, 12, 0, 0x20, 3};
    unsigned char dolby[27] = {0x46, 0xd0, 0, 3};
    unsigned char hb[3] = {0x81, 1, 5};

    check(!lldv_vsif_suppress, "suppression defaults off");
    device.cur_VIC = 93; device.rxcap.hf_ieeeoui = HF_IEEEOUI;
    device.rxcap.VIC_count = 1; device.rxcap.VIC[0] = 93;
    regs[HDMITX_DWC_FC_AVIVID] = 93; dirty = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 && regs[HDMITX_DWC_FC_VSDSIZE] == 5 &&
          (regs[HDMITX_DWC_FC_DATAUTO0] & 8),
          "off: modern sink keeps upstream HDMI 1.4 4K VSIF");
    regs[HDMITX_DWC_FC_AVIVID] = 93;
    hdmitx_set_datapacket(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 && regs[HDMITX_DWC_FC_VSDSIZE] == 5,
          "off: generic writer keeps upstream HDMI 1.4 4K VSIF");
    hb[2] = 27; dirty = 0; regs[HDMITX_DWC_FC_AVICONF0] = 1;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 && !(dirty & HDMITX_HDR_PKT_AVI) &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 27 && regs[HDMITX_DWC_FC_VSDIEEEID0] == 0x46 &&
          (regs[HDMITX_DWC_FC_DATAUTO0] & 8),
          "off: LLDV sends Dolby VSIF without AVI VIC repair");
    check(hdmitx_get_cur_dv_st() == HDMI_DV_VSIF_LL, "off: LLDV reports DV-LL");
    check(uses_legacy(EOTF_T_LL_MODE, &device),
          "off: forced LLDV keeps upstream legacy fallback on non-DV EDID");
    check(uses_legacy(EOTF_T_SDR, &device) && uses_legacy(EOTF_T_DOLBYVISION, &device),
          "off: other legacy fallbacks unchanged");
    device.hwop.setdatapacket = hdmitx_set_datapacket;
    regs[HDMITX_DWC_FC_AVIVID] = 93; avivid_zero_writes = 0;
    hdmitx_construct_vsif(&device, VT_HDMI14_4K, 1, NULL);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 && regs[HDMITX_DWC_FC_VSDSIZE] == 5 &&
          regs[HDMITX_DWC_FC_VSDPAYLOAD0] == 0x20 && regs[HDMITX_DWC_FC_VSDPAYLOAD0 + 1] == 3,
          "off: construct_vsif keeps upstream HDMI-VIC signaling");
    memset(regs, 0, sizeof(regs)); memset(&device, 0, sizeof(device));
    dirty = 0; avivid_zero_writes = 0;
    lldv_vsif_suppressed = false; lldv_vsif_saved = false;
    puts("PASS: default-off upstream behaviour");
}
int main(void) {
    unsigned char legacy[27] = {3, 12, 0, 0x20, 3};
    unsigned char dolby[27] = {0x46, 0xd0, 0, 3};
    unsigned char hb[3] = {0x81, 1, 5};
    default_off_tests();
    lldv_vsif_suppress = true;
    device.cur_VIC = 93;
    regs[HDMITX_DWC_FC_AVICONF1] = 0xe8;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0, "SDR retains HDMI-4K VIC encoding");
    hb[2] = 27;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 93, "LLDV restores removed HDMI-4K timing in AVI");
    check(regs[HDMITX_DWC_FC_AVICONF1] == 0xe8, "LLDV VIC repair preserves colorimetry");
    check((dirty & HDMITX_HDR_PKT_AVI) != 0, "AVI repair participates in HDR trust tracking");
    check(!uses_legacy(EOTF_T_LL_MODE, &device), "forced LLDV skips legacy fallback on non-DV EDID");
    check(uses_legacy(EOTF_T_SDR, &device), "SDR legacy fallback unchanged");
    check(uses_legacy(EOTF_T_DOLBYVISION, &device), "standard DV legacy fallback unchanged");
    hb[2] = 5;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0, "Stop retains valid SDR HDMI-4K encoding");
    hb[2] = 27; dolby[3] = 2; dirty = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 && !(dirty & HDMITX_HDR_PKT_AVI),
          "standard TV-led DV is not rewritten");
    dolby[3] = 1;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0, "inactive Dolby signal is not rewritten");
    dolby[3] = 3; device.cur_VIC = 0; dirty = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 && !(dirty & HDMITX_HDR_PKT_AVI),
          "unknown mode is not fabricated");
    device.cur_VIC = 93; regs[HDMITX_DWC_FC_AVIVID] = 16; dirty = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 16 && !(dirty & HDMITX_HDR_PKT_AVI),
          "already programmed AVI VIC is preserved");
    device.cur_VIC = 93; device.rxcap.hf_ieeeoui = HF_IEEEOUI;
    device.rxcap.VIC_count = 1; device.rxcap.VIC[0] = 93;
    regs[HDMITX_DWC_FC_AVIVID] = 0; hb[2] = 5; dirty = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 93 &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 0 &&
          !(regs[HDMITX_DWC_FC_DATAUTO0] & 8),
          "modern sink keeps explicit AVI timing without legacy VSIF");
    check(dirty & HDMITX_HDR_PKT_AVI, "modern timing repair uses HDR trust");
    regs[HDMITX_DWC_FC_AVIVID] = 0;
    hdmitx_set_datapacket(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 93 &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 0,
          "generic legacy packet path uses the same modern declaration");
    hb[2] = 24;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 24,
          "standard legacy Dolby Vision VSIF remains intact");
    hb[2] = 5; device.rxcap.VIC[0] = 94;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 5,
          "modern sink without the explicit CTA VIC retains legacy signaling");
    device.rxcap.VIC[0] = 93; device.rxcap.hf_ieeeoui = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 5,
          "HDMI 1.4 sink preserves legacy 4K timing");
    device.rxcap.hf_ieeeoui = HF_IEEEOUI;
    device.cur_VIC = 16; device.rxcap.VIC[0] = 16;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0,
          "inconsistent HDMI-VIC and current timing are not fabricated");
    device.cur_VIC = 95; device.rxcap.VIC[0] = 95; legacy[4] = 1;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 95 &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 0,
          "other explicitly advertised legacy 4K timings normalize correctly");
    device.cur_VIC = HDMITX_VESA_OFFSET + 93;
    device.rxcap.VIC[0] = 93; legacy[4] = 3;
    regs[HDMITX_DWC_FC_AVIVID] = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0,
          "VESA mode is not mistaken for a CTA VIC sharing its low bits");
    hb[2] = 27; dolby[3] = 3; dirty = 0;
    hdmitx_set_packet(HDMI_PACKET_VEND, dolby, hb);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 && !(dirty & HDMITX_HDR_PKT_AVI),
          "LLDV repair does not fabricate timing for a VESA mode");
    {
        struct hdmitx_vidpara param = {93};
        legacy[4] = 3; hb[2] = 5;
        device.rxcap.VIC[0] = 93; device.rxcap.hf_ieeeoui = HF_IEEEOUI;
        device.cur_VIC = HDMI_Unknown; device.cur_video_param = &param;
        regs[HDMITX_DWC_FC_AVIVID] = 93; regs[HDMITX_DWC_FC_VSDSIZE] = 0;
        avivid_zero_writes = 0;
        hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
        check(regs[HDMITX_DWC_FC_AVIVID] == 93 &&
              regs[HDMITX_DWC_FC_VSDSIZE] == 0 && !avivid_zero_writes,
              "first SDR packet of a full modeset uses the modern declaration");
        regs[HDMITX_DWC_FC_AVIVID] = 16;
        hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
        check(regs[HDMITX_DWC_FC_AVIVID] == 0 &&
              regs[HDMITX_DWC_FC_VSDSIZE] == 5,
              "modeset fallback requires the configured AVI VIC to match");
        device.cur_video_param = NULL; regs[HDMITX_DWC_FC_AVIVID] = 93;
        hdmitx_set_packet(HDMI_PACKET_VEND, legacy, hb);
        check(regs[HDMITX_DWC_FC_AVIVID] == 0,
              "unknown mode without video parameters is not fabricated");
    }
    device.hwop.setdatapacket = hdmitx_set_datapacket;
    device.cur_VIC = 93; device.rxcap.hf_ieeeoui = HF_IEEEOUI;
    regs[HDMITX_DWC_FC_AVIVID] = 93; avivid_zero_writes = 0;
    hdmitx_construct_vsif(&device, VT_HDMI14_4K, 1, NULL);
    check(regs[HDMITX_DWC_FC_AVIVID] == 93 && !avivid_zero_writes &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 0,
          "construct_vsif has no transient AVI VIC 0 on a modern sink");
    device.rxcap.hf_ieeeoui = 0;
    hdmitx_construct_vsif(&device, VT_HDMI14_4K, 1, NULL);
    check(regs[HDMITX_DWC_FC_AVIVID] == 0 &&
          regs[HDMITX_DWC_FC_VSDSIZE] == 5 &&
          regs[HDMITX_DWC_FC_VSDPAYLOAD1] == 3,
          "construct_vsif keeps legacy HDMI-VIC signaling for HDMI 1.4 sinks");
    device.cur_VIC = 16; regs[HDMITX_DWC_FC_AVIVID] = 16;
    hdmitx_construct_vsif(&device, VT_HDMI14_4K, 1, NULL);
    check(regs[HDMITX_DWC_FC_AVIVID] == 16,
          "construct_vsif leaves non-4K AVI VIC untouched");
    LLDV_TESTS();
    puts("PASS: LLDV repair, both modern AVI paths, modeset/construct_vsif consistency, legacy/TV-led preservation, and HDR trust");
    return 0;
}
"""
with tempfile.TemporaryDirectory(prefix="lldv-vsif-test-") as temp:
    path = pathlib.Path(temp)
    (path / "test.c").write_text(code)
    subprocess.run(["cc", "-std=gnu89", "-Wall", "-Werror",
                    str(path / "test.c"), "-o", str(path / "test")], check=True)
    subprocess.run([str(path / "test")], check=True)
