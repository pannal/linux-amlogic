# Amlogic Dolby Vision Pipeline — Register & Architecture Reference

*Derived from reverse-engineering the `amdolby_vision.c` kernel module on Amlogic G12B (S922X) "box2" platform, kernel 4.9.269. Assembled by Claude (Anthropic) and pannal through iterative empirical testing on real hardware, diagnostic logging, and source code analysis over 45 experimental builds during investigation of OSD color issues in VP (Video Processor) HDR10 output mode. pannal provided the hardware, test results (photos + dmesg), and critical insight at every turn; Claude wrote the code, analyzed register values, and kept (most of) the notes.*

---

## High-Level Summary

The Amlogic DV (Dolby Vision) hardware consists of three processing cores that form a compositing pipeline:

```
                    ┌──────────┐
  Decoded Video ───>│  Core 1  │──┐
                    │ (Video)  │  │    ┌──────────────┐    ┌──────────┐
                    └──────────┘  ├───>│  Compositor   │───>│  Core 3  │───> HDMI TX
                    ┌──────────┐  │    │ (Pre-blend)   │    │ (Output) │
  OSD (UI) ────────>│  Core 2  │──┘    └──────────────┘    └──────────┘
                    │  (OSD)   │
                    └──────────┘
```

**Core 1** processes video (BL = base layer, optionally EL = enhancement layer). It converts from the source color space into DV's internal IPT (Intensity/Protan/Tritan) perceptual color space.

**Core 2** processes the OSD (on-screen display / UI overlay). It performs the same conversion as Core 1 but for the graphics plane, bringing OSD pixels into the same IPT space so they can be composited with video.

**Core 3** takes the composited IPT signal and converts it to the display's required output format — HDR10 (PQ), SDR, or DV tunnel mode. It is the final stage before HDMI output.

The **compositor** (pre-blend) sits between Cores 1/2 and Core 3. It blends video and OSD in IPT space, using alpha from the OSD plane for transparency.

### Key Insight: Why This Architecture Matters

Because video and OSD are composited *before* Core 3, you cannot process them independently at the output stage. If you bypass Core 3 for video, you also bypass it for OSD. This is the fundamental constraint that drove the entire investigation.

### The Fix (VP HDR10 OSD)

For VP (Video Processor) mode with HDR10 output on non-DV displays, the DV library's tone-mapping g_2_l LUT produces incorrect OSD colors (pink/magenta). The fix: replace only the g_2_l LUT with a standard gamma 2.2 → linear degamma curve, and let the rest of the pipeline run unmodified. Core 2's a2b + c2d and Core 3's d2c + b2a are matched inverse pairs that cancel out, leaving: **g_2_l(gamma→linear) → OETF(linear→PQ) → POST(RGB→YCbCr)** — a correct SDR-to-HDR10 conversion.

---

## Register Address Spaces

Each core has its own address space, defined by bit-shifted offsets:

| Core | Offset Constant | Base Offset |
|------|----------------|-------------|
| Core 1 | `CORE1_OFFSET` | `0x01000000` (bit 24) |
| Core 1.1 | `CORE1_1_OFFSET` | `0x02000000` (bit 25) |
| Core 2A | `CORE2A_OFFSET` | `0x04000000` (bit 26) |
| Core 3 | `CORE3_OFFSET` | `0x08000000` (bit 27) |
| Core TV | `CORETV_OFFSET` | `0x10000000` (bit 28) |

Register writes use `VSYNC_WR_DV_REG()` which passes through `addr_map()` for address translation. All DV register writes must happen in vsync interrupt context.

**Header file:** `drivers/amlogic/media/enhancement/amvecm/arch/vpp_dolbyvision_regs.h`

---

## Core 1 — Video Processing

**Function:** `dolby_core1_set()` (line ~1305)

### Purpose
Processes the decoded video frame. Converts from source color space (typically BT.2020 YCbCr for HDR, BT.709 for SDR) into DV's internal IPT representation.

### Control Registers

