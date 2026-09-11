cmake_minimum_required(VERSION 3.20)
project(phds_gegi_driver)

if(POLICY CMP0167)
    cmake_policy(SET CMP0167 NEW)
endif()

# CMAKE PARAMETERS
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# --- Prism messaging (vendored submodule, deps/prism) -----------------------
# Build only what this driver needs: the NATS transport (the default
# --protocol), no examples/tests/bindings from the Prism tree itself.
set(PSM_BUILD_NATS ON CACHE BOOL "Build NATS transport" FORCE)
set(PSM_BUILD_ICE OFF CACHE BOOL "Build Ice transport" FORCE)
set(PSM_BUILD_DDS OFF CACHE BOOL "Build DDS transport" FORCE)
set(PSM_BUILD_ZEROMQ OFF CACHE BOOL "Build ZeroMQ transport" FORCE)
set(PSM_BUILD_EXAMPLES OFF CACHE BOOL "Build example applications" FORCE)
set(PSM_BUILD_TESTS OFF CACHE BOOL "Build unit tests" FORCE)
set(PSM_BUILD_PYTHON_BINDINGS OFF CACHE BOOL "Build Python bindings" FORCE)
set(PSM_BUILD_CSHARP_BINDINGS OFF CACHE BOOL "Build C# bindings" FORCE)

add_subdirectory(deps/prism)

find_package(Boost REQUIRED COMPONENTS thread regex)

# INCLUDES
include_directories(
		include
		${Boost_INCLUDE_DIRS}
)

# LIBRARIES
add_library(tcp_event_reader src/phds_gegi_driver/socket_comms/tcp_event_reader.cpp)
target_link_libraries(tcp_event_reader ${Boost_THREAD_LIBRARY} ${Boost_REGEX_LIBRARY})

add_library(${PROJECT_NAME} src/phds_gegi_driver/prism_phds_gegi_driver/phds_gegi_driver.cpp)
target_link_libraries(${PROJECT_NAME} tcp_event_reader prism_messaging)

## Executables
add_executable(${PROJECT_NAME}_node src/phds_gegi_driver/prism_phds_gegi_driver/main.cpp)
target_link_libraries(${PROJECT_NAME}_node ${PROJECT_NAME} prism_messaging)

## Python nodes are launched directly with the system/venv Python
## interpreter (see README.md) — no catkin install step needed:
##   src/phds_gegi_driver/spectrum_node.py
##   src/phds_gegi_driver/spherical_heatmap_node.py
##   src/phds_gegi_driver/activity_node.py
##   src/phds_gegi_driver/data_recorder_node.py

## Unit tests (pure logic - no detector, no messaging server required).
## Run with:  bash test/run_tests.sh   (plain pytest, no catkin)

## Live-NATS Prism typed-messaging coverage test (EXCLUDE_FROM_ALL: build and
## run explicitly, since it requires a reachable nats-server). Proves that
## this driver's own JSON schemas are byte-compatible with the first-class
## psm::types registered in deps/prism (see docs/PRISM_MIGRATION.md).
##   cmake --build build --target test_prism_types_coverage
##   nats-server &
##   ./build/test_prism_types_coverage
enable_testing()
add_executable(test_prism_types_coverage EXCLUDE_FROM_ALL test/test_prism_types_coverage.cpp)
target_link_libraries(test_prism_types_coverage prism_messaging)
add_test(NAME prism_types_coverage COMMAND test_prism_types_coverage)
set_tests_properties(prism_types_coverage PROPERTIES SKIP_RETURN_CODE 77)
