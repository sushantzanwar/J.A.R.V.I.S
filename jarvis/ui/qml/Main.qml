import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: win
    objectName: "mainWindow"
    visible: true
    width: 1360
    height: 840
    minimumWidth: 1040
    minimumHeight: 660
    title: "J.A.R.V.I.S"
    color: Theme.bg

    property int tab: 0   // 0 = tasks, 1 = timeline

    function openSettings() { settings.open() }
    function askClear() { confirmClear.open() }
    function gb(v) { return v.toFixed(1) + " GB" }
    function workerColor(s) { return s === "active" ? Theme.cyan : (s === "paused" || s === "stale" ? Theme.violet : Theme.muted) }

    Shortcut { sequence: "Ctrl+L"; onActivated: input.forceActiveFocus() }
    Shortcut { sequence: "Ctrl+,"; onActivated: win.openSettings() }
    Shortcut { sequence: "Ctrl+1"; onActivated: win.tab = 0 }
    Shortcut { sequence: "Ctrl+2"; onActivated: win.tab = 1 }
    Shortcut { sequence: "Ctrl+Space"; onActivated: if (backend) backend.toggleVoiceRecording() }

    Settings {
        id: settings
        onClearRequested: win.askClear()
    }

    Popup {
        id: confirmClear
        objectName: "confirmClear"
        modal: true
        focus: true
        anchors.centerIn: Overlay.overlay
        width: 420
        padding: 20
        closePolicy: Popup.CloseOnEscape
        Overlay.modal: Rectangle { color: "#D0000000" }
        background: Rectangle { color: Theme.panel; radius: 12; border.width: 1; border.color: Theme.violet }
        onOpened: cancelClear.forceActiveFocus()
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                Layout.fillWidth: true
                text: "Delete all saved conversation history on this PC? This cannot be undone."
                wrapMode: Text.Wrap
                color: Theme.text
                font.family: Theme.sans
                font.pixelSize: 14
                Accessible.role: Accessible.AlertMessage
                Accessible.name: text
            }
            RowLayout {
                spacing: 8
                Item { Layout.fillWidth: true }
                GhostButton { id: cancelClear; objectName: "cancelClear"; text: "CANCEL"; onClicked: confirmClear.close() }
                GhostButton {
                    objectName: "confirmClearYes"
                    text: "DELETE"
                    danger: true
                    primary: true
                    onClicked: { backend.clearHistory(); confirmClear.close() }
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 20
        spacing: 14

        // ---- top bar
        RowLayout {
            Layout.fillWidth: true
            spacing: 18
            Text {
                text: "J.A.R.V.I.S"
                color: Theme.ice
                font.family: Theme.sans
                font.pixelSize: 22
                font.weight: Font.Light
                font.letterSpacing: 8
                Accessible.role: Accessible.Heading
                Accessible.name: "Jarvis"
            }
            Rectangle { width: 1; height: 18; color: Theme.border }
            Text {
                objectName: "modeText"
                text: backend.providerState === "connected" ? "NATIVE  ·  MODEL: " + backend.providerName.toUpperCase()
                                                            : "NATIVE  ·  OFFLINE MODE"
                color: Theme.muted
                font.family: Theme.mono
                font.pixelSize: 11
                font.letterSpacing: 2
            }
            Item { Layout.fillWidth: true }
            GhostButton {
                objectName: "settingsButton"
                text: "SETTINGS"
                compact: true
                Accessible.name: "Open settings and diagnostics (Control comma)"
                onClicked: win.openSettings()
            }
            Text {
                id: clock
                color: Theme.cyan
                font.family: Theme.mono
                font.pixelSize: 14
                text: Qt.formatTime(new Date(), "HH:mm:ss")
                Timer {
                    interval: 1000; running: true; repeat: true
                    onTriggered: clock.text = Qt.formatTime(new Date(), "HH:mm:ss")
                }
            }
        }

        // ---- data notice (corrupt / unavailable / newer-schema local data)
        Rectangle {
            id: banner
            objectName: "storeBanner"
            visible: backend.storeNotice !== ""
            Layout.fillWidth: true
            Layout.preferredHeight: bannerRow.implicitHeight + 20
            radius: 8
            color: "#17102B"
            border.width: 1
            border.color: Theme.violet
            Accessible.role: Accessible.AlertMessage
            Accessible.name: backend.storeNotice
            RowLayout {
                id: bannerRow
                anchors.fill: parent
                anchors.margins: 10
                spacing: 12
                Text {
                    Layout.fillWidth: true
                    text: backend.storeNotice
                    wrapMode: Text.Wrap
                    color: Theme.text
                    font.family: Theme.sans
                    font.pixelSize: 13
                }
                GhostButton { text: "DISMISS"; compact: true; onClicked: backend.dismissNotice() }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 16

            // ---- left: system + engine
            ColumnLayout {
                Layout.preferredWidth: 290
                Layout.maximumWidth: 290
                Layout.fillHeight: true
                spacing: 16

                Panel {
                    title: "System"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 232
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 12
                        StatBar {
                            Layout.fillWidth: true
                            label: "CPU"
                            value: backend.status.cpu
                            detail: Math.round(backend.status.cpu * 100) + "%"
                            Accessible.name: "CPU " + Math.round(backend.status.cpu * 100) + " percent"
                        }
                        StatBar {
                            Layout.fillWidth: true
                            label: "Memory"
                            value: backend.status.ram
                            detail: win.gb(backend.status.ram_used_gb) + " / " + win.gb(backend.status.ram_total_gb)
                            Accessible.name: "Memory " + Math.round(backend.status.ram * 100) + " percent"
                        }
                        StatBar {
                            Layout.fillWidth: true
                            label: "Disk"
                            value: backend.status.disk
                            detail: Math.round(backend.status.disk_used_gb) + " / " + Math.round(backend.status.disk_total_gb) + " GB"
                            Accessible.name: "Disk " + Math.round(backend.status.disk * 100) + " percent"
                        }
                        Item { Layout.fillHeight: true }
                    }
                }

                Panel {
                    title: "Engine"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 10
                        Repeater {
                            model: [
                                { k: "Interface", v: "Qt Quick (native)", c: Theme.cyan },
                                { k: "Language model",
                                  v: backend.providerState === "connected" ? backend.providerName
                                     : (backend.providerState === "builtin" ? "offline (none)" : backend.providerState),
                                  c: backend.providerState === "connected" ? Theme.cyan
                                     : (backend.providerState === "builtin" ? Theme.muted : Theme.violet) },
                                { k: "Worker", v: backend.workerState.state, c: win.workerColor(backend.workerState.state) },
                                { k: "Model router", v: "not built", c: Theme.muted },
                                { k: "Task bridge", v: "local, approval-gated", c: Theme.cyan }
                            ]
                            delegate: RowLayout {
                                Layout.fillWidth: true
                                Accessible.role: Accessible.StaticText
                                Accessible.name: modelData.k + ": " + modelData.v
                                Rectangle { width: 7; height: 7; radius: 4; color: modelData.c }
                                Text {
                                    text: modelData.k
                                    color: Theme.text
                                    font.family: Theme.sans
                                    font.pixelSize: 13
                                }
                                Item { Layout.fillWidth: true }
                                Text {
                                    text: modelData.v
                                    color: Theme.muted
                                    font.family: Theme.mono
                                    font.pixelSize: 11
                                }
                            }
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }

            // ---- center: orb + chat
            ColumnLayout {
                Layout.fillWidth: true
                Layout.minimumWidth: 380
                Layout.fillHeight: true
                spacing: 8

                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 270
                    Orb {
                        anchors.centerIn: parent
                        width: 270
                        height: 270
                        mode: backend.orbMode
                        reduceMotion: backend.reduceMotion
                    }
                }
                Text {
                    Layout.alignment: Qt.AlignHCenter
                    text: backend.orbMode === "thinking" ? "PROCESSING" : (backend.orbMode === "speaking" ? "RESPONDING" : "STANDING BY")
                    color: Theme.cyan
                    font.family: Theme.mono
                    font.pixelSize: 11
                    font.letterSpacing: 4
                }

                Panel {
                    title: "Conversation"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    headerContent: GhostButton {
                        objectName: "clearButton"
                        text: "CLEAR"
                        compact: true
                        danger: true
                        Accessible.name: "Clear conversation history"
                        onClicked: win.askClear()
                    }

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 10

                        ListView {
                            id: chat
                            objectName: "chatList"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 8
                            model: backend.messages
                            onCountChanged: Qt.callLater(chat.positionViewAtEnd)
                            ScrollBar.vertical: ScrollBar { }
                            Accessible.role: Accessible.List
                            Accessible.name: "Conversation"

                            delegate: Item {
                                width: chat.width
                                height: bubble.height
                                readonly property bool mine: modelData.role === "user"
                                Accessible.role: Accessible.StaticText
                                Accessible.name: (mine ? "You" : "Jarvis") + " at " + modelData.time + ": " + modelData.text
                                Rectangle {
                                    id: bubble
                                    anchors.right: parent.mine ? parent.right : undefined
                                    anchors.left: parent.mine ? undefined : parent.left
                                    width: Math.max(msg.contentWidth, stamp.implicitWidth) + 28
                                    height: msg.contentHeight + stamp.implicitHeight + 26
                                    radius: 8
                                    color: parent.mine ? "#10202B" : "#111318"
                                    border.width: 1
                                    border.color: parent.mine ? Qt.rgba(0.31, 0.85, 1, 0.35) : Qt.rgba(0.6, 0.42, 1, 0.3)
                                    Text {
                                        id: msg
                                        x: 14
                                        y: 10
                                        width: chat.width * 0.8 - 28
                                        text: modelData.text
                                        wrapMode: Text.Wrap
                                        color: Theme.text
                                        font.family: Theme.sans
                                        font.pixelSize: 14
                                    }
                                    Text {
                                        id: stamp
                                        x: 14
                                        y: msg.y + msg.contentHeight + 6
                                        text: modelData.time
                                        color: Theme.muted
                                        font.family: Theme.mono
                                        font.pixelSize: 10
                                    }
                                }
                            }

                            Text {
                                anchors.centerIn: parent
                                visible: chat.count === 0
                                text: "No messages yet. Ask something, or type help."
                                color: Theme.muted
                                font.family: Theme.sans
                                font.pixelSize: 13
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            TextField {
                                id: input
                                objectName: "chatInput"
                                Layout.fillWidth: true
                                placeholderText: (backend && backend.voiceRecording) ? "Listening… (speak now, press Ctrl+Space to finish)" : "Ask Jarvis…  (try: help)"
                                placeholderTextColor: (backend && backend.voiceRecording) ? Theme.cyan : Theme.muted
                                color: Theme.text
                                font.family: Theme.sans
                                font.pixelSize: 14
                                maximumLength: 500
                                leftPadding: 14
                                selectionColor: Theme.cyan
                                Accessible.name: "Message to Jarvis"
                                background: Rectangle {
                                    radius: 8
                                    color: "#0A0C0F"
                                    border.width: (input.activeFocus || (backend && backend.voiceRecording)) ? 2 : 1
                                    border.color: (backend && backend.voiceRecording) ? Theme.cyan : (input.activeFocus ? Theme.cyan : Theme.border)
                                }
                                onAccepted: send.clicked()
                            }
                            GhostButton {
                                id: ptt
                                objectName: "pttButton"
                                text: (backend && backend.voiceRecording) ? "REC ●" : "MIC"
                                primary: Boolean(backend && backend.voiceRecording)
                                danger: Boolean(backend && backend.voiceRecording)
                                implicitWidth: 78
                                Accessible.name: (backend && backend.voiceRecording) ? "Stop voice recording" : "Push to talk (Control Space)"
                                onClicked: if (backend) backend.toggleVoiceRecording()
                            }
                            GhostButton {
                                id: send
                                objectName: "sendButton"
                                text: "SEND"
                                primary: true
                                implicitWidth: 84
                                Accessible.name: "Send message"
                                onClicked: {
                                    backend.send(input.text)
                                    input.clear()
                                    input.forceActiveFocus()
                                }
                            }
                        }
                    }
                }
            }

            // ---- right: tasks and timeline
            Panel {
                title: "Activity"
                Layout.preferredWidth: 330
                Layout.maximumWidth: 330
                Layout.fillHeight: true

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10

                    RowLayout {
                        spacing: 8
                        GhostButton {
                            objectName: "tabTasks"
                            text: "TASKS"
                            compact: true
                            selected: win.tab === 0
                            Accessible.name: "Tasks (Control 1)" + (selected ? ", selected" : "")
                            onClicked: win.tab = 0
                        }
                        GhostButton {
                            objectName: "tabTimeline"
                            text: "TIMELINE"
                            compact: true
                            selected: win.tab === 1
                            Accessible.name: "Timeline (Control 2)" + (selected ? ", selected" : "")
                            onClicked: win.tab = 1
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Text {
                        visible: win.tab === 0
                        text: backend.awaitingCount + " awaiting your approval"
                        color: backend.awaitingCount > 0 ? Theme.violet : Theme.muted
                        font.family: Theme.mono
                        font.pixelSize: 11
                    }

                    StackLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        currentIndex: win.tab

                        ListView {
                            id: taskList
                            objectName: "taskList"
                            clip: true
                            spacing: 8
                            model: backend.tasks
                            ScrollBar.vertical: ScrollBar { }
                            Accessible.role: Accessible.List
                            Accessible.name: "Tasks"

                            delegate: Rectangle {
                                width: taskList.width
                                height: 62
                                radius: 8
                                color: "#0A0C0F"
                                border.width: 1
                                border.color: Theme.border
                                Accessible.role: Accessible.ListItem
                                Accessible.name: "Task " + modelData.id + ": " + modelData.title + ". " + modelData.label +
                                                 ". Permission " + modelData.needs
                                Rectangle {
                                    width: 3; height: parent.height - 16; radius: 1
                                    anchors.verticalCenter: parent.verticalCenter
                                    color: Theme.tone(modelData.tone)
                                }
                                Column {
                                    x: 16; y: 10
                                    width: parent.width - 28
                                    spacing: 6
                                    Text {
                                        width: parent.width
                                        text: "#" + modelData.id + "  " + modelData.title
                                        elide: Text.ElideRight
                                        color: Theme.text
                                        font.family: Theme.sans
                                        font.pixelSize: 13
                                    }
                                    Text {
                                        width: parent.width
                                        text: modelData.label + "  ·  " + modelData.needs
                                        elide: Text.ElideRight
                                        color: Theme.tone(modelData.tone)
                                        font.family: Theme.mono
                                        font.pixelSize: 11
                                    }
                                }
                            }

                            Text {
                                anchors.centerIn: parent
                                width: parent.width - 24
                                visible: taskList.count === 0
                                horizontalAlignment: Text.AlignHCenter
                                wrapMode: Text.Wrap
                                text: "No tasks yet. Tasks submitted from ChatGPT appear here and wait for your approval."
                                color: Theme.muted
                                font.family: Theme.sans
                                font.pixelSize: 13
                            }
                        }

                        ListView {
                            id: timelineList
                            objectName: "timelineList"
                            clip: true
                            spacing: 6
                            model: backend.timeline
                            ScrollBar.vertical: ScrollBar { }
                            Accessible.role: Accessible.List
                            Accessible.name: "Activity timeline"

                            delegate: Item {
                                width: timelineList.width
                                height: Math.max(entryTime.implicitHeight, entryText.implicitHeight) + 6
                                Accessible.role: Accessible.ListItem
                                Accessible.name: modelData.time + ": " + modelData.text
                                Text {
                                    id: entryTime
                                    width: 78
                                    text: modelData.time
                                    color: Theme.muted
                                    font.family: Theme.mono
                                    font.pixelSize: 11
                                }
                                Text {
                                    id: entryText
                                    x: 84
                                    width: parent.width - 90
                                    text: modelData.text
                                    wrapMode: Text.Wrap
                                    color: Theme.text
                                    font.family: Theme.sans
                                    font.pixelSize: 12
                                }
                            }

                            Text {
                                anchors.centerIn: parent
                                visible: timelineList.count === 0
                                text: "No activity yet."
                                color: Theme.muted
                                font.family: Theme.sans
                                font.pixelSize: 13
                            }
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "Read-only view. Approve in a terminal:\npython bridge\\admin.py approve <id>"
                        wrapMode: Text.Wrap
                        color: Theme.muted
                        font.family: Theme.mono
                        font.pixelSize: 10
                    }
                }
            }
        }

        Text {
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            text: "Ctrl+L chat   ·   Ctrl+1 tasks   ·   Ctrl+2 timeline   ·   Ctrl+, settings   ·   Esc close"
            color: Theme.muted
            font.family: Theme.mono
            font.pixelSize: 10
        }
    }

    Component.onCompleted: input.forceActiveFocus()
}
