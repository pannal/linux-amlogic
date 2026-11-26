#!/usr/bin/env python3
"""Exercise the HDMI TX 2.0 modeset colour policy with synthetic receiver EDIDs."""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
root = parser.parse_args().source_root
tx = root / 'drivers/amlogic/media/vout/hdmitx'
main = (tx / 'hdmi_tx_20/hdmi_tx_main.c').read_text()
edid = (tx / 'hdmi_tx_20/hdmi_tx_edid.c').read_text()
inc = root / 'include/linux/amlogic/media/vout'
common = (inc / 'hdmi_tx/hdmi_common.h').read_text()
vinfo = (inc / 'vinfo.h').read_text()


def section(source, start, end):
    return source[source.index(start):source.index(end, source.index(start))]


# Only the VIC definitions and pixel formats are used by the policy.
types = section(common, '#define HDMITX_VIC420_OFFSET', '/* Refer to') + section(common, 'enum hdmi_vic {', 'struct hdmi_cea_timing')
types += section(common, 'enum hdmi_color_depth {', 'enum hdmi_3d_type')
types += section(vinfo, 'enum eotf_type {', '};') + '};\n' + section(vinfo, 'enum mode_type {', '};') + '};\n'
helpers = section(main, 'static bool hdmitx_native_dv_tunnel(', 'static  int  set_disp_mode(') if 'static bool hdmitx_native_dv_tunnel(' in main else ''
# Run the production code from parsed mode/attrs to the final VIC passed to hardware.
modeset = section(main, 'pr_info("set_disp_mode_auto - eotf', 'hdmitx_pre_display_init();')
parse420 = section(edid, 'static bool Y420VicRight(', 'static int _edid_parsedrmsb(')

