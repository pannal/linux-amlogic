#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Exercise production AFBC unregister/keeper code without video hardware."""

import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[3]
VIDEO = ROOT / "drivers/amlogic/media/video_sink/video.c"
KEEPER = ROOT / "drivers/amlogic/media/video_sink/video_keeper.c"
HERE = Path(__file__).resolve().parent


def function(source, marker):
    """Extract a complete definition, ignoring braces in comments/literals."""
    masked = re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: " " * len(match.group()),
        source,
        flags=re.DOTALL,
    )
    start = source.index(marker)
    opening = masked.index("{", start)
    depth = 1
    for pos in range(opening + 1, len(masked)):
        depth += (masked[pos] == "{") - (masked[pos] == "}")
        if not depth:
            return source[start:pos + 1] + "\n"
    raise ValueError("Unterminated production function: " + marker)


def production(video, keeper):
    definitions = []
    for source, names in (
        (keeper, (
            "static void video_keeper_update_keeper_mem(",
            "static int video_keeper_frame_keep_locked(",
            "static unsigned int vf_keep_current_locked(",
            "unsigned int vf_keep_current(",
            "void video_keeper_new_frame_notify(",
        )),
        (video, (
            "static int update_amvideo_recycle_buffer(",
            "static void release_di_buffer(",
            "static struct vframe_s *video_afbc_scanout_buffer(",
            "static void video_keep_afbc_scanout(",
            "static void video_vf_unreg_provider(",
            "static void video_vf_light_unreg_provider(",
        )),
    ):
        for name in names:
            definitions.append(function(source, name))
    return "\n".join(definitions)


def constants():
    headers = {
        "include/linux/amlogic/media/vfm/vframe.h": (
            "VIDTYPE_COMPRESS", "VIDTYPE_SCATTER", "VIDTYPE_DI_PW",
            "VIDTYPE_PRE_INTERLACE", "VFRAME_FLAG_DOUBLE_FRAM",
            "VFRAME_FLAG_DI_PW_VFM",
        ),
        "drivers/amlogic/media/video_sink/video_priv.h": (
            "DISPBUF_TO_PUT_MAX", "DEBUG_FLAG_TOGGLE_SKIP_KEEP_CURRENT",
        ),
        "include/linux/amlogic/media/codec_mm/codec_mm_keeper.h": (
            "MEM_TYPE_CODEC_MM", "MEM_TYPE_CODEC_MM_SCATTER",
        ),
    }
    definitions = []
    for path, names in headers.items():
        source = (ROOT / path).read_text()
        for name in names:
            match = re.search(r"^#define\s+" + name + r"\s+[^\n]+",
                              source, re.MULTILINE)
            if not match:
                raise ValueError("Missing production constant: " + name)
            definitions.append(match.group())
    return "\n".join(definitions) + "\n"


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError("Negative-control anchor changed: " + old)
    return source.replace(old, new, 1)


