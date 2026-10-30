/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>
#include "xtos/bci.h"
#include "xtos/bci_uapi.h"
#define CHECK(v) do {if(!(v)){fprintf(stderr,"bci_guest line=%d errno=%d\n",__LINE__,errno);return 1;}}while(0)
static int config_contract(int fd)
{
    struct xtos_bci_config_v1 cfg={.version=1};
    CHECK(ioctl(fd,XTOS_BCI_GET_CONFIG,&cfg)==0);
    CHECK(sizeof(cfg)==384 && cfg.version==1 && cfg.channels==8);
    CHECK(cfg.sample_rate_hz==250 && cfg.enabled_mask==255 && cfg.generation==1);
    CHECK(cfg.capabilities==(XTOS_BCI_CONFIG_READBACK|XTOS_BCI_FIRMWARE_READBACK));
    CHECK(!strcmp(cfg.firmware,"Synthetic fixture; not physical firmware"));
    for(unsigned int i=0;i<8;i++) CHECK(cfg.gain[i]==24 && cfg.registers[5+i]==0x68);
    for(size_t i=0;i<sizeof(cfg);i++) {
        struct xtos_bci_config_v1 bad={.version=1},before;
        unsigned char *bytes=(unsigned char *)&bad;
        bytes[i]^=0x40;before=bad;
        CHECK(ioctl(fd,XTOS_BCI_GET_CONFIG,&bad)==-1 && errno==EINVAL);
        CHECK(!memcmp(&bad,&before,sizeof(bad)));
    }
    return 0;
}
int main(void)
{
    int fd=open("/dev/xtos0",O_RDWR|O_NONBLOCK);
    CHECK(fd>=0);
    CHECK(config_contract(fd)==0);
    CHECK(ioctl(fd,XTOS_BCI_START_STREAM)==0);
    struct xtos_bci_snapshot_request r={.version=1,.requested_bytes=64};
    CHECK(ioctl(fd,XTOS_BCI_SNAPSHOT_ACQUIRE,&r)==0);
    CHECK(r.payload_bytes==64 && r.mapping_bytes>=128 && r.channels==8);
    CHECK(mmap(NULL,r.mapping_bytes,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0)==MAP_FAILED);
    CHECK(mmap(NULL,r.mapping_bytes+4096,PROT_READ,MAP_SHARED,fd,0)==MAP_FAILED);
    CHECK(mmap(NULL,r.mapping_bytes,PROT_READ,MAP_SHARED,fd,4096)==MAP_FAILED);
    void *map=mmap(NULL,r.mapping_bytes,PROT_READ,MAP_SHARED,fd,0);
    CHECK(map!=MAP_FAILED);
    const struct xtos_bci_snapshot_header *h=map;
    const int32_t *p=(const int32_t *)(h+1);
    CHECK(h->magic==XTOS_BCI_SNAPSHOT_MAGIC && p[0]==1 && p[15]==16);
    CHECK(mprotect(map,r.mapping_bytes,PROT_READ|PROT_WRITE)==-1 && errno==EACCES);
    char bytes[32];
    CHECK(read(fd,bytes,sizeof(bytes))==-1 && errno==EBUSY);
    struct xtos_bci_snapshot_consume c={.version=1,.token=r.token,.cursor=r.cursor,.bytes=r.payload_bytes};
    CHECK(ioctl(fd,XTOS_BCI_SNAPSHOT_CONSUME,&c)==0);
    CHECK(ioctl(fd,XTOS_BCI_SNAPSHOT_CONSUME,&c)==-1);
    struct xtos_bci_snapshot_release release={.version=1,.token=r.token};
    CHECK(ioctl(fd,XTOS_BCI_SNAPSHOT_RELEASE,&release)==0);
    CHECK(p[0]==1 && p[15]==16);
    CHECK(close(fd)==0);
    CHECK(munmap(map,r.mapping_bytes)==0);
    xtos_bci_session_t *s=xtos_bci_session_create("/dev/xtos0");
    CHECK(s!=NULL && xtos_bci_start(s)==0);
    struct xtos_bci_config_v1 config;
    CHECK(xtos_bci_get_config(s,&config)==0 && config.generation==1 && config.gain[0]==24);
    struct xtos_bci_snapshot snapshot;
    CHECK(xtos_bci_snapshot_acquire(s,64,&snapshot)==0);
    CHECK(snapshot.payload[0]==1 && snapshot.payload[15]==16);
    CHECK(xtos_bci_snapshot_consume(s)==0);
    CHECK(xtos_bci_snapshot_release(s)==0);
    xtos_bci_session_destroy(s);
    fd=open("/dev/xtos0",O_RDWR|O_NONBLOCK);CHECK(fd>=0);
    CHECK(ioctl(fd,XTOS_BCI_START_STREAM)==0);
    memset(&r,0,sizeof(r));r.version=1;r.requested_bytes=64;
    CHECK(ioctl(fd,XTOS_BCI_SNAPSHOT_ACQUIRE,&r)==0);
    map=mmap(NULL,r.mapping_bytes,PROT_READ,MAP_SHARED,fd,0);CHECK(map!=MAP_FAILED);
    p=(const int32_t *)((const char *)map+64);
    struct xtos_bci_config_v1 sentinel;
    memset(&config,0,sizeof(config));config.version=1;sentinel=config;
    int unplug=open("/sys/module/bci_fixture/parameters/unplug",O_WRONLY);CHECK(unplug>=0);
    CHECK(write(unplug,"1",1)==1);close(unplug);
    CHECK(ioctl(fd,XTOS_BCI_GET_CONFIG,&config)==-1 && errno==ENODEV);
    CHECK(!memcmp(&config,&sentinel,sizeof(config)));
    CHECK(ioctl(fd,XTOS_BCI_START_STREAM)==-1 && errno==ENODEV);
    CHECK(read(fd,bytes,sizeof(bytes))==-1 && errno==ENODEV);
    struct pollfd pollfd={.fd=fd,.events=POLLIN};CHECK(poll(&pollfd,1,0)==1);
    CHECK((pollfd.revents&(POLLHUP|POLLERR))==(POLLHUP|POLLERR));
    CHECK(p[0]==1 && p[15]==16);
    pid_t child=fork();CHECK(child>=0);
    if(!child){close(fd);int ok=p[0]==1&&p[15]==16;munmap(map,r.mapping_bytes);_exit(ok?0:1);}
    int status;CHECK(waitpid(child,&status,0)==child && WIFEXITED(status) && !WEXITSTATUS(status));
    CHECK(close(fd)==0 && p[15]==16);
    CHECK(munmap(map,r.mapping_bytes)==0);
    puts("BCI_CONFIG_GUEST=PASS");
    puts("BCI_KERNEL_GUEST=PASS");
    return 0;
}
