/*
 * include/linux/amlogic/media/rdma/rdma_mgr.h
 *
 * Copyright (C) 2017 Amlogic, Inc. All rights reserved.
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
 * more details.
 *
 */

#ifndef RDMA_MGR_H_
#define RDMA_MGR_H_

struct rdma_op_s {
	void (*irq_cb)(void *arg);
	void *arg;
};

#define RDMA_TRIGGER_VSYNC_INPUT 0x1
#define RDMA_TRIGGER_LINE_INPUT (1 << 5)
#define RDMA_TRIGGER_MANUAL	0x100
#define RDMA_TRIGGER_DEBUG1 0x101
#define RDMA_TRIGGER_DEBUG2 0x102
#define RDMA_AUTO_START_MASK 0x80000

enum rdma_ver_e {
	RDMA_VER_1,
	RDMA_VER_2,
};

enum cpu_ver_e {
	CPU_NORMAL,
	CPU_G12B,
	CPU_TL1,
	CPU_SC2,
};

struct rdma_device_data_s {
	enum cpu_ver_e cpu_type;
	enum rdma_ver_e rdma_ver;
	u32 trigger_mask_len;
};

u32 is_meson_g12b_revb(void);
/*
 *	rdma_read_reg(), rdma_write_reg(), rdma_clear() can only be called
 *	after rdma_register() is called and
 *	before rdma_unregister() is called
 */
int rdma_register(struct rdma_op_s *rdma_op, void *op_arg, int table_size);

/*
 *	rdma_unregister() waits for the RDMA interrupt and for the channel's
 *	last transfer, and sleeps: process context only, never in its irq_cb
 *	(keep_buf, 1 for every channel, keeps the register buffer for reuse)
 */
void rdma_unregister(int i);

/*
 *	rdma_quiesce() disarms the channel and keeps rdma_config() from arming
 *	it again until rdma_resume(); it returns once no irq_cb or transfer of
 *	the channel runs, and sleeps: process context only
 */
int rdma_quiesce(int handle);
void rdma_resume(int handle);

int rdma_config(int handle, int trigger_type);

u32 rdma_read_reg(int handle, u32 adr);

int rdma_write_reg(int handle, u32 adr, u32 val);

int rdma_write_reg_bits(int handle, u32 adr, u32 val, u32 start, u32 len);

int rdma_clear(int handle);
/* Optional exact-batch receipt; zero means no verified completion. */
void rdma_frame_tracking(int handle);
u64 rdma_frame_begin(int handle);
void rdma_frame_end(int handle, u64 serial, u64 cookie, bool keep_pending);
u64 rdma_frame_completed(int handle);
#endif
