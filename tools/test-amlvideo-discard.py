#!/usr/bin/env python3
"""Production AML discard-marker and sink admission fixtures; no device proof.

Actual QBUF/DQBUF/provider-return bodies and sink discard helpers/caller guards
are executed with recording provider/DV services. Normal toggle/IRQ/DI engines
remain modeled boundaries. Both DV-enabled and DV-disabled configurations run.
"""
import argparse
import os
import re
from pathlib import Path
import runpy
import subprocess
import tempfile

QUEUE = runpy.run_path(str(Path(__file__).with_name('test-amlvideo-queue.py')))
function = QUEUE['function']


IO_SERVICES = r'''
#ifndef V4L2_BUF_FLAG_DONE
#define V4L2_BUF_FLAG_DONE 4
#endif
#ifndef VFRAME_FLAG_AMLVIDEO_DISCARD
#define VFRAME_FLAG_AMLVIDEO_DISCARD 0x1000000
#endif
#define VFRAME_EVENT_RECEIVER_PUT 2
static struct vframe_s *returned;
static unsigned provider_puts;
static int vf_put(struct vframe_s *vf,const char *name){(void)name;assert(!queue_spin_depth&&!producer_mutex_depth);returned=vf;++provider_puts;return 0;}
static void vf_notify_provider(const char *name,int event,void *data){(void)name;(void)event;(void)data;assert(!queue_spin_depth&&!producer_mutex_depth);}
'''

IO_TESTS = r'''
static void initialize(void){
  assert(!pthread_mutex_init(&dev.vf_mutex.native,NULL));
  atomic_init(&dev.vf_mutex.attempts,0);atomic_init(&dev.vf_mutex.held,0);
  assert(!pthread_mutex_init(&dev.queue_lock.native,NULL));
  atomic_init(&dev.queue_lock.attempts,0);atomic_init(&dev.queue_lock.held,0);
  amlvideo_queue_init(&dev);file.dev=&dev;provider_count=32;omx_freerun_index=100;
  for(unsigned i=0;i<provider_count;++i){frames[i].index=i;frames[i].pts_us64=41708*i+1;frames[i].duration=4004;frames[i].width=1920;frames[i].height=1080;frames[i].flag=0x20000;}
}
int main(void){
  initialize();struct v4l2_buffer p[3]={{0}};
  for(int i=0;i<3;++i){assert(!vidioc_dqbuf(&file,NULL,&p[i]));assert(frames[i].flag==0x20000);}
  // DONE marks its matching index, never the earlier normal prefix.
  p[1].flags=V4L2_BUF_FLAG_DONE;assert(!vidioc_qbuf(&file,NULL,&p[1]));
  assert(frames[0].flag==0x20000&&frames[1].flag==(0x20000|VFRAME_FLAG_AMLVIDEO_DISCARD)&&frames[2].flag==0x20000);
  assert(vfq_pop(&dev.q_ready)==&frames[0]);assert(vfq_pop(&dev.q_ready)==&frames[1]);
  assert(notifications==2&&vfq_peek(&dev.q_omx)==&frames[2]);
  // Old marked target is an idempotent return; it cannot taint a newer frame.
  assert(!vidioc_qbuf(&file,NULL,&p[1]));assert(frames[2].flag==0x20000&&notifications==2);
  assert(!vidioc_qbuf(&file,NULL,&p[2]));assert(frames[2].flag==0x20000);
  // Original provider frame may retain its marker after a DI copy was consumed.
  amlvideo_vf_put(&frames[1],&dev);
  assert(returned==&frames[1]&&provider_puts==1&&frames[1].flag==0x20000);
  frames[3].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
  struct v4l2_buffer fresh={0};assert(!vidioc_dqbuf(&file,NULL,&fresh));assert(frames[3].flag==0x20000);
  puts("PASS marker lifecycle / exact DONE target / original return clear");
}
'''

