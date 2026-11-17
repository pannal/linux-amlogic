#!/usr/bin/env python3
"""Exercise production AFBC completion, RDMA append, rebuild and backup replay.

MMIO executes the published table once per modeled vsync with the G12B AFBC
backup enabled. The header-address recovery exclusion comes from the production
source. IRQ timing, GPU behavior and physical scanout remain device checks.
"""
import argparse
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / "tools/test-osd-rdma-recovery.py"))["function"]

PRELUDE = r"""

#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint32_t u32;
typedef int32_t s32;
typedef uint8_t u8;
#define OSD1 0
#define VIU1 0
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x, v) ((x) = (v))
#define rmb() ((void)0)
#define smp_rmb() ((void)0)
#define wmb() ((void)0)
#define pr_info(...) ((void)0)
#define pr_debug(...) ((void)0)
#define WARN_ON(x) assert(!(x))
#define pr_err(...) ((void)0)
#define EXPORT_SYMBOL(x)
#define spin_lock_irqsave(l, f) ((f) = 0)
#define spin_unlock_irqrestore(l, f) ((void)(f))
#define OSD_RDMA_CHANNEL_INDEX 3
#define DONE (1U << (24 + OSD_RDMA_CHANNEL_INDEX))
#define OSD_RDMA_UPDATE_RETRY_COUNT 3
#define RDMA_TEMP_TBL_SIZE (512 * 8)
#define OSD_RDMA_FLAG_REG 99
#define OSD_RDMA_STATUS_IS_REJECT false
#define OSD_RDMA_STATUS_CLEAR_REJECT ((void)0)
#define OSD_RDMA_STATUS_MARK_TBL_RST 0x99
#define OSD_RDMA_STATUS_MARK_TBL_DONE 0
#define RDMA_STATUS 1
#define RDMA_CTRL 2
#define END_ADDR 3
#define VPP_MISC 4
#define VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0 0x3a10
#define VPU_MAFBC_COMMAND 0x3a05
#define VPU_MAFBC_IRQ_MASK 0x3a03
#define VIU_SW_RESET 0x1100
#define VIU_OSD1_TEST_RDDATA 0x1101
#define OSD1_AFBCD_ENABLE 0x1102
#define HW_RESET_OSD1_REGS 1
#define HW_RESET_AFBCD_REGS 2
#define HW_RESET_MALI_AFBCD_REGS 4
#define __MESON_CPU_MAJOR_ID_GXM 3
#define MALI_AFBC 2
#define OSD_HIGH_ONE 2
#define OSD_REG_BACKUP_COUNT 0
#define OSD_AFBC_REG_BACKUP_COUNT 0
#define MALI_AFBC_REG_BACKUP_COUNT 1
struct rdma_table_item
{
  u32 addr, val;
};
static struct rdma_table_item rdma_table[512], storage[512], *rdma_temp_tbl = storage;
static u32 item_count, table_paddr = 0x1000, end, done, address;
static unsigned int reset_writes;
static u32 osd_rdma_done_seq[1], osd_rdma_done_jiffies[1], jiffies;
static int rdma_enable = 1, osd_rdma_handle = 3, rdma_reset_tigger_flag, disable_osd_rdma_reset;
static u32 vsync_irq_count, rdma_irq_count, rdma_recovery_count, debug_rdma_status,
    dump_reg_trigger;
static u32 rdma_table_resets, osd_afbc_scanout_replayed, osd_afbc_scanout_replay_seq;
static bool osd_rdma_done;
static struct
{
  bool used;
} osd_afbc_scanout[1] = {{true}};
static struct
{
  int rdma_trace_enable, rdma_trace_num, rdma_trace_reg[4], hw_rdma_en, afbc_regs_backup;
  struct
  {
    int cpu_id, afbc_type, osd_ver;
  } osd_meson_dev;
} osd_hw = {.hw_rdma_en = 1, .afbc_regs_backup = 1, .osd_meson_dev = {4, MALI_AFBC, OSD_HIGH_ONE}};
static struct
{
  u32 osd_ctrl_stat;
} hw_osd_reg_array[1];
static u32 osd_reg_backup[1], osd_backup[1], osd_afbc_reg_backup[1], osd_afbc_backup[1];
static u32 mali_afbc_reg_backup[] = {VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0}, mali_afbc_backup[32];
static u32 backup_enable = HW_RESET_MALI_AFBCD_REGS;
static u32 osd_reg_read(u32 reg)
{
  if (reg == RDMA_STATUS)
    return done;
  if (reg == END_ADDR)
    return end;
  return 0;
}
static void osd_reg_write(u32 reg, u32 val)
{
  if (reg == RDMA_CTRL)
    done &= ~val;
  if (reg == END_ADDR)
    end = val;
  if (reg == VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0)
    address = val;
  if (reg == VIU_SW_RESET && val)
    ++reset_writes;
}
static void update_backup_reg(u32 addr, u32 val)
{
  if (addr == VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0)
    mali_afbc_backup[addr - VPU_MAFBC_IRQ_MASK] = val;
}
static void update_recovery_item(u32 addr, u32 val)
{
}
static void osd_rdma_mem_cpy(struct rdma_table_item *dst, struct rdma_table_item *src, u32 len)
{
  memcpy(dst, src, len);
}
static bool is_rdma_reg(u32 addr)
{
  return true;
}
static void viu2_osd_reg_set(u32 addr, u32 val)
{
  assert(false);
}
static void osd_update_scan_mode(void);
static void osd_update_3d_mode(void)
{
}
static u32 get_output_device_id(int index)
{
  return VIU1;
}
static void wait_rdma_done_wakeup(void);
static void osd_hw_reset(void);
"""

