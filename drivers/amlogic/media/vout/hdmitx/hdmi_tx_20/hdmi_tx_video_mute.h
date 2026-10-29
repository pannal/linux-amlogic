/* SPDX-License-Identifier: GPL-2.0 */
#ifndef HDMITX_VIDEO_MUTE_H
#define HDMITX_VIDEO_MUTE_H

#include <linux/spinlock.h>

/* The sysfs reference count and the internal HDR request are independent
 * owners. Serializing both also avoids lost updates from copying atomic_t. */
static DEFINE_SPINLOCK(video_mute_request_lock);
static bool video_mute_internal;

static void hdmitx_update_video_mute(struct hdmitx_dev *hdev, bool userspace,
				    bool mute, bool deferred)
{
	unsigned long flags;
	unsigned char op;

	spin_lock_irqsave(&video_mute_request_lock, flags);
	if (userspace) {
		if (mute)
			atomic_inc(&hdev->kref_video_mute);
		else if (atomic_read(&hdev->kref_video_mute) > 0)
			atomic_dec(&hdev->kref_video_mute);
	} else {
		video_mute_internal = mute;
	}
	op = video_mute_internal || atomic_read(&hdev->kref_video_mute) > 0 ?
		VIDEO_MUTE : VIDEO_UNMUTE;
	if (deferred) {
		if (hdev->hdmi_init == 1)
			WRITE_ONCE(hdev->vid_mute_op, op);
	} else {
		hdev->hwop.cntlconfig(hdev, CONF_VIDEO_MUTE_OP, op);
	}
	spin_unlock_irqrestore(&video_mute_request_lock, flags);
}
#endif
