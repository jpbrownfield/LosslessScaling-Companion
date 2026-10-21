cmake_minimum_required(VERSION 3.20)
project(LSC_StartupGuard LANGUAGES CXX)
if(NOT WIN32 OR NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
  message(FATAL_ERROR "Startup Guard requires Windows x64")
endif()
add_library(LSC_StartupGuard SHARED startup_guard.cpp)
target_compile_features(LSC_StartupGuard PRIVATE cxx_std_17)
target_compile_definitions(LSC_StartupGuard PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)
target_link_libraries(LSC_StartupGuard PRIVATE user32 comctl32 dwmapi)
set_property(TARGET LSC_StartupGuard PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
if(MSVC)
  target_compile_options(LSC_StartupGuard PRIVATE /W4 /permissive- /guard:cf)
  target_link_options(LSC_StartupGuard PRIVATE /guard:cf /DYNAMICBASE /NXCOMPAT)
endif()
set_target_properties(LSC_StartupGuard PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/../../build/native")
add_executable(startup_guard_test_host WIN32 test_host.cpp)
target_compile_features(startup_guard_test_host PRIVATE cxx_std_17)
target_compile_definitions(startup_guard_test_host PRIVATE UNICODE _UNICODE WIN32_LEAN_AND_MEAN NOMINMAX)
target_link_libraries(startup_guard_test_host PRIVATE user32 dwmapi)
set_property(TARGET startup_guard_test_host PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
set_target_properties(startup_guard_test_host PROPERTIES OUTPUT_NAME LosslessScaling
  RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/../../build/native/startup-guard-test")

find_program(CSC_COMPILER csc.exe PATHS "$ENV{WINDIR}/Microsoft.NET/Framework64/v4.0.30319" NO_DEFAULT_PATH)
if(CSC_COMPILER)
  get_filename_component(FRAMEWORK_DIRECTORY "${CSC_COMPILER}" DIRECTORY)
  set(WPF_HOST_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../../build/native/startup-guard-wpf-test")
  file(TO_NATIVE_PATH "${WPF_HOST_DIRECTORY}" WPF_HOST_NATIVE)
  file(TO_NATIVE_PATH "${FRAMEWORK_DIRECTORY}" FRAMEWORK_NATIVE)
  add_custom_target(startup_guard_wpf_test_host ALL
    COMMAND ${CMAKE_COMMAND} -E make_directory "${WPF_HOST_DIRECTORY}"
    COMMAND "${CSC_COMPILER}" /nologo /target:winexe /platform:x64
      "/out:${WPF_HOST_NATIVE}\\LosslessScaling.exe"
      "/reference:${FRAMEWORK_NATIVE}\\WPF\\PresentationFramework.dll"
      "/reference:${FRAMEWORK_NATIVE}\\WPF\\PresentationCore.dll"
      "/reference:${FRAMEWORK_NATIVE}\\WPF\\WindowsBase.dll"
      "/reference:${FRAMEWORK_NATIVE}\\System.Xaml.dll"
      wpf_test_host.cs
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    VERBATIM)
endif()
