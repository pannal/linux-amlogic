/*
 * drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hw/hdmi_tx_ddc.c
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

#include <linux/version.h>
#include <linux/module.h>
#include <linux/types.h>
#include <linux/kernel.h>
#include <linux/delay.h>
#include <linux/interrupt.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/device.h>
#include <linux/mm.h>
#include <linux/major.h>
#include <linux/platform_device.h>
#include <linux/mutex.h>
#include <linux/cdev.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/delay.h>
#include <linux/clk.h>
#include <linux/iopoll.h>

#include <linux/amlogic/media/vout/hdmi_tx/hdmi_tx_ddc.h>
#include <linux/amlogic/media/vout/hdmi_tx/hdmi_info_global.h>
#include <linux/amlogic/media/vout/hdmi_tx/hdmi_tx_module.h>
#include "common.h"
#include "hdmi_tx_reg.h"

static DEFINE_MUTEX(ddc_mutex);

/* Caller holds ddc_mutex from programming the operation through completion. */
static uint32_t ddc_wait_complete(uint8_t slave, uint8_t offset_addr)
{
	unsigned int status;
	int ret;

	/* Keep the existing settling delay, then allow a slow transfer to finish. */
	mdelay(2);
	ret = readx_poll_timeout(hdmitx_rd_reg, HDMITX_DWC_IH_I2CM_STAT0,
		status, status & 0x3, 1000, 18000);
	if (ret) {
		/* Abort before releasing the bus to the next DDC operation. */
		hdmitx_wr_reg(HDMITX_DWC_I2CM_SOFTRSTZ, 0);
		pr_info("E: ddc timeout 0x%02x 0x%02x status 0x%02x\n",
			slave, offset_addr, status);
	} else if (status & (1 << 0)) {
		pr_info("E: ddc transfer 0x%02x 0x%02x status 0x%02x\n",
			slave, offset_addr, status);
	}
	/* DONE (bit 1) is required; ERROR (bit 0) takes precedence. */
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	return !ret && !(status & (1 << 0));
}

static uint32_t ddc_write_1byte(uint8_t slave, uint8_t offset_addr,
	uint8_t data)
{
	uint32_t st = 0;

	mutex_lock(&ddc_mutex);
	/* Discard completion/error left by the previous transfer. */
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SLAVE, slave);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_ADDRESS, offset_addr);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_DATAO, data);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 4);
	st = ddc_wait_complete(slave, offset_addr);

	mutex_unlock(&ddc_mutex);
	return st;
}

#if 0
static uint32_t ddc_readext_8byte(uint8_t slave, uint8_t offset_addr,
	uint8_t *data)
{
	uint32_t st = 0;
	int32_t i;

	mutex_lock(&ddc_mutex);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SLAVE, slave);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_ADDRESS, offset_addr);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGADDR,  EDIDSEG_ADR);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGPTR, 0x00);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 3);
	mdelay(2);
	if (hdmitx_rd_reg(HDMITX_DWC_IH_I2CM_STAT0) & (1 << 0)) {
		st = 0;
		pr_info("E: ddc rdext8b 0x%02x 0x%02x\n",
			slave, offset_addr);
	} else
		st = 1;
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	for (i = 0; i < 8; i++)
		data[i] = hdmitx_rd_reg(HDMITX_DWC_I2CM_READ_BUFF0 + i);

	mutex_unlock(&ddc_mutex);
	return st;
}
#endif

static uint32_t ddc_read_8byte(uint8_t slave, uint8_t offset_addr,
	uint8_t *data)
{
	uint32_t st = 0;
	int32_t i;

	mutex_lock(&ddc_mutex);
	/* Discard completion/error left by the previous transfer. */
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SLAVE, slave);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_ADDRESS, offset_addr);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 2);
	st = ddc_wait_complete(slave, offset_addr);
	if (st)
		for (i = 0; i < 8; i++)
			data[i] = hdmitx_rd_reg(HDMITX_DWC_I2CM_READ_BUFF0 + i);

	mutex_unlock(&ddc_mutex);
	return st;
}

#if 0
static uint32_t ddc_readext_1byte(uint8_t slave, uint8_t address, uint8_t *data)
{
	uint32_t st = 0;
	int32_t i;

	mutex_lock(&ddc_mutex);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SLAVE, slave);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_ADDRESS, offset_addr);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGADDR,  EDIDSEG_ADR);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGPTR, 0x00);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 1);
	mdelay(2);
	if (hdmitx_rd_reg(HDMITX_DWC_IH_I2CM_STAT0) & (1 << 0)) {
		st = 0;
		pr_info("E: hdmitx: ddc rd8b 0x%02x 0x%02x\n",
			slave, offset_addr);
	} else
		st = 1;
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	*data = hdmitx_rd_reg(HDMITX_DWC_I2CM_DATAI);

	mutex_unlock(&ddc_mutex);
	return st;
}
#endif

static uint32_t ddc_read_1byte(uint8_t slave, uint8_t offset_addr,
	uint8_t *data)
{
	uint32_t st = 0;

	mutex_lock(&ddc_mutex);
	/* Discard completion/error left by the previous transfer. */
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SLAVE, slave);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_ADDRESS, offset_addr);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 0);
	st = ddc_wait_complete(slave, offset_addr);
	if (st)
		*data = hdmitx_rd_reg(HDMITX_DWC_I2CM_DATAI);

	mutex_unlock(&ddc_mutex);
	return st;
}

