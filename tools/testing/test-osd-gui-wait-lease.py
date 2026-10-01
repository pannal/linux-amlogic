#!/usr/bin/env python3
"""Production lease helpers with modeled fd/copy/pid plumbing, not device output."""
import argparse
from pathlib import Path
import subprocess
import tempfile

PRELUDE = r'''
#include <cassert>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <fcntl.h>
#include <mutex>
#include <sys/ioctl.h>
#include <thread>
#include <atomic>
using u32=uint32_t;using __s32=int32_t;using __u32=uint32_t;
#define __user
#define CONFIG_COMPAT
#define THIS_MODULE nullptr
#define GFP_KERNEL 0
struct pid {int number,refs;};struct task {pid* group;};
static pid first{42,0},second{43,0},reused{42,0};
static thread_local task caller{&first};
#define current (&caller)
static pid* task_tgid(task*t){return t->group;}
static pid* get_pid(pid*p){++p->refs;return p;}
static void put_pid(pid*p){--p->refs;}
struct list_head {list_head *next,*prev;};
#define LIST_HEAD(n) list_head n{&n,&n}
static void list_add(list_head*x,list_head*h){x->next=h->next;x->prev=h;h->next->prev=x;h->next=x;}
static void list_del(list_head*x){x->prev->next=x->next;x->next->prev=x->prev;}
#define entry(p,t,m) reinterpret_cast<t*>(reinterpret_cast<char*>(p)-offsetof(t,m))
#define list_for_each_entry(p,h,m) for(auto* it=(h)->next;it!=(h)&&((p)=entry(it,osd_gui_wait_lease,m),true);it=it->next)
#define DEFINE_MUTEX(n) std::mutex n
static void mutex_lock(std::mutex*m){m->lock();}
static void mutex_unlock(std::mutex*m){m->unlock();}
struct inode{};struct file;struct osd_gui_wait_lease;
struct file_operations {void*owner;long(*unlocked_ioctl)(file*,unsigned,unsigned long);long(*compat_ioctl)(file*,unsigned,unsigned long);int(*release)(inode*,file*);};
struct file{osd_gui_wait_lease*private_data;const file_operations*ops;};
static file* descriptors[128];static int nextfd=10,closed_unused,allocation_fail;
static bool copy_in_fail,copy_out_fail,file_fail,fd_fail;
static int get_unused_fd_flags(int flags){assert(flags==O_CLOEXEC);return fd_fail?-EMFILE:nextfd++;}
static void put_unused_fd(int){++closed_unused;}
static osd_gui_wait_lease* kzalloc(size_t n,int){return allocation_fail?nullptr:reinterpret_cast<osd_gui_wait_lease*>(calloc(1,n));}
static void kfree(void*p){free(p);}
static file* anon_inode_getfile(const char*,const file_operations*ops,osd_gui_wait_lease*p,int flags){assert(flags==O_RDWR);return file_fail?reinterpret_cast<file*>(-ENOMEM):new file{p,ops};}
#define IS_ERR(p) (reinterpret_cast<intptr_t>(p)<0)
#define PTR_ERR(p) static_cast<int>(reinterpret_cast<intptr_t>(p))
static void fput(file*f){f->ops->release(nullptr,f);delete f;}
static void fd_install(int fd,file*f){assert(!descriptors[fd]);descriptors[fd]=f;}
static int copy_to_user(void*d,const void*s,size_t n){if(copy_out_fail)return 1;memcpy(d,s,n);return 0;}
static int copy_from_user(void*d,const void*s,size_t n){if(copy_in_fail)return 1;memcpy(d,s,n);return 0;}
static void* compat_ptr(unsigned long p){return reinterpret_cast<void*>(p);}
'''
TESTS = r'''
static file* create(u32 node,int&fd){fd=-1;assert(osd_gui_wait_create(node,&fd)==0);assert(fd>=0);return descriptors[fd];}
static void closefd(int fd){fput(descriptors[fd]);descriptors[fd]=nullptr;}
static long set(file*f,u32 value){return f->ops->compat_ioctl(f,FBIOSET_GUI_WAIT_LEASE,reinterpret_cast<unsigned long>(&value));}
int main(){
 assert(FBIOGET_GUI_WAIT_LEASE==0x80044622UL&&FBIOSET_GUI_WAIT_LEASE==0x40044623UL);
 int fd;auto*f=create(0,fd);assert(!osd_gui_wait_enabled(0));
 assert(set(f,1)==0&&osd_gui_wait_enabled(0)&&!osd_gui_wait_enabled(2));
 // A Mali callback thread in the same process sees the lease; another TGID
 // (including reuse of the same numeric PID) neither sees nor controls it.
 std::thread native([]{assert(osd_gui_wait_enabled(0));});native.join();
 caller.group=&second;assert(!osd_gui_wait_enabled(0)&&set(f,0)==-EPERM);
 caller.group=&reused;assert(!osd_gui_wait_enabled(0));caller.group=&first;
 assert(set(f,2)==-EINVAL&&osd_gui_wait_enabled(0));
 copy_in_fail=true;assert(set(f,0)==-EFAULT&&osd_gui_wait_enabled(0));copy_in_fail=false;
 assert(f->ops->unlocked_ioctl(f,0,0)==-ENOTTY);
 // Explicit disable is effective while an inherited/duplicated file remains.
 assert(set(f,0)==0&&!osd_gui_wait_enabled(0));
 int fd2;auto*g=create(2,fd2);assert(set(g,1)==0&&osd_gui_wait_enabled(2));
 assert(!osd_gui_wait_enabled(0));assert(set(f,1)==0);
 std::atomic<bool> done{false};std::thread concurrent([&]{while(!done)osd_gui_wait_enabled(0);});
 closefd(fd);done=true;concurrent.join();assert(!osd_gui_wait_enabled(0));
 closefd(fd2);assert(!osd_gui_wait_enabled(2)&&first.refs==0);
 // Failure before fd publication must leave no registry entry or pid ref.
 for(int n=0;n<4;++n){int out=-1;fd_fail=n==0;allocation_fail=n==1;file_fail=n==2;copy_out_fail=n==3;
 assert(osd_gui_wait_create(0,&out)<0&&out==-1&&first.refs==0);
 assert(osd_gui_wait_leases.next==&osd_gui_wait_leases);}
 puts("PASS process/node scope, callback thread, PID identity, disable/release, concurrent lookup, ABI and allocation/copy failures");
}
'''
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    header=(root/'drivers/amlogic/media/osd/osd_gui_wait.h').read_text()
    header='\n'.join(x for x in header.splitlines() if not x.startswith('#include'))
    with tempfile.TemporaryDirectory(prefix='osd-gui-lease-') as tmp:
        p=Path(tmp)
        def run(body,negative=False):
            (p/'test.cpp').write_text(PRELUDE+body+TESTS)
            subprocess.run(['g++','-std=c++20','-Wall','-Werror','-pthread','-fsanitize=address,undefined','-fno-pie','-no-pie',str(p/'test.cpp'),'-o',str(p/'test')],check=True)
            result=subprocess.run([str(p/'test')],capture_output=negative,text=True)
            if negative:
                assert result.returncode!=0 and 'Assertion' in result.stderr,result
            else:result.check_returncode()
        run(header)
        if args.negative_controls:
            for needle in ['lease->node == node &&','lease->tgid == task_tgid(current)','lease->enabled &&']:
                assert header.count(needle)==1
                run(header.replace(needle,'' if needle.endswith('&&') else 'true'),True)
                print('Rejected negative control:',needle)
if __name__=='__main__':main()
