cmake_minimum_required(VERSION 3.20)
project(LSC_ReShadeBridge VERSION 1.0.0 LANGUAGES CXX)

if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "LS Companion ReShade Bridge requires Windows x64")
endif()

add_library(LSC_ReShadeBridge SHARED
  src/bridge.cpp
  src/main.cpp
  src/window_target.cpp
)
target_compile_features(LSC_ReShadeBridge PRIVATE cxx_std_20)
target_compile_definitions(LSC_ReShadeBridge PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)
target_include_directories(LSC_ReShadeBridge PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/src
  ${CMAKE_CURRENT_SOURCE_DIR}/../vendor/losslessproxy-sdk/include
)
target_link_libraries(LSC_ReShadeBridge PRIVATE user32)

if(MSVC)
  target_compile_options(LSC_ReShadeBridge PRIVATE /W4 /permissive- /EHsc /guard:cf)
  target_link_options(LSC_ReShadeBridge PRIVATE /guard:cf /DYNAMICBASE /NXCOMPAT)
endif()

set(BUNDLE_DIRECTORY "${CMAKE_SOURCE_DIR}/../../build/native/LSP-ReShade")
set_target_properties(LSC_ReShadeBridge PROPERTIES
  OUTPUT_NAME "LSC_ReShadeBridge"
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${BUNDLE_DIRECTORY}"
  LIBRARY_OUTPUT_DIRECTORY_RELEASE "${BUNDLE_DIRECTORY}"
)
add_custom_command(TARGET LSC_ReShadeBridge POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${CMAKE_CURRENT_SOURCE_DIR}/addon.json"
    "$<TARGET_FILE_DIR:LSC_ReShadeBridge>/addon.json"
)
