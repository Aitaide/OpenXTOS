#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "bci_decode.h"
#define CHECK(v) do { if (!(v)) { fprintf(stderr,"line=%d\n",__LINE__); return 1; } } while (0)
int main(void)
{
    uint8_t p[33] = {0}; uint32_t gains[8] = {24,12,8,6,4,2,1,24};
    int32_t out[8], before[8];
    p[0]=0xa0; p[32]=0xc0;
    for (int ch=0;ch<8;ch++) { p[2+3*ch]=0; p[3+3*ch]=0; p[4+3*ch]=24; }
    CHECK(bci_decode_gains(p,gains,out)==0);
    for(int ch=0;ch<8;ch++) CHECK(out[ch]==(int32_t)(24LL*4500000000LL/(gains[ch]*8388607LL)));
    memset(out,0x5a,sizeof(out)); memcpy(before,out,sizeof(out));
    gains[7]=3; CHECK(bci_decode_gains(p,gains,out)==-1 && errno==EINVAL);
    CHECK(memcmp(before,out,sizeof(out))==0);
    gains[7]=1; p[23]=0x7f;p[24]=0xff;p[25]=0xff;
    CHECK(bci_decode_gains(p,gains,out)==-1 && errno==ERANGE);
    CHECK(memcmp(before,out,sizeof(out))==0);
    gains[7]=2;
    CHECK(bci_decode_gains(p,gains,out)==-1 && errno==ERANGE);
    p[23]=0x80;p[24]=0;p[25]=0; gains[7]=24;
    CHECK(bci_decode_gains(p,gains,out)==0 && out[7]==-187500022);
    p[32]=0xb0; CHECK(bci_decode_gains(p,gains,out)==-1 && errno==EINVAL);
    puts("bci_decode=pass"); return 0;
}