| Register | Offset | Purpose |
|----------|--------|---------|
| `DOLBY_CORE1_REG_START` | +0x00 | Base for DM registers |
| `DOLBY_CORE1_REG_START + 1` | +0x01 | **Control/bypass flags** |
| `DOLBY_CORE1_CLKGATE_CTRL` | +0xf2 | Clock gating |
| `DOLBY_CORE1_SWAP_CTRL0..5` | +0xf3..f8 | Input data routing/swizzle |
| `DOLBY_CORE1_DMA_CTRL` | +0xf9 | LUT DMA control |
| `DOLBY_CORE1_DMA_PORT` | +0xff | LUT DMA data port |

### Bypass Flags (REG_START + 1)

Written as: `VSYNC_WR_DV_REG(DOLBY_CORE1_REG_START + 1, 2 | bypass_flag)`

| Bit | Function | VP_TM Threshold |
|-----|----------|----------------|
| 1 | CSC bypass (color space conversion) | > 2 |
| 2 | CVM bypass (color volume mapping / LUTs) | > 1 |

When both bits are set (full bypass, VP_TM > 2), Core 1 outputs raw YCbCr passthrough — no color space conversion is performed. The video data passes through to the compositor as-is.

### DM Registers (27 registers, written to REG_START + 6 + i)

| Index | Field | Purpose |
|-------|-------|---------|
| 0–1 | `s_range`, `s_range_inverse` | Signal range parameters |
| 2–4 | `frame_fmt1–3` | Frame format descriptors |
| 5–9 | `y2rgb_coeff1–5` | Input matrix: YCbCr → RGB |
| 10–12 | `y2rgb_off1–3` | Input matrix offsets |
| 13 | `eotf` | EOTF selector |
| 14–18 | `a2b_coeff1–5` | RGB → LMS conversion |
| 19–23 | `c2d_coeff1–5` | LMS → IPT conversion |
| 24 | `c2d_off` | Output offset |
| 25–26 | `active_region` | Active picture dimensions |

### Pipeline
```
Input YCbCr → y2rgb(YCbCr→RGB) → EOTF → a2b(RGB→LMS) → [CVM LUTs] → c2d(LMS→IPT) → Output IPT
```

---

## Core 2 — OSD Processing

**Function:** `dolby_core2_set()` (line ~1657)

### Purpose
Processes the OSD graphics plane. The OSD enters as 8-bit per channel data that has been format-converted by hardware. Despite being "RGB" from the application's perspective, it arrives at Core 2 in a YCbCr-like format due to hardware channel ordering (confirmed empirically — y2rgb is necessary, not a no-op).

### Control Registers

| Register | Offset | Purpose |
|----------|--------|---------|
| `DOLBY_CORE2A_REG_START` | +0x00 | Base for DM registers |
| `DOLBY_CORE2A_REG_START + 1` | +0x01 | **Control/bypass flags** |
| `DOLBY_CORE2A_REG_START + 2` | +0x02 | Programming start trigger |
| `DOLBY_CORE2A_REG_START + 3` | +0x03 | Programming done trigger |
| `DOLBY_CORE2A_CTRL` | +0x01 | Core control |
| `DOLBY_CORE2A_CLKGATE_CTRL` | +0x32 | Clock gating (used during LUT load on GXM) |
| `DOLBY_CORE2A_SWAP_CTRL0..5` | +0x33..38 | Input data routing/swizzle |
| `DOLBY_CORE2A_DMA_CTRL` | +0x39 | LUT DMA control |
| `DOLBY_CORE2A_DMA_PORT` | +0x3f | LUT DMA data port |

### SWAP_CTRL5 — Input Channel Routing

Platform-specific values:

| Platform | Value | Effect |
|----------|-------|--------|
| TXLX STB | `0xf8000000` | Channel routing for TXLX |
| G12B/box2 | `0xa8000000` | Channel routing for G12 |
| Other | `0x00000000` | Default/no swap |

This register controls how OSD pixel channels are routed into Core 2. It is critical — bypassing Core 2 via `DOLBY_PATH_CTRL` also bypasses SWAP_CTRL5, causing RGB-as-YCbCr artifacts (green/pink/teal tint).

### Bypass Flags (REG_START + 1)

Written as: `VSYNC_WR_DV_REG(DOLBY_CORE2A_REG_START + 1, 2 | bypass_flag)`

