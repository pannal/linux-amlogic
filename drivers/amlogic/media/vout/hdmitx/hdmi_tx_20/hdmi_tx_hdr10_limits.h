/* SPDX-License-Identifier: GPL-2.0 */
#ifndef HDMI_TX_HDR10_LIMITS_H
#define HDMI_TX_HDR10_LIMITS_H

/* Build a transmission copy; never replace source or recovery metadata. */
void hdmitx_hdr10_limit_packet(unsigned char *dst, const unsigned char *db,
                             const unsigned char *hb);

#endif
