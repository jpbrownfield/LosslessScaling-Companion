#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <string>

namespace lsc {
struct PacingSnapshot {
    uint64_t observedPresents=0, successfulPresents=0, eligiblePresents=0;
    size_t intervals=0, relativeStalls=0;
    double medianMs=0, p95Ms=0, maximumMs=0, presentP95Ms=0;
    uint64_t sequence=0, generation=0, dropped=0, overflow=0, totalDropped=0;
    uint64_t sourcePid=0, sourceWindow=0, outputWindow=0, chain=0, device=0, context=0, monitor=0;
    uint32_t refreshHz=0, monitorWidth=0, monitorHeight=0;
    double startedMs=0, endedMs=0, ageMs=0;
    bool enabled=true, identityVerified=false, focused=false;
    std::string presentationLuid;
};
class PacingProbe {
    struct Sample { double interval=0, present=0; };
    std::array<Sample, 2048> samples{};
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
        std::array<double,2048> intervals{},durations{};
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

// Completed duration windows. Observe never allocates or sorts. Readers copy a
// completed probe under the try-lock's peer mutex, then sort their private copy.
class PacingWindows {
    PacingProbe active_, completed_;
    double start_=0, completedStart_=0, completedEnd_=0;
    size_t observations_=0, completedObservations_=0;
    uint64_t sequence_=0,startDropped_=0,completedDropped_=0;
public:
    void Reset() { active_=PacingProbe{};completed_=PacingProbe{};start_=completedStart_=completedEnd_=0;observations_=completedObservations_=0;startDropped_=completedDropped_=0; }
    void Observe(double entered,double returned,uint64_t dropped=0) {
        if(!std::isfinite(entered)||!std::isfinite(returned)||returned<entered)return;
        if(!start_) {start_=entered;startDropped_=dropped;}
        if(entered-start_>=2000) {
            completed_=active_;completedStart_=start_;completedEnd_=entered;
            completedObservations_=observations_;completedDropped_=dropped-startDropped_;++sequence_;
            active_=PacingProbe{};start_=entered;startDropped_=dropped;observations_=0;
        }
        active_.Observe(entered,returned);++observations_;
    }
    PacingSnapshot Snapshot() const {
        auto result=completed_.Snapshot();result.sequence=sequence_;
        result.startedMs=completedStart_;result.endedMs=completedEnd_;result.dropped=completedDropped_;
        result.overflow=completedObservations_>2049?completedObservations_-2049:0;
        return result;
    }
};
}