| Bit | Documented Function | Actual Behavior |
|-----|---------------------|-----------------|
| 0 | CVM bypass | **NON-FUNCTIONAL** on G12B. Setting this bit has no observable effect on the output. |

**Important:** CVM bypass is non-functional for Core 2. The only way to modify the CVM LUT behavior is to override the LUT buffer contents before the DMA write.

### DM Registers (24 registers, written to REG_START + 6 + i)

| Index | Field | Purpose |
|-------|-------|---------|
| 0–1 | `s_range`, `s_range_inverse` | Signal range |
| 2–6 | `y2rgb_coeff1–5` | Input matrix: YCbCr → RGB (5 packed regs) |
| 7–9 | `y2rgb_off1–3` | Input matrix offsets |
| 10 | `frame_fmt` | Frame format selector |
| 11 | `eotf` | EOTF selector (0 = linear / no transfer function) |
| 12–16 | `a2b_coeff1–5` | Color space conversion: RGB → LMS |
| 17–21 | `c2d_coeff1–5` | Final conversion: LMS → IPT |
| 22 | `c2d_off` | Output offset |
| 23 | `vdr_res` | Overridden with `(vsize << 16) \| hsize` |

### Pipeline
```
OSD Input → y2rgb(YCbCr→RGB) → EOTF → a2b(RGB→LMS) → [CVM LUTs: g_2_l] → c2d(LMS→IPT) → Output
```

### CVM LUT Structure

The LUT buffer `p_core2_lut[]` has 1280 entries (256 × 5 tables), written via DMA in groups of 4 with reversed byte order:

| Range | Table | Purpose | Identity Value |
|-------|-------|---------|----------------|
| [0..255] | `tm_lut_i` | Tone map interpolation | Packed 2×16-bit ramp |
| [256..511] | `tm_lut_s` | Tone map slope | `0x07ff8fff` (constant) |
| [512..767] | `sm_lut_i` | Saturation map interpolation | `0x07ff8fff` (constant) |
| [768..1023] | `sm_lut_s` | Saturation map slope | `0x07ff8fff` (constant) |
| [1024..1279] | **`g_2_l`** | **Gamma-to-linear (EOTF)** | Values from ~1,294 to ~103,813,904 |

**`g_2_l` is the critical LUT.** It performs the gamma-to-linear conversion (EOTF) that the DV pipeline needs to work in linear light. The library populates it with a DV-specific tone-mapping curve; our fix replaces it with a standard gamma 2.2 degamma.

DMA write sequence:
```c
VSYNC_WR_DV_REG(DOLBY_CORE2A_DMA_CTRL, 0x1401);  // Start DMA
for (i = 0; i < 1280; i += 4) {
    VSYNC_WR_DV_REG(DOLBY_CORE2A_DMA_PORT, p_core2_lut[i + 3]);  // Reversed
    VSYNC_WR_DV_REG(DOLBY_CORE2A_DMA_PORT, p_core2_lut[i + 2]);  // byte
    VSYNC_WR_DV_REG(DOLBY_CORE2A_DMA_PORT, p_core2_lut[i + 1]);  // order
    VSYNC_WR_DV_REG(DOLBY_CORE2A_DMA_PORT, p_core2_lut[i]);
}
```

### LUT Update Control Flags

| Flag | Effect |
|------|--------|
| `CP_FLAG_CHANGE_TC2` | Forces `set_lut = true` (LUT contents changed) |
| `CP_FLAG_CONST_TC2` | Forces `set_lut = false` (LUT contents unchanged) |
| `reset` parameter | Overrides `CP_FLAG_CONST_TC2` — if reset=1, LUT is always written |
| `force_set_lut` | Additional force flag, cleared after each frame |

**Feedback loop concern:** The library copies previous frame's DM register values via `memcpy`, so any overrides we make to p_core2_dm_regs are visible to the library on the next frame. For g_2_l, we override the buffer after the library writes it and before DMA, so it persists.

### Confirmed Library Values (VP HDR10 mode)

