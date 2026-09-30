#!/usr/bin/env python3
"""Exercise production framebuffer wait/pan bodies with modeled scanout.

The harness does not emulate Mali/GPU rendering or physical RDMA. It tests the
handoff used by the CE Mali fbdev binary: pan, wait, then reuse a framebuffer.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile


def function_body(source, name):
    start = source.index('{', source.index(name + '('))
    depth = 1
    end = start + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def cases(source):
    start = source.index('\tcase FBIO_WAITFORVSYNC:')
    return source[start:source.index('\tcase FBIOGET_OSD_SCALE_AXIS:', start)]


HARNESS = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef int32_t s32;
typedef int64_t s64;
enum { FBIO_WAITFORVSYNC=0x40044620, FBIO_WAITFORVSYNC_64=0x40044621,
       VPU_VIU_VD1=1, HW_OSD_COUNT=3, DISP_GEOMETRY=1, VIU1=0, VIU2=1 };
static struct { u32 viu1_osd_count; } osd_meson_dev = {2};
static struct {
    struct { int x_start,x_end,y_start,y_end; } pandata[3];
    struct { int x,y,w,h; } src_data[3];
    bool osd_fps_start[3];
    unsigned osd_fps[3];
} osd_hw;
static int power_down, waited[2], current_buffer, pending_buffer, scanned_writes;
static int copy_fault;
static size_t copied;
static s64 timestamp[2];
static s64 clock_now = INT64_C(0x1234567800000000);
static int pxp_mode, wait_mode;
static unsigned long waited_timeout;
typedef struct { s64 tv64; } ktime_t;
static ktime_t ktime_get(void) { return (ktime_t){++clock_now}; }
#define msecs_to_jiffies(ms) (ms)
#define osd_vsync_wq VIU1
#define osd_vsync2_wq VIU2
static u32 get_output_device_id(u32 index) { return index < 2 ? 0 : 1; }
static void add_to_update_list(u32 index, int update) {
    assert(update==DISP_GEOMETRY);
    pending_buffer = osd_hw.pandata[index].y_start / 1080;
}
#define osd_log_dbg2(...) ((void)0)
#define MODULE_BASE 0
static int get_vpu_mem_pd_vmod(int mod) { assert(mod==VPU_VIU_VD1); return power_down; }
static long wait_refresh(int output, bool condition, unsigned long timeout) {
    ++waited[output];
    waited_timeout=timeout;
    assert(!condition);
    if(wait_mode==1) return 0; // Existing timeout result is ignored.
    if(wait_mode==2) return -512; // Existing interrupted result is ignored.
    if (pending_buffer >= 0) current_buffer = pending_buffer;
    pending_buffer = -1;
    timestamp[output]=++clock_now;
    return 1;
}
#define wait_event_interruptible_timeout(queue,condition,timeout) \
    wait_refresh(queue,condition,timeout)
static s64 osd_wait_vsync_event(void)
@WAIT1@
static s64 osd_wait_vsync_event_viu2(void)
@WAIT2@
static int copy_to_user(void *dest, const void *src, size_t size) {
    copied = size;
    if (copy_fault) return size;
    memcpy(dest, src, size);
    return 0;
}
static int ioctl_wait(u32 cmd, int node, void *argp) {
    struct { int node; } value={node}, *info=&value;
    int ret=0;
    s32 vsync_timestamp=0;
    s64 vsync_timestamp_64=0;
    switch(cmd) {
    @CASES@
    default: return -1;
    }
    return ret;
}
static void osd_pan_display_hw(u32 index, unsigned xoffset, unsigned yoffset)
@PAN@

static void reset(void) {
    memset(&osd_hw,0,sizeof(osd_hw));
    memset(waited,0,sizeof(waited));
    for(int i=0;i<3;++i) osd_hw.pandata[i].y_end=1079;
    current_buffer=0; pending_buffer=-1; scanned_writes=0;
    copy_fault=0; copied=0;
    pxp_mode=0; wait_mode=0; waited_timeout=0;
    timestamp[0]=timestamp[1]=clock_now;
}
static void abi_cases(bool legacy) {
    for(int down=0;down<2;++down) for(int node=0;node<3;++node)
    for(int wide=0;wide<2;++wide) for(int mode=0;mode<3;++mode)
    for(int pxp=0;pxp<2;++pxp) {
        reset(); power_down=down; wait_mode=mode; pxp_mode=pxp;
        unsigned char data[8]; memset(data,0xcc,sizeof(data));
        s64 before=timestamp[node<2 ? 0 : 1];
        assert(ioctl_wait(wide ? FBIO_WAITFORVSYNC_64 : FBIO_WAITFORVSYNC,node,data)==0);
        bool should_wait = !legacy || wide || down;
        assert(waited[node<2 ? 0 : 1]==should_wait);
        assert(waited[node<2 ? 1 : 0]==0);
        assert(waited_timeout==(should_wait ? (pxp ? 50 : 1000) : 0));
        assert(copied==(wide ? sizeof(s64) : sizeof(s32)));
        s64 expected=(should_wait && mode==0) ? before+2 : before;
        if(wide) { s64 out; memcpy(&out,data,8); assert(out==expected); }
        else {
            s32 out; memcpy(&out,data,4);
            assert(out==(should_wait ? (s32)expected : 0));
            for(int i=4;i<8;++i) assert(data[i]==0xcc);
        }
        copy_fault=1;
        assert(ioctl_wait(wide ? FBIO_WAITFORVSYNC_64 : FBIO_WAITFORVSYNC,node,data)
               ==(wide ? sizeof(s64) : sizeof(s32)));
    }
}
static void framebuffer_reuse(bool legacy) {
    // Model rapid final-clear, interrupted hide and show/hide/show contents.
    // Every produced frame uses the actual pan + WAITFORVSYNC sequence. GUI
    // animation and pixel colour are inputs, not an emulated Kodi assertion.
    const unsigned char sequences[][6] = {{255,128,0,0,0,0},
                                         {255,128,255,255,0,0},
                                         {255,0,255,0,255,0}};
    for(int count=2;count<=3;++count) for(int scene=0;scene<3;++scene) {
        reset(); power_down=0;
        unsigned char pixels[3]={255,255,255};
        for(int step=0;step<6;++step) {
            int buffer=(step+1)%count;
            if(buffer==current_buffer) ++scanned_writes;
            pixels[buffer]=sequences[scene][step];
            osd_pan_display_hw(0,0,buffer*1080);
            s32 stamp=0;
            assert(ioctl_wait(FBIO_WAITFORVSYNC,0,&stamp)==0);
        }
        if(legacy) assert(scanned_writes>0);
        else { assert(scanned_writes==0); assert(pixels[current_buffer]==0); }
    }
    // Replacement framebuffer starts with new pan geometry, not old offsets.
    reset(); power_down=0;
    osd_pan_display_hw(0,0,1080);
    s32 stamp;
    ioctl_wait(FBIO_WAITFORVSYNC,0,&stamp);
    assert(current_buffer==(legacy ? 0 : 1));
}
int main(int argc,char **argv) {
    bool legacy=argc>1;
    abi_cases(legacy); framebuffer_reuse(legacy);
    puts(legacy ? "Legacy defect reproduced: scanout buffer reused before refresh"
                : "Framebuffer wait/pan ABI, routing, timeout/interruption and buffer-reuse cases passed");
    return 0;
}
'''


def run(block, pan, wait1, wait2, legacy=False):
    with tempfile.TemporaryDirectory(prefix='osd-fb-vsync-') as temp:
        root = Path(temp)
        code = root / 'wait.c'
        code.write_text(HARNESS.replace('@CASES@', block).replace('@PAN@', pan)
                        .replace('@WAIT1@', wait1).replace('@WAIT2@', wait2))
        subprocess.run(['cc', '-std=c11', '-O1', '-g', '-fsanitize=address,undefined',
                        '-fno-pie', '-no-pie', str(code), '-o', str(root / 'wait')], check=True)
        return subprocess.run([str(root / 'wait')] + (['legacy'] if legacy else []),
                              capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parents = Path(__file__).resolve().parents
    parser.add_argument('--root', type=Path, default=parents[2] if len(parents) > 2 else Path.cwd())
    parser.add_argument('--expect-legacy-defect', action='store_true')
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    folder = args.root / 'drivers/amlogic/media/osd'
    block = cases((folder / 'osd_fb.c').read_text())
    hw = (folder / 'osd_hw.c').read_text()
    pan = function_body(hw, 'void osd_pan_display_hw')
    wait1 = function_body(hw, 's64 osd_wait_vsync_event')
    wait2 = function_body(hw, 's64 osd_wait_vsync_event_viu2')
    result = run(block, pan, wait1, wait2, args.expect_legacy_defect)
    print(result.stdout.strip())
    if result.returncode:
        print(result.stderr)
        raise SystemExit(result.returncode)
    if args.negative_controls:
        mutations = {
            'fake-hardware-wait': block.replace('vsync_timestamp = (s32)osd_wait_vsync_event();',
                'vsync_timestamp = power_down ? (s32)osd_wait_vsync_event() : 0;'),
            'wrong-output': block.replace('vsync_timestamp = (s32)osd_wait_vsync_event_viu2();',
                'vsync_timestamp = (s32)osd_wait_vsync_event();'),
            'wrong-ABI-width': block.replace('sizeof(s32)', 'sizeof(s64)'),
        }
        for name, mutant in mutations.items():
            if mutant == block or run(mutant, pan, wait1, wait2).returncode == 0:
                raise SystemExit('Negative control survived: ' + name)
            print('Rejected negative control:', name)


if __name__ == '__main__':
    main()