fixture = r"""
#define BIT(n) (1U << (n))
#define VIC_MAX_NUM 32
#define pr_info(...) ((void)0)
#define HF_IEEEOUI 0xc45dd8
struct dv_info { int ver, sup_10b_12b_444; };
struct rx_cap {
	unsigned int VIC[VIC_MAX_NUM], VIC_count, native_Mode, Max_TMDS_Clock1, Max_TMDS_Clock2;
	int dc_30bit_420, dc_36bit_420, ColorDeepSupport, hf_ieeeoui;
	struct dv_info dv_info;
};
struct hdmi_format_para { enum hdmi_vic vic; enum hdmi_color_space cs;
	enum hdmi_color_depth cd; int cr; unsigned int tmds_clk; };
struct hdmitx_dev { struct rx_cap rxcap; struct hdmi_format_para *para; char fmt_attr[16];
	int edid_parsing, hdmi_current_eotf_type, hdmi_current_tunnel_mode, flag_3dfp; };
static int xbmc_dv_non_ipt, xbmc_aml_linux_force_422, xbmc_dv_vp;
static struct hdmi_format_para formats[512];
static struct hdmi_format_para *hdmi_get_fmt_paras(enum hdmi_vic vic) { return &formats[vic]; }
"""
cases = r"""
static void check(struct hdmitx_dev *hdev, enum hdmi_vic vic, enum hdmi_color_space request,
		  enum hdmi_color_depth depth, enum hdmi_vic output,
		  enum hdmi_color_space chroma, enum hdmi_color_depth bpc)
{
	/* hdmi_get_fmt_name prefix-matches the base record, even for a 420 alias. */
	struct hdmi_format_para *para = &formats[vic & HDMITX_VIC_MASK];
	struct {const char *name;} timing = {.name = "smpte60hz"}, *info = &timing;
	para->cs = request;
	para->cd = depth;
	hdev->para = para;
	MODESET
	assert(vic == output);
	assert(hdev->para->vic == output);
	assert(hdev->para->cs == chroma);
	assert(hdev->para->cd == bpc);
}
int main(void)
{
	const enum hdmi_vic hd = HDMI_1080p60, base = HDMI_4096x2160p60_256x135;
	const enum hdmi_vic only = HDMI_4096x2160p60_256x135_Y420;
	const enum hdmi_color_space y422 = COLORSPACE_YUV422, y420 = COLORSPACE_YUV420;
	const enum hdmi_color_depth b8 = COLORDEPTH_24B, b10 = COLORDEPTH_30B, b12 = COLORDEPTH_36B;
	unsigned char y420vdb[] = {0xe2, 0x0e, base};
	unsigned char multi[] = {0xe3, 0x0e, HDMI_3840x2160p60_16x9, base};
	struct rx_cap mixed = {.VIC = {HDMI_3840x2160p60_16x9}, .VIC_count = 1};
	struct hdmitx_dev d = {.edid_parsing = 1, .fmt_attr = ",422,12bit",
		.rxcap = {.VIC = {hd}, .VIC_count = 1, .native_Mode = BIT(4), .Max_TMDS_Clock2 = 120}};
	struct rx_cap *rx = &d.rxcap;
	formats[base].vic = base; formats[base].tmds_clk = 594000;
	formats[only].vic = only; formats[only].tmds_clk = 594000;
	formats[hd].vic = hd; formats[hd].tmds_clk = 148500;
	assert(!Edid_ParsingY420VDBBlock(rx, y420vdb));
	check(&d, base, y422, b12, only, y420, b8);
	check(&d, hd, y422, b12, hd, y422, b12);
	rx->dc_30bit_420 = 1;
	check(&d, base, y422, b12, only, y420, b10);
	rx->dc_36bit_420 = 1;
	check(&d, base, y422, b12, only, y420, b12);
	strcpy(d.fmt_attr, ",420,8bit");
	check(&d, only, y420, b8, only, y420, b8);
	strcpy(d.fmt_attr, ",422,12bit");
	rx->Max_TMDS_Clock2 = 89; /* 445 MHz falls short of 445.5 MHz. */
	check(&d, base, y422, b12, only, y420, b10);
	rx->Max_TMDS_Clock2 = 0; rx->Max_TMDS_Clock1 = 60;
	check(&d, base, y422, b12, only, y420, b8);
	/* Both native DV tunnels retain their transport. VS10 HDR does not tunnel. */
	d.hdmi_current_eotf_type = EOTF_T_DOLBYVISION;
	d.hdmi_current_tunnel_mode = RGB_8BIT;
	check(&d, base, y422, b12, base, COLORSPACE_YUV444, b8);
	d.hdmi_current_eotf_type = EOTF_T_LL_MODE;
	d.hdmi_current_tunnel_mode = YUV422_BIT12;
	check(&d, base, y422, b12, base, y422, b12);
	xbmc_dv_non_ipt = 1; xbmc_aml_linux_force_422 = 1;
	check(&d, base, y422, b12, only, y420, b8);
	d.hdmi_current_eotf_type = EOTF_T_NULL;
	xbmc_dv_non_ipt = 0; xbmc_aml_linux_force_422 = 0;
	/* On reconnect the new sink advertises both forms of this same timing. */
	rx->VIC[rx->VIC_count++] = base;
	rx->Max_TMDS_Clock2 = 120;
	check(&d, only, y420, b12, base, y422, b12);
	check(&d, base, y422, b12, base, y422, b12);
	xbmc_aml_linux_force_422 = 1;
	check(&d, only, y420, b12, base, y422, b12);
	xbmc_aml_linux_force_422 = 0;
	strcpy(d.fmt_attr, ",422,");
	check(&d, base, y422, b8, base, y422, b8);
	strcpy(d.fmt_attr, ",422,12bit");
	rx->native_Mode = BIT(5);
	check(&d, base, y422, b12, only, y420, b12);
	check(&d, hd, y422, b12, hd, COLORSPACE_RGB444, b8);
	rx->VIC[0] = hd; rx->VIC[1] = base; rx->VIC_count = 2;
	rx->native_Mode = BIT(4);
	check(&d, only, y420, b12, base, y422, b12);
	assert(!strcmp(d.fmt_attr, ",422,12bit"));
	/* A duplicate converted in the first position must not hide the next VIC. */
	assert(!Edid_ParsingY420VDBBlock(&mixed, multi));
	assert(mixed.VIC_count == 2 && mixed.VIC[1] == only);
	return 0;
}
"""
source = '#include <stdbool.h>\n#include <assert.h>\n#include <string.h>\n'
source += types + fixture + parse420 + helpers + cases.replace('MODESET', modeset)
with tempfile.TemporaryDirectory() as tmp:
    exe = Path(tmp) / 'test'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu99', '-Wall', '-x', 'c', '-', '-o',
                    str(exe)], input=source, text=True, check=True)
    subprocess.run([str(exe)], cwd=tmp, check=True)
print('PASS: per-timing HDMI chroma and depth, bandwidth, reconnect and native DV controls')