```
y2rgb = [00004000 400064c8 e208f403 76c24000 000e0000]
        → BT.2020 YCbCr→RGB, scale=14, input order [Cb, Cr, Y]
  offsets = [00326400 ffeb0580 003b6100]

eotf  = 00000000 (linear / no transfer function)

a2b   = [50ce2705 13ef082b 0d015f0d 11060361 000f6b97]
        → RGB→LMS, scale=15, all positive, rows sum to ~1.0

c2d   = [06660666 47480333 0656b262 05b70ce4 000ced65]
        → LMS→IPT, scale=12
        → Row 0 sums to ~1.0 (luma/Intensity)
        → Rows 1-2 sum to ~0 (chroma/Protan/Tritan)

g_2_l = [0]=1294  [64]=6824871  [128]=42545610  [192]=86739402  [255]=103813904
        → DV-specific gamma→linear with tone mapping, max ~103.8M
```

### Output

Core 2 outputs data in **[I, P, T]** channel order (channels [0, 1, 2]). This enters the compositor and then Core 3.

---

## Core 3 — Output Conversion

**Function:** `dolby_core3_set()` (line ~1862)

### Purpose
Converts the composited IPT signal into the display's required format. Supports multiple output modes. This is the final processing stage before the signal reaches the VPP (Video Post-Processor) and HDMI TX.

### Control Registers

| Register | Offset | Purpose |
|----------|--------|---------|
| `DOLBY_CORE3_REG_START` | +0x00 | Base for DM registers |
| `DOLBY_CORE3_REG_START + 1` | +0x01 | **Output mode select** (critical!) |
| `DOLBY_CORE3_REG_START + 2` | +0x02 | Programming trigger |
| `DOLBY_CORE3_REG_START + 4` | +0x04 | Additional control |
| `DOLBY_CORE3_REG_START + 5` | +0x05 | Additional control |
| `DOLBY_CORE3_CLKGATE_CTRL` | +0xf0 | Clock gating |
| `DOLBY_CORE3_SWAP_CTRL1..6` | +0xf2..f7 | Output timing/routing |
| `DOLBY_CORE3_DIAG_CTRL` | +0xf8 | Diagnostic mode |

### Output Mode (REG_START + 1) — The Most Important Register

| Value | Mode | Description |
|-------|------|-------------|
| **0x00** | IPT 12-bit 444 bypass | Passthrough — minimal processing, 12-bit clamp |
| 0x01 | IPT tunnel over RGB 8-bit | DV tunnel mode for DV-capable displays |
| **0x02** | **HDR10 output, RGB 10-bit PQ** | Full conversion pipeline — the correct mode |
| 0x03 | Deep color SDR, RGB 10-bit Gamma | SDR 10-bit output |
| 0x04 | SDR, RGB 8-bit Gamma | SDR 8-bit output |

**Mode 0x02 is the key.** It activates the full inverse pipeline: d2c → b2a → OETF → output_range → POST matrix.

### DM Registers (26 registers, written to REG_START + 6 + i)

| Index | Field | Purpose |
|-------|-------|---------|
| 0–4 | `d2c_coeff1–5` | IPT → LMS inverse conversion |
| 5–9 | `b2a_coeff1–5` | LMS → RGB inverse conversion |
| 10–11 | `eotf_param1–2` | OETF parameters (output transfer function) |
| 12 | `ipt_scale` | IPT scaling factor |
| 13–15 | `ipt_off1–3` | IPT offsets |
| 16–17 | `output_range1–2` | Output clamping range |
| 18–22 | `rgb2yuv_coeff1–5` | POST matrix: RGB → YCbCr |
| 23–25 | `rgb2yuv_off0–2` | POST matrix offsets |

### Pipeline by Mode

**Mode 0x02 (HDR10) — Full pipeline:**
```
IPT Input → d2c(IPT→LMS) → b2a(LMS→RGB) → OETF(linear→PQ) → output_range(clamp) → POST(RGB→YCbCr)
```

**Mode 0x00 (Bypass) — Minimal pipeline:**
```
Input → ipt_scale(divide) → ipt_off(add offsets) → 12-bit clamp → Output
```