static uint32_t hdcp_rd_bksv(uint8_t *data)
{
	return ddc_read_8byte(HDCP_SLAVE, HDCP14_BKSV, data);
}

void scdc_rd_sink(uint8_t adr, uint8_t *val)
{
	hdmitx_ddc_hw_op(DDC_MUX_DDC);
	ddc_read_1byte(SCDC_SLAVE, adr, val);
}

void scdc_wr_sink(uint8_t adr, uint8_t val)
{
	hdmitx_ddc_hw_op(DDC_MUX_DDC);
	ddc_write_1byte(SCDC_SLAVE, adr, val);
}

uint32_t hdcp_rd_hdcp14_ver(void)
{
	int ret = 0;
	uint8_t bksv[8] = {0};

	hdmitx_ddc_hw_op(DDC_MUX_DDC);
	ret = hdcp_rd_bksv(&bksv[0]);
	if (ret)
		return 1;
	ret = hdcp_rd_bksv(&bksv[0]);
	if (ret)
		return 1;

	return 0;
}

uint32_t hdcp_rd_hdcp22_ver(void)
{
	uint32_t ret;
	uint8_t ver;

	hdmitx_ddc_hw_op(DDC_MUX_DDC);
	ret = ddc_read_1byte(HDCP_SLAVE, HDCP2_VERSION, &ver);
	if (ret)
		return ver == 0x04;
	ret = ddc_read_1byte(HDCP_SLAVE, HDCP2_VERSION, &ver);
	if (ret)
		return ver == 0x04;

	return 0;
}

/*
 * Start the I2C reactivation sequence, not a checked EDID read. The caller
 * deliberately slows SCL before this call and restores its normal timing
 * after we return. Waiting for completion here can hold the slow timing
 * until a timeout and abort the very transaction used to reactivate the bus.
 */
void edid_read_head_8bytes(void)
{
	hdmitx_ddc_hw_op(DDC_MUX_DDC);
	mutex_lock(&ddc_mutex);
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SLAVE, EDID_SLAVE);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_ADDRESS, 0);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 2);
	mdelay(2);
	if (hdmitx_rd_reg(HDMITX_DWC_IH_I2CM_STAT0) & (1 << 0))
		pr_info("E: ddc I2C reactive read\n");
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 0x7);
	mutex_unlock(&ddc_mutex);
}

#define EDID_WAIT_TIMEOUT	10
void hdmitx_read_edid(unsigned char *rx_edid)
{
	unsigned int timeout = 0;
	unsigned int i;
	unsigned int byte_num = 0;
	unsigned char blk_no = 1;

	if (!rx_edid)
		return;
	mutex_lock(&ddc_mutex);
	/* Program SLAVE/ADDR */
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SLAVE, 0x50);
	hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 1 << 1);
	/* Read complete EDID data sequentially */
	while (byte_num < 128 * blk_no) {
		hdmitx_wr_reg(HDMITX_DWC_I2CM_ADDRESS, byte_num & 0xff);
		if ((byte_num >= 256) && (byte_num < 512) && (blk_no > 2)) {
			/* Program SEGMENT/SEGPTR */
			hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGADDR, 0x30);
			hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGPTR, 0x1);
			hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 3);
		} else if ((byte_num >= 512) && (byte_num < 768) && (blk_no > 2)) {
			/* Program SEGMENT/SEGPTR */
			hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGADDR, 0x30);
			hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGPTR, 0x2);
			hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 3);
		} else if ((byte_num >= 768) && (byte_num < 1024) && (blk_no > 2)) {
			/* Program SEGMENT/SEGPTR */
			hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGADDR, 0x30);
			hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGPTR, 0x3);
			hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 3);
		} else {
			hdmitx_wr_reg(HDMITX_DWC_I2CM_OPERATION, 1 << 2);
		}
		/* Wait until I2C done */
		timeout = 0;
		while (!(hdmitx_rd_reg(HDMITX_DWC_IH_I2CM_STAT0) & (1 << 1)) &&
		       (timeout < EDID_WAIT_TIMEOUT)) {
			usleep_range(2000, 2010);
			timeout++;
		}
		if (timeout == EDID_WAIT_TIMEOUT)
			pr_info(HW "ddc timeout\n");
		hdmitx_wr_reg(HDMITX_DWC_IH_I2CM_STAT0, 1 << 1);
		/* Read back 8 bytes */
		for (i = 0; i < 8; i++) {
			rx_edid[byte_num] =
				hdmitx_rd_reg(HDMITX_DWC_I2CM_READ_BUFF0 + i);
			if (byte_num == 126)
				blk_no = rx_edid[126] + 1;
			byte_num++;
		}
		if (byte_num > 127 && byte_num < 256)
			if ((rx_edid[128 + 4] == 0xe2) && (rx_edid[128 + 5] == 0x78))
				blk_no = rx_edid[128 + 6] + 1;
		if (blk_no > 8) {
			pr_info(HW "edid extension block number:");
			pr_info(HW " %d, reset to MAX 7\n",
				blk_no - 1);
			blk_no = 8; /* Max extended block */
		}
	}
	/* Because DRM will use segment registers,
	 * so clear the registers to default
	 */
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGADDR, 0x0);
	hdmitx_wr_reg(HDMITX_DWC_I2CM_SEGPTR, 0x0);
	mutex_unlock(&ddc_mutex);
}
