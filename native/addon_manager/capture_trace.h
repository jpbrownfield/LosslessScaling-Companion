#pragma once
#include <array>
#include <atomic>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>
#include <vector>

namespace RealFrameHook {
enum class CaptureStep {
    NoCallback, WithCallback, ProfileWait, MissingIdentity, UnmatchedShader,
    CaptureMatched, ScalerMatched, Unsupported, CaptureSkipped,
    HandoffAttempt, CallbackEntered, HandoffAccepted, HandoffDeclined, Count
};
struct CaptureTraceSnapshot {
    std::map<std::string, uint64_t> counts;
    std::vector<std::string> shaders;
};
class CaptureTrace {
    static constexpr size_t StepCount = static_cast<size_t>(CaptureStep::Count);
    std::array<std::atomic<uint64_t>, StepCount> counts{};
    std::atomic<unsigned> samples{0};
    std::mutex sampleMutex;
    std::vector<std::string> shaders;
public:
    void Count(CaptureStep step) {
        counts[static_cast<size_t>(step)].fetch_add(1, std::memory_order_relaxed);
    }
    void Reset() {
        std::lock_guard<std::mutex> lock(sampleMutex);
        for (auto& count : counts) count.store(0, std::memory_order_relaxed);
        shaders.clear();
        samples.store(0, std::memory_order_relaxed);
    }
    void Sample(const std::array<uint8_t, 32>& sha, unsigned x, unsigned y, unsigned z) {
        if (samples.load(std::memory_order_relaxed) >= 8 ||
            samples.fetch_add(1, std::memory_order_relaxed) >= 8) return;
        std::string text;
        text.reserve(100);
        constexpr char hex[] = "0123456789abcdef";
        for (const auto byte : sha) { text += hex[byte >> 4]; text += hex[byte & 15]; }
        text += " groups=" + std::to_string(x) + "x" + std::to_string(y) + "x" + std::to_string(z);
        std::lock_guard<std::mutex> lock(sampleMutex);
        if (shaders.size() < 8) shaders.push_back(std::move(text));
    }
    CaptureTraceSnapshot Snapshot() {
        static constexpr std::array<const char*, StepCount> names{
            "dispatchesWithoutCallback", "dispatchesWithCallback", "profileWaits",
            "missingShaderIdentities", "unmatchedShaders", "captureShaderMatches",
            "scalerShaderMatches", "unsupportedPauses", "skippedCaptureDispatches",
            "handoffAttempts", "callbackInvocations", "handoffAccepted", "handoffDeclined"
        };
        CaptureTraceSnapshot result;
        for (size_t index = 0; index < StepCount; ++index)
            result.counts.emplace(names[index], counts[index].load(std::memory_order_relaxed));
        std::lock_guard<std::mutex> lock(sampleMutex);
        result.shaders = shaders;
        return result;
    }
};
}