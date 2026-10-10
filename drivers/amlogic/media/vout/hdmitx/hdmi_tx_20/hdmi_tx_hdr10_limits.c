// SPDX-License-Identifier: GPL-2.0
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/compiler.h>
#include <linux/string.h>

#include "hdmi_tx_hdr10_limits.h"

/* Whole nits; zero disables each cap. Belongs to the hdmitx20 composite. */
static unsigned int xbmc_hdr10_max_lum_override;
static unsigned int xbmc_hdr10_max_cll_override;

static int hdr10_limit_set(const char *val, const struct kernel_param *kp)
{
	unsigned int limit;
	int ret = kstrtouint(val, 0, &limit);

	if (ret)
		return ret;
	if (limit > 10000)
		return -EINVAL;
	/* Apply at the next packet send, without replaying old HDR signaling. */
	WRITE_ONCE(*(unsigned int *)kp->arg, limit);
	return 0;
}

static const struct kernel_param_ops hdr10_limit_ops = {
	.set = hdr10_limit_set,
	.get = param_get_uint,
};

module_param_cb(xbmc_hdr10_max_lum_override, &hdr10_limit_ops,
		&xbmc_hdr10_max_lum_override, 0664);
MODULE_PARM_DESC(xbmc_hdr10_max_lum_override,
		"Outgoing HDR10 mastering maximum cap in nits (0 disables)");
module_param_cb(xbmc_hdr10_max_cll_override, &hdr10_limit_ops,
		&xbmc_hdr10_max_cll_override, 0664);
MODULE_PARM_DESC(xbmc_hdr10_max_cll_override,
		"Outgoing HDR10 MaxCLL cap in nits (0 disables)");

static void hdr10_cap_nits(unsigned char *field, unsigned int limit)
{
	unsigned int value = field[0] | (field[1] << 8);

	/* Unknown zero remains zero; never synthesize a missing value. */
	if (limit && value > limit) {
		field[0] = limit & 0xff;
		field[1] = (limit >> 8) & 0xff;
	}
}

void hdmitx_hdr10_limit_packet(unsigned char *dst, const unsigned char *db,
			     const unsigned char *hb)
{
	memcpy(dst, db, 26);
	/* Static metadata type 1, version 1, PQ. HLG/SDR/unknown bypass. */
	if (hb[0] != 0x87 || hb[1] != 1 || hb[2] != 26 ||
	    db[0] != 2 || db[1] != 0)
		return;

	/* MIN is 0.0001 nit; MaxFALL/primaries/white point are untouched. */
	hdr10_cap_nits(dst + 18, READ_ONCE(xbmc_hdr10_max_lum_override));
	hdr10_cap_nits(dst + 22, READ_ONCE(xbmc_hdr10_max_cll_override));
}