SINK_PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
typedef atomic_int atomic_t;
#define ATOMIC_INIT(v) ATOMIC_VAR_INIT(v)
static int atomic_cmpxchg(atomic_t *p,int old,int value){atomic_compare_exchange_strong(p,&old,value);return old;}
static int atomic_xchg(atomic_t *p,int value){return atomic_exchange(p,value);}
static atomic_t video_get_owner = ATOMIC_INIT(0);
typedef uint32_t u32;
#define VFRAME_FLAG_AMLVIDEO_DISCARD 0x1000000
#define VIDTYPE_COMPRESS 8
#define VIDTYPE_MVC 16
#define get_count main_gets
#define get_count_pip pip_gets
#define VIDTYPE_DI_PW 4
#define VIDTYPE_V4L_EOS 1
#define VIDEO_NOTIFY_PROVIDER_GET 1
#define VIDEO_NOTIFY_PROVIDER_PUT 2
#define RECEIVER_NAME "main"
#define RECEIVERPIP_NAME "pip"
#define VFM_PATH_AMVIDEO 0
#define VFM_PATH_DEF 1
#define VFM_PATH_AUTO 2
#define VFM_PATH_PIP 3
#define VIDEO_START 9
struct vframe_s {u32 flag,type,pts,disp_pts,omx_index,width,height,compWidth,compHeight;uint64_t pts_us64,disp_pts_us64;void *fence;};
struct video_recv_s {const char *recv_name;bool active;int path_id;unsigned notify_flag;struct vframe_s *cur_buf,*original_vf;};
struct vframe_provider_s {int unused;};
static struct vframe_provider_s provider;
static struct {int display_path_id;} glayer_info[1];
static struct vframe_s frames[4],hist_test_vf,vf_local,current,*cur_dispbuf;
static bool hist_test_flag,framepacking_support;
static int framepacking_width,framepacking_height,get_di_count,fence_status;
static struct vframe_s *cur_pipbuf;
static int fence_get_status(void *fence){(void)fence;return fence_status;}
static bool reenter_get;static int peek_calls,steal_at_peek;
static struct vframe_s *video_get_frame(struct vframe_s *,bool,bool,u32);
static struct vframe_s *pending[4],*last_metadata,*last_put,*last_error;
static int count,head,vd1_path_id,pip_loop,put_di_count,video_notify_flag;
static unsigned video_drop_vf_cnt,videopip_drop_vf_cnt;
static bool dv_enabled,wait_metadata,get_empty,put_error,provider_exists;
static int metadata_result;
static unsigned gets,put_calls,metadata_calls,wait_calls,dv_puts,main_gets,pip_gets,main_errors,pip_errors,toggles,starts;
static bool video_suspend,nopostvideostart,video_start_post,show_first_frame_nosync,show_first_picture,show_nosync,slowsync_repeat_enable;
static int hdmi_in_onvideo,frame_repeat_count;
static u32 started_pts;
static struct vframe_s *vf_peek(const char *name){(void)name;++peek_calls;if(steal_at_peek==peek_calls)++head;if(reenter_get){reenter_get=false;assert(!video_get_frame(NULL,false,false,0));}return head<count?pending[head]:NULL;}
static struct vframe_s *vf_get(const char *name){(void)name;++gets;if(get_empty)return NULL;return head<count?pending[head++]:NULL;}
static int vf_put(struct vframe_s *vf,const char *name){(void)name;assert(!(vf->flag&VFRAME_FLAG_AMLVIDEO_DISCARD));++put_calls;last_put=vf;return put_error?-1:0;}
static struct vframe_provider_s *vf_get_provider(const char *name){(void)name;return provider_exists?&provider:NULL;}
static bool is_dolby_vision_enable(void){return dv_enabled;}
static int dolby_vision_wait_metadata(struct vframe_s *vf){assert(vf->flag&VFRAME_FLAG_AMLVIDEO_DISCARD);++wait_calls;return wait_metadata?1:0;}
static int dolby_vision_update_metadata(struct vframe_s *vf,bool drop){assert(drop);assert(vf->flag&VFRAME_FLAG_AMLVIDEO_DISCARD);++metadata_calls;last_metadata=vf;return metadata_result;}
static void dolby_vision_vf_put(struct vframe_s *vf){assert(vf==last_put);++dv_puts;}
static bool check_dispbuf(struct vframe_s *vf,bool error){assert(error&&vf==last_put);++main_errors;last_error=vf;return true;}
static bool check_pipbuf(struct vframe_s *vf,bool error){assert(error&&vf==last_put);++pip_errors;last_error=vf;return true;}
static void tsync_avevent_locked(int event,u32 pts){assert(event==VIDEO_START);++starts;started_pts=pts;}
static u32 timestamp_vpts_get(void){return 17;}
static void toggle(struct vframe_s *vf){assert(vf&&!(vf->flag&VFRAME_FLAG_AMLVIDEO_DISCARD));++toggles;cur_dispbuf=vf;}
'''

SINK_TESTS = r'''
static void reset(void){
  memset(frames,0,sizeof(frames));memset(pending,0,sizeof(pending));
  atomic_store(&video_get_owner,0);hist_test_flag=framepacking_support=false;get_di_count=0;fence_status=1;cur_pipbuf=NULL;reenter_get=false;peek_calls=steal_at_peek=0;
  head=count=0;vd1_path_id=VFM_PATH_AMVIDEO;glayer_info[0].display_path_id=VFM_PATH_AMVIDEO;
  pip_loop=put_di_count=video_notify_flag=0;video_drop_vf_cnt=videopip_drop_vf_cnt=0;
  metadata_result=0;dv_enabled=true;wait_metadata=get_empty=put_error=false;provider_exists=true;
  gets=put_calls=metadata_calls=wait_calls=dv_puts=main_gets=pip_gets=main_errors=pip_errors=toggles=starts=0;
  last_metadata=last_put=last_error=NULL;cur_dispbuf=&current;
  video_suspend=nopostvideostart=video_start_post=show_first_frame_nosync=show_first_picture=show_nosync=slowsync_repeat_enable=false;
  hdmi_in_onvideo=frame_repeat_count=0;started_pts=0;
  for(int i=0;i<4;++i){frames[i].flag=0x20000;frames[i].pts=100+i;pending[i]=&frames[i];}
}
static int expected_dv(bool enabled){
#ifdef CONFIG_AMLOGIC_MEDIA_ENHANCEMENT_DOLBYVISION
  return enabled;
#else
  (void)enabled;return 0;
#endif
}
static void helpers(void){
  for(int pip=0;pip<2;++pip)for(int path=0;path<2;++path)for(int enabled=0;enabled<2;++enabled){
    reset();count=1;dv_enabled=enabled;glayer_info[0].display_path_id=pip&&path?VFM_PATH_PIP:VFM_PATH_AMVIDEO;
    assert(video_discard_frame(&frames[0],pip,path)==0&&gets==0&&put_calls==0);
    frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;frames[0].type=VIDTYPE_DI_PW;
    assert(video_discard_frame(&frames[0],pip,path)==1);
    assert(cur_dispbuf==&current&&head==1&&put_calls==1&&last_put==&frames[0]&&frames[0].flag==0x20000);
    assert(main_gets==(unsigned)!pip&&pip_gets==(unsigned)pip);
    assert(video_drop_vf_cnt==(unsigned)!pip&&videopip_drop_vf_cnt==(unsigned)pip);
    assert(metadata_calls==(unsigned)expected_dv(path&&enabled));
    assert(dv_puts==(unsigned)expected_dv(enabled&&(!pip||path)));
  }
  reset();count=1;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;wait_metadata=true;
  if(expected_dv(true)){
    assert(video_discard_frame(&frames[0],false,true)==-EAGAIN);
    assert(!gets&&!put_calls&&!metadata_calls&&!video_drop_vf_cnt&&head==0);
    wait_metadata=false;
  }
  assert(video_discard_frame(&frames[0],false,true)==1&&cur_dispbuf==&current);
  reset();count=1;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;get_empty=true;
  assert(video_discard_frame(&frames[0],false,true)==-EAGAIN&&!put_calls&&!metadata_calls&&!video_drop_vf_cnt);
  assert(frames[0].flag&VFRAME_FLAG_AMLVIDEO_DISCARD);
  for(int pip=0;pip<2;++pip){
    reset();count=1;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;put_error=true;
    assert(video_discard_frame(&frames[0],pip,false)==1);
    assert(main_errors==(unsigned)!pip&&pip_errors==(unsigned)pip&&last_error==&frames[0]);
  }
  // A terminal parser error must still return the consumed frame exactly once.
  reset();count=1;metadata_result=-1;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
  assert(video_discard_frame(&frames[0],false,true)==1&&gets==1&&put_calls==1&&frames[0].flag==0x20000);
  assert(metadata_calls==(unsigned)expected_dv(true)&&dv_puts==(unsigned)expected_dv(true));
  // A DI copy is cleared independently; the original clears on provider return.
  reset();struct vframe_s original={.flag=0x20000|VFRAME_FLAG_AMLVIDEO_DISCARD};
  frames[0]=original;count=1;assert(video_discard_frame(&frames[0],false,true)==1);
  assert(frames[0].flag==0x20000&&original.flag==(0x20000|VFRAME_FLAG_AMLVIDEO_DISCARD));
}
static void common_helpers(void){
  for(int path=0;path<2;++path)for(int enabled=0;enabled<2;++enabled){
    reset();count=1;dv_enabled=enabled;
    struct video_recv_s ins={.recv_name="common",.active=true,.path_id=path?VFM_PATH_AMVIDEO:VFM_PATH_PIP,.cur_buf=&current,.original_vf=&current};
    assert(common_discard_frame(&ins,&frames[0])==0&&!gets);
    frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
    assert(common_discard_frame(&ins,&frames[0])==1);
    assert(ins.cur_buf==&current&&ins.original_vf==&current&&frames[0].flag==0x20000&&put_calls==1);
    assert(metadata_calls==(unsigned)expected_dv(path&&enabled)&&dv_puts==(unsigned)expected_dv(path&&enabled));
  }
  reset();count=1;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
  struct video_recv_s ins={.recv_name="common",.active=true,.path_id=VFM_PATH_AMVIDEO};
  wait_metadata=true;if(expected_dv(true)){assert(common_discard_frame(&ins,&frames[0])==-EAGAIN&&!gets&&!put_calls);wait_metadata=false;}
  assert(common_discard_frame(&ins,&frames[0])==1);
}
static void callers(void){
  reset();cur_dispbuf=NULL;count=2;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;frames[1].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
  startup();assert(head==2&&!starts&&!video_start_post&&!cur_dispbuf&&video_drop_vf_cnt==2);
  reset();cur_dispbuf=&vf_local;count=2;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
  startup();assert(head==1&&starts==1&&started_pts==frames[1].pts&&cur_dispbuf==&vf_local);
  reset();cur_dispbuf=NULL;count=1;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;wait_metadata=true;
  startup();if(expected_dv(true)){assert(head==0&&!starts);wait_metadata=false;startup();}assert(head==1&&!starts);
  for(int which=0;which<3;++which){
    reset();count=3;frames[1].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
    struct video_recv_s ins={.recv_name="common",.active=true,.path_id=VFM_PATH_AMVIDEO};
    if(which==0)main_loop();else if(which==1){vd1_path_id=glayer_info[0].display_path_id=VFM_PATH_PIP;pip_loop_body();}else common_loop(&ins);
    assert(head==3&&toggles==2&&put_calls==1&&last_put==&frames[1]&&cur_dispbuf==&frames[2]);
  }
}
static void ownership(void){
  // Synthetic histogram frames are outside the provider ownership path.
  reset();count=1;hist_test_flag=true;
  assert(video_vf_peek()==&hist_test_vf);
  assert(video_discard_frame(&hist_test_vf,false,true)==0&&!gets);
  main_loop();assert(toggles==1&&cur_dispbuf==&hist_test_vf&&head==0&&!gets);
  // Marked A cannot be stolen by either ordinary IRQ or indexed OMX consumer.
  reset();count=2;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
  assert(!process_get(&frames[0],100)&&head==0&&!gets);
  assert(!video_vf_get()&&head==0&&!gets);
  assert(video_discard_frame(&frames[0],false,true)==1&&head==1&&put_calls==1);
  // Stale marked peek must not discard unmarked replacement B, even before metadata.
  reset();count=2;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;head=1;
  assert(video_discard_frame(&frames[0],false,true)==-EAGAIN&&head==1&&!gets&&!put_calls&&!metadata_calls&&!wait_calls);
  // Reverse race: ordinary A is consumed by OMX after normal admission; B is marked.
  reset();count=2;frames[1].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;
  assert(video_discard_frame(&frames[0],false,true)==0);
  assert(process_get(&frames[0],100)==&frames[0]);
  assert(!video_vf_get()&&head==1&&gets==1);
  assert(video_discard_frame(&frames[1],false,true)==1&&head==2&&put_calls==1);
  // Conditional pop checks expected identity and OMX bound inside ownership.
  reset();count=2;frames[0].omx_index=30;
  assert(!video_get_frame(&frames[1],false,false,0)&&head==0);
  assert(!process_get(&frames[0],29)&&head==0);
  assert(process_get(&frames[0],30)==&frames[0]&&head==1);
  assert(!atomic_load(&video_get_owner));
  // Reentrant provider callbacks cannot enter the second consumer.
  reset();count=1;reenter_get=true;
  assert(video_get_frame(NULL,false,false,0)==&frames[0]&&gets==1);
  assert(!atomic_load(&video_get_owner));
  // Busy owners return promptly without touching provider state.
  reset();count=1;atomic_store(&video_get_owner,1);
  assert(!video_get_frame(NULL,false,false,0)&&!peek_calls);
  assert(video_discard_head(&frames[0])==-EAGAIN&&!peek_calls);
  atomic_store(&video_get_owner,0);
  // Startup normal peek A may change to marked B before the second anchor peek.
  reset();count=2;cur_dispbuf=NULL;frames[1].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;steal_at_peek=3;
  startup();assert(head==1&&!starts&&!video_start_post);
  // Failed marked fence stays for cleanup; unsignalled fences still wait.
  reset();count=1;frames[0].fence=&current;frames[0].flag|=VFRAME_FLAG_AMLVIDEO_DISCARD;fence_status=-1;
  assert(video_vf_peek()==&frames[0]&&head==0&&!put_calls);
  fence_status=0;assert(!video_vf_peek()&&head==0&&!put_calls);
}
int main(void){helpers();common_helpers();callers();ownership();puts("PASS discard helpers / production startup and loop guards / DV wait retry / cleanup");}
'''


def compile_run(code, includes, dv, negative=False):
    with tempfile.TemporaryDirectory(prefix='amlvideo-discard-') as tmp:
        tmp = Path(tmp)
        (tmp / 'test.c').write_text(code)
        cmd = [os.environ.get('CC', 'gcc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror',
               '-Wno-unused-parameter', '-Wno-unused-function', '-Wno-unused-variable',
               '-Wno-misleading-indentation', '-Wno-unused-but-set-variable', '-pthread', '-fno-pie', '-no-pie',
               '-fsanitize=address,undefined', '-fno-omit-frame-pointer']
        if dv:
            cmd.append('-DCONFIG_AMLOGIC_MEDIA_ENHANCEMENT_DOLBYVISION')
        for include in includes:
            cmd.extend(['-I', str(include)])
        cmd.extend([str(tmp / 'test.c'), '-o', str(tmp / 'test')])
        subprocess.run(cmd, check=True)
        result = subprocess.run([str(tmp / 'test')], capture_output=True, text=True, timeout=10)
        if negative:
            assert result.returncode != 0 and 'Assertion' in result.stderr, (result.stdout, result.stderr)
            print('REJECTED expected failure DV=' + str(dv), result.stderr.strip())
        else:
            assert result.returncode == 0, (result.stdout, result.stderr)
            print(result.stdout.strip(), 'DV=' + str(dv))


def io_harness(source):
    prelude = QUEUE['PRELUDE'].replace('u32 index,omx_index,type,signal_type,pts,duration;',
                                       'u32 index,omx_index,type,signal_type,pts,duration,flag;')
    signatures = ['static void amlvideo_queue_init(', 'static int vidioc_qbuf(',
                  'static int vidioc_dqbuf(', 'static void amlvideo_vf_put(']
    return prelude + IO_SERVICES + '\n'.join(function(source,s) for s in signatures) + IO_TESTS


def caller_guard(source, anchor, stop):
    begin = source.index(anchor)
    return source[begin:source.index(stop, begin)]


def sink_harness(video, common):
    bodies = [function(video, sig) for sig in [
        'static struct vframe_s *video_get_frame(', 'static int video_discard_head(',
        'static inline struct vframe_s *pip_vf_peek(', 'static inline struct vframe_s *pip_vf_get(',
        'static inline struct vframe_s *video_vf_peek(',
        'static inline struct vframe_s *video_vf_get_internal(', 'static inline struct vframe_s *video_vf_get(',
        'static inline int pip_vf_put(', 'static inline int video_vf_put(',
        'static int video_discard_frame(']]
    bodies += [function(common, sig) for sig in [
        'static inline struct vframe_s *common_vf_peek(',
        'static inline struct vframe_s *common_vf_get(',
        'static inline void common_vf_put(', 'static int common_discard_frame(']]
    startup = caller_guard(video, '/* A discarded first frame', '/* buffer switch management */')
    main_guard = caller_guard(video[video.index(startup)+len(startup):],
        'int discarded = video_discard_frame(vf, false,',
        'if (debug_flag & DEBUG_FLAG_OMX_DEBUG_DROP_FRAME)')
    pip_guard = caller_guard(video, 'int discarded = video_discard_frame(vf, true,',
        'if (!vf->frame_dirty)')
    common_guard = caller_guard(common, 'int discarded = common_discard_frame(ins, vf);',
        'if (!vf->frame_dirty)')
    wrappers = ('static void startup(void){struct vframe_s *vf;' + startup +
        'SET_FILTER: return;}\n' +
        'static void main_loop(void){struct vframe_s *vf=video_vf_peek();while(vf&&!video_suspend){' + main_guard +
        'struct vframe_s *normal=video_vf_get();if(!normal)break;toggle(normal);vf=video_vf_peek();}}\n' +
        'static void pip_loop_body(void){struct vframe_s *vf=pip_vf_peek();while(vf&&!video_suspend){' + pip_guard +
        'toggle(pip_vf_get());vf=pip_vf_peek();}}\n' +
        'static void common_loop(struct video_recv_s *ins){struct vframe_s *vf=common_vf_peek(ins);while(vf){' + common_guard +
        'toggle(common_vf_get(ins));vf=common_vf_peek(ins);}}\n')
    process_guard=caller_guard(function(video,'static void set_omx_pts('),
        'vf = video_get_frame(vf, false, true, frame_num);','index_dropped = vf->omx_index;')
    wrappers += ('static struct vframe_s *process_get(struct vframe_s *vf,u32 frame_num){'
        'while(vf){' + process_guard + 'return vf;}return NULL;}\n')
    return SINK_PRELUDE + '\n'.join(bodies) + wrappers + SINK_TESTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source',type=Path,help='Alternate amlvideo.c for regression baseline')
    parser.add_argument('--io-only',action='store_true')
    parser.add_argument('--expect-failure',action='store_true')
    parser.add_argument('--controls',action='store_true')

    args = parser.parse_args()
    header=(args.kernel_root/'include/linux/amlogic/media/vfm/vframe.h').read_text()
    marker=re.search(r'^#define\s+VFRAME_FLAG_AMLVIDEO_DISCARD\s+(0x[0-9a-fA-F]+)',header,re.M)
    assert marker and int(marker.group(1),16)==0x1000000, 'Fixture marker must match production header'
    directory=args.kernel_root/'drivers/amlogic/media/video_processor/video_dev'
    source=(args.source or directory/'amlvideo.c').read_text()
    for dv in [False,True]:
        compile_run(io_harness(source),[directory/'common'],dv,args.expect_failure)
    if args.controls:
        for name,old,new in [
            ('QBUF marker propagation','vf->flag |= VFRAME_FLAG_AMLVIDEO_DISCARD;', '(void)vf;'),
            ('fresh DQ clear','dev->vf->flag &= ~VFRAME_FLAG_AMLVIDEO_DISCARD;', '(void)dev->vf;'),
            ('provider original clear','vf->flag &= ~VFRAME_FLAG_AMLVIDEO_DISCARD;', '(void)vf;'),
        ]:
            mutated=source.replace(old,new,1)
            assert mutated != source,name
            print('CONTROL',name)
            compile_run(io_harness(mutated),[directory/'common'],False,True)
    if args.io_only:
        return
    sink=args.kernel_root/'drivers/amlogic/media/video_sink'
    video=(sink/'video.c').read_text()
    common=(sink/'video_receiver.c').read_text()
    assert video.count('vf_get(RECEIVER_NAME)') == 1, 'Raw main consumer bypasses admission helper'
    process=function(video,'static void set_omx_pts(')
    assert 'video_get_frame(vf, false, true, frame_num)' in process
    assert re.search(r'video_get_frame\(vf, false, true, frame_num\);\s*if \(!vf\)\s*break;',process)
    code=sink_harness(video,common)
    for dv in [False,True]:
        compile_run(code,[],dv,args.expect_failure)
    if args.controls:
        controls = [
            ('histogram admission', code.replace('if (!pip && hist_test_flag)', 'if (false)'), False),
            ('expected head identity', code.replace('expected && vf != expected', 'false'), False),
            ('ordinary marker refusal', code.replace('!!(vf->flag & VFRAME_FLAG_AMLVIDEO_DISCARD) != discard', 'false'), False),
            ('stale discard peek', code.replace('marked = video_discard_head(vf);', 'marked = vf && (vf->flag & VFRAME_FLAG_AMLVIDEO_DISCARD);'), True),
            ('OMX index bound', code.replace('limit_index && vf->omx_index > max_index', 'false'), False),
            ('owner exclusion', code.replace('if (atomic_cmpxchg(&video_get_owner, 0, 1))', 'if (false)'), False),
            ('startup replacement guard', code.replace('if (vf && !(vf->flag & VFRAME_FLAG_AMLVIDEO_DISCARD))', 'if (vf)'), False),
            ('startup integration', code.replace('int discarded = video_discard_frame(vf, false,', 'int discarded = 0; /* video_discard_frame(vf, false,', 1).replace('vd1_path_id == VFM_PATH_AUTO);', 'vd1_path_id == VFM_PATH_AUTO); */', 1), False),
            ('main loop integration', code.replace(main_guard_text(video), 'int discarded = 0;\n', 1), False),
            ('PIP loop integration', code.replace(caller_guard(video, 'int discarded = video_discard_frame(vf, true,', 'if (!vf->frame_dirty)'), 'int discarded = 0;\n', 1), False),
            ('common loop integration', code.replace(caller_guard(common, 'int discarded = common_discard_frame(ins, vf);', 'if (!vf->frame_dirty)'), 'int discarded = 0;\n', 1), False),
            ('DV pair cleanup', code.replace('dolby_vision_vf_put(vf);', '(void)vf;'), True),
            ('metadata wait', code.replace('process_dv && dolby_vision_wait_metadata(vf) == 1', 'false'), True),
            ('metadata drop', code.replace('dolby_vision_update_metadata(vf, true)', 'dolby_vision_update_metadata(vf, false)'), True),
            ('paired put', code.replace('if (video_vf_put(vf) < 0)', 'if (0)'), False),
        ]
        for name, mutant, dv in controls:
            assert mutant != code, name
            print('CONTROL',name)
            compile_run(mutant,[],dv,True)


def main_guard_text(video):
    begin=video.index('/* buffer switch management */',video.index('/* A discarded first frame'))
    return caller_guard(video[begin:], 'int discarded = video_discard_frame(vf, false,',
                        'if (debug_flag & DEBUG_FLAG_OMX_DEBUG_DROP_FRAME)')


if __name__=='__main__':
    main()
