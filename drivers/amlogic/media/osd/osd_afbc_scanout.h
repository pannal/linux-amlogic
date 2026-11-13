/* SPDX-License-Identifier: GPL-2.0 */
#ifndef OSD_AFBC_SCANOUT_H
#define OSD_AFBC_SCANOUT_H

#include <linux/anon_inodes.h>
#include <linux/dma-buf.h>
#include <linux/file.h>
#include <linux/list.h>
#include <linux/pid.h>
#include <linux/sched.h>
#include <linux/workqueue.h>

/* AFBC scanout session for a GPU-rendered GUI, without the Android sync-fence
 * paths. Fixed-width, compat-safe ABI.
 *
 * FBIOGET_OSD_AFBC_SCANOUT on a framebuffer copies out a CLOEXEC session fd.
 * The other commands act on that session:
 *
 *   ALLOC   allocates one physically contiguous display buffer sized for a
 *           complete width x height RGBA8 AFBC frame (16x16 or 32x8
 *           superblocks, sparse body) and returns a CLOEXEC dma-buf fd for
 *           the GPU import. The buffer the plane shows, or is queued to
 *           show, cannot be replaced (EBUSY).
 *   FLIP    shows a buffer the caller has finished rendering, top row first
 *           (REVERSE_FALSE) or bottom row first, as GL renders it
 *           (REVERSE_Y). The decoder takes it with the next vsync whose
 *           register update carries it; with OSD_AFBC_SCANOUT_WAIT the call
 *           returns once that vsync has passed, when the previous buffer is
 *           no longer read, or fails with ETIMEDOUT if that took over a
 *           second (the flip stays queued). While the display is not scanning
 *           out (no vsync for OSD_AFBC_SCANOUT_STALL_MS: off, or changing
 *           mode) it fails with EAGAIN and queues nothing. The decoder holds
 *           one row of superblocks: a frame wider than 1920 pixels needs 32x8
 *           ones (SUPER_BLOCK_ASPECT), as a row of 16x16 ones that wide does
 *           not fit (EINVAL).
 *   DISABLE returns to the linear framebuffer at the last panned offset and
 *           returns when the plane has left the session's buffers (ETIMEDOUT
 *           after a second), or with OSD_AFBC_SCANOUT_ON_PAN in the vsync of
 *           the next pan: the caller has swapped a linear frame that is still
 *           in flight.
 *
 * The session owns the buffers. The display never reads memory that has been
 * freed: a buffer the plane may still read, because its replacement or the
 * return to the linear framebuffer is not on screen yet, is kept until it
 * has been for a vsync. That holds for ALLOC, for a mode set and for closing
 * the session, also by process exit, which first returns the plane to the
 * linear framebuffer. A mode set leaves the session stale (FLIP fails with
 * ESTALE) until a buffer is allocated again. Either way the linear
 * framebuffer holds only what was drawn before the session, so the plane
 * stays transparent until its next frame.
 */
#define OSD_AFBC_SCANOUT_BUFFERS 3
#define OSD_AFBC_SCANOUT_WAIT 1
#define OSD_AFBC_SCANOUT_ON_PAN 1
#define OSD_AFBC_SCANOUT_TIMEOUT_MS 1000
/* Longer than a frame at 24 Hz with any interrupt latency. */
#define OSD_AFBC_SCANOUT_STALL_MS 200

struct osd_afbc_scanout_buffer {
	__u32 index;
	__u32 width;
	__u32 height;
	__s32 fd;
	__u32 size;
};

struct osd_afbc_scanout_flip {
	__u32 index;
	__u32 inter_format;	/* YUV_TRANSFORM, BLOCK_SPLIT, ... of osd_sync.h */
	__u32 reverse;		/* REVERSE_FALSE or REVERSE_Y for a GL-origin frame */
	__u32 flags;
};

#define FBIOGET_OSD_AFBC_SCANOUT _IOR('F', 0x24, __s32)
#define OSD_AFBC_SCANOUT_ALLOC \
	_IOWR('F', 0x25, struct osd_afbc_scanout_buffer)
#define OSD_AFBC_SCANOUT_FLIP _IOW('F', 0x26, struct osd_afbc_scanout_flip)
#define OSD_AFBC_SCANOUT_DISABLE _IOW('F', 0x27, __u32)

