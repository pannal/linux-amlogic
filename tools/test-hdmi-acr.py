#!/usr/bin/env python3
"""Run production ACR tables, lookup and caller with register service stand-ins.

No kernel/module build or physical HDMI/automatic-CTS acceptance is implied.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
# The script is installed alongside this existing source-extraction harness.
spec = importlib.util.spec_from_file_location('pcm_test', ROOT / 'tools/test-pcm-native-layouts.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
header = (ROOT / 'include/linux/amlogic/media/vout/hdmi_tx/hdmi_common.h').read_text()
tables = (ROOT / 'drivers/amlogic/media/vout/hdmitx/hdmi_common/hdmi_parameters.c').read_text()
hw = (ROOT / 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hw/hdmi_tx_hw.c').read_text()

prelude = r'''
#include <assert.h>
#include <stdio.h>
#define pr_info(...) ((void)0)
#define HW ""
#define AUDIO_PARA_MAX_NUM 14
enum { HDMITX_DWC_AUD_INPUTCLKFS, HDMITX_DWC_AUD_N3, HDMITX_DWC_AUD_CTS3,
       HDMITX_DWC_AUD_CTS2, HDMITX_DWC_AUD_CTS1, HDMITX_DWC_AUD_N2, HDMITX_DWC_AUD_N1 };
static unsigned int regs[7], writes;
static void hdmitx_wr_reg(unsigned int reg, unsigned int value) {
 assert(reg < 7); regs[reg]=value; ++writes;
}
struct hdmi_format_para {
 struct { unsigned int frac_freq, pixel_freq; } timing;
 enum hdmi_color_space cs;
 enum hdmi_color_depth cd;
};
struct hdmitx_dev { int chip_type, tx_aud_src, frac_rate_policy; struct hdmi_format_para *para; };
struct hdmitx_audpara { enum hdmi_audio_type type; enum hdmi_audio_fs sample_rate; };
'''
enums = '\n'.join(helpers.declaration(header, 'enum', name) for name in (
    'hdmi_color_space', 'hdmi_color_depth', 'hdmi_audio_type', 'hdmi_audio_fs'))
begin = tables.index('struct hdmi_audio_fs_ncts aud_32k_para =')
lookup = helpers.function(tables, 'hdmi_get_aud_n_paras')
definitions = tables[begin:tables.index(lookup)]
caller = helpers.function(hw, 'set_aud_acr_pkt')

main = r'''
static void check(int chip, int mode120, int fractional, enum hdmi_color_space cs,
 enum hdmi_color_depth depth, enum hdmi_audio_fs rate, unsigned int expected,
 int source, enum hdmi_audio_type type) {
 struct hdmi_format_para p={{mode120 ? 296703 : 148352,mode120 ? 297000 : 148500},cs,depth};
 struct hdmitx_dev d={chip,source,fractional,&p};
 struct hdmitx_audpara a={type,rate};
 unsigned int n;
 for(n=0;n<7;n++) regs[n]=0xff;
 writes=0;
 set_aud_acr_pkt(&d,&a);
 n=((regs[HDMITX_DWC_AUD_N3]&15)<<16)|(regs[HDMITX_DWC_AUD_N2]<<8)|regs[HDMITX_DWC_AUD_N1];
 assert(n==expected);
 assert(regs[HDMITX_DWC_AUD_N3]==(0x80|(expected>>16)));
 assert(regs[HDMITX_DWC_AUD_CTS3]==0 && regs[HDMITX_DWC_AUD_CTS2]==0 && regs[HDMITX_DWC_AUD_CTS1]==0);
 assert(regs[HDMITX_DWC_AUD_INPUTCLKFS]==((type==CT_MAT || type==CT_DTS_HD_MA) ? 2 : source ? 4 : 0));
 assert(writes==((type==CT_MAT || type==CT_DTS_HD_MA) ? 9 : 8));
}
int main(void) {
 unsigned int mode,frac,source,type,i,depth,cs,cases=0;
 const enum hdmi_audio_fs rates[]={FS_32K,FS_44K1,FS_48K,FS_88K2,FS_96K,FS_176K4,FS_192K,FS_768K};
 const enum hdmi_audio_type types[]={CT_PCM,CT_MAT,CT_DTS_HD_MA};
 const enum hdmi_color_depth depths[]={COLORDEPTH_24B,COLORDEPTH_30B,COLORDEPTH_36B,COLORDEPTH_48B};
 const enum hdmi_color_space spaces[]={COLORSPACE_YUV422,COLORSPACE_RGB444};
 /* Expected ordinary-depth base values, independent of the production tables. */
 const unsigned int base[2][2][3]={{{4096,6272,6144},{11648,8918,5824}},
                                    {{3072,4704,5120},{5824,4459,5824}}};
 for(mode=0;mode<2;mode++) for(frac=0;frac<2;frac++)
 for(source=0;source<2;source++) for(type=0;type<3;type++)
 for(depth=0;depth<4;depth++) for(cs=0;cs<2;cs++) for(i=0;i<8;i++) {
  unsigned int family=i==0 ? 0 : (i==1 || i==3 || i==5) ? 1 : 2;
  unsigned int multiplier=(i==3 || i==4) ? 2 : (i==5 || i==6) ? 4 : 1;
  unsigned int n=base[mode][frac][family];
  if(cs==1 && depth>=2 && mode) n=family==0 ? 4096 : family==1 ? 6272 : 6144;
  if(cs==1 && depth==2 && !mode && frac && family==1) n=17836;
  if(cs==1 && depth==2 && !mode && frac && family==2) n=11648;
  unsigned int expected=n*multiplier;
  check(MESON_CPU_ID_G12A,mode,frac,spaces[cs],depths[depth],rates[i],expected,source,types[type]);
  if(mode && frac && source && types[type]==CT_PCM && rates[i]==FS_44K1 &&
     (spaces[cs]==COLORSPACE_YUV422 || depths[depth]<=COLORDEPTH_30B))
   expected=4460;
  check(MESON_CPU_ID_G12B,mode,frac,spaces[cs],depths[depth],rates[i],expected,source,types[type]);
  cases+=2;
 }
 assert(hdmi_get_aud_n_paras(FS_44K1,COLORDEPTH_24B,123456)==6272);
 assert(hdmi_get_aud_n_paras(FS_88K2,COLORDEPTH_24B,123456)==12544);
 assert(hdmi_get_aud_n_paras(FS_768K,COLORDEPTH_24B,123456)==6144);
 assert(aud_44k1_para.array[11].n==4704 && aud_44k1_para.array[11].cts==247500);
 assert(297000ULL*1000*4704==44100ULL*128*247500);
 printf("PASS %u production caller/lookup cases, automatic CTS and source/HBR controls\n",cases);
}
'''
import re
flag = re.search(r'^#define DTS_HD_MA[^\n]*', header, re.M)
assert flag
chip_header = (ROOT / 'include/linux/amlogic/media/vout/hdmi_tx/hdmi_tx_module.h').read_text()
chip_ids = re.findall(r'^#define MESON_CPU_ID_G12[AB]\s+\d+', chip_header, re.M)
assert len(chip_ids) == 2
program = '\n'.join(chip_ids) + '\n' + flag[0] + '\n' + enums + prelude + helpers.declaration(header, 'struct', 'hdmi_audio_fs_ncts')
program += definitions + lookup + caller + main
with tempfile.TemporaryDirectory() as temporary:
    source = Path(temporary) / 'acr.c'
    binary = Path(temporary) / 'acr'
    source.write_text(program)
    subprocess.run(['cc', '-std=gnu99', '-Wall', '-Werror', str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
