#!/usr/bin/env python3
"""Host admission and ADRP-veneer regressions extracted from production C.
Does not execute ARM64 relocations in a running kernel.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile


def function(text, start):
    begin = text.index(start)
    brace = text.index('{', begin)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[begin:end]


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
root = args.source_root
module = (root / 'kernel/module.c').read_text()
plt = (root / 'arch/arm64/kernel/module-plts.c').read_text()
source = r'''
#include <assert.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
typedef uint64_t u64; typedef uint32_t u32; typedef uint32_t __le32;
#define ARCH_RELOCATES_KCRCTAB 1
#define reloc_start 0x400000UL
#define cpu_to_le32(x) (x)
#define le32_to_cpu(x) (x)
#define AARCH64_BREAK_FAULT 0xd4200000U
#define AARCH64_INSN_BRANCH_NOLINK 0
#define pr_warn_once(...) ((void)0)
#define pr_warn(...) ((void)0)
#define pr_debug(...) ((void)0)
struct plt_entry { u32 mov0,mov1,mov2,br; };
struct elf64_shdr { unsigned long sh_addr,sh_size; };
typedef struct elf64_shdr Elf_Shdr;
struct modversion_info { unsigned long crc; char name[56]; };
struct module {
 char name[64];
 struct { struct elf64_shdr *plt; int plt_num_entries,plt_max_entries; } arch;
};
static int force_result, force_calls;
static int try_to_force_load(struct module *mod,const char *reason) {
 (void)mod;(void)reason;force_calls++;return force_result;
}
static bool branch_fail;
/* Existing branch-emission service boundary; veneer instruction construction
 * and reservation behavior below are the actual production implementation. */
static u32 aarch64_insn_gen_branch_imm(unsigned long pc,u64 to,int kind) {
 (void)kind;
 if (branch_fail) return AARCH64_BREAK_FAULT;
 int64_t delta=(int64_t)to-(int64_t)pc;
 assert(!(delta&3) && delta>=-(1LL<<27) && delta<(1LL<<27));
 return 0x14000000U|((delta>>2)&0x3ffffff);
}
'''
source += function(module, 'static unsigned long maybe_relocated(') + '\n'
source += function(module, 'static int check_version(') + '\n'
source += function(plt, 'u64 module_emit_veneer_for_adrp(') + '\n'
source += r'''
static void test_versions(void) {
 struct module legacy={.name="dovi"},newer={.name="dovi5"},shim={.name="dv_compat_shim"};
 struct modversion_info v={.crc=0x12345678UL};strcpy(v.name,"module_layout");
 Elf_Shdr sec[2]={{0},{.sh_addr=(unsigned long)&v,.sh_size=sizeof(v)}};
 unsigned long kernel_crc=v.crc+reloc_start,module_crc=v.crc;
 struct module *targets[]={&newer,&shim};
 for(unsigned i=0;i<2;i++) {
  struct module *m=targets[i];force_calls=0;
  assert(check_version(sec,1,"module_layout",m,&kernel_crc,NULL)==1);
  assert(check_version(sec,1,"module_layout",m,&module_crc,&legacy)==1);
  v.crc++;assert(check_version(sec,1,"module_layout",m,&kernel_crc,NULL)==0);v.crc--;
  assert(check_version(sec,1,"missing",m,&kernel_crc,NULL)==0);
  assert(check_version(sec,0,"module_layout",m,&kernel_crc,NULL)==0 && !force_calls);
  assert(check_version(sec,1,"module_layout",m,NULL,NULL)==0);
 }
 /* Vendor compatibility outside the new integration is preserved. */
 v.crc++;assert(check_version(sec,1,"module_layout",&legacy,&kernel_crc,NULL)==1);v.crc--;
 assert(check_version(sec,1,"missing",&legacy,&kernel_crc,NULL)==1);
 force_result=0;assert(check_version(sec,0,"module_layout",&legacy,&kernel_crc,NULL)==1 && force_calls==1);
 force_result=-1;assert(check_version(sec,0,"module_layout",&legacy,&kernel_crc,NULL)==0);
 assert(check_version(sec,1,"module_layout",&legacy,NULL,NULL)==1);
}
static void test_veneer(void) {
 struct { struct plt_entry entries[2]; u32 next; } arena={0};
 Elf_Shdr sh={.sh_addr=(unsigned long)&arena.entries};
 struct module mod={.arch={.plt=&sh,.plt_max_entries=2}};
 for(unsigned rd=0;rd<31;rd++) {
  u64 page=0xffffff8076543000ULL;
  mod.arch.plt_num_entries=0;arena.next=0x90000000U|rd;
  u64 address=module_emit_veneer_for_adrp(&mod,&arena.next,page);
  assert(address==(u64)&arena.entries[0] && mod.arch.plt_num_entries==1);
  struct plt_entry *p=(void*)(uintptr_t)address;
  /* Decode actual emitted MOVN/MOVK instructions as an ARM64 register value. */
  assert((p->mov0&0xffe0001fU)==(0x92800000U|rd));
  assert((p->mov1&0xffe0001fU)==(0xf2a00000U|rd));
  assert((p->mov2&0xffe0001fU)==(0xf2c00000U|rd));
  u64 value=~(u64)((p->mov0>>5)&0xffffU);
  value=(value&~0xffff0000ULL)|((u64)((p->mov1>>5)&0xffffU)<<16);
  value=(value&~0xffff00000000ULL)|((u64)((p->mov2>>5)&0xffffU)<<32);
  assert(value==page);
  int64_t delta=(int32_t)((p->br&0x3ffffffU)<<6)>>4;
  assert((u64)&p->br+delta==(u64)&arena.next+4);
 }
 struct plt_entry before=arena.entries[0];
 mod.arch.plt_num_entries=2;
 assert(module_emit_veneer_for_adrp(&mod,&arena.next,0xffffff8000000000ULL)==0);
 assert(mod.arch.plt_num_entries==2 && !memcmp(&before,&arena.entries[0],sizeof(before)));
 mod.arch.plt_num_entries=0;branch_fail=true;
 assert(module_emit_veneer_for_adrp(&mod,&arena.next,0xffffff8000000000ULL)==0);
 assert(!mod.arch.plt_num_entries && !memcmp(&before,&arena.entries[0],sizeof(before)));
}
int main(void) {test_versions();test_veneer();puts("PASS: scoped strict CRC admission and bounded ADRP veneer encoding");}
'''
with tempfile.TemporaryDirectory(prefix='dv-module-compat-') as work:
    work = Path(work)
    (work / 'test.c').write_text(source)
    subprocess.run(['cc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                    str(work / 'test.c'), '-o', str(work / 'test')], check=True)
    import os
    subprocess.run([str(work / 'test')], check=True, env={**os.environ, 'ASAN_OPTIONS':'detect_leaks=0'})
