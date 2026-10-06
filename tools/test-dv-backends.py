#!/usr/bin/env python3
"""Exercise actual driver helpers/registration against host kernel-service mocks."""
from pathlib import Path
import argparse, subprocess, re, os, tempfile
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--negative-controls',action='store_true',help='Verify representative unsafe mutations are rejected')
ap.add_argument('--source-root',type=Path,default=Path(__file__).resolve().parents[1])
ap.add_argument('--driver-dir',type=Path,help='Optional staged driver source directory')
a=ap.parse_args();root=a.source_root.resolve()
p=a.driver_dir.resolve() if a.driver_dir else root/'drivers/amlogic/media/enhancement/amdolby_vision'
public=(root/'include/linux/amlogic/media/amdolbyvision/dolby_vision.h').read_text()
public=public[:public.index('void enable_dolby_vision')]
public=re.sub(r'^#include.*\n','',public,flags=re.M)+'\n#endif\n'
private=re.sub(r'^#include.*\n','',(p/'amdolby_vision.h').read_text(),flags=re.M)
abi=re.sub(r'^#include.*\n','',(p/'dv5_compat_abi.h').read_text(),flags=re.M)
driver=(p/'amdolby_vision.c').read_text()
def function(name):
    m=re.search(r'^int '+name+r'\([^;]+?\)\s*\{',driver,re.M)
    assert m,name
    start=m.start();end=m.end();depth=1
    while depth:
        if driver[end]=='{':depth+=1
        if driver[end]=='}':depth-=1
        end+=1
    return driver[start:end]+'\n'
globals_=driver[driver.index('DEFINE_SPINLOCK(dovi_lock);'):driver.index('static void dv_legacy_release(void);')]
helpers=(p/'dv_backend.h').read_text()
format_helpers=driver[driver.index('#define LEVEL_3_LENGTH'):driver.index('static u32 last_total_md_size;')]
start=driver.index('static inline int prepare_dv_meta'); brace=driver.index('{',start);end=brace+1;depth=1
while depth:
    depth+=(driver[end]=='{')-(driver[end]=='}');end+=1
format_helpers=re.search(r'^#define CORE_META_LENGTH.*$',driver,re.M).group(0)+'\n'+driver[start:end]+'\n'+format_helpers
preamble=r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <pthread.h>
#include <unistd.h>
#include <sched.h>
typedef uint8_t u8;typedef uint16_t u16;typedef uint32_t u32;typedef uint64_t u64;
typedef int16_t s16;typedef int32_t s32;typedef uint32_t __le32;typedef unsigned int uint;
struct vframe_s; enum vpu_mod_e {HOST_VPU};enum vd_path_e {HOST_VD};
#define BIT(n) (1U<<(n))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define __aligned(n) __attribute__((aligned(n)))
#define READ_ONCE(x) (x)
#define module_param(...)
#define MODULE_PARM_DESC(...)
#define EXPORT_SYMBOL(...)
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
typedef u32 __be32;
#define be32_to_cpup(p) get_unaligned_be32(p)
static void put_unaligned_le32(u32 n,void*v){u8*p=v;p[0]=n;p[1]=n>>8;p[2]=n>>16;p[3]=n>>24;}
#define DEFINE_SPINLOCK(n) pthread_mutex_t n=PTHREAD_MUTEX_INITIALIZER
#define spin_lock_irqsave(n,f) do{(f)=0;assert(pthread_mutex_lock(n)==0);lock_depth++;assert(lock_depth==1);}while(0)
#define spin_unlock_irqrestore(n,f) do{(void)(f);assert(lock_depth==1);lock_depth--;assert(pthread_mutex_unlock(n)==0);}while(0)
static _Thread_local int lock_depth;
typedef int atomic_t;
#define ATOMIC_INIT(n) (n)
static int atomic_cmpxchg(atomic_t*p,int old,int value){__atomic_compare_exchange_n(p,&old,value,false,__ATOMIC_ACQ_REL,__ATOMIC_ACQUIRE);return old;}
static void atomic_set_release(atomic_t*p,int v){__atomic_store_n(p,v,__ATOMIC_RELEASE);}
static unsigned wait_calls;
static void cond_resched(void){__atomic_add_fetch(&wait_calls,1,__ATOMIC_RELAXED);sched_yield();}

