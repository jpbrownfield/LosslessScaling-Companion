#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace lsc {
struct PacingSnapshot {
    uint64_t observedPresents=0, successfulPresents=0, eligiblePresents=0;
    size_t intervals=0, relativeStalls=0;
    double medianMs=0, p95Ms=0, maximumMs=0, presentP95Ms=0;
};
class PacingProbe {
    struct Sample { double interval=0, present=0; };
    std::array<Sample, 512> samples{};
    size_t count=0, next=0;
    double previous=0;
public:
    void Observe(double enteredMs, double returnedMs) {
        if(!std::isfinite(enteredMs)||!std::isfinite(returnedMs)||returnedMs<enteredMs)return;
        if(previous>0 && enteredMs>previous) {
            samples[next]={enteredMs-previous,returnedMs-enteredMs};
            next=(next+1)%samples.size();count=(std::min)(count+1,samples.size());
        }
        previous=enteredMs;
    }
    PacingSnapshot Snapshot() const {
        PacingSnapshot result;result.intervals=count;if(!count)return result;
        std::array<double,512> intervals{},durations{};
        for(size_t index=0;index<count;++index) {
            intervals[index]=samples[index].interval;durations[index]=samples[index].present;
        }
        std::sort(intervals.begin(),intervals.begin()+count);
        std::sort(durations.begin(),durations.begin()+count);
        result.medianMs=count%2?intervals[count/2]:(intervals[count/2-1]+intervals[count/2])/2;
        const auto percentile=static_cast<size_t>(std::ceil(count*0.95))-1;
        result.p95Ms=intervals[percentile];result.maximumMs=intervals[count-1];
        result.presentP95Ms=durations[percentile];
        if(count>=30)for(size_t index=0;index<count;++index)
            if(intervals[index]>result.medianMs*1.5)++result.relativeStalls;
        return result;
    }
};
}