#include <errno.h>
#include <stdio.h>
#include "cli_options.h"
#define CHECK(v) do { if (!(v)) { fprintf(stderr,"line %d\n",__LINE__); return 1; } } while (0)
int main(void) {
 struct bci_cli_options o;
 char *ok[]={"bci","--input","capture.bin","--output","out.jsonl"};
 CHECK(bci_cli_options_parse(5,ok,&o)==0 && o.capacity==256 && o.duration_ms==30000);
 char *bad[]={"bci","--input","capture.bin","--device","/dev/ttyUSB0","--output","out"};
 CHECK(bci_cli_options_parse(7,bad,&o)==-1);
 char *negative[]={"bci","--input","a","--output","b","--duration-ms","-1"};
 CHECK(bci_cli_options_parse(7,negative,&o)==-1);
 char *zero[]={"bci","--input","a","--output","b","--capacity","0"};
 CHECK(bci_cli_options_parse(7,zero,&o)==-1);
 char *garbage[]={"bci","--input","a","--output","b","--capacity","12x"};
 CHECK(bci_cli_options_parse(7,garbage,&o)==-1);
 char *dup[]={"bci","--input","a","--output","b","--capacity","2","--capacity","4"};
 CHECK(bci_cli_options_parse(9,dup,&o)==-1);
 char *big[]={"bci","--device","a","--output","b","--reconnect-attempts","33"};
 CHECK(bci_cli_options_parse(7,big,&o)==-1);
 puts("cli_options=pass"); return 0;
}
