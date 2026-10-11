#!/usr/bin/env python3
"""Exercise the production read-only backend publication and fresh-CP tagging.

Uses host locks/register-state stand-ins. Does not establish actual vframe,
register-programming or HDMI timing on a device.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(source, signature):
    start = source.index(signature)
    pos = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[pos] == '{') - (source[pos] == '}')
        pos += 1
    return source[start:pos]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    driver = (args.root / 'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c').read_text()
    video = (args.root / 'drivers/amlogic/media/video_sink/video.c').read_text()
    helpers = driver[driver.index('static DEFINE_SPINLOCK(dv_backend_lock);'):driver.index('static void apply_stb_core_settings\n')]
    show = function(driver, 'static ssize_t backend_state_show(')
    parse = function(driver, 'static int dv_parse_metadata_internal(')
    tag = parse[parse.index('\t\tdv_pending_new = use_new;'):parse.index('\t\tapplied_graphic_pq = parsed_graphic_pq;')]
    assert parse.index('u64 backend_epoch = dv_backend_generation()') < parse.index('if (flag >= 0) {\n' + tag)
    assert '__ATTR(backend_state, 0444, backend_state_show, NULL)' in driver
    program = function(driver, 'static void apply_stb_core_settings\n')
    assert program.index('WRITE_ONCE(amdv_multi_dv_mode, dv_pending_new)') < program.index('dv_backend_publish(enable && (dolby_vision_mask & 1), core1_programmed ? 1 : 0,')
    assert 'if (!enable)\n\t\tdolby_vision_backend_reset()' in function(driver, 'void enable_dolby_vision(')
    receiver = function(video, 'static int video_receiver_event_fun(int type, void *data, void *private_data)')
    assert 'dolby_vision_backend_reset()' not in receiver
    retirements = []
    for signature in ['static void video_vf_unreg_provider(void)\n',
                      'static void video_vf_light_unreg_provider(int need_keep_frame)\n']:
        retirement = function(video, signature)
        assert retirement.index('atomic_inc(&video_unreg_flag)') < retirement.index('while (atomic_read(&video_inirq_flag)')
        assert retirement.index('while (atomic_read(&video_inirq_flag)') < retirement.index('dolby_vision_backend_reset()') < retirement.index('atomic_dec(&video_unreg_flag)')
        fence = retirement[retirement.index('atomic_inc(&video_unreg_flag)'):retirement.index('schedule();') + len('schedule();')]
        retirements.append('static void retire(void){\n' + fence + '\n' + retirement[retirement.index('dolby_vision_backend_reset()'):retirement.index('dolby_vision_backend_reset()') + len('dolby_vision_backend_reset();')] + '\natomic_dec(&video_unreg_flag);\n}')
    for signature in ['int unregister_dv_functions(', 'int unregister_dv_functions_multi(']:
        assert 'dolby_vision_backend_reset()' in function(driver, signature)
    skip_guards = []
    for name in ['stb_dolby_core1_set', 'dolby_core1_set']:
        core = function(driver, 'static int ' + name + '\n')
        guard = next(line.strip() for line in core.splitlines() if 'FLAG_DISABE_CORE_SETTING)) return' in line)
        assert 'return 1;' in guard
        skip_guards.append(guard)
    preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <pthread.h>
#include <sys/types.h>
typedef uint64_t u64;typedef uint32_t u32;
#define DEFINE_SPINLOCK(name) pthread_mutex_t name=PTHREAD_MUTEX_INITIALIZER
#define spin_lock_irqsave(lock, flags) do {(flags)=0;assert(!pthread_mutex_lock(lock));}while(0)
#define spin_unlock_irqrestore(lock, flags) do {(void)(flags);assert(!pthread_mutex_unlock(lock));}while(0)
#define EXPORT_SYMBOL(...)
#define READ_ONCE(value) (value)
#define scnprintf snprintf
#define PAGE_SIZE 4096
struct class{};struct class_attribute{};
static bool dv_pending_new,dolby_vision_on,dolby_vision_core1_on,dv_new_runtime_failed;
static unsigned int dv_new_backend_available;
'''
    wrapper_preamble = r'''
#include <string.h>
typedef uint8_t u8;
#define WRITE_ONCE(dest,value) ((dest)=(value))
#define CP_FLAG_CHANGE_ALL 0xff
#define CP_FLAG_CONST_TC2 2
#define CP_FLAG_CHANGE_TC2 4
#define FLAG_DISABE_CORE_SETTING 0x800
#define FLAG_CERTIFICAION 0x400
#define FLAG_MUTE 0x100
#define FORMAT_DOVI 1
#define VIDEO_MUTE_ON_DV 1
#define VIDEO_MUTE_OFF 0
#define MUTE_TYPE_NONE 0
#define DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL 1
#define VPP_CLIP_MISC0 0
#define VPP_CLIP_MISC1 1
#define VSYNC_WR_MPEG_REG(...) ((void)0)
#define pr_dolby_dbg(...) ((void)0)
struct dm_lut_ipcore {u32 data[4];};
struct settings_s {
 bool mode_changed,el_flag,el_halfsize_flag;
 u32 src_format,dm_reg1[4],comp_reg[4],dm_lut1[4],dm_reg2[4],dm_reg3[4];
 struct dm_lut_ipcore dm_lut2;
 struct {u32 size;u32 raw_metadata[4];} md_reg3;
};
static struct settings_s new_dovi_setting,dovi_setting;
struct vinfo_s {u32 width,height,field_height,sync_duration_num,sync_duration_den;};
static struct vinfo_s display={1920,1080,1080,24,1};
static const struct vinfo_s* get_current_vinfo(void){return &display;}
static u32 dolby_vision_flags,dolby_vision_mask=7,stb_core_setting_update_flag;
static u32 osd_graphic_width=1920,osd_graphic_height=1080,dv_cert_graphic_width,dv_cert_graphic_height;
static bool force_reset_core2,stb_core2_const_flag,dv_applied_l11,dv_pending_l11,txlx;
static int amdv_multi_dv_mode,dv_applied_vsif,dv_pending_vsif,dv_applied_ci,dv_pending_ci;
static int cur_mute_type,dolby_vision_mode,core1_writes,debug_dolby;
static bool is_dolby_vision_stb_mode(void){return true;}
static bool is_meson_txlx_stbmode(void){return txlx;}
static void adjust_vpotch(void){}
static int get_mute_type(void){return 0;}
static int get_video_mute(void){return 0;}
static void dolby_core2_set(u32*a,u32*b,u32 w,u32 h,u32 flags){(void)a;(void)b;(void)w;(void)h;(void)flags;}
static void dolby_core3_set(u32 size,u32*a,u32*b,u32 w,u32 h,bool tunnel,u8 pps){(void)size;(void)a;(void)b;(void)w;(void)h;(void)tunnel;(void)pps;}
'''
    for name, guard in zip(['stb_dolby_core1_set', 'dolby_core1_set'], skip_guards):
        wrapper_preamble += '\nstatic int ' + name + '(u32*a,u32*b,u32*c,int w,int h,int bl,int el,int half,bool src,bool reset){\n(void)a;(void)b;(void)c;(void)w;(void)h;(void)bl;(void)el;(void)half;(void)src;(void)reset;\n' + guard + '\n++core1_writes;return 0;}'
    candidate = '\nstatic void candidate(bool fresh, bool video, bool use_new, bool success, u64 backend_epoch) {\nvoid *vf=video?(void*)1:NULL;unsigned int toggle_mode=fresh?1:0;(void)toggle_mode;if(success){\n' + tag + '\n}}\n'
    tests = r'''
static void expect(int backend,int usable) {
 char text[PAGE_SIZE];unsigned long long generation;int actual;unsigned int healthy;
 backend_state_show(NULL,NULL,text);assert(sscanf(text,"%llu %d %u",&generation,&actual,&healthy)==3);
 assert(generation==dv_backend_generation());assert(actual==backend);assert(healthy==(unsigned int)usable);
}
int main(void) {
 dolby_vision_on=dolby_vision_core1_on=true;dv_new_backend_available=1;
 expect(-1,1);
 u64 epoch=dv_backend_generation();candidate(true,true,true,true,epoch);dv_backend_publish(true,7,1920,1080);expect(1,1);
 candidate(true,true,false,true,epoch);dv_backend_publish(true,7,1920,1080);expect(0,1); // original with newer registered
 candidate(true,true,true,true,epoch);dv_backend_publish(true,7,1920,1080);expect(1,1);
 dolby_vision_backend_reset();expect(-1,1); // unchanged raw backend across stop/start
 dv_backend_publish(true,7,1920,1080);expect(-1,1); // old pending cannot establish new lifetime
 epoch=dv_backend_generation();candidate(false,true,true,true,epoch);dv_backend_publish(true,7,1920,1080);expect(-1,1); // retained old frame cannot stamp a new provider
 candidate(true,true,true,false,epoch);dv_backend_publish(true,7,1920,1080);expect(-1,1); // failed CP candidate
 candidate(true,true,false,true,epoch);dv_backend_publish(true,7,1920,1080);expect(0,1); // successful original/conversion/VP route
 candidate(true,true,true,true,epoch);dv_backend_publish(true,7,1920,1080);expect(1,1);
 candidate(true,true,false,false,epoch);dv_backend_publish(true,4,1920,1080);expect(1,1); // failed control path leaves actual registers and identity intact
 dolby_vision_on=false;expect(-1,1);dolby_vision_on=true;
 dolby_vision_core1_on=false;expect(-1,1);dolby_vision_core1_on=true;
 candidate(true,false,false,true,epoch);dv_backend_publish(false,7,0,0);expect(-1,1); // GUI/disc hold
 candidate(true,true,true,true,epoch);dv_backend_publish(true,4,1920,1080);expect(-1,1); // core3-only is not video programming
 dv_backend_publish(true,1,0,1080);expect(-1,1);
 dv_backend_publish(true,1,1920,0);expect(-1,1);
 dv_backend_publish(true,1,1920,1080);expect(1,1);
 epoch=dv_backend_generation();dolby_vision_backend_reset();candidate(true,true,true,true,epoch);dv_backend_publish(true,7,1920,1080);expect(-1,1); // late CP completion after reset
 epoch=dv_backend_generation();candidate(true,true,false,true,epoch);dv_backend_publish(true,7,1920,1080);dv_new_runtime_failed=true;expect(0,0); // applied original after newer failure
 dv_new_runtime_failed=false;expect(0,1);dv_new_backend_available=0;expect(0,0);
 // Provider entry fences new IRQs but does not advance observation yet. Old
 // IRQ completion remains in the old generation until the synchronized reset.
 epoch=dv_backend_generation();candidate(true,true,true,true,epoch);dv_backend_publish(true,7,1920,1080);
 dolby_vision_backend_reset();expect(-1,0); // after old IRQ drain, before admission resumes
 candidate(false,true,true,true,dv_backend_generation());dv_backend_publish(true,7,1920,1080);expect(-1,0);
 puts("Applied backend generation/freshness/active/GUI/failure publication checks passed");
}
'''
    wrapper_tests = r'''
int main(void){
 dolby_vision_on=dolby_vision_core1_on=true;dv_new_backend_available=1;
 for(int path=0;path<2;++path){
  txlx=path;dolby_vision_backend_reset();u64 epoch=dv_backend_generation();
  candidate(true,true,true,true,epoch);
  apply_stb_core_settings(1,7,true,(1920U<<16),0);expect(-1,1); // core3 display height cannot repair zero video height
  apply_stb_core_settings(1,7,true,(1920U<<16)|0xffff,0);expect(-1,1);
  apply_stb_core_settings(1,7,true,(0xffffU<<16)|1080,0);expect(-1,1);
  apply_stb_core_settings(1,4,true,(1920U<<16)|1080,0);expect(-1,1);
  candidate(true,true,false,true,epoch);apply_stb_core_settings(1,7,true,(1920U<<16)|1080,0);expect(0,1);
  int writes=core1_writes;dolby_vision_flags=FLAG_DISABE_CORE_SETTING;
  candidate(true,true,true,true,epoch);apply_stb_core_settings(1,7,true,(1920U<<16)|1080,0);assert(core1_writes==writes);expect(0,1);
  dolby_vision_flags=0;apply_stb_core_settings(1,7,true,(1920U<<16)|1080,0);assert(core1_writes==writes+1);expect(1,1);
  apply_stb_core_settings(1,7,true,(1920U<<16),0);expect(-1,1); // actual bypass clears a previously applied identity too
 }
 puts("Full core-programming wrapper: normalized video dimensions and skipped updates passed");
}
'''
    with tempfile.TemporaryDirectory(prefix='dv-backend-state-') as tmp:
        tmp = Path(tmp)

        def run(text, name, expected=True):
            source, exe = tmp / (name + '.c'), tmp / name
            source.write_text(text)
            subprocess.run(['cc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-pthread', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie'] + ([] if expected else ['-Wno-unused-but-set-variable']) + [str(source), '-o', str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
            assert (result.returncode == 0) == expected, (name, result.stdout, result.stderr)
            if not expected:
                assert "Assertion" in result.stderr, (name, result.stderr)
            print(result.stdout.strip() if expected else 'Rejected negative control: ' + name)

        run(preamble + helpers + show + candidate + tests, 'state')
        expectation = tests[tests.index('static void expect('):tests.index('int main(void)')]
        full = preamble + helpers + show + wrapper_preamble + program + candidate + expectation + wrapper_tests
        run(full, 'program-wrapper')
        schedule = r'''
static int video_unreg_flag,video_inirq_flag=1;
#define atomic_inc(p) (++*(p))
#define atomic_dec(p) (--*(p))
#define atomic_read(p) (*(p))
static void schedule(void){
 assert(video_unreg_flag==1); // new IRQs cannot pass the existing fence
 u64 epoch=dv_backend_generation();candidate(true,true,true,true,epoch);
 dv_backend_publish(true,7,1920,1080);video_inirq_flag=0;
}
'''
        retiring_main = 'int main(void){dolby_vision_on=dolby_vision_core1_on=true;dv_new_backend_available=1;retire();expect(-1,1);assert(video_unreg_flag==0);}'
        for index, retiring in enumerate(retirements):
            retiring_source = preamble + helpers + show + candidate + expectation + schedule + retiring + retiring_main
            run(retiring_source, 'provider-retirement-' + str(index))
            if args.negative_controls:
                # Reproduce the independently found old-IRQ/new-epoch schedule.
                early = retiring.replace('dolby_vision_backend_reset();', '').replace('static void retire(void){', 'static void retire(void){dolby_vision_backend_reset();')
                run(retiring_source.replace(retiring,early), 'early-provider-reset-' + str(index), False)
        if args.negative_controls:
            for name, old, new in [('stale-pending', 'dv_backend_pending_epoch == dv_backend_epoch', 'true'),
                                   ('kept-frame', 'toggle_mode == 1 || dv_backend_pending_epoch == backend_epoch', 'true'),
                                   ('engine-off', 'READ_ONCE(dolby_vision_on) && READ_ONCE(dolby_vision_core1_on) ?', 'true ?')]:
                production = helpers + show + candidate
                assert old in production
                run(preamble + production.replace(old, new) + tests, name, False)
            for name, old, new in [('display-height-as-video', 'video_h_size, video_v_size);', 'h_size, v_size);'),
                                   ('skipped-program-published', 'core1_programmed ? 1 : 0', 'mask')]:
                assert old in full
                run(full.replace(old,new), name, False)
    print('Read-only publication, VD1 provider boundaries and successful-CP/core wiring passed')


if __name__ == '__main__':
    main()
