#!/usr/bin/env python3
"""Production update_table_item with modeled MMIO, IRQ exclusion and writer interleavings.
Host checks do not establish RDMA hardware exclusion, IRQ duration or visible output.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def function(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]

PRELUDE = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;typedef uint8_t u8;
struct rdma_table_item {u32 addr,val;};
static struct rdma_table_item rdma_table[512];
static u32 item_count,table_paddr=0x1000,end_addr;
static int rdma_reset_tigger_flag,vsync_irq_count,rdma_irq_count;
static bool held,rejected,irq_requested,irq_pending,irq_interleaved;
static int locks,inject_lock,inject_count,inject_status,writes,logs;
static u32 written_addr[512],written_val[512];
#define READ_ONCE(x) (x)
#define OSD_RDMA_UPDATE_RETRY_COUNT 3
#define OSD_RDMA_FLAG_REG 99
#define OSD_RDMA_STATUS_MARK_TBL_RST 10
#define OSD_RDMA_STATUS_MARK_TBL_DONE 11
#define END_ADDR 100
#define RDMA_STATUS 101
#define OSD_RDMA_STATUS_IS_REJECT (rejected)
#define pr_info(...) do { assert(!held); ++logs; if(false)printf(__VA_ARGS__); } while(0)
#define pr_debug(...) do {if(false)printf(__VA_ARGS__);} while(0)
static void lock(void) {
 assert(!held);
 if(++locks==inject_lock){if(inject_count)item_count=inject_count;if(inject_status)rejected=true;}
 held=true;
}
static void unlock(void) {
 assert(held);held=false;
 if(irq_pending){assert(end_addr==table_paddr+15 && item_count==2);irq_pending=false;}
}
#define spin_lock_irqsave(m,f) do { (f)=0;lock(); } while(0)
#define spin_unlock_irqrestore(m,f) do { (void)(f);unlock(); } while(0)
static u32 osd_reg_read(u32 addr){return addr==END_ADDR?end_addr:0;}
static void osd_reg_write(u32 addr,u32 val){
 assert(held);assert(writes<512);written_addr[writes]=addr;written_val[writes++]=val;
 if(addr==END_ADDR)end_addr=val;
 if(irq_requested){irq_requested=false;if(held)irq_pending=true;else irq_interleaved=true;}
}
static void update_backup_reg(u32 addr,u32 val){(void)addr;(void)val;assert(held);}
static void update_recovery_item(u32 addr,u32 val){(void)addr;(void)val;assert(held);}
static void osd_rdma_mem_cpy(struct rdma_table_item* dst,struct rdma_table_item* src,u32 len){
 assert(held);assert(dst>=rdma_table&&dst<rdma_table+512);memcpy(dst,src,len);
}
"""
TESTS = r"""
static void setup(u32 count){
 memset(rdma_table,0,sizeof(rdma_table));
 item_count=count;rdma_table[0]=(struct rdma_table_item){99,10};
 for(u32 i=1;i<count-1;++i)rdma_table[i]=(struct rdma_table_item){i,i+100};
 rdma_table[count-1]=(struct rdma_table_item){99,11};
 end_addr=table_paddr+count*8-1;held=rejected=irq_requested=irq_pending=irq_interleaved=false;
 locks=inject_lock=inject_count=inject_status=writes=logs=rdma_reset_tigger_flag=0;
}
int main(void){
 setup(501);irq_requested=true;assert(update_table_item(700,800,0)==-1);
 assert(!held&&!irq_pending&&!irq_interleaved&&item_count==2);
 assert(writes==501&&written_addr[0]==1&&written_addr[498]==499);
 assert(written_addr[499]==700&&written_val[499]==800);
 assert(rdma_table[0].val==10&&rdma_table[1].val==11&&end_addr==table_paddr+15);
 setup(4);rdma_reset_tigger_flag=1;assert(update_table_item(700,800,1)==-1);
 assert(item_count==2&&writes==4&&rdma_reset_tigger_flag==1); // watchdog owns this flag
 setup(500);assert(update_table_item(700,800,0)==0&&item_count==501);
 assert(end_addr==table_paddr+501*8-1&&rdma_table[500].val==11);
 assert(update_table_item(701,801,0)==-1&&item_count==2);
 // Concurrent writer fills between initial predicate and normal append admission.
 setup(4);inject_lock=2;inject_count=501;
 assert(update_table_item(700,800,0)==-1&&item_count==2);
 // Same race at exhausted reject-retry append; no append beyond the bound.
 setup(4);rejected=true;inject_lock=5;inject_count=501;
 assert(update_table_item(700,800,0)==-1&&item_count==2);
 setup(4);rejected=true;assert(update_table_item(700,800,1)==-2&&item_count==4&&writes==0&&!held);
 setup(4);inject_lock=2;inject_status=1;
 assert(update_table_item(700,800,1)==-3&&item_count==5&&writes==0&&!held);
 setup(4);rejected=true;assert(update_table_item(700,800,0)==-1&&item_count==5&&writes==0&&!held);
 puts("PASS serialized direct recovery, boundary/racing appends, IRQ exclusion and preserved reject/retry semantics");
}
"""

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--negative-control',action='store_true')
    args=parser.parse_args()
    source=(ROOT/'drivers/amlogic/media/osd/osd_rdma.c').read_text()
    body=function(source,'static int update_table_item(')
    with tempfile.TemporaryDirectory(prefix='osd-rdma-recovery-') as tmp:
        p=Path(tmp)
        def run(body, expected=None):
            (p/'test.c').write_text(PRELUDE+body+TESTS)
            subprocess.run([os.environ.get('CC','gcc'),'-std=gnu11','-Wall','-Werror',
                            '-fsanitize=address,undefined','-fno-omit-frame-pointer',
                            str(p/'test.c'),'-o',str(p/'test')],check=True)
            result=subprocess.run([str(p/'test')],capture_output=bool(expected),text=True,timeout=10)
            if expected:
                assert result.returncode!=0 and expected in result.stderr,result
                print('Rejected negative control:', expected)
            else: result.check_returncode()
        run(body)
        if args.negative_control:
            needle='\tspin_lock_irqsave(&rdma_lock, flags);\n\tif ((item_count > 500)'
            assert body.count(needle)==2
            run(body.replace(needle,'\tif ((item_count > 500)',1),'held')
            needle='\tif ((item_count > 500) || READ_ONCE(rdma_reset_tigger_flag))\n\t\tgoto recover;'
            pos=body.rindex(needle)
            run(body[:pos]+body[pos:].replace(needle,'',1),'item_count==2')
            needle='\t\tif ((item_count > 500) || READ_ONCE(rdma_reset_tigger_flag))\n\t\t\tgoto recover;'
            assert body.count(needle)==1
            run(body.replace(needle,''),'item_count==2')
            needle='\t\tspin_unlock_irqrestore(&rdma_lock, flags);\n\t\tif (report)'
            assert body.count(needle)==1
            run(body.replace(needle,'\t\tif (report)'),'!held')

if __name__=='__main__':main()
