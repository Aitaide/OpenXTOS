/* SPDX-License-Identifier: GPL-2.0 */
#ifndef BCI_CONFIG_CLI_H
#define BCI_CONFIG_CLI_H
/* Called with the complete bci argv: bci config --device /dev/xtosN. */
int bci_config_cli_main(int argc, char **argv);
#endif