struct module {bool live;int refs;};
static struct module old_owner={true,0},new_owner={true,0};
static unsigned long old_cp_addr,new_cp_addr;
static struct module *__module_address(unsigned long a){if(a==old_cp_addr)return &old_owner;if(a==new_cp_addr)return &new_owner;return NULL;}
static bool try_module_get(struct module*m){assert(lock_depth);if(!m)return true;if(!m->live)return false;m->refs++;return true;}
static void module_put(struct module*m){assert(lock_depth);if(m){assert(m->refs>0);m->refs--;}}
static bool cpu_g12b=true;
static bool is_meson_g12b_cpu(void){return cpu_g12b;}
static u32 get_unaligned_be32(const void *v){const u8*p=v;return (u32)p[0]<<24|(u32)p[1]<<16|(u32)p[2]<<8|p[3];}
static void be32(void *v,u32 n){u8*p=v;p[0]=n>>24;p[1]=n>>16;p[2]=n>>8;p[3]=n;}
struct vinfo_s{u32 sync_duration_num,sync_duration_den,width,height;};
struct provider_aux_req_s{char*aux_buf;int aux_size;};
#define DV_SEI 0x01000000
'''
services=r'''
static void *metadata_parser;
static bool metadata_parser_reset_flag,module_installed;
static int dv_parse_metadata_internal(struct vframe_s*,u8,bool,bool);
static struct hdr10_parameter hdr10_param;
static char old_comp[32784],old_md[4096];
static char *comp_buf[]={old_comp},*md_buf[]={old_md};static int current_id;
static unsigned int xbmc_dv_vp;
static struct vinfo_s host_vinfo={50,1,1920,1080};
static struct vinfo_s *get_current_vinfo(void){return &host_vinfo;}
static bool dolby_vision_on_in_uboot,dolby_vision_on,dolby_vision_wait_on,dolby_vision_wait_init;
static unsigned int current_hdr_cap,current_sink_available;
static bool is_vinfo_available(const struct vinfo_s*v){return v!=NULL;}
static void is_sink_cap_changed(const struct vinfo_s*v,unsigned int*c,unsigned int*s){(void)v;(void)c;(void)s;}
static bool chip_support_dv(void){return true;}
static void *vmalloc(size_t n){return malloc(n);}
static void vfree(void*p){free(p);}
static char *ko_info;
static unsigned int dolby_vision_hdr10_policy,last_dolby_vision_hdr10_policy,efuse_mode,support_info,dolby_vision_run_mode_delay;
#define SDR_BY_DV_F_SINK 1
#define HDR_BY_DV_F_SINK 2
#define RUN_MODE_DELAY_GXM 2
#define DOLBY_TV_CLKGATE_CTRL 0
#define DOLBY_TV_REG_START 0
#define DOLBY_CORE1_REG_START 0
#define READ_VPP_DV_REG(n) 0
#define WRITE_VPP_DV_REG(n,v) ((void)(v))
static bool is_meson_txlx(void){return false;}
static bool is_meson_tm2(void){return false;}
static bool is_meson_g12(void){return true;}
static bool is_meson_txlx_stbmode(void){return false;}
static bool is_meson_tm2_stbmode(void){return false;}
static bool is_meson_sc2(void){return false;}
static bool is_meson_gxm(void){return false;}
static void adjust_vpotch(void){}
static void adjust_vpotch_tv(void){}
static struct dovi_setting_s dovi_setting,new_dovi_setting;
static bool xbmc_meta_level_5=true,xbmc_meta_level_5_osdst,xbmc_meta_level_5_subt,dolby_vision_xbmc_osd,dolby_vision_subtitles;
static bool xbmc_detect_active_area,xbmc_force_l5_override,xbmc_l5_override_additive;
static u16 xbmc_detected_l5_top,xbmc_detected_l5_bottom,xbmc_detected_l5_left,xbmc_detected_l5_right;
static u16 xbmc_override_l5_top,xbmc_override_l5_bottom,xbmc_override_l5_left,xbmc_override_l5_right;
unsigned int debug_dolby;static bool dump_enable;
static int dolby_vision_signal_range=SIGNAL_RANGE_SMPTE;
static void dump_buffer(const char*s,const void*b,size_t n){(void)s;(void)b;(void)n;}

