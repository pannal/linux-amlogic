// SPDX-License-Identifier: GPL-2.0
/* Host boundary model; the code under test is included from production files. */
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "production_constants.h"

#define CHECK(condition, message) do { \
	if (!(condition)) { \
		fprintf(stderr, "FAIL: %s (line %d)\n", message, __LINE__); \
		exit(1); \
	} \
} while (0)

typedef uint32_t u32;
typedef unsigned long ulong;
typedef int atomic_t;
#define atomic_inc(value) (++*(value))
#define atomic_dec(value) (--*(value))
#define atomic_read(value) (*(value))
#define atomic_set(value, next) (*(value) = (next))
#define atomic64_inc atomic_inc
#define WRITE_ONCE(value, next) ((value) = (next))
#define smp_wmb() ((void)0)
#define EAGAIN 11
#define VFRAME_SOURCE_TYPE_PPMGR 1
#define VFRAME_SIGNAL_FMT_INVALID 0
#define VIDEO_DISABLE_FORNEXT 1
#define VIDEO_STOP 1
#define AFBC_ENABLE 1
#define AFBC_HEAD_BADDR 2
#define IS_DI_PROCESSED(type) \
	((type) & (VIDTYPE_PRE_INTERLACE | VIDTYPE_DI_PW))
#define IS_DI_POSTWRTIE(type) ((type) & VIDTYPE_DI_PW)

struct vframe_pic_mode_s { int unused; };
struct vframe_s {
	u32 type, flag, compHeadAddr;
	int video_angle, source_type;
	void *vf_ext, *mem_handle, *mem_head_handle, *mem_dw_handle;
	struct vframe_pic_mode_s pic_mode;
};

static struct vframe_s vf_local, vf_local_ext, vf_local2, local_pip;
static struct vframe_s *cur_dispbuf, *cur_dispbuf2, *cur_pipbuf;
static struct vframe_s *cur_rdma_buf;
static struct vframe_s *dispbuf_to_put[DISPBUF_TO_PUT_MAX];
#ifdef CONFIG_AMLOGIC_MEDIA_VSYNC_RDMA
static struct vframe_s *pip_rdma_buf;
static struct vframe_s *pipbuf_to_put[DISPBUF_TO_PUT_MAX];
static int pipbuf_to_put_num;
#endif
static struct vframe_s *recycle_buf[2][1 + DISPBUF_TO_PUT_MAX];
static struct {
	struct vframe_s **dispbuf_mapping;
	struct { u32 afbc_enable, afbc_head_baddr; } vd_afbc_reg;
	int disable_video;
} vd_layer[2];
static int recycle_cnt[2], di_release_count, dispbuf_to_put_num;
static int lock, video_keeper_mutex;
static int keep_id, keep_head_id, keep_dw_id, keep_el_id, keep_el_head_id;
static int keep_el_dw_id, keep_video_on;
static int new_frame_count, first_frame_toggled, videopeek, nopostvideostart;
static int hold_property_changed, frame_detect_drop_count;
static int frame_detect_receive_count, fps_sample_duration;
static int need_disable_vd2, trickmode_fffb, to_notify_trick_wait;
static int vsync_pts_100, vsync_pts_112, vsync_pts_125, vsync_freerun;
static int vsync_pts_align, vsync_pts_aligned, pip_loop, pip_frame_count;
static int blackout, force_blackout, nopostvideostop, hdmi_in_onvideo;
static int video_start_post, enable_video_discontinue_report;
static int show_first_picture, show_first_frame_nosync, vframe_walk_delay;
static int time_setomxpts, time_setomxpts_last, vdin_err_crc_cnt;
static atomic_t video_unreg_flag, video_inirq_flag, presentation_epoch;
static atomic_t primary_src_fmt, trickmode_framedone;
static struct video_frame_detect_s { int unused; } video_frame_detect;

static bool spin_locked, mutex_locked, rdma_enabled;
static bool layer_enabled[2], fail_scanout_keep;
static unsigned int now_ms, warnings, schedules, mask_calls;
static u32 registers[256];
static const char *test_name;

struct memory {
	bool provider_owned, held;
	unsigned int release_at;
	unsigned char contents;
	int type;
};
/* Three independent allocations (body, header, double-write) per frame. */
static struct memory memory[6];
static struct vframe_s scanout, queued, decoy, di_frame;