In mode 0x00:
- d2c, b2a, and OETF are **all bypassed** (confirmed: identity d2c override had no effect)
- `ipt_scale` **IS active** — acts as a divider: `output = input / ipt_scale`. Zeroing it produces black.
- `ipt_off` **IS active** — adds offsets (limited-range Y + chroma centering)
- Output is clamped to 12-bit range (0–4095). Values from g_2_l in the 103M range clip to 4095.
- POST matrix behavior depends on VPP registers (see POST Matrix section)

### Confirmed Library Values (VP HDR10 mode)

```
d2c       = [0c7c7fff 7fff1a45 110df16d 042d7fff 000fa95c]
            → IPT→LMS, scale=15 (inverse of Core2's c2d)

b2a       = [b59767a2 e8fd02c6 faea3c19 fdb6ffe8 000d2260]
            → LMS→RGB, scale=13 (inverse of Core2's a2b)

eotf      = [00000000 9c400000]
            → OETF parameters (linear→PQ encoding)

ipt_scale = 0x00000db0 (3504 decimal)
ipt_off   = [0x00000100, 0x00000800, 0x00000800]
            → [256, 2048, 2048] = [Y limited-range offset, Cb center, Cr center]

range     = [0x00010000, 0x00000000]

rgb2yuv   = [4a411cc5 f05b067e 3803d7a0 cc7d3803 000ffb7e]
            → BT.2020 RGB→YCbCr POST matrix, scale=15
  offsets = [0x0100, 0x0800, 0x0800]
```

### The Cancellation Principle

Core 2's `a2b` (RGB→LMS) + `c2d` (LMS→IPT) and Core 3's `d2c` (IPT→LMS) + `b2a` (LMS→RGB) are **matched inverse pairs computed by the DV library**. When Core 3 runs in mode 0x02, these four matrices cancel out, and the net effect on OSD is:

```
OSD → y2rgb → g_2_l(gamma→linear) → [a2b → c2d → d2c → b2a = identity] → OETF(linear→PQ) → POST(RGB→YCbCr)
```

This is why replacing only g_2_l is sufficient — everything else cancels.

---

## VPP Registers — Post-Processing Control

These registers sit outside the DV cores, in the Video Post-Processor. They control routing and post-processing applied after Core 3's output.

### VPP_DOLBY_CTRL (0x1d93)

The master control register for DV-related VPP routing.

| Bits | Function | Notes |
|------|----------|-------|
| 0 | Skip PPS/dither/CM | TXLX: 1=skip, 0=enable |
| 1 | WM to VKS enable | TXLX: LL mode routing |
| 2 | Bypass gainoff to VKS | TXLX: LL mode routing |
| **6–7** | **POST matrix routing** | **3 = route through POST matrix, 0 = bypass** |

**Critical finding:** Bits 6-7 control POST matrix data *routing*, independent of `VPP_MATRIX_CTRL` bit 0 (the matrix enable). HDR10 mode sets bits 6-7 = 3 during mode changes. Even with `VPP_MATRIX_CTRL` bit 0 cleared, if bits 6-7 are set, data still flows through the POST matrix hardware.

### VPP_MATRIX_CTRL (0x1d5f)

Controls the VPP shared matrix hardware.

| Bit | Function |
|-----|----------|
| 0 | POST matrix enable (1=on, 0=off) |

The POST matrix is programmed by `enable_rgb_to_yuv_matrix_for_dvll()` which:
1. Unpacks the DV coefficient format from `p_core3_dm_regs[18..25]`
2. Converts to the VPP matrix coefficient format (different packing!)
3. Writes to `VPP_MATRIX_*` registers via `set_vpp_matrix(VPP_MATRIX_POST, ...)`

**To fully disable the POST matrix, BOTH must be cleared:**
```c
VSYNC_WR_DV_REG_BITS(VPP_DOLBY_CTRL, 0, 6, 2);   // Clear routing
VSYNC_WR_DV_REG_BITS(VPP_MATRIX_CTRL, 0, 0, 1);   // Clear enable
```

### DOLBY_PATH_CTRL (0x1a0c)

Controls which processing blocks are in the data path.

| Bit | Function |
|-----|----------|
| 0 | BL (Base Layer / Core 1) enable (0=enabled, 1=disabled) |
| 1 | EL (Enhancement Layer) enable (0=enabled, 1=disabled) |
| 2 | OSD (Core 2) enable (0=enabled, 1=disabled) |

