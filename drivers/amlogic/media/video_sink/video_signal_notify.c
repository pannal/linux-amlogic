/*
 * drivers/amlogic/media/video_sink/video_signal_notify.c
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
#include <linux/amlogic/media/video_sink/video_signal_notify.h>

/* Atomic: the chain is called from the video and HDR paths, and a client
 * module that unregisters must not be called afterwards (unregister waits
 * for running calls).
 */
static ATOMIC_NOTIFIER_HEAD(vd_signal_notifier_list);

int vd_signal_register_client(struct notifier_block *nb)
{
	return atomic_notifier_chain_register(&vd_signal_notifier_list, nb);
}
EXPORT_SYMBOL(vd_signal_register_client);

int vd_signal_unregister_client(struct notifier_block *nb)
{
	return atomic_notifier_chain_unregister(&vd_signal_notifier_list, nb);
}
EXPORT_SYMBOL(vd_signal_unregister_client);

int vd_signal_notifier_call_chain(unsigned long val, void *v)
{
	return atomic_notifier_call_chain(&vd_signal_notifier_list, val, v);
}
EXPORT_SYMBOL_GPL(vd_signal_notifier_call_chain);