TESTS = r"""
enum QueuePoint
{
  NONE,
  BEFORE_IRQ,
  WAKE,
  AFTER_REBUILD,
  AFTER_IRQ
};
static enum QueuePoint queue_point;
static u32 queued_sequence;
static u32 next_header;
static unsigned int frames_to_queue;
static unsigned int frames_completed;
static bool pending_frame;

static void queue_flip(void)
{
  assert(!pending_frame);
  assert(update_table_item(VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0, next_header, 0) == 0);
  assert(update_table_item(VPU_MAFBC_COMMAND, 1, 0) == 0);
  queued_sequence = osd_afbc_scanout_seq_queued(OSD1);
  pending_frame = true;
}

static void queue_at(enum QueuePoint point)
{
  if (queue_point == point && frames_to_queue)
  {
    --frames_to_queue;
    queue_flip();
  }
}

static void check_completion(void)
{
  if (!pending_frame || !osd_afbc_scanout_settled(OSD1, queued_sequence, 1))
    return;
  assert(address == next_header);
  pending_frame = false;
  ++frames_completed;
  ++next_header;
}

static void wait_rdma_done_wakeup(void)
{
  check_completion();
  queue_at(WAKE);
}

static void osd_update_scan_mode(void)
{
  queue_at(AFTER_REBUILD);
}

static void osd_hw_reset(void)
{
  /* AFBC active on G12B requests this reset every frame. The production
   * flush decides whether to reset hardware and which backups to replay. */
  osd_rdma_reset_and_flush(HW_RESET_MALI_AFBCD_REGS);
}

static void tick(void)
{
  u32 count = (end + 1 - table_paddr) / sizeof(struct rdma_table_item);
  for (u32 i = 0; i < count; ++i)
    osd_reg_write(rdma_table[i].addr, rdma_table[i].val);
  done = DONE;
  ++jiffies;
  queue_at(BEFORE_IRQ);
  osd_rdma_irq(NULL);
  done = 0; /* The RDMA manager also acknowledges after the callback. */
  queue_at(AFTER_IRQ);
  check_completion();
}

static void setup(bool disable_reset, enum QueuePoint point, unsigned int frames)
{
  memset(rdma_table, 0, sizeof(rdma_table));
  memset(storage, 0, sizeof(storage));
  item_count = 2;
  rdma_table[0].addr = rdma_table[1].addr = OSD_RDMA_FLAG_REG;
  end = table_paddr + 15;
  done = jiffies = reset_writes = 0;
  address = 0xa;
  next_header = 0xb;
  osd_rdma_done_seq[0] = 0;
  rdma_table_resets = osd_afbc_scanout_replayed = osd_afbc_scanout_replay_seq = 0;
  mali_afbc_backup[VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0 - VPU_MAFBC_IRQ_MASK] = address;
  disable_osd_rdma_reset = disable_reset;
  queue_point = point;
  frames_to_queue = frames;
  frames_completed = 0;
  pending_frame = false;
}

static void first_flip(bool disable_reset, enum QueuePoint point)
{
  setup(disable_reset, point, 1);
  tick();
  /* The preceding table could not contain this newly appended header. */
  assert(address == 0xa);
  assert(frames_completed == 0 && pending_frame);
  assert(!osd_afbc_scanout_settled(OSD1, queued_sequence, 1));
  tick();
  assert(address == 0xb);
  /* Appending during the IRQ after its count but before its final ACK may
   * conservatively count one extra vsync. It must never certify early. */
  if (pending_frame)
    tick();
  assert(frames_completed == 1 && !pending_frame);
  if (disable_reset)
    assert(reset_writes == 0);
  else
    assert(reset_writes > 0);
}

static void queued_cadence(bool disable_reset)
{
  setup(disable_reset, WAKE, 40);
  tick(); /* The first wake supplies the first queued frame. */
  assert(queued_sequence == 1);
  for (unsigned int i = 1; i <= 40; ++i)
  {
    tick();
    assert(frames_completed == i);
    assert(address == 0xa + i);
  }
  assert(!pending_frame && frames_to_queue == 0);
  if (disable_reset)
    assert(reset_writes == 0);
}

static void restart_receipts(void)
{
  setup(false, NONE, 0);
  osd_rdma_done_seq[0] = 10;
  rdma_table_resets = 1;
  assert(!osd_afbc_scanout_settled(OSD1, 1, 1));
  osd_afbc_scanout_replayed = 1;
  osd_afbc_scanout_replay_seq = 10;
  assert(!osd_afbc_scanout_settled(OSD1, 1, 1));
  osd_rdma_done_seq[0] = 11;
  assert(osd_afbc_scanout_settled(OSD1, 1, 1));
}

int main(void)
{
  for (int disable_reset = 0; disable_reset <= 1; ++disable_reset)
  {
    for (enum QueuePoint point = BEFORE_IRQ; point <= AFTER_IRQ; ++point)
      first_flip(disable_reset, point);
    queued_cadence(disable_reset);
  }
  restart_receipts();
  puts("PASS AFBC header delivery, late receipts, reset-disabled replay, 40 flips in 40 vsyncs and "
       "restart receipts");
}
"""


