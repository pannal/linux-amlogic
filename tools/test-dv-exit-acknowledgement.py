#!/usr/bin/env python3
"""Production fake-SDR/BYPASS exit and DV packet-delay branches with modeled I/O.

A deterministic sysfs observer polls at each toggle setter return and IRQ return.
Hardware/core writes, metadata policy and IRQ admission are stand-ins. This tests
premature acknowledgement and preserved control flow, not wire/TV acceptance.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1] /
                    'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c')
parser.add_argument('--negative-controls', action='store_true')
a = parser.parse_args()
source = a.source.read_text()


def function(text, signature):
    start = text.index(signature)
    brace = text.index('{', start)
    depth, end = 1, brace + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


def harness(text):
    process = function(text, 'static int dv_process_internal(')
    start = process.index('if ((!vf && video_turn_off) || (video_status == -1))')
    end = process.index('if ((dolby_vision_flags & FLAG_CERTIFICAION) ||', start)
    exit_path = process[start:end]
    packet = function(text, 'static void send_hdmi_pkt\n')
    packet = function(packet, '} else if (last_dst_format == FORMAT_DOVI) {')[1:]
    toggle = function(text, 'void dolby_vision_set_toggle_flag(int flag)')
    toggle = toggle.replace('void dolby_vision_set_toggle_flag(', 'void production_toggle(')
    defines = []
    for name in ['MAX_TRANSITION_DELAY', 'FLAG_VSYNC_CNT', 'FLAG_TOGGLE_FRAME', 'FLAG_FORCE_HDMI_PKT', 'BYPASS_PROCESS']:
        defines.append(next(line for line in text.splitlines() if line.startswith('#define ' + name)))
    declaration = ('bool fake_sdr_bypass = false;' if 'bool fake_sdr_bypass = false;' in process else '')
    return '\n'.join(defines) + PRE + toggle + MIDDLE.replace('@EXIT@', exit_path).replace('@DECL@', declaration).replace('@PACKET@', packet) + MAIN


PRE = r'''
#include <cassert>
#include <cstdio>
#include <cstring>
#define DOLBY_VISION_OUTPUT_MODE_BYPASS 5
#define DOLBY_VISION_FOLLOW_SOURCE 0
#define FLAG_MUTE 2
#define FORMAT_DOVI 1
#define FORMAT_SDR 0
#define FORMAT_HDR10PLUS 2
#define FORMAT_HLG 3
#define FORMAT_CUVA 4
#define VIDEO_MUTE_ON_DV 1
#define VIDEO_MUTE_OFF 0
#define pr_dolby_dbg(...) ((void)0)
struct VF { int format; };
struct Device { void* dv_info = (void*)1; void(*fresh_tx_vsif_pkt)(int,int,void*,bool); };
struct Info { Device* vout_device; };
static unsigned dolby_vision_flags = FLAG_TOGGLE_FRAME;
static int dolby_vision_policy = 0, dolby_vision_target_mode = 1, dolby_vision_mode = 1;
static int dolby_vision_status = 1, last_dst_format = FORMAT_DOVI;
static int sdr_delay = 0, sdr_transition_delay = 0;
static bool dolby_vision_wait_on = true, dolby_vision_wait_init = true;
static bool dolby_vision_core1_on = false;
static int dolby_vision_src_format = FORMAT_DOVI, vsync_count = 0;
static int dovi_setting_video_flag = 0, dolby_vision_mask = 7, pps_state = 0;
static struct { int video_width = 3840, video_height = 2160; } dovi_setting;
static int first_packets = 0, sdr_packets = 0, disable_calls = 0, metadata_calls = 0;
static int irq_calls = 0, first_packet_irq = -1, sdr_packet_irq = -1;
static bool user_enabled = true, core_enabled = true, premature_ack = false;
static bool observe_setter = true, acknowledged = false;
static void observe_sysfs() {
  if (!acknowledged && !(dolby_vision_flags & FLAG_TOGGLE_FRAME)) {
    acknowledged = true;
    premature_ack = core_enabled;
    // aml_dv_toggle_frame returns; aml_dv_off writes enable=N. Subsequent
    // video.c DV processing is gated by is_dolby_vision_enable().
    user_enabled = false;
  }
}
'''
MIDDLE = r'''
static void dolby_vision_set_toggle_flag(int value) {
  production_toggle(value);
  if (observe_setter) observe_sysfs();
}
static bool dolby_vision_policy_process(unsigned* mode, int, VF*) {
  if (dolby_vision_policy == DOLBY_VISION_FOLLOW_SOURCE && *mode != 5) {
    *mode = 5; return true;
  }
  return false;
}
static int dv_parse_metadata_internal(VF*, int, bool, bool) { ++metadata_calls; return 1; }
static bool is_hlg_frame(VF* vf) { return vf->format == FORMAT_HLG; }
static bool is_hdr10plus_frame(VF* vf) { return vf->format == FORMAT_HDR10PLUS; }
static bool is_cuva_frame(VF* vf) { return vf->format == FORMAT_CUVA; }
static int get_video_mute() { return 0; }
static void apply_stb_core_settings(int,int,int,int,int) {}
static void enable_dolby_vision(int value) {
  assert(value == 0); core_enabled = false; dolby_vision_status = BYPASS_PROCESS; ++disable_calls;
}
static void vsif(int,int,void*,bool sdr) {
  if (sdr) { ++sdr_packets; sdr_packet_irq = irq_calls; }
  else { ++first_packets; first_packet_irq = irq_calls; }
}
static void send_hdmi_pkt(int,int dst_format,Info* vinfo,VF* vf) {
  // This fixture extracts the complete DOVI-to-other packet transition only.
  if (false) {}
  @PACKET@
}
static int process(Info* vinfo, VF* vf, int video_status) {
  ++irq_calls;
  bool video_turn_off = true;
  unsigned mode = dolby_vision_mode;
  @DECL@
  @EXIT@
  return 0;
}
'''
MAIN = r'''
int main(int argc, char** argv) {
  assert(argc == 2);
  Device device; device.fresh_tx_vsif_pkt = vsif; Info info{&device};
  Info* vinfo = &info; VF* vf = nullptr; VF frame{FORMAT_HDR10PLUS};
  int video_status = 0;
  const bool normal = !strcmp(argv[1], "normal");
  const bool core_off = !strcmp(argv[1], "follow-source");
  const bool core_on = !strcmp(argv[1], "video-off");
  const bool absent = !strcmp(argv[1], "no-vinfo");
  const bool idle = !strcmp(argv[1], "already-bypass");
  const bool always = !strcmp(argv[1], "always-on");
  const bool hdr = !strcmp(argv[1], "hdr10plus-next");
  const bool restart = !strcmp(argv[1], "interrupt-exit");
  assert(normal || core_off || core_on || absent || idle || always || hdr || restart);
  if (normal) observe_setter = false;
  if (core_on) { dolby_vision_core1_on = true; video_status = -1; }
  if (absent) vinfo = nullptr;
  if (idle) { dolby_vision_mode = 5; dolby_vision_status = BYPASS_PROCESS; core_enabled = false; }
  if (always) dolby_vision_policy = 2;
  if (hdr) { vf = &frame; video_status = -1; }
  process(vinfo, vf, video_status); observe_sysfs();
  if (!idle && !always) assert(metadata_calls == (core_on ? 1 : 0));
  if (always) {
    assert(core_enabled && !acknowledged && sdr_delay == 0 && first_packets == 0);
  } else if (restart) {
    assert(sdr_delay == 1 && !acknowledged);
    dolby_vision_mode = 1; dolby_vision_policy = 2;
    process(vinfo, vf, video_status); observe_sysfs();
    assert(sdr_delay == 0 && core_enabled && !acknowledged);
  } else {
    for (int i = 0; i < 15 && user_enabled; ++i) {
      process(vinfo, vf, video_status); observe_sysfs();
    }
    assert(!premature_ack && acknowledged && !core_enabled && sdr_delay == 0);
    if (absent || idle || hdr) {
      assert(irq_calls == 1 && disable_calls == (idle ? 0 : 1));
      if (hdr) assert(sdr_packets == 1 && first_packets == 0);
    } else {
      assert(disable_calls == 1 && first_packets == 1 && sdr_packets == 1);
      assert(sdr_packet_irq - first_packet_irq == MAX_TRANSITION_DELAY);
      assert(irq_calls == MAX_TRANSITION_DELAY + 1);
    }
  }
  printf("PASS %s: IRQs=%d exit_packets=%d SDR_packets=%d core_disable=%d\n",
         argv[1], irq_calls, first_packets, sdr_packets, disable_calls);
}
'''


def run(text, cases, failure=None):
    with tempfile.TemporaryDirectory(prefix='dv-exit-ack-') as tmp:
        out = Path(tmp)
        (out / 'test.cpp').write_text(harness(text))
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-pie', '-no-pie',
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        for case in cases:
            result = subprocess.run([str(out / 'test'), case], capture_output=True, text=True,
                                    timeout=10, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
            if failure:
                assert result.returncode != 0 and failure in result.stderr, result
                print('REJECTED', case, failure)
            else:
                print(result.stdout, end='')
                assert result.returncode == 0, result.stderr


run(source, ['normal', 'follow-source', 'video-off', 'no-vinfo', 'already-bypass',
             'always-on', 'hdr10plus-next', 'interrupt-exit'])
if a.negative_controls:
    marker = 'fake_sdr_bypass = true;\n\t\t\t\tdolby_vision_set_toggle_flag(1);'
    assert source.count(marker) == 1
    run(source.replace(marker, marker.replace('flag(1)', 'flag(0)')),
        ['follow-source'], '!premature_ack')
    marker = '(!fake_sdr_bypass &&\n\t\t     (dolby_vision_flags & FLAG_TOGGLE_FRAME))'
    assert source.count(marker) == 1
    mutant = source.replace(marker, '(dolby_vision_flags & FLAG_TOGGLE_FRAME)')
    mutant = mutant.replace('bool fake_sdr_bypass = false;', '').replace('fake_sdr_bypass = true;', '')
    run(mutant, ['follow-source'], 'metadata_calls ==')
