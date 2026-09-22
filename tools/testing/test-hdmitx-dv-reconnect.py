#!/usr/bin/env python3
"""Host checks of production HDMI reconnect policy/work functions with driver stubs.

This does not build a kernel or verify hardware, IRQ timing, or DV blob behavior.
Run with Python 3 and a host C compiler; all generated files are temporary.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = (root / 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hdmi_tx_main.c').read_text()
start = source.index('/* A reconnect must use the newly read sink capabilities. */')
end = source.index('struct vsif_debug_save vsif_debug_info;', start)
production = source[start:end]
wrapper_start = source.index('static int set_disp_mode_auto(void)\n{')
wrapper_end = source.index('\n}\n', wrapper_start) + 3
production += source[wrapper_start:wrapper_end]
arm_start = source.index('\t/* Remember the live context,')
arm_end = source.index('\n\tif (hdev->cedst_policy)', arm_start)
production += '\nstatic void arm_on_unplug(struct hdmitx_dev *hdev)\n{\nunsigned long flags;\n' + source[arm_start:arm_end] + '\n}\n'

# These checks cover the call-site wiring omitted from the host driver stubs.
vsif_start = source.index('void hdmitx_set_vsif_pkt(enum eotf_type type,\n')
vsif = source[vsif_start:source.index('struct hdr10plus_para hdr10p_config_data;', vsif_start)]
assert vsif.index('hdmitx_queue_dv_reconnect(hdev);') < vsif.rindex('spin_unlock_irqrestore')
assert vsif.index('if (hdev->ready == 0)') < vsif.index('hdmitx_queue_dv_reconnect(hdev);')
plugin_start = source.index('static void hdmitx_hpd_plugin_handler(struct work_struct *work)\n{')
plugin = source[plugin_start:source.index('static void clear_rx_vinfo', plugin_start)]
assert plugin.index('hdmitx_get_edid(hdev);') < plugin.index('hdmitx_dv_reconnect_allowed(hdev)') < plugin.index('set_disp_mode_auto_locked();')
for name in ['hdmitx_early_suspend', 'hdmitx_reboot_notifier', 'amhdmitx_remove']:
    begin = source.index(name + '(')
    body = source[begin:source.index('\n}', begin)]
    assert 'hdmitx_cancel_dv_reconnect(' in body, name
assert 'INIT_WORK(&hdmitx_device->work_dv_reconnect, hdmitx_dv_reconnect_work);' in source
for name in ['store_attr', 'store_disp_mode']:
    begin = source.index(name + '(')
    body = source[begin:source.index('\n}', begin)]
    assert 'mutex_lock(&setclk_mutex);' in body and 'mutex_unlock(&setclk_mutex);' in body
assert 'set_disp_mode_auto();' not in plugin


preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define DV_IEEE_OUI 0xd046
#define CORRECT 1
#define DOLBY_VISION_OUTPUT_MODE_IPT 0
#define DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL 1
#define EOTF_T_DOLBYVISION 1
#define EOTF_T_LL_MODE 4
#define RGB_8BIT 1
#define MISC_HPD_GPI_ST 1
#define container_of(ptr, type, member) ((type *)((char *)(ptr) - offsetof(type, member)))
#define pr_info(...) ((void)0)
struct work_struct { bool queued; };
struct dv_info { int ieeeoui, block_flag, ver, length, Interface; };
struct hdmitx_dev {
    struct { struct dv_info dv_info; } rxcap;
    struct { int (*cntlmisc)(struct hdmitx_dev *, int, int); } hwop;
    int edid_spinlock;
    struct work_struct work_dv_reconnect;
    bool dv_reconnect_pending, hpd_lock, bist_lock, hpd_state, ready;
    int hdmi_current_eotf_type, hdmi_current_tunnel_mode;
};
static struct hdmitx_dev dev;
static bool xbmc_dv_non_ipt, xbmc_aml_linux_force_422;
static bool dv_enabled, dv_on, physical_hpd;
static int dv_mode, setclk_mutex, spin_depth, mutex_depth, modesets, force_flags;
static int programmed_cs, programmed_cd;
#ifdef CONFIG_AMLOGIC_MEDIA_ENHANCEMENT_DOLBYVISION
static int get_dolby_vision_mode(void) { return dv_mode; }
static bool is_dolby_vision_enable(void) { return dv_enabled; }
static bool is_dolby_vision_on(void) { return dv_on; }
static void dolby_vision_set_toggle_flag(int flags) { force_flags |= flags; }
#endif
#define spin_lock_irqsave(lock, flags) do { (void)(lock); (flags)=0; assert(spin_depth++ == 0); } while (0)
#define spin_unlock_irqrestore(lock, flags) do { (void)(lock); (void)(flags); assert(--spin_depth == 0); } while (0)
static void mutex_lock(int *lock) { (void)lock; assert(!spin_depth); assert(mutex_depth++ == 0); }
static void mutex_unlock(int *lock) { (void)lock; assert(!spin_depth); assert(--mutex_depth == 0); }
static void schedule_work(struct work_struct *work) { assert(spin_depth == 1); work->queued = true; }
static void cancel_work_sync(struct work_struct *work) { assert(!spin_depth && !mutex_depth); work->queued = false; }
static int hpd_read(struct hdmitx_dev *hdev, int op, int arg) { (void)hdev; (void)op; (void)arg; return physical_hpd; }
static int set_disp_mode_auto_locked(void) {
    assert(mutex_depth == 1 && !spin_depth);
    assert(!dev.dv_reconnect_pending && !dev.ready);
    assert(dev.hdmi_current_eotf_type == EOTF_T_DOLBYVISION);
    assert(dev.hdmi_current_tunnel_mode == RGB_8BIT);
    modesets++;
    /* Driver/hardware boundary stub: a modeset consumes current transport. */
    programmed_cs = 2; programmed_cd = 8;
    dev.ready = true;
    return 0;
}
'''
cases = r'''
static int checks;
#define CHECK(expr) do { assert(expr); checks++; } while (0)
static void reset(void) {
    memset(&dev, 0, sizeof(dev));
    dev.rxcap.dv_info = (struct dv_info){DV_IEEE_OUI, CORRECT, 2, 11, 2};
    dev.hwop.cntlmisc = hpd_read;
    dev.hpd_state = dev.ready = true;
    dev.hdmi_current_eotf_type = EOTF_T_DOLBYVISION;
    dev.hdmi_current_tunnel_mode = RGB_8BIT;
    dv_enabled = dv_on = physical_hpd = true;
    dv_mode = 1; xbmc_dv_non_ipt = xbmc_aml_linux_force_422 = false;
    modesets = force_flags = 0; programmed_cs = 1; programmed_cd = 12;
}
static void queue(void) { unsigned long flags; spin_lock_irqsave(&dev.edid_spinlock, flags); hdmitx_queue_dv_reconnect(&dev); spin_unlock_irqrestore(&dev.edid_spinlock, flags); }
static void run(void) { dev.work_dv_reconnect.queued = false; hdmitx_dv_reconnect_work(&dev.work_dv_reconnect); }
int main(void) {
    int i;
    reset(); queue(); CHECK(!dev.work_dv_reconnect.queued);
    arm_on_unplug(&dev); CHECK(dev.dv_reconnect_pending);
#ifndef CONFIG_AMLOGIC_MEDIA_ENHANCEMENT_DOLBYVISION
    (void)i;
    queue(); CHECK(!dev.work_dv_reconnect.queued);
    run(); CHECK(modesets == 0);
    hdmitx_cancel_dv_reconnect(&dev); CHECK(!dev.dv_reconnect_pending && !dev.ready);
#else
    /* Reported sequence: old DV cleared, regular reconnect, new actual VSIF. */
    dev.hdmi_current_eotf_type = 0; queue(); CHECK(!dev.work_dv_reconnect.queued);
    arm_on_unplug(&dev); CHECK(dev.dv_reconnect_pending); /* HPD bounce */
    dev.hdmi_current_eotf_type = EOTF_T_DOLBYVISION;
    queue(); CHECK(dev.work_dv_reconnect.queued);
    queue(); run(); CHECK(modesets == 1 && !dev.dv_reconnect_pending);
    CHECK(programmed_cs == 2 && programmed_cd == 8 && force_flags == 3);
    queue(); CHECK(!dev.work_dv_reconnect.queued); /* no modeset loop */
    /* Changes after queueing must be revalidated by the worker. */
    for (i=0; i<11; i++) {
        reset(); arm_on_unplug(&dev); queue(); CHECK(dev.work_dv_reconnect.queued);
        switch(i) {
        case 0: dev.hpd_lock=true; break;
        case 1: dev.bist_lock=true; break;
        case 2: xbmc_dv_non_ipt=true; break;
        case 3: xbmc_aml_linux_force_422=true; break;
        case 4: dv_enabled=false; break;
        case 5: dv_on=false; break;
        case 6: dv_mode=5; break;
        case 7: dev.rxcap.dv_info.ieeeoui=0; break;
        case 8: dev.rxcap.dv_info.Interface=0; break;
        case 9: dev.hdmi_current_eotf_type=EOTF_T_LL_MODE; break;
        case 10: dev.hdmi_current_tunnel_mode=0; break;
        }
        run(); CHECK(modesets == 0 && force_flags == 0 && !dev.dv_reconnect_pending);
    }
    for (i=0; i<3; i++) {
        reset(); arm_on_unplug(&dev); queue();
        if(i==0) physical_hpd=false;
        if(i==1) dev.hpd_state=false;
        if(i==2) dev.ready=false;
        run(); CHECK(modesets==0 && dev.dv_reconnect_pending);
        physical_hpd=dev.hpd_state=dev.ready=true;
        queue(); run(); CHECK(modesets==1);
    }
    reset(); arm_on_unplug(&dev); queue(); hdmitx_cancel_dv_reconnect(&dev);
    CHECK(!dev.work_dv_reconnect.queued && !dev.dv_reconnect_pending && !dev.ready);
    dev.ready=true; queue(); CHECK(!dev.work_dv_reconnect.queued);
    reset(); dev.hdmi_current_eotf_type=EOTF_T_LL_MODE; arm_on_unplug(&dev); CHECK(!dev.dv_reconnect_pending);
    reset(); xbmc_dv_non_ipt=true; arm_on_unplug(&dev); CHECK(!dev.dv_reconnect_pending);
    reset(); dev.rxcap.dv_info.block_flag=0; CHECK(!hdmitx_sink_supports_std_dv(&dev));
    reset(); dev.rxcap.dv_info.ver=0; CHECK(hdmitx_sink_supports_std_dv(&dev));
    dev.rxcap.dv_info.ver=1;
    for(i=0; i<16; i++) { dev.rxcap.dv_info.length=i; CHECK(hdmitx_sink_supports_std_dv(&dev) == (i==11 || i==14)); }
    dev.rxcap.dv_info.ver=2;
    for(i=0; i<5; i++) { dev.rxcap.dv_info.Interface=i; CHECK(hdmitx_sink_supports_std_dv(&dev) == (i==2 || i==3)); }
    dev.rxcap.dv_info.ver=3; CHECK(!hdmitx_sink_supports_std_dv(&dev));
#endif
    reset(); dev.ready=false;
    set_disp_mode_auto(); CHECK(modesets == 1);
    CHECK(spin_depth == 0 && mutex_depth == 0);
    printf("%d host checks passed\n", checks);
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='hdmitx-dv-reconnect-') as tmp:
    cfile = Path(tmp) / 'test.c'
    cfile.write_text(preamble + production + cases)
    for enabled in [True, False]:
        binary = Path(tmp) / ('dv-on' if enabled else 'dv-off')
        command = [os.environ.get('CC', 'cc'), '-std=gnu99', '-Wall', '-Wextra', '-Werror', '-fsanitize=undefined', '-o', str(binary), str(cfile)]
        if enabled:
            command.insert(1, '-DCONFIG_AMLOGIC_MEDIA_ENHANCEMENT_DOLBYVISION')
        subprocess.run(command, check=True)
        subprocess.run([str(binary)], check=True)
print('Call-site wiring checks passed; no kernel build or hardware test performed.')
