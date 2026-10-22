import QtQuick
import QtQuick.Controls

// Keyboard- and screen-reader-friendly switch. `checked` is driven by the backend; clicks emit clicked().
AbstractButton {
    id: root
    property string caption: ""

    checkable: false
    focusPolicy: Qt.StrongFocus
    implicitHeight: 34
    Accessible.role: Accessible.CheckBox
    Accessible.name: root.caption
    Accessible.checked: root.checked

    contentItem: Row {
        spacing: 12
        Rectangle {
            width: 40
            height: 22
            radius: 11
            anchors.verticalCenter: parent.verticalCenter
            color: root.checked ? Theme.cyan : "#1E242C"
            border.width: root.visualFocus ? 2 : 1
            border.color: root.visualFocus ? Theme.ice : (root.checked ? Theme.cyan : Theme.muted)
            Rectangle {
                width: 16
                height: 16
                radius: 8
                y: 3
                x: root.checked ? 21 : 3
                color: root.checked ? Theme.bg : Theme.muted
                Behavior on x { NumberAnimation { duration: 120 } }
            }
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.caption
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 13
        }
    }
}
