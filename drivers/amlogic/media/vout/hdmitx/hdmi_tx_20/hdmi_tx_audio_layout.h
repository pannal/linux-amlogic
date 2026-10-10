/* SPDX-License-Identifier: GPL-2.0 */
#ifndef HDMITX_AUDIO_LAYOUT_H
#define HDMITX_AUDIO_LAYOUT_H

#include <linux/amlogic/media/vout/hdmi_tx/hdmi_info_global.h>

/* The source/DMA still carries eight slots. CC describes active speakers. */
static inline unsigned int hdmitx_pcm_rear_center_channels(
	const struct hdmitx_audpara *audio_param)
{
	if (audio_param->type != CT_PCM || !audio_param->layout_valid ||
	    audio_param->channel_num != CC_8CH)
		return 0;
	if (audio_param->layout == 0x0e)
		return 6;
	if (audio_param->layout == 0x0f)
		return 7;
	return 0;
}

static inline unsigned int hdmitx_pcm_rear_center_invalid(
	const struct hdmitx_audpara *audio_param)
{
	unsigned int channels = hdmitx_pcm_rear_center_channels(audio_param);

	/* V3r..V0r,V3l..V0l: padded slot7, and slot2 for 6.0, are invalid. */
	return channels == 6 ? 0x82 : (channels == 7 ? 0x80 : 0);
}

#endif
