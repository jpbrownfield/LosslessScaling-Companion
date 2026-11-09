// SPDX-License-Identifier: MIT
#include "../companion_adapter.h"
extern "C" __declspec(dllexport) unsigned lsc_dlss5nr_adapter_selection_version(){return 1;}
extern "C" __declspec(dllexport) int lsc_dlss5nr_selected_cuda_ordinal(){return lsc_amd::selectedOrdinal.load();}
extern "C" __declspec(dllexport) uint64_t lsc_dlss5nr_selected_adapter_luid(){return lsc_amd::selectedLuid.load();}