struct osd_afbc_scanout_session {
	struct pid *tgid;
	u32 node;
	bool stale;
	struct ion_handle *handle[OSD_AFBC_SCANOUT_BUFFERS];
	u32 paddr[OSD_AFBC_SCANOUT_BUFFERS];
	u32 width[OSD_AFBC_SCANOUT_BUFFERS];
	u32 height[OSD_AFBC_SCANOUT_BUFFERS];
	/* Holds the buffer if it is freed before it is retired: allocated with
	 * it, so the plane can never be left reading freed memory.
	 */
	struct osd_afbc_scanout_grave *grave[OSD_AFBC_SCANOUT_BUFFERS];
	/* Left by the plane at this change, not yet known to be retired. */
	bool retiring[OSD_AFBC_SCANOUT_BUFFERS];
	u32 retire_seq[OSD_AFBC_SCANOUT_BUFFERS];
	int shown;	/* on screen, or queued for the next vsync */
};

/* Buffers whose session let go of them before the plane did. */
struct osd_afbc_scanout_grave {
	struct list_head node;
	struct ion_handle *handle;
	u32 osd;
	u32 seq;
};

static struct osd_afbc_scanout_session *osd_afbc_scanout_sessions[OSD_COUNT];
static DEFINE_MUTEX(osd_afbc_scanout_lock);
static LIST_HEAD(osd_afbc_scanout_graves);

/* Retired once the change that left the buffer is on screen; the second
 * vsync is a margin, as freeing is never urgent.
 */
#define OSD_AFBC_SCANOUT_RETIRE_VSYNCS 2

static void osd_afbc_scanout_reap_work(struct work_struct *work);
static DECLARE_DELAYED_WORK(osd_afbc_scanout_reaper,
			    osd_afbc_scanout_reap_work);

static u32 osd_afbc_scanout_size(u32 width, u32 height)
{
	/* 16x16 or 32x8 superblocks, whichever needs more; 1 KiB bodies. */
	u32 blocks = max(DIV_ROUND_UP(width, 16) * DIV_ROUND_UP(height, 16),
			 DIV_ROUND_UP(width, 32) * DIV_ROUND_UP(height, 8));

	return PAGE_ALIGN(ALIGN(blocks * 16, 4096) + blocks * 16 * 16 * 4);
}

/* Caller holds osd_afbc_scanout_lock. Returns true if graves remain. */
static bool osd_afbc_scanout_reap(void)
{
	struct osd_afbc_scanout_grave *grave, *next;

	list_for_each_entry_safe(grave, next, &osd_afbc_scanout_graves, node) {
		if (!osd_afbc_scanout_settled(grave->osd, grave->seq,
					      OSD_AFBC_SCANOUT_RETIRE_VSYNCS))
			continue;
		ion_free(fb_ion_client, grave->handle);
		list_del(&grave->node);
		kfree(grave);
	}
	return !list_empty(&osd_afbc_scanout_graves);
}

static void osd_afbc_scanout_reap_work(struct work_struct *work)
{
	bool pending;

	mutex_lock(&osd_afbc_scanout_lock);
	pending = osd_afbc_scanout_reap();
	mutex_unlock(&osd_afbc_scanout_lock);
	if (pending)
		schedule_delayed_work(&osd_afbc_scanout_reaper,
				      msecs_to_jiffies(50));
}

/* Caller holds osd_afbc_scanout_lock. The plane left @index at its last
 * change: a flip to another buffer, or the return to linear.
 */
static void osd_afbc_scanout_retire(struct osd_afbc_scanout_session *session,
				    u32 index)
{
	session->retiring[index] = true;
	session->retire_seq[index] = osd_afbc_scanout_change_seq(session->node);
}

/* Caller holds osd_afbc_scanout_lock. A linear pan (ON_PAN) can end the
 * session's scanout without the session: catch up with it.
 */
static void osd_afbc_scanout_sync(struct osd_afbc_scanout_session *session)
{
	if (session->shown >= 0 && !osd_afbc_scanout_active(session->node)) {
		osd_afbc_scanout_retire(session, session->shown);
		session->shown = -1;
	}
}

/* Caller holds osd_afbc_scanout_lock and has made sure the buffer is not
 * shown. A buffer the plane may still be reading is kept until it is not.
 */
