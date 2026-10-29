#!/usr/bin/env python3
"""Production mute ownership/IRQ/mode tables with register and spinlock doubles.
No hardware or physical scanout claims. --baseline REGREV must fail at overwrite.
"""
from pathlib import Path
import argparse,re,subprocess,tempfile

def function(s,marker):
    a=s.index(marker);i=s.index('{',a);depth=1;i+=1
    while depth:
        depth+=(s[i]=='{')-(s[i]=='}');i+=1
    return s[a:i]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',type=Path,default=Path.cwd());ap.add_argument('--baseline');ap.add_argument('--negative-controls',action='store_true');args=ap.parse_args()
    root=args.root;rel='drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/'
    def read(name,baseline=False):
        if args.baseline and baseline:return subprocess.check_output(['git','show',args.baseline+':'+rel+name],cwd=root,text=True)
        return (root/rel/name).read_text()
    enc=read('hw/enc_cfg_hw.c',True);hw=read('hw/hdmi_tx_hw.c',True)
    enc=enc[enc.index('#define MREG_END_MARKER'):]
    mute=hw[hw.index('\tcase CONF_VIDEO_MUTE_OP:'):hw.index('\tcase CONF_CLR_AVI_PACKET:')]
    irq=function(hw,'static irqreturn_t vsync_intr_handler(')
    ownership=read('hdmi_tx_video_mute.h').replace('#include <linux/spinlock.h>','')
    regs=sorted(set(re.findall(r'\bP_[A-Z0-9_]+\b',enc+mute)))
    vics=sorted(set(re.findall(r'\bHDMIV?_[A-Za-z0-9_]+\b',enc)))
    pre=r'''
#include <assert.h>
#include <stdio.h>
#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>
#include <pthread.h>
#include <errno.h>
#include <string.h>
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define pr_info(...) ((void)0)
#define MESON_CPU_ID_SC2 100
#define VIDEO_NONE_OP 0
#define VIDEO_MUTE 1
#define VIDEO_UNMUTE 2
#define CONF_VIDEO_MUTE_OP 0
#define IRQ_HANDLED 1
typedef int irqreturn_t;
#define xchg(p,v) __atomic_exchange_n(p,v,__ATOMIC_SEQ_CST)
#define WRITE_ONCE(p,v) __atomic_store_n(&(p),v,__ATOMIC_SEQ_CST)
static __thread int lock_depth;static bool check_selector_lock;
#define DEFINE_SPINLOCK(n) pthread_mutex_t n=PTHREAD_MUTEX_INITIALIZER
#define spin_lock_irqsave(p,f) do { (f)=0; assert(pthread_mutex_lock(p)==0); ++lock_depth; } while(0)
#define spin_unlock_irqrestore(p,f) do { (void)(f); --lock_depth; assert(pthread_mutex_unlock(p)==0); } while(0)
typedef struct {int counter;} atomic_t;
#define atomic_read(p) __atomic_load_n(&(p)->counter,__ATOMIC_SEQ_CST)
#define atomic_inc(p) __atomic_add_fetch(&(p)->counter,1,__ATOMIC_SEQ_CST)
#define atomic_dec(p) __atomic_sub_fetch(&(p)->counter,1,__ATOMIC_SEQ_CST)
struct device {};struct device_attribute {};
'''
    pre+='enum {'+','.join(regs)+'};\nenum hdmi_vic {'+','.join(vics)+'};\n'
    pre+=r'''
struct reg_s {unsigned reg,val;};
struct video_param {enum hdmi_vic VIC;};
struct hdmitx_dev {struct video_param* cur_video_param; int flag_3dfp,bist_lock,chip_type,hdmi_init;
 unsigned char vid_mute_op; atomic_t kref_video_mute;
 struct {int (*cntlconfig)(struct hdmitx_dev*,unsigned,unsigned);} hwop;};
static unsigned regfile[2048];static bool forbid_normal;
static void hd_write_reg(unsigned reg,unsigned val){
 assert(reg<2048);
 if(check_selector_lock && reg==P_ENCP_VIDEO_MODE_ADV)assert(lock_depth>0);
 if(forbid_normal&&reg==P_ENCP_VIDEO_MODE_ADV)assert(!(val&8));
 regfile[reg]=val;
}
static void hd_set_reg_bits(unsigned reg,unsigned val,unsigned start,unsigned len){
 unsigned mask=((1u<<len)-1)<<start;hd_write_reg(reg,(regfile[reg]&~mask)|((val<<start)&mask));}
'''
    source=pre+enc+r'''
static bool inject_request;
static int configure(struct hdmitx_dev* hdev,unsigned cmd,unsigned argv){switch(cmd){
'''+mute+r'''}
 if(inject_request){inject_request=false;WRITE_ONCE(hdev->vid_mute_op,VIDEO_UNMUTE);}
 return 0;}
'''+irq+ownership+'\nstatic struct hdmitx_dev hdmitx_device;\n'
    tests=r'''
static void drain(void){vsync_intr_handler(0,&hdmitx_device);}
static bool muted(void){return !(regfile[P_ENCP_VIDEO_MODE_ADV]&8)&&regfile[P_VENC_VIDEO_TST_EN]==1;}
static void user(bool on){assert(store_vid_mute(NULL,NULL,on?"1":"0",1)==1);drain();}
static void internal(bool on){hdmitx_video_mute_op(on?0:1);drain();}
static void reset(void){
 hdmitx_device=(struct hdmitx_dev){.hwop={configure},.hdmi_init=1};
 internal(false);assert(!muted());
}
static void* add_users(void* unused){for(int i=0;i<10000;i++)store_vid_mute(NULL,NULL,"1",1);return NULL;}
static void* remove_users(void* unused){for(int i=0;i<10000;i++)store_vid_mute(NULL,NULL,"0",1);return NULL;}
static void test_owners(void){
 reset();user(true);internal(true);internal(false);assert(muted());
 user(true);user(false);assert(muted());user(false);assert(!muted());
 internal(true);user(true);user(false);assert(muted());internal(false);assert(!muted());
 user(false);assert(atomic_read(&hdmitx_device.kref_video_mute)==0);
 assert(store_vid_mute(NULL,NULL,"x",1)==1);assert(store_vid_mute(NULL,NULL,"",0)==0);
 pthread_t a,b;pthread_create(&a,NULL,add_users,NULL);pthread_create(&b,NULL,add_users,NULL);
 pthread_join(a,NULL);pthread_join(b,NULL);assert(atomic_read(&hdmitx_device.kref_video_mute)==20000);drain();assert(muted());
 pthread_create(&a,NULL,remove_users,NULL);pthread_create(&b,NULL,remove_users,NULL);
 pthread_join(a,NULL);pthread_join(b,NULL);assert(atomic_read(&hdmitx_device.kref_video_mute)==0);drain();assert(!muted());
}
static void* toggle_mute(void* unused){for(int i=0;i<10000;i++)configure(&hdmitx_device,CONF_VIDEO_MUTE_OP,i%2?VIDEO_MUTE:VIDEO_UNMUTE);return NULL;}
static void* write_selector(void* unused){struct reg_s r={P_ENCP_VIDEO_MODE_ADV,0x18};for(int i=0;i<10000;i++)setreg(&r);return NULL;}
static void test_selector_race(void){
 reset();pthread_t a,b;check_selector_lock=true;
 pthread_create(&a,NULL,toggle_mute,NULL);pthread_create(&b,NULL,write_selector,NULL);
 pthread_join(a,NULL);pthread_join(b,NULL);check_selector_lock=false;
 assert(muted());configure(&hdmitx_device,CONF_VIDEO_MUTE_OP,VIDEO_UNMUTE);assert(!muted());
}
static void test_mode(enum hdmi_vic mode, bool framepacking){
 reset();struct video_param param={mode};hdmitx_device.cur_video_param=&param;hdmitx_device.flag_3dfp=framepacking;
 set_vmode_enc_hw(&hdmitx_device);user(true);assert(muted());
 forbid_normal=true;set_vmode_enc_hw(&hdmitx_device);forbid_normal=false;assert(muted());
 drain();assert(muted());user(false);assert(!muted());
 set_vmode_enc_hw(&hdmitx_device);assert(regfile[P_ENCP_VIDEO_MODE_ADV]&8);
 hdmitx_device.bist_lock=1;set_vmode_enc_hw(&hdmitx_device);assert(!(regfile[P_ENCP_VIDEO_MODE_ADV]&8));
 hdmitx_device.bist_lock=0;set_vmode_enc_hw(&hdmitx_device);assert(regfile[P_ENCP_VIDEO_MODE_ADV]&8);
}
int main(void){
 test_owners();
 @MODES@
 test_selector_race();
 reset();hdmitx_device.vid_mute_op=VIDEO_MUTE;inject_request=true;drain();
 assert(hdmitx_device.vid_mute_op==VIDEO_UNMUTE);drain();assert(!muted());
 puts("PASS: complete production mode tables, mute/IRQ ordering, HDR/userspace ownership and concurrent references");
}
'''
    # Exercise every progressive encoder table through its actual VIC selection.
    modes=re.findall(r'\{(HDMI_[A-Za-z0-9_]+), tvregs_',enc)
    tests=tests.replace('@MODES@','\n'.join('test_mode('+v+',false);' for v in sorted(set(modes)) if 'p' in v)+'\n'+ '\n'.join('test_mode('+v+',true);' for v in re.findall(r'\{(HDMI_[A-Za-z0-9_]+), tvregs_', enc[enc.index('static struct vic_tvregs_set tvregsTab_3dfp'):enc.index('static inline void setreg')])) )
    def run(src,fail=False):
        with tempfile.TemporaryDirectory(prefix='hdmitx-mute-') as tmp:
            p=Path(tmp);(p/'test.c').write_text(src)
            subprocess.run(['cc','-std=gnu99','-Wall','-Wextra','-Werror','-Wno-sign-compare','-Wno-unused-parameter','-pthread','-fsanitize=undefined','-fno-sanitize-recover=all',str(p/'test.c'),'-o',str(p/'test')],check=True)
            r=subprocess.run([str(p/'test')],capture_output=True,text=True)
            if fail:assert r.returncode!=0,r.stdout
            else:assert r.returncode==0,r.stdout+r.stderr;print(r.stdout.strip())
    for main in ['hdmi_tx_main.c','hdmi_tx_main_drm.c']:
        s=read(main)
        full=source+function(s,'void hdmitx_video_mute_op(')+function(s,'static ssize_t store_vid_mute(')+tests
        print(main,flush=True);run(full)
        if args.negative_controls:
            unlocked=full.replace('spin_lock_irqsave(&video_mute_lock, flags);','flags=0;').replace('spin_unlock_irqrestore(&video_mute_lock, flags);','(void)flags;')
            # Keep the declaration used; no synchronization remains in production paths.
            unlocked=unlocked.replace('static DEFINE_SPINLOCK(video_mute_lock);','')
            run(unlocked,True);print('REJECTED: unserialized selector',flush=True)
            for label,old,new in [
                ('overwrite selector','value &= ~(1U << 3);','value |= (1U << 3);'),
                ('HDR overrides userspace','video_mute_internal || atomic_read(&hdev->kref_video_mute) > 0','video_mute_internal'),
                ('userspace overrides HDR','video_mute_internal || atomic_read(&hdev->kref_video_mute) > 0','atomic_read(&hdev->kref_video_mute) > 0'),
                ('allow underflow','else if (atomic_read(&hdev->kref_video_mute) > 0)','else'),
                ('lose IRQ request','hdev->hwop.cntlconfig(hdev, CONF_VIDEO_MUTE_OP, mute_op);','{ hdev->hwop.cntlconfig(hdev, CONF_VIDEO_MUTE_OP, mute_op); hdev->vid_mute_op=VIDEO_NONE_OP; }')]:
                assert old in full,label
                run(full.replace(old,new),True);print('REJECTED:',label,flush=True)
if __name__=='__main__':main()
