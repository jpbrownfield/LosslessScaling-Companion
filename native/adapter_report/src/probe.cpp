// Lightweight, isolated DXGI snapshot: never loads LS or creates a D3D device.
#include "report.h"
#include <cstdio>

int main() {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    try {
        const auto report = lsc::CollectAdapterSnapshot();
        puts(report.c_str());
        return 0;
    } catch (...) {
        return 1;
    }
}
