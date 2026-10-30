/* SPDX-License-Identifier: GPL-2.0 */
#ifndef BCI_DIAGNOSTIC_CLI_H
#define BCI_DIAGNOSTIC_CLI_H
/* Full argv: bci diagnose --device PATH --mode MODE --channel 1..8 --output PATH.
 * Serial/exclusive invocation only. 0=complete/restored, 2=arguments/setup,
 * 3=capture/cancel/restoration/output failure. No SDK dependency. */
int bci_diagnostic_cli_main(int argc, char **argv);
#endif
