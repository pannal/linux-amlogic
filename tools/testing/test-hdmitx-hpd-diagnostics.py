#!/usr/bin/env python3
"""Check actual HPD IRQ/trigger bodies with recording hardware boundaries.

Checks event-only reads, acknowledgement/dispatch order and existing recovery
decisions. Does not emulate electrical HPD, real IRQ scheduling or receiver lock.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path,
                    default=Path(__file__).resolve().parents[2])
parser.add_argument('--source-file', type=Path)
parser.add_argument('--sanitize', action='store_true')
args = parser.parse_args()
source = (args.source_file or args.source_root /
          'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hw/hdmi_tx_hw.c').read_text()
start = source.index('static irqreturn_t intr_handler(')
irq = source[start:source.index('\n}', start) + 2]
start = source.index('\tcase MISC_TRIGGER_HPD:')
trigger = source[start:source.index('\n\tcase ', start + 1)]

fixture = r'''
#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define SYS "system: "
#define HW "hw: "
#define READ_ONCE(x) (x)
#define _RET_IP_ 1UL
#define HZ 250
#define IRQ_HANDLED 1
#define PERIPHS_REG_ADDR(x) (0x10000 + 4 * (x))
#define P_PERIPHS_PIN_MUX_B PERIPHS_REG_ADDR(0xbb)
enum { HDMITX_TOP_INTR_STAT, HDMITX_DWC_HDCP22REG_STAT,
       HDMITX_TOP_INTR_STAT_CLR, HDMITX_DWC_PHY_STAT0,
       HDMITX_TOP_HPD_FILTER, HDMITX_TOP_SKP_CNTL_STAT,
       HDMITX_TOP_NONCE_0, HDMITX_TOP_NONCE_1,
       HDMITX_TOP_NONCE_2, HDMITX_TOP_NONCE_3, MISC_TRIGGER_HPD };
enum { MESON_CPU_ID_G12A, MESON_CPU_ID_G12B, MESON_CPU_ID_SM1 };
enum { HDMI_TX_HPD_PLUGIN = 1, HDMI_TX_HPD_PLUGOUT = 2,
       HDMI_TX_INTERNAL_INTR = 4, HPD_READ_HPD_GPIO = 7 };
typedef int irqreturn_t;
struct hdmitx_dev {
    int chip_type, ready;
    unsigned hpd_lock, hdmitx_event;
    unsigned char hpd_state, rhpd_state;
    void *hdmi_wq;
    int work_hpd_plugin, work_hpd_plugout, work_internal_intr;
};
static struct hdmitx_dev device;
static unsigned top, phy, pad, mux, filter;
static int acks, samples, phy_queries, bandgap, earc, queues, writes, logs;
static int ids[3], delays[3];
static unsigned write_regs[10], write_values[10];
static char messages[10][512];
static int checks, cases;
#define CHECK(x) do { checks++; if (!(x)) { \
    fprintf(stderr, "line %d: %s\n", __LINE__, #x); assert(x); } } while (0)
static unsigned hdmitx_rd_reg(unsigned reg) {
    if (reg == HDMITX_TOP_INTR_STAT) { CHECK(acks == 0); return top; }
    if (reg == HDMITX_DWC_HDCP22REG_STAT) { CHECK(acks == 0); return 0; }
    if (reg == HDMITX_TOP_SKP_CNTL_STAT) return 1;
    CHECK(acks == 2); /* No diagnostic read before acknowledgement. */
    samples++;
    if (reg == HDMITX_DWC_PHY_STAT0) return phy;
    CHECK(reg == HDMITX_TOP_HPD_FILTER);
    return filter;
}
static unsigned hd_read_reg(unsigned reg) {
    CHECK(acks == 2); samples++;
    if (reg == PERIPHS_REG_ADDR(0x1b)) return pad;
    CHECK(reg == P_PERIPHS_PIN_MUX_B);
    return mux;
}
static void hdmitx_wr_reg(unsigned reg, unsigned value) {
    CHECK(writes < 10); write_regs[writes] = reg; write_values[writes++] = value;
    if (reg == HDMITX_TOP_INTR_STAT_CLR) {
        CHECK(acks == 0 && writes == 1 && value == ~0U); acks++;
    } else if (reg == HDMITX_DWC_HDCP22REG_STAT) {
        CHECK(acks == 1 && writes == 2 && value == 255); acks++;
    } else if (reg == HDMITX_TOP_INTR_STAT) {
        CHECK(logs == (device.chip_type == MESON_CPU_ID_G12B ? 1 : 0));
    } else {
        CHECK(reg >= HDMITX_TOP_NONCE_0 && reg <= HDMITX_TOP_NONCE_3);
    }
}
static void pr_info(const char *format, ...) {
    va_list ap;
    CHECK(logs < 10);
    va_start(ap, format);
    vsnprintf(messages[logs++], sizeof(messages[0]), format, ap);
    va_end(ap);
}
static int hdmitx_hpd_hw_op(int cmd) {
    CHECK(cmd == HPD_READ_HPD_GPIO); phy_queries++; return phy & 2;
}
static void hdmitx_phy_bandgap_en(struct hdmitx_dev *hdev) {
    CHECK(hdev == &device); bandgap++;
}
static void earc_hdmitx_hpdst(int state) { earc = state; }
static bool queue_delayed_work(void *wq, int *work, int delay) {
    CHECK(wq == device.hdmi_wq && queues < 3 && acks == 2);
    ids[queues] = work == &device.work_hpd_plugin ? 1 :
                  work == &device.work_hpd_plugout ? 2 : 4;
    delays[queues++] = delay; return true;
}
@IRQ@
static int inject(struct hdmitx_dev *hdev, unsigned argv) {
    switch (MISC_TRIGGER_HPD) {
@TRIGGER@
    }
    return -1;
}
static void reset(void) {
    device = (struct hdmitx_dev){.chip_type = MESON_CPU_ID_G12B,
        .hpd_state = 1, .rhpd_state = 1, .ready = 1, .hdmitx_event = 3};
    acks = samples = phy_queries = bandgap = queues = writes = logs = 0;
    earc = -1; top = 0; phy = 2; pad = 4; mux = 0x100; filter = 0xa0a0;
    memset(messages, 0, sizeof(messages));
}
int main(void) {
    int chip, lock, level;
    unsigned status;
    for (chip = 0; chip < 3; chip++)
    for (lock = 0; lock < 2; lock++)
    for (level = 0; level < 2; level++)
    for (status = 0; status < 32; status++) {
        unsigned effective = status, expected = 3;
        int n = 0;
        bool detailed = chip == MESON_CPU_ID_G12B && (status & 6);
        reset(); cases++; device.chip_type = chip; device.hpd_lock = lock;
        top = 0x80000000U | status; phy = level ? 2 : 0;
        pad = level ? 4 : 0;
        CHECK(intr_handler(0, &device) == IRQ_HANDLED);
        CHECK(acks == 2 && writes == 2);
        CHECK(samples == (detailed ? 4 : 0));
        CHECK((strstr(messages[0], "hpd_diag=1") != NULL) == detailed);
        if (detailed) {
            CHECK(strstr(messages[0], level ? "phy=2 pad=1" : "phy=0 pad=0"));
            CHECK(strstr(messages[0], "mux=1 filter=a0a0 old=1/1 ready=1 lock="));
        }
        if (lock) {
            CHECK(device.hdmitx_event == 3 && device.rhpd_state == 1);
            CHECK(queues == 0 && bandgap == 0 && phy_queries == 0 && earc == -1);
            continue;
        }
        if ((status & 6) == 6) effective &= ~(level ? 4U : 2U);
        CHECK(phy_queries == ((status & 6) == 6));
        if (effective & 2) {
            expected = (expected | 1) & ~2U;
            CHECK(ids[n] == 1 && delays[n++] == HZ / 2);
            CHECK(device.rhpd_state == 1 && bandgap == 1 && earc == 1);
        } else if (effective & 4) {
            expected = (expected | 2) & ~1U;
            CHECK(ids[n] == 2 && delays[n++] == 0);
            CHECK(device.rhpd_state == 0 && bandgap == 0 && earc == 0);
        } else CHECK(bandgap == 0 && earc == -1);
        if (effective & 1) {
            expected |= 4;
            CHECK(ids[n] == 4 && delays[n++] == HZ / 10);
        }
        CHECK(queues == n && device.hdmitx_event == expected);
        CHECK(device.hpd_state == 1 && device.ready == 1);
    }
    /* Independent pad/PHY disagreement must stay visible, not influence dispatch. */
    reset(); top = 4; pad = 0; intr_handler(0, &device); cases++;
    CHECK(strstr(messages[0], "phy=2 pad=0") && device.rhpd_state == 0);
    reset(); top = 2; phy = 0; intr_handler(0, &device); cases++;
    CHECK(strstr(messages[0], "phy=0 pad=1") && device.rhpd_state == 1);
    for (chip = 0; chip < 3; chip++)
    for (status = 0; status < 3; status++) {
        reset(); device.chip_type = chip; CHECK(inject(&device, status) == 0); cases++;
        CHECK(writes == 1 && samples == 0 && queues == 0);
        CHECK(write_regs[0] == HDMITX_TOP_INTR_STAT);
        CHECK(write_values[0] == (status == 1 ? 2U : 4U));
        if (chip == MESON_CPU_ID_G12B) {
            CHECK(strstr(messages[0], status == 1 ? "level=1" : "level=0"));
            CHECK(strstr(messages[0], "caller="));
        }
    }
    printf("%d production-body cases, %d checks passed\n", cases, checks);
}
'''
fixture = fixture.replace('@IRQ@', irq).replace('@TRIGGER@', trigger)
with tempfile.TemporaryDirectory(prefix='hdmitx-hpd-') as temporary:
    work = Path(temporary)
    (work / 'test.c').write_text(fixture)
    command = ['cc', '-std=gnu99', '-Wall', '-Wextra', '-Werror',
               '-Wno-unused-parameter', '-Wno-unused-function',
               '-g', str(work / 'test.c'), '-o', str(work / 'test')]
    if args.sanitize:
        command[1:1] = ['-fsanitize=address,undefined', '-fno-omit-frame-pointer']
    subprocess.run(command, check=True)
    subprocess.run([str(work / 'test')], check=True)
