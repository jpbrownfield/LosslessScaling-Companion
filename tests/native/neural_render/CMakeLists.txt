cmake_minimum_required(VERSION 3.20)
project(LSC_NRProbe LANGUAGES CXX)
if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "The NR probe requires Windows x64")
endif()
if(NOT LSC_UPSTREAM_SOURCE OR NOT LSC_IMGUI_SOURCE)
  message(FATAL_ERROR "Supply the pinned host source and its pinned ImGui checkout")
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
add_executable(LSC_NRProbe probe.cpp
  "${LSC_IMGUI_SOURCE}/imgui.cpp" "${LSC_IMGUI_SOURCE}/imgui_draw.cpp"
  "${LSC_IMGUI_SOURCE}/imgui_tables.cpp" "${LSC_IMGUI_SOURCE}/imgui_widgets.cpp")
target_include_directories(LSC_NRProbe PRIVATE "${LSC_IMGUI_SOURCE}"
  "${LSC_UPSTREAM_SOURCE}/manager/sdk/include" "${LSC_UPSTREAM_SOURCE}/manager/third_party")
target_compile_definitions(LSC_NRProbe PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)
target_link_libraries(LSC_NRProbe PRIVATE d3d11 dxgi)
