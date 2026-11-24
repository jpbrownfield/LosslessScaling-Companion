// SPDX-License-Identifier: MIT
#include "eam/addon_exports.h"
#include <atomic>
static IHost* host=nullptr;
static unsigned calls=0;
static std::atomic<bool> failing{false};
static bool Frame(const EamRealFrame&,void*){++calls;return !failing;}
EAM_EXPORT bool Register(IHost* h,bool required){host=h;bool ok=h->SetRealFrameCallback(Frame,nullptr);if(ok)h->RequireRealFrameProcessing(required);return ok;}
EAM_EXPORT void Remove(){if(host){host->RequireRealFrameProcessing(false);volatile bool removed=host->SetRealFrameCallback(nullptr,nullptr);(void)removed;}}
EAM_EXPORT unsigned Calls(){return calls;}
EAM_EXPORT void Fail(bool value){failing=value;}
EAM_EXPORT void Require(bool value){if(host)host->RequireRealFrameProcessing(value);}