'''
tests=r'''
static unsigned parse_calls,init_calls,reset_calls,release_calls,old_reset_calls,new_reset_calls;
static int parse_ret,cp_ret,reset_ret,md_mode;static bool init_ok=true;
static char *cp_seen_md,*cp_seen_comp;static int cp_seen_md_len,cp_seen_comp_len;
static bool block_cp;static _Thread_local bool exit_owned;
static pthread_mutex_t gate=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t gate_cv=PTHREAD_COND_INITIALIZER;
static bool cp_entered,cp_leave,cp_returned,publish_leave,unreg_started,unreg_done;
static void *fake_init(int f){(void)f;assert(lock_depth&&new_owner.refs);init_calls++;return init_ok?(void*)1:NULL;}
static int fake_reset(int f){assert(lock_depth&&new_owner.refs);assert(f==1);reset_calls++;return reset_ret;}
static void fake_release(void){assert(lock_depth);release_calls++;}
static int fake_parse(char *r,int n,char *c,int *cl,char*m,int *ml,bool e){
 assert(lock_depth&&new_owner.refs);assert(r==dv_new_rpu&&n==8&&e);parse_calls++;
 assert(c==dv_new_comp&&m==dv_new_md);assert(!r[0]&&!r[1]&&!r[2]&&r[3]==2);
 memset(m,0,71);*ml=71;*cl=3;memcpy(c,"new",3);
 if(md_mode==1){m[70]=1;}if(md_mode==2){*ml=4097;}if(md_mode==3){*cl=32785;}
 return parse_ret;
}
static int cp(bool newer,enum signal_format_enum in,enum signal_format_enum out,char*c,int cl,char*m,int ml,enum priority_mode_enum p,int b,int ch,int r,int gm,int gx,int tm,int tx,int ne,struct hdr10_parameter*h,struct dovi_setting_s*s){
 (void)out;(void)p;(void)b;(void)ch;(void)r;(void)gm;(void)gx;(void)tm;(void)tx;(void)ne;(void)h;
 assert(lock_depth);assert(newer?new_owner.refs||exit_owned:old_owner.refs);
 if(in==FORMAT_INVALID){assert(s==&dv_reset_setting);if(newer)new_reset_calls++;else old_reset_calls++;s->video_width=777;return 0;}
 cp_seen_md=m;cp_seen_comp=c;cp_seen_md_len=ml;cp_seen_comp_len=cl;
 if(block_cp){assert(pthread_mutex_lock(&gate)==0);cp_entered=true;pthread_cond_broadcast(&gate_cv);while(!cp_leave)pthread_cond_wait(&gate_cv,&gate);pthread_mutex_unlock(&gate);}
 s->video_width=4321;return cp_ret;
}
#define CPARGS enum signal_format_enum i,enum signal_format_enum o,char*c,int cl,char*m,int ml,enum priority_mode_enum p,int b,int ch,int r,int gm,int gx,int tm,int tx,int ne,struct hdr10_parameter*h,struct dovi_setting_s*s
#define CPVALS i,o,c,cl,m,ml,p,b,ch,r,gm,gx,tm,tx,ne,h,s
static int old_cp(CPARGS){return cp(false,CPVALS);}
static int new_cp(CPARGS){return cp(true,CPVALS);}
static struct dolby_vision_func_s oldf,newf;
static int run(bool newer,struct dovi_setting_s*s){return dv_run_control_path(newer,FORMAT_DOVI,FORMAT_DOVI,newer?dv_new_comp:old_comp,newer?dv_new_comp_size:0,newer?dv_new_md:old_md,newer?dv_new_md_size:71,0,12,0,0,0,0,0,0,1,&hdr10_param,s);}
static char aux[64];static struct provider_aux_req_s req={aux,14};
static void setup(void){
 old_cp_addr=(unsigned long)old_cp;new_cp_addr=(unsigned long)new_cp;
 newf.version_info="mock";newf.control_path=new_cp;newf.metadata_parser_init=fake_init;newf.metadata_parser_reset=fake_reset;newf.metadata_parser_process=fake_parse;newf.metadata_parser_release=fake_release;
 oldf=newf;oldf.control_path=old_cp;dolby_vision_probe_ok=1;
 be32(aux,6);be32(aux+4,DV_SEI);memcpy(aux+8,"\1\2\3\4\5\6",6);
}
static void test_registration(void){
 assert(register_dv_functions_multi(&newf)==-ENODEV);assert(register_dv_functions(NULL)==-EINVAL);
 struct dolby_vision_func_s bad=newf;bad.metadata_parser_release=NULL;assert(register_dv_functions_multi(&bad)==-EINVAL);
 assert(register_dv_functions(&oldf)==0);assert(p_funcs_stb==&oldf&&module_installed);
 old_owner.live=false;assert(register_dv_functions_multi(&newf)==-ENODEV);assert(!p_funcs_new&&!old_owner.refs);old_owner.live=true;
 unsigned info=support_info;support_info&=~1U;assert(register_dv_functions_multi(&newf)==-ENODEV);support_info=info;
 assert(register_dv_functions_multi(&newf)==0);assert(old_owner.refs==1&&dv_new_legacy_ref&&dv_new_backend_available);
 assert(register_dv_functions_multi(&newf)==-EBUSY);assert(old_owner.refs==1);
 assert(unregister_dv_functions()==-EBUSY);assert(p_funcs_stb==&oldf&&module_installed&&old_owner.refs==1);

}
static void test_routes(void){
 xbmc_dv_source_native=true;struct vinfo_s v={50000,1000,1920,1080};assert(dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));
 v.sync_duration_num=50001;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));
 v.sync_duration_num=59940;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.sync_duration_num=60000;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));
 v.sync_duration_num=24000;assert(dv_new_route(FORMAT_DOVI_LL,FORMAT_DOVI,&v));v.sync_duration_num=25000;assert(dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));
 v.sync_duration_den=0;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.sync_duration_den=1000;
 v.sync_duration_num=0;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.sync_duration_num=25000;
 v.width=0;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.width=1920;v.height=0;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.height=1080;
 v.width=16385;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.width=1920;v.height=16385;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.height=1080;
 v.width=16384;v.height=16384;assert(dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));v.width=1920;v.height=1080;
 assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,NULL));assert(!dv_new_route(FORMAT_HDR10,FORMAT_DOVI,&v));assert(!dv_new_route(FORMAT_DOVI,FORMAT_SDR,&v));
 dv_new_blob_enable=false;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_blob_enable=true;
 dv_new_runtime_failed=true;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_runtime_failed=false;
 dv_new_backend_available=false;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_backend_available=true;
 xbmc_dv_source_native=false;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));xbmc_dv_source_native=true;
 cpu_g12b=false;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));cpu_g12b=true;
 xbmc_dv_vp=1;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));xbmc_dv_vp=0;
 dv_new_blob_max_hz=0;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_blob_max_hz=51;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_blob_max_hz=50;
 dv_new_signal_range=-2;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_signal_range=2;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));
 dv_new_signal_range=-1;dolby_vision_signal_range=SIGNAL_RANGE_SDI;assert(!dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_signal_range=0;assert(dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_signal_range=1;assert(dv_new_route(FORMAT_DOVI,FORMAT_DOVI,&v));dv_new_signal_range=-1;dolby_vision_signal_range=SIGNAL_RANGE_SMPTE;
}
static void test_raw(void){
 memset(old_md,0x6a,sizeof(old_md));memset(old_comp,0x6b,sizeof(old_comp));metadata_parser=(void*)42;
 assert(dv_prepare_new(&req,false,false));assert(parse_calls==1&&init_calls==1&&reset_calls==1&&dv_new_md_size==71&&dv_new_comp_size==3);
 assert(metadata_parser==(void*)42&&old_md[0]==0x6a&&old_comp[0]==0x6b&&old_owner.refs==1&&!new_owner.refs);
 assert(dv_prepare_new(NULL,false,true));assert(parse_calls==1);
 assert(dv_prepare_new(&req,true,false));assert(parse_calls==2&&!dv_new_md_size&&!dv_new_comp_size);
 assert(dv_prepare_new(&req,false,true));assert(parse_calls==3&&init_calls==1);
 int len=req.aux_size;req.aux_size=15;assert(!dv_prepare_new(&req,false,false));req.aux_size=len;
 be32(aux,99);assert(!dv_prepare_new(&req,false,false));be32(aux,6);
 memcpy(aux+14,aux,14);req.aux_size=28;assert(!dv_prepare_new(&req,false,false));req.aux_size=14;
 be32(aux+4,99);assert(!dv_prepare_new(&req,false,false));be32(aux+4,DV_SEI);
 for(int i=1;i<=3;i++){md_mode=i;assert(!dv_prepare_new(&req,false,false));assert(!dv_new_parser_ready&&!dv_new_md_size);}md_mode=0;
 parse_ret=-EINVAL;assert(!dv_prepare_new(&req,false,false));parse_ret=0;
 reset_ret=-EINVAL;assert(!dv_prepare_new(&req,false,false));assert(!dv_new_parser_ready);reset_ret=0;
 init_ok=false;assert(!dv_prepare_new(&req,false,false));init_ok=true;
 new_owner.live=false;assert(!dv_prepare_new(&req,false,false));new_owner.live=true;
 assert(dv_prepare_new(&req,false,false));
}
static void test_ll(void){
 char m[4096]={0},out[4096],before[4096];m[70]=1;be32(m+71,3);m[75]=1;m[76]=1;m[77]=2;m[78]=3;memcpy(before,m,sizeof(m));
 assert(dv_new_metadata_valid(m,79));int n=dv_new_ll_metadata(m,79,out);assert(n==92&&out[70]==2&&out[83]==5);assert(!memcmp(m,before,sizeof(m)));
 m[70]=2;be32(m+79,7);m[83]=5;assert(!dv_new_metadata_valid(m,91));assert(dv_new_ll_metadata(m,91,out)==-EINVAL);
 be32(m+79,8);memset(m+84,0x77,8);assert(dv_new_metadata_valid(m,92));assert(dv_new_ll_metadata(m,92,out)==92);for(int i=84;i<92;i++)assert(!out[i]);
 m[70]=3;assert(!dv_new_metadata_valid(m,92));m[70]=2;be32(m+79,0xffffffff);assert(!dv_new_metadata_valid(m,92));assert(dv_new_ll_metadata(m,92,out)==-EINVAL);
 assert(dv_new_ll_metadata(m,70,out)==-EINVAL);assert(dv_new_ll_metadata(m,4097,out)==-EINVAL);
}
static void test_source_copy(void){
 unsigned char m[4096]={0},out[CORE_META_LENGTH]={0};struct md_reg_ipcore3 core={0};
 m[70]=2;be32(m+71,3);m[75]=1;be32(m+79,8);m[83]=5;m[85]=2;m[87]=4;m[89]=6;m[91]=8;
 assert(dv_new_metadata_valid((char*)m,92));prepare_dv_meta(&core,m,71);source_meta_copy(m,92,&core);
 assert(reverse_dv_meta(out,&core)==92);assert(!memcmp(m+71,out+71,21));
 xbmc_detect_active_area=true;xbmc_force_l5_override=true;xbmc_l5_override_additive=true;xbmc_override_l5_top=10;xbmc_override_l5_bottom=20;xbmc_override_l5_left=30;xbmc_override_l5_right=40;
 prepare_dv_meta(&core,m,71);source_meta_copy(m,92,&core);assert(reverse_dv_meta(out,&core)==92);assert(out[85]==32&&out[87]==44&&out[89]==16&&out[91]==28);
 dolby_vision_xbmc_osd=true;xbmc_meta_level_5_osdst=true;prepare_dv_meta(&core,m,71);source_meta_copy(m,92,&core);reverse_dv_meta(out,&core);for(int i=84;i<92;i++)assert(!out[i]);
 dolby_vision_xbmc_osd=false;xbmc_meta_level_5_osdst=false;xbmc_detect_active_area=false;xbmc_force_l5_override=false;xbmc_l5_override_additive=false;
 memset(m,0,sizeof(m));m[70]=1;be32(m+71,CORE_META_LENGTH-76);m[75]=1;assert(dv_new_metadata_valid((char*)m,CORE_META_LENGTH));prepare_dv_meta(&core,m,71);struct md_reg_ipcore3 saved=core;source_meta_copy(m,CORE_META_LENGTH,&core);assert(!memcmp(&core,&saved,sizeof(core)));
 /* Prepending L5 must recheck the following block against reduced capacity. */
 memset(m,0,sizeof(m));m[70]=2;be32(m+71,3);m[75]=1;be32(m+79,CORE_META_LENGTH-84);m[83]=7;assert(dv_new_metadata_valid((char*)m,CORE_META_LENGTH));source_meta_copy(m,CORE_META_LENGTH,&core);assert(!memcmp(&core,&saved,sizeof(core)));
 /* Incomplete trailing input and short L5 preserve the generated packet. */
 m[70]=1;source_meta_copy(m,80,&core);assert(!memcmp(&core,&saved,sizeof(core)));be32(m+79,7);m[83]=5;source_meta_copy(m,91,&core);assert(!memcmp(&core,&saved,sizeof(core)));
 assert(prepare_dv_meta(NULL,m,71)==-EINVAL);assert(prepare_dv_meta(&core,NULL,71)==-EINVAL);assert(prepare_dv_meta(&core,m,0)==-EINVAL);assert(prepare_dv_meta(&core,m,CORE_META_LENGTH+1)==-EINVAL);
 saved=core;core.raw_metadata[0]=70<<8;assert(!reverse_dv_meta(out,&core));core=saved;core.size=513;assert(!reverse_dv_meta(out,&core));core=saved;
 /* Wider source metadata is bounded by the real Core3 packet capacity. */
 memset(m,0,sizeof(m));m[70]=1;be32(m+71,1000);m[75]=1;source_meta_copy(m,1076,&core);assert(reverse_dv_meta(out,&core)==1089&&out[70]==2);

}
static void test_cp(void){
 struct dovi_setting_s s={0},before; s.video_width=123;before=s;cp_ret=-EINVAL;
 assert(run(true,&s)==-EINVAL);assert(!memcmp(&s,&before,sizeof(s)));assert(old_reset_calls==1&&dv_context_new);
 cp_ret=0;assert(run(true,&s)==0);assert(s.video_width==4321&&cp_seen_md==dv_new_md&&cp_seen_comp==dv_new_comp&&cp_seen_md_len==71&&cp_seen_comp_len==3);
 s.use_ll_flag=1;assert(run(true,&s)==0);assert(cp_seen_md==dv_new_cp_md);s.use_ll_flag=0;
 before=s;cp_ret=-EINVAL;assert(run(false,&s)==-EINVAL);assert(!memcmp(&s,&before,sizeof(s)));assert(new_reset_calls==1&&!dv_context_new&&!dv_new_parser_ready);
 cp_ret=0;assert(run(false,&s)==0);assert(cp_seen_md==old_md&&cp_seen_comp==old_comp);
 assert(dv_prepare_new(&req,false,false));s.use_ll_flag=1;dv_new_md_size=70;before=s;assert(run(true,&s)==-EINVAL);assert(!memcmp(&s,&before,sizeof(s)));dv_new_md_size=71;s.use_ll_flag=0;
}
static int dv_parse_metadata_internal(struct vframe_s*v,u8 t,bool b,bool d){
 (void)v;(void)t;(void)b;(void)d;struct dovi_setting_s s={0};int ret=run(true,&s);
 pthread_mutex_lock(&gate);cp_returned=true;pthread_cond_broadcast(&gate_cv);while(!publish_leave)pthread_cond_wait(&gate_cv,&gate);pthread_mutex_unlock(&gate);
 assert(p_funcs_new&&dv_new_md_size==71);dv_pending_new=true;return ret;
}
static void *thread_cp(void*x){(void)x;assert(dolby_vision_parse_metadata(NULL,0,false,false)==0);return NULL;}
static void *thread_unreg(void*x){(void)x;pthread_mutex_lock(&gate);unreg_started=true;pthread_cond_broadcast(&gate_cv);pthread_mutex_unlock(&gate);exit_owned=true;assert(unregister_dv_functions_multi()==0);exit_owned=false;pthread_mutex_lock(&gate);unreg_done=true;pthread_cond_broadcast(&gate_cv);pthread_mutex_unlock(&gate);return NULL;}
static void test_quiescence(void){
 pthread_t call,unreg;block_cp=true;cp_ret=0;assert(!pthread_create(&call,NULL,thread_cp,NULL));
 pthread_mutex_lock(&gate);while(!cp_entered)pthread_cond_wait(&gate_cv,&gate);pthread_mutex_unlock(&gate);
 assert(new_owner.refs==1&&old_owner.refs==1);assert(!pthread_create(&unreg,NULL,thread_unreg,NULL));
 pthread_mutex_lock(&gate);while(!unreg_started)pthread_cond_wait(&gate_cv,&gate);assert(!unreg_done);cp_leave=true;pthread_cond_broadcast(&gate_cv);while(!cp_returned)pthread_cond_wait(&gate_cv,&gate);pthread_mutex_unlock(&gate);
 while(!__atomic_load_n(&wait_calls,__ATOMIC_RELAXED)){pthread_mutex_lock(&gate);assert(!unreg_done);pthread_mutex_unlock(&gate);sched_yield();}
 pthread_mutex_lock(&gate);assert(!unreg_done&&p_funcs_new&&!new_owner.refs&&dv_new_md_size==71);assert(dolby_vision_parse_metadata(NULL,0,false,false)==-EBUSY);publish_leave=true;pthread_cond_broadcast(&gate_cv);pthread_mutex_unlock(&gate);
 assert(!pthread_join(call,NULL)&&!pthread_join(unreg,NULL));assert(unreg_done&&!p_funcs_new&&!dv_new_backend_available&&!new_owner.refs&&!old_owner.refs&&!dv_new_legacy_ref&&!dv_context_new&&!dv_new_parser_ready);
 assert(unregister_dv_functions_multi()==0);metadata_parser=NULL;assert(unregister_dv_functions()==0);assert(!p_funcs_stb&&!module_installed&&!ko_info);
}
int main(void){setup();test_registration();test_routes();test_raw();test_ll();test_source_copy();test_cp();test_quiescence();puts("PASS: extracted routing/raw/LL/L5 metadata, CP rollback, registration ownership and whole-transaction unregister quiescence");return 0;}
'''
observation=driver[driver.index('static DEFINE_SPINLOCK(dv_backend_lock);'):driver.index('static void apply_stb_core_settings\n')]
combined=preamble+public+private+abi+globals_+observation+services+helpers+format_helpers+''.join(function(n) for n in ['register_dv_functions','unregister_dv_functions','register_dv_functions_multi','unregister_dv_functions_multi','dolby_vision_parse_metadata'])+tests
flags=['-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-function','-Wno-unused-variable','-Wno-unused-parameter','-Wno-sign-compare','-Wno-unused-but-set-variable','-fsanitize=address,undefined','-fno-omit-frame-pointer','-pthread']
variants=[('production',combined)]
if a.negative_controls:
    mutations=[
      ('integer_refresh','(u64)vinfo->sync_duration_num <=\n\t\t(u64)READ_ONCE(dv_new_blob_max_hz) * vinfo->sync_duration_den','vinfo->sync_duration_num / vinfo->sync_duration_den <= dv_new_blob_max_hz'),
      ('lost_legacy_lifetime','dv_new_legacy_ref = true;','dv_new_legacy_ref = false;'),
      ('unregister_without_transaction','dv_wait_transaction();','/* deliberately unsafe negative control */'),
      ('publish_failed_output','if (ret >= 0)\n\t\t*setting = dv_cp_candidate;','*setting = dv_cp_candidate;'),
      ('reverse_word_count','dw_needed = 1 + (byte_size - 1 + 3) / 4;','dw_needed = (byte_size + 3) / 4;'),
      ('unchecked_l5_append','if (remaining_space < LEVEL_5_LENGTH || num_levels == 255)\n      return;','if (num_levels == 255)\n      return;'),
    ]
    for name,old,new in mutations:
        assert old in combined,name
        variants.append((name,combined.replace(old,new)))
with tempfile.TemporaryDirectory(prefix='dv-backend-test-') as td:
    for name,code in variants:
        c=Path(td)/(name+'.c');binary=Path(td)/name;c.write_text(code)
        subprocess.run(['gcc',*flags,str(c),'-o',str(binary)],check=True)
        result=subprocess.run([str(binary)],env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},capture_output=name!='production',text=True)
        if name=='production':result.check_returncode()
        else:
            assert result.returncode!=0,'Unsafe negative control escaped: '+name
            assert 'Assertion' in result.stderr or 'AddressSanitizer' in result.stderr,'Unexpected failure: '+name+' '+result.stderr
            print('PASS negative control:',name)
