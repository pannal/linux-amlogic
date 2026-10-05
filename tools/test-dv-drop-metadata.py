#!/usr/bin/env python3
"""Production DV metadata writers and repeat selection with a modeled parser.

Checks retained-buffer identity and parser advancement, not the proprietary
parser's RPU interpretation, control-path library or device output.
"""
import argparse, pathlib, subprocess, tempfile
p=argparse.ArgumentParser();p.add_argument('--source',type=pathlib.Path,default=pathlib.Path(__file__).resolve().parents[1] / 'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c');p.add_argument('--expect-failure',action='store_true');a=p.parse_args();s=a.source.read_text()
def balanced(start):
    brace=s.index('{',start);depth=1;i=brace+1
    while depth:
        if s[i]=='{': depth+=1
        elif s[i]=='}': depth-=1
        i+=1
    return s[start:i]
wrapper=balanced(s.index('static int parse_sei_and_meta\n'))
parse_start=s.index('static int dv_parse_metadata_internal(') if 'static int dv_parse_metadata_internal(' in s else s.index('int dolby_vision_parse_metadata(')
repeat=balanced(s.index('if (meta_flag_bl && meta_flag_el)',parse_start))
start=s.index('if (ret == 1) { /*parse succeeded*/',parse_start)
fast=balanced(start)
pre=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
struct vframe_s { struct {char *md_buf,*comp_buf;int md_size,comp_size,parse_ret_flags;} src_fmt; };
struct provider_aux_req_s { char *aux_buf; int aux_size; };
enum signal_format_enum { FORMAT_SDR, FORMAT_DOVI };
static char normal_md[2][16],normal_comp[2][16],drop_md[2][16],drop_comp[2][16];
static char *md_buf[2]={normal_md[0],normal_md[1]},*comp_buf[2]={normal_comp[0],normal_comp[1]};
static char *drop_md_buf[2]={drop_md[0],drop_md[1]},*drop_comp_buf[2]={drop_comp[0],drop_comp[1]};
static int current_id,backup_comp_size,backup_md_size,parse_result,parser_calls,debug_dolby,dump_enable;
static int last_total_md_size=4,last_total_comp_size=4,mel_mode;
static struct { int el_flag; } dovi_setting;
static int is_dolby_vision_stb_mode(void) { return 1; }
#define pr_dolby_dbg(...) ((void)0)
static void dump_buffer(const char*a,const char*b,int c) {(void)a;(void)b;(void)c;}
static int parse_sei_and_meta_ext(struct vframe_s*vf,char*aux,int size,int*comp_size,int*md_size,enum signal_format_enum*format,int*ret_flags,char*md,char*comp) {
 (void)vf;(void)aux;(void)size;(void)ret_flags; parser_calls++;*format=FORMAT_DOVI;
 if(parse_result==0){memset(comp,'B',8);memset(md,'B',8);*comp_size=*md_size=8;} return parse_result;
}
static void reset(void) { memset(normal_md,'C',sizeof normal_md);memset(normal_comp,'C',sizeof normal_comp);memset(normal_md[0],'A',4);memset(normal_comp[0],'A',4);current_id=0;backup_md_size=backup_comp_size=4;parser_calls=0;parse_result=0; }
'''
fastfn='\nstatic void fast_path(struct vframe_s *vf, bool drop_flag) { int ret=1,meta_flag_bl=1,total_md_size=0,total_comp_size=0,ret_flags=0;enum signal_format_enum src_format=FORMAT_SDR;\n'+fast+'\nassert(!meta_flag_bl && src_format==FORMAT_DOVI && total_md_size==8 && total_comp_size==8 && ret_flags==3);}\n'
repeatfn='\nstatic void repeat_presented(void) { int meta_flag_bl=1,meta_flag_el=1,total_md_size=0,total_comp_size=0,el_flag=0,mel_flag=0;\n'+repeat+'\n(void)el_flag;(void)mel_flag;assert(!meta_flag_bl&&total_md_size==4&&total_comp_size==4);assert(md_buf[current_id][0]==\'A\'&&comp_buf[current_id][0]==\'A\');}\n'
main=r'''
int main(int argc,char**argv) {
 assert(argc==2);reset();struct vframe_s vf={0};char aux=1;struct provider_aux_req_s req={&aux,1};int cs=0,ms=0,rf=0;enum signal_format_enum fmt=FORMAT_SDR;
 if(!strcmp(argv[1],"parsed-drop")) {
  int ret=parse_sei_and_meta(&vf,&req,&cs,&ms,&fmt,&rf,true);assert(ret==0&&parser_calls==1&&drop_md[1][0]=='B');
  assert(current_id==0&&backup_md_size==4&&backup_comp_size==4);assert(md_buf[current_id][0]=='A'&&comp_buf[current_id][0]=='A');repeat_presented();parse_result=3;cs=ms=0;ret=parse_sei_and_meta(&vf,&req,&cs,&ms,&fmt,&rf,false);assert(ret==3&&parser_calls==2&&cs==4&&ms==4&&current_id==0);repeat_presented();
 } else if(!strcmp(argv[1],"parsed-normal")) {
  int ret=parse_sei_and_meta(&vf,&req,&cs,&ms,&fmt,&rf,false);assert(ret==0&&parser_calls==1&&current_id==1&&backup_md_size==8&&md_buf[current_id][0]=='B');
 } else if(!strcmp(argv[1],"parsed-failure")) {
  parse_result=3;int ret=parse_sei_and_meta(&vf,&req,&cs,&ms,&fmt,&rf,true);assert(ret==3&&parser_calls==1&&current_id==0&&backup_md_size==4&&ms==4&&md_buf[0][0]=='A');
 } else if(!strcmp(argv[1],"preparsed-drop")) {
  char b[8];memset(b,'B',8);vf.src_fmt.md_buf=b;vf.src_fmt.comp_buf=b;vf.src_fmt.md_size=8;vf.src_fmt.comp_size=8;vf.src_fmt.parse_ret_flags=3;fast_path(&vf,true);assert(current_id==0&&md_buf[0][0]=='A'&&comp_buf[0][0]=='A');
 } else if(!strcmp(argv[1],"preparsed-normal")) {
  char b[8];memset(b,'B',8);vf.src_fmt.md_buf=b;vf.src_fmt.comp_buf=b;vf.src_fmt.md_size=8;vf.src_fmt.comp_size=8;vf.src_fmt.parse_ret_flags=3;fast_path(&vf,false);assert(md_buf[0][0]=='B'&&comp_buf[0][0]=='B');
 } else assert(0);return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='amldv-discard-') as d:
    root=pathlib.Path(d);src=root/'test.c';exe=root/'test';src.write_text(pre+wrapper+fastfn+repeatfn+main)
    subprocess.run(['cc','-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-fsanitize=undefined,address','-fno-pie','-no-pie',str(src),'-o',str(exe)],check=True)
    for case in ['parsed-drop','parsed-normal','parsed-failure','preparsed-drop','preparsed-normal']:
        r=subprocess.run([str(exe),case],capture_output=True,text=True,timeout=10)
        expected=a.expect_failure and case in ['parsed-drop','preparsed-drop']
        if expected:
            assert r.returncode!=0 and 'Assertion' in r.stderr,(case,'baseline did not fail its assertion',r.stderr);print('REPRODUCED',case, r.stderr.strip().splitlines()[-1])
        else:
            assert r.returncode==0,(case,r.stdout,r.stderr);print('PASS',case)
