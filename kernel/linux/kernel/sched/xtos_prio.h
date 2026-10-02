/* SPDX-License-Identifier: GPL-2.0 */
#ifndef _KERNEL_SCHED_XTOS_PRIO_H
#define _KERNEL_SCHED_XTOS_PRIO_H

#define XTOS_NR_PRIORITIES 15

#define XTOS_PRIO_CRITICAL    0
#define XTOS_PRIO_EEG_SIGNAL  1
#define XTOS_PRIO_BCI_EVENT   2
#define XTOS_PRIO_AR_RENDER   3
#define XTOS_PRIO_AI_URGENT   4
#define XTOS_PRIO_DEVICE_IO   5
#define XTOS_PRIO_RT_HIGH     6
#define XTOS_PRIO_RT_MED      7
#define XTOS_PRIO_RT_LOW      8
#define XTOS_PRIO_RT_MIN      9
#define XTOS_PRIO_NORMAL      10
#define XTOS_PRIO_BATCH_HIGH  11
#define XTOS_PRIO_BATCH_MED   12
#define XTOS_PRIO_BATCH_LOW   13
#define XTOS_PRIO_IDLE        14

#ifndef XTOS_PRIO_MIN
#define XTOS_PRIO_MIN XTOS_PRIO_CRITICAL
#endif
#ifndef XTOS_PRIO_MAX
#define XTOS_PRIO_MAX XTOS_PRIO_IDLE
#endif

#define XTOS_PRIO_VALID(prio) ((prio) >= XTOS_PRIO_MIN && \
				   (prio) <= XTOS_PRIO_MAX)
#define XTOS_PRIO_IS_RT(prio) ((prio) >= XTOS_PRIO_MIN && \
				   (prio) <= XTOS_PRIO_RT_MIN)

#endif /* _KERNEL_SCHED_XTOS_PRIO_H */