static void log_message(bool warning, const char *format, ...)
{
	if (warning)
		warnings++;
}
#define pr_warn(...) log_message(true, __VA_ARGS__)
#define pr_warn_ratelimited(...) log_message(true, __VA_ARGS__)
#define pr_info(...) log_message(false, __VA_ARGS__)
#define pr_err(...) log_message(false, __VA_ARGS__)
#define pr_debug(...) log_message(false, __VA_ARGS__)

#define spin_lock_irqsave(unused, flags) do { \
	(void)(unused); \
	(flags) = 0; \
	CHECK(!spin_locked, "recursive spinlock"); \
	spin_locked = true; \
} while (0)
#define spin_unlock_irqrestore(unused, flags) do { \
	(void)(unused); \
	(void)(flags); \
	CHECK(spin_locked, "unbalanced spinlock"); \
	spin_locked = false; \
} while (0)

static void mutex_lock(int *unused)
{
	CHECK(!spin_locked && !mutex_locked, "keeper must run outside spinlock");
	mutex_locked = true;
}

static void mutex_unlock(int *unused)
{
	CHECK(mutex_locked, "unbalanced keeper mutex");
	mutex_locked = false;
}

static void schedule(void)
{
	schedules++;
	video_inirq_flag = 0;
}

static bool is_vsync_rdma_enable(void) { return rdma_enabled; }
static bool is_dolby_vision_enable(void) { return false; }
static unsigned int get_video_debug_flags(void) { return 0; }
static int get_blackout_policy(void) { return blackout | force_blackout; }
static void presentation_route_invalidate(void) { }
static void tsync_avevent(int event, int value) { }
static void dim_post_keep_cmd_release2(struct vframe_s *vf) { }

static u32 read_vcbus(u32 reg)
{
	CHECK(reg < 256, "invalid simulated register");
	if (reg == vd_layer[0].vd_afbc_reg.afbc_enable ||
	    reg == vd_layer[0].vd_afbc_reg.afbc_head_baddr) {
		CHECK(spin_locked && video_unreg_flag && !video_inirq_flag,
		      "scanout snapshot must follow IRQ drain, under lock");
	}
	return registers[reg];
}
#define READ_VCBUS_REG(reg) read_vcbus(reg)

static int codec_mm_keeper_mask_keep_mem(void *handle, int type)
{
	struct memory *mem = handle;

	CHECK(mutex_locked, "memory must be pinned under keeper mutex");
	CHECK(mem >= memory && mem < memory + 6, "unknown memory handle");
	CHECK(mem->provider_owned, "provider handle expired before pin");
	CHECK(mem->type == type, "wrong scatter/body/header allocation type");
	mask_calls++;
	if (fail_scanout_keep && mem < memory + 3)
		return 0;
	mem->held = true;
	mem->release_at = 0;
	return (int)(mem - memory) + 1;
}

static void codec_mm_keeper_unmask_keeper(int id, int delay_ms)
{
	CHECK(id > 0 && id <= 6, "invalid keeper ID");
	CHECK(memory[id - 1].held, "release of unpinned memory");
	memory[id - 1].release_at = now_ms + delay_ms;
	if (!delay_ms)
		memory[id - 1].held = false;
}

static void safe_switch_videolayer(int layer, bool enable, bool unused)
{
	layer_enabled[layer] = enable;
}

static void video_keeper_new_frame_notify(void);

static void try_free_keep_video(int flags)
{
	video_keeper_new_frame_notify();
}

#include "production_functions.h"

