/* SPDX-License-Identifier: GPL-2.0 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "bci_stream.h"
#include "cyton_serial.h"
#include "bci_decode.h"
#include "cli_options.h"
#include "bci_config_cli.h"
#include "bci_calibration_cli.h"
#include "bci_diagnostic_cli.h"

#define MAX_INPUT_BYTES (64u*1024u*1024u)
#define MAX_OUTPUT_FRAMES 1000000u
struct context {
    FILE *output;
    struct bci_stream *stream;
    xtos_cyton_parser parser;
    uint32_t gain[34][8]; /* Immutable per-generation; published before push. */
    uint32_t enabled_mask[34];
    char identity[34][256];
    uint64_t generation, sequence, bytes, accepted, errors, missing, partial, disconnected, discarded;
    uint8_t previous;
    int have_counter, gap_pending, output_error;
    uint64_t output_frames;
};
static volatile sig_atomic_t stop_requested;
static void on_signal(int signum) { (void)signum; stop_requested=1; }
static uint64_t monotonic_ns(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC,&t)) return 0;
    return (uint64_t)t.tv_sec*1000000000u+(uint64_t)t.tv_nsec;
}
static int json_string(FILE *output,const char *text)
{
    if(fputc('"',output)==EOF) return -1;
    for(const unsigned char *p=(const unsigned char *)text;*p;p++) {
        if(*p=='"' || *p=='\\') { if(fputc('\\',output)==EOF || fputc(*p,output)==EOF) return -1; }
        else if(*p<32) { if(fprintf(output,"\\u%04x",(unsigned)*p)<0) return -1; }
        else if(fputc(*p,output)==EOF) return -1;
    }
    return fputc('"',output)==EOF?-1:0;
}
static int emit(const struct bci_output_frame *frame, void *arg)
{
    struct context *c=arg;
    const struct bci_input_frame *f=&frame->input;
    if (++c->output_frames>MAX_OUTPUT_FRAMES || f->generation==0 || f->generation>=34) { errno=EFBIG; return -1; }
    if (fprintf(c->output,"{\"schema\":\"xtos-bci-stream-v1\",\"sequence\":%"PRIu64
                ",\"generation\":%"PRIu64",\"receive_ns\":%"PRIu64",\"processed_ns\":%"PRIu64
                ",\"sample_rate_hz\":250,\"unit\":\"nanovolt\",\"quality\":%u,\"filter_reset\":%u,\"enabled_mask\":%u,\"samples_nv\":[",
                f->sequence,f->generation,f->receive_ns,frame->processed_ns,f->quality,frame->filter_reset,c->enabled_mask[f->generation])<0) goto fail;
    for(unsigned i=0;i<8;i++) if(fprintf(c->output,"%s%d",i?",":"",f->samples_nv[i])<0) goto fail;
    if(fputs("],\"filtered_nv\":[",c->output)==EOF) goto fail;
    for(unsigned i=0;i<8;i++) if(fprintf(c->output,"%s%.17g",i?",":"",frame->filtered_nv[i])<0) goto fail;
    if(fputs("],\"gain\":[",c->output)==EOF) goto fail;
    for(unsigned i=0;i<8;i++) if(fprintf(c->output,"%s%u",i?",":"",c->gain[f->generation][i])<0) goto fail;
    if(fputs("],\"firmware_identity\":",c->output)==EOF ||
       json_string(c->output,c->identity[f->generation]) || fputs("}\n",c->output)==EOF) goto fail;
    return 0;
fail:
    c->output_error=1;
    return -1;
}
static void new_generation(struct context *c,uint64_t generation,const uint32_t gain[8])
{
    if(c->parser.packet_pos) c->partial++;
    c->generation=generation;
    c->sequence=0;
    c->have_counter=0;
    c->gap_pending=generation>1;
    memcpy(c->gain[generation],gain,sizeof(c->gain[generation]));
    xtos_cyton_parser_init(&c->parser);
}
static int feed(struct context *c,const unsigned char *data,size_t count,uint64_t received)
{
    int32_t nominal[8];
    c->bytes+=count;
    if(c->bytes>MAX_INPUT_BYTES || !received) { errno=EFBIG; return -1; }
    for(size_t i=0;i<count;i++) {
        unsigned long lost=c->parser.missing_samples;
        if(!c->parser.packet_pos && data[i]!=XTOS_CYTON_START_BYTE) c->discarded++;
        int result=xtos_cyton_parser_feed(&c->parser,data[i],nominal);
        if(result<0) { c->errors++;c->gap_pending=1;continue; }
        if(!result) continue;
        struct bci_input_frame f={0};
        uint8_t counter=c->parser.packet[1];
        int first=!c->have_counter;
        if(c->have_counter) c->sequence+=(uint8_t)(counter-c->previous);
        c->previous=counter;c->have_counter=1;
        f.sequence=c->sequence;f.generation=c->generation;f.receive_ns=received;f.sample_rate_hz=250;
        c->missing+=c->parser.missing_samples-lost;
        if(c->gap_pending || c->parser.missing_samples!=lost) f.quality|=BCI_QUALITY_GAP;
        if(c->generation>1 && first) f.quality|=BCI_QUALITY_RECOVERED;
        if(bci_decode_gains(c->parser.packet,c->gain[c->generation],f.samples_nv)) { c->errors++;c->gap_pending=1;continue; }
        if(bci_stream_push(c->stream,&f)) {
            c->gap_pending=1;
            if(errno!=EAGAIN) return -1;
        } else { c->gap_pending=0;c->accepted++; }
    }
    return 0;
}
static int serial_event(enum cyton_serial_event event,const struct cyton_board_config *board,
    const unsigned char *bytes,size_t count,uint64_t generation,uint64_t received,void *arg)
{
    struct context *c=arg;
    if(event==CYTON_CONNECTED) {
        if(!board || !generation || generation>=34 || generation<=c->generation) { errno=EPROTO;return -1; }
        c->enabled_mask[generation]=board->enabled_mask;
        memcpy(c->identity[generation],board->identity,sizeof(board->identity));
        new_generation(c,generation,board->gain);
    } else if(event==CYTON_BYTES) {
        if(generation!=c->generation) { errno=EPROTO;return -1; }
        return feed(c,bytes,count,received);
    } else if(event==CYTON_DISCONNECTED) {
        c->disconnected++;
        if(c->parser.packet_pos) c->partial++;
        xtos_cyton_parser_init(&c->parser);
        c->gap_pending=1;
    }
    return 0;
}
static int read_fixture(struct context *c,const char *path)
{
    unsigned char buf[4096];
    struct stat st;
    int fd=open(path,O_RDONLY|O_CLOEXEC|O_NONBLOCK|O_NOFOLLOW),ret=0;
    if(fd<0) return -1;
    if(fstat(fd,&st) || !S_ISREG(st.st_mode) || st.st_size<0 || (uint64_t)st.st_size>MAX_INPUT_BYTES) { close(fd);errno=EINVAL;return -1; }
    uint32_t gain[8]={24,24,24,24,24,24,24,24};
    c->enabled_mask[1]=255;
    new_generation(c,1,gain);
    while(!stop_requested) {
        ssize_t n=read(fd,buf,sizeof(buf));
        if(!n) break;
        if(n<0) { if(errno==EINTR) continue; ret=-1;break; }
        if(feed(c,buf,(size_t)n,monotonic_ns())) { ret=-1;break; }
    }
    if(c->parser.packet_pos) c->partial++;
    close(fd);
    return ret;
}
int main(int argc,char **argv)
{
    if(argc>1 && !strcmp(argv[1],"config")) return bci_config_cli_main(argc,argv);
    if(argc>1 && !strcmp(argv[1],"diagnose")) return bci_diagnostic_cli_main(argc,argv);
    if(argc>1 && (!strcmp(argv[1],"calibrate") || !strcmp(argv[1],"apply-calibration")))
        return bci_calibration_cli_main(argc,argv);
    struct bci_cli_options opt;
    struct context c={0};
    struct cyton_serial_stats serial_stats={0};
    struct bci_stream_stats stats={0};
    struct stat output_stat;
    int fd,result,close_result,io_error=0;
    if(argc==2 && !strcmp(argv[1],"--help")) {
        puts("usage: bci (--input RAW_FIXTURE | --device TTY) --output NEW_JSONL [--capacity N] [--duration-ms N] [--command-timeout-ms N] [--reconnect-delay-ms N] [--reconnect-attempts N]\n       bci config --device /dev/xtosN\n       bci diagnose --device TTY --mode shorted|impedance-p|impedance-n --channel 1..8 --output NEW_JSON [--frames 32..4096] [--settle-frames 0..1024] [--command-timeout-ms 1..3000]\n       bci calibrate --points PAIRED_CSV --session-file SESSION --firmware-file FIRMWARE --reference-id ID --source fixture|external-reference --mask MASK --min-span-nv N --max-residual-nv N --output NEW_MODEL\n       bci apply-calibration --model MODEL --input RAW_CSV --session-file SESSION --firmware-file FIRMWARE --reference-id ID --source fixture|external-reference --output NEW_CSV");return 0;
    }
    if(bci_cli_options_parse(argc,argv,&opt)) { fputs("bci: invalid options; use --help\n",stderr);return 2; }
    fd=open(opt.output,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);
    if(fd<0) { perror("bci: output");return 2; }
    if(fstat(fd,&output_stat) || !S_ISREG(output_stat.st_mode)) { close(fd);return 2; }
    c.output=fdopen(fd,"w");
    if(!c.output) { close(fd);return 2; }
    c.stream=bci_stream_create(opt.capacity,emit,&c);
    if(!c.stream) { fclose(c.output);perror("bci: stream");return 2; }
    struct sigaction action={0};
    action.sa_handler=on_signal;sigemptyset(&action.sa_mask);
    if(sigaction(SIGINT,&action,NULL) || sigaction(SIGTERM,&action,NULL)) { bci_stream_destroy(c.stream);fclose(c.output);return 2; }
    if(opt.input) result=read_fixture(&c,opt.input);
    else {
        struct cyton_serial_options options={.device=opt.device,.duration_ms=opt.duration_ms,
            .command_timeout_ms=opt.command_timeout_ms,.reconnect_delay_ms=opt.reconnect_delay_ms,
            .reconnect_attempts=opt.reconnect_attempts};
        result=cyton_serial_run(&options,&stop_requested,serial_event,&c,&serial_stats);
        if(c.parser.packet_pos) c.partial++;
    }
    if(result) { perror("bci: acquisition");io_error=1; }
    close_result=bci_stream_close(c.stream);
    bci_stream_get_stats(c.stream,&stats);
    bci_stream_destroy(c.stream);
    if(fflush(c.output) || ferror(c.output)) io_error=1;
    if(fclose(c.output)) io_error=1;
    int degraded=stop_requested || io_error || close_result || c.errors || c.missing || c.partial || c.disconnected || c.discarded ||
                 stats.dropped_full || stats.invalid || stats.output_errors || !stats.processed || serial_stats.stop_errors;
    int summary_result=printf("{\"schema\":\"xtos-bci-summary-v1\",\"status\":\"%s\",\"source_mode\":\"%s\",\"physical_evidence\":false,"
           "\"bytes\":%"PRIu64",\"accepted\":%"PRIu64",\"processed\":%"PRIu64",\"missing_frames\":%"PRIu64
           ",\"parser_errors\":%"PRIu64",\"partial_frames\":%"PRIu64",\"discarded_bytes\":%"PRIu64",\"dropped_full\":%"PRIu64
           ",\"filter_resets\":%"PRIu64",\"connections\":%"PRIu64",\"reconnects\":%"PRIu64
           ",\"stop_errors\":%"PRIu64",\"interrupted\":%s}\n",
           degraded?"degraded":"ok",opt.input?"fixture":"serial",c.bytes,c.accepted,stats.processed,c.missing,c.errors,c.partial,c.discarded,
           stats.dropped_full,stats.resets,serial_stats.connections,serial_stats.reconnects,serial_stats.stop_errors,
           stop_requested?"true":"false");
    if(summary_result<0 || fflush(stdout)==EOF || ferror(stdout)) {
        fputs("bci: summary output failed\n",stderr);
        return 3;
    }
    return degraded?3:0;
}
