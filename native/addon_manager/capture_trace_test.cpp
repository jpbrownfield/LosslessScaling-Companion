#include "capture_trace.h"
#include <iostream>
#include <stdexcept>
#include <thread>

static void Expect(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
int main() { try {
    RealFrameHook::CaptureTrace trace;
    std::vector<std::thread> workers;
    for (unsigned worker = 0; worker < 4; ++worker) workers.emplace_back([&trace] {
        for (unsigned iteration = 0; iteration < 1000; ++iteration)
            trace.Count(RealFrameHook::CaptureStep::HandoffAttempt);
    });
    for (auto& worker : workers) worker.join();
    std::array<uint8_t, 32> sha{};
    sha[0] = 0xab; sha[31] = 0xef;
    for (unsigned dispatch = 0; dispatch < 100; ++dispatch) trace.Sample(sha, 80, 58, 1);
    const auto snapshot = trace.Snapshot();
    Expect(snapshot.counts.at("handoffAttempts") == 4000, "Concurrent handoff counts lost");
    Expect(snapshot.counts.at("callbackInvocations") == 0, "Handoff must not imply callback entry");
    Expect(snapshot.shaders.size() == 8, "Shader samples must stay bounded");
    Expect(snapshot.shaders.front().substr(0, 2) == "ab", "Leading hash byte");
    Expect(snapshot.shaders.front().substr(62) == "ef groups=80x58x1", "Hash and group shape");
    trace.Reset();
    const auto reset = trace.Snapshot();
    for (const auto& entry : reset.counts) Expect(entry.second == 0, "Settings must reset counters");
    Expect(reset.shaders.empty(), "Settings must reset shader samples");
    trace.Sample(sha, 1, 2, 3);
    Expect(trace.Snapshot().shaders.size() == 1, "Sampling must resume after settings reset");
    std::cout << "Capture trace passed (counts, concurrency, bounded hashes, settings reset).\n";
    return 0;
} catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; } }