#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Compile actual ION/ARM64 dispatch functions against a recording cache model.

This verifies dispatch and the dirty-zero failure mechanism, not real CPU cache
instructions or GPU hardware. Run from any directory with python3 and cc.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]


def function(source, marker):
    start = source.index(marker)
    pos = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[pos] == '{') - (source[pos] == '}')
        pos += 1
    return source[start:pos]


ion_path = 'drivers/staging/android/ion/ion.c'
current = (ROOT/ion_path).read_text()
baseline = subprocess.check_output(['git', 'show', 'HEAD^:'+ion_path], cwd=ROOT, text=True)
arch = (ROOT/'arch/arm64/include/asm/dma-mapping.h').read_text()
mapping = (ROOT/'arch/arm64/mm/dma-mapping.c').read_text()
dma = (ROOT/'include/linux/dma-mapping.h').read_text()
prefix = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define CONFIG_ARM64 1
#define PAGE_SIZE 4096
#define min_t(type,a,b) ((type)(a)<(type)(b)?(type)(a):(type)(b))
enum dma_data_direction { DMA_BIDIRECTIONAL=0, DMA_TO_DEVICE=1, DMA_FROM_DEVICE=2 };
struct page { void *address; };
struct scatterlist { size_t length,dma_length; uintptr_t dma_address; };
struct device;
struct dma_map_ops { void (*sync_sg_for_device)(struct device*,struct scatterlist*,int,enum dma_data_direction); };
struct device { struct { struct dma_map_ops *dma_ops; } archdata; };
static unsigned dummy_calls,real_calls,flush_calls;
static size_t flushed_bytes,real_dma_length;
static bool dirty, no_mapping;
static const void *hole;
#define virt_addr_valid(addr) (!no_mapping && (const void *)(addr)!=hole)
static unsigned char *cached_alias,*dram;
static void sg_init_table(struct scatterlist*s,int n){(void)n;memset(s,0,sizeof(*s));}
static void sg_set_page(struct scatterlist*s,struct page*p,size_t n,size_t off){(void)p;(void)off;s->length=n;}
#define sg_dma_address(s) ((s)->dma_address)
#define sg_dma_len(s) ((s)->dma_length)
#define page_to_phys(p) ((uintptr_t)(p)->address)
#define page_address(p) ((p)->address)
#define BUG_ON(x) assert(!(x))
#define valid_dma_direction(x) ((x)>=0 && (x)<=2)
#define debug_dma_sync_sg_for_device(...) ((void)0)
#define pr_info_once(...) ((void)0)
static void __dma_flush_area(const void *address,size_t size) {
 assert((const unsigned char *)address>=cached_alias);flush_calls++;flushed_bytes+=size;
 if(dirty)memcpy(dram+((const unsigned char *)address-cached_alias),address,size);
 dirty=false;
}
'''
ops = r'''
static void counted_dummy(struct device*d,struct scatterlist*s,int n,enum dma_data_direction dir){
 dummy_calls++;__dummy_sync_sg(d,s,n,dir);
}
static void real_sync(struct device*d,struct scatterlist*s,int n,enum dma_data_direction dir){
 (void)d;(void)n;(void)dir;real_calls++;real_dma_length=sg_dma_len(s);
}
static struct dma_map_ops dummy_dma_ops={counted_dummy};
static struct dma_map_ops real_dma_ops={real_sync};
'''
test = r'''
int main(void){
 for(unsigned order=0;order<3;order++){
  size_t size=order==0?4096:order==1?65536:1048576;
  cached_alias=calloc(1,size);dram=malloc(size);assert(cached_alias&&dram);
  memset(dram,0x77,size);dirty=true;dummy_calls=real_calls=flush_calls=0;flushed_bytes=0;
  struct page page={cached_alias};ion_pages_sync_for_device(NULL,&page,size,DMA_BIDIRECTIONAL);
  // CPU writes through WC mapping, followed by an example device/body write.
  memset(dram,0xa5,size);dram[0]=0x6b;
  // Eviction of a still-dirty normal kernel alias overwrites body AND guard.
  if(dirty)memcpy(dram,cached_alias,size);
  int intact=dram[0]==0x6b && dram[size-1]==0xa5;
  printf("size=%zu fixed=%d dummy_calls=%u flush_calls=%u intact_after_eviction=%d\n",size,FIXED,dummy_calls,flush_calls,intact);
  if(FIXED){assert(flush_calls==1 && flushed_bytes==size && !dummy_calls && intact);}
  else{assert(dummy_calls==1 && !flush_calls && !intact);}
  // Existing device-aware DMA dispatch must be preserved.
  struct device device={{&real_dma_ops}};ion_pages_sync_for_device(&device,&page,size,DMA_TO_DEVICE);
  assert(real_calls==1);if(FIXED)assert(real_dma_length==size);
  if(FIXED){
   // Reserved no-map heaps must not dereference an absent linear mapping.
   no_mapping=true;flush_calls=0;ion_pages_sync_for_device(NULL,&page,size,DMA_BIDIRECTIONAL);
   assert(!flush_calls);no_mapping=false;
   // A hole divides mapped ranges; neither flush may cross the hole.
   if(size>=3*PAGE_SIZE){
    hole=cached_alias+PAGE_SIZE;flush_calls=0;flushed_bytes=0;
    ion_pages_sync_for_device(NULL,&page,size,DMA_BIDIRECTIONAL);
    assert(flush_calls==2 && flushed_bytes==size-PAGE_SIZE);hole=NULL;
   }
  }
  free(cached_alias);free(dram);
 }
 return 0;
}
'''
out = Path(tempfile.mkdtemp(prefix='ion-cache-sync-'))
for name, source, fixed in [('baseline', baseline, 0), ('fixed', current, 1)]:
    code = prefix + function(mapping, 'static void __dummy_sync_sg(') + ops
    code += function(arch, 'static inline struct dma_map_ops *__generic_dma_ops(')
    code += '\n#define get_dma_ops(dev) __generic_dma_ops(dev)\n'
    code += function(dma, 'static inline void\ndma_sync_sg_for_device(')
    code += function(source, 'void ion_pages_sync_for_device(') + test
    cpp = out/(name+'.c')
    cpp.write_text(code)
    exe = out/name
    subprocess.run(['cc', '-O2', '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-Wno-unused-function', '-DFIXED='+str(fixed),
                    str(cpp), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print('PASS actual dispatch: NULL device skipped cache maintenance before fix; mapped-range flush after fix; no-map heaps and real-device DMA path preserved')
