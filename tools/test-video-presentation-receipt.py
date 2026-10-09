#!/usr/bin/env python3
"""Extract production RDMA manager/fence and sink receipt gates. MMIO completion,
IRQ delivery and vframes are controlled host inputs, not hardware acceptance.
Exercises distinct software/armed/completed batches and unsafe negative controls.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]

def function(s,sig):
 a=s.index(sig);b=s.index('{',a)+1;depth=1
 while depth:
  depth+=(s[b]=='{')-(s[b]=='}');b+=1
 return s[a:b]

PRELUDE=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;typedef unsigned long long u64;typedef uintptr_t dma_addr_t;typedef int irqreturn_t;
#define IRQ_HANDLED 1
#define RDMA_NUM 8
#define MAX_CONFLICT 32
#define MAX_TRACE_NUM 16
#define CPU_SC2 3
#define RDMA_STATUS 100
#define RDMA_CTRL 101
#define RDMA_ACCESS_MAN 102
#define RESET4_REGISTER 103
#define RDMA_TRIGGER_VSYNC_INPUT 1
#define RDMA_TRIGGER_MANUAL 0x100
#define RDMA_AUTO_START_MASK 0x80000
#define EXPORT_SYMBOL(x)
#define pr_info(...) ((void)0)
static int debug_flag,reset_count,rdma_watchdog_count,rdma_trace_enable,rdma_trace_num;
static u32 rdma_trace_reg[MAX_TRACE_NUM];
static int ctrl_ahb_rd_burst_size=3,ctrl_ahb_wr_burst_size=3;
struct {int cpu_type;int trigger_mask_len;} rdma_meson_dev={0,8};
static void* rdma_rst;
static void reset_control_reset(void* p){(void)p;}
static bool held;
#define spin_lock_irqsave(l,f) do{(f)=0;assert(!held);held=true;}while(0)
#define spin_unlock_irqrestore(l,f) do{(void)(f);assert(held);held=false;}while(0)
static u32 regs[512];
static bool done_during_copy;
static void* copy_table(void* d,const void* s,size_t n){void* r=memcpy(d,s,n);if(done_during_copy){done_during_copy=false;regs[RDMA_STATUS]|=1u<<25;}return r;}
#define memcpy copy_table
static u32 READ_VCBUS_REG(u32 r){assert(r<512);return regs[r];}
static void WRITE_VCBUS_REG(u32 r,u32 v){assert(r<512);if(r==RDMA_CTRL)regs[RDMA_STATUS]&=~v;else regs[r]=v;}
#define WRITE_MPEG_REG WRITE_VCBUS_REG
static void WRITE_VCBUS_REG_BITS(u32 r,u32 v,u32 p,u32 n){u32 mask=((1u<<n)-1)<<p;WRITE_VCBUS_REG(r,(regs[r]&~mask)|((v<<p)&mask));}
struct rdma_op_s {void (*irq_cb)(void*);void* arg;};
static int rdma_check_conflict(int h,u32 a,void* p){(void)h;(void)a;(void)p;return 0;}
static void rdma_update_conflict(u32 a,u32 v){(void)a;(void)v;}
@STRUCTS@
static struct rdma_device_info rdma_info;
static int rdma_isr_count;
@METHODS@
static u32 sw[128],dma[128];
static struct rdma_regadr_s regadr={.rdma_ahb_start_addr=110,.rdma_ahb_end_addr=111,
 .trigger_mask_reg=112,.addr_inc_reg=113,.rw_flag_reg=114,
 .clear_irq_bitpos=25,.irq_status_bitpos=25};
static struct rdma_instance_s* ins;
static void rearm(void* unused){(void)unused;assert(!held);rdma_config(1,1);}
static struct rdma_op_s op;
static void setup(void){
 memset(&rdma_info,0,sizeof(rdma_info));memset(regs,0,sizeof(regs));memset(sw,0,sizeof(sw));memset(dma,0,sizeof(dma));
 ins=&rdma_info.rdma_ins[1];ins->rdma_regadr=&regadr;ins->op=&op;op.irq_cb=NULL;
 ins->reg_buf=sw;ins->rdma_table_addr=dma;ins->rdma_table_size=sizeof(dma);ins->rdma_table_phy_addr=0x1000;
 held=false;done_during_copy=false;rdma_frame_tracking(1);
}
static void frame(u64 cookie){u64 s=rdma_frame_begin(1);rdma_write_reg(1,200,(u32)cookie);rdma_write_reg(1,201,99);rdma_frame_end(1,s,cookie,false);}
static void done(void){regs[RDMA_STATUS]|=1u<<25;rdma_mgr_isr(0,NULL);}
'''
TESTS=r'''
int main(void){
 setup();frame(8);assert(!rdma_frame_completed(1));assert(rdma_config(1,1)==1);assert(!rdma_frame_completed(1));
 done();assert(rdma_frame_completed(1)==8); // actual table done, not config
 setup();frame(8);rdma_config(1,1);frame(9);op.irq_cb=rearm;
 done();assert(rdma_frame_completed(1)==8&&ins->frame_armed==9);done();assert(rdma_frame_completed(1)==9);
 setup();u64 s=rdma_frame_begin(1);rdma_write_reg(1,200,3);rdma_config(1,1);rdma_write_reg(1,201,4);
 rdma_frame_end(1,s,8,false);rdma_config(1,1);done();assert(rdma_frame_completed(1)==8); // config deferred until full paused frame
 setup();frame(8);rdma_config(1,1);frame(9);rdma_config(1,1);done();assert(!rdma_frame_completed(1)); // overwrite active batch
 setup();frame(8);regs[RDMA_STATUS]=1u<<25;rdma_config(1,1);done();assert(!rdma_frame_completed(1)); // stale pending status
 setup();frame(8);done_during_copy=true;rdma_config(1,1);done();assert(!rdma_frame_completed(1)); // late status during copy
 setup();frame(8);rdma_config(1,1);rdma_clear(1);done();assert(!rdma_frame_completed(1));
 setup();frame(8);rdma_config(1,1);rdma_reset(0);done();assert(!rdma_frame_completed(1));
 // Paused first frame waits behind an older DMA table over another video IRQ.
 setup();rdma_write_reg(1,202,1);rdma_config(1,1);frame(8);
 s=rdma_frame_begin(1);op.irq_cb=rearm;done();assert(!rdma_frame_completed(1));
 rdma_frame_end(1,s,0,true);rdma_config(1,1);done();assert(rdma_frame_completed(1)==8);
 setup();frame(8);s=rdma_frame_begin(1);rdma_frame_end(1,s,0,false);
 rdma_config(1,1);done();assert(!rdma_frame_completed(1)); // unsupported replacement cancels older candidate
 setup();frame(8);s=rdma_frame_begin(1);ins->rdma_item_count=64;rdma_write_reg(1,201,55);
 rdma_frame_end(1,s,9,false);rdma_config(1,1);done();assert(!rdma_frame_completed(1)); // overflow/direct recovery
 setup();frame(8);rdma_config(1,0x101);done();assert(!rdma_frame_completed(1)); // debug replay is not tagged completion
 setup();frame(8);rdma_config(1,0);done();assert(!rdma_frame_completed(1)); // disabled/empty
 puts("PASS production RDMA frame receipt: pending/armed/completed, callback rearm, deferred/overwritten batch, stale IRQ, clear/reset/overflow, unsupported modes");
 return 0;
}
'''

INTEGRATION = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef unsigned long long u64;
#define CONFIG_AMLOGIC_MEDIA_VSYNC_RDMA 1
#define VFRAME_FLAG_FAKE_FRAME 1
#define VFRAME_FLAG_AMLVIDEO_DISCARD 2
#define VIDTYPE_COMPRESS 4
#define VFM_PATH_AMVIDEO 0
#define VFM_PATH_DEF 1
#define VSYNC_RDMA 0
#define PAGE_SIZE 4096
#define EXPORT_SYMBOL(x)
#define scnprintf snprintf
struct vframe_s {int type,flag,canvas0Addr;bool local,di;};
static struct vframe_s frame,old;
static struct vframe_s* cur_dispbuf;
static struct {struct vframe_s* dispbuf;bool switch_vf,do_switch,vd1_vd2_mux;} vd_layer[1];
static struct {bool need_no_compress;} glayer_info[1];
static int video_unreg_flag;
static u64 presentation_epoch=8,complete=8;
static int atomic_read(int* p){return *p;}
static u64 atomic64_read(u64* p){return *p;}
static bool is_local_vf(struct vframe_s* f){return f->local;}
static bool is_di_post_mode(struct vframe_s* f){return f->di;}
static int second_rdma_feature,cur_enable[1]={1},vsync_rdma_handle[1]={1};
static int ends;static u64 end_cookie;static bool end_keep;
static u64 rdma_frame_begin(int h){assert(h==1);return 7;}
static u64 rdma_frame_completed(int h){assert(h==1);return complete;}
static void rdma_frame_end(int h,u64 s,u64 c,bool keep){assert(h==1&&s==7);++ends;end_cookie=c;end_keep=keep;}
struct class {};struct class_attribute {};
@WRAPPER@
@SOURCE_GATE@
@STATE@
typedef int irqreturn_t;
#define IRQ_HANDLED 1
#define VIDEO_NONE_OP 0
#define VIDEO_MUTE 1
#define VIDEO_UNMUTE 2
#define CONF_VIDEO_MUTE_OP 3
#define WRITE_ONCE(a,b) ((a)=(b))
static __attribute__((unused)) unsigned char exchange(unsigned char* p,unsigned char v){unsigned char old=*p;*p=v;return old;}
#define xchg exchange
struct hdmitx_dev {int hdmi_init;unsigned char vid_mute_op;struct {void (*cntlconfig)(struct hdmitx_dev*,int,int);} hwop;};
static struct hdmitx_dev hdmitx_device;
@MUTE_SETTER@
@MUTE_IRQ@
static int applied_op;static bool inject_new_mute;
static void apply(struct hdmitx_dev* h,int cmd,int op){assert(cmd==CONF_VIDEO_MUTE_OP);assert(h==&hdmitx_device);applied_op=op;if(inject_new_mute){inject_new_mute=false;hdmitx_video_mute_op(0);}}
int main(void){
 frame.canvas0Addr=7;cur_dispbuf=vd_layer[0].dispbuf=&frame;
 assert(presentation_source_supported(&frame,0));
 frame.canvas0Addr=0;assert(!presentation_source_supported(&frame,0));
 frame.type=VIDTYPE_COMPRESS;assert(presentation_source_supported(&frame,0));
 glayer_info[0].need_no_compress=true;assert(!presentation_source_supported(&frame,0));glayer_info[0].need_no_compress=false;
 frame.local=true;assert(!presentation_source_supported(&frame,0));frame.local=false;
 frame.di=true;assert(!presentation_source_supported(&frame,0));frame.di=false;
 frame.flag=VFRAME_FLAG_FAKE_FRAME;assert(!presentation_source_supported(&frame,0));frame.flag=VFRAME_FLAG_AMLVIDEO_DISCARD;assert(!presentation_source_supported(&frame,0));frame.flag=0;
 vd_layer[0].switch_vf=true;assert(!presentation_source_supported(&frame,0));vd_layer[0].switch_vf=false;
 vd_layer[0].do_switch=true;assert(!presentation_source_supported(&frame,0));vd_layer[0].do_switch=false;
 vd_layer[0].vd1_vd2_mux=true;assert(!presentation_source_supported(&frame,0));vd_layer[0].vd1_vd2_mux=false;
 assert(!presentation_source_supported(&frame,2));assert(!presentation_source_supported(&old,0));
 video_unreg_flag=1;assert(!presentation_source_supported(&frame,0));video_unreg_flag=0;
 assert(vsync_rdma_frame_begin()==7);second_rdma_feature=1;vsync_rdma_frame_end(7,8,true);
 assert(ends==1&&end_cookie==0&&!end_keep);assert(!vsync_rdma_frame_begin());assert(!vsync_rdma_frame_completed());second_rdma_feature=0;
 cur_enable[0]=0;assert(!vsync_rdma_frame_begin());vsync_rdma_frame_end(7,8,true);assert(ends==2&&end_cookie==0&&!end_keep);cur_enable[0]=1;
 char buf[PAGE_SIZE];presentation_state_show(NULL,NULL,buf);assert(!strcmp(buf,"1 8 8\n"));
 complete=7;presentation_state_show(NULL,NULL,buf);assert(!strcmp(buf,"1 8 0\n"));
 complete=8;video_unreg_flag=1;presentation_state_show(NULL,NULL,buf);assert(!strcmp(buf,"1 8 0\n"));video_unreg_flag=0;
 hdmitx_device.hdmi_init=1;hdmitx_device.hwop.cntlconfig=apply;hdmitx_video_mute_op(1);inject_new_mute=true;
 vsync_intr_handler(0,&hdmitx_device);assert(applied_op==VIDEO_UNMUTE);assert(hdmitx_device.vid_mute_op==VIDEO_MUTE);
 vsync_intr_handler(0,&hdmitx_device);assert(applied_op==VIDEO_MUTE&&hdmitx_device.vid_mute_op==VIDEO_NONE_OP);
 puts("PASS production source eligibility, provider epoch readout, mode-switch close and HDMI new-request consumption");return 0;
}
"""

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--negative-controls',action='store_true');p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--baseline-hdmi');a=p.parse_args();root=a.root
 s=(root/'drivers/amlogic/media/common/rdma/rdma_mgr.c').read_text()
 structs='\n'.join(function(s,'struct '+n+' {')+';' for n in ['rdma_regadr_s','rdma_instance_s','rdma_conflict_regs_s','rdma_device_info'])
 methods='\n'.join(function(s,sig) for sig in [
  'static void rdma_frame_invalidate(struct rdma_instance_s *ins)\n{',
  'void rdma_frame_tracking(', 'u64 rdma_frame_begin(', 'void rdma_frame_end(', 'u64 rdma_frame_completed(',
  'static void rdma_reset(', 'int rdma_config(', 'int rdma_clear(', 'int rdma_write_reg(', 'irqreturn_t rdma_mgr_isr('])
 source=PRELUDE.replace('@STRUCTS@',structs).replace('@METHODS@',methods)+TESTS
 # Bound production integration: final VD1 gate after blend writes, before config,
 # and provider invalidation while video IRQ is fenced.
 video=(root/'drivers/amlogic/media/video_sink/video.c').read_text()
 irq=function(video,'static irqreturn_t vsync_isr_in(')
 assert irq.index('vpp_blend_update(vinfo);')<irq.index('vsync_rdma_frame_end(')<irq.index('vsync_rdma_process();')
 for sig in ['static void video_vf_unreg_provider(', 'static void video_vf_light_unreg_provider(']:
  f=function(video,sig);assert f.index('while (atomic_read(&video_inirq_flag)')<f.index('atomic64_inc(&presentation_epoch)')<f.index('atomic_dec(&video_unreg_flag)')
 with tempfile.TemporaryDirectory(prefix='video-receipt-') as td:
  td=Path(td)
  def run(src,negative=False):
   (td/'test.c').write_text(src)
   cmd=[os.environ.get('CC','gcc'),'-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-misleading-indentation','-Wno-sign-compare','-Wno-unused-variable',str(td/'test.c'),'-o',str(td/'test')]
   if not negative:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
   subprocess.run(cmd,check=True)
   r=subprocess.run([str(td/'test')],capture_output=True,text=True,timeout=10,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
   if negative:assert r.returncode and 'Assertion' in r.stderr,r.stderr
   else:print(r.stdout,end='');assert not r.returncode,r.stderr
  run(source)
  wrapper=(root/'drivers/amlogic/media/common/rdma/rdma.c').read_text()
  hdmi_path='drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hw/hdmi_tx_hw.c'
  hdmi=(subprocess.check_output(['git','show',f'{a.baseline_hdmi}:{hdmi_path}'],cwd=root,text=True)
        if a.baseline_hdmi else (root/hdmi_path).read_text())
  setter=(root/'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hdmi_tx_main.c').read_text()
  integration=INTEGRATION.replace('@SOURCE_GATE@',function(video,'static bool presentation_source_supported('))
  integration=integration.replace('@WRAPPER@','\n'.join(function(wrapper,f) for f in ['u64 vsync_rdma_frame_begin(', 'void vsync_rdma_frame_end(', 'u64 vsync_rdma_frame_completed(']))
  integration=integration.replace('@STATE@',function(video,'static ssize_t presentation_state_show('))
  integration=integration.replace('@MUTE_SETTER@',function(setter,'void hdmitx_video_mute_op('))
  integration=integration.replace('@MUTE_IRQ@',function(hdmi,'static irqreturn_t vsync_intr_handler('))
  integration='#include <string.h>\n#include <sys/types.h>\n'+integration
  run(integration)
  if a.negative_controls:
   for name,old,new in [
    ('absent source registers admitted','frame->canvas0Addr ||','true ||'),
    ('old provider receipt admitted','applied != epoch','false'),
    ('new HDMI request erased','hdev->hwop.cntlconfig(hdev, CONF_VIDEO_MUTE_OP, mute_op);','hdev->hwop.cntlconfig(hdev, CONF_VIDEO_MUTE_OP, mute_op); hdev->vid_mute_op=VIDEO_NONE_OP;'),
   ]:
    assert old in integration;run(integration.replace(old,new,1),True);print('Rejected:',name)
  if a.negative_controls:
   for name,old,new in [
    ('config mistaken for completion','ins->frame_pending = 0;\n\t\tif (auto_start','ins->frame_completed = ins->frame_armed;\n\t\tins->frame_pending = 0;\n\t\tif (auto_start'),
    ('config copies partial frame','ins->frame_building && trigger_type == RDMA_TRIGGER_VSYNC_INPUT','false'),
    ('inflight overwrite gets receipt','!ins->frame_inflight','true'),
    ('late status gets receipt','if (ins->frame_tracking &&\n\t\t\t    (READ_VCBUS_REG(RDMA_STATUS) &', 'if (false &&\n\t\t\t    (READ_VCBUS_REG(RDMA_STATUS) &'),
    ('held IRQ loses paused receipt','else if (!keep_pending)', 'else if (true)'),
    ('unsupported frame inherits receipt','else if (!keep_pending)', 'else if (false)'),
    ('clear preserves armed tag','\trdma_frame_invalidate(ins);\n\tins->rdma_write_count = 0;','\tins->rdma_write_count = 0;'),
   ]:
    assert old in source,name;run(source.replace(old,new,1),True);print('Rejected:',name)
if __name__=='__main__':main()
