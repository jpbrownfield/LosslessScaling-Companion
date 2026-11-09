// SPDX-License-Identifier: MIT
#pragma once
namespace eam { class AddonManager; }
namespace lsc {
bool StartHeadless(eam::AddonManager* manager);
bool IsHeadless();
void PrepareSm86ForAdapter(unsigned vendor);
void ObserveRealFrameAdapter(unsigned vendor);
bool AllowDispatchModule(void* module);
}
