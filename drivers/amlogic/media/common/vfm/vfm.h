/*
 * drivers/amlogic/media/common/vfm/vfm.h
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

#ifndef __AML_VFM_H
#define __AML_VFM_H

extern int vfm_map_add(char *id, char *name_chain);

extern int vfm_map_remove(char *id);

char *vf_get_provider_name(const char *receiver_name);

bool vf_get_provider_name_copy(const char *receiver_name, char *name,
			       size_t size);

#define VFM_NAME_LEN    100

bool vf_get_receiver_name_copy(const char *provider_name, char *name,
			       size_t size);

bool vf_get_receiver_name_routed(const char *provider_name, char *name,
				 size_t size, bool *routed);

struct vframe_receiver_s *vf_get_receiver_routed(const char *provider_name,
						 bool *routed);

void vf_update_active_map(void);

int provider_list(char *buf, int size);

int receiver_list(char *buf, int size);

struct vframe_provider_s *vf_get_provider_by_name(const char *provider_name);

extern int vfm_mode;

extern int vfm_debug_flag;
extern int vfm_trace_enable;	/* 1; */
extern int vfm_trace_num;	/*  */

/*
 * Whether a provider whose receiver cannot be found is worth an error: a
 * map routes it (routed, from vf_get_receiver_routed()), but to no
 * registered receiver. A provider no map routes, such as a vdin1 session
 * whose frames userspace takes through /dev/vdin1, has nobody to tell;
 * vfm_debug_flag bit 0 logs it as well.
 */
static inline bool vf_receiver_expected(bool routed)
{
	return routed || (vfm_debug_flag & 1);
}

struct vfmctl {
	char name[10];
	char val[300];
};

#define VFM_IOC_MAGIC  'V'
#define VFM_IOCTL_CMD_ADD   _IOW(VFM_IOC_MAGIC, 0x00, struct vfmctl)
#define VFM_IOCTL_CMD_RM    _IOW(VFM_IOC_MAGIC, 0x01, struct vfmctl)
#define VFM_IOCTL_CMD_DUMP   _IOW(VFM_IOC_MAGIC, 0x02, struct vfmctl)
#define VFM_IOCTL_CMD_ADDDUMMY    _IOW(VFM_IOC_MAGIC, 0x03, struct vfmctl)
#define VFM_IOCTL_CMD_SET   _IOW(VFM_IOC_MAGIC, 0x04, struct vfmctl)
#define VFM_IOCTL_CMD_GET   _IOWR(VFM_IOC_MAGIC, 0x05, struct vfmctl)

#endif
