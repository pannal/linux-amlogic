#!/usr/bin/env python3
"""Execute selected production kernel map/HDMI functions with host service stand-ins.

This does not compile a kernel/module, emulate interrupts/ALSA locking, or prove
receiver delivery. Actual CE syntax checks are a separate verification job.
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
    pattern = re.compile(r'(?m)^[ \t]*(?:static\s+)?(?:inline\s+)?(?:unsigned\s+(?:int|char)|(?:struct|enum)\s+\w+|int|void|bool)\s*(?:\*\s*)*\b' + re.escape(name) + r'\s*\([^;{}]*\)\s*\{')
    matches = list(pattern.finditer(text))
    assert len(matches) == 1, (name, len(matches))
    match = matches[0]
    return text[match.start():balanced(text, match.end() - 1)]


def declaration(text, kind, name):
    match = re.search(r'\b' + kind + r'\s+' + re.escape(name) + r'\s*\{', text)
    assert match, name
    return text[match.start():balanced(text, match.end() - 1)] + ';'


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <sound/asound.h>
#define __user
#define __maybe_unused
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define GFP_KERNEL 0
#define module_param(...)
#define MODULE_PARM_DESC(...)
#define pr_info(...) ((void)0)
#define pr_err(...) ((void)0)
#define pr_debug(...) ((void)0)
#define dev_warn(...) ((void)0)
#define AUD ""
#define HW ""
#define DTS_HD_MA (1<<8)
#define SNDRV_CTL_TLVT_CONTAINER 0
#define SNDRV_CTL_TLVT_CHMAP_FIXED 0x101
#define SNDRV_PCM_STREAM_PLAYBACK 0
#define SNDRV_PCM_STREAM_CAPTURE 1
#define put_user(v,p) (*(p)=(v),0)
struct mutex { int held; };
static int interrupt_lock;
static void mutex_init(struct mutex *m) { m->held=0; }
static void mutex_lock(struct mutex *m) { assert(!m->held);m->held=1; }
static int mutex_lock_interruptible(struct mutex *m) { if(interrupt_lock) return 1;mutex_lock(m);return 0; }
static void mutex_unlock(struct mutex *m) { assert(m->held);m->held=0; }
static int fail_alloc, alloc_live;
static void *kzalloc(size_t n,int flags) { (void)flags;if(fail_alloc && --fail_alloc==0) return NULL;void *p=calloc(1,n);if(p) alloc_live++;return p; }
static void *kcalloc(size_t n,size_t size,int flags) { return kzalloc(n*size,flags); }
static void kfree(void *p) { if(p) {alloc_live--;free(p);} }
struct snd_pcm_runtime { unsigned int channels,rate,sample_bits;int format; struct {int state;} *status;void *private_data; };
struct snd_pcm;
struct snd_pcm_substream { struct snd_pcm *pcm;struct snd_pcm_runtime *runtime;int stream,number;struct snd_pcm_substream *next;void *private_data; };
struct snd_kcontrol { unsigned int count;struct {unsigned int access;} *vd;int (*get)(struct snd_kcontrol*,struct snd_ctl_elem_value*);int (*put)(struct snd_kcontrol*,struct snd_ctl_elem_value*);struct {int (*c)(struct snd_kcontrol*,int,unsigned int,unsigned int*);} tlv;void *private_data;void (*private_free)(struct snd_kcontrol*); };
struct snd_pcm { struct {struct snd_kcontrol *chmap_kctl;unsigned int substream_count;struct snd_pcm_substream *substream;} streams[2];void *card; };
struct snd_pcm_chmap { struct snd_pcm *pcm;int stream;struct snd_kcontrol *kctl;void *private_data; };
struct device {void *driver_data;};
struct snd_soc_pcm_runtime {struct snd_pcm *pcm;struct {struct device *dev;} *platform;};
static void *snd_kcontrol_chip(struct snd_kcontrol *k) { return k->private_data; }
static unsigned int snd_ctl_get_ioffidx(struct snd_kcontrol *k,const void *id) { (void)k;return ((const struct snd_ctl_elem_id*)id)->index; }
static struct snd_pcm_substream *snd_pcm_chmap_substream(struct snd_pcm_chmap *info,unsigned int index) { struct snd_pcm_substream *s=info->pcm->streams[info->stream].substream;for(;s;s=s->next)if((unsigned)s->number==index)return s;return NULL; }
static void base_private_free(struct snd_kcontrol *k) {struct snd_pcm_chmap *info=snd_kcontrol_chip(k);info->pcm->streams[info->stream].chmap_kctl=NULL;kfree(info);}
static int add_error;
static int snd_pcm_add_chmap_ctls(struct snd_pcm *pcm,int stream,const void *map,int max_channels,unsigned long private_value,struct snd_pcm_chmap **out) {
 (void)map;(void)max_channels;(void)private_value;if(add_error)return add_error;
 struct snd_pcm_chmap *info=kzalloc(sizeof(*info),0);if(!info)return -ENOMEM;
 struct snd_kcontrol *k=kzalloc(sizeof(*k),0);if(!k){kfree(info);return -ENOMEM;}
 k->count=pcm->streams[stream].substream_count;k->vd=kcalloc(k->count,sizeof(*k->vd),0);if(!k->vd){kfree(info);kfree(k);return -ENOMEM;}
 info->pcm=pcm;info->stream=stream;info->kctl=k;k->private_data=info;k->private_free=base_private_free;pcm->streams[stream].chmap_kctl=k;*out=info;return 0;
}
static int snd_ctl_remove(void *card,struct snd_kcontrol *k) { (void)card;k->private_free(k);kfree(k->vd);kfree(k);return 0; }
static unsigned int regs[64], writes[64];
#define HDMITX_DWC_FC_AUDSCHNLS0 0
#define HDMITX_DWC_FC_AUDSCHNLS3 3
#define HDMITX_DWC_FC_AUDSCHNLS4 4
#define HDMITX_DWC_FC_AUDSCHNLS5 5
#define HDMITX_DWC_FC_AUDSCHNLS6 6
#define HDMITX_DWC_FC_AUDSCHNLS7 7
#define HDMITX_DWC_FC_AUDSCHNLS8 8
#define HDMITX_DWC_FC_AUDSV 9
#define HDMITX_DWC_FC_AUDICONF0 10
#define HDMITX_DWC_FC_AUDICONF1 11
#define HDMITX_DWC_FC_AUDICONF2 12
#define HDMITX_DWC_FC_AUDICONF3 13
static void hdmitx_wr_reg(unsigned int r,unsigned int v) { assert(r<ARRAY_SIZE(regs));regs[r]=v;writes[r]++; }
static void hdmitx_set_reg_bits(unsigned int r,unsigned int v,unsigned int start,unsigned int len) {unsigned int mask=((1u<<len)-1)<<start;assert(r<ARRAY_SIZE(regs));regs[r]=(regs[r]&~mask)|((v<<start)&mask);writes[r]++;}
struct hdmitx_dev {unsigned int aud_output_ch;int audio_param_update_flag;};

#define dev_err(...) ((void)0)
#define SNDRV_DMA_TYPE_DEV 0
#define TDM_BUFFER_BYTES (512 * 1024 * 2)
struct aml_tdm {struct device *dev;void *actrl;int id;void *fddr,*tddr;int i2s2hdmitx;struct {int separate_tohdmitx_en;} *chipinfo;};
static int aml_tdm_hardware, lifecycle_open,lifecycle_close, lifecycle_params,lifecycle_free;
static void *dev_get_drvdata(struct device *dev) { return dev->driver_data; }
static void snd_soc_set_runtime_hwparams(struct snd_pcm_substream *s,const void *hw) {(void)s;(void)hw;}
static int snd_pcm_lib_preallocate_pages(struct snd_pcm_substream *s,int type,struct device *dev,unsigned int a,unsigned int b) {(void)s;(void)type;(void)dev;(void)a;(void)b;return 0;}
static void snd_pcm_lib_preallocate_free(struct snd_pcm_substream *s) {(void)s;}
static int get_aed_dst(void) {return 0;}
static bool is_aed_reserve_frddr(void) {return false;}
static void aml_tdm_ddr_isr(void) {}
static void *aml_audio_register_frddr(struct device *d,void *a,void *fn,struct snd_pcm_substream *s,bool reserve) {(void)d;(void)a;(void)fn;(void)s;(void)reserve;lifecycle_open++;return &lifecycle_open;}
static void *aml_audio_register_toddr(struct device *d,void *a,void *fn,struct snd_pcm_substream *s) {(void)d;(void)a;(void)fn;(void)s;return &lifecycle_open;}
static void aml_audio_unregister_frddr(struct device *d,struct snd_pcm_substream *s) {(void)d;(void)s;lifecycle_close++;}
static void aml_audio_unregister_toddr(struct device *d,struct snd_pcm_substream *s) {(void)d;(void)s;}
static unsigned int params_buffer_bytes(struct snd_pcm_hw_params *p) {(void)p;return 4096;}
static int snd_pcm_lib_malloc_pages(struct snd_pcm_substream *s,unsigned int n) {(void)s;(void)n;lifecycle_params++;return 0;}
static int snd_pcm_lib_free_pages(struct snd_pcm_substream *s) {(void)s;lifecycle_free++;return 0;}

'''

# Runtime test body is appended once all extracted definitions are available.
TEST_BODY = r'''
static struct snd_pcm pcm;
static struct snd_pcm_substream streams[2];
static struct snd_pcm_runtime runtimes[2];
static struct {int state;} states[2];
static struct snd_soc_pcm_runtime rtd;
static const int stereo[2]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR};
static const int three1[4]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR,SNDRV_CHMAP_LFE,SNDRV_CHMAP_FC};
static const int six0[8]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR,SNDRV_CHMAP_NA,SNDRV_CHMAP_FC,SNDRV_CHMAP_RL,SNDRV_CHMAP_RR,SNDRV_CHMAP_RC,SNDRV_CHMAP_NA};
static const int six1[8]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR,SNDRV_CHMAP_LFE,SNDRV_CHMAP_FC,SNDRV_CHMAP_RL,SNDRV_CHMAP_RR,SNDRV_CHMAP_RC,SNDRV_CHMAP_NA};
static const int seven1[8]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR,SNDRV_CHMAP_LFE,SNDRV_CHMAP_FC,SNDRV_CHMAP_RL,SNDRV_CHMAP_RR,SNDRV_CHMAP_RLC,SNDRV_CHMAP_RRC};
static const int five1[6]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR,SNDRV_CHMAP_LFE,SNDRV_CHMAP_FC,SNDRV_CHMAP_RL,SNDRV_CHMAP_RR};
static const int four0[6]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR,SNDRV_CHMAP_NA,SNDRV_CHMAP_NA,SNDRV_CHMAP_RL,SNDRV_CHMAP_RR};
static const int five0[6]={SNDRV_CHMAP_FL,SNDRV_CHMAP_FR,SNDRV_CHMAP_NA,SNDRV_CHMAP_FC,SNDRV_CHMAP_RL,SNDRV_CHMAP_RR};
static struct snd_kcontrol *ctl(void) { return pcm.streams[0].chmap_kctl; }
static int put_map(unsigned int index,const int *map,unsigned int n) {struct snd_ctl_elem_value value={0};value.id.index=index;for(unsigned int i=0;i<n;i++)value.value.integer.value[i]=map[i];return ctl()->put(ctl(),&value);}
static void expect_map(unsigned int index,const int *map,unsigned int n) {struct snd_ctl_elem_value value;memset(&value,0x5a,sizeof(value));value.id.index=index;assert(ctl()->get(ctl(),&value)==0);for(unsigned int i=0;i<8;i++)assert(value.value.integer.value[i]==(i<n?map[i]:0));}
static void expect_unresolved(unsigned int index) {expect_map(index,NULL,0);struct aud_para a={.layout=0x13,.layout_valid=true};aml_dai_tdm_chmap_layout(&streams[index],&a);assert(!a.layout_valid&&a.layout==0);}
static void expect_ca(unsigned int index,unsigned int ca) {struct aud_para a={0};aml_dai_tdm_chmap_layout(&streams[index],&a);assert(a.layout_valid&&a.layout==ca);unsigned int old=notifications;prepare_hdmi(&streams[index]);assert(notifications==old+1&&notified_event==AOUT_EVENT_IEC_60958_PCM&&notified.layout_valid&&notified.layout==ca);assert(transport_slots==runtimes[index].channels);assert(lane_mask==(transport_slots>6?0xf:(transport_slots>4?7:(transport_slots>2?3:1))));}
static void setup(void) {memset(&pcm,0,sizeof(pcm));memset(streams,0,sizeof(streams));memset(runtimes,0,sizeof(runtimes));rtd.pcm=&pcm;pcm.streams[0].substream_count=2;pcm.streams[0].substream=&streams[0];for(int i=0;i<2;i++){streams[i].pcm=&pcm;streams[i].number=i;streams[i].runtime=&runtimes[i];runtimes[i].channels=8;runtimes[i].status=(void*)&states[i];states[i].state=SNDRV_PCM_STATE_PREPARED;}streams[0].next=&streams[1];}
static void free_ctl(void) {if(ctl())snd_ctl_remove(pcm.card,ctl());assert(!ctl());assert(alloc_live==0);}
static void test_registration(void) {setup();pcm.streams[0].substream_count=0;assert(aml_tdm_pcm_new(&rtd)==0);assert(!ctl());pcm.streams[0].substream_count=2;add_error=-EIO;assert(aml_tdm_pcm_new(&rtd)==-EIO);assert(!ctl()&&alloc_live==0);add_error=0;for(int allocation=1;allocation<=4;allocation++){fail_alloc=allocation;assert(aml_tdm_pcm_new(&rtd)==-ENOMEM);assert(!ctl()&&alloc_live==0);}assert(aml_tdm_pcm_new(&rtd)==0);assert(ctl()&&ctl()->count==2&&ctl()->get&&ctl()->put&&ctl()->tlv.c);expect_unresolved(0);expect_unresolved(1);free_ctl();}
static void check_tlv(int expected) {unsigned int values[160]={0};assert(ctl()->tlv.c(ctl(),0,sizeof(values),values)==0);assert(values[0]==SNDRV_CTL_TLVT_CONTAINER);unsigned int *p=values+2,*end=(unsigned int*)((char*)(values+2)+values[1]);int rows=0;for(;p<end;){assert(p[0]==SNDRV_CTL_TLVT_CHMAP_FIXED);unsigned int n=p[1]/sizeof(unsigned int);assert(n==2||n==4||n==6||n==8);for(unsigned int i=0;i<n;i++)assert(p[2+i]==(unsigned)channel_allocations[rows].speakers[7-i]);if(rows==4||rows==5){assert(n==8);for(unsigned int i=0;i<8;i++)assert(p[2+i]==(unsigned)(rows==4?six0[i]:six1[i]));}p+=2+n;rows++;}assert(p==end&&rows==expected);assert(ctl()->tlv.c(ctl(),0,4,values)==-ENOMEM);}
static void test_receipts(void) {setup();assert(aml_tdm_pcm_new(&rtd)==0);extra_pcm_layouts=0;check_tlv(6);assert(put_map(0,six0,8)>=0);expect_map(0,six0,8);expect_ca(0,0x0e);expect_unresolved(1);assert(put_map(1,six1,8)>=0);expect_ca(1,0x0f);expect_ca(0,0x0e);
 /* Repeated prepare/XRUN snapshots retain the current receipt. */
 for(int i=0;i<3;i++)expect_ca(0,0x0e);
 assert(put_map(0,six1,8)>=0);expect_map(0,six1,8);expect_ca(0,0x0f);assert(put_map(0,seven1,8)>=0);expect_map(0,seven1,8);expect_ca(0,0x13);
 int bad[8];memcpy(bad,six1,sizeof(bad));bad[6]=SNDRV_CHMAP_LFE;assert(put_map(0,bad,8)<0);expect_unresolved(0);
 assert(put_map(0,six0,8)>=0);runtimes[0].channels=6;expect_unresolved(0);assert(put_map(0,six0,8)<0);expect_unresolved(0);
 assert(put_map(0,five1,6)>=0);expect_map(0,five1,6);expect_ca(0,0x0b);
 assert(put_map(0,four0,6)<0);expect_unresolved(0);extra_pcm_layouts=1;check_tlv(8);assert(put_map(0,four0,6)>=0);expect_map(0,four0,6);expect_ca(0,0x08);assert(put_map(0,five0,6)>=0);expect_ca(0,0x0a);extra_pcm_layouts=0;expect_unresolved(0);
 runtimes[0].channels=2;assert(put_map(0,stereo,2)>=0);expect_map(0,stereo,2);expect_ca(0,0x00);runtimes[0].channels=4;assert(put_map(0,three1,4)>=0);expect_map(0,three1,4);expect_ca(0,0x03);runtimes[0].channels=8;assert(put_map(0,six1,8)>=0);aml_dai_tdm_chmap_reset(&streams[0]);expect_unresolved(0);expect_ca(1,0x0f);
 assert(put_map(0,six0,8)>=0);struct snd_pcm_runtime reruntime=runtimes[0];streams[0].runtime=&reruntime;expect_unresolved(0);streams[0].runtime=&runtimes[0];struct snd_pcm_substream replacement=streams[0];pcm.streams[0].substream=&replacement;expect_map(0,NULL,0);struct aud_para stolen={0};aml_dai_tdm_chmap_layout(&replacement,&stolen);assert(!stolen.layout_valid);pcm.streams[0].substream=&streams[0];aml_dai_tdm_chmap_reset(&streams[0]);expect_unresolved(0);
 streams[0].runtime=NULL;assert(put_map(0,six1,8)<0);expect_unresolved(0);streams[0].runtime=&runtimes[0];states[0].state=SNDRV_PCM_STATE_RUNNING;assert(put_map(0,six1,8)<0);expect_unresolved(0);states[0].state=SNDRV_PCM_STATE_SETUP;assert(put_map(0,six0,8)>=0);expect_ca(0,0x0e);assert(put_map(2,six1,8)<0);
 interrupt_lock=1;assert(put_map(0,six1,8)==-EINTR);interrupt_lock=0;expect_ca(0,0x0e);free_ctl();}

static void test_lifecycle(void) {setup();struct device d={0};struct aml_tdm t={.dev=&d};d.driver_data=&t;struct {struct device *dev;} platform={.dev=&d};rtd.platform=(void*)&platform;streams[0].private_data=&rtd;assert(aml_tdm_pcm_new(&rtd)==0);
 /* Fresh PCM open must retire a same-count receipt from a prior owner. */
 assert(put_map(0,six0,8)>=0);assert(aml_tdm_open(&streams[0])==0);expect_unresolved(0);assert(lifecycle_open==1);
 assert(put_map(0,six1,8)>=0);struct snd_pcm_hw_params p={0};assert(aml_tdm_hw_params(&streams[0],&p)==0);expect_unresolved(0);assert(lifecycle_params==1);
 assert(put_map(0,six0,8)>=0);assert(aml_tdm_hw_free(&streams[0])==0);expect_unresolved(0);assert(lifecycle_free==1);
 assert(put_map(0,six1,8)>=0);assert(aml_tdm_close(&streams[0])==0);expect_unresolved(0);assert(lifecycle_close==1);
 assert(aml_tdm_open(&streams[0])==0);expect_unresolved(0);assert(put_map(0,seven1,8)>=0);expect_ca(0,0x13);assert(aml_tdm_close(&streams[0])==0);expect_unresolved(0);free_ctl();}

static struct hdmitx_audpara audio(unsigned int ca,bool valid) {struct hdmitx_audpara p={0};p.type=CT_PCM;p.channel_num=CC_8CH;p.sample_rate=FS_48K;p.sample_size=SS_24BITS;p.layout=ca;p.layout_valid=valid;return p;}
static unsigned int inactive_bits(const int *map) {unsigned int invalid=0,active=0;for(unsigned int slot=0;slot<8;slot++){if(map[slot]==SNDRV_CHMAP_NA)invalid|=1u<<((slot/2)+(slot%2?4:0));else active++;}assert(active==6||active==7||active==8);assert(!(invalid&(1u<<3)));return invalid;}
static void verify_hdmi(struct hdmitx_audpara *p,unsigned int cc,unsigned int ca,unsigned int invalid) {struct hdmitx_dev h={.aud_output_ch=0x8f};unsigned char db[32]={0},cs[48]={0};memset(regs,0,sizeof(regs));memset(writes,0,sizeof(writes));set_aud_chnls(&h,p);set_aud_info_pkt(&h,p);hdmi_tx_construct_aud_packet(p,db,cs,CC_8CH);assert(((regs[HDMITX_DWC_FC_AUDICONF0]>>4)&7)==cc);assert(regs[HDMITX_DWC_FC_AUDICONF2]==ca);assert(regs[HDMITX_DWC_FC_AUDSV]==invalid);assert((db[0]&7)==cc);assert(db[3]==ca);assert(h.aud_output_ch==0x8f);}
static void test_hdmi(void) {struct hdmitx_audpara p=audio(0x0e,true);verify_hdmi(&p,5,0x0e,inactive_bits(six0));p.layout=0x0f;verify_hdmi(&p,6,0x0f,inactive_bits(six1));p.layout=0x13;verify_hdmi(&p,7,0x13,0);p.layout=0x0e;p.layout_valid=false;verify_hdmi(&p,7,0x13,0);p.layout_valid=true;p.channel_num=CC_6CH;struct hdmitx_dev h={.aud_output_ch=0x6f};memset(regs,0,sizeof(regs));set_aud_chnls(&h,&p);assert(regs[HDMITX_DWC_FC_AUDSV]==0);p=audio(0x0e,true);set_aud_chnls(&h,&p);assert(regs[HDMITX_DWC_FC_AUDSV]==inactive_bits(six0));p.type=CT_MAT;set_aud_chnls(&h,&p);set_aud_info_pkt(&h,&p);assert(regs[HDMITX_DWC_FC_AUDSV]==0&&regs[HDMITX_DWC_FC_AUDICONF2]==0x13&&((regs[10]>>4)&7)==7);p.type=CT_DOLBY_D;set_aud_chnls(&h,&p);set_aud_info_pkt(&h,&p);assert(regs[9]==0&&regs[12]==0&&((regs[10]>>4)&7)==1);unsigned char db[32]={0},cs[48]={0};hdmi_tx_construct_aud_packet(&p,db,cs,CC_8CH);assert(db[3]==0&&cs[0]==2&&cs[27]==0x1e);p=audio(0,false);p.channel_num=CC_2CH;h.aud_output_ch=0;set_aud_chnls(&h,&p);set_aud_info_pkt(&h,&p);assert(regs[9]==0&&regs[12]==0&&((regs[10]>>4)&7)==1);}
static void test_forced_output(void) {const unsigned int forced[3]={2,4,6},ca[3]={0,3,0x0b};for(unsigned int layout=0x0e;layout<=0x0f;layout++){struct hdmitx_audpara p=audio(layout,true);for(unsigned int i=0;i<3;i++){struct hdmitx_dev h={.aud_output_ch=(forced[i]<<4)|((1u<<(forced[i]/2))-1)};set_aud_info_pkt(&h,&p);assert(((regs[10]>>4)&7)==forced[i]-1&&regs[12]==ca[i]);}unsigned char db[32]={0},cs[48]={0};hdmi_tx_construct_aud_packet(&p,db,cs,CC_6CH);assert(db[3]==0x0b&&(db[0]&7)==CC_8CH);}}
static void test_notifier(void) {struct hdmitx_dev h={0};struct hdmitx_audpara p=audio(0x13,false);struct aud_para a={0};a.layout=0x0e;a.layout_valid=true;notify_receipt(&h,&p,&a);assert(h.audio_param_update_flag&&p.layout==0x0e&&p.layout_valid);h.audio_param_update_flag=0;notify_receipt(&h,&p,&a);assert(!h.audio_param_update_flag);a.layout=0x0f;notify_receipt(&h,&p,&a);assert(h.audio_param_update_flag&&p.layout==0x0f);h.audio_param_update_flag=0;a.layout_valid=false;notify_receipt(&h,&p,&a);assert(h.audio_param_update_flag&&!p.layout_valid);}
int main(void) {test_registration();test_receipts();test_lifecycle();test_hdmi();test_forced_output();test_notifier();puts("PASS native maps, receipts, HDMI packet/register/reset and notifier");return 0;}
'''


def production(root):
    paths = {'tdm': 'sound/soc/amlogic/auge/tdm.c', 'state': 'sound/soc/amlogic/auge/tdm_hw.h', 'audio': 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hdmi_tx_audio.c', 'hw': 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hw/hdmi_tx_hw.c', 'main': 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hdmi_tx_main.c', 'helper': 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20/hdmi_tx_audio_layout.h', 'common': 'include/linux/amlogic/media/vout/hdmi_tx/hdmi_common.h', 'info': 'include/linux/amlogic/media/vout/hdmi_tx/hdmi_info_global.h', 'ext': 'include/linux/amlogic/media/vout/hdmi_tx/hdmi_tx_ext.h'}
    text = {key: (root / value).read_text() for key, value in paths.items()}
    result = [PRELUDE]
    for name in ['hdmi_audio_type', 'hdmi_audio_chnnum', 'hdmi_audio_fs', 'hdmi_audio_sampsize']:
        result.append(declaration(text['common'], 'enum', name))
    result += [declaration(text['info'], 'struct', 'hdmitx_audpara'), declaration(text['ext'], 'struct', 'aud_para'), declaration(text['state'], 'struct', 'aml_chmap')]
    result.append(text['tdm'][text['tdm'].index('struct channel_speaker_allocation {'): text['tdm'].index('static void dump_pcm_setting')])
    # Function dependencies are selected from actual definitions, skipping prototypes.
    for name in ['aml_dai_tdm_chmap_state', 'aml_dai_tdm_chmap_kctrl_get', 'aml_dai_tdm_chmap_reset', 'aml_dai_tdm_chmap_layout', 'aml_dai_tdm_chmap_ctl_tlv', 'aml_dai_tdm_chmap_ctl_get', 'aml_dai_tdm_chmap_ctl_put', 'aml_dai_tdm_chmap_private_free', 'aml_tdm_pcm_new']:
        result.append(function(text['tdm'], name))
    for name in ['aml_tdm_open', 'aml_tdm_close', 'aml_tdm_hw_params', 'aml_tdm_hw_free']:
        result.append(function(text['tdm'], name))
    result.append(re.sub(r'^\s*#(?:ifndef|define|endif|include).*$', '', text['helper'], flags=re.M))
    for key, start, end in [('audio', 'static const unsigned char channel_status_freq', 'static void hdmi_tx_construct_aud_packet'), ('hw', 'static unsigned char aud_csb_sampfreq', 'static void set_aud_chnls')]:
        result.append(text[key][text[key].index(start):text[key].index(end)])
    result += ['#define GET_OUTCHN_NO(a) (((a)>>4)&0xf)\n#define GET_OUTCHN_MSK(a) ((a)&0xf)']
    result += [function(text['audio'], 'hdmi_tx_construct_aud_packet'), function(text['hw'], 'set_aud_chnls'), function(text['hw'], 'set_aud_info_pkt')]
    m = re.search(r'if\s*\(audio_param->layout\s*!=\s*aud_param->layout[^{}]+\{', text['main'])
    assert m, 'notifier receipt update'
    result.append('static void notify_receipt(struct hdmitx_dev *hdev,struct hdmitx_audpara *audio_param,struct aud_para *aud_param) {\n' + text['main'][m.start():balanced(text['main'], m.end()-1)] + '\n}')
    # Execute the target HDMI branch verbatim, with observable hardware/notifier services.
    prepare = function(text['tdm'], 'aml_dai_tdm_prepare')
    match = re.search(r'if\s*\(p_tdm->i2s2hdmitx\)\s*\{', prepare)
    assert match
    branch = prepare[match.start():balanced(prepare, match.end()-1)]
    result += [r'''
struct aud_para notified;
static unsigned int notified_event,transport_slots,lane_mask,notifications;
#define AOUT_EVENT_IEC_60958_PCM 1
static void i2s_to_hdmitx_ctrl(int separated,int id) {(void)separated;(void)id;}
static void hdmitx_ext_set_i2s_mask(unsigned int n,unsigned int mask) {transport_slots=n;lane_mask=mask;}
static int aout_notifier_call_chain(unsigned int event,void *payload) {notified_event=event;notified=*(struct aud_para*)payload;notifications++;return 0;}
static void prepare_hdmi(struct snd_pcm_substream *substream) {
struct snd_pcm_runtime *runtime=substream->runtime;
struct aml_tdm tdm={.i2s2hdmitx=1},*p_tdm=&tdm;
int separated=0;
struct aud_para aud_param={0};
aud_param.rate=runtime->rate;aud_param.size=runtime->sample_bits;aud_param.chs=runtime->channels;
''' + branch + '\n}']
    result.append(TEST_BODY)
    return '\n\n'.join(result), {value: hashlib.sha256((root/value).read_bytes()).hexdigest() for value in paths.values()}


def mutants(code):
    """Each faulty production variant must compile then die at a runtime assertion."""
    variants = []
    def replace_once(text, old, new):
        assert text.count(old) == 1, (old, text.count(old))
        return text.replace(old, new, 1)
    def change(name, func, old, new):
        original = function(code, func)
        variants.append((name, replace_once(code, original, replace_once(original, old, new))))
    def plain(name, old, new):
        variants.append((name, replace_once(code, old, new)))
    def row(name, ca, old, new):
        match = re.search(r'^\{[^\n]+\.ca = '+ca+r' \}[^\n]*$', code, re.M)
        assert match, ca
        original = match.group()
        variants.append((name, replace_once(code, original, replace_once(original, old, new))))
    plain('hidden-native-knob', '#define BASE_PCM_LAYOUTS 6', '#define BASE_PCM_LAYOUTS 4')
    change('optional-layouts-default-exposed', 'aml_n_pcm_layouts', 'extra_pcm_layouts ?', 'true ?')
    row('rear-centre-missing', '0x0e', 'NA,   RC,', 'NA,   NA,')
    row('six0-has-lfe', '0x0e', 'FC,   NA,', 'FC,  LFE,')
    row('six1-lfe-missing', '0x0f', 'FC,  LFE,', 'FC,   NA,')
    row('native-unpadded', '0x0e', '.channels = 8', '.channels = 6')
    row('wrong-allocation-receipt', '0x0e', '.ca = 0x0e', '.ca = 0x13')
    change('collapsed-substream-index', 'aml_dai_tdm_chmap_state', 'return &states[idx];', 'return &states[0];')
    change('get-owner-unchecked', 'aml_dai_tdm_chmap_ctl_get', 'prtd->substream == substream &&', 'true &&')
    change('snapshot-owner-unchecked', 'aml_dai_tdm_chmap_layout', 'prtd->substream == substream &&', 'true &&')
    change('get-runtime-unchecked', 'aml_dai_tdm_chmap_ctl_get', 'prtd->runtime == substream->runtime &&', 'true &&')
    change('snapshot-runtime-unchecked', 'aml_dai_tdm_chmap_layout', 'prtd->runtime == substream->runtime &&', 'true &&')
    change('snapshot-count-unchecked', 'aml_dai_tdm_chmap_layout', 'channel_allocations[li].channels == substream->runtime->channels', 'true')
    change('slot-validation-skipped', 'aml_dai_tdm_chmap_ctl_put', 'ucontrol->value.integer.value[channel] !=\n\t\t\t    channel_allocations[layout].speakers[7 - channel]', 'false')
    change('rejected-map-keeps-receipt', 'aml_dai_tdm_chmap_ctl_put', 'prtd->chmap_layout = matched_layout;\n\tprtd->substream = matched_layout >= 0 ? substream : NULL;\n\tprtd->runtime = matched_layout >= 0 ? runtime : NULL;', 'if (matched_layout >= 0) {prtd->chmap_layout = matched_layout;prtd->substream = substream;prtd->runtime = runtime;}')
    change('snapshot-reuses-output', 'aml_dai_tdm_chmap_layout', 'aud_param->layout = 0;\n\taud_param->layout_valid = false;', '/* unsafe reused output */')
    for func, name in [('aml_tdm_open', 'open-retains-receipt'), ('aml_tdm_close', 'close-retains-receipt'), ('aml_tdm_hw_params', 'hwparams-retains-receipt'), ('aml_tdm_hw_free', 'hwfree-retains-receipt')]:
        change(name, func, 'aml_dai_tdm_chmap_reset(substream);', '/* unsafe receipt retention */')
    change('receipt-memory-leak', 'aml_dai_tdm_chmap_private_free', 'kfree(info->private_data);', '/* unsafe leaked receipt */')
    change('copied-donor-invalidity', 'hdmitx_pcm_rear_center_invalid', 'channels == 6 ? 0x82 : (channels == 7 ? 0x80 : 0)', 'channels == 6 ? 0x1a : (channels == 7 ? 0x08 : 0)')
    change('rear-centre-marked-invalid', 'hdmitx_pcm_rear_center_invalid', 'channels == 6 ? 0x82 : (channels == 7 ? 0x80 : 0)', 'channels == 6 ? 0x8a : (channels == 7 ? 0x88 : 0)')
    change('left-right-invalidity-swapped', 'hdmitx_pcm_rear_center_invalid', 'channels == 6 ? 0x82 : (channels == 7 ? 0x80 : 0)', 'channels == 6 ? 0x28 : (channels == 7 ? 0x08 : 0)')
    change('raw-treated-as-native-pcm', 'hdmitx_pcm_rear_center_channels', 'audio_param->type != CT_PCM ||', 'false ||')
    change('unverified-receipt-used', 'hdmitx_pcm_rear_center_channels', '!audio_param->layout_valid ||', 'false ||')
    change('wrong-transport-size-used', 'hdmitx_pcm_rear_center_channels', 'audio_param->channel_num != CC_8CH', 'false')
    change('software-padded-cc', 'hdmi_tx_construct_aud_packet', 'AUD_DB[0] = hdmitx_pcm_rear_center_channels(audio_param) - 1;', 'AUD_DB[0] = audio_param->channel_num;')
    change('hardware-padded-cc', 'set_aud_info_pkt', 'native_channels - 1, 4, 3);', 'audio_param->channel_num, 4, 3);')
    change('software-wrong-ca', 'hdmi_tx_construct_aud_packet', 'AUD_DB[3] = audio_param->layout;', 'AUD_DB[3] = 0x13;')
    change('hardware-wrong-ca', 'set_aud_info_pkt', 'hdmitx_wr_reg(HDMITX_DWC_FC_AUDICONF2, audio_param->layout);', 'hdmitx_wr_reg(HDMITX_DWC_FC_AUDICONF2, 0x13);')
    original = function(code, 'set_aud_chnls')
    assert original.count('hdmitx_wr_reg(HDMITX_DWC_FC_AUDSV, 0);') == 2
    variants.append(('invalidity-not-reset', replace_once(code, original, original.replace('hdmitx_wr_reg(HDMITX_DWC_FC_AUDSV, 0);', '(void)0;'))))
    change('same-count-layout-not-refreshed', 'notify_receipt', 'hdev->audio_param_update_flag = 1;', 'hdev->audio_param_update_flag = 0;')
    change('validity-loss-not-refreshed', 'notify_receipt', 'audio_param->layout_valid != aud_param->layout_valid', 'false')
    change('prepare-missing-exact-receipt', 'prepare_hdmi', 'aml_dai_tdm_chmap_layout(substream, &aud_param);', '/* unsafe unresolved notification */')
    change('prepare-missing-fourth-lane', 'prepare_hdmi', 'hdmitx_ext_set_i2s_mask(runtime->channels, 0xf);', 'hdmitx_ext_set_i2s_mask(runtime->channels, 0x7);')
    change('hardware-forced-output-overridden', 'set_aud_info_pkt', 'native_channels = 0;', '/* unsafe ignored output override */')
    change('software-forced-output-overridden', 'hdmi_tx_construct_aud_packet', 'hdmi_ch != CC_6CH &&', 'true &&')
    return variants


def run(args):
    logs = Path(args.logs) if args.logs else Path(tempfile.mkdtemp(prefix='pcm-native-kernel-'))
    logs.mkdir(parents=True, exist_ok=True)
    code, hashes = production(Path(args.kernel_root))
    records = []
    variants = [('positive', code)] + (mutants(code) if args.negative_controls else [])
    for name, source in variants:
        c = logs / (name+'.c'); binary = logs / name;c.write_text(source)
        argv = ['gcc','-std=gnu11','-g','-O1','-Wall','-Wextra','-Werror=implicit-function-declaration','-Werror=incompatible-pointer-types','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-strict-overflow','-fno-pie','-no-pie',str(c),'-o',str(binary)]
        built = subprocess.run(argv, text=True, capture_output=True)
        (logs/(name+'-compile.log')).write_text(built.stdout+built.stderr)
        assert built.returncode == 0, f'{name}: compile failure; see {logs}'
        env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:abort_on_error=1', UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
        observed = subprocess.run([str(binary)], text=True, capture_output=True, env=env)
        output = observed.stdout+observed.stderr
        (logs/(name+'-run.log')).write_text(output)
        if name == 'positive':
            assert observed.returncode == 0, f'positive failed: {output}'
        else:
            assert observed.returncode == -6 and 'Assertion' in output and 'AddressSanitizer' not in output and 'runtime error:' not in output, f'{name}: did not fail by intended assertion: {output}'
        records.append(dict(name=name, compiler_argv=argv, compile_returncode=built.returncode, runtime_returncode=observed.returncode))
    report = dict(kernel_root=args.kernel_root, script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), extracted_sha256=hashlib.sha256(code.encode()).hexdigest(), host_compiler=subprocess.check_output(['gcc','--version'], text=True).splitlines()[0], source_sha256=hashes, results=records, caveats='Host extracted production functions with explicit ALSA allocation/control/mutex/register stand-ins; no full kernel linkage, live ALSA concurrency, DMA/interrupt/receiver delivery or CE runtime proof.')
    (logs/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(f'PASS {len(records)} kernel production cases; logs {logs}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel-root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--logs')
    parser.add_argument('--negative-controls', action='store_true')
    run(parser.parse_args())
