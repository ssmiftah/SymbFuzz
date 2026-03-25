cmake_minimum_required(VERSION 3.16)
project(SymbFuzz VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# ---- Build type --------------------------------------------------------
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

set(CMAKE_CXX_FLAGS_RELEASE "-O3 -DNDEBUG")
set(CMAKE_CXX_FLAGS_DEBUG   "-g -O0 -fsanitize=address,undefined")

message(STATUS "Build type : ${CMAKE_BUILD_TYPE}")

# ---- Z3 ----------------------------------------------------------------
find_library(Z3_LIB z3 REQUIRED
    HINTS /usr/lib /usr/local/lib ENV LD_LIBRARY_PATH)
find_path(Z3_INCLUDE_DIR z3++.h
    HINTS /usr/include /usr/local/include
    PATH_SUFFIXES z3
    REQUIRED)
message(STATUS "Z3 library : ${Z3_LIB}")
message(STATUS "Z3 includes: ${Z3_INCLUDE_DIR}")

# ---- Sources -----------------------------------------------------------
set(SOURCES
    src/frontend/yosys_driver.cpp
    src/frontend/smt2_parser.cpp
    src/bmc/smt2_builder.cpp
    src/bmc/bmc_engine.cpp
    src/bmc/result_parser.cpp
    src/solver/z3_solver.cpp
    src/utils/bitvec_utils.cpp
    src/main.cpp
)

add_executable(symbfuzz ${SOURCES})

target_include_directories(symbfuzz PRIVATE
    include
    ${Z3_INCLUDE_DIR}
)

target_link_libraries(symbfuzz PRIVATE ${Z3_LIB})

# Warnings
target_compile_options(symbfuzz PRIVATE -Wall -Wextra -Wpedantic)

# ---- Install -----------------------------------------------------------
include(GNUInstallDirs)

# Binary → <prefix>/bin/symbfuzz
install(TARGETS symbfuzz
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)

# Python package → editable install via pip (optional, requires pip)
find_program(PIP_EXE pip3 pip NAMES pip3 pip DOC "pip executable")
if(PIP_EXE)
    install(CODE "
        message(STATUS \"Installing Python package via pip...\")
        execute_process(
            COMMAND \"${PIP_EXE}\" install --editable \"${CMAKE_SOURCE_DIR}\"
            RESULT_VARIABLE _pip_ret
        )
        if(NOT _pip_ret EQUAL 0)
            message(WARNING \"pip install failed (exit \${_pip_ret}). Install manually: pip install -e ${CMAKE_SOURCE_DIR}\")
        endif()
    ")
endif()

# ---- Summary -----------------------------------------------------------
message(STATUS "")
message(STATUS "== SymbFuzz ${PROJECT_VERSION} ==")
message(STATUS "  Build    : ${CMAKE_BUILD_TYPE}")
message(STATUS "  Binary   : symbfuzz")
message(STATUS "  Install  : ${CMAKE_INSTALL_PREFIX}/${CMAKE_INSTALL_BINDIR}/symbfuzz")
message(STATUS "")
message(STATUS "  Build  :  cmake --build <build-dir>")
message(STATUS "  Install:  cmake --install <build-dir>")
message(STATUS "")