static void osd_afbc_scanout_free(struct osd_afbc_scanout_session *session,
				  u32 index)
{
	struct osd_afbc_scanout_grave *grave = session->grave[index];
	struct ion_handle *handle = session->handle[index];
	bool busy = session->retiring[index] &&
		!osd_afbc_scanout_settled(session->node,
					  session->retire_seq[index],
					  OSD_AFBC_SCANOUT_RETIRE_VSYNCS);

	session->retiring[index] = false;
	session->grave[index] = NULL;
	if (!handle)
		return;
	session->handle[index] = NULL;
	session->paddr[index] = 0;
	if (busy) {
		grave->handle = handle;
		grave->osd = session->node;
		grave->seq = session->retire_seq[index];
		list_add_tail(&grave->node, &osd_afbc_scanout_graves);
		schedule_delayed_work(&osd_afbc_scanout_reaper,
				      msecs_to_jiffies(50));
		return;
	}
	kfree(grave);
	ion_free(fb_ion_client, handle);
}

static struct ion_handle *osd_afbc_scanout_ion_alloc(u32 size)
{
	struct ion_handle *handle;

	/* Heaps whose buffer is one contiguous range, which ion_phys() can
	 * describe: the ION CMA pool, which a 4K linear framebuffer nearly
	 * fills, then the codec pool.
	 */
	handle = ion_alloc(fb_ion_client, size, 0, (1 << ION_HEAP_TYPE_DMA), 0);
	if (IS_ERR_OR_NULL(handle))
		handle = ion_alloc(fb_ion_client, size, 0,
				   (1 << ION_HEAP_TYPE_CUSTOM), 0);
	return IS_ERR_OR_NULL(handle) ? NULL : handle;
}

static long osd_afbc_scanout_alloc(struct osd_afbc_scanout_session *session,
				   void __user *argp)
{
	struct osd_afbc_scanout_buffer buffer;
	struct osd_afbc_scanout_grave *grave, *slot;
	struct ion_handle *handle;
	struct dma_buf *dmabuf;
	ion_phys_addr_t addr = 0;
	size_t len = 0;
	u32 size;
	int fd;

	if (copy_from_user(&buffer, argp, sizeof(buffer)))
		return -EFAULT;
	if (buffer.index >= OSD_AFBC_SCANOUT_BUFFERS ||
	    buffer.width < 16 || buffer.width > 4096 ||
	    buffer.height < 16 || buffer.height > 2304)
		return -EINVAL;
	osd_afbc_scanout_sync(session);
	if (session->shown == (int)buffer.index)
		return -EBUSY;
	if (!fb_ion_client)
		return -ENODEV;
	size = osd_afbc_scanout_size(buffer.width, buffer.height);
	osd_afbc_scanout_free(session, buffer.index);
	osd_afbc_scanout_reap();
	slot = kzalloc(sizeof(*slot), GFP_KERNEL);
	if (!slot)
		return -ENOMEM;
	handle = osd_afbc_scanout_ion_alloc(size);
	if (!handle && !list_empty(&osd_afbc_scanout_graves)) {
		/* Buffers of a mode just left are retired within two vsyncs. */
		grave = list_last_entry(&osd_afbc_scanout_graves,
					struct osd_afbc_scanout_grave, node);
		osd_afbc_scanout_wait(grave->osd, grave->seq,
				      OSD_AFBC_SCANOUT_RETIRE_VSYNCS,
				      OSD_AFBC_SCANOUT_TIMEOUT_MS);
		osd_afbc_scanout_reap();
		handle = osd_afbc_scanout_ion_alloc(size);
	}
	if (!handle) {
		fd = -ENOMEM;
		goto fail;
	}
	if (ion_phys(fb_ion_client, handle, &addr, &len) || len < size ||
	    (u64)addr + len > U32_MAX) {
		fd = -ENOMEM;
		goto fail_free;
	}
	/* The fd is installed only once its number is copied out: an fd the
	 * caller never learnt of would keep the buffer allocated.
	 */
	dmabuf = ion_share_dma_buf(fb_ion_client, handle);
	if (IS_ERR(dmabuf)) {
		fd = PTR_ERR(dmabuf);
		goto fail_free;
	}
	fd = get_unused_fd_flags(O_CLOEXEC);
	if (fd < 0)
		goto fail_put;
	buffer.fd = fd;
	buffer.size = size;
	if (copy_to_user(argp, &buffer, sizeof(buffer))) {
		put_unused_fd(fd);
		fd = -EFAULT;
		goto fail_put;
	}
	fd_install(fd, dmabuf->file);
	session->grave[buffer.index] = slot;
	session->handle[buffer.index] = handle;
	session->paddr[buffer.index] = (u32)addr;
	session->width[buffer.index] = buffer.width;
	session->height[buffer.index] = buffer.height;
	/* A stale session's other buffers belong to the mode it left. */
	if (session->stale) {
		u32 i;

		for (i = 0; i < OSD_AFBC_SCANOUT_BUFFERS; i++)
			if (i != buffer.index)
				osd_afbc_scanout_free(session, i);
		session->stale = false;
	}
	return 0;

fail_put:
	dma_buf_put(dmabuf);
fail_free:
	ion_free(fb_ion_client, handle);
fail:
	kfree(slot);
	return fd;
}

