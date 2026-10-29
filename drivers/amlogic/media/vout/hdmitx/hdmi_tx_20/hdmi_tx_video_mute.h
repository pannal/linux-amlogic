/* SPDX-License-Identifier: GPL-2.0 */
#ifndef HDMITX_VIDEO_MUTE_H
#define HDMITX_VIDEO_MUTE_H

#include <linux/spinlock.h>

/* Kodi has a dedicated idempotent slot, so a new Kodi process can clear its
 * predecessor's request without decrementing anonymous userspace references.
 * Exactly one Kodi instance owns that slot. HDR and legacy references remain
 * independent owners. Both main variants are mutually exclusive build inputs. */
enum hdmitx_video_mute_owner {
	HDMITX_MUTE_USERSPACE,
	HDMITX_MUTE_HDR,
	HDMITX_MUTE_KODI,
};
static DEFINE_SPINLOCK(video_mute_request_lock);
static bool video_mute_internal;
static bool video_mute_kodi;

static void hdmitx_update_video_mute(struct hdmitx_dev *hdev,
				    enum hdmitx_video_mute_owner owner,
				    bool mute, bool deferred)
{
	unsigned long flags;
	unsigned char op;

	spin_lock_irqsave(&video_mute_request_lock, flags);
	if (owner == HDMITX_MUTE_USERSPACE) {
		if (mute)
			atomic_inc(&hdev->kref_video_mute);
		else if (atomic_read(&hdev->kref_video_mute) > 0)
			atomic_dec(&hdev->kref_video_mute);
	} else if (owner == HDMITX_MUTE_HDR) {
		video_mute_internal = mute;
	} else if (owner == HDMITX_MUTE_KODI) {
		WRITE_ONCE(video_mute_kodi, mute);
	} else {
		spin_unlock_irqrestore(&video_mute_request_lock, flags);
		return;
	}
	op = video_mute_internal || video_mute_kodi ||
		atomic_read(&hdev->kref_video_mute) > 0 ?
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
