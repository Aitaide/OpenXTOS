#include <stddef.h>
#include <stdio.h>
#include "xtos/bci_uapi.h"
_Static_assert(sizeof(struct xtos_bci_snapshot_request)==72, "request layout");
_Static_assert(sizeof(struct xtos_bci_snapshot_header)==64, "header layout");
_Static_assert(sizeof(struct xtos_bci_snapshot_consume)==48, "consume layout");
_Static_assert(sizeof(struct xtos_bci_snapshot_release)==24, "release layout");
_Static_assert(offsetof(struct xtos_bci_snapshot_header,payload_bytes)==16, "payload alignment");
_Static_assert(offsetof(struct xtos_bci_snapshot_request,token)==40, "token offset");
int main(void) { if (_IOC_NR(XTOS_BCI_SNAPSHOT_ACQUIRE)!=13 || _IOC_NR(XTOS_BCI_SNAPSHOT_CONSUME)!=14 || _IOC_NR(XTOS_BCI_SNAPSHOT_RELEASE)!=15) return 1; puts("snapshot_uapi=pass"); return 0; }
