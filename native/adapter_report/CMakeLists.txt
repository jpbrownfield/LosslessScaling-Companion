cmake_minimum_required(VERSION 3.20)
project(LSC_AdapterReport VERSION 1.0.0 LANGUAGES CXX)

if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "LS Companion Adapter Report requires Windows x64")
endif()

add_library(LSC_AdapterReport SHARED
  src/report.cpp
  src/main.cpp
)
target_compile_features(LSC_AdapterReport PRIVATE cxx_std_20)
target_compile_definitions(LSC_AdapterReport PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)
target_include_directories(LSC_AdapterReport PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../vendor/losslessproxy-sdk/include
)
target_link_libraries(LSC_AdapterReport PRIVATE user32 dxgi)

if(MSVC)
  target_compile_options(LSC_AdapterReport PRIVATE /W4 /permissive- /EHsc /guard:cf)
  target_link_options(LSC_AdapterReport PRIVATE /guard:cf /DYNAMICBASE /NXCOMPAT)
endif()

set(BUNDLE_DIRECTORY "${CMAKE_SOURCE_DIR}/../../build/native/LSC-AdapterReport")
set_target_properties(LSC_AdapterReport PROPERTIES
  OUTPUT_NAME "LSC_AdapterReport"
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${BUNDLE_DIRECTORY}"
  LIBRARY_OUTPUT_DIRECTORY_RELEASE "${BUNDLE_DIRECTORY}"
)
add_custom_command(TARGET LSC_AdapterReport POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${CMAKE_CURRENT_SOURCE_DIR}/addon.json"
    "$<TARGET_FILE_DIR:LSC_AdapterReport>/addon.json"
)

# Off-LS fixture: proves the reporter end-to-end (real DLL entry point,
# real IHost vtable, real D3D11 device) without running Lossless Scaling.
add_executable(LSC_AdapterReportHost src/report_host.cpp)
target_compile_features(LSC_AdapterReportHost PRIVATE cxx_std_20)
target_compile_definitions(LSC_AdapterReportHost PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)
target_include_directories(LSC_AdapterReportHost PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../vendor/losslessproxy-sdk/include
)
target_link_libraries(LSC_AdapterReportHost PRIVATE d3d11 dxgi user32)
if(MSVC)
  target_compile_options(LSC_AdapterReportHost PRIVATE /W4 /permissive- /EHsc)
endif()
set_target_properties(LSC_AdapterReportHost PROPERTIES
  OUTPUT_NAME "LSC_AdapterReportHost"
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${BUNDLE_DIRECTORY}"
)
