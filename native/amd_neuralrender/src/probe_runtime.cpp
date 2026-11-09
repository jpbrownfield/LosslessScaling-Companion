// SPDX-License-Identifier: MIT
// Test fixture only: copies GPU textures. Never a model/inference implementation.
#define DLSS5NR_BUILD
#include "dlss5nr.h"
#include <d3d12.h>
#include <cstring>
struct dlss5nr_context{};
extern "C" {
DLSS5NR_API int dlss5nr_create(const dlss5nr_create_info*,dlss5nr_context** out){*out=new dlss5nr_context;return 0;}
DLSS5NR_API void dlss5nr_destroy(dlss5nr_context* ctx){delete ctx;}
DLSS5NR_API void dlss5nr_default_settings(dlss5nr_settings* s){*s={};s->struct_size=sizeof(*s);s->enabled=1;}
DLSS5NR_API void dlss5nr_set_settings(dlss5nr_context*,const dlss5nr_settings*){}
DLSS5NR_API int dlss5nr_process(dlss5nr_context*,const dlss5nr_frame* f){
    D3D12_RESOURCE_BARRIER b[2]{};
    b[0].Type=b[1].Type=D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
    b[0].Transition={f->input,0,D3D12_RESOURCE_STATES(f->input_state),D3D12_RESOURCE_STATE_COPY_SOURCE};
    b[1].Transition={f->output,0,D3D12_RESOURCE_STATES(f->output_state),D3D12_RESOURCE_STATE_COPY_DEST};
    f->command_list->ResourceBarrier(2,b);f->command_list->CopyResource(f->output,f->input);
    for(auto& barrier:b){auto state=barrier.Transition.StateBefore;barrier.Transition.StateBefore=barrier.Transition.StateAfter;barrier.Transition.StateAfter=state;}
    f->command_list->ResourceBarrier(2,b);return 0;
}
DLSS5NR_API void dlss5nr_present(dlss5nr_context*,ID3D12CommandQueue*){}
DLSS5NR_API void dlss5nr_get_status(dlss5nr_context*,dlss5nr_status* s){*s={};s->struct_size=sizeof(*s);s->state=DLSS5NR_STATE_RUNNING;
    strcpy_s(s->message,"GPU copy fixture only; no NR inference");}
}
