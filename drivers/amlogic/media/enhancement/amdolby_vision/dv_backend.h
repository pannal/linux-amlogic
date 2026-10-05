/* The callback exclusion lock also protects registration and context ownership.
 * Each call takes an owner reference while the table is still protected; an
 * unloading module cannot receive a new call. No lock spans provider callbacks.
 */
static bool dv_funcs_valid(const struct dolby_vision_func_s *f)
{
	return f && f->version_info && f->control_path &&
		f->metadata_parser_init && f->metadata_parser_reset &&
		f->metadata_parser_process && f->metadata_parser_release;
}

static void dv_legacy_release(void)
{
	unsigned long flags;
	spin_lock_irqsave(&dovi_lock, flags);
	if (metadata_parser && p_funcs_stb && try_module_get(dv_legacy_owner)) {
		p_funcs_stb->metadata_parser_release();
		module_put(dv_legacy_owner);
	}
	metadata_parser = NULL;
	metadata_parser_reset_flag = true;
	spin_unlock_irqrestore(&dovi_lock, flags);
}

/* Both numerator and denominator must be known; never truncate 50.x to 50. */
static bool dv_new_route(enum signal_format_enum src,
			 enum signal_format_enum dst, const struct vinfo_s *vinfo)
{
	return READ_ONCE(dv_new_blob_enable) && !READ_ONCE(dv_new_runtime_failed) &&
		READ_ONCE(dv_new_signal_range) >= -1 && READ_ONCE(dv_new_signal_range) <= 1 &&
		!(READ_ONCE(dv_new_signal_range) == -1 &&
		  READ_ONCE(dolby_vision_signal_range) == SIGNAL_RANGE_SDI) &&
		READ_ONCE(dv_new_backend_available) &&
		READ_ONCE(xbmc_dv_source_native) && is_meson_g12b_cpu() &&
		(src == FORMAT_DOVI || src == FORMAT_DOVI_LL) &&
		dst == FORMAT_DOVI && READ_ONCE(xbmc_dv_vp) == 0 &&
		vinfo && vinfo->width && vinfo->height &&
		vinfo->width <= 16384 && vinfo->height <= 16384 &&
		vinfo->sync_duration_num && vinfo->sync_duration_den &&
		READ_ONCE(dv_new_blob_max_hz) && READ_ONCE(dv_new_blob_max_hz) <= 50 &&
		(u64)vinfo->sync_duration_num <=
		(u64)READ_ONCE(dv_new_blob_max_hz) * vinfo->sync_duration_den;
}

/* Called under dovi_lock with the newer owner's text retained (or by its exit). */
static void dv_new_retire_locked(void)
{
	if (p_funcs_new && dv_context_new) {
		p_funcs_new->control_path(FORMAT_INVALID, 0,
			dv_new_comp, 0, dv_new_md, 0,
			0, 0, 0, SIGNAL_RANGE_SMPTE, 0, 0, 0, 0, 0,
			&hdr10_param, &dv_reset_setting);
	}
	dv_context_new = false;
	if (p_funcs_new && dv_new_parser_ready)
		p_funcs_new->metadata_parser_release();
	dv_new_parser_ready = false;
	dv_new_md_size = 0;
	dv_new_comp_size = 0;
	dv_pending_l11 = false;
}

void amdv_set_l11(const struct dv5_vsif_parameter_s *vsif,
		  const struct dv5_content_info_s *ci)
{
	/* Adapter calls this only inside the serialized control-path transaction. */
	dv_pending_l11 = vsif && ci;
	if (dv_pending_l11) {
		dv_pending_vsif = *vsif;
		dv_pending_ci = *ci;
	}
}
EXPORT_SYMBOL(amdv_set_l11);

/* This interface does not change vframe_s or externally compiled decoder ABI.
 * Cached vf MD/COMP always belongs to the legacy parser. The new parser sees
 * the original HEVC SEI, never the legacy cache or its parse-error backups.
 * AM6B+ has no hardware AV1 path; unsupported raw formats stay on legacy.
 */
