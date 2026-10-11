#!/usr/bin/env python3
"""Execute byte-extracted Core2 apply/set/enable-disable and publication with host I/O.
No device access. Distinct LUT words and recorded write requests are the oracle.
"""
import argparse, hashlib, json, os, re, resource, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=None

def block(src,sig,semicolon=False):
    start=src.index(sig); p=src.index('{',start); depth=1; end=p+1
    while depth:
        depth+=(src[end]=='{')-(src[end]=='}'); end+=1
    return src[start:end]+(';' if semicolon else '')

def replace_once(src,old,new):
    assert src.count(old)==1,(old,src.count(old))
    return src.replace(old,new)

PRE=r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include <map>
using u8=uint8_t;using u16=uint16_t;using u32=uint32_t;using u64=uint64_t;
#define WRITE_ONCE(a,b) ((a)=(b))
#define pr_dolby_dbg(...) ((void)0)
#define pr_info(...) ((void)0)
#define BIT(x) (1U<<(x))
'''
STUBS=r'''
static_assert(sizeof(dm_lut_ipcore)==1280*sizeof(u32));
static_assert(sizeof(dm_reg_ipcore2)==24*sizeof(u32));
static dovi_setting_s dovi_setting{},new_dovi_setting{};
static bool observing=false;
static bool dolby_vision_on=true,force_reset_core2=false,force_set_lut=false;
static bool dv_pending_new=true,stb_core2_const_flag=false;
static u32 amdv_multi_dv_mode=1,stb_core_setting_update_flag=0;
static u32 dolby_vision_flags=0,dolby_vision_mask=7,dolby_vision_core2_on_cnt=DV_CORE2_RECONFIG_CNT;
static u32 first_reseted=0,force_update_reg=0,dolby_vision_on_count=5;
static u32 dolby_vision_run_mode_delay=0; // G12B native path fixture.
static u32 debug_dolby=0,xbmc_dv_vp=0,xbmc_dv_vp_tm=0;
static u32 g_htotal_add=64,g_vtiming=0,g_vtotal_add=0,g_vsize_add=0;
static u32 g_hwidth=8,g_vwidth=8,g_hpotch=8,g_vpotch=8;
static u32 osd_graphic_width=1920,osd_graphic_height=1080;
static u32 dv_cert_graphic_width=1920,dv_cert_graphic_height=1080;
static bool dv_applied_l11=false,dv_pending_l11=false;
static int dv_applied_vsif=0,dv_pending_vsif=0,dv_applied_ci=0,dv_pending_ci=0;
static int cur_mute_type=0,dolby_vision_mode=DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL;
static bool dovi_setting_video_flag=true,dolby_vision_core1_on=true;
static bool dolby_vision_wait_on=false,dolby_vision_wait_init=false,dolby_vision_on_in_uboot=false;
static u32 dolby_ctrl_backup=0,viu_misc_ctrl_backup=0,vpp_matrix_backup=0,vpp_dummy1_backup=0;
static u32 vpp_data_conv_para0_backup=0,vpp_data_conv_para1_backup=0,setting_update_count=0;
static int efuse_mode=0,xbmc_dv_deep_color=0,dv_ll_output_mode=0;
static int last_dolby_vision_ll_policy=0,dolby_vision_ll_policy=0;
static u32 dolby_vision_core1_on_cnt=0,frame_count=0,vsync_count=0;
static u64 el_stall_pts_us64=0;
static u32 el_stall_count=0,el_absent_frames=0,dolby_vision_wait_count=0;
static bool el_track_ended=false;
static u32 core1_disp_hsize=0,core1_disp_vsize=0,dolby_vision_status=3;
static u32 dolby_vision_target_mode=1,dolby_vision_src_format=0,dolby_vision_enable=1;
static u32 cur_csc_type[1]={0};
static char dma_vaddr[8*STB_DMA_TBL_SIZE];
static u32 sdr_degamma[256]={1}; // VP branch disabled for native DV fixture.
static const char* mute_type_str[]={"none"};
struct vinfo_s{u32 width=3840,height=2160,field_height=2160,sync_duration_num=24,sync_duration_den=1;};
static vinfo_s display;
static const vinfo_s* get_current_vinfo(){return &display;}
static bool is_meson_box2(){return true;}static bool is_meson_box(){return false;}
static bool is_meson_gxm(){return false;}static bool is_meson_txlx(){return false;}
static bool is_meson_txlx_stbmode(){return false;}static bool is_meson_tm2_stbmode(){return false;}
static bool is_meson_sc2(){return false;}static bool is_meson_g12(){return true;}
static bool is_dolby_vision_stb_mode(){return true;}
static void adjust_vpotch(){}static bool need_skip_cvm(int){return false;}
static int get_vpu_mem_pd_vmod(int){return 0;}static int get_dv_mem_power_flag(int){return 0;}
static void dv_mem_power_on(int){}
static int reset_calls=0,context_calls=0,backend_reset_calls=0,power_off_core2=0;
static void dolby_core_reset(int core){assert(core==DOLBY_CORE2A);++reset_calls;}
static void dv_mem_power_off(int core){if(core==VPU_DOLBY2)++power_off_core2;}
static void destroy_context(){++context_calls;} // Opaque parser/CP reset boundary, not correctness modeled.
static void dolby_vision_backend_reset(){++backend_reset_calls;}
static void dv_backend_publish(bool,int,u32,u32){observing=false;}
static void osd_bypass(int){}static void hdr_osd_off(){}static void hdr_vd1_off(){}
static void set_hdr_module_status(int,int){}static void video_effect_bypass(int){}
static void enable_rgb_to_yuv_matrix_for_dvll(int,u32*,int){}static void osd_path_enable(int){}
static int get_mute_type(){return 0;}static int get_video_mute(){return VIDEO_MUTE_OFF;}
static int dolby_core1_set(u32*,u32*,u32*,int,int,int,int,int,bool,bool){return 0;}
static int stb_dolby_core1_set(u32*,u32*,u32*,int,int,int,int,int,bool,bool){return 0;}
static void dolby_core3_set(u32,u32*,u32*,u32,u32,bool,u8){}
struct Write{u32 address,value;};
static std::vector<Write> writes;
static std::vector<u32> dma;
static std::map<u32,u32> regs;
static dm_lut_ipcore old_at_entry;
static u32 combined_flags=0;
static void write_reg(u32 address,u32 value){
  if(observing){
    assert(!memcmp(&dovi_setting.dm_lut2,&old_at_entry,sizeof(old_at_entry)));
    if(address==DOLBY_CORE2A_CLKGATE_CTRL)combined_flags=stb_core_setting_update_flag;
  }
  writes.push_back({address,value});regs[address]=value;
  if(address==DOLBY_CORE2A_DMA_PORT)dma.push_back(value);
}
#define VSYNC_WR_DV_REG(a,v) write_reg(a,v)
#define VSYNC_WR_DV_REG_BITS(a,v,s,n) write_reg(a,(regs[a]&~(((1U<<(n))-1)<<(s)))|(((v)&((1U<<(n))-1))<<(s)))
#define VSYNC_RD_DV_REG(a) regs[a]
#define VSYNC_WR_MPEG_REG(a,v) write_reg(a,v)
'''
TESTS=r'''
static dm_lut_ipcore curve(u32 tag){dm_lut_ipcore out;u32*p=(u32*)&out;for(int i=0;i<1280;++i)p[i]=(tag<<20)|i;return out;}
static void pending(const dm_lut_ipcore& lut){
 new_dovi_setting=dovi_setting;new_dovi_setting.dm_lut2=lut;
 new_dovi_setting.video_width=3840;new_dovi_setting.video_height=2160;
 new_dovi_setting.src_format=FORMAT_DOVI;
}
static bool same(const dm_lut_ipcore&a,const dm_lut_ipcore&b){return !memcmp(&a,&b,sizeof(a));}
static void begin(){writes.clear();dma.clear();old_at_entry=dovi_setting.dm_lut2;observing=true;combined_flags=0;}
static void completed(const dm_lut_ipcore& expected,u32 flags){
 observing=false;assert(combined_flags==flags);assert(stb_core_setting_update_flag==0);
 assert(same(new_dovi_setting.dm_lut2,expected));assert(same(dovi_setting.dm_lut2,expected));
 assert(new_dovi_setting.video_width==0&&new_dovi_setting.video_height==0);
}
static void full_dma(const dm_lut_ipcore& expected){
 assert(dma.size()==1280);const u32*p=(const u32*)&expected;
 for(size_t i=0;i<1280;++i)assert(dma[i]==p[(i&~3U)+(3-(i&3U))]);
}
static void submit(unsigned flag){
 cp_accept(flag);begin();production_frame();
}
static int dm_writes(){int n=0;for(auto w:writes)if(w.address>=DOLBY_CORE2A_REG_START+6&&w.address<DOLBY_CORE2A_REG_START+30)++n;return n;}
int main(int argc,char**argv){
 assert(argc==3);dv_pending_new=atoi(argv[1]);amdv_multi_dv_mode=dv_pending_new;
 const bool adjacent=!strcmp(argv[2],"adjacent");
 auto O=curve(1),A=curve(2),invalid=curve(3),C=curve(4);
 dovi_setting.dm_lut2=O;dovi_setting.dm_reg2.vdr_res=(1080U<<16)|1920;
 // Startup fixture is already running beyond reconfiguration, so CONST cannot be masked by reset.
 pending(A);submit(CP_FLAG_CHANGE_TC2);completed(A,CP_FLAG_CHANGE_TC2);full_dma(A);
 assert(reset_calls==0);assert(!stb_core2_const_flag);
 pending(invalid);new_dovi_setting.dm_reg2.s_range=0x1234;
 submit(CP_FLAG_CONST_TC2|CP_FLAG_CHANGE_TC2);
 completed(A,CP_FLAG_CONST_TC2|CP_FLAG_CHANGE_TC2);assert(stb_core2_const_flag);assert(dma.empty());
 assert(dm_writes()==1); // DM changes do not transfer the invalid curve.
 if(adjacent){
  pending(C);submit(CP_FLAG_CHANGE_TC2);completed(C,CP_FLAG_CONST_TC2|CP_FLAG_CHANGE_TC2);
  const auto first_dma=dma;const size_t first_words=dma.size();const u32 first_flags=combined_flags;
  pending(C);submit(0);completed(C,CP_FLAG_CHANGE_TC2);
  printf("backend=%d adjacent observation: current CHANGE cached C; combined flags=0x%x; first DMA=%zu; following zero-flags DMA=%zu; reset calls=%d\n",dv_pending_new,first_flags,first_words,dma.size(),reset_calls);fflush(stdout);
  // Current valid CHANGE may not publish a distinct curve without a corresponding transfer request.
  assert(first_words==1280);
  const u32*words=(const u32*)&C;
  for(size_t i=0;i<1280;++i)assert(first_dma[i]==words[(i&~3U)+(3-(i&3U))]);
  if(dv_pending_new)assert(dma.empty());else full_dma(C); // Historical CHANGE retry stays intact.
  puts("adjacent CONST-to-CHANGE: PASS");return 0;
 }
 int contexts=context_calls,backend=backend_reset_calls,resets=reset_calls;
 enable_dolby_vision(0);
 assert(!dolby_vision_on&&!dolby_vision_core1_on&&force_reset_core2);
 assert(dolby_vision_core2_on_cnt==0&&dolby_vision_on_count==0);
 assert(context_calls==contexts+1&&backend_reset_calls==backend+1&&power_off_core2==1);
 assert(stb_core_setting_update_flag==CP_FLAG_CHANGE_ALL&&!stb_core2_const_flag);
 dovi_setting_s empty{};empty.src_format=FORMAT_SDR;
 assert(!memcmp(&dovi_setting,&empty,sizeof(empty)));
 // cp_accept uses the actual |= flag statement: CHANGE_ALL from disable is preserved.
 pending(C);submit(CP_FLAG_CHANGE_TC2);completed(C,CP_FLAG_CHANGE_ALL);full_dma(C);
 assert(reset_calls==resets+1&&!force_reset_core2);assert(dm_writes()==24);
 assert(dolby_vision_on&&amdv_multi_dv_mode==(u32)dv_pending_new);
 // Preserve static feedback. Current CONST must own the decision even when prior CHANGE_ALL is ORed.
 pending(invalid);submit(CP_FLAG_CONST_TC2|CP_FLAG_CHANGE_TC2);completed(C,CP_FLAG_CHANGE_ALL);full_dma(C);
 assert(stb_core2_const_flag&&dolby_vision_core2_on_cnt==1);
 pending(invalid);submit(0);completed(C,CP_FLAG_CONST_TC2|CP_FLAG_CHANGE_TC2);full_dma(C);
 assert(dolby_vision_core2_on_cnt==2);
 // Observe steady-state after the actual 120-application reconfiguration window.
 while(dolby_vision_core2_on_cnt<DV_CORE2_RECONFIG_CNT){pending(invalid);submit(0);completed(C,0);full_dma(C);}
 pending(invalid);submit(0);completed(C,0);assert(dma.empty());
 printf("backend=%d lifecycle PASS: retained LUT; 1280 ordered restart words; 24 reset DM; flag/cache lifetime; 120 reconfig applications\n",dv_pending_new);
}
'''

def generate(text):
    pub=(ROOT/'include/linux/amlogic/media/amdolbyvision/dolby_vision.h').read_text()
    private=(ROOT/'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.h').read_text()
    types='\n'.join(block(src,sig,True) for src,sig in [
        (pub,'struct composer_reg_ipcore {'),(pub,'struct dm_reg_ipcore1 {'),(pub,'struct dm_lut_ipcore {'),
        (private,'enum signal_format_enum {'),(private,'enum graphics_format_enum  {'),
        (private,'struct dm_reg_ipcore2 {'),(private,'struct dm_reg_ipcore3 {'),(private,'struct md_reg_ipcore3 {'),
        (private,'struct hdr10_infoframe {')]+[(private,'struct '+name+' {') for name in ['ext_level_1','ext_level_2','ext_level_4','ext_level_5','ext_level_6','ext_level_255','ext_md_s','dovi_setting_s']])
    setter=block(text,'static int dolby_core2_set\n')
    apply=block(text,'static void apply_stb_core_settings\n')
    disable=block(text,'void enable_dolby_vision(int enable)')
    process=block(text,'static int dv_process_internal(')
    start=process.index('\t\t\tapply_stb_core_settings(\n\t\t\t\tdovi_setting_video_flag,')
    end=process.index('\n\t\t\tif (dovi_setting_video_flag && dolby_vision_on_count == 0)',start)
    caller=process[start:end]
    counter_start=process.index('\tif (dolby_vision_core1_on) {\n\t\tif (dolby_vision_on_count <=')
    counter=process[counter_start:process.index('\n\treturn 0;',counter_start)]
    guard_start=process.rfind('\t\tif (((new_dovi_setting.video_width',0,start)
    assert guard_start>=0
    caller_guard=process[guard_start:start]
    assert caller.count('memcpy(&dovi_setting, &new_dovi_setting, sizeof(dovi_setting));')==1
    flagline='stb_core_setting_update_flag |= flag;'
    assert text.count(flagline)==1
    defines=[]
    for name in ['CP_FLAG_CHANGE_TC2','CP_FLAG_CONST_TC2','CP_FLAG_CHANGE_ALL','DV_CORE2_RECONFIG_CNT','STB_DMA_TBL_SIZE','BYPASS_PROCESS','DOLBY_VISION_LL_DISABLE']:
        defines.append(next(l for l in text.splitlines() if l.startswith('#define '+name+' ' ) or l.startswith('#define '+name+'\t')))
    defines += [l for l in text.splitlines() if l.startswith('#define FLAG_')]
    defines += [l for l in pub.splitlines() if l.startswith('#define DOLBY_VISION_OUTPUT_MODE') or l.startswith('#define MUTE_TYPE')]
    reg_headers=['drivers/amlogic/media/enhancement/amvecm/arch/vpp_dolbyvision_regs.h','drivers/amlogic/media/enhancement/amvecm/arch/vpp_regs.h','include/linux/amlogic/media/registers/regs/viu_regs.h']
    # Actual register tokens used by these exact functions; simple definitions only.
    constants=set(re.findall(r'\b(?:DOLBY_[A-Z0-9_]+|VPP_[A-Z0-9_]+|VIU_[A-Z0-9_]+|VPU_HDMI_FMT_CTRL)\b',setter+apply+disable))
    mapping={}
    for name in reg_headers:
        for l in (ROOT/name).read_text().splitlines():
            m=re.match(r'#define\s+(\w+)\s+(.+)',l)
            if m:mapping[m[1]]=l
    defines += [mapping[name] for name in sorted(constants) if name in mapping]
    for name in ['CORE1_OFFSET','CORE1_1_OFFSET','CORE2A_OFFSET','CORE3_OFFSET','CORETV_OFFSET']:
        defines.append(mapping[name])
    defines += ['#define DOLBY_CORE2A 4','#define VPU_DOLBY2 2','#define VPU_DOLBY1A 1','#define VPU_PRIME_DOLBY_RAM 3','#define VPU_DOLBY_CORE3 4','#define VPU_DOLBY1B 5','#define VPU_DOLBY0 6','#define VPU_MEM_POWER_DOWN 1','#define VD1_PATH 0','#define HDR_MODULE_BYPASS 0','#define VPP_MATRIX_NULL 0','#define VIDEO_MUTE_OFF 0','#define VIDEO_MUTE_ON_DV 2']
    generated=PRE+'\n'.join(defines)+'\n'+types+'\n'+STUBS+'\n'+setter+'\n'+apply+'\n'+disable
    generated+='\nstatic void cp_accept(unsigned flag){'+flagline+'}\n'
    # Preserve actual guard, normalization, apply, whole-setting copy and pending-dimension clearing.
    generated+='\nstatic void production_frame(){\nconst bool reset_flag=false;const unsigned core_mask=7;const u8 pps_state=0;\n'+caller_guard+caller+'\nenable_dolby_vision(1);\n}\n'+counter+'\n}\n'+TESTS
    return generated,{'setter':setter,'apply':apply,'disable':disable,'caller':caller,'types':types,'caller_guard':caller_guard,'counter':counter}

def run_tests(args):
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    source=args.source or ROOT/'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c';text=source.read_text()
    base,parts=generate(text)
    (OUT/'production-extraction.cpp').write_text(base)
    info={'source':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'fragments':{k:{'sha256':hashlib.sha256(v.encode()).hexdigest(),'bytes':len(v)}for k,v in parts.items()},'results':[]}
    def run(code,name,expect=True,mode='lifecycle',backend=1):
        cpp=OUT/(name+'.cpp');exe=OUT/name;cpp.write_text(code)
        cmd=['g++','-std=c++17','-Wall','-Wextra','-Werror','-Wno-unused-variable','-Wno-unused-function','-Wno-unused-parameter','-fno-strict-aliasing','-fsanitize=address,undefined','-fno-omit-frame-pointer','-no-pie',str(cpp),'-o',str(exe)]
        compile_result=subprocess.run(cmd,capture_output=True,text=True)
        (OUT/(name+'-compile.txt')).write_text(compile_result.stdout+compile_result.stderr)
        assert compile_result.returncode==0,(name,compile_result.stderr)
        r=subprocess.run([str(exe),str(backend),mode],capture_output=True,text=True,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=1','UBSAN_OPTIONS':'halt_on_error=1'})
        (OUT/(name+'-run.txt')).write_text(r.stdout+r.stderr)
        assert (r.returncode==0)==expect,(name,r.stdout,r.stderr)
        if not expect:assert 'Assertion' in r.stderr,(name,r.stderr)
        if mode=='adjacent' and not expect:assert 'first_words==1280' in r.stderr,(name,r.stderr)
        kind='original-counterexample' if mode=='adjacent' and not expect and name!='historical-const-veto' else 'positive' if expect else 'negative-control'
        info['results'].append({'kind':kind,'name':name,'backend':backend,'mode':mode,'returncode':r.returncode,'expected_success':expect,'stdout':r.stdout,'stderr':r.stderr})
        print(r.stdout.strip() if expect else ('Original-code counterexample confirmed: ' if kind=='original-counterexample' else 'Rejected negative control: ')+name)
        if mode=='adjacent' and not expect:print(r.stdout.strip())
    run(base,'newer');run(base,'legacy',backend=0)
    if args.negative_controls:
        changes={
          'missing-retained-copy':('memcpy(&new_dovi_setting.dm_lut2, &dovi_setting.dm_lut2, sizeof(struct dm_lut_ipcore));','(void)0;'),
          'reversed-retained-copy':('memcpy(&new_dovi_setting.dm_lut2, &dovi_setting.dm_lut2, sizeof(struct dm_lut_ipcore));','memcpy(&dovi_setting.dm_lut2, &new_dovi_setting.dm_lut2, sizeof(struct dm_lut_ipcore));'),
          'current-change-beats-const':('if (update_bk & CP_FLAG_CONST_TC2)\n        stb_core2_const_flag = true;\n      else if (update_bk & CP_FLAG_CHANGE_TC2)\n        stb_core2_const_flag = false;', 'if (update_bk & CP_FLAG_CHANGE_TC2)\n        stb_core2_const_flag = false;\n      else if (update_bk & CP_FLAG_CONST_TC2)\n        stb_core2_const_flag = true;'),
          'reset-no-dm':('if (reset || p_core2_dm_regs[i] != last_dm[i]) {','if (p_core2_dm_regs[i] != last_dm[i]) {'),
          'disable-keeps-counter':('\t\tdolby_vision_core2_on_cnt = 0;','\t\t(void)0;'),
          'post-or-const-decision':('if (update_bk != CP_FLAG_CHANGE_ALL) {','if (stb_core_setting_update_flag != CP_FLAG_CHANGE_ALL) {'),
          'reset-no-dma':('if (set_lut || reset || force_set_lut) {','if (set_lut || force_set_lut) {'),
          'truncated-dma':('i < (256 * 5); i += 4','i < (256 * 5 - 4); i += 4'),
          'wrong-dma-word':('p_core2_lut[i + 3]);','p_core2_lut[i + 2]);'),
          'disable-keeps-const':('\t\t\tstb_core2_const_flag = false;','\t\t\t(void)0;'),
          'disable-keeps-cache':('memset(&dovi_setting, 0, sizeof(dovi_setting));','(void)0;'),
          'disable-no-seed':('\t\t\tstb_core_setting_update_flag = CP_FLAG_CHANGE_ALL;','\t\t\t(void)0;'),
          'no-update-history':('update_flag_more = update_bk;','update_flag_more = 0;'),
          'uncleared-flags':('stb_core_setting_update_flag = 0;\n  update_flag_more','(void)0;\n  update_flag_more'),
          'missing-publication':('memcpy(&dovi_setting, &new_dovi_setting, sizeof(dovi_setting));','(void)0;'),
        }
        for name,(old,new) in changes.items():run(replace_once(base,old,new),name,False)
        old=parts['caller'];copy='memcpy(&dovi_setting, &new_dovi_setting, sizeof(dovi_setting));'
        early=copy+'\n'+old.replace(copy,'(void)0;')
        run(replace_once(base,old,early),'premature-publication',False)
    run(base,'adjacent-change',not args.expect_adjacent_failures,mode='adjacent')
    run(base,'adjacent-change-legacy',not args.expect_adjacent_failures,mode='adjacent',backend=0)
    if args.negative_controls:
        broken=replace_once(base,'update_flags &= ~CP_FLAG_CONST_TC2;','(void)0;')
        run(broken,'historical-const-veto',False,mode='adjacent')
    (OUT/'results.json').write_text(json.dumps(info,indent=2)+'\n')
    print('Host request/ownership boundary only; opaque CP and physical application are unverified.')
def main():
    global ROOT,OUT
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=ROOT)
    ap.add_argument('--source',type=Path,help='Alternative driver file for before/after verification')
    ap.add_argument('--output',type=Path,help='Retain generated sources and results here')
    ap.add_argument('--negative-controls',action='store_true')
    ap.add_argument('--expect-adjacent-failures',action='store_true',help='Diagnose the original driver: both adjacent binaries must fail their unchanged transfer assertions')
    args=ap.parse_args();ROOT=args.root.resolve()
    with tempfile.TemporaryDirectory(prefix='dv-core2-lifecycle-') as tmp:
        OUT=args.output.resolve() if args.output else Path(tmp)
        OUT.mkdir(parents=True,exist_ok=True)
        run_tests(args)

if __name__=='__main__':main()
