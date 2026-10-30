#ifndef XTOS_BCI_CALIBRATION_CLI_H
#define XTOS_BCI_CALIBRATION_CLI_H
/* full argc/argv: argv[0] is bci; command is argv[1]. Offline file helper.
 * Returns 0 success, 2 invalid inputs/conflict, 3 output/I/O completion error.
 * Source kind is a declaration; hashes bind bytes, never physical acceptance.
 * Temporarily selects per-thread C numeric locale and ignores SIGPIPE, restoring
 * both before return. Caller must serialize process-wide signal disposition use.
 * No ancestor containment is asserted for explicit caller-selected paths.
 */
int bci_calibration_cli_main(int argc, char **argv);
#endif