static bool dv_new_metadata_valid(const char *md, int size)
{
	const u8 *p, *end;
	unsigned int count = 0;
	if (size < ETSI_META_OFFSET || size > DV5_MD_CAPACITY)
		return false;
	p = (const u8 *)md + ETSI_META_OFFSET;
	end = (const u8 *)md + size;
	while (p < end) {
		u32 len;
		if (end - p < 5)
			return false;
		len = get_unaligned_be32(p);
		if (len > end - p - 5 || count == 255 ||
		    (p[4] == 5 && len < 8))
			return false;
		p += len + 5;
		count++;
	}
	return count == (u8)md[ETSI_META_OFFSET - 1];
}

static bool dv_new_parse_raw_locked(struct provider_aux_req_s *req,
				  bool drop, bool repeat)
{
	const u8 *p, *end;
	int md_len = 0, comp_len = 0;
	bool found = false;
	int ret;

	if (repeat && dv_new_parser_ready && dv_new_md_size > 0)
		return true;
	if (!req || !req->aux_buf || req->aux_size < 9)
		return false;
	p = (const u8 *)req->aux_buf;
	end = p + req->aux_size;
	while (end - p >= 8) {
		u32 len = get_unaligned_be32(p);
		u32 type = get_unaligned_be32(p + 4);
		p += 8;
		if (!len || len > end - p)
			return false;
		if (type == DV_SEI) {
			/* A second RPU in one record set has no supported ownership
			 * contract. Reject rather than concatenate opaque outputs. */
			if (found || len > sizeof(dv_new_rpu) - 2)
				return false;
			memset(dv_new_rpu, 0, 3);
			memcpy(dv_new_rpu + 3, p + 1, len - 1);
			found = true;
		}
		p += len;
	}
	if (!found || p != end)
		return false;
	/* Find the RPU length again only after the complete record set validated. */
	p = (const u8 *)req->aux_buf;
	while (get_unaligned_be32(p + 4) != DV_SEI)
		p += 8 + get_unaligned_be32(p);
	ret = get_unaligned_be32(p) + 2;
	if (!dv_new_parser_ready) {
		if (!p_funcs_new->metadata_parser_init(0))
			return false;
		dv_new_parser_ready = true;
		if (p_funcs_new->metadata_parser_reset(1)) {
			dv_new_retire_locked();
			return false;
		}
	}
	/* Full-size scratch destinations are required: the blob ABI has no
	 * capacity parameter. Its exact fingerprint/layout is checked by CE. */
	ret = p_funcs_new->metadata_parser_process(dv_new_rpu, ret,
			 dv_new_comp, &comp_len, dv_new_md, &md_len, true);
	if (ret < 0 || !dv_new_metadata_valid(dv_new_md, md_len) ||
	    comp_len < 0 || comp_len > sizeof(dv_new_comp)) {
		dv_new_retire_locked();
		return false;
	}
	if (drop) {
		/* Dropped metadata advances the parser but is never reusable as
		 * the displayed frame. A repeat after this must reparse raw SEI. */
		dv_new_md_size = 0;
		dv_new_comp_size = 0;
	} else {
		dv_new_md_size = md_len;
		dv_new_comp_size = comp_len;
	}
	return true;
}

static bool dv_prepare_new(struct provider_aux_req_s *req, bool drop, bool repeat)
{
	unsigned long flags;
	bool ok = false;
	spin_lock_irqsave(&dovi_lock, flags);
	if (p_funcs_stb && p_funcs_new && try_module_get(dv_new_owner)) {
		ok = dv_new_parse_raw_locked(req, drop, repeat);
		module_put(dv_new_owner);
	}
	spin_unlock_irqrestore(&dovi_lock, flags);
	return ok;
}

static void dv_retire_new(void)
{
	unsigned long flags;
	spin_lock_irqsave(&dovi_lock, flags);
	if (p_funcs_new && try_module_get(dv_new_owner)) {
		dv_new_retire_locked();
		module_put(dv_new_owner);
	}
	spin_unlock_irqrestore(&dovi_lock, flags);
}

/* Rebuild LL metadata in separate bounded scratch. The legacy LL L1 clamp and
 * filtering are deliberately absent here. Empty L5 offsets are required by
 * this blob's native LL input contract; output geometry remains core-owned.
 */