static void reset(bool scatter)
{
	memset(memory, 0, sizeof(memory));
	for (int i = 0; i < 6; i++) {
		memory[i].provider_owned = true;
		memory[i].contents = 0x5a;
		memory[i].type = scatter && i % 3 == 0 ?
			MEM_TYPE_CODEC_MM_SCATTER : MEM_TYPE_CODEC_MM;
	}
	scanout = (struct vframe_s) {
		.type = VIDTYPE_COMPRESS | (scatter ? VIDTYPE_SCATTER : 0),
		.compHeadAddr = 0x123450,
		.mem_handle = &memory[0],
		.mem_head_handle = &memory[1],
		.mem_dw_handle = &memory[2],
	};
	queued = (struct vframe_s) {
		.type = scanout.type,
		.compHeadAddr = 0x987650,
		.mem_handle = &memory[3],
		.mem_head_handle = &memory[4],
		.mem_dw_handle = &memory[5],
	};
	decoy = scanout;
	decoy.type = 0;
	di_frame = scanout;
	di_frame.type |= VIDTYPE_DI_PW;
	memset(&vf_local, 0, sizeof(vf_local));
	memset(dispbuf_to_put, 0, sizeof(dispbuf_to_put));
	memset(recycle_buf, 0, sizeof(recycle_buf));
	memset(recycle_cnt, 0, sizeof(recycle_cnt));
	memset(vd_layer, 0, sizeof(vd_layer));
	memset(registers, 0, sizeof(registers));
	/* Deliberately different from legacy fixed-register addresses. */
	vd_layer[0].vd_afbc_reg.afbc_enable = 0x31;
	vd_layer[0].vd_afbc_reg.afbc_head_baddr = 0x72;
	vd_layer[0].dispbuf_mapping = &cur_dispbuf;
	registers[0x31] = 0x100;
	registers[0x72] = scanout.compHeadAddr >> 4;
	cur_dispbuf = cur_rdma_buf = &queued;
	cur_dispbuf2 = cur_pipbuf = NULL;
	dispbuf_to_put[0] = &decoy;
	dispbuf_to_put[1] = &di_frame;
	dispbuf_to_put[DISPBUF_TO_PUT_MAX - 1] = &scanout;
	dispbuf_to_put_num = DISPBUF_TO_PUT_MAX;
	keep_id = keep_head_id = keep_dw_id = 0;
	keep_el_id = keep_el_head_id = keep_el_dw_id = keep_video_on = 0;
	blackout = force_blackout = 0;
	video_unreg_flag = 0;
	video_inirq_flag = 1;
	spin_locked = mutex_locked = false;
	layer_enabled[0] = layer_enabled[1] = rdma_enabled = true;
	fail_scanout_keep = false;
	now_ms = warnings = schedules = mask_calls = 0;
}

static bool reuse(struct memory *mem)
{
	if (mem->held && mem->release_at && now_ms >= mem->release_at)
		mem->held = false;
	if (mem->provider_owned || mem->held)
		return false;
	mem->contents = 0xee;
	return true;
}

static void provider_returns(void)
{
	CHECK(!spin_locked && !mutex_locked && !video_unreg_flag,
	      "unregister did not release locks/guard");
	CHECK(schedules == 1, "unregister did not drain pending IRQ");
	for (int i = 0; i < 6; i++)
		memory[i].provider_owned = false;
}

static void unregister_frame(bool light, bool keep)
{
	if (light)
		video_vf_light_unreg_provider(keep);
	else
		video_vf_unreg_provider();
}

static void test_lifetime(bool light, bool scatter)
{
	reset(scatter);
	unregister_frame(light, true);
	provider_returns();
#ifdef CONFIG_AMLOGIC_MEDIA_VSYNC_RDMA
	CHECK(dispbuf_to_put_num == 0, "unregister must clear deferred queue");
	for (int i = 0; i < DISPBUF_TO_PUT_MAX; i++)
		CHECK(!dispbuf_to_put[i], "stale deferred-put entry");
	for (int i = 0; i < 3; i++)
		CHECK(!reuse(&memory[i]),
		      "AFBC scanout allocation recycled before RDMA handoff");
	/* Replacement must remain the current keeper, not the old scanout. */
	CHECK(keep_id == 4 && keep_head_id == 5 && keep_dw_id == 6,
	      "queued frame must remain the keeper after scanout pin");
	now_ms = 40;
	for (int i = 0; i < 3; i++) {
		CHECK(!reuse(&memory[i]), "scanout release predates modeled handoff");
		CHECK(memory[i].contents == 0x5a, "scanout allocation overwritten");
	}
#else
	for (int i = 0; i < 3; i++)
		CHECK(reuse(&memory[i]), "non-RDMA path gained scanout retention");
#endif
	for (int i = 3; i < 6; i++)
		CHECK(!reuse(&memory[i]), "replacement frame was not retained");
	CHECK(!warnings, "unexpected AFBC warning on tracked scanout");
	/* Model the replacement being applied, then its eventual retirement. */
	registers[0x72] = queued.compHeadAddr >> 4;
	video_keeper_new_frame_notify();
	now_ms = 1000;
	for (int i = 0; i < 6; i++)
		CHECK(reuse(&memory[i]), "keeper leaked retired memory");
}