**Warning:** Disabling OSD via bit 2 bypasses Core 2 entirely, including SWAP_CTRL5 channel routing. OSD pixels then arrive at the compositor as raw RGB without the channel reordering that SWAP_CTRL5 provides, causing severe color artifacts (green/pink/teal).

### VPP_WRAP_OSD1_MATRIX_EN_CTRL (0x3d6d)

OSD matrix enable in the VPP wrapper (G12 platform).

| Bit | Function |
|-----|----------|
| 0 | Matrix enable |

On G12/SM1/TL1, this register is typically written to 0 to disable the VPP wrapper OSD matrix in favor of the HDR2 path. Writing it to 1 activates the `VIU_OSD1_MATRIX_*` registers but **breaks alpha blending** — transparent OSD pixels become visible.

### OSD Path Registers (VIU_OSD1_*)

These are the pre-Core2 OSD processing registers. On G12, they are typically controlled by the `hdr_osd_reg` structure and written by `osd_path_enable()`.

| Register | Address | Purpose |
|----------|---------|---------|
| `VIU_OSD1_MATRIX_CTRL` | 0x1a90 | OSD matrix control (enable, mode) |
| `VIU_OSD1_MATRIX_COEF00_01` | 0x1a91 | Matrix coefficients |
| `VIU_OSD1_MATRIX_PRE_OFFSET0_1` | 0x1a98 | Pre-matrix offsets |
| `VIU_OSD1_EOTF_CTL` | 0x1ad4 | OSD EOTF (electro-optical) control |
| `VIU_OSD1_OETF_CTL` | 0x1adc | OSD OETF (opto-electronic) control |

**On G12/box2:** The `VIU_OSD1_MATRIX_*` registers have **no observable effect** unless `VPP_WRAP_OSD1_MATRIX_EN_CTRL` is set, which breaks alpha. The VPP shared matrix path (`VPP_MATRIX_CTRL`) also has no effect on the OSD when Core 2 is active. The OSD matrix is effectively handled entirely within Core 2's y2rgb stage.

---

## Coefficient Matrix Packing Format

All DV 3×3 matrices are packed into 5 registers using the same format:

```
Given matrix:
    M00 M01 M02
    M10 M11 M12
    M20 M21 M22

Packed as:
    reg[0] = (M00 << 16) | (M02 & 0xFFFF)
    reg[1] = (M12 << 16) | (M01 & 0xFFFF)
    reg[2] = (M11 << 16) | (M10 & 0xFFFF)
    reg[3] = (M20 << 16) | (M22 & 0xFFFF)
    reg[4] = (scale << 16) | (M21 & 0xFFFF)
```

