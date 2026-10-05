/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _DV5_COMPAT_ABI_H_
#define _DV5_COMPAT_ABI_H_

#include <linux/types.h>

#define DV5_NUM_IPCORE1 2
#define DV5_NUM_IPCORE2 1
#define DV5_IPCORE2_ID  DV5_NUM_IPCORE1
#define DV5_NUM_INPUTS  (DV5_NUM_IPCORE1 + DV5_NUM_IPCORE2)

#define DV5_CP_P420 0
#define DV5_CP_UYVY 1
#define DV5_CP_P444 2
#define DV5_CP_I444 3
#define DV5_CP_YUV  0
#define DV5_CP_RGB  1
#define DV5_CP_IPT  2
#define DV5_SIGNAL_RANGE_SMPTE 0
#define DV5_SIGNAL_RANGE_FULL  1
#define DV5_SIGNAL_RANGE_SDI   2
#define DV5_IN_MODE_OTT      0
#define DV5_IN_MODE_HDMI     1
#define DV5_IN_MODE_GRAPHICS 2

#define DV5_TYPE_DOVI 0
#define DV5_TYPE_ATSC 1
#define DV5_TYPE_DVB  2

#define DV5_GRAPHIC_W 1920
#define DV5_GRAPHIC_H 1080

#define DV5_CP_FLAG_CHANGE_TC  0x10
#define DV5_CP_FLAG_CHANGE_TC2 0x20
#define DV5_CP_FLAG_BLOB_CHANGE_TC  0x100
#define DV5_CP_FLAG_BLOB_CHANGE_TC2 0x200
#define DV5_CP_FLAG_CONST_TC2       0x200000

#define DV5_VIDEO_BIT_DEPTH 12

struct dovi_setting_video_s {
	struct composer_reg_ipcore comp_reg;
	struct dm_reg_ipcore1 dm_reg;
	struct dm_lut_ipcore dm_lut;
};

struct private_info_s {
	int valid;
	int src_format;
	bool el_flag;
	bool el_halfsize_flag;
	u32 video_width;
	u32 video_height;
	int set_bit_depth;
	int set_chroma_format;
	int set_yuv_range;
	int color_format;
	char *in_comp;
	int in_comp_size;
	char *in_md;
	int in_md_size;
	char *vsem_if;
	int vsem_if_size;
	int input_mode;
	int use_primaries_for_dv;
	struct hdr10_parameter *p_hdr10_param;
};

struct dv5_vsif_parameter_s {
	int lowlatency;
	int backlight_ctrl_md_present;
	int src_dm_version;
	int eff_tmax_pq;
	int dobly_vision_signal;
	int auxi_md_present;
	int l11_md_present;
	u8 auxi_runmode;
	u8 auxi_runversion;
	u8 auxi_debug0;
	u8 content_type;
	u8 intended_white_point;
	u8 l11_byte2;
	u8 l11_byte3;
	int bt2020_container;
};

struct dv5_content_info_s {
	u8 content_type_info;
	u8 white_point;
	u8 l11_byte2;
	u8 l11_byte3;
};

struct m_dovi_setting_s {
	int num_input;
	int num_video;
	int pri_input;
	int enable_multi_core1;
	int enable_debug;
	int set_priority;
	u32 dovi2hdr10_nomapping;
	u32 use_ll_flag;
	u32 ll_rgb_desired;
	u32 vout_width;
	u32 vout_height;
	u8 vsvdb_tbl[32];
	u32 vsvdb_len;
	u32 vsvdb_changed;
	u32 mode_changed;
	int dst_format;
	struct private_info_s input[DV5_NUM_INPUTS];
	int set_graphic_min_lum;
	int set_graphic_max_lum;
	int set_target_min_lum;
	int set_target_max_lum;
	struct dovi_setting_video_s core1[DV5_NUM_IPCORE1];
	struct dm_reg_ipcore2 dm_reg2;
	struct dm_reg_ipcore3 dm_reg3;
	struct dm_lut_ipcore dm_lut2;
	struct md_reg_ipcore3 md_reg3;
	struct hdr10_infoframe hdr_info;
	u32 diagnostic_enable;
	u32 diagnostic_mux_select;
	u32 dovi_ll_enable;
	struct ext_md_s ext_md;
	struct dv5_vsif_parameter_s output_vsif;
	u8 *output_ctrl_data;
	u32 output_ctrl_data_len;
	int ctrl_data_type;
	struct dv5_content_info_s content_info;
	u32 reserved[128];
};

struct dv5_funcs {
	const char *version_info;
	void *(*metadata_parser_init)(int flag);
	int (*metadata_parser_reset)(int flag);
	int (*metadata_parser_process)(char *src_rpu, int rpu_len,
				       char *dst_comp, int *comp_len,
				       char *dst_md, int *md_len, bool src_eos);
	void (*metadata_parser_release)(void);
	void *control_path;
	void *tv_control_path;
	int (*multi_control_path)(struct m_dovi_setting_s *cp_para);
	void *(*multi_mp_init)(int flag);
	int (*multi_mp_reset)(void *ctx_arg, int flag);
	int (*multi_mp_process)(void *ctx_arg, char *src_rpu, int rpu_len,
				char *dst_comp, int *comp_len,
				char *dst_md, int *md_len, bool src_eos,
				int input_format);
	void (*multi_mp_release)(void **ctx_arg);
};

void amdv_set_l11(const struct dv5_vsif_parameter_s *vsif,
		  const struct dv5_content_info_s *content_info);

#endif
