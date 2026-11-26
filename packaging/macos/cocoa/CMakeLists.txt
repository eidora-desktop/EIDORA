cmake_minimum_required(VERSION 3.24)
project(EidoraQtCocoa VERSION 6.11.2 LANGUAGES C CXX OBJCXX)

# Build Qt's own platform target against the same SDK as the PySide runtime.
set(QT_SKIP_AUTO_PLUGIN_INCLUSION ON)
find_package(Qt6 ${PROJECT_VERSION} EXACT REQUIRED COMPONENTS Core CorePrivate Gui GuiPrivate BuildInternals)
qt_build_repo_begin()
foreach(framework IN ITEMS Foundation AppKit Carbon CoreServices CoreVideo IOKit IOSurface Metal QuartzCore UniformTypeIdentifiers)
    find_library(FW${framework} ${framework} REQUIRED)
endforeach()

include(FetchContent)
# A changed backport must start from pristine sources, including in reused builds.
set(patch_file "${CMAKE_CURRENT_SOURCE_DIR}/accessibility-lifetime.patch")
file(SHA256 "${patch_file}" patch_hash)
set(FETCHCONTENT_BASE_DIR "${CMAKE_CURRENT_BINARY_DIR}/_deps/${patch_hash}")
FetchContent_Declare(qtbase_sources
    URL https://download.qt.io/archive/qt/6.11/6.11.2/submodules/qtbase-everywhere-src-6.11.2.tar.xz
    URL_HASH SHA256=5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    TLS_VERIFY ON
    # Only the Cocoa plugin is needed from the archive.
    SOURCE_SUBDIR src/plugins/platforms/cocoa
    PATCH_COMMAND /usr/bin/patch -p1 -F0 --forward -i "${patch_file}"
)
FetchContent_MakeAvailable(qtbase_sources)
set_target_properties(QCocoaIntegrationPlugin PROPERTIES
    BUILD_WITH_INSTALL_RPATH ON
    INSTALL_RPATH "@loader_path/../../lib"
)

install(TARGETS QCocoaIntegrationPlugin LIBRARY DESTINATION platforms COMPONENT EIDORACocoa)
install(FILES CMakeLists.txt NOTICE.txt accessibility-lifetime.patch
    "${qtbase_sources_SOURCE_DIR}/LICENSES/LGPL-3.0-only.txt"
    "${qtbase_sources_SOURCE_DIR}/LICENSES/GPL-3.0-only.txt"
    DESTINATION licenses/qt-cocoa COMPONENT EIDORACocoa)