- Coefficients are **signed 16-bit**. Range: -32768 to +32767.
- `0x8000` = -32768 (NOT +32768 — two's complement)
- `1.0` is represented as `2^scale`. For scale=12: 1.0 = 4096. For scale=14: 1.0 = 16384.
- The scale field occupies the upper 16 bits of reg[4].

### Unpacking Example (from `enable_rgb_to_yuv_matrix_for_dvll`)

```c
M02 = (int16_t)(reg[0] & 0xFFFF);
M00 = (int16_t)(reg[0] >> 16);
M01 = (int16_t)(reg[1] & 0xFFFF);
M12 = (int16_t)(reg[1] >> 16);
M10 = (int16_t)(reg[2] & 0xFFFF);
M11 = (int16_t)(reg[2] >> 16);
M22 = (int16_t)(reg[3] & 0xFFFF);
M20 = (int16_t)(reg[3] >> 16);
M21 = (int16_t)(reg[4] & 0xFFFF);
scale = (reg[4] >> 16) & 0x0F;
```

---

## VP_TM Bypass Levels

`xbmc_dv_vp_tm` (set from Kodi userspace) controls progressive bypass depth:

| VP_TM | Core 1 Effect | Core 2 Effect | Core 3 Effect |
|-------|---------------|---------------|---------------|
| ≤ 1 | Normal | Normal | Normal |
| > 1 | CVM bypass (bit 2) | CVM bypass (bit 0, non-functional) | Normal |
| > 2 | + CSC bypass (bit 1) → raw YCbCr output | — | Normal |
| > 3 | — | **g_2_l override** (gamma degamma) | Normal (mode 0x02 HDR10) |

At VP_TM > 3, Core 1 is fully bypassed (raw YCbCr passthrough), and Core 2 has only its g_2_l LUT replaced. Core 3 runs normally in HDR10 mode.

---

## Output Mode Interactions

### HDR10 Output (cur_dv_mode = 0x02, the working configuration)

```
Core 1: Video YCbCr → [bypassed at VP_TM>2] → raw YCbCr passthrough
Core 2: OSD → y2rgb → g_2_l(gamma→linear) → a2b(RGB→LMS) → c2d(LMS→IPT) → IPT
  Compositor: blend video + OSD in IPT space
Core 3 (mode 0x02): → d2c(IPT→LMS) → b2a(LMS→RGB) → OETF(linear→PQ) → POST(RGB→YCbCr) → HDMI
```

POST matrix is active and necessary — Core 3 mode 0x02 outputs RGB, HDMI TX expects YCbCr.

### IPT Bypass (cur_dv_mode = 0x00, the failed approach)

```
Core 3 (mode 0x00): → ipt_scale(divide by 3504) → ipt_off(add [256,2048,2048]) → 12-bit clamp
```

This mode was attempted for maximum passthrough but causes problems:
1. g_2_l output range (~103M) clips to 12-bit after ipt_scale division
2. No OETF — display gets wrong transfer function
3. OSD and video cannot be processed independently

---

## Safety Notes

### READ_VPP_REG in Vsync Context

**`READ_VPP_REG` / `VSYNC_RD_DV_REG` is NOT safe in vsync interrupt context for VPP registers.** Using it causes hard crashes and reboots. Always use buffer values (`p_core2_dm_regs[]`, `p_core3_dm_regs[]`) for diagnostics instead. Write-only operations (`VSYNC_WR_DV_REG`) are safe.

### Library Feedback Loop

The DV library copies previous frame's register values via `memcpy` (controlled by `stb_core2_const_flag` / `CP_FLAG_CONST_TC2`). Overrides to `p_core2_dm_regs[]` are visible to the library on subsequent frames. For LUT overrides, write to the buffer between library population and DMA write.

### DM Register Change Detection

DM registers are only written to hardware when `reset` is true OR the value differs from `last_dm[i]`. If you override a register to the same value the library set, the write may be skipped on subsequent frames. The `reset` flag forces all writes.

---

## Appendix: Approaches Tried and Results

During the investigation, 45 experimental builds were tested across multiple sessions. Key learnings:

| # | Approach | Result | Learning |
|---|----------|--------|----------|
| 1 | DOLBY_PATH_CTRL bypass (disable Core 2) | Green/pink/teal | Bypasses SWAP_CTRL5, breaks channel routing |
| 6 | POST matrix disable (VPP_MATRIX_CTRL only) | No change | VPP_DOLBY_CTRL routing still active |
| 7 | Zero Core3 d2c + ipt_scale | Black screen | ipt_scale=0 kills output |
| 12 | CVM bypass re-assertion | No change | CVM bypass non-functional on Core 2 |
| 13 | LUT dump | Revealed g_2_l | Only non-identity LUT in CVM |
| 14-18 | Various g_2_l curves | All yellow | POST matrix cross-contamination dominated |
| 19 | VPP_DOLBY_CTRL fix + wrong Cb/Cr order | Teal | POST matrix finally disabled; revealed channel order |
| 20 | VPP_DOLBY_CTRL fix + correct order | Yellow/washed | POST gone, but PQ-per-channel distorts colors |
| **24** | **Core3 mode 0x02 + gamma degamma g_2_l** | **Correct colors** | **Let the pipeline do its job** |

The root cause was the DV library's g_2_l LUT, which contains a DV-specific tone-mapping curve unsuitable for non-DV displays. Replacing it with a standard gamma 2.2 → linear degamma while keeping all other pipeline elements at library values produces correct HDR10 output.
