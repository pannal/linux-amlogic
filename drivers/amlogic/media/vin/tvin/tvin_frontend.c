/*
 * drivers/amlogic/media/vin/tvin/tvin_frontend.c
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

/* Standard Linux headers */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/list.h>
#include <linux/spinlock.h>
#include <linux/device.h>
/* Amlogic headers */
#include <linux/amlogic/media/vfm/vframe.h>

/* Local headers */
#include "tvin_frontend.h"

#define CLASS_NAME      "tvin"

static struct class *tvcom_clsp;
static struct list_head head = LIST_HEAD_INIT(head);
static DEFINE_SPINLOCK(list_lock);

int tvin_frontend_init(struct tvin_frontend_s *fe,
		struct tvin_decoder_ops_s *dec_ops,
		struct tvin_state_machine_ops_s *sm_ops,
		int index)
{
	if (!fe)
		return -1;
	fe->dec_ops = dec_ops;
	fe->sm_ops  = sm_ops;
	fe->index   = index;
	fe->owner   = NULL;

	INIT_LIST_HEAD(&fe->list);
	return 0;
}
EXPORT_SYMBOL(tvin_frontend_init);

int tvin_reg_frontend(struct tvin_frontend_s *fe)
{
	ulong flags;
	struct tvin_frontend_s *f, *t;
	if (!strlen(fe->name) || !fe->dec_ops ||
		!fe->dec_ops->support || !fe->sm_ops)
		return -1;

	/* check whether the frontend is registered already */
	spin_lock_irqsave(&list_lock, flags);
	list_for_each_entry_safe(f, t, &head, list) {
		if (!strcmp(f->name, fe->name)) {
			spin_unlock_irqrestore(&list_lock, flags);
			return -1;
		}
	}
	list_add_tail(&fe->list, &head);
	spin_unlock_irqrestore(&list_lock, flags);
	return 0;
}
EXPORT_SYMBOL(tvin_reg_frontend);

/*
 * Notes: This unregister interface doesn't call kfree to free memory
 * for the object, because the register interface doesn't call kmalloc
 * to allocate memory for the object. You should call the kfree yourself
 * to free memory for the object you allocated.
 */
void tvin_unreg_frontend(struct tvin_frontend_s *fe)
{
	ulong flags;
	struct tvin_frontend_s *f, *t;

	spin_lock_irqsave(&list_lock, flags);
	list_for_each_entry_safe(f, t, &head, list) {
		if (f == fe) {
			list_del(&f->list);
			break;
		}
	}
	spin_unlock_irqrestore(&list_lock, flags);
}
EXPORT_SYMBOL(tvin_unreg_frontend);

/*
 * The lookup and the module reference are taken under the list lock, which
 * tvin_unreg_frontend() also takes: a frontend found here is not yet
 * unregistered, and its module, once held, cannot start unloading. A module
 * already going fails try_module_get(), so its frontend is not handed out.
 * The holder calls tvin_put_frontend() after its last call into the
 * frontend has returned.
 */
struct tvin_frontend_s *tvin_hold_frontend(enum tvin_port_e port, int index)
{
	struct tvin_frontend_s *f, *found = NULL;
	ulong flags;

	spin_lock_irqsave(&list_lock, flags);
	list_for_each_entry(f, &head, list) {
		if (f->dec_ops && f->dec_ops->support &&
		    !f->dec_ops->support(f, port) && f->index == index) {
			if (try_module_get(f->owner))
				found = f;
			break;
		}
	}
	spin_unlock_irqrestore(&list_lock, flags);
	return found;
}
EXPORT_SYMBOL(tvin_hold_frontend);

void tvin_put_frontend(struct tvin_frontend_s *fe)
{
	if (fe)
		module_put(fe->owner);
}
EXPORT_SYMBOL(tvin_put_frontend);

static ssize_t frontend_name_show(struct class *cls,
		struct class_attribute *attr, char *buf)
{
	size_t len = 0;
	struct tvin_frontend_s *f = NULL;
	ulong flags;

	/* under the lock unregistering takes: an entry is freed after it */
	spin_lock_irqsave(&list_lock, flags);
	list_for_each_entry(f, &head, list) {
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "%s idx:%d port:%d\n", f->name,
				 f->index, f->port);
	}
	spin_unlock_irqrestore(&list_lock, flags);
	return len;
}
static CLASS_ATTR(frontend_names, 0444, frontend_name_show, NULL);

static int __init tvin_common_init(void)
{
	int ret = 0;
	tvcom_clsp = class_create(THIS_MODULE, CLASS_NAME);
	if (!tvcom_clsp) {
		pr_err("[tvin_com..]%s: create tvin common class error.\n",
				__func__);
		return -1;
	}
	ret = class_create_file(tvcom_clsp, &class_attr_frontend_names);
	return ret;
}
static void __exit tvin_common_exit(void)
{
	class_remove_file(tvcom_clsp, &class_attr_frontend_names);
	class_destroy(tvcom_clsp);
}
module_init(tvin_common_init);
module_exit(tvin_common_exit);
MODULE_LICENSE("GPL");

