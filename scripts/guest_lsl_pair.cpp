// LSL in-guest acceptance: outlet -> resolve -> inlet round-trip on localhost.
// Mirrors tests/test_lsl_build.py PAIR_SRC but zero-dependency output markers.
#include <stdio.h>
#include <chrono>
#include <thread>
#include <lsl_cpp.h>

int main(void) {
    try {
        lsl::stream_info info("XtosLive", "EEG", 1, 100.0, lsl::cf_float32, "xtos-live-check");
        lsl::stream_outlet outlet(info);
        std::this_thread::sleep_for(std::chrono::seconds(1));
        auto found = lsl::resolve_stream("name", "XtosLive", 1, 10.0);
        if (found.empty()) { fprintf(stderr, "resolve_failed\n"); return 1; }
        lsl::stream_inlet inlet(found[0]);
        inlet.open_stream(5.0);
        float sample[1] = {3.14f};
        for (int i = 0; i < 5; i++) {
            outlet.push_sample(sample);
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        float got[1] = {0};
        double t = inlet.pull_sample(got, 1, 10.0);
        if (t != 0 && got[0] > 3.0) {
            printf("GUEST_LSL_PAIR=PASS value=%.2f\n", got[0]);
            return 0;
        }
        fprintf(stderr, "bad_sample value=%f t=%f\n", got[0], t);
        return 1;
    } catch (const std::exception &e) {
        fprintf(stderr, "pair_error=%s\n", e.what());
        return 1;
    }
}