static int dv_new_ll_metadata(const char *md, int size, char *out)
{
	static const u8 zero_l5[] = { 0, 0, 0, 8, 5, 0, 0, 0, 0, 0, 0, 0, 0 };
	const u8 *p = (const u8 *)md + ETSI_META_OFFSET;
	const u8 *end = (const u8 *)md + size;
	size_t used = ETSI_META_OFFSET;
	unsigned int count = 0;
	bool have_l1 = false, have_l5 = false;

	if (size < ETSI_META_OFFSET || size > DV5_MD_CAPACITY)
		return -EINVAL;
	memcpy(out, md, ETSI_META_OFFSET);
	while (p < end) {
		u32 payload;
		size_t len;
		u8 level;
		if (end - p < 5)
			return -EINVAL;
		payload = get_unaligned_be32(p);
		if (payload > end - p - 5)
			return -EINVAL;
		len = payload + 5;
		level = p[4];
		if (level == 5 && payload < 8)
			return -EINVAL;
		if (have_l1 && !have_l5 && level > 5) {
			if (sizeof(zero_l5) > DV5_MD_CAPACITY - used || count == 255)
				return -EOVERFLOW;
			memcpy(out + used, zero_l5, sizeof(zero_l5));
			used += sizeof(zero_l5);
			count++;
			have_l5 = true;
		}
		if (level) {
			if (len > DV5_MD_CAPACITY - used || count == 255)
				return -EOVERFLOW;
			memcpy(out + used, p, len);
			if (level == 5) {
				memset(out + used + 5, 0, 8);
				have_l5 = true;
			}
			have_l1 |= level == 1;
			used += len;
			count++;
		}
		p += len;
	}
	if (have_l1 && !have_l5) {
		if (sizeof(zero_l5) > DV5_MD_CAPACITY - used || count == 255)
			return -EOVERFLOW;
		memcpy(out + used, zero_l5, sizeof(zero_l5));
		used += sizeof(zero_l5);
		count++;
	}
	out[ETSI_META_OFFSET - 1] = count;
	return used;
}

/* Processing context switches are serialized with parser callbacks and module
 * registration. The legacy pre-parser stays attached to its legacy owner;
 * it never supplies metadata to the newer CP. */
static int dv_run_control_path(bool use_new,
		enum signal_format_enum in, enum signal_format_enum out,
		char *comp, int comp_size, char *md, int md_size,
		enum priority_mode_enum priority, int bitdepth, int chroma, int range,
		int graphic_min, int graphic_max, int target_min, int target_max,
		int no_el, struct hdr10_parameter *hdr10,
		struct dovi_setting_s *setting)
{
	const struct dolby_vision_func_s *f;
	struct module *owner;
	unsigned long flags;
	int ret = -ENODEV;
	bool changed;

	spin_lock_irqsave(&dovi_lock, flags);
	f = use_new ? p_funcs_new : p_funcs_stb;
	owner = use_new ? dv_new_owner : dv_legacy_owner;
	if (!p_funcs_stb || !f || !try_module_get(owner))
		goto unlock;
	changed = use_new != dv_context_new;
	if (changed) {
		if (dv_context_new) {
			if (p_funcs_new && try_module_get(dv_new_owner)) {
				dv_new_retire_locked();
				module_put(dv_new_owner);
			}
		} else if (use_new && p_funcs_stb && try_module_get(dv_legacy_owner)) {
			p_funcs_stb->control_path(FORMAT_INVALID, 0,
				comp_buf[current_id], 0, md_buf[current_id], 0,
				0, 0, 0, SIGNAL_RANGE_SMPTE, 0, 0, 0, 0, 0,
				&hdr10_param, &dv_reset_setting);
			module_put(dv_legacy_owner);
		}
		dv_context_new = use_new;

	}
	dv_pending_l11 = false;
	dv_cp_candidate = *setting;
	if (changed)
		dv_cp_candidate.mode_changed = 1;
	if (use_new && setting->use_ll_flag) {
		md_size = dv_new_ll_metadata(md, md_size, dv_new_cp_md);
		if (md_size < 0) {
			ret = md_size;
			goto put;
		}
		md = dv_new_cp_md;
	}
	ret = f->control_path(in, out, comp, comp_size, md, md_size, priority,
		bitdepth, chroma, range, graphic_min, graphic_max, target_min,
		target_max, no_el, hdr10, &dv_cp_candidate);
	if (ret >= 0)
		*setting = dv_cp_candidate;
put:
	module_put(owner);
unlock:
	spin_unlock_irqrestore(&dovi_lock, flags);
	return ret;
}
