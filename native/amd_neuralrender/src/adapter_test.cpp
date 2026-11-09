// SPDX-License-Identifier: MIT
#include "../companion_adapter.h"
#include <cstring>
#include <iostream>
#include <stdexcept>

int main(){try {
    // Deliberately different enumeration indices, device tokens and identities.
    const uint64_t luids[]={0xabc00000001ull,0xdef00000002ull,0xabc00000003ull};
    const int devices[]={17,42,91};
    auto count=[](int* out){*out=3;return 0;};
    auto get=[&](int* out,int ordinal){if(ordinal<0||ordinal>=3)return 1;*out=devices[ordinal];return 0;};
    auto luid=[&](char* out,unsigned* mask,int device){for(int i=0;i<3;++i)if(device==devices[i]){memcpy(out,&luids[i],8);*mask=1;return 0;}return 1;};
    for(int desired:{2,1,0}) {
        lsc_amd::AdapterSelection selected;std::string error;
        if(!lsc_amd::SelectAdapter(luids[desired],count,get,luid,selected,error)||selected.ordinal!=desired||selected.device!=devices[desired])
            throw std::runtime_error("Failed to select the requested non-primary GPU: "+error);
    }
    lsc_amd::AdapterSelection selected;std::string error;
    if(lsc_amd::SelectAdapter(0x99999999,count,get,luid,selected,error)||selected.device!=-1)
        throw std::runtime_error("Unavailable GPU must not fall back to GPU zero");
    auto missing=[](char*,unsigned*,int){return 1;};
    if(lsc_amd::SelectAdapter(luids[1],count,get,missing,selected,error))throw std::runtime_error("Missing identity accepted");
    std::cout<<"Adapter selection passed: all three requested GPUs select their matching CUDA token, including ordinals 1 and 2.\n";return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
