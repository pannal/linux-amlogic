#!/usr/bin/env python3
"""Execute the production AUGE playback triggers against a model of one FRDDR.

The model follows what the G12B hardware was observed to do: turning the FRDDR
off clears CTRL0 (and CTRL2 on src_sel_ctrl chips), the read pointer reloads
from INIT_ADDR on every on/off edge, and data only moves while the destination
and interrupt settings are present. Checks: PAUSE_RELEASE continues at the
paused frame with the settings intact, and START/STOP (what Kodi uses) never
run the pause code.

This does not compile a kernel/module or emulate ALSA locking, IRQs or DMA
timing. Actual CE syntax checks are a separate verification job.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def balanced(text, opening):
    depth = 0
    token = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]', re.S)
    for match in token.finditer(text, opening):
        value = match.group()
        if value == '{':
            depth += 1
        elif value == '}':
            depth -= 1
            if depth == 0:
                return match.end()
    raise AssertionError('unclosed production block')


def function(text, name):
    pattern = re.compile(r'(?m)^[ \t]*(?:static\s+)?(?:inline\s+)?(?:unsigned\s+int|int|void|bool)\s*\n?\s*\b' + re.escape(name) + r'\s*\([^;{}]*\)\s*\{')
    matches = list(pattern.finditer(text))
    assert len(matches) == 1, (name, len(matches))
    match = matches[0]
    return text[match.start():balanced(text, match.end() - 1)]


def declaration(text, kind, name):
    match = re.search(r'\b' + kind + r'\s+' + re.escape(name) + r'\s*\{', text)
    assert match, (kind, name)
    return text[match.start():balanced(text, match.end() - 1)] + ';'


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#define EINVAL 22
#define udelay(x) ((void)0)
#define pr_debug(...) ((void)0)
#define pr_err(...) ((void)0)
#define pr_info(...) ((void)0)
#define dev_info(...) ((void)0)
#define dev_dbg(...) ((void)0)
#define dev_err(...) ((void)0)
static unsigned long gcd(unsigned long a, unsigned long b) {while (b) {unsigned long t = a % b; a = b; b = t;} return a;}
static unsigned long lcm(unsigned long a, unsigned long b) {return a / gcd(a, b) * b;}
#define roundup(x, y) ((((x) + ((y) - 1)) / (y)) * (y))
#define FIFO_BURST 8

enum {SNDRV_PCM_STREAM_PLAYBACK = 0, SNDRV_PCM_STREAM_CAPTURE = 1};
enum {SNDRV_PCM_TRIGGER_STOP = 0, SNDRV_PCM_TRIGGER_START = 1, SNDRV_PCM_TRIGGER_PAUSE_PUSH = 3,
      SNDRV_PCM_TRIGGER_PAUSE_RELEASE = 4, SNDRV_PCM_TRIGGER_SUSPEND = 5, SNDRV_PCM_TRIGGER_RESUME = 6};
struct snd_pcm_runtime {unsigned int channels; unsigned long dma_addr; size_t dma_bytes; unsigned int frame_bits;};
#define frames_to_bytes(r, f) ((f) * (r)->frame_bits / 8)
struct snd_card {int dev;};
struct snd_pcm {struct snd_card *card;};
struct snd_pcm_substream {int stream; struct snd_pcm_runtime *runtime; struct snd_pcm *pcm;};
struct snd_soc_card {int unused;};
struct snd_soc_component {struct snd_soc_card *card;};
struct snd_soc_dai {void *drvdata; struct snd_soc_component *component;};
#define snd_soc_dai_get_drvdata(d) ((d)->drvdata)
struct device;
struct toddr;
struct toddr_src_conf;
struct aml_audio_controller {int unused;};
#define SHAREBUFFER_NONE (-1)
'''

MODEL = r'''
/* One FRDDR, register numbers as in regs.h (reg_base = EE_AUDIO_FRDDR_A_CTRL0). */
static unsigned int regs[0x100], pos;
static void edge(unsigned int before, unsigned int after)
{
	if ((before ^ after) & 0x80000000u)
		pos = regs[EE_AUDIO_FRDDR_A_INIT_ADDR];
}
int aml_audiobus_write(struct aml_audio_controller *a, unsigned int reg, unsigned int val)
{
	unsigned int before = regs[reg];
	(void)a; regs[reg] = val;
	if (reg == EE_AUDIO_FRDDR_A_CTRL0)
		edge(before, val);
	return 0;
}
unsigned int aml_audiobus_read(struct aml_audio_controller *a, unsigned int reg)
{
	(void)a;
	return reg == EE_AUDIO_FRDDR_A_STATUS2 ? pos : regs[reg];
}
int aml_audiobus_update_bits(struct aml_audio_controller *a, unsigned int reg, unsigned int mask, unsigned int val)
{
	return aml_audiobus_write(a, reg, (regs[reg] & ~mask) | (val & mask));
}
static bool aml_frddr_burst_finished(struct frddr *fr) {(void)fr; return true;}
static int aml_check_and_release_sharebuffer(struct frddr *fr, enum frddr_dest dst) {(void)fr; (void)dst; return 0;}
#define aml_frddr_check(fr) ((void)(fr))
#define aml_toddr_enable(t, e) ((void)(t))
#define aml_toddr_burst_finished(t) ((void)(t), true)
'''

DRIVERS = r'''
/* Stand-ins for each driver's own state and its non-FRDDR blocks. */
struct tdm_chipinfo {bool need_mute_tdm; bool async_fifo;};
struct aml_tdm {struct frddr *fddr; struct toddr *tddr; struct aml_audio_controller *actrl; int id;
		int samesource_sel; int lane_cnt; struct tdm_chipinfo *chipinfo;};
#define vad_tdm_is_running(id) ((void)(id), 0)
#define pm_audio_is_suspend() 0
#define pm_audio_set_suspend(x) ((void)0)
#define vad_update_buffer(x) ((void)0)
#define audio_toddr_irq_enable(t, e) ((void)0)
#define aml_tdm_fifo_reset(a, s, i) ((void)0)
#define aml_aed_top_enable(f, e) ((void)0)
#define aml_tdm_enable(a, s, i, e) ((void)0)
#define tdm_sharebuffer_trigger(p, c, cmd) ((void)0)
#define aml_tdm_mute_playback(a, i, m, l) ((void)0)
#define aml_tdmout_enable_gain(i, e) ((void)0)
#define tdm_sharebuffer_mute(p, m) ((void)0)
#define locker_reset(l) ((void)(l))
#define aml_get_card_locker(c) ((void)(c), (void *)0)

struct spdif_chipinfo {bool async_fifo;};
struct aml_spdif {struct frddr *fddr; struct toddr *tddr; struct aml_audio_controller *actrl; int id;
		  int samesource_sel; bool clk_cont; struct spdif_chipinfo *chipinfo;};
#define aml_spdif_fifo_reset(a, s, i) ((void)0)
#define aml_spdif_enable(a, s, i, e) ((void)0)
#define aml_spdif_mute(a, s, i, m) ((void)0)
#define spdif_sharebuffer_trigger(p, c, cmd) ((void)0)
#define spdif_sharebuffer_mute(p, m) ((void)0)

enum toddr_src {FRATV = 1, FRHDMIRX = 2};
struct extn {struct frddr *fddr; struct toddr *tddr;};
#define toddr_src_get() FRATV
#define fratv_enable(e) ((void)0)
#define frhdmirx_enable(e) ((void)0)

struct earc {struct frddr *fddr; struct toddr *tddr; void *tx_top_map, *tx_cmdc_map, *tx_dmac_map, *rx_cmdc_map, *rx_dmac_map;
	     int tx_audio_coding_type;};
#define earctx_enable(a, b, c, d, e) ((void)0)
#define earcrx_enable(a, b, e) ((void)0)
'''

TEST_BODY = r'''
#define START 0x74800000u
#define BYTES 0x9600u
typedef int (*trigger_fn)(struct snd_pcm_substream *, int, struct snd_soc_dai *);

static struct ddr_chipinfo g12a = {.same_src_fn = true, .fifo_depth = 0x400};
static struct ddr_chipinfo sm1 = {.same_src_fn = true, .src_sel_ctrl = true, .fifo_depth = 0x400};
static struct tdm_chipinfo tdm_chip;
static struct spdif_chipinfo spdif_chip;
static struct aml_audio_controller actrl;
static struct frddr fr;
static struct snd_card card;
static struct snd_pcm pcm = {.card = &card};
static struct snd_soc_card soc_card;
static struct snd_soc_component component = {.card = &soc_card};
static struct aml_tdm tdm = {.fddr = &fr, .actrl = &actrl, .id = 2, .samesource_sel = SHAREBUFFER_NONE, .chipinfo = &tdm_chip};
static struct aml_spdif spdif = {.fddr = &fr, .actrl = &actrl, .samesource_sel = SHAREBUFFER_NONE, .chipinfo = &spdif_chip};
static struct extn ext = {.fddr = &fr};
static struct earc arc = {.fddr = &fr};

/* What hw_params + prepare program before a start. */
static void prepare(struct ddr_chipinfo *chip, enum frddr_dest dst)
{
	memset(regs, 0, sizeof(regs));
	memset(&fr, 0, sizeof(fr));
	fr.actrl = &actrl;
	fr.reg_base = EE_AUDIO_FRDDR_A_CTRL0;
	fr.chipinfo = chip;
	fr.channels = 2;
	aml_frddr_set_buf(&fr, START, START + BYTES - FIFO_BURST);
	aml_frddr_set_intrpt(&fr, 0x2580 / FIFO_BURST);
	aml_frddr_select_dst(&fr, dst);
}

static unsigned int dest_reg(void)
{
	return fr.chipinfo->src_sel_ctrl ? EE_AUDIO_FRDDR_A_CTRL2 : EE_AUDIO_FRDDR_A_CTRL0;
}

/* The DMA only moves while it is on, routed to its destination and its interrupt is set. */
static void play(unsigned int bytes)
{
	if ((regs[EE_AUDIO_FRDDR_A_CTRL0] & 0x80000000u) && (regs[dest_reg()] & 0x7) == (fr.dest & 0x7) &&
	    (regs[EE_AUDIO_FRDDR_A_CTRL0] & (0xffu << 16)))
		pos += bytes;
}

static void pause_resume(const char *name, trigger_fn trigger, void *drv, struct ddr_chipinfo *chip,
			 enum frddr_dest dst, unsigned int frame_bits, unsigned int played, unsigned int expect)
{
	struct snd_pcm_runtime rt = {.channels = 2, .dma_addr = START, .dma_bytes = BYTES, .frame_bits = frame_bits};
	struct snd_pcm_substream ss = {.stream = SNDRV_PCM_STREAM_PLAYBACK, .runtime = &rt, .pcm = &pcm};
	struct snd_soc_dai dai = {.drvdata = drv, .component = &component};
	unsigned int ctrl0, dest;

	prepare(chip, dst);
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_START, &dai) == 0);
	play(played);
	assert(pos == START + played);
	ctrl0 = regs[EE_AUDIO_FRDDR_A_CTRL0];
	dest = regs[dest_reg()];
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_PAUSE_PUSH, &dai) == 0);
	assert(!(regs[EE_AUDIO_FRDDR_A_CTRL0] & 0x80000000u));
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_PAUSE_RELEASE, &dai) == 0);
	/* Resumed where it paused, on a frame boundary, with every setting back. */
	assert(pos == START + expect);
	assert(regs[EE_AUDIO_FRDDR_A_CTRL0] == ctrl0);
	assert(regs[dest_reg()] == dest);
	play(64);
	assert(pos == START + expect + 64);
	printf("PASS %s pause/resume\n", name);
}

/* Kodi's path: STOP then prepare + START must not touch INIT_ADDR or replay saved settings. */
static void stop_start(const char *name, trigger_fn trigger, void *drv)
{
	struct snd_pcm_runtime rt = {.channels = 2, .dma_addr = START, .dma_bytes = BYTES, .frame_bits = 64};
	struct snd_pcm_substream ss = {.stream = SNDRV_PCM_STREAM_PLAYBACK, .runtime = &rt, .pcm = &pcm};
	struct snd_soc_dai dai = {.drvdata = drv, .component = &component};

	prepare(&g12a, TDMOUT_C);
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_START, &dai) == 0);
	play(0x1000);
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_STOP, &dai) == 0);
	assert(regs[EE_AUDIO_FRDDR_A_CTRL0] == 0);
	aml_frddr_set_buf(&fr, START, START + BYTES - FIFO_BURST);
	aml_frddr_set_intrpt(&fr, 0x2580 / FIFO_BURST);
	aml_frddr_select_dst(&fr, TDMOUT_C);
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_START, &dai) == 0);
	assert(regs[EE_AUDIO_FRDDR_A_INIT_ADDR] == START && pos == START);
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_SUSPEND, &dai) == 0);
	assert(trigger(&ss, SNDRV_PCM_TRIGGER_RESUME, &dai) == 0);
	assert(regs[EE_AUDIO_FRDDR_A_INIT_ADDR] == START && pos == START);
	printf("PASS %s stop/start untouched\n", name);
}

int main(void)
{
	struct {const char *name; trigger_fn fn; void *drv;} all[] = {
		{"tdm", aml_dai_tdm_trigger, &tdm}, {"spdif", aml_dai_spdif_trigger, &spdif},
		{"extn", extn_dai_trigger, &ext}, {"earc", earc_dai_trigger, &arc}};

	for (unsigned int i = 0; i < 4; i++) {
		pause_resume(all[i].name, all[i].fn, all[i].drv, &g12a, TDMOUT_C, 64, 0x2a00, 0x2a00);
		stop_start(all[i].name, all[i].fn, all[i].drv);
	}
	/* 6 x 16-bit frames are 12 bytes: resume at the next frame that is also a burst boundary. */
	pause_resume("tdm 6ch s16", aml_dai_tdm_trigger, &tdm, &g12a, TDMOUT_C, 96, 0x2a08, 0x2a18);
	/* src_sel_ctrl chips keep the destination in CTRL2, which turning off clears too. */
	pause_resume("tdm src_sel_ctrl", aml_dai_tdm_trigger, &tdm, &sm1, TDMOUT_C, 64, 0x2a00, 0x2a00);
	puts("PASS all");
	return 0;
}
'''


def production(root):
    paths = {'ddr_h': 'sound/soc/amlogic/auge/ddr_mngr.h', 'ddr': 'sound/soc/amlogic/auge/ddr_mngr.c',
             'regs': 'sound/soc/amlogic/auge/regs.h', 'tdm': 'sound/soc/amlogic/auge/tdm.c',
             'spdif': 'sound/soc/amlogic/auge/spdif.c', 'extn': 'sound/soc/amlogic/auge/extn.c',
             'earc': 'sound/soc/amlogic/auge/earc.c'}
    text = {key: (root / value).read_text() for key, value in paths.items()}
    result = [PRELUDE]
    result += re.findall(r'(?m)^#define EE_AUDIO_FRDDR_A_\w+\s+0x[0-9a-fA-F]+$', text['regs'])
    result += [declaration(text['ddr_h'], 'enum', 'ddr_num'), declaration(text['ddr_h'], 'enum', 'frddr_dest'),
               declaration(text['ddr_h'], 'struct', 'ddr_chipinfo'), declaration(text['ddr_h'], 'struct', 'frddr'), MODEL]
    for name in ['calc_frddr_address', 'aml_frddr_set_buf', 'aml_frddr_set_intrpt', 'aml_frddr_get_position',
                 'aml_frddr_enable', 'aml_frddr_select_dst', 'aml_frddr_pause_save', 'aml_frddr_pause_restore']:
        result.append(function(text['ddr'], name))
    result.append(DRIVERS)
    for key, name in [('tdm', 'aml_dai_tdm_trigger'), ('spdif', 'aml_dai_spdif_trigger'),
                      ('extn', 'extn_dai_trigger'), ('earc', 'earc_dai_trigger')]:
        result.append(function(text[key], name))
    result.append(TEST_BODY)
    return '\n\n'.join(result), {value: hashlib.sha256((root / value).read_bytes()).hexdigest() for value in paths.values()}


def mutants(code):
    """Each faulty variant must compile, then die at a runtime assertion."""
    variants = []

    def change(name, func, old, new):
        original = function(code, func)
        assert original.count(old) == 1, (name, old)
        variants.append((name, code.replace(original, original.replace(old, new), 1)))
    change('restart-at-buffer-start', 'aml_frddr_pause_restore', 'start + offset', 'start')
    change('settings-not-restored', 'aml_frddr_pause_restore', 'fr->pause_ctrl0 & ~(1u << 31)', '0')
    change('no-frame-alignment', 'aml_frddr_pause_restore', 'roundup(offset, unit)', 'offset')
    change('ctrl2-not-restored', 'aml_frddr_pause_restore', 'fr->pause_ctrl2', '0')
    return variants


def run(args):
    logs = Path(args.logs) if args.logs else Path(tempfile.mkdtemp(prefix='pcm-pause-kernel-'))
    logs.mkdir(parents=True, exist_ok=True)
    code, hashes = production(Path(args.kernel_root))
    records = []
    variants = [('positive', code)] + (mutants(code) if args.negative_controls else [])
    for name, source in variants:
        c = logs / (name + '.c')
        binary = logs / name
        c.write_text(source)
        argv = ['gcc', '-std=gnu11', '-g', '-O1', '-Wall', '-Wextra', '-Wno-unused-parameter', '-Werror=implicit-function-declaration',
                '-Werror=incompatible-pointer-types', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                # production code shifts 1 << 31 like the kernel build allows (-fno-strict-overflow)
                '-fno-strict-overflow', '-fno-sanitize=shift-base', str(c), '-o', str(binary)]
        built = subprocess.run(argv, text=True, capture_output=True)
        (logs / (name + '-compile.log')).write_text(built.stdout + built.stderr)
        assert built.returncode == 0, f'{name}: compile failure; see {logs}'
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:abort_on_error=1', UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
        observed = subprocess.run([str(binary)], text=True, capture_output=True, env=env)
        output = observed.stdout + observed.stderr
        (logs / (name + '-run.log')).write_text(output)
        if name == 'positive':
            assert observed.returncode == 0, f'positive failed: {output}'
        else:
            assert observed.returncode == -6 and 'Assertion' in output and 'runtime error:' not in output, f'{name}: did not fail by intended assertion: {output}'
        records.append(dict(name=name, compile_returncode=built.returncode, runtime_returncode=observed.returncode))
    report = dict(kernel_root=args.kernel_root, extracted_sha256=hashlib.sha256(code.encode()).hexdigest(), source_sha256=hashes, results=records,
                  caveats='Host-extracted production triggers and FRDDR functions with a register model; no kernel build, ALSA locking, IRQ or DMA timing proof.')
    (logs / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'PASS {len(records)} kernel production cases; logs {logs}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--logs')
    parser.add_argument('--negative-controls', action='store_true')
    run(parser.parse_args())
