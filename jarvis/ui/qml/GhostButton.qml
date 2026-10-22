import QtQuick
import QtQuick.Controls

Button {
    id: root
    property bool primary: false
    property bool danger: false
    property bool selected: false      // shown filled, e.g. the active choice in a segmented row
    property bool compact: false
    readonly property bool filled: primary || selected

    implicitHeight: compact ? 26 : 34
    leftPadding: compact ? 10 : 16
    rightPadding: compact ? 10 : 16
    focusPolicy: Qt.StrongFocus
    Accessible.role: Accessible.Button
    Accessible.name: root.text

    contentItem: Text {
        text: root.text
        color: root.filled ? Theme.bg : (root.danger ? Theme.violet : Theme.cyan)
        font.family: Theme.sans
        font.pixelSize: 12
        font.weight: Font.DemiBold
        font.letterSpacing: 1.5
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
    }
    background: Rectangle {
        radius: 8
        color: root.filled ? (root.down ? Theme.violet : (root.hovered ? Theme.ice : (root.danger ? Theme.violet : Theme.cyan)))
                           : (root.down ? "#1A2230" : (root.hovered ? "#111821" : "transparent"))
        border.width: root.visualFocus ? 2 : 1
        border.color: root.visualFocus ? Theme.ice : (root.filled ? "transparent" : (root.danger ? Theme.violet : Theme.cyan))
    }
}
