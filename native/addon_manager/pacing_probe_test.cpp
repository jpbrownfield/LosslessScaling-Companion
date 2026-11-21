#include "pacing_probe.h"
#include <cstdio>
#include <limits>
#include <chrono>
#include <mutex>

int main() {
    lsc::PacingProbe probe;
    double now=1;
    for(int frame=0;frame<100;++frame) {probe.Observe(now,now+0.25);now+=10;}
    auto snapshot=probe.Snapshot();
    if(snapshot.intervals!=99||snapshot.medianMs!=10||snapshot.relativeStalls!=0)return 1;
    now+=40;probe.Observe(now,now+0.25);
    snapshot=probe.Snapshot();
    if(snapshot.relativeStalls!=1||snapshot.maximumMs!=50)return 2;
    for(int frame=0;frame<2200;++frame) {now+=20;probe.Observe(now,now+0.5);}
    snapshot=probe.Snapshot();
    if(snapshot.intervals!=2048||snapshot.medianMs!=20||snapshot.relativeStalls!=0)return 3;
    probe.Observe(std::numeric_limits<double>::quiet_NaN(),now);
    probe.Observe(now+10,now);
    if(probe.Snapshot().intervals!=2048)return 4;
    lsc::PacingWindows windows;
    for(int frame=0;frame<402;++frame)windows.Observe(1+frame*10,1+frame*10+.1);
    auto completed=windows.Snapshot();
    if(completed.sequence!=2||completed.startedMs!=2001||completed.endedMs!=4001||completed.intervals!=199)return 5;
    if(windows.Snapshot().sequence!=completed.sequence)return 6; // repeated readers cannot create evidence
    windows.Reset();
    if(windows.Snapshot().intervals!=0)return 7;
    for(int frame=0;frame<5002;++frame)windows.Observe(1+frame*.5,1+frame*.5+.1);
    if(!windows.Snapshot().overflow)return 8;
    lsc::PacingWindows dropped;
    for(int frame=0;frame<202;++frame)dropped.Observe(1+frame*10,1+frame*10+.1,frame>=50?1:0);
    if(dropped.Snapshot().dropped!=1)return 10;
    for(int frame=202;frame<402;++frame)dropped.Observe(1+frame*10,1+frame*10+.1,1);
    if(dropped.Snapshot().dropped!=0)return 11;
    // CPU-only upper-level collection cost including try-lock, QPC-equivalent
    // clock reads and the once-per-window fixed-buffer copy. No GPU workload.
    std::mutex gate;lsc::PacingWindows performance;
    const auto start=std::chrono::steady_clock::now();
    for(int frame=0;frame<100000;++frame) {
        const auto entered=std::chrono::steady_clock::now();
        std::unique_lock lock(gate,std::try_to_lock);
        if(lock.owns_lock())performance.Observe(1+frame*10,1+frame*10+.1);
        const auto returned=std::chrono::steady_clock::now();
        (void)entered;(void)returned;
    }
    const auto cost=std::chrono::duration<double,std::micro>(std::chrono::steady_clock::now()-start).count()/100000;
    std::printf("Collection CPU microbenchmark: %.4f us/observation (not a live GPU overhead verdict)\n",cost);
    if(cost>50.)return 9;
    std::puts("LS relative pacing: steady cadence, injected stall, bounded recovery, rate change, invalid samples PASS");
    return 0;
}