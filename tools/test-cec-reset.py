#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Exercise production CEC-A initialization/reset with host register stubs.

Checks register state and reset timing, not real CEC transmission or Kodi exit.
"""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
)
root = parser.parse_args().source_root
api = (root / "drivers/amlogic/cec/hdmi_aocec_api.c").read_text()


def function(signature):
    start = api.index(signature)
    return api[start : api.index("\n}", start) + 2] + "\n"


production = "".join(
    function(signature)
    for signature in (
        "static void cec_set_reg_bits(",
        "static void ao_ceca_init(",
        "void ceca_hw_reset(",
    )
)

preamble = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
enum { AO_CEC_GEN_CNTL, AO_CEC_CLK_CNTL_REG0, AO_CEC_CLK_CNTL_REG1,
       AO_RTI_PWR_CNTL_REG0, AO_RTC_ALT_CLK_CNTL0, AO_RTC_ALT_CLK_CNTL1,
       AO_CRT_CLK_CNTL1, REGISTER_COUNT };
enum { CEC_CHIP_TXL = 1, CEC_CHIP_G12B, CEC_CHIP_A1 };
static struct { unsigned int chip_id; bool ee_to_ao; } platform;
static struct { typeof(platform) *plat_data; } device = { &platform }, *cec_dev = &device;
static unsigned int regs[REGISTER_COUNT], preserved, delays, arbitration, writes;
static bool checking_reset;
#define dprintk(...) ((void)0)
static unsigned int read_ao(unsigned int reg) {
  assert(reg < REGISTER_COUNT);
  return regs[reg];
}
static void write_ao(unsigned int reg, unsigned int value) {
  assert(reg < REGISTER_COUNT);
  if (checking_reset) {
    const unsigned int sequence[] = { 1, 3, 2 };
    assert(reg == AO_CEC_GEN_CNTL && writes < 3);
    assert(value == (preserved | sequence[writes++]));
  }
  regs[reg] = value;
}
static void udelay(unsigned int delay) {
  assert(checking_reset && delay == 9 && writes == 2);
  assert((regs[AO_CEC_GEN_CNTL] & 7) == 3);
  ++delays;
}
static void ceca_arbit_bit_time_set(unsigned int bit, unsigned int time,
                                    unsigned int flag) {
  assert(flag == 0 && (regs[AO_CEC_GEN_CNTL] & 7) == 2);
  assert((bit == 3 && time == 0x118) ||
         (bit == 5 && time == 0) || (bit == 7 && time == 0x2aa));
  arbitration |= 1u << bit;
}
"""
tests = r"""
static void reset_and_check(void) {
  preserved = regs[AO_CEC_GEN_CNTL] & ~7u;
  delays = arbitration = writes = 0;
  checking_reset = true;
  ceca_hw_reset();
  checking_reset = false;
  assert(regs[AO_CEC_GEN_CNTL] == (preserved | 2));
  assert(writes == 3 && delays == 1);
  assert(arbitration == ((1u << 3) | (1u << 5) | (1u << 7)));
}
int main(void) {
  /* Probe initializes the controller and immediately resets it. */
  for (unsigned int ao = 0; ao < 2; ++ao) {
    platform.chip_id = CEC_CHIP_G12B;
    platform.ee_to_ao = ao;
    ao_ceca_init();
    assert(regs[AO_CEC_GEN_CNTL] == (ao ? 0x710a : 2));
    reset_and_check();
    /* SYS_CTRL ownership handoffs and error/timeout recovery repeat reset. */
    for (unsigned int repeat = 0; repeat < 3; ++repeat) reset_and_check();
  }
  /* Preserve each unrelated bit and every initial reset/gate state. */
  for (unsigned int bit = 3; bit < 32; ++bit) {
    for (unsigned int low = 0; low < 8; ++low) {
      regs[AO_CEC_GEN_CNTL] = (1u << bit) | low;
      reset_and_check();
    }
  }
  puts("CEC-A reset preserves clock/filter settings and reset/arbitration timing");
}
"""

with tempfile.TemporaryDirectory(prefix="cec-reset-") as directory:
    path = Path(directory)
    source = path / "probe.c"
    source.write_text(preamble + production + tests)
    for cut in (False, True):
        binary = path / ("zapper-cut" if cut else "full")
        flags = ["-DCONFIG_AMLOGIC_ZAPPER_CUT"] if cut else []
        subprocess.run(
            [
                os.environ.get("CC", "cc"),
                "-std=gnu99",
                "-Wall",
                "-Wextra",
                "-Werror",
                *flags,
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run([str(binary)], check=True)
