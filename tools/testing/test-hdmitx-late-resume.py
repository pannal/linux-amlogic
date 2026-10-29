#!/usr/bin/env python3
"""Host checks of the production late-resume, syscore-resume and platform PM bodies.

The plugin work and the modeset are modelled at the setclk_mutex boundary, so
interleavings with late resume are reproduced deterministically. This does not
build a kernel or verify hardware, HPD timing or receiver lock. Each mutation
control must make the fixture fail. Run with Python 3 and a host C compiler.
"""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
args = parser.parse_args()
source = (args.source_root / 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hdmi_tx_main.c').read_text()


def body(signature, end='\n}\n'):
    start = source.index(signature)
    return source[start:source.index(end, start) + len(end)]


production = ''.join(body(s) for s in (
    'static int hdmitx_is_hdmi_vmode(char *mode_name)\n{',
    'static void hdmitx_late_resume(struct early_suspend *h)\n{',
    'static void hdmitx_syscore_resume(void)\n{',
    'static int amhdmitx_suspend(struct platform_device *pdev,',
    'static int amhdmitx_resume(struct platform_device *pdev)\n{'))
production += body('static struct syscore_ops hdmitx_syscore_ops = {', '\n};\n')

# Wiring the host model stands in for.
auto = body('static int set_disp_mode_auto_locked(void)\n{')
assert auto.index('const unsigned int gen = READ_ONCE(hdmitx_wake_gen);') < auto.index('hdmitx_set_display(hdev, vic);')
assert auto.index('if (ret >= 0) {') < auto.index('hdmitx_setup_gen = gen;') < auto.index('if (hdev->cur_VIC == HDMI_Unknown) {')
plugin = body('static void hdmitx_hpd_plugin_handler(struct work_struct *work)\n{')
order = ['if (!(hdev->hdmitx_event & (HDMI_TX_HPD_PLUGIN)))', 'mutex_lock(&setclk_mutex);',
         'hdev->hdmitx_event &= ~HDMI_TX_HPD_PLUGIN;', 'hdmitx_get_edid(hdev);',
         'set_disp_mode_auto_locked();', 'mutex_unlock(&setclk_mutex);']
assert [plugin.index(s) for s in order] == sorted(plugin.index(s) for s in order)
early = body('static void hdmitx_early_suspend(struct early_suspend *h)\n{')
assert 'phdmi->ready = 0;' in early and 'phdmi->cur_VIC = HDMI_Unknown;' in early
assert early.rstrip().endswith('phdmi->hpd_lock = 0;\n}')
probe = body('static int amhdmitx_probe(struct platform_device *pdev)\n{')
assert 'register_syscore_ops(&hdmitx_syscore_ops);' in probe
assert 'unregister_syscore_ops(&hdmitx_syscore_ops);' in body('static int amhdmitx_remove(struct platform_device *pdev)\n{')

preamble = r'''
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define pr_info(...) ((void)0)
#define pr_debug(...) ((void)0)
#define SYS ""
#define HDMI_TX_HPD_PLUGIN 0x1
#define HDMI_TX_HPD_PLUGOUT 0x2
#define EXTCON_DISP_HDMI 1
#define HDMI_WAKEUP 1
enum hdmi_vic { HDMI_Unknown = 0, VIC_2160P60 = 97, HDMI_VIC_FAKE = 0x200 };
enum { MISC_HPD_GPI_ST = 1, MISC_HPLL_FAKE, MISC_SUSFLAG, MISC_I2C_REACTIVE };
enum { HDMITX_EARLY_SUSPEND_RESUME_CNTL = 1, HDMITX_LATE_RESUME };
enum { CONF_AUDIO_MUTE_OP = 1, AUDIO_MUTE };
enum { DDC_RESET_HDCP = 1 };
enum { HDMITX_HPD_EVENT, HDMITX_HDCPPWR_EVENT, HDMITX_AUDIO_EVENT };
struct mutex { bool held; };
struct clk { int enabled; };
struct extcon_dev { int state; };
struct vinfo_s { char *name; };
struct early_suspend { void *param; };
struct platform_device { int id; };
typedef struct { int event; } pm_message_t;
struct syscore_ops { int (*suspend)(void); void (*resume)(void); };
struct hdmitx_dev {
    struct { struct clk *hdmi_clk_vapb, *hdmi_clk_vpu; } hdmitx_clk_tree;
    struct {
        int (*cntlmisc)(struct hdmitx_dev *, unsigned int, unsigned int);
        int (*cntl)(struct hdmitx_dev *, unsigned int, unsigned int);
        int (*cntlconfig)(struct hdmitx_dev *, unsigned int, unsigned int);
        int (*cntlddc)(struct hdmitx_dev *, unsigned int, unsigned long);
    } hwop;
    int ready, cur_VIC, hpd_lock, hpd_state, already_used, edid_parsing;
    unsigned int hdmitx_event;
    unsigned char *edid_ptr;
};

static int checks, failures;
#define CHECK(x) do { checks++; if (!(x)) { failures++; \
    fprintf(stderr, "%s:%d: %s: CHECK(%s)\n", __FILE__, __LINE__, scenario, #x); } } while (0)

enum op { FAKE_HPLL, EDID, MODESET, RESET, SUSFLAG };
enum actor { LATE, PLUGIN };
static const char *scenario = "";
static struct hdmitx_dev hdmitx_device;
static struct mutex setclk_mutex;
static struct clk vapb, vpu;
static struct extcon_dev ext_hdmi, ext_power, ext_audio;
static struct extcon_dev *hdmitx_extcon_hdmi = &ext_hdmi, *hdmitx_extcon_power = &ext_power,
    *hdmitx_extcon_audio = &ext_audio;
static struct vinfo_s vinfo = { "2160p60hz" };
static unsigned int hdmitx_wake_gen, hdmitx_setup_gen;
static bool physical_hpd, worker_queued, worker_at_unlock, in_worker;
static enum actor actor;
static struct { enum op op; enum actor who; } ops[64];
static int nops;

static void record(enum op op)
{
    if (nops < 64)
        ops[nops].op = op, ops[nops].who = actor, nops++;
    /* Every late-resume hardware step except SUSFLAG is serialized. */
    if (actor == LATE && op != SUSFLAG)
        CHECK(setclk_mutex.held);
}
static int count(enum op op, enum actor who)
{
    int i, n = 0;
    for (i = 0; i < nops; i++)
        n += ops[i].op == op && ops[i].who == who;
    return n;
}
/* Nothing may disturb the link after the plugin work set it up. */
static bool plugin_setup_last(void)
{
    int i, last = -1;
    for (i = 0; i < nops; i++)
        if (ops[i].op == MODESET && ops[i].who == PLUGIN)
            last = i;
    for (i = last + 1; last >= 0 && i < nops; i++)
        if (ops[i].op != SUSFLAG)
            return false;
    return last >= 0;
}

static void plugin_work(void);
static void mutex_lock(struct mutex *m) { CHECK(!m->held); m->held = true; }
static void mutex_unlock(struct mutex *m)
{
    CHECK(m->held);
    m->held = false;
    /* Model plugin work blocked on setclk_mutex: it takes over on release. */
    if (m == &setclk_mutex && worker_at_unlock && worker_queued && !in_worker) {
        worker_queued = false;
        plugin_work();
    }
}
static int clk_prepare_enable(struct clk *c) { c->enabled++; return 0; }
static int extcon_set_state_sync(struct extcon_dev *e, int id, int state) { (void)id; e->state = state; return 0; }
static void hdmitx_set_uevent(int event, int value) { (void)event; (void)value; }
static void hdmitx_notify_hpd(int hpd, void *p) { (void)hpd; (void)p; }
static struct vinfo_s *hdmitx_get_current_vinfo(void) { return &vinfo; }
static enum hdmi_vic hdmitx_edid_vic_tab_map_vic(const char *name) { return name && *name ? VIC_2160P60 : HDMI_Unknown; }
static void hdmitx_get_edid(struct hdmitx_dev *hdev) { (void)hdev; record(EDID); }
static int cntlmisc(struct hdmitx_dev *hdev, unsigned int cmd, unsigned int argv)
{
    (void)argv;
    if (cmd == MISC_HPD_GPI_ST)
        return physical_hpd;
    if (cmd == MISC_HPLL_FAKE) {
        record(FAKE_HPLL);
        hdev->cur_VIC = HDMI_VIC_FAKE;
    }
    if (cmd == MISC_SUSFLAG) {
        record(SUSFLAG);
        CHECK(!setclk_mutex.held);
    }
    return 0;
}
static int cntl(struct hdmitx_dev *hdev, unsigned int cmd, unsigned int argv)
{
    (void)hdev;
    if (cmd == HDMITX_EARLY_SUSPEND_RESUME_CNTL && argv == HDMITX_LATE_RESUME)
        record(RESET);
    return 0;
}
static int cntlconfig(struct hdmitx_dev *hdev, unsigned int cmd, unsigned int argv) { (void)hdev; (void)cmd; (void)argv; return 0; }
static int cntlddc(struct hdmitx_dev *hdev, unsigned int cmd, unsigned long argv) { (void)hdev; (void)cmd; (void)argv; return 0; }

/* Model of the production modeset's generation and state bookkeeping. */
static int set_disp_mode_auto_locked(void)
{
    const unsigned int gen = READ_ONCE(hdmitx_wake_gen);

    CHECK(setclk_mutex.held);
    hdmitx_device.ready = 0;
    record(MODESET);
    hdmitx_device.cur_VIC = VIC_2160P60;
    hdmitx_setup_gen = gen;
    hdmitx_device.ready = 1;
    return 0;
}
static int set_disp_mode_auto(void)
{
    int ret;

    mutex_lock(&setclk_mutex);
    ret = set_disp_mode_auto_locked();
    mutex_unlock(&setclk_mutex);
    return ret;
}
'''

cases = r'''
/* Model of hdmitx_hpd_plugin_handler, in the order asserted on its source. */
static void plugin_work(void)
{
    if (!(hdmitx_device.hdmitx_event & HDMI_TX_HPD_PLUGIN))
        return;
    in_worker = true;
    mutex_lock(&setclk_mutex);
    actor = PLUGIN;
    hdmitx_device.hdmitx_event &= ~HDMI_TX_HPD_PLUGIN;
    hdmitx_get_edid(&hdmitx_device);
    set_disp_mode_auto_locked();
    hdmitx_device.hpd_state = 1;
    actor = LATE;
    mutex_unlock(&setclk_mutex);
    in_worker = false;
}

static void start(const char *name)
{
    scenario = name;
    memset(&hdmitx_device, 0, sizeof(hdmitx_device));
    hdmitx_device.hdmitx_clk_tree.hdmi_clk_vapb = &vapb;
    hdmitx_device.hdmitx_clk_tree.hdmi_clk_vpu = &vpu;
    hdmitx_device.hwop.cntlmisc = cntlmisc;
    hdmitx_device.hwop.cntl = cntl;
    hdmitx_device.hwop.cntlconfig = cntlconfig;
    hdmitx_device.hwop.cntlddc = cntlddc;
    setclk_mutex.held = false;
    physical_hpd = true;
    worker_queued = worker_at_unlock = in_worker = false;
    actor = LATE;
    nops = 0;
    /* Output running before suspend, set up in the previous wake. */
    hdmitx_device.hdmitx_event |= HDMI_TX_HPD_PLUGIN;
    plugin_work();
    nops = 0;
}
/* State effects of hdmitx_early_suspend asserted on its source. */
static void early_suspend(void)
{
    hdmitx_device.ready = 0;
    hdmitx_device.cur_VIC = HDMI_Unknown;
    hdmitx_device.hpd_lock = 0;
}
static void hpd_pulse(void)
{
    hdmitx_device.hdmitx_event |= HDMI_TX_HPD_PLUGIN;
    hdmitx_device.hdmitx_event &= ~HDMI_TX_HPD_PLUGOUT;
    worker_queued = true;
}
static void run_worker(void)
{
    if (worker_queued) {
        worker_queued = false;
        plugin_work();
    }
}
static void platform_suspend(void)
{
    struct platform_device pdev = { 0 };
    pm_message_t msg = { 0 };

    CHECK(amhdmitx_suspend(&pdev, msg) == 0);
}
static void system_sleep(void)
{
    hdmitx_syscore_ops.resume();
}
static void platform_resume(void)
{
    struct platform_device pdev = { 0 };

    CHECK(amhdmitx_resume(&pdev) == 0);
}
static void late_resume(void)
{
    struct early_suspend es = { &hdmitx_device };

    hdmitx_late_resume(&es);
    CHECK(!setclk_mutex.held);
    CHECK(count(SUSFLAG, LATE) == 1);
}
static void full_fallback(bool hpd)
{
    CHECK(count(FAKE_HPLL, LATE) == 1 && count(EDID, LATE) == (hpd ? 1 : 0));
    CHECK(count(MODESET, LATE) == 1 && count(RESET, LATE) == 1);
    CHECK(hdmitx_device.cur_VIC == VIC_2160P60 && hdmitx_device.ready);
}

int main(void)
{
    /* Control: the plugin work restored the output during this wake. */
    start("restored before late resume");
    early_suspend(); platform_suspend(); hpd_pulse(); system_sleep();
    run_worker(); platform_resume(); late_resume();
    CHECK(count(MODESET, PLUGIN) == 1 && plugin_setup_last());
    CHECK(count(FAKE_HPLL, LATE) == 0 && count(EDID, LATE) == 0);
    CHECK(count(MODESET, LATE) == 0 && count(RESET, LATE) == 0);

    /* Control: no HPD pulse, late resume sets the output up as before. */
    start("no plugin");
    early_suspend(); platform_suspend(); system_sleep(); platform_resume(); late_resume();
    full_fallback(true);
    start("no plugin, HPD low");
    early_suspend(); platform_suspend(); physical_hpd = false;
    system_sleep(); platform_resume(); late_resume();
    full_fallback(false);

    /* Review P2 #1: plugin work is pending when late resume decides and
     * takes setclk_mutex as soon as late resume releases it. */
    start("plugin pending at late resume");
    early_suspend(); platform_suspend(); system_sleep(); hpd_pulse(); worker_at_unlock = true;
    platform_resume(); late_resume();
    CHECK(count(MODESET, LATE) == 0 && count(EDID, LATE) == 0);
    CHECK(count(MODESET, PLUGIN) == 1 && plugin_setup_last());
    CHECK(hdmitx_device.cur_VIC == VIC_2160P60 && !worker_queued);
    /* The plugin work may still be queued behind its delay. */
    start("plugin queued after late resume");
    early_suspend(); platform_suspend(); system_sleep(); hpd_pulse();
    platform_resume(); late_resume(); run_worker();
    CHECK(count(MODESET, LATE) == 0 && count(MODESET, PLUGIN) == 1 && plugin_setup_last());
    /* An unplug before the deferred work runs leaves no stray setup. */
    start("plugin cancelled by unplug");
    early_suspend(); platform_suspend(); system_sleep(); hpd_pulse();
    platform_resume(); late_resume();
    hdmitx_device.hdmitx_event = HDMI_TX_HPD_PLUGOUT; physical_hpd = false; run_worker();
    CHECK(count(MODESET, LATE) == 0 && count(MODESET, PLUGIN) == 0);

    /* Review P2 #2: plugin work set the mode after platform suspend, before
     * the system slept. Late resume must not keep that output. */
    start("modeset after platform suspend");
    early_suspend(); platform_suspend(); hpd_pulse(); run_worker();
    system_sleep(); platform_resume(); late_resume();
    full_fallback(true);
    start("modeset in early suspend window");
    early_suspend(); hpd_pulse(); run_worker(); platform_suspend();
    system_sleep(); platform_resume(); late_resume();
    full_fallback(true);
    /* Without a system sleep the window output stays live and is kept. */
    start("suspend aborted before sleep");
    early_suspend(); hpd_pulse(); run_worker(); late_resume();
    CHECK(count(MODESET, LATE) == 0 && count(RESET, LATE) == 0 && count(FAKE_HPLL, LATE) == 0);

    printf("late resume: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
'''

# Each control reverts one part of the fix; the fixture must then fail.
mutations = {
    'decision released before the fallback': [
        ('\tpending = !restored && (phdmi->hdmitx_event & HDMI_TX_HPD_PLUGIN);',
         '\tpending = false;\n\tmutex_unlock(&setclk_mutex);'),
        ('\t\tset_disp_mode_auto_locked();', '\t\tset_disp_mode_auto();'),
        ('\tmutex_unlock(&setclk_mutex);\n\tphdmi->hwop.cntlmisc(phdmi, MISC_SUSFLAG, 0);',
         '\tphdmi->hwop.cntlmisc(phdmi, MISC_SUSFLAG, 0);')],
    'pending plugin ignored': [
        ('\tpending = !restored && (phdmi->hdmitx_event & HDMI_TX_HPD_PLUGIN);', '\tpending = false;')],
    'no wake generation': [
        ('\t\t   hdmitx_setup_gen == READ_ONCE(hdmitx_wake_gen) &&\n', '')],
    'syscore resume not wired': [
        ('\tWRITE_ONCE(hdmitx_wake_gen, hdmitx_wake_gen + 1);', '\t(void)0;')],
    'fake HPLL on a restored link': [
        ('\tif (!restored && info && (hdmitx_is_hdmi_vmode(info->name) == 1))',
         '\tif (info && (hdmitx_is_hdmi_vmode(info->name) == 1))')],
    'controller reset of a restored link': [
        ('\tif (!restored)\n\t\tphdmi->hwop.cntl(', '\tif (1)\n\t\tphdmi->hwop.cntl(')],
}


def build_and_run(tmp, name, code):
    cfile = Path(tmp) / (name + '.c')
    binary = Path(tmp) / name
    cfile.write_text(preamble + code + cases)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu99', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', '-Wno-unused-parameter', '-fsanitize=undefined',
                    '-fno-sanitize-recover=all', '-o', str(binary), str(cfile)], check=True)
    return subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)


with tempfile.TemporaryDirectory(prefix='hdmitx-late-resume-') as tmp:
    result = build_and_run(tmp, 'production', production)
    print(result.stdout, end='')
    if result.returncode:
        raise SystemExit(result.stderr)
    for index, (name, edits) in enumerate(mutations.items()):
        code = production
        for old, new in edits:
            assert code.count(old) == 1, (name, old)
            code = code.replace(old, new)
        result = build_and_run(tmp, 'mutation%d' % index, code)
        assert result.returncode, 'mutation control passed: ' + name
        print('mutation rejected: %s (%s)' % (name, result.stderr.splitlines()[0]))
print('Wiring checks passed; no kernel build or hardware test performed.')
