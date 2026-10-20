import QtQuick
import DiscoveryCanvas 1.0

Item {
    id: root
    anchors.fill: parent
    property color backgroundColor: "#ffffff"
    property real viewportFrameX: 0
    property real viewportFrameY: 0
    property real viewportFrameWidth: width
    property real viewportFrameHeight: height
    property bool viewportFrameActive: false

    Rectangle {
        anchors.fill: parent
        color: root.backgroundColor
    }

    LassoPaintItem {
        id: lassoFillItem
        objectName: "lassoFillItem"
        anchors.fill: parent
        z: 0.5
    }

    ScatterPaintItem {
        id: scatterItem
        objectName: "scatterItem"
        anchors.fill: parent
        property bool draw_background: false
        z: 1
    }

    LassoPaintItem {
        id: lassoOverlayItem
        objectName: "lassoOverlayItem"
        anchors.fill: parent
        z: 100
    }

    Rectangle {
        id: zoomDragItem
        objectName: "zoomDragItem"
        property real rectX: 0
        property real rectY: 0
        property real rectWidth: 0
        property real rectHeight: 0
        x: rectX
        y: rectY
        width: rectWidth
        height: rectHeight
        visible: rectWidth > 0 && rectHeight > 0
        color: "transparent"
        border.color: "#3182ce"
        border.width: 1
        z: 1000
    }

    Rectangle {
        id: findBlinkItem
        objectName: "findBlinkItem"
        property real centerX: 0
        property real centerY: 0
        property real phase: 0
        property bool active: false
        width: 28 + (6 * Math.sin(phase) * Math.sin(phase))
        height: width
        x: centerX - width / 2
        y: centerY - height / 2
        radius: width / 2
        visible: active
        color: "transparent"
        border.color: "#f59e0b"
        border.width: 3
        opacity: 0.45 + (0.45 * Math.sin(phase) * Math.sin(phase))
        z: 1010
    }

    Rectangle {
        id: selectionItem
        objectName: "selectionItem"
        property real centerX: 0
        property real centerY: 0
        property bool active: false
        width: 26
        height: 26
        x: centerX - width / 2
        y: centerY - height / 2
        radius: width / 2
        visible: active
        color: "#2a3b82f6"
        border.color: "#2563eb"
        border.width: 3
        z: 1011
    }

    Item {
        anchors.fill: parent
        visible: root.viewportFrameActive
        z: 2000

        Rectangle {
            x: 0
            y: 0
            width: root.viewportFrameX
            height: parent.height
            color: "#80000000"
        }
        Rectangle {
            x: root.viewportFrameX + root.viewportFrameWidth
            y: 0
            width: Math.max(0, parent.width - x)
            height: parent.height
            color: "#80000000"
        }
        Rectangle {
            x: root.viewportFrameX
            y: 0
            width: root.viewportFrameWidth
            height: root.viewportFrameY
            color: "#80000000"
        }
        Rectangle {
            x: root.viewportFrameX
            y: root.viewportFrameY + root.viewportFrameHeight
            width: root.viewportFrameWidth
            height: Math.max(0, parent.height - y)
            color: "#80000000"
        }
    }
}