static long osd_afbc_scanout_ioctl(struct file *file, unsigned int cmd,
				   unsigned long arg)
{
	struct osd_afbc_scanout_session *session = file->private_data;
	void __user *argp = (void __user *)arg;
	struct osd_afbc_scanout_flip flip;
	u32 paddr, width, height, flags, seq = 0;
	long ret = 0;

	if (session->tgid != task_tgid(current))
		return -EPERM;
	switch (cmd) {
	case OSD_AFBC_SCANOUT_ALLOC:
		mutex_lock(&osd_afbc_scanout_lock);
		ret = osd_afbc_scanout_alloc(session, argp);
		mutex_unlock(&osd_afbc_scanout_lock);
		return ret;
	case OSD_AFBC_SCANOUT_FLIP:
		if (copy_from_user(&flip, argp, sizeof(flip)))
			return -EFAULT;
		if (flip.index >= OSD_AFBC_SCANOUT_BUFFERS ||
		    (flip.inter_format & ~(YUV_TRANSFORM | BLOCK_SPLIT |
					   SUPER_BLOCK_ASPECT |
					   TILED_HEADER_EN)) ||
		    (flip.reverse != REVERSE_FALSE &&
		     flip.reverse != REVERSE_Y) ||
		    (flip.flags & ~OSD_AFBC_SCANOUT_WAIT))
			return -EINVAL;
		mutex_lock(&osd_afbc_scanout_lock);
		osd_afbc_scanout_sync(session);
		if (session->stale) {
			ret = -ESTALE;
		} else if (!session->handle[flip.index]) {
			ret = -ENOENT;
		} else if (session->width[flip.index] > 1920 &&
			   !(flip.inter_format & SUPER_BLOCK_ASPECT)) {
			ret = -EINVAL;
		} else if (!osd_afbc_scanout_scanning(session->node,
						      OSD_AFBC_SCANOUT_STALL_MS)) {
			/* Queued now, the flip and the freeing of whatever it
			 * leaves behind would wait for the display.
			 */
			ret = -EAGAIN;
		} else {
			paddr = session->paddr[flip.index];
			width = session->width[flip.index];
			height = session->height[flip.index];
			ret = osd_afbc_scanout_flip(session->node, paddr, width,
						    height, flip.inter_format,
						    flip.reverse);
			if (!ret) {
				if (session->shown >= 0 &&
				    session->shown != (int)flip.index)
					osd_afbc_scanout_retire(session,
								session->shown);
				session->retiring[flip.index] = false;
				session->shown = flip.index;
				seq = osd_afbc_scanout_change_seq(session->node);
			}
		}
		mutex_unlock(&osd_afbc_scanout_lock);
		if (!ret && (flip.flags & OSD_AFBC_SCANOUT_WAIT))
			ret = osd_afbc_scanout_wait(session->node, seq, 1,
						    OSD_AFBC_SCANOUT_TIMEOUT_MS);
		return ret;
	case OSD_AFBC_SCANOUT_DISABLE:
		if (copy_from_user(&flags, argp, sizeof(flags)))
			return -EFAULT;
		if (flags & ~OSD_AFBC_SCANOUT_ON_PAN)
			return -EINVAL;
		mutex_lock(&osd_afbc_scanout_lock);
		osd_afbc_scanout_sync(session);
		if (flags & OSD_AFBC_SCANOUT_ON_PAN) {
			osd_afbc_scanout_disable_on_pan(session->node);
		} else {
			osd_afbc_scanout_disable(session->node);
			if (session->shown >= 0)
				osd_afbc_scanout_retire(session,
							session->shown);
			session->shown = -1;
			seq = osd_afbc_scanout_change_seq(session->node);
		}
		mutex_unlock(&osd_afbc_scanout_lock);
		if (!(flags & OSD_AFBC_SCANOUT_ON_PAN))
			ret = osd_afbc_scanout_wait(session->node, seq, 1,
						    OSD_AFBC_SCANOUT_TIMEOUT_MS);
		return ret;
	}
	return -ENOTTY;
}

