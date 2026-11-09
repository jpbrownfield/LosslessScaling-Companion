// SPDX-License-Identifier: MIT
#include "eam/addon_exports.h"
static IHost* host=nullptr;
static unsigned calls=0;
static bool Frame(const EamRealFrame&,void*){++calls;return true;}
EAM_EXPORT bool Register(IHost* h,bool required){host=h;bool ok=h->SetRealFrameCallback(Frame,nullptr);if(ok)h->RequireRealFrameProcessing(required);return ok;}
EAM_EXPORT void Remove(){if(host){host->RequireRealFrameProcessing(false);volatile bool removed=host->SetRealFrameCallback(nullptr,nullptr);(void)removed;}}
EAM_EXPORT unsigned Calls(){return calls;}
