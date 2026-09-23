#!/usr/bin/env python3
"""Check production CEC diagnostics with host register stubs, without hardware."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
root = parser.parse_args().source_root
main = (root / 'drivers/amlogic/cec/hdmi_ao_cec.c').read_text()
api = (root / 'drivers/amlogic/cec/hdmi_aocec_api.c').read_text()


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2] + '\n'


production = ''.join((function(api, 'unsigned int get_pin_status('),
                      function(api, 'const char *cec_pin_level('),
                      function(main, 'static ssize_t wake_config_show('),
                      function(main, 'static void cec_log_tx_timeout(')))
registers = sorted(set(re.findall(r'\b(?:AO_|PADCTRL_|PREG_|DWC_|CEC_(?:TX_|RX_|LOGICAL_))[A-Z0-9_]+', production)))
chips = sorted(set(re.findall(r'\bCEC_CHIP_[A-Z0-9_]+', production)) | {'CEC_CHIP_G12B'})
preamble = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#define PAGE_SIZE 4096
struct class { int unused; };
struct class_attribute { int unused; };
enum { CEC_A, CEC_B };
'''
preamble += 'enum { ' + ','.join(registers + chips) + ', REGISTER_COUNT };\n'
preamble += r'''
static unsigned int regs[REGISTER_COUNT];
static bool seen[REGISTER_COUNT], pin_status;
static struct { unsigned int chip_id; bool ee_to_ao; } platform;
static struct { unsigned int cec_func_config; } hdmi;
static struct {
  unsigned int cfg, phy_addr, framework_on, hal_flag;
  struct { unsigned int log_addr, addr_enable; } cec_info;
  typeof(platform) *plat_data;
} device, *cec_dev=&device;
static unsigned int read_ao(unsigned int reg) { seen[reg]=true; return regs[reg]; }
static unsigned int read_pad_reg(unsigned int reg) { return read_ao(reg); }
static unsigned int aocec_rd_reg(unsigned int reg) { return read_ao(reg); }
static unsigned int hdmirx_cec_read(unsigned int reg) { return read_ao(reg); }
static unsigned int cec_config(unsigned int value, bool write) {
  (void)value; assert(!write); return cec_dev->cfg;
}
static typeof(hdmi) *get_hdmitx_device(void) { return &hdmi; }
static unsigned int cecb_irq_stat(void) { return 3; }
static int scnprintf(char *buf, size_t size, const char *format, ...) {
  va_list args; int result;
  va_start(args,format); result=vsnprintf(buf,size,format,args); va_end(args);
  return result < (int)size ? result : (int)size-1;
}
static char log_buffer[4096];
static void capture_log(const char *format, ...) {
  va_list args; size_t len=strlen(log_buffer);
  va_start(args,format); vsnprintf(log_buffer+len,sizeof(log_buffer)-len,format,args); va_end(args);
}
#define CEC_ERR(...) capture_log(__VA_ARGS__)
'''
tests = r'''
int main(void) {
  char output[PAGE_SIZE+16]; ssize_t len;
  cec_dev->plat_data=&platform;
  platform.chip_id=CEC_CHIP_G12B;
  assert(!strcmp(cec_pin_level(),"unsupported"));
  platform.chip_id=CEC_CHIP_SC2;
  regs[PADCTRL_GPIOH_I]=0; assert(!strcmp(cec_pin_level(),"low"));
  regs[PADCTRL_GPIOH_I]=8; assert(!strcmp(cec_pin_level(),"high"));
  platform.chip_id=CEC_CHIP_TXHD2;
  regs[PADCTRL_GPIOAO_I_TXHD2]=1u<<29;
  assert(get_pin_status()==2); assert(!strcmp(cec_pin_level(),"high"));
  platform.chip_id=CEC_CHIP_G12B;
  hdmi.cec_func_config=0x7f; cec_dev->cfg=0x27;
  regs[AO_DEBUG_REG0]=0x8100003f; regs[AO_DEBUG_REG1]=0xa4512100;
  cec_dev->phy_addr=0x3000; cec_dev->cec_info.log_addr=4;
  cec_dev->cec_info.addr_enable=0x10; pin_status=true;
  memset(output,0x5a,sizeof(output));
  len=wake_config_show(NULL,NULL,output);
  assert(len==(ssize_t)strlen(output) && len<PAGE_SIZE);
  for(int i=PAGE_SIZE;i<(int)sizeof(output);i++) assert(output[i]==0x5a);
  assert(strstr(output,"runtime_config:0x27\n"));
  assert(strstr(output,"saved_config:0x3f\n"));
  assert(strstr(output,"runtime_physical_addr:0x3000\n"));
  assert(strstr(output,"saved_physical_addr:0x2100\n"));
  assert(strstr(output,"saved_logical_addr1:0x1\n"));
  assert(strstr(output,"saved_logical_addr2:0x4\n"));
  assert(strstr(output,"saved_device_type:0x5\n"));
  assert(strstr(output,"pin_level:unsupported\n"));
  assert(strstr(output,"pin_status_cached:1\n"));
#ifdef CONFIG_AMLOGIC_HDMITX
  assert(strstr(output,"boot_config:0x7f\n"));
#else
  assert(!strstr(output,"boot_config:"));
#endif
  for(unsigned int controller=0;controller<2;controller++) {
    for(unsigned int ao=0;ao<2;ao++) {
      platform.ee_to_ao=ao; log_buffer[0]=0; memset(seen,0,sizeof(seen));
      cec_log_tx_timeout(controller);
      assert(seen[AO_DEBUG_REG0] && seen[AO_DEBUG_REG1]);
      if(controller==CEC_A) {
        assert(seen[AO_CEC_INTR_MASKN] && seen[AO_CEC_INTR_STAT]);
        assert(seen[CEC_TX_MSG_STATUS] && seen[CEC_RX_MSG_STATUS]);
        assert(!seen[AO_CECB_GEN_CNTL] && !seen[DWC_CEC_CTRL]);
      } else {
        assert(seen[DWC_CEC_CTRL] && seen[DWC_CEC_TX_CNT]);
        assert(seen[AO_CECB_INTR_STAT]==(bool)ao);
        assert(!seen[AO_CEC_GEN_CNTL] && !seen[CEC_RX_MSG_STATUS]);
      }
      assert(strlen(log_buffer)<1024);
    }
  }
  puts("pin levels, wake-state formatting and timeout controller selection passed");
  return 0;
}
'''
# Ensure the diagnostic hook runs before the unchanged timeout recovery.
tx = function(main, 'int cec_ll_tx(')
timeout = tx[tx.index('if (ret == 0)'):]
assert timeout.index('cec_log_tx_timeout(cec_sel);') < timeout.index('cec_hw_reset(cec_sel);')
assert '__ATTR_RO(wake_config)' in main
assert 'pin_status: %d' not in main

with tempfile.TemporaryDirectory(prefix='cec-diagnostics-') as directory:
    path = Path(directory)
    source = path / 'probe.c'
    source.write_text(preamble + production + tests)
    for hdmitx in (False, True):
        binary = path / ('hdmitx' if hdmitx else 'no-hdmitx')
        flags = ['-DCONFIG_AMLOGIC_HDMITX'] if hdmitx else []
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu99', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-Wno-unused-function',
                        *flags, str(source), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
