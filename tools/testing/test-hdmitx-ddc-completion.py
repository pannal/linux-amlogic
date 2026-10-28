#!/usr/bin/env python3
"""Exercise production DDC helpers with a virtual clock and register model.

This checks transfer completion, not electrical I2C behavior or HDMI receiver
lock. No kernel build or device access is performed.
"""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--source-file', type=Path)
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
       HDMITX_DWC_I2CM_SOFTRSTZ, HDMITX_DWC_I2CM_READ_BUFF0 = 16 };
static int ddc_mutex, locked, active, resets, reads, reports, failures, checks;
static unsigned status, completion_status, operation;
static int64_t now, launched, complete_after;
#define CHECK(x) do { checks++; if (!(x)) { \
    fprintf(stderr, "line %d: %s\n", __LINE__, #x); failures++; } } while (0)
static void mutex_lock(int *p) { (void)p; assert(!locked); locked = 1; }
static void mutex_unlock(int *p) { (void)p; assert(locked); locked = 0; }
static void mdelay(int ms) { now += ms * 1000; }
static void usleep_range(int low, int high) { assert(low <= high); now += high; }
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
    assert(locked);
    if (reg == HDMITX_DWC_IH_I2CM_STAT0) status &= ~val; /* W1C */
    else if (reg == HDMITX_DWC_I2CM_OPERATION) {
        CHECK(!active); active = 1; launched = now; operation = val;
    } else if (reg == HDMITX_DWC_I2CM_SOFTRSTZ) {
        CHECK(val == 0); active = 0; resets++;
    }
}
static unsigned hdmitx_rd_reg(unsigned reg) {
    assert(locked);
    if (reg == HDMITX_DWC_IH_I2CM_STAT0) {
        if (active && complete_after >= 0 && now - launched >= complete_after) {
            status |= completion_status;
            active = 0;
        }
        return status;
    }
    reads++;
    CHECK(!active);
    if (reg == HDMITX_DWC_I2CM_DATAI) return 0x5a;
    assert(reg >= HDMITX_DWC_I2CM_READ_BUFF0 && reg < HDMITX_DWC_I2CM_READ_BUFF0 + 8);
    return 0x60 + reg - HDMITX_DWC_I2CM_READ_BUFF0;
}
'''
cases = r'''
static void setup(unsigned stale, unsigned completion, int64_t delay) {
    assert(!locked);
    active = resets = reads = reports = 0;
    status = stale; completion_status = completion; complete_after = delay;
    now = launched = 0;
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
    subprocess.run([str(exe)], check=True)