#ifdef CONFIG_COMPAT
static long osd_afbc_scanout_compat_ioctl(struct file *file, unsigned int cmd,
					  unsigned long arg)
{
	return osd_afbc_scanout_ioctl(file, cmd,
				      (unsigned long)compat_ptr(arg));
}
#endif

/* Does not wait: the buffers the plane may still read are kept until it has
 * left them, also when the process is being killed.
 */
static int osd_afbc_scanout_release(struct inode *inode, struct file *file)
{
	struct osd_afbc_scanout_session *session = file->private_data;
	u32 i;

	mutex_lock(&osd_afbc_scanout_lock);
	osd_afbc_scanout_sync(session);
	if (session->shown >= 0) {
		/* Hidden first: a vsync in between shows nothing stale. */
		osd_afbc_scanout_hide(session->node);
		osd_afbc_scanout_disable(session->node);
		osd_afbc_scanout_retire(session, session->shown);
		session->shown = -1;
	}
	for (i = 0; i < OSD_AFBC_SCANOUT_BUFFERS; i++)
		osd_afbc_scanout_free(session, i);
	osd_afbc_scanout_sessions[session->node] = NULL;
	mutex_unlock(&osd_afbc_scanout_lock);
	put_pid(session->tgid);
	kfree(session);
	return 0;
}

static const struct file_operations osd_afbc_scanout_fops = {
	.owner = THIS_MODULE,
	.unlocked_ioctl = osd_afbc_scanout_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl = osd_afbc_scanout_compat_ioctl,
#endif
	.release = osd_afbc_scanout_release,
};

/* A mode set reprograms the plane from the linear framebuffer. */
static void osd_afbc_scanout_invalidate(u32 node)
{
	struct osd_afbc_scanout_session *session;

	if (node >= OSD_COUNT)
		return;
	mutex_lock(&osd_afbc_scanout_lock);
	session = osd_afbc_scanout_sessions[node];
	if (session) {
		osd_afbc_scanout_sync(session);
		if (session->shown >= 0) {
			osd_afbc_scanout_hide(node);
			osd_afbc_scanout_disable(node);
			osd_afbc_scanout_retire(session, session->shown);
		}
		session->shown = -1;
		session->stale = true;
	} else {
		/* A client without a session draws its own frames. */
		osd_afbc_scanout_unhide(node);
	}
	mutex_unlock(&osd_afbc_scanout_lock);
}

static int osd_afbc_scanout_create(u32 node, void __user *argp)
{
	struct osd_afbc_scanout_session *session;
	struct file *file;
	int fd, ret;

	if (node != DEV_OSD0 || osd_meson_dev.afbc_type != MALI_AFBC)
		return -ENODEV;
	fd = get_unused_fd_flags(O_CLOEXEC);
	if (fd < 0)
		return fd;
	session = kzalloc(sizeof(*session), GFP_KERNEL);
	if (!session) {
		put_unused_fd(fd);
		return -ENOMEM;
	}
	session->tgid = get_pid(task_tgid(current));
	session->node = node;
	session->shown = -1;
	mutex_lock(&osd_afbc_scanout_lock);
	if (osd_afbc_scanout_sessions[node]) {
		mutex_unlock(&osd_afbc_scanout_lock);
		ret = -EBUSY;
		goto fail;
	}
	file = anon_inode_getfile("osd-afbc-scanout", &osd_afbc_scanout_fops,
				  session, O_RDWR);
	if (IS_ERR(file)) {
		mutex_unlock(&osd_afbc_scanout_lock);
		ret = PTR_ERR(file);
		goto fail;
	}
	osd_afbc_scanout_sessions[node] = session;
	mutex_unlock(&osd_afbc_scanout_lock);
	if (copy_to_user(argp, &fd, sizeof(fd))) {
		fput(file); /* Releases the session as well. */
		put_unused_fd(fd);
		return -EFAULT;
	}
	fd_install(fd, file);
	return 0;
fail:
	put_pid(session->tgid);
	kfree(session);
	put_unused_fd(fd);
	return ret;
}
#endif
