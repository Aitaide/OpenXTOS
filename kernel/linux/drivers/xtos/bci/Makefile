# SPDX-License-Identifier: GPL-2.0
#
# Makefile for XTOS BCI drivers
#

obj-$(CONFIG_XTOS_BCI) += xtos_bci_core.o
xtos_bci_core-y := xtos_bci_core_impl.o xtos_bci_snapshot.o xtos_bci_frame.o xtos_bci_frame_snapshot.o

# Keep the established module and source names while linking the helper.
$(obj)/xtos_bci_core_impl.o: modname := xtos_bci_core
$(obj)/xtos_bci_core_impl.o: $(src)/xtos_bci_core.c FORCE
	$(call if_changed_rule,cc_o_c)

obj-$(CONFIG_XTOS_BCI) += xtos_bci_buffer.o

# Device drivers
obj-$(CONFIG_XTOS_BCI_OPENBCI) += devices/openbci_cyton.o

# Add more device drivers here
# obj-$(CONFIG_XTOS_BCI_EMOTIV) += devices/emotiv_epoc.o
# obj-$(CONFIG_XTOS_BCI_MUSE) += devices/muse_s.o
