#!/usr/bin/env python3
"""Exercise native and default OSD composition and the G12 DV size consumer with modeled MMIO."""
import argparse
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
DV = ROOT / "drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c"
OSD = ROOT / "drivers/amlogic/media/osd/osd_hw.c"

SUPPORT = r"""
#define CONFIG_AMLOGIC_MEDIA_ENHANCEMENT_DOLBYVISION
#define VSYNCOSD_WR_MPEG_REG(addr, val) write_reg(addr, val)
#define osd_log_dbg(...) ((void)0)
constexpr int VIU1 = 0;
static bool enable_vd_zorder, update_to_dv;
static void update_graphic_status() {}
static struct {
  struct { u32 background_w, background_h; } disp_info[1];
  struct { bool has_dolby_vision, osd0_sc_independ; } osd_meson_dev;
  bool blend_bypass, osd_preblend_en, enable[OSD_BLEND_LAYERS];
} osd_hw;

static bool blank, is_osd_off;
static bool is_graphics_output_off() { return blank; }
static u32 graphic_pq_target, graphic_pq_tries, applied_graphic_pq;
static u32 old_dolby_vision_graphic_max, dolby_vision_graphic_max;
static u32 graphic_pq_active() { return 0; }
static bool graphic_pq_may_change() { return false; }
"""

CHECKS = r"""
static void apply_and_check(u32 width, u32 height, u32 porch) {
  regs.erase(DOLBY_CORE2A_SWAP_CTRL2);
  apply_stb_core_settings(1, 2, false, (3840U << 16) | 2160, 0);
  assert(regs[DOLBY_CORE2A_SWAP_CTRL2] == ((width << 16) | height));
  assert((regs[DOLBY_CORE2A_SWAP_CTRL4] & 0xffff) == porch);
}

static void native_compositor(u32 width, u32 height) {
  layer_blend_reg_s blend{};
  blend.osd_blend_blend0_size = (height << 16) | width;
  set_blend_reg(&blend);
  assert(regs[VPP_OSD1_IN_SIZE] == ((height << 16) | width));
  assert(regs[DOLBY_CORE2A_SWAP_CTRL2] == ((width << 16) | height));
  assert(pending_osd_graphic_size == ((width << 16) | height));
}

static void default_compositor(u32 width, u32 height) {
  osd_hw.disp_info[VIU1] = {width, height};
  osd_setting_default_hwc();
  assert(regs[VPP_OSD1_IN_SIZE] == ((height << 16) | width));
}

int main() {
  g_vpotch = 0x20;
  osd_hw.osd_meson_dev.has_dolby_vision = true;
  default_compositor(1920, 1080);
  assert(is_graphic_changed() == 0);

  // Native 4096 sets bit 12 of the blend width.
  native_compositor(4096, 2160);
  assert(is_graphic_changed() == 2);
  apply_and_check(4096, 2160, 0x10);

  native_compositor(3840, 2160);
  assert(is_graphic_changed() == 2);
  apply_and_check(3840, 2160, 0x10);

  blank = true;
  assert(is_graphic_changed() == 1);
  default_compositor(1920, 1080);
  assert(is_graphic_changed() == 0);
  assert(osd_graphic_width == 3840 && osd_graphic_height == 2160);
  blank = false;
  assert(is_graphic_changed() == 2);
  apply_and_check(1920, 1080, 0x20);
  default_compositor(1920, 1080);
  assert(is_graphic_changed() == 0);

  // Restore native graphics, then enter the fallback with the OSD visible.
  native_compositor(3840, 2160);
  assert(is_graphic_changed() == 2);
  apply_and_check(3840, 2160, 0x10);
  default_compositor(1920, 1080);
  assert(is_graphic_changed() == 2);
  apply_and_check(1920, 1080, 0x20);

  default_compositor(1920, 0);
  assert(is_graphic_changed() == 0);
  osd_hw.osd_meson_dev.has_dolby_vision = false;
  default_compositor(1280, 720);
  assert(is_graphic_changed() == 0);
}
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--osd-source", type=Path, default=OSD)
    args = parser.parse_args()
    fixture = runpy.run_path(str(TOOLS / "test-dv-core2-lifecycle.py"))
    block = fixture["block"]
    dv = DV.read_text()
    osd = args.osd_source.read_text()
    native = block(osd, "static void set_blend_reg(struct layer_blend_reg_s *blend_reg)\n{")
    default = block(osd, "static void osd_setting_default_hwc(void)\n{")
    code, _ = fixture["generate"](dv)
    code = code.removesuffix(fixture["TESTS"])
    # Use the production G12 family predicate for Core2 register programming.
    predicate = block(dv, "static inline bool is_meson_box(void)")
    code = fixture["replace_once"](code, "static bool is_meson_box(){return false;}", "")
    code = code.replace("static bool is_meson_g12(){return true;}",
                        "static bool is_meson_g12(){return true;}\n" + predicate)
    sizes = dv[dv.index("static unsigned int osd_graphic_width ="):
               dv.index("static unsigned int dv_cert_graphic_width =")]
    code = fixture["replace_once"](code, "static u32 osd_graphic_width=1920,osd_graphic_height=1080;", sizes)
    header = (OSD.parent / "osd_reg.h").read_text()
    registers = sorted(set(re.findall(r"VSYNCOSD_WR_MPEG_REG\(\s*(\w+)", default + native)))
    definitions = [re.search(rf"^#define\s+{reg}\s+0x\w+", header, re.M)[0] for reg in registers
                   if not re.search(rf"^#define\s+{reg}\s", code, re.M)]
    osd_types = (OSD.parent / "osd.h").read_text()
    definitions.append(re.search(r"^#define OSD_BLEND_LAYERS\s+\d+", osd_types, re.M)[0])
    definitions.append(block(osd_types, "struct layer_blend_reg_s {", semicolon=True))
    code += "\n" + "\n".join(definitions) + "\n" + SUPPORT
    code += re.search(r"^#define GRAPHIC_PQ_TRIES .*", dv, re.M)[0] + "\n"
    code += block(dv, "void update_graphic_width_height(") + "\n" + default + "\n" + native + "\n"
    code += block(dv, "static int is_graphic_changed(void)") + "\n" + CHECKS
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "size.cpp"
        exe = Path(tmp) / "size"
        source.write_text(code)
        subprocess.run(["g++", "-std=c++17", "-fno-strict-aliasing", "-Werror", str(source), "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True, cwd=tmp)
    print("PASS: native 4096 and 3840 Core2 handoff, default OSD geometry, porch and blank/unblank")


if __name__ == "__main__":
    main()
