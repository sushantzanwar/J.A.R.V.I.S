import QtQuick

Rectangle {
    id: root
    property string title: ""
    default property alias content: body.data
    property alias headerContent: headerArea.data

    color: Theme.panel
    radius: 10
    border.color: Theme.border
    border.width: 1

    Accessible.role: Accessible.Pane
    Accessible.name: root.title

    Rectangle {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 14
        anchors.rightMargin: 14
        height: 1
        color: Theme.cyan
        opacity: 0.4
    }

    Text {
        id: heading
        x: 16
        y: 13
        text: root.title.toUpperCase()
        color: Theme.cyan
        font.family: Theme.sans
        font.pixelSize: 11
        font.weight: Font.DemiBold
        font.letterSpacing: 2
    }

    Item {
        id: headerArea
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: 6
        anchors.rightMargin: 10
        height: 26
        width: childrenRect.width
    }

    Item {
        id: body
        anchors.margins: 16
        anchors.topMargin: 12
        anchors.top: heading.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
    }
}