enum lookup_case {
	TRACKED, SPARSE_QUEUE, RDMA_OFF, NO_CURRENT, LOCAL_FRAME, UNCOMPRESSED,
	WRONG_LAYER, AFBC_OFF, ZERO_HEAD, CURRENT_HEAD, UNTRACKED, DI_ONLY,
};

static void test_lookup(enum lookup_case which)
{
	struct vframe_s *found;
	ulong flags;

	reset(false);
	switch (which) {
	case TRACKED: break;
	case SPARSE_QUEUE:
		dispbuf_to_put[0] = dispbuf_to_put[1] = NULL;
		break;
	case RDMA_OFF: rdma_enabled = false; break;
	case NO_CURRENT: cur_dispbuf = NULL; break;
	case LOCAL_FRAME: cur_dispbuf = &vf_local; break;
	case UNCOMPRESSED: queued.type = 0; break;
	case WRONG_LAYER: vd_layer[0].dispbuf_mapping = &cur_pipbuf; break;
	case AFBC_OFF: registers[0x31] = 0; break;
	case ZERO_HEAD: registers[0x72] = 0; break;
	case CURRENT_HEAD: registers[0x72] = queued.compHeadAddr >> 4; break;
	case UNTRACKED: registers[0x72] = 0x55555; break;
	case DI_ONLY: dispbuf_to_put[DISPBUF_TO_PUT_MAX - 1] = NULL; break;
	}
	video_unreg_flag = 1;
	video_inirq_flag = 0;
	spin_lock_irqsave(&lock, flags);
	found = video_afbc_scanout_buffer();
	spin_unlock_irqrestore(&lock, flags);
#ifdef CONFIG_AMLOGIC_MEDIA_VSYNC_RDMA
	CHECK(found == ((which == TRACKED || which == SPARSE_QUEUE) ?
			&scanout : NULL), "incorrect hardware/queue scanout match");
	CHECK(warnings == (unsigned int)(which == UNTRACKED || which == DI_ONLY),
	      "untracked AFBC scanout must be diagnosed");
#else
	CHECK(!found && !warnings, "RDMA-disabled lookup must be inactive");
#endif
}

static void test_policies(void)
{
	for (int forced = 0; forced < 2; forced++) {
		reset(false);
		blackout = !forced;
		force_blackout = forced;
		unregister_frame(false, true);
		provider_returns();
		CHECK(!mask_calls && !layer_enabled[0],
		      "blackout must disable video without new scanout pin");
	}
	reset(false);
	registers[0x72] = 0x55555;
	unregister_frame(true, false);
	provider_returns();
	CHECK(!mask_calls && !warnings,
	      "light unregister without keep must not retain or probe scanout");
	for (int i = 0; i < 6; i++)
		CHECK(reuse(&memory[i]), "no-keep light unregister retained memory");

	for (int light = 0; light < 2; light++) {
		reset(false);
		registers[0x72] = queued.compHeadAddr >> 4;
		unregister_frame(light, true);
		provider_returns();
		CHECK(mask_calls == 3 && !warnings,
		      "already-current scanout must not get a second pin");

		reset(false);
		rdma_enabled = false;
		unregister_frame(light, true);
		provider_returns();
		CHECK(mask_calls == 3 && !warnings,
		      "runtime RDMA-off must retain only current frame");
	}
}

static void test_failed_pin(void)
{
#ifdef CONFIG_AMLOGIC_MEDIA_VSYNC_RDMA
	for (int light = 0; light < 2; light++) {
		reset(false);
		fail_scanout_keep = true;
		unregister_frame(light, true);
		provider_returns();
		CHECK(warnings == 1, "failed scanout pin must be reported");
		for (int i = 3; i < 6; i++)
			CHECK(!reuse(&memory[i]),
			      "scanout pin failure must not skip current keeper");
	}
#endif
}

int main(void)
{
	test_name = "AFBC scanout lifetime";
	for (int light = 0; light < 2; light++)
		for (int scatter = 0; scatter < 2; scatter++)
			test_lifetime(light, scatter);
	for (int which = TRACKED; which <= DI_ONLY; which++)
		test_lookup(which);
	test_policies();
	test_failed_pin();
	printf("PASS: %s, full/light reset, allocation types, gates and warnings\n",
	       test_name);
	return 0;
}
