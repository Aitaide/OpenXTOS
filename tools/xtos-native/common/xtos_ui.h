/* xtos_ui.h: shared TTY-aware text styling for XTOS native tools.
 *
 * Design rules (keep all three):
 *   1. Color only when stdout is an interactive TTY. Piped/redirected/SSH
 *      output stays byte-identical to the unstyled text so every scripted
 *      marker (XTOS_FW_STATUS, PREEMPT_RT active, PKG_VERIFY, ...) and every
 *      --json stream keeps parsing.
 *   2. 256-color/truecolor terminals only get basic 16-color SGR codes, so
 *      every terminal from vt100 up renders identically. TERM=dumb disables.
 *   3. This header is self-contained freestanding C: no libc calls beyond
 *      isatty() at init; usable from any tool by #including the path.
 *
 * Usage:
 *   #include "xtos_ui.h"   // compile with -I tools/xtos-native/common
 *   int main(...) { xui_init(); printf("%s", XUI_TITLE("XTOS Info")); }
 *
 * Style vocabulary (deliberately small):
 *   XUI_TITLE  bold + bright cyan    section/tool headings
 *   XUI_LABEL  bold                  "Kernel:" style field names
 *   XUI_OK     bold + bright green   PASS/active/success values
 *   XUI_WARN   bold + bright yellow  warnings
 *   XUI_BAD    bold + bright red     errors/failure values
 *   XUI_DIM    dim                   secondary/metadata text
 *   XUI_ACC    bright magenta        command hints, calls-to-action
 *   XUI_RESET  reset                 end of styled span
 * With styling off every macro expands to "" and output is plain ASCII.
 */
#ifndef XTOS_UI_H
#define XTOS_UI_H

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Styling state, resolved once at startup from the actual stdout. */
static int xui_style = -1; /* -1 = uninitialized */

static void xui_init(void) {
    if (xui_style >= 0) return;
    xui_style = 0;
    if (!isatty(STDOUT_FILENO)) return;
    const char *term = getenv("TERM");
    if (!term || !term[0] || strcmp(term, "dumb") == 0) return;
    /* Basic colors only: any real terminal type qualifies. TERM=linux,
     * xterm*, vt100+ and even unknown vendors all support SGR 30-37/90-97. */
    xui_style = 1;
}

static const char *xui(int enabled, const char *codes) __attribute__((unused));
static const char *xui(int enabled, const char *codes) {
    if (!enabled) return "";
    xui_init();
    return xui_style ? codes : "";
}

/* Styled printf wrappers: with styling off they are plain printf, so
 * format-string argument checking still applies at -Wformat. */
#define XUI_TITLE(fmt, ...) xui_p(1, "\033[1;96m", fmt, ##__VA_ARGS__)
#define XUI_LABEL(fmt, ...) xui_p(1, "\033[1m",     fmt, ##__VA_ARGS__)
#define XUI_OK(fmt, ...)    xui_p(1, "\033[1;92m", fmt, ##__VA_ARGS__)
#define XUI_WARN(fmt, ...)  xui_p(1, "\033[1;93m", fmt, ##__VA_ARGS__)
#define XUI_BAD(fmt, ...)   xui_p(1, "\033[1;91m", fmt, ##__VA_ARGS__)
#define XUI_DIM(fmt, ...)   xui_p(1, "\033[2m",   fmt, ##__VA_ARGS__)
#define XUI_ACC(fmt, ...)   xui_p(1, "\033[95m",  fmt, ##__VA_ARGS__)

__attribute__((format(printf, 3, 4)))
static void xui_p(int color, const char *codes, const char *fmt, ...) __attribute__((unused));
__attribute__((format(printf, 3, 4)))
__attribute__((unused))
static void xui_p(int color, const char *codes, const char *fmt, ...) {
    (void)color;
    va_list ap;
    va_start(ap, fmt);
    if (xui_style) fputs(codes, stdout);
    vprintf(fmt, ap);
    if (xui_style) fputs("\033[0m", stdout);
    va_end(ap);
}

/* Raw code strings for printf %s embedding inside a wider format string. */
#define XUI_TITLE_S xui(1, "\033[1;96m")
#define XUI_LABEL_S xui(1, "\033[1m")
#define XUI_OK_S    xui(1, "\033[1;92m")
#define XUI_WARN_S  xui(1, "\033[1;93m")
#define XUI_BAD_S   xui(1, "\033[1;91m")
#define XUI_DIM_S   xui(1, "\033[2m")
#define XUI_ACC_S   xui(1, "\033[95m")
#define XUI_RESET_S xui(1, "\033[0m")

#endif /* XTOS_UI_H */
