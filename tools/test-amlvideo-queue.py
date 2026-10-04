#!/usr/bin/env python3
"""Actual amlvideo QBUF/DQBUF and vfp ring regression; host services only.

No kernel/device/IRQ acceptance is implied. Provider frames, V4L2 containers,
notifications and locks are host stand-ins. Provider peek/get/states and queue
reset helper are extracted whole when available; receiver event dispatch is not.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile


def function(source, signature):
    start = source.index(signature)
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
typedef uint32_t u32;
typedef uint64_t u64;
#define AMLVIDEO_POOL_SIZE 16
#define VIDTYPE_V4L_EOS 1
#define VIDTYPE_COMPRESS 2
#define VIDTYPE_DI_PW 4
#define VIDTYPE_INTERLACE 8
#define V4L2_FIELD_INTERLACED 1
#define VFRAME_EVENT_PROVIDER_VFRAME_READY 1
#define VFRAME_FLAG_AMLVIDEO_DISCARD 0x1000000
#define V4L2_BUF_FLAG_DONE 0x4
#define smp_mb() atomic_thread_fence(memory_order_seq_cst)
#define smp_rmb() atomic_thread_fence(memory_order_acquire)
#define smp_wmb() atomic_thread_fence(memory_order_release)
#define do_div(n, d) ((n) /= (d))
#define DUR2PTS(n) ((n) - ((n) >> 4))
#define ATRACE_COUNTER(name,value) do{assert(!queue_spin_depth);(void)(name);(void)(value);}while(0)
#define pr_info(...) ((void)0)
struct vframe_s {
  u32 index,omx_index,type,flag,signal_type,pts,duration;
  u64 pts_us64;
  struct {int master_display_colour;} prop;
  u32 hdr10p_data_size;char *hdr10p_data_buf;
  bool next_vf_pts_valid;u32 next_vf_pts;
  u32 compWidth,compHeight,width,height;
};
#include "vfp.h"
struct mutex {pthread_mutex_t native;atomic_int attempts;atomic_int held;};
static _Thread_local int queue_spin_depth;
static _Thread_local int producer_mutex_depth;
static void mutex_lock(struct mutex *m){assert(!queue_spin_depth);atomic_fetch_add(&m->attempts,1);assert(!pthread_mutex_lock(&m->native));assert(atomic_fetch_add(&m->held,1)==0);++producer_mutex_depth;}
static void mutex_unlock(struct mutex *m){assert(producer_mutex_depth==1);--producer_mutex_depth;assert(atomic_fetch_sub(&m->held,1)==1);assert(!pthread_mutex_unlock(&m->native));}
typedef struct mutex spinlock_t;
static void host_spin_lock(spinlock_t *lock){assert(!queue_spin_depth);atomic_fetch_add(&lock->attempts,1);assert(!pthread_mutex_lock(&lock->native));assert(atomic_fetch_add(&lock->held,1)==0);++queue_spin_depth;}
static void host_spin_unlock(spinlock_t *lock){assert(queue_spin_depth==1);--queue_spin_depth;assert(atomic_fetch_sub(&lock->held,1)==1);assert(!pthread_mutex_unlock(&lock->native));}
#define spin_lock_irqsave(lock,flags) do{(flags)=0;host_spin_lock(lock);}while(0)
#define spin_unlock_irqrestore(lock,flags) do{(void)(flags);host_spin_unlock(lock);}while(0)
struct vivi_dev {
  struct vfq_s q_ready,q_omx;
  struct mutex vf_mutex;
  spinlock_t queue_lock;
  struct vframe_s *amlvideo_pool_ready[17],*amlvideo_pool_omx[17];
  struct vframe_s *vf;
  struct {char name[32];} v4l2_dev;
  char vf_receiver_name[32],vf_provider_name[32];
  u32 frame_num;int first_frame;u64 last_pts_us64;
  struct {u32 signal_type;int master_display_colour;u32 hdr10p_data_size;char hdr10p_data_buf[128];} am_parm;
};
struct file {struct vivi_dev *dev;};
struct v4l2_buffer {u32 index,sequence,field,flags;struct {u32 tv_sec,tv_usec;} timestamp;struct {u32 type,flags;} timecode;};
struct vframe_states {int vf_pool_size,buf_recycle_num,buf_free_num,buf_avail_num;};
static struct vivi_dev *video_drvdata(struct file *f){return f->dev;}
static u32 omx_freerun_index;
static struct vivi_dev dev;
static struct file file;
static struct vframe_s frames[4096];
static size_t provider_next,provider_count;
static unsigned notifications;
static struct vframe_s *vf_peek(const char *name){(void)name;assert(!queue_spin_depth);return provider_next<provider_count?&frames[provider_next]:NULL;}
static struct vframe_s *vf_get(const char *name){(void)name;assert(!queue_spin_depth);assert(producer_mutex_depth==1);return provider_next<provider_count?&frames[provider_next++]:NULL;}
static void vf_notify_receiver(const char *name,int event,void *data){(void)name;(void)event;(void)data;assert(!queue_spin_depth&&!producer_mutex_depth);++notifications;}
'''

TESTS = r'''
static void init(void){
  memset(&dev,0,sizeof(dev));memset(frames,0,sizeof(frames));
  assert(!pthread_mutex_init(&dev.vf_mutex.native,NULL));
  atomic_init(&dev.vf_mutex.attempts,0);atomic_init(&dev.vf_mutex.held,0);
  assert(!pthread_mutex_init(&dev.queue_lock.native,NULL));
  atomic_init(&dev.queue_lock.attempts,0);atomic_init(&dev.queue_lock.held,0);
  reset_queues();
  file.dev=&dev;provider_next=0;provider_count=4096;notifications=0;omx_freerun_index=100;
  for(size_t i=0;i<provider_count;++i){frames[i].index=(u32)i;frames[i].pts_us64=(u64)i*41708+1;frames[i].duration=4004;frames[i].width=1920;frames[i].height=1080;}
}
static struct v4l2_buffer dequeue(void){struct v4l2_buffer p={0};assert(vidioc_dqbuf(&file,NULL,&p)==0);return p;}
static void release(u32 index){struct v4l2_buffer p={.index=index};assert(vidioc_qbuf(&file,NULL,&p)==0);}
static void absent(void){
  init();struct v4l2_buffer a=dequeue(),b=dequeue(),c=dequeue();(void)a;(void)b;(void)c;
  int rp=dev.q_omx.rp,wp=dev.q_omx.wp;
  release(99);
  fprintf(stderr,"absent: ready=%d pending=%d notifications=%u\n",vfq_level(&dev.q_ready),vfq_level(&dev.q_omx),notifications);
  assert(vfq_level(&dev.q_ready)==0&&vfq_level(&dev.q_omx)==3&&notifications==0);
  assert(dev.q_omx.rp==rp&&dev.q_omx.wp==wp);
  release(b.index);assert(vfq_level(&dev.q_ready)==2&&vfq_level(&dev.q_omx)==1);
  unsigned before=notifications;release(a.index); // old returned target, not a newer prefix
  assert(vfq_level(&dev.q_ready)==2&&vfq_level(&dev.q_omx)==1&&notifications==before);
  assert(vfq_pop(&dev.q_ready)==&frames[0]);assert(vfq_pop(&dev.q_ready)==&frames[1]);
  release(c.index);assert(vfq_pop(&dev.q_ready)==&frames[2]);
  before=notifications;release(c.index);assert(notifications==before&&vfq_empty(&dev.q_ready));
}
static void capacity(void){
  init();struct v4l2_buffer last={0};
  for(int i=0;i<8;++i)last=dequeue();release(last.index);
  for(int i=0;i<7;++i)last=dequeue();
  assert(vfq_level(&dev.q_ready)==8&&vfq_level(&dev.q_omx)==7);
  size_t fetched=provider_next;u32 serial=omx_freerun_index,sequence=dev.frame_num;
  struct v4l2_buffer blocked={.index=0xabcdef};int result=vidioc_dqbuf(&file,NULL,&blocked);
  if(result==0){
    fprintf(stderr,"overflow before release: ready=%d pending=%d\n",vfq_level(&dev.q_ready),vfq_level(&dev.q_omx));
    release(blocked.index);
    fprintf(stderr,"overflow after release: ready=%d pending=%d\n",vfq_level(&dev.q_ready),vfq_level(&dev.q_omx));
  }
  assert(result==-EAGAIN);
  assert(provider_next==fetched&&omx_freerun_index==serial&&dev.frame_num==sequence&&blocked.index==0xabcdef);
  assert(!atomic_load(&dev.vf_mutex.held));
  release(last.index);assert(vfq_level(&dev.q_ready)==15&&vfq_empty(&dev.q_omx));
  assert(vfq_pop(&dev.q_ready)==&frames[0]);
  last=dequeue();assert(last.index==serial&&provider_next==fetched+1);
  release(last.index);assert(vfq_level(&dev.q_ready)==15);
  for(int i=1;i<16;++i)assert(vfq_pop(&dev.q_ready)==&frames[i]);
  assert(vfq_empty(&dev.q_ready)&&vfq_empty(&dev.q_omx)&&notifications==16);
}
static void wrap(void){
  init();omx_freerun_index=UINT32_MAX-2;
  for(int round=0;round<100;++round){
    size_t first=provider_next;struct v4l2_buffer p[6];
    for(int i=0;i<6;++i)p[i]=dequeue();
    release(p[2].index);assert(vfq_level(&dev.q_ready)==3&&vfq_level(&dev.q_omx)==3);
    for(int i=0;i<3;++i)assert(vfq_pop(&dev.q_ready)==&frames[first+i]);
    unsigned before=notifications;release(p[1].index);assert(notifications==before&&vfq_level(&dev.q_omx)==3);
    release(p[5].index);for(int i=3;i<6;++i)assert(vfq_pop(&dev.q_ready)==&frames[first+i]);
    assert(vfq_empty(&dev.q_ready)&&vfq_empty(&dev.q_omx));
  }
  assert(notifications==600&&omx_freerun_index==(u32)(UINT32_MAX-2+600u));
}
struct job {bool dequeue;struct v4l2_buffer p;int result;atomic_int finished;};
static void *ioctl_thread(void *arg){struct job *job=arg;job->result=job->dequeue?vidioc_dqbuf(&file,NULL,&job->p):vidioc_qbuf(&file,NULL,&job->p);atomic_store(&job->finished,1);return NULL;}
static void wait_for_mutex_or_finish(struct job *job,int attempts){
  for(int i=0;i<2000;++i){if(atomic_load(&dev.vf_mutex.attempts)>attempts||atomic_load(&job->finished))return;usleep(1000);}
  assert(!"ioctl did not reach mutex within bounded host schedule");
}
static void reset_lock(void){
  init();struct v4l2_buffer stale=dequeue();
  mutex_lock(&dev.vf_mutex);int attempts=atomic_load(&dev.vf_mutex.attempts);
  struct job job={.p=stale};atomic_init(&job.finished,0);pthread_t thread;assert(!pthread_create(&thread,NULL,ioctl_thread,&job));
  wait_for_mutex_or_finish(&job,attempts);
  assert(!atomic_load(&job.finished));assert(vfq_level(&dev.q_omx)==1&&vfq_empty(&dev.q_ready));
  // Model provider RESET while it owns the same production mutex.
  reset_queues();
  frames[100].pts_us64=777;vfq_push(&dev.q_omx,&frames[100]);
  mutex_unlock(&dev.vf_mutex);assert(!pthread_join(thread,NULL));
  assert(job.result==0&&vfq_peek(&dev.q_omx)==&frames[100]&&vfq_empty(&dev.q_ready)&&notifications==0);
  mutex_lock(&dev.vf_mutex);attempts=atomic_load(&dev.vf_mutex.attempts);
  job=(struct job){.dequeue=true};atomic_init(&job.finished,0);assert(!pthread_create(&thread,NULL,ioctl_thread,&job));
  wait_for_mutex_or_finish(&job,attempts);assert(!atomic_load(&job.finished));
  reset_queues();
  for(int i=0;i<15;++i)vfq_push(&dev.q_ready,&frames[200+i]);
  size_t fetched=provider_next;mutex_unlock(&dev.vf_mutex);assert(!pthread_join(thread,NULL));
  assert(job.result==-EAGAIN&&provider_next==fetched&&vfq_level(&dev.q_ready)==15&&vfq_empty(&dev.q_omx));
}
struct provider_job {bool reset;struct vframe_s *result;atomic_int finished;};
static void *provider_thread(void *arg){struct provider_job *job=arg;if(job->reset)reset_queues();else job->result=amlvideo_vf_get(&dev);atomic_store(&job->finished,1);return NULL;}
static void provider_lock(void){
  init();struct v4l2_buffer p=dequeue();release(p.index);
  struct vframe_states states={0};assert(amlvideo_vf_states(&states,&dev)==0);
  assert(states.buf_avail_num==1&&amlvideo_vf_peek(&dev)==&frames[0]);
  for(int reset=0;reset<2;++reset){
    host_spin_lock(&dev.queue_lock);int attempts=atomic_load(&dev.queue_lock.attempts);
    struct provider_job job={.reset=reset};atomic_init(&job.finished,0);
    pthread_t thread;assert(!pthread_create(&thread,NULL,provider_thread,&job));
    int i;for(i=0;i<2000;++i){if(atomic_load(&dev.queue_lock.attempts)>attempts||atomic_load(&job.finished))break;usleep(1000);}
    assert(i<2000&&!atomic_load(&job.finished));
    // An admitted reset/consumer cannot overlap these raw ring writes.
    vfq_init(&dev.q_ready,16,dev.amlvideo_pool_ready);vfq_init(&dev.q_omx,16,dev.amlvideo_pool_omx);
    host_spin_unlock(&dev.queue_lock);assert(!pthread_join(thread,NULL));
    assert(job.result==NULL&&vfq_empty(&dev.q_ready)&&vfq_empty(&dev.q_omx));
  }
}
int main(int argc,char **argv){assert(argc==2);if(!strcmp(argv[1],"absent"))absent();else if(!strcmp(argv[1],"capacity"))capacity();else if(!strcmp(argv[1],"wrap"))wrap();else if(!strcmp(argv[1],"reset-lock"))reset_lock();else if(!strcmp(argv[1],"provider-lock"))provider_lock();else assert(!"unknown case");puts("PASS");}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--source', type=Path)
    parser.add_argument('--test', choices=['absent', 'capacity', 'wrap', 'reset-lock', 'provider-lock'], action='append')
    parser.add_argument('--expect-failure', action='store_true')
    args = parser.parse_args()
    directory = args.kernel_root / 'drivers/amlogic/media/video_processor/video_dev'
    # These cases deliberately exercise the production 16-slot ring boundary.
    assert '#define AMLVIDEO_POOL_SIZE 16' in (directory / 'amlvideo.h').read_text()
    source = (args.source or directory / 'amlvideo.c').read_text()
    signatures = ['static struct vframe_s *amlvideo_vf_peek(',
                  'static struct vframe_s *amlvideo_vf_get(',
                  'static int amlvideo_vf_states(', 'static int vidioc_qbuf(',
                  'static int vidioc_dqbuf(']
    if 'static void amlvideo_queue_init(' in source:
        signatures.insert(0, 'static void amlvideo_queue_init(')
        reset = 'static void reset_queues(void){amlvideo_queue_init(&dev);}\n'
    else:
        reset = ('static void reset_queues(void){'
                 'vfq_init(&dev.q_ready,16,dev.amlvideo_pool_ready);'
                 'vfq_init(&dev.q_omx,16,dev.amlvideo_pool_omx);}\n')
    bodies = '\n'.join(function(source, signature) for signature in signatures)
    with tempfile.TemporaryDirectory(prefix='amlvideo-queue-') as tmp:
        tmp = Path(tmp)
        (tmp / 'test.c').write_text(PRELUDE + bodies + reset + TESTS)
        subprocess.run([os.environ.get('CC', 'gcc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-misleading-indentation', '-pthread',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                        '-I', str(directory / 'common'), str(tmp / 'test.c'), '-o', str(tmp / 'test')], check=True)
        for case in args.test or ['absent', 'capacity', 'wrap', 'reset-lock', 'provider-lock']:
            result = subprocess.run([str(tmp / 'test'), case], capture_output=True, text=True, timeout=10)
            if args.expect_failure:
                assert result.returncode != 0 and 'Assertion' in result.stderr, (case, result.stdout, result.stderr)
                print('REPRODUCED old defect:', case, result.stderr.strip())
            else:
                assert result.returncode == 0, (case, result.stdout, result.stderr)
                print('PASS:', case)


if __name__ == '__main__':
    main()
