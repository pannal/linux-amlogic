/* SPDX-License-Identifier: GPL-2.0 */
#ifndef OSD_GUI_WAIT_H
#define OSD_GUI_WAIT_H

#include <linux/anon_inodes.h>
#include <linux/pid.h>
#include <linux/sched.h>

/* Fixed-width, compat-safe ABI. CREATE copies out a CLOEXEC lease fd; it does
 * not return the fd as ioctl's result (old OSD drivers accept unknown commands).
 * SET acts on that lease, not on a framebuffer descriptor.
 */
#define FBIOGET_GUI_WAIT_LEASE _IOR('F', 0x22, __s32)
#define FBIOSET_GUI_WAIT_LEASE _IOW('F', 0x23, __u32)

struct osd_gui_wait_lease {
	struct list_head link;
	struct pid *tgid;
	u32 node;
	bool enabled;
};
static LIST_HEAD(osd_gui_wait_leases);
static DEFINE_MUTEX(osd_gui_wait_lock);

static bool osd_gui_wait_enabled(u32 node)
{
	struct osd_gui_wait_lease *lease;
	bool enabled = false;

	mutex_lock(&osd_gui_wait_lock);
	list_for_each_entry(lease, &osd_gui_wait_leases, link) {
		if (lease->enabled && lease->node == node &&
		    lease->tgid == task_tgid(current)) {
			enabled = true;
			break;
		}
	}
	mutex_unlock(&osd_gui_wait_lock);
	return enabled;
}

static long osd_gui_wait_set(struct file *file, unsigned int cmd,
			     unsigned long arg)
{
	struct osd_gui_wait_lease *lease = file->private_data;
	u32 enabled;

	if (cmd != FBIOSET_GUI_WAIT_LEASE)
		return -ENOTTY;
	if (lease->tgid != task_tgid(current))
		return -EPERM;
	if (copy_from_user(&enabled, (void __user *)arg, sizeof(enabled)))
		return -EFAULT;
	if (enabled > 1)
		return -EINVAL;
	mutex_lock(&osd_gui_wait_lock);
	lease->enabled = enabled;
	mutex_unlock(&osd_gui_wait_lock);
	return 0;
}

#ifdef CONFIG_COMPAT
static long osd_gui_wait_set_compat(struct file *file, unsigned int cmd,
				    unsigned long arg)
{
	return osd_gui_wait_set(file, cmd, (unsigned long)compat_ptr(arg));
}
#endif

static int osd_gui_wait_release(struct inode *inode, struct file *file)
{
	struct osd_gui_wait_lease *lease = file->private_data;

	mutex_lock(&osd_gui_wait_lock);
	list_del(&lease->link);
	mutex_unlock(&osd_gui_wait_lock);
	put_pid(lease->tgid);
	kfree(lease);
	return 0;
}

static const struct file_operations osd_gui_wait_fops = {
	.owner = THIS_MODULE,
	.unlocked_ioctl = osd_gui_wait_set,
#ifdef CONFIG_COMPAT
	.compat_ioctl = osd_gui_wait_set_compat,
#endif
	.release = osd_gui_wait_release,
};

static int osd_gui_wait_create(u32 node, void __user *argp)
{
	struct osd_gui_wait_lease *lease;
	struct file *file;
	int fd, ret;

	fd = get_unused_fd_flags(O_CLOEXEC);
	if (fd < 0)
		return fd;
	lease = kzalloc(sizeof(*lease), GFP_KERNEL);
	if (!lease) {
		put_unused_fd(fd);
		return -ENOMEM;
	}
	lease->tgid = get_pid(task_tgid(current));
	lease->node = node;
	file = anon_inode_getfile("osd-gui-wait", &osd_gui_wait_fops,
				lease, O_RDWR);
	if (IS_ERR(file)) {
		ret = PTR_ERR(file);
		put_pid(lease->tgid);
		kfree(lease);
		put_unused_fd(fd);
		return ret;
	}
	mutex_lock(&osd_gui_wait_lock);
	list_add(&lease->link, &osd_gui_wait_leases);
	mutex_unlock(&osd_gui_wait_lock);
	if (copy_to_user(argp, &fd, sizeof(fd))) {
		fput(file); /* Removes the disabled lease as well. */
		put_unused_fd(fd);
		return -EFAULT;
	}
	fd_install(fd, file);
	return 0;
}
#endif
