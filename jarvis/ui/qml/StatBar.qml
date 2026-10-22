import QtQuick

Item {
    id: root
    property string label: ""
    property string detail: ""
    property real value: 0   // 0..1

    implicitHeight: 42

    Text {
        text: root.label
        color: Theme.text
        font.family: Theme.sans
        font.pixelSize: 13
    }
    Text {
        anchors.right: parent.right
        text: root.detail
        color: Theme.muted
        font.family: Theme.mono
        font.pixelSize: 12
    }
    Rectangle {
        id: track
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 6
        radius: 3
        color: "#161A20"

        Rectangle {
            height: parent.height
            radius: 3
            width: Math.max(0, Math.min(1, root.value)) * track.width
            color: root.value > 0.85 ? Theme.violet : Theme.cyan
            Behavior on width { NumberAnimation { duration: 600; easing.type: Easing.OutCubic } }
            Behavior on color { ColorAnimation { duration: 300 } }
        }
    }
}
