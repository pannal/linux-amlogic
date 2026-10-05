// SPDX-License-Identifier: GPL-2.0

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/printk.h>
#include <linux/string.h>
#include <linux/ratelimit.h>
#include <linux/random.h>
#include <linux/mutex.h>
#include <linux/errno.h>
#include <linux/preempt.h>
#include <stdarg.h>
#include <linux/amlogic/media/amdolbyvision/dolby_vision.h>
#include <linux/amlogic/media/vpu/vpu.h>
#include <linux/amlogic/media/video_sink/video.h>
enum vd_path_e;
#include "amdolby_vision.h"
#include "dv5_compat_abi.h"

#ifdef CONFIG_CFI_CLANG
#error "DV5 compat shim requires the supported non-CFI target kernel"
#endif

static unsigned int dv_shim_debug;
module_param(dv_shim_debug, uint, 0664);
MODULE_PARM_DESC(dv_shim_debug, "bit0 reg, bit1 control_path, bit2 output, bit3 CFI checks");

#define DVSHIM_REG BIT(0)
#define DVSHIM_CP BIT(1)
#define DVSHIM_OUT BIT(2)
#define DVSHIM_CFI BIT(3)

#define dvshim_dbg(bit, fmt, args...)                                                              \
	do {                                                                                       \
		static DEFINE_RATELIMIT_STATE(_rs, HZ, 2);                                         \
		if (unlikely((dv_shim_debug & (bit)) && __ratelimit(&_rs)))                        \
			pr_info("DVSHIM: " fmt, ##args);                                           \
	} while (0)

static unsigned long dvshim_cfi_hits;
module_param(dvshim_cfi_hits, ulong, 0444);
static unsigned long dvshim_ubsan_hits;
module_param(dvshim_ubsan_hits, ulong, 0444);
static unsigned long dvshim_stkchk_hits;
module_param(dvshim_stkchk_hits, ulong, 0444);
static unsigned long dvshim_cp_calls;
module_param(dvshim_cp_calls, ulong, 0444);
static unsigned long dvshim_mp_calls;
module_param(dvshim_mp_calls, ulong, 0444);

static const struct dv5_funcs *blob_funcs;
static struct dolby_vision_func_s adapted_funcs;
static struct module *blob_owner;
static unsigned long blob_text;
static DEFINE_MUTEX(registration_lock);

/* All adapter calls are serialized by the driver's dovi_lock.  Registration
 * publishes only after initialization; unregister quiesces calls before teardown.
 * No shim spinlock may recurse into the driver lock. */
#define DV5_MD_CAPACITY 4096
#define DV5_COMP_CAPACITY 32784
#define DV5_MAX_RPU_SIZE 1024
static char mp_md[DV5_MD_CAPACITY];
static char mp_comp[DV5_COMP_CAPACITY];

struct dv5_last_cp {
	enum signal_format_enum in, out;
	u32 use_ll, ll_rgb, width, height;
	int priority;
};
static struct dv5_last_cp last_cp;

/* The prepared supported blob's 100 canary load pairs reference this lifetime
 * constant.  It is initialized before registration and never changed afterward.
 * Both prologue and epilogue checks remain present in the blob. */
unsigned long dv5_stack_chk_guard __aligned(8);
EXPORT_SYMBOL(dv5_stack_chk_guard);

/* Preparation reads these from the compiled target shim instead of assuming a
 * module name placement from a different vendor kernel configuration. */
const u32 dv5_module_name_offset = offsetof(struct module, name);
const u32 dv5_module_name_size = sizeof(((struct module *)0)->name);
EXPORT_SYMBOL(dv5_module_name_offset);
EXPORT_SYMBOL(dv5_module_name_size);
static struct m_dovi_setting_s shim_m_setting;
static struct m_dovi_setting_s shim_invalid;
/* Allocated before publication in sleepable registration; callback init/release
 * only attach/detach and reset.  The blob uses vmalloc/vfree for real lifetime. */
static void *mp_ctx;
static bool mp_attached;
static bool mp_ready;
static int mp_dv_type = DV5_TYPE_DOVI;

#define DV5_OUTPUT_CTRL_DATA_SIZE 0x1000
static u8 shim_output_ctrl_data[DV5_OUTPUT_CTRL_DATA_SIZE];

int get_cpu_type_from_media(void)
{
	static bool once;

	if (!once) {
		once = true;
		dvshim_dbg(DVSHIM_REG,
			   "get_cpu_type_from_media -> 0 (force get_meson_cpu_version fallback)\n");
	}
	return 0;
}
EXPORT_SYMBOL(get_cpu_type_from_media);

/* Exact .text offsets and CFI IDs from the supported source SHA-256:
 * f6c26659a255447685ceac9441e399c999b1fae9c6435c48d70e14a14dd7f8f7.
 * Preparation must enforce that profile before loading. */
static bool dv5_cfi_allowed(u64 id, unsigned long target, unsigned long base)
{
	if (!base)
		return false;
	if (id == 0xf7247573865c0423ULL)
		return target == base + 0x1e8;
	if (id == 0xf5d57d915c360469ULL)
		return target == base + 0x1c4;
	return false;
}

void __cfi_slowpath_diag(u64 id, void *ptr, void *diag)
{
	void (*check)(u64, void *, void *);
	unsigned long base = READ_ONCE(blob_text);

	if (!dv5_cfi_allowed(id, (unsigned long)ptr, base))
		panic("DV5: rejected CFI type/target id=%llx target=%px", id, ptr);
	check = (void (*)(u64, void *, void *))base;
	/* Execute the blob's real type checker, not an unchecked returning stub. */
	check(id, ptr, diag);
	dvshim_cfi_hits++;
}
EXPORT_SYMBOL(__cfi_slowpath_diag);

void __noreturn __ubsan_handle_cfi_check_fail_abort(void *data, void *value, void *vtype)
{
	dvshim_ubsan_hits++;
	panic("DV5: CFI check failed target=%px", value);
}
EXPORT_SYMBOL(__ubsan_handle_cfi_check_fail_abort);

#ifndef CONFIG_CC_STACKPROTECTOR
void __noreturn __stack_chk_fail(void)
{
	dvshim_stkchk_hits++;
	panic("DV5: stack protector check failed");
}
EXPORT_SYMBOL(__stack_chk_fail);
#endif

static void cp_forget(void)
{
	last_cp.in = FORMAT_INVALID;
	last_cp.out = FORMAT_INVALID;
	last_cp.use_ll = ~0U;
	last_cp.ll_rgb = ~0U;
	last_cp.priority = -1;
	last_cp.width = 0;
	last_cp.height = 0;
}

static bool blob_get(void) { return blob_funcs && blob_owner && try_module_get(blob_owner); }

static void blob_put(void) { module_put(blob_owner); }

int _printk(const char *fmt, ...)
{
	va_list args;
	int r;

	va_start(args, fmt);
	r = vprintk(fmt, args);
	va_end(args);
	return r;
}
EXPORT_SYMBOL(_printk);

static void cp_setup_invalid(void)
{
	memset(&shim_invalid, 0, sizeof(shim_invalid));
	shim_invalid.num_input = 0;
	shim_invalid.input[0].src_format = FORMAT_INVALID;
	shim_invalid.input[1].src_format = FORMAT_INVALID;
	shim_invalid.input[DV5_IPCORE2_ID].src_format = FORMAT_INVALID;
}

static void cp_send_reset(void)
{
	if (blob_funcs && blob_funcs->multi_control_path) {
		dvshim_dbg(DVSHIM_CP, "reset (num_input=0)\n");
		blob_funcs->multi_control_path(&shim_invalid);
	}
}

static int cp_adapter(enum signal_format_enum in_format, enum signal_format_enum out_format,
		      char *in_comp, int in_comp_size, char *in_md, int in_md_size,
		      enum priority_mode_enum set_priority, int set_bit_depth,
		      int set_chroma_format, int set_yuv_range, int set_graphic_min_lum,
		      int set_graphic_max_lum, int set_target_min_lum, int set_target_max_lum,
		      int set_no_el, struct hdr10_parameter *hdr10_param,
		      struct dovi_setting_s *output)
{
	struct m_dovi_setting_s *m = &shim_m_setting;
	struct private_info_s *vid = &m->input[0];
	struct private_info_s *gfx = &m->input[DV5_IPCORE2_ID];
	bool need_reset;
	int flag;
	int ret;

	dvshim_cp_calls++;

	if (!output || !blob_funcs || !blob_funcs->multi_control_path)
		return -ENODEV;
	if (in_format != FORMAT_INVALID &&
	    (in_format != FORMAT_DOVI && in_format != FORMAT_DOVI_LL))
		return -EINVAL;
	if (in_format != FORMAT_INVALID &&
	    (out_format != FORMAT_DOVI || in_comp_size < 0 || in_comp_size > DV5_COMP_CAPACITY ||
	     in_md_size < 0 || in_md_size > DV5_MD_CAPACITY || (in_comp_size && !in_comp) ||
	     (in_md_size && !in_md) || output->vsvdb_len > sizeof(output->vsvdb_tbl) ||
	     set_chroma_format < DV5_CP_P420 || set_chroma_format > DV5_CP_I444 ||
	     set_yuv_range < DV5_SIGNAL_RANGE_SMPTE || set_yuv_range > DV5_SIGNAL_RANGE_SDI ||
	     set_priority < V_PRIORITY || set_priority > VIDEO_PRIORITY_DELAY ||
	     set_bit_depth < 8 || set_bit_depth > 12 || !(output->video_width >> 16) ||
	     (output->video_width >> 16) == 0xffff || !(output->video_height >> 16) ||
	     (output->video_height >> 16) == 0xffff || output->g_format < G_SDR_YUV ||
	     output->g_format > G_HDR_RGB || output->g_bitdepth < 8 || output->g_bitdepth > 12))
		return -EINVAL;
	if (in_format != FORMAT_INVALID && (!mp_attached || !mp_ready))
		return -ENODEV;
	if (!blob_get())
		return -ENODEV;

	if (in_format == FORMAT_INVALID) {
		cp_forget();
		cp_send_reset();
		amdv_set_l11(NULL, NULL);
		blob_put();
		return -1;
	}

	memset(m, 0, sizeof(*m));
	m->num_input = DV5_NUM_INPUTS;
	m->num_video = DV5_NUM_IPCORE1;
	m->pri_input = 0;
	m->enable_multi_core1 = 0;
	m->set_priority = set_priority;
	m->dst_format = out_format;
	m->vout_width = output->vout_width;
	m->vout_height = output->vout_height;
	m->use_ll_flag = output->use_ll_flag;
	m->ll_rgb_desired = output->ll_rgb_desired;
	m->dovi2hdr10_nomapping = output->dovi2hdr10_nomapping;
	memcpy(m->vsvdb_tbl, output->vsvdb_tbl, sizeof(m->vsvdb_tbl));
	m->vsvdb_len = output->vsvdb_len;
	m->vsvdb_changed = output->vsvdb_changed;
	m->mode_changed = output->mode_changed;
	m->set_graphic_min_lum = set_graphic_min_lum;
	m->set_graphic_max_lum = set_graphic_max_lum;
	m->set_target_min_lum = set_target_min_lum;
	m->set_target_max_lum = set_target_max_lum;
	m->output_ctrl_data = shim_output_ctrl_data;
	m->output_ctrl_data_len = DV5_OUTPUT_CTRL_DATA_SIZE;

	vid->valid = 1;
	vid->src_format = in_format;
	vid->el_flag = !set_no_el;
	vid->el_halfsize_flag = output->el_halfsize_flag;
	vid->video_width = output->video_width >> 16;
	vid->video_height = output->video_height >> 16;
	vid->set_bit_depth = DV5_VIDEO_BIT_DEPTH;
	vid->set_chroma_format = set_chroma_format;
	vid->set_yuv_range = set_yuv_range;
	vid->color_format = DV5_CP_YUV;
	vid->in_comp = in_comp;
	vid->in_comp_size = in_comp_size;
	vid->in_md = in_md;
	vid->in_md_size = in_md_size;
	vid->input_mode = DV5_IN_MODE_OTT;
	vid->p_hdr10_param = hdr10_param;

	gfx->valid = 1;
	gfx->input_mode = DV5_IN_MODE_GRAPHICS;
	gfx->src_format = (output->g_format == G_HDR_YUV || output->g_format == G_HDR_RGB)
			      ? FORMAT_HDR10
			      : FORMAT_SDR;
	gfx->video_width = DV5_GRAPHIC_W;
	gfx->video_height = DV5_GRAPHIC_H;
	gfx->set_bit_depth = output->g_bitdepth;
	gfx->set_chroma_format = DV5_CP_I444;
	gfx->set_yuv_range = DV5_SIGNAL_RANGE_FULL;
	gfx->color_format = (output->g_format == G_SDR_RGB || output->g_format == G_HDR_RGB)
				? DV5_CP_RGB
				: DV5_CP_YUV;

	dvshim_dbg(
	    DVSHIM_CP,
	    "cp in=%d out=%d el=%d bd=%d %ux%u md=%d cmp=%d pri=%d vsvdb=%d ll=%d gfmt=%d gbd=%d\n",
	    in_format, out_format, vid->el_flag, set_bit_depth, vid->video_width, vid->video_height,
	    in_md_size, in_comp_size, set_priority, m->vsvdb_len, m->use_ll_flag, output->g_format,
	    output->g_bitdepth);

	need_reset = in_format != last_cp.in || out_format != last_cp.out ||
		     m->use_ll_flag != last_cp.use_ll || m->ll_rgb_desired != last_cp.ll_rgb ||
		     (int)set_priority != last_cp.priority || vid->video_width != last_cp.width ||
		     vid->video_height != last_cp.height;
	if (need_reset)
		cp_send_reset();
	last_cp.in = in_format;
	last_cp.out = out_format;
	last_cp.use_ll = m->use_ll_flag;
	last_cp.ll_rgb = m->ll_rgb_desired;
	last_cp.priority = set_priority;
	last_cp.width = vid->video_width;
	last_cp.height = vid->video_height;

	flag = blob_funcs->multi_control_path(m);
	blob_put();
	if (flag < 0) {
		cp_forget();
		return flag;
	}
	if (m->output_ctrl_data != shim_output_ctrl_data ||
	    m->output_ctrl_data_len > DV5_OUTPUT_CTRL_DATA_SIZE ||
	    m->vsvdb_len > sizeof(m->vsvdb_tbl) ||
	    m->md_reg3.size > ARRAY_SIZE(m->md_reg3.raw_metadata) || m->dovi_ll_enable > 1 ||
	    m->diagnostic_enable > 1) {
		cp_forget();
		return -EOVERFLOW;
	}
	if (flag)
		dvshim_dbg(DVSHIM_CP, "multi_control_path ret=%d\n", flag);

	memcpy(&output->comp_reg, &m->core1[0].comp_reg, sizeof(output->comp_reg));
	memcpy(&output->dm_reg1, &m->core1[0].dm_reg, sizeof(output->dm_reg1));
	memcpy(&output->dm_lut1, &m->core1[0].dm_lut, sizeof(output->dm_lut1));
	memcpy(&output->dm_reg2, &m->dm_reg2, sizeof(output->dm_reg2));
	memcpy(&output->dm_reg3, &m->dm_reg3, sizeof(output->dm_reg3));
	memcpy(&output->dm_lut2, &m->dm_lut2, sizeof(output->dm_lut2));
	memcpy(&output->md_reg3, &m->md_reg3, sizeof(output->md_reg3));
	memcpy(&output->hdr_info, &m->hdr_info, sizeof(output->hdr_info));
	memcpy(&output->ext_md, &m->ext_md, sizeof(output->ext_md));
	amdv_set_l11(&m->output_vsif, &m->content_info);
	output->src_format = in_format;
	output->dst_format = out_format;
	output->el_flag = vid->el_flag;
	output->diagnostic_enable = (out_format == FORMAT_DOVI) ? m->diagnostic_enable : 0;
	output->diagnostic_mux_select = (out_format == FORMAT_DOVI) ? m->diagnostic_mux_select : 0;
	output->dovi_ll_enable = (out_format == FORMAT_DOVI) ? m->dovi_ll_enable : 0;

	if (unlikely(dv_shim_debug & DVSHIM_OUT)) {
		u32 *d1 = (u32 *)&output->dm_reg1;
		u32 *d3 = (u32 *)&output->dm_reg3;
		char b[260];
		int i, n = 0;

		dvshim_dbg(DVSHIM_OUT, "y2rgb c=%08x %08x %08x %08x %08x o=%08x %08x %08x\n", d1[5],
			   d1[6], d1[7], d1[8], d1[9], d1[10], d1[11], d1[12]);
		for (i = 0; i < 26 && n < 250; i++)
			n += scnprintf(b + n, sizeof(b) - n, "%08x ", d3[i]);
		dvshim_dbg(DVSHIM_OUT, "dm3= %s\n", b);
	}

	if (flag < 0)
		return flag;
	ret = 0;
	if (flag & DV5_CP_FLAG_BLOB_CHANGE_TC)
		ret |= DV5_CP_FLAG_CHANGE_TC;
	if (flag & DV5_CP_FLAG_BLOB_CHANGE_TC2)
		ret |= DV5_CP_FLAG_CHANGE_TC2;
	ret |= flag & DV5_CP_FLAG_CONST_TC2;
	return ret;
}

static void *mp_init_adapter(int flag)
{
	/* The supported blob ignores init's flag.  Driver reset conveys the
	 * sequence reset separately, without allocating in the IRQ callback. */
	(void)flag;
	if (!mp_ctx || !mp_ready || !blob_get())
		return NULL;
	mp_attached = true;
	blob_put();
	return mp_ctx;
}

static int mp_reset_adapter(int flag)
{
	int ret;

	if (!mp_attached || !mp_ready || !mp_ctx)
		return -ENODEV;
	/* The blob's multi reset ignores its flag and unconditionally resets.
	 * Legacy reset(0) is a no-op; do not erase temporal state every frame. */
	if (!flag)
		return 0;
	if (!blob_get())
		return -ENODEV;
	mp_dv_type = (flag & 0x2) ? DV5_TYPE_ATSC : DV5_TYPE_DOVI;
	ret = blob_funcs->multi_mp_reset(mp_ctx, flag);
	blob_put();
	if (ret)
		mp_ready = false;
	return ret;
}

static int mp_process_adapter(char *src_rpu, int rpu_len, char *dst_comp, int *comp_len,
			      char *dst_md, int *md_len, bool src_eos)
{
	int ret, comp_size = 0, md_size = 0;

	/* Caller must provide standalone full-capacity destinations.  Parse
	 * fragments into these, then perform checked concatenation in the driver;
	 * the inherited callback ABI has no destination-capacity arguments. */
	if (!src_rpu || rpu_len < 7 || rpu_len > DV5_MAX_RPU_SIZE || !dst_comp || !dst_md ||
	    !comp_len || !md_len)
		return -EINVAL;
	if (!mp_attached || !mp_ready || !mp_ctx || !blob_get())
		return -ENODEV;
	ret = blob_funcs->multi_mp_process(mp_ctx, src_rpu, rpu_len, mp_comp, &comp_size, mp_md,
					   &md_size, src_eos, mp_dv_type);
	blob_put();
	dvshim_mp_calls++;
	if (ret < 0)
		return ret;
	if (comp_size < 0 || comp_size > DV5_COMP_CAPACITY || md_size < 0 ||
	    md_size > DV5_MD_CAPACITY)
		return -EOVERFLOW;
	memcpy(dst_comp, mp_comp, comp_size);
	memcpy(dst_md, mp_md, md_size);
	*comp_len = comp_size;
	*md_len = md_size;
	return ret;
}

/* Used only in sleepable registration rollback/unregister, after callbacks
 * are unpublished/quiesced.  Caller module_init/module_exit owns blob text. */
static void mp_release_owned(void)
{
	if (mp_ctx && blob_funcs)
		blob_funcs->multi_mp_release(&mp_ctx);
	mp_ctx = NULL;
	mp_attached = false;
	mp_ready = false;
	mp_dv_type = DV5_TYPE_DOVI;
	cp_forget();
	amdv_set_l11(NULL, NULL);
}

static void mp_release_adapter(void)
{
	/* Safe under dovi_lock: reset has no allocation in the supported binary.
	 * Actual vmalloc/vfree lifetime is confined to registration/unregister. */
	if (mp_ctx && mp_attached && blob_get()) {
		mp_ready = blob_funcs->multi_mp_reset(mp_ctx, 1) == 0;
		cp_send_reset();
		blob_put();
	}
	mp_attached = false;
	mp_dv_type = DV5_TYPE_DOVI;
	memset(mp_comp, 0, sizeof(mp_comp));
	memset(mp_md, 0, sizeof(mp_md));
	cp_forget();
	amdv_set_l11(NULL, NULL);
}

static bool dv5_branch_matches(unsigned long base, unsigned long stub, unsigned long body)
{
	u32 insn = le32_to_cpu(*(__le32 *)(base + stub));

	return insn == (0x14000000U | ((body - stub) / 4));
}

static int validate_blob(const struct dv5_funcs *f, struct module **owner_out,
			 unsigned long *base_out)
{
	struct module *owner;
	unsigned long base, text_end;

	if (!f || !f->version_info || !f->multi_control_path || !f->multi_mp_init ||
	    !f->multi_mp_reset || !f->multi_mp_process || !f->multi_mp_release)
		return -EINVAL;
	if (strnlen(f->version_info, 499) == 499 || !strstr(f->version_info, "[stb:2.6:e]-[v1.0]-"))
		return -EINVAL;
	/* The supported 96-byte table has exactly these five non-null callbacks. */
	if (f->metadata_parser_init || f->metadata_parser_reset || f->metadata_parser_process ||
	    f->metadata_parser_release || f->control_path || f->tv_control_path)
		return -EINVAL;
	base = (unsigned long)f->multi_control_path - 0x1d0;
	preempt_disable();
	owner = __module_address((unsigned long)f->multi_control_path);
	if (!owner || strcmp(owner->name, "dovi5")) {
		preempt_enable();
		return -EINVAL;
	}
	text_end = (unsigned long)owner->core_layout.base + owner->core_layout.text_size;
	if (base < (unsigned long)owner->core_layout.base || base > text_end ||
	    text_end - base < 0x28ef8 || (base & 4095)) {
		preempt_enable();
		return -EINVAL;
	}
	preempt_enable();
	if ((unsigned long)f->multi_mp_init != base + 0x1d4 ||
	    (unsigned long)f->multi_mp_reset != base + 0x1e4 ||
	    (unsigned long)f->multi_mp_process != base + 0x1d8 ||
	    (unsigned long)f->multi_mp_release != base + 0x1e0 ||
	    le32_to_cpu(*(__le32 *)base) != 0xd2848888U ||
	    !dv5_branch_matches(base, 0x1d0, 0x3af0) || !dv5_branch_matches(base, 0x1d4, 0x288f8) ||
	    !dv5_branch_matches(base, 0x1e4, 0x28c80) ||
	    !dv5_branch_matches(base, 0x1d8, 0x28ce4) ||
	    !dv5_branch_matches(base, 0x1e0, 0x28c1c) ||
	    !dv5_branch_matches(base, 0x1e8, 0x28a6c) || !dv5_branch_matches(base, 0x1c4, 0x3a48))
		return -EINVAL;
	*owner_out = owner;
	*base_out = base;
	return 0;
}

int register_dv5shim_func(const struct dv5_funcs *f)
{
	struct module *owner;
	unsigned long base;
	int ret;

	mutex_lock(&registration_lock);
	if (blob_funcs) {
		ret = -EBUSY;
		goto out;
	}
	ret = validate_blob(f, &owner, &base);
	if (ret)
		goto out;
	blob_funcs = f;
	blob_owner = owner;
	blob_text = base;
	mp_attached = false;
	mp_ready = false;
	/* Sleepable allocation while no callbacks are published. */
	mp_ctx = f->multi_mp_init(0);
	if (!mp_ctx) {
		ret = -ENOMEM;
		goto rollback;
	}
	ret = f->multi_mp_reset(mp_ctx, 1);
	if (ret)
		goto rollback;
	mp_ready = true;
	cp_forget();
	memset(&adapted_funcs, 0, sizeof(adapted_funcs));
	adapted_funcs.version_info = f->version_info;
	adapted_funcs.metadata_parser_init = mp_init_adapter;
	adapted_funcs.metadata_parser_reset = mp_reset_adapter;
	adapted_funcs.metadata_parser_process = mp_process_adapter;
	adapted_funcs.metadata_parser_release = mp_release_adapter;
	adapted_funcs.control_path = cp_adapter;
	cp_setup_invalid();
	ret = register_dv_functions_multi(&adapted_funcs);
	if (!ret)
		goto out;
rollback:
	mp_release_owned();
	blob_funcs = NULL;
	blob_owner = NULL;
	blob_text = 0;
	memset(&adapted_funcs, 0, sizeof(adapted_funcs));
out:
	mutex_unlock(&registration_lock);
	return ret;
}
EXPORT_SYMBOL(register_dv5shim_func);

int unregister_dv5shim_func(void)
{
	int ret;

	mutex_lock(&registration_lock);
	if (!blob_funcs) {
		ret = -ENOENT;
		goto out;
	}
	ret = unregister_dv_functions_multi();
	/* Module exit cannot leave live callbacks into retiring module text. */
	if (ret)
		panic("DV5: driver failed to quiesce registered backend (%d)", ret);
	mp_release_owned();
	blob_funcs = NULL;
	blob_owner = NULL;
	blob_text = 0;
	memset(&adapted_funcs, 0, sizeof(adapted_funcs));
out:
	mutex_unlock(&registration_lock);
	return ret;
}
EXPORT_SYMBOL(unregister_dv5shim_func);

static int __init dv_compat_shim_init(void)
{
	BUILD_BUG_ON(sizeof(struct dv5_funcs) != 96);
	BUILD_BUG_ON(sizeof(struct private_info_s) != 104);
	BUILD_BUG_ON(sizeof(struct m_dovi_setting_s) != 20312);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, input) != 96);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, core1) != 424);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, dm_reg2) != 12264);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, dm_reg3) != 12360);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, dm_lut2) != 12464);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, md_reg3) != 17584);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, hdr_info) != 19636);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, ext_md) != 19680);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, output_vsif) != 19732);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, output_ctrl_data) != 19776);
	BUILD_BUG_ON(offsetof(struct m_dovi_setting_s, content_info) != 19792);
	do {
		get_random_bytes(&dv5_stack_chk_guard, sizeof(dv5_stack_chk_guard));
		dv5_stack_chk_guard &= ~0xffUL;
	} while (!dv5_stack_chk_guard);
	pr_info("DVSHIM: dv5->4.9 compat shim loaded, m_setting=%zu B, debug=0x%x\n",
		sizeof(struct m_dovi_setting_s), dv_shim_debug);
	return 0;
}

static void __exit dv_compat_shim_exit(void)
{
	pr_info("DVSHIM: unloaded cp=%lu cfi=%lu ubsan=%lu stkchk=%lu\n", dvshim_cp_calls,
		dvshim_cfi_hits, dvshim_ubsan_hits, dvshim_stkchk_hits);
}

module_init(dv_compat_shim_init);
module_exit(dv_compat_shim_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Dolby Vision 5.15 dovi.ko compatibility shim for the 4.9 kernel");