def harness():
    directory = ROOT / "drivers/amlogic/media/osd"
    rdma = (directory / "osd_rdma.c").read_text()
    hardware = (directory / "osd_hw.c").read_text()
    backup = (directory / "osd_backup.c").read_text()
    recovery = function(backup, "static s32 get_recovery_item_g12a(")
    # Extract the actual backed-header exclusion, which caused the lost write.
    # Other register classes are outside this AFBC header/command fixture.
    exclusion = re.search(
        r"case VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0:(.*?)\n\t\tbase =",
        recovery, re.S).group(1)
    adapter = (
        "static int get_recovery_item(u32 addr, u32 *value, u32 *mask) {\n"
        "  int ret = -1;\n"
        "  switch (addr) {\n"
        "  case VPU_MAFBC_HEADER_BUF_ADDR_LOW_S0:\n" + exclusion +
        "\n    break;\n  }\n  return ret;\n}\n")
    functions = [function(rdma, signature) for signature in (
        "static inline void reset_rdma_table(",
        "static int update_table_item(",
        "static inline int wrtie_reg_internal(",
        "bool osd_rdma_table_pending(",
        "void osd_rdma_ack_vsync(",
        "u32 osd_rdma_reset_count(",
        "int osd_rdma_reset_and_flush(",
    )]
    functions.extend(function(hardware, signature) for signature in (
        "static u32 osd_afbc_scanout_seq_now(",
        "static u32 osd_afbc_scanout_seq_queued(",
        "bool osd_afbc_scanout_settled(",
        "void osd_update_vsync_hit(",
    ))
    functions.append(function(rdma, "static void osd_rdma_irq("))
    return PRELUDE + adapter + "\n".join(functions) + TESTS


def run(source, negative=False):
    with tempfile.TemporaryDirectory(prefix="osd-afbc-queued-") as temporary:
        directory = Path(temporary)
        path = directory / "test.c"
        binary = directory / "test"
        path.write_text(source)
        command = [os.environ.get("CC", "gcc"), "-std=gnu99",
                   "-Werror=implicit-function-declaration", "-fno-pie", "-no-pie",
                   str(path), "-o", str(binary)]
        if not negative:
            command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
        subprocess.run(command, check=True)
        result = subprocess.run([str(binary)], capture_output=negative, text=True)
        if negative:
            assert result.returncode != 0 and "Assertion" in result.stderr, result.stderr
        else:
            result.check_returncode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    source = harness()
    run(source)
    if args.negative_controls:
        for label, before, after in (
            ("drop AFBC backup replay with hardware reset disabled",
             "reset_bit &= HW_RESET_MALI_AFBCD_REGS;", "reset_bit = 0;"),
            ("leave the completed table pending until after waking the waiter",
             "\t\tosd_rdma_ack_vsync();", "\t\t(void)0;"),
            ("certify a flip using an uncounted completed table",
             "return osd_afbc_scanout_seq_now(index) + late;",
             "return osd_afbc_scanout_seq_now(index);"),
        ):
            assert source.count(before) == 1, label
            run(source.replace(before, after), negative=True)
            print("Rejected negative control:", label)


if __name__ == "__main__":
    main()