def negative_controls(code):
    full_pin = (
        "\tif (cur_dispbuf && !(blackout | force_blackout))\n"
        "\t\tvideo_keep_afbc_scanout(scanout_vf);\n"
    )
    light_pin = ("\tif (need_keep_frame) {\n"
                 "\t\tvideo_keep_afbc_scanout(scanout_vf);\n")
    early_lookup = "\tscanout_vf = video_afbc_scanout_buffer();\n"
    late_lookup = (
        "\tspin_unlock_irqrestore(&lock, flags);\n\n"
        "\tif (vd_layer[0].dispbuf_mapping"
    )
    controls = {
        "missing-full-unregister-pin": replace_once(
            code, full_pin, "\t(void)scanout_vf;\n",
        ),
        "missing-light-unregister-pin": replace_once(
            code, light_pin, "\tif (need_keep_frame) {\n"
            "\t\t(void)scanout_vf;\n",
        ),
        "lookup-after-queue-cleared": replace_once(
            replace_once(code, early_lookup, ""),
            late_lookup,
            "\tscanout_vf = video_afbc_scanout_buffer();\n" + late_lookup,
        ),
        "retain-queued-before-scanout": replace_once(
            replace_once(code, full_pin, ""),
            '\tpr_info("video_vf_unreg_provider:',
            full_pin + '\tpr_info("video_vf_unreg_provider:',
        ),
        "legacy-afbc-registers": replace_once(
            replace_once(code, "vd_layer[0].vd_afbc_reg.afbc_enable",
                         "AFBC_ENABLE"),
            "vd_layer[0].vd_afbc_reg.afbc_head_baddr", "AFBC_HEAD_BADDR",
        ),
        "immediate-keeper-release": replace_once(
            code, "codec_mm_keeper_unmask_keeper(old_id, 120);",
            "codec_mm_keeper_unmask_keeper(old_id, 0);",
        ),
        "missing-header-retention": replace_once(
            code, "cur_buf->mem_head_handle,", "NULL,",
        ),
        "ignore-light-keep-policy": replace_once(
            code, "\tif (need_keep_frame) {\n", "\tif (true) {\n",
        ),
        "missing-retention-warning": replace_once(
            code, '\t\tpr_warn("Failed to retain AFBC scanout buffer: head=%x\\n",\n'
                  '\t\t\tvf->compHeadAddr);',
            "\t\t(void)0;",
        ),
        "ignore-runtime-rdma-gate": replace_once(
            code, "!is_vsync_rdma_enable() || !cur_dispbuf",
            "!cur_dispbuf",
        ),
        "ignore-afbc-enable-gate": replace_once(
            code,
            "!(READ_VCBUS_REG(vd_layer[0].vd_afbc_reg.afbc_enable) & 0x100)",
            "false",
        ),
        "allow-di-postwrite-match": replace_once(
            code, "\t\t    !(vf->type & VIDTYPE_DI_PW) &&\n", "",
        ),
    }
    reuse_failure = "AFBC scanout allocation recycled before RDMA handoff"
    lookup_failure = "incorrect hardware/queue scanout match"
    expected_failures = {
        "missing-full-unregister-pin": reuse_failure,
        "missing-light-unregister-pin": reuse_failure,
        "lookup-after-queue-cleared": reuse_failure,
        "retain-queued-before-scanout":
            "queued frame must remain the keeper after scanout pin",
        "legacy-afbc-registers": reuse_failure,
        "immediate-keeper-release": reuse_failure,
        "missing-header-retention": reuse_failure,
        "ignore-light-keep-policy":
            "light unregister without keep must not retain or probe scanout",
        "missing-retention-warning": "failed scanout pin must be reported",
        "ignore-runtime-rdma-gate": lookup_failure,
        "ignore-afbc-enable-gate": lookup_failure,
        "allow-di-postwrite-match": reuse_failure,
    }
    return controls, expected_failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true",
                        help="also verify deliberately broken source variants")
    args = parser.parse_args()
    code = production(VIDEO.read_text(), KEEPER.read_text())
    controls, expected_failures = (
        negative_controls(code) if args.negative_controls else ({}, {})
    )
    compiler = shlex.split(os.environ.get("CC", "cc"))
    with tempfile.TemporaryDirectory(prefix="afbc-scanout-") as directory:
        out = Path(directory)
        (out / "production_constants.h").write_text(constants())
        variants = [("rdma", code, True, False),
                    ("no-rdma", code, False, False)]
        variants += [(name, mutant, True, True)
                     for name, mutant in controls.items()]
        for name, source, rdma, negative in variants:
            (out / "production_functions.h").write_text(source)
            executable = out / name
            command = compiler + [
                "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror",
                "-Wno-unused-parameter", "-Wno-unused-function",
                "-I", str(out), str(HERE / "afbc_scanout.c"),
                "-o", str(executable),
            ]
            if rdma:
                command.append("-DCONFIG_AMLOGIC_MEDIA_VSYNC_RDMA")
            subprocess.run(command, check=True)
            result = subprocess.run([str(executable)], capture_output=True,
                                    text=True, check=False)
            if negative:
                if (result.returncode != 1 or
                        "FAIL: " + expected_failures[name] not in result.stderr):
                    raise RuntimeError(
                        name + " did not fail a runtime invariant:\n" +
                        result.stdout + result.stderr
                    )
                print("PASS negative control:", name, "->",
                      result.stderr.strip())
            else:
                if result.returncode:
                    raise RuntimeError(name + ":\n" +
                                       result.stdout + result.stderr)
                print(name + ": " + result.stdout.strip())
    print("PASS: production unregister/keeper regression")
    if controls:
        print("PASS:", len(controls), "negative controls")


if __name__ == "__main__":
    main()
