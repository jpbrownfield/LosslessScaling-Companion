#include "pacing_probe.h"
#include <cstdio>
#include <limits>

int main() {
    lsc::PacingProbe probe;
    double now=1;
    for(int frame=0;frame<100;++frame) {probe.Observe(now,now+0.25);now+=10;}
    auto snapshot=probe.Snapshot();
    if(snapshot.intervals!=99||snapshot.medianMs!=10||snapshot.relativeStalls!=0)return 1;
    now+=40;probe.Observe(now,now+0.25);
    snapshot=probe.Snapshot();
    if(snapshot.relativeStalls!=1||snapshot.maximumMs!=50)return 2;
    for(int frame=0;frame<600;++frame) {now+=20;probe.Observe(now,now+0.5);}
    snapshot=probe.Snapshot();
    if(snapshot.intervals!=512||snapshot.medianMs!=20||snapshot.relativeStalls!=0)return 3;
    probe.Observe(std::numeric_limits<double>::quiet_NaN(),now);
    probe.Observe(now+10,now);
    if(probe.Snapshot().intervals!=512)return 4;
    std::puts("LS relative pacing: steady cadence, injected stall, bounded recovery, rate change, invalid samples PASS");
    return 0;
}