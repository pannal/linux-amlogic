#!/usr/bin/env python3
"""Exercise production DDC helpers with a virtual clock and register model.

This checks transfer completion, not electrical I2C behavior or HDMI receiver
lock. The reactive wake path checks clock-restore ordering and lock ownership until
completion/abort, not SCL frequency, electrical recovery or hardware lock semantics. No kernel build or device access is performed.
"""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--source-file', type=Path)
parser.add_argument('--hardware-source', type=Path)
args = parser.parse_args()
source = (args.source_file or args.source_root / 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hw/hdmi_tx_ddc.c').read_text()
iopoll = (args.source_root / 'include/linux/iopoll.h').read_text()


def function(signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2] + '\n'


# Use the kernel's real timeout loop; only time, sleeping and MMIO are modelled.
start = iopoll.index('#define readx_poll_timeout(')
poll = iopoll[start:iopoll.index('\n})', start) + 4]
production = ''
if 'static uint32_t ddc_wait_complete(' in source:
    production += function('static uint32_t ddc_wait_complete(')
for name in ('ddc_write_1byte', 'ddc_read_1byte', 'ddc_read_8byte'):
    production += function('static uint32_t ' + name + '(')

production += function('void hdmitx_ddc_reactivate(' if 'void hdmitx_ddc_reactivate(' in source
                       else 'void edid_read_head_8bytes(')
hardware = (args.hardware_source or args.source_root /
            'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hw/hdmi_tx_hw.c').read_text()
start = hardware.index('\tcase MISC_I2C_REACTIVE:')
reactive_case = hardware[start:hardware.index('\t\tbreak;', start) + len('\t\tbreak;')]
production += '''
static void run_reactive_case(void) {
    struct hdmitx_dev device = {0}, *hdev = &device;
    switch (MISC_I2C_REACTIVE) {
''' + reactive_case + '\n    }\n}\n'

preamble = r'''
#include <assert.h>
#include <errno.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
typedef int64_t ktime_t;
enum { HDMITX_DWC_I2CM_SLAVE, HDMITX_DWC_I2CM_ADDRESS,
       HDMITX_DWC_I2CM_DATAO, HDMITX_DWC_I2CM_OPERATION,
       HDMITX_DWC_IH_I2CM_STAT0, HDMITX_DWC_I2CM_DATAI,
       HDMITX_DWC_I2CM_SOFTRSTZ, HDMITX_DWC_I2CM_READ_BUFF0 = 16,
       HDMITX_DWC_I2CM_SS_SCL_HCNT_1 = 32, HDMITX_DWC_I2CM_SS_SCL_HCNT_0,
       HDMITX_DWC_A_HDCPCFG1, HDMITX_DWC_HDCP22REG_CTRL };
enum { DDC_MUX_DDC, MISC_I2C_REACTIVE, EDID_SLAVE = 0x50 };
struct hdmitx_dev { int unused; };
static int64_t normal_clock_restored;
static unsigned scl_high;
static int ddc_mutex, locked, active, resets, reads, reports, failures, checks;
static unsigned status, completion_status, operation;
static int64_t now, launched, complete_after;
#define CHECK(x) do { checks++; if (!(x)) { \
    fprintf(stderr, "line %d: %s\n", __LINE__, #x); failures++; } } while (0)
static void mutex_lock(int *p) { (void)p; assert(!locked); locked = 1; }
static void mutex_unlock(int *p) { (void)p; assert(locked); CHECK(!active); locked = 0; }
/* Completion belongs to the controller clock, not to status-register reads. */
static void advance_time(int64_t us) {
    now += us;
    if (active && complete_after >= 0 && now - launched >= complete_after) {
        status |= completion_status;
        active = 0;
    }
}
static void mdelay(int ms) { advance_time(ms * 1000); }
static void usleep_range(int low, int high) { assert(low <= high); advance_time(high); }
static ktime_t ktime_get(void) { return now; }
static ktime_t ktime_add_us(ktime_t t, int us) { return t + us; }
static int ktime_compare(ktime_t a, ktime_t b) { return (a > b) - (a < b); }
#define might_sleep_if(x) ((void)(x))
static void pr_info(const char *format, ...) {
    char message[160];
    va_list ap;
    va_start(ap, format);
    vsnprintf(message, sizeof(message), format, ap);
    va_end(ap);
    reports++;
}
static void hdmitx_wr_reg(unsigned reg, unsigned val) {
    assert(locked); /* Clock changes belong to the same transaction. */
    if (reg == HDMITX_DWC_I2CM_SS_SCL_HCNT_1) { scl_high = (scl_high & 255) | (val << 8); return; }
    if (reg == HDMITX_DWC_I2CM_SS_SCL_HCNT_0) { scl_high = (scl_high & 0xff00) | val; return; }
    assert(locked);
    if (reg == HDMITX_DWC_IH_I2CM_STAT0) status &= ~val; /* W1C */
    else if (reg == HDMITX_DWC_I2CM_OPERATION) {
        CHECK(!active); active = 1; launched = now; operation = val;
        advance_time(0);
    } else if (reg == HDMITX_DWC_I2CM_SOFTRSTZ) {
        CHECK(val == 0); active = 0; resets++;
    }
}
static unsigned hdmitx_rd_reg(unsigned reg) {
    assert(locked);
    if (reg == HDMITX_DWC_IH_I2CM_STAT0) return status;
    reads++;
    CHECK(!active);
    if (reg == HDMITX_DWC_I2CM_DATAI) return 0x5a;
    assert(reg >= HDMITX_DWC_I2CM_READ_BUFF0 && reg < HDMITX_DWC_I2CM_READ_BUFF0 + 8);
    return 0x60 + reg - HDMITX_DWC_I2CM_READ_BUFF0;
}
static void hdmitx_ddc_hw_op(int cmd) { CHECK(cmd == DDC_MUX_DDC); }
static void hdmitx_hdcp_opr(int cmd) { CHECK(cmd == 4); }
static void hdmitx_set_reg_bits(unsigned reg, int val, int start, int width) {
    CHECK((reg == HDMITX_DWC_A_HDCPCFG1 && start == 0) ||
          (reg == HDMITX_DWC_HDCP22REG_CTRL && start == 2));
    CHECK(val == 0 && width == 1);
}
static void hdmi_hwi_init(struct hdmitx_dev *dev) {
    (void)dev;
    CHECK(locked && scl_high == 0xfff6);
    normal_clock_restored = now;
    scl_high = 0xcf;
}
'''
cases = r'''
static void setup(unsigned stale, unsigned completion, int64_t delay) {
    assert(!locked);
    active = resets = reads = reports = 0;
    status = stale; completion_status = completion; complete_after = delay;
    now = launched = 0;
    normal_clock_restored = -1; scl_high = 0xcf;
}
static unsigned transfer(int kind, uint8_t *buf) {
    if (kind == 0) return ddc_write_1byte(0x54, 0x20, 3);
    if (kind == 1) return ddc_read_1byte(0x54, 0x21, buf);
    return ddc_read_8byte(0x3a, 0, buf);
}
static void check_data(int kind, const uint8_t *buf, int success) {
    int i;
    int expected_reads = success ? (kind == 1 ? 1 : kind == 2 ? 8 : 0) : 0;
    CHECK(reads == expected_reads);
    for (i = 0; i < 8; i++) {
        uint8_t expected = 0xa5;
        if (success && kind == 1 && i == 0) expected = 0x5a;
        if (success && kind == 2) expected = 0x60 + i;
        CHECK(buf[i] == expected);
    }
    CHECK(!locked);
}
int main(void) {
    int kind, delay, stale, failed;
    const int delays[] = {0, 1000, 7000, 19000};
    uint8_t buf[8];
    for (kind = 0; kind < 3; kind++) {
        for (stale = 0; stale <= 3; stale++) {
            for (delay = 0; delay < 4; delay++) {
                setup(stale, 2, delays[delay]);
                memset(buf, 0xa5, sizeof(buf));
                CHECK(transfer(kind, buf) == 1);
                CHECK(now >= delays[delay] && now <= 20000);
                CHECK(operation == (kind == 0 ? 16U : kind == 1 ? 1U : 4U));
                CHECK(!active && !resets && !reports && !status);
                check_data(kind, buf, 1);
            }
            setup(stale, 2, -1); /* never completes, including stale DONE */
            memset(buf, 0xa5, sizeof(buf));
            CHECK(transfer(kind, buf) == 0);
            CHECK(now >= 20000 && now <= 22000);
            CHECK(!active && resets == 1 && reports == 1 && !status);
            check_data(kind, buf, 0);
            /* The next transfer must not inherit the unfinished operation. */
            completion_status = 2; complete_after = 7000;
            reads = reports = 0;
            CHECK(transfer(kind, buf) == 1);
            CHECK(!active && resets == 1 && !reports && !status);
            check_data(kind, buf, 1);
        }
        for (failed = 1; failed <= 3; failed += 2) { /* ERROR and ERROR|DONE */
            setup(0, failed, 7000);
            memset(buf, 0xa5, sizeof(buf));
            CHECK(transfer(kind, buf) == 0);
            CHECK(now >= 7000 && now < 20000);
            CHECK(!active && !resets && reports == 1 && !status);
            check_data(kind, buf, 0);
        }
    }
    /* Keep the legacy 2 ms slow launch and 5 ms normal-clock settle, but
     * never publish a free bus while the reactive operation remains active.
     * Delays are adversarial inputs, not measured controller timings. */
    for (delay = 0; delay < 6; delay++) {
        const int reactive_delays[] = {0, 1000, 7000, 19000, 140000, -1};
        int timeout = reactive_delays[delay] < 0 || reactive_delays[delay] > 25000;
        for (failed = 1; failed <= 3; failed++) {
            setup(3, failed, reactive_delays[delay]);
            run_reactive_case();
            CHECK(normal_clock_restored == 2000 && now >= 7000 && now <= 27000);
            CHECK(now >= (timeout ? 25000 : reactive_delays[delay]));
            CHECK(scl_high == 0xcf && resets == timeout);
            CHECK(reports == (timeout || (failed & 1)));
            CHECK(reads == 0 && operation == 4 && !locked && !status && !active);
            /* A late DONE must not make a subsequent never-completing read
             * succeed, nor may a checked read start on the reactive transfer. */
            complete_after = -1; completion_status = 2; reads = reports = 0;
            memset(buf, 0xa5, sizeof(buf));
            CHECK(transfer(1, buf) == 0);
            CHECK(!active && !status && resets == timeout + 1 && reports == 1);
            check_data(1, buf, 0);
            /* All checked operation kinds still work after that handoff. */
            for (kind = 0; kind < 3; kind++) {
                complete_after = 7000; reads = reports = 0;
                memset(buf, 0xa5, sizeof(buf));
                CHECK(transfer(kind, buf) == 1);
                CHECK(!active && !status && resets == timeout + 1 && !reports);
                check_data(kind, buf, 1);
            }
        }
    }
    printf("DDC completion: %d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
'''
with tempfile.TemporaryDirectory(prefix='hdmitx-ddc-') as tmp:
    c = Path(tmp) / 'test.c'
    exe = Path(tmp) / 'test'
    c.write_text(preamble + poll + '\n' + production + cases)
    subprocess.run([os.environ.get('CC', 'gcc'), '-std=gnu11', '-Wall', '-Wextra',
                    '-Werror', '-Wno-unused-function', '-fsanitize=undefined',
                    '-fno-sanitize-recover=all', str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True, timeout=15)
