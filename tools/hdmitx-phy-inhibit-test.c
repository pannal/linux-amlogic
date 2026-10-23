/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include "constants.inc"
#define PAGE_SIZE 4096
#define SYS ""
#define pr_info(...) ((void)0)
#define RXSEN_LOW_CNT 5
#define MISC_TMDS_PHY_OP 1
#define MISC_TMDS_RXSENSE 2
#define MISC_HPD_GPI_ST 3
#define TMDS_PHY_ENABLE 1
#define TMDS_PHY_DISABLE 0
#define TMDS_CFG 1
#define msleep(x) ((void)0)
#define mdelay(x) ((void)0)
#define mutex_lock(x) assert(pthread_mutex_lock(x) == 0)
#define mutex_unlock(x) assert(pthread_mutex_unlock(x) == 0)
struct device {};
struct device_attribute {};
struct hdmitx_dev {
 bool phy_inhibit;
 int hpd_state, ready, cur_VIC, chip_type, div40;
 struct { int cs, cd; } *para;
 struct {
  int (*cntlmisc)(struct hdmitx_dev*, unsigned, unsigned);
  void (*debugfun)(struct hdmitx_dev*, const char*);
 } hwop;
};
static struct hdmitx_dev hdmitx_device;
static pthread_mutex_t setclk_mutex = PTHREAD_MUTEX_INITIALIZER;
static struct hdmitx_dev* get_hdmitx_device(void) { return &hdmitx_device; }
static unsigned regs[TEST_ENUM_END], writes, enabled, disabled, scdc, clocks, debug;
static bool output;
static unsigned step, scdc_step, enable_step;
static bool sysfs_streq(const char* a, const char* b) {
 size_t n = strlen(b); return !strncmp(a,b,n) && (!a[n] || (a[n]=='\n' && !a[n+1]));
}
static void hd_write_reg(unsigned reg, unsigned value) { regs[reg]=value; ++writes; }
static void hd_set_reg_bits(unsigned reg, unsigned value, unsigned start, unsigned len)
{ (void)start; (void)len; hd_write_reg(reg, value); }
static unsigned hd_read_reg(unsigned reg) { return regs[reg]; }
static void set_phy_by_mode(unsigned mode)
{ (void)mode; assert(!hdmitx_device.phy_inhibit); output=true; ++enabled; enable_step=++step; }
static void set_tmds_clk_div40(unsigned div40)
{ assert(div40 == (unsigned)hdmitx_device.div40); ++clocks; }
static void scdc_wr_sink(unsigned reg, unsigned value)
{ (void)reg; assert(value == (hdmitx_device.div40 ? 3u : 0u)); ++scdc; scdc_step=++step; }
#include "hw.inc"
static int misc(struct hdmitx_dev* hdev, unsigned cmd, unsigned arg) {
 if (cmd == MISC_HPD_GPI_ST) return hdev->hpd_state;
 if (cmd == MISC_TMDS_RXSENSE) return hdmitx_tmds_rxsense();
 assert(cmd == MISC_TMDS_PHY_OP);
 if (arg == TMDS_PHY_ENABLE) hdmitx_set_phy(hdev);
 else { hdmi_phy_suspend(); output=false; ++disabled; }
 return 0;
}
static void diagnostic(struct hdmitx_dev* hdev, const char* command)
{ (void)command; assert(!hdev->phy_inhibit); ++debug; }
#include "main.inc"
static pthread_barrier_t acquired, release_worker;
static void* worker(void* unused) {
 (void)unused;
 mutex_lock(&setclk_mutex);
 pthread_barrier_wait(&acquired);
 pthread_barrier_wait(&release_worker);
 hdmitx_set_phy(&hdmitx_device);
 mutex_unlock(&setclk_mutex);
 return NULL;
}
static void* inhibit(void* unused) {
 (void)unused; assert(store_phy_inhibit(NULL,NULL,"1\n",2)==2); return NULL;
}
int main(void) {
 typeof(*hdmitx_device.para) para = {COLORSPACE_YUV422, COLORDEPTH_24B};
 hdmitx_device.para=&para;
 hdmitx_device.hwop.cntlmisc=misc;
 hdmitx_device.hwop.debugfun=diagnostic;
 hdmitx_device.hpd_state=hdmitx_device.ready=1;
 hdmitx_device.cur_VIC=HDMI_1080p60;
 hdmitx_device.chip_type=MESON_CPU_ID_G12B;
 char buf[PAGE_SIZE];
 assert(store_phy_inhibit(NULL,NULL,"2",1)==-EINVAL);
 assert(store_phy_inhibit(NULL,NULL,"1junk",5)==-EINVAL);
 assert(store_phy_inhibit(NULL,NULL,"",0)==-EINVAL);
 assert(store_phy_inhibit(NULL,NULL,"1\n",2)==2);
 assert(show_phy_inhibit(NULL,NULL,buf)==2 && !strcmp(buf,"1\n"));
 unsigned baseline=writes;
 assert(store_phy_inhibit(NULL,NULL,"1",1)==1 && writes==baseline);
 assert(store_phy(NULL,NULL,"1",1)==1 && !output && writes==baseline);
 // Every rxsense chip branch is gated before its first MMIO write.
 int chips[]={MESON_CPU_ID_G12A,MESON_CPU_ID_G12B,MESON_CPU_ID_SC2,
  MESON_CPU_ID_GXBB,MESON_CPU_ID_GXL,MESON_CPU_ID_GXM,MESON_CPU_ID_TM2,999};
 for (unsigned i=0;i<sizeof(chips)/sizeof(*chips);++i) {
  hdmitx_device.chip_type=chips[i];
  assert(show_rxsense_state(NULL,NULL,buf)==1 && !strcmp(buf,"0"));
  hdmitx_set_phy(&hdmitx_device);
  assert(writes==baseline);
 }
 hdmitx_device.chip_type=MESON_CPU_ID_G12B;
 assert(store_debug(NULL,NULL,"prbs",4)==-EBUSY && debug==0);
 // All release preconditions are necessary, including stale ready/VIC cases.
 for (int hpd=0;hpd<2;++hpd) for (int ready=0;ready<2;++ready) for (int vic=0;vic<2;++vic) {
  assert(store_phy_inhibit(NULL,NULL,"1",1)==1);
  hdmitx_device.hpd_state=hpd; hdmitx_device.ready=ready;
  hdmitx_device.cur_VIC=vic ? HDMI_1080p60 : HDMI_Unknown;
  unsigned old_scdc=scdc;
  assert(store_phy_inhibit(NULL,NULL,"0",1)==1);
  assert(!hdmitx_device.phy_inhibit);
  assert(output == !!(hpd && ready && vic));
  assert(scdc == old_scdc + !!(hpd && ready && vic));
  if (output) assert(scdc_step < enable_step);
 }
 // A later completed ordinary modeset can enable after an invalid-state release.
 hdmitx_device.hpd_state=hdmitx_device.ready=1; hdmitx_device.cur_VIC=HDMI_1080p60;
 mutex_lock(&setclk_mutex); hdmitx_set_phy(&hdmitx_device); mutex_unlock(&setclk_mutex);
 assert(output);
 hdmitx_device.div40=1;
 assert(store_phy_inhibit(NULL,NULL,"1",1)==1);
 assert(store_phy_inhibit(NULL,NULL,"0",1)==1 && scdc_step < enable_step);
 baseline=writes;
 assert(store_phy_inhibit(NULL,NULL,"0",1)==1 && writes==baseline);
 // In-flight worker first: the inhibit store waits and disables after enable.
 pthread_barrier_init(&acquired,NULL,2); pthread_barrier_init(&release_worker,NULL,2);
 pthread_t w,i;
 pthread_create(&w,NULL,worker,NULL); pthread_barrier_wait(&acquired);
 pthread_create(&i,NULL,inhibit,NULL); pthread_barrier_wait(&release_worker);
 pthread_join(w,NULL); pthread_join(i,NULL);
 assert(hdmitx_device.phy_inhibit && !output);
 // Inhibit first: the same worker sees the latch and never writes PHY registers.
 baseline=writes;
 pthread_create(&w,NULL,worker,NULL); pthread_barrier_wait(&acquired);
 pthread_barrier_wait(&release_worker); pthread_join(w,NULL);
 assert(!output && writes==baseline);
 pthread_barrier_destroy(&acquired); pthread_barrier_destroy(&release_worker);
 assert(clocks==scdc && enabled>0 && disabled>0);
}
