cmake_minimum_required(VERSION 3.21)
project(discovery_renderer LANGUAGES CXX)

set(EIDORA_PYSIDE6_QT_LIB_DIR "" CACHE PATH "PySide6 Qt/lib directory used to load the plugin")
set(EIDORA_PYSIDE6_QT_VERSION "" CACHE STRING "Exact Qt runtime version shipped by PySide6")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Quick QuickWidgets Gui GuiPrivate Concurrent ShaderTools)

if(EIDORA_PYSIDE6_QT_VERSION AND NOT Qt6Core_VERSION VERSION_EQUAL EIDORA_PYSIDE6_QT_VERSION)
    message(FATAL_ERROR
        "Qt SDK ${Qt6Core_VERSION} does not match the PySide6 runtime ${EIDORA_PYSIDE6_QT_VERSION}. "
        "Build the discovery renderer with the exact matching Qt SDK."
    )
endif()

add_library(discovery_renderer MODULE
    discovery_render_item.cpp
    discovery_render_item.h
    discovery_lasso_overlay_item.cpp
    discovery_lasso_overlay_item.h
    discovery_pin_overlay_item.cpp
    discovery_pin_overlay_item.h
    discovery_renderer_plugin.cpp
)

qt_add_shaders(discovery_renderer "discovery_renderer_shaders"
    PREFIX "/DiscoveryRenderer"
    FILES
        shaders/point_marker.vert
        shaders/point_marker_quad.vert
        shaders/point_marker.frag
        shaders/thumbnail_atlas.vert
        shaders/thumbnail_atlas.frag
)

target_link_libraries(
    discovery_renderer
    PRIVATE
        Qt6::Quick
        Qt6::QuickWidgets
        Qt6::Gui
        Qt6::GuiPrivate
        Qt6::Concurrent
)

set_target_properties(discovery_renderer PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/DiscoveryRenderer"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/DiscoveryRenderer"
)

add_custom_command(TARGET discovery_renderer POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E make_directory "$<TARGET_FILE_DIR:discovery_renderer>"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
        "${CMAKE_CURRENT_SOURCE_DIR}/qml/DiscoveryRenderer/qmldir"
        "$<TARGET_FILE_DIR:discovery_renderer>/qmldir"
)

if(APPLE AND EIDORA_PYSIDE6_QT_LIB_DIR)
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_custom_command(TARGET discovery_renderer POST_BUILD
        COMMAND "${Python3_EXECUTABLE}"
            "${CMAKE_CURRENT_SOURCE_DIR}/patch_macos_qt_rpaths.py"
            --library "$<TARGET_FILE:discovery_renderer>"
            --qt-lib-dir "${EIDORA_PYSIDE6_QT_LIB_DIR}"
        COMMENT "Binding discovery renderer to the PySide6 Qt runtime"
        VERBATIM
    )
endif()
