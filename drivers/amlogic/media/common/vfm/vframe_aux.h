/* SPDX-License-Identifier: GPL-2.0 */
#ifndef AML_VFRAME_AUX_H
#define AML_VFRAME_AUX_H

#include <asm/unaligned.h>

/* The provider prefixes each payload with a big-endian length and type. */
static inline bool vframe_aux_record(const char *record, u32 remaining,
				     u32 *size, u32 *type)
{
	if (remaining < 8)
		return false;
	*size = get_unaligned_be32(record);
	*type = get_unaligned_be32(record + 4);
	return *size && *size <= remaining - 8;
}

#endif
