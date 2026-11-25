# SealPIR benchmark driver. Compiles SealPIR's library sources (pir*.cpp, unmodified, from the
# extracted source archive) with the same flags as the driver and links the project-local SEAL 4.0.0.
cmake_minimum_required(VERSION 3.13)
project(sealpir_bench LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

set(SEALPIR_SOURCE_DIR "" CACHE PATH "Extracted SealPIR source tree")
set(SEALPIR_COMMIT "unknown" CACHE STRING "SealPIR commit SHA of the source archive")
if(NOT EXISTS "${SEALPIR_SOURCE_DIR}/src/pir.cpp")
  message(FATAL_ERROR "SEALPIR_SOURCE_DIR does not contain src/pir.cpp: ${SEALPIR_SOURCE_DIR}")
endif()

find_package(SEAL 4.0.0 EXACT REQUIRED)

add_library(sealpir_lib STATIC
  ${SEALPIR_SOURCE_DIR}/src/pir.cpp
  ${SEALPIR_SOURCE_DIR}/src/pir_client.cpp
  ${SEALPIR_SOURCE_DIR}/src/pir_server.cpp)
target_include_directories(sealpir_lib PUBLIC ${SEALPIR_SOURCE_DIR}/src)
target_link_libraries(sealpir_lib PUBLIC SEAL::seal)

add_executable(sealpir_bench sealpir_bench.cpp)
target_link_libraries(sealpir_bench PRIVATE sealpir_lib)
string(TOUPPER "${CMAKE_BUILD_TYPE}" _bt)
target_compile_definitions(sealpir_bench PRIVATE
  SEALPIR_COMMIT="${SEALPIR_COMMIT}"
  BENCH_CXX_FLAGS="${CMAKE_CXX_FLAGS} ${CMAKE_CXX_FLAGS_${_bt}}")

# Calibration: SEAL primitive costs with this SEAL 4.0.0 build (compare with seal_ops_bench.py).
add_executable(seal_ops_bench seal_ops_bench.cpp)
target_link_libraries(seal_ops_bench PRIVATE SEAL::seal)
