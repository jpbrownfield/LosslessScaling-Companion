// SPDX-License-Identifier: MIT
#pragma once
#include <functional>
namespace eam { class AddonManager; }
namespace lsc {
bool StartManagedHost(eam::AddonManager* manager, void (*startPanel)(eam::AddonManager*),
                      std::function<void()> beforeAddons = {});
bool IsManagedHost();
bool IsHeadless();
void PrepareSm86ForAdapter(unsigned vendor);
void ObserveRealFrameAdapter(unsigned vendor);
bool AllowDispatchModule(void* module);
}
