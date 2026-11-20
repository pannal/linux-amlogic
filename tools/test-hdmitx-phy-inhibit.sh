#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
# Production control/register paths, pthread serialization, and recording MMIO.
# This does not execute kernel workqueues or establish sink compatibility.
set -Eeuo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
dir="$root/drivers/amlogic/media/vout/hdmitx/hdmi_tx_20"
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
awk '/^(static )?(ssize_t|void|int) (store_phy_inhibit|show_phy_inhibit|store_phy|store_debug|show_rxsense_state)\(/ {copy=1} copy {print} copy && /^}$/ {copy=0}' "$dir/hdmi_tx_main.c" > "$out/main.inc"
awk '/^(static )?(void|int) (hdmitx_set_phy|hdmitx_restore_phy|hdmitx_tmds_rxsense|hdmi_phy_suspend)\(.*\)$/ {copy=1} copy {print} copy && /^}$/ {copy=0}' "$dir/hw/hdmi_tx_hw.c" > "$out/hw.inc"
# The test records accesses; register values and VIC identifiers need distinct
# constants but are not interpreted as a hardware register map.
{ echo 'enum { HDMI_Unknown = 0,'
  grep -hoE '\b(HDMI_[A-Za-z0-9_]+|P_[A-Za-z0-9_]+|MESON_CPU_ID_[A-Za-z0-9_]+|COLOR[A-Z0-9_]+)\b' "$out/hw.inc" | sort -u | sed 's/$/,/'
  echo 'TEST_ENUM_END };'
} > "$out/constants.inc"
"${CC:-gcc}" -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter \
  -pthread -fsanitize=address,undefined -fno-omit-frame-pointer -fno-pie -no-pie \
  -I"$out" "$root/tools/hdmitx-phy-inhibit-test.c" -o "$out/test"
"$out/test"
echo 'PHY inhibit control and recorded-register tests passed.'
if [ "${1:-}" = --negative-controls ]; then
  cp "$out/main.inc" "$out/main.good"
  cp "$out/hw.inc" "$out/hw.good"
  for mutation in bypass-inhibit missing-ready; do
    cp "$out/main.good" "$out/main.inc"
    cp "$out/hw.good" "$out/hw.inc"
    case "$mutation" in
      bypass-inhibit) sed -i 's/if (hdev->phy_inhibit)/if (false)/' "$out/hw.inc" ;;
      missing-ready) sed -i 's/hdev->ready == 1/true/' "$out/main.inc" ;;
    esac
    "${CC:-gcc}" -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter \
      -pthread -I"$out" "$root/tools/hdmitx-phy-inhibit-test.c" -o "$out/mutant"
    if (ulimit -c 0; "$out/mutant") > "$out/mutant.log" 2>&1; then
      echo "Negative control unexpectedly passed: $mutation" >&2; exit 1
    fi
    grep -q 'Assertion' "$out/mutant.log"
    echo "Rejected negative control: $mutation"
  done
fi
