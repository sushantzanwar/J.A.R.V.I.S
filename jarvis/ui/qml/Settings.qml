import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Safe preferences and read-only diagnostics. Nothing here approves tasks, edits the worker, or runs commands.
Popup {
    id: root
    objectName: "settingsPopup"
    signal clearRequested()

    modal: true
    focus: true
    padding: 0
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    anchors.centerIn: Overlay.overlay
    width: Math.min(720, Overlay.overlay ? Overlay.overlay.width - 48 : 720)
    height: Math.min(760, Overlay.overlay ? Overlay.overlay.height - 48 : 760)

    onOpened: {
        diag.text = backend.diagnosticsText()
        closeButton.forceActiveFocus()
    }

    Overlay.modal: Rectangle { color: "#D0000000" }
    background: Rectangle {
        color: Theme.panel
        radius: 12
        border.width: 1
        border.color: Qt.rgba(0.31, 0.85, 1, 0.5)
    }

    component Heading: Text {
        color: Theme.cyan
        font.family: Theme.sans
        font.pixelSize: 11
        font.weight: Font.DemiBold
        font.letterSpacing: 2
        Layout.topMargin: 6
    }
    component Body: Text {
        color: Theme.text
        font.family: Theme.sans
        font.pixelSize: 13
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }
    component Note: Text {
        color: Theme.muted
        font.family: Theme.sans
        font.pixelSize: 12
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }

    contentItem: ColumnLayout {
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: 20
            Layout.bottomMargin: 8
            Text {
                text: "SETTINGS & DIAGNOSTICS"
                color: Theme.ice
                font.family: Theme.sans
                font.pixelSize: 15
                font.weight: Font.Light
                font.letterSpacing: 4
                Layout.fillWidth: true
                Accessible.role: Accessible.Heading
                Accessible.name: "Settings and diagnostics"
            }
            GhostButton {
                id: closeButton
                objectName: "settingsClose"
                text: "CLOSE"
                compact: true
                onClicked: root.close()
            }
        }

        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.leftMargin: 20
            Layout.rightMargin: 12
            Layout.bottomMargin: 16
            clip: true
            contentWidth: availableWidth

            ColumnLayout {
                width: scroll.availableWidth - 8
                spacing: 10

                // ---- language model
                Heading { text: "LANGUAGE MODEL" }
                RowLayout {
                    spacing: 8
                    Repeater {
                        model: backend.providerNames
                        delegate: GhostButton {
                            objectName: "provider-" + modelData
                            text: modelData === "offline" ? "OFFLINE" : "CLAUDE CODE"
                            selected: backend.providerName === modelData
                            Accessible.name: "Use " + (modelData === "offline" ? "offline mode" : "Claude Code") +
                                             (selected ? ", selected" : "")
                            onClicked: backend.setProvider(modelData)
                        }
                    }
                    Item { Layout.fillWidth: true }
                    GhostButton {
                        text: "RE-CHECK"
                        onClicked: backend.refreshProvider()
                    }
                }
                RowLayout {
                    spacing: 8
                    Rectangle {
                        width: 9; height: 9; radius: 5
                        color: backend.providerState === "connected" ? Theme.cyan
                               : (backend.providerState === "builtin" ? Theme.muted : Theme.violet)
                    }
                    Text {
                        objectName: "providerStateText"
                        text: backend.providerState.toUpperCase()
                        color: Theme.text
                        font.family: Theme.mono
                        font.pixelSize: 12
                    }
                }
                Body {
                    objectName: "providerExplanation"
                    text: backend.providerExplanation
                }
                Note {
                    text: "Claude Code runs the first-party claude command under your existing login. Usage counts against " +
                          "your plan. Jarvis never reads or stores your login."
                }

                // ---- preferences
                Heading { text: "PREFERENCES" }
                Toggle {
                    objectName: "toggleHistory"
                    caption: "Save conversation history on this PC"
                    checked: backend.saveHistory
                    onClicked: backend.setSaveHistory(!backend.saveHistory)
                }
                Toggle {
                    objectName: "toggleMotion"
                    caption: "Reduce motion (freeze the orb animation)"
                    checked: backend.reduceMotion
                    onClicked: backend.setReduceMotion(!backend.reduceMotion)
                }

                // ---- data
                Heading { text: "LOCAL DATA" }
                Note { text: "Stored in one local file; nothing is sent anywhere:" }
                TextEdit {
                    objectName: "dataLocation"
                    text: backend.storeLocation
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.WrapAnywhere
                    color: Theme.text
                    font.family: Theme.mono
                    font.pixelSize: 12
                    Layout.fillWidth: true
                    Accessible.role: Accessible.StaticText
                    Accessible.name: "Data location " + backend.storeLocation
                }
                Note {
                    text: "Holds chat history, these preferences and short activity notes. Secret-looking text (API keys, " +
                          "tokens, passwords) is redacted before saving. Provider logins are never stored. It is outside the " +
                          "project folder and is never committed."
                }
                Note {
                    visible: !backend.storePersistent
                    color: Theme.violet
                    text: "This session is NOT being saved. See the notice at the top of the window."
                }
                RowLayout {
                    spacing: 8
                    GhostButton {
                        objectName: "clearHistoryButton"
                        text: "CLEAR HISTORY…"
                        danger: true
                        onClicked: root.clearRequested()
                    }
                    GhostButton {
                        objectName: "resetPrefsButton"
                        text: "RESET PREFERENCES"
                        onClicked: backend.resetPreferences()
                    }
                }

                // ---- durable memory
                Heading { text: "DURABLE MEMORY (LOCAL SQLITE)" }
                Body {
                    text: (backend && backend.memories ? backend.memories.length : 0) + " structured memory record(s) stored."
                }
                Note {
                    text: {
                        if (!backend || !backend.memories || backend.memories.length === 0)
                            return "No memories stored yet. Jarvis stores facts locally and offline.";
                        var counts = {};
                        for (var i = 0; i < backend.memories.length; i++) {
                            var c = backend.memories[i].category;
                            counts[c] = (counts[c] || 0) + 1;
                        }
                        var parts = [];
                        for (var k in counts) parts.push(k + ": " + counts[k]);
                        return "By category: " + parts.join(" · ") + ". Export or manage below.";
                    }
                }
                RowLayout {
                    spacing: 8
                    GhostButton {
                        objectName: "exportMemoriesButton"
                        text: "EXPORT (JSON)"
                        onClicked: if (backend) backend.exportMemories()
                    }
                    GhostButton {
                        id: clearMemBtn
                        objectName: "clearMemoriesButton"
                        property bool armed: false
                        text: armed ? "CONFIRM CLEAR (BACKUP CREATED)" : "CLEAR ALL MEMORIES…"
                        danger: true
                        onClicked: {
                            if (!armed) {
                                armed = true;
                            } else {
                                armed = false;
                                if (backend) backend.clearMemories("");
                            }
                        }
                    }
                    GhostButton {
                        visible: clearMemBtn.armed
                        text: "CANCEL"
                        compact: true
                        onClicked: clearMemBtn.armed = false
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 4
                    visible: Boolean(backend && backend.memories && backend.memories.length > 0)
                    Repeater {
                        model: backend && backend.memories ? backend.memories.slice(0, 5) : []
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            Text {
                                text: "[" + modelData.category + "] " + modelData.key + ": " + modelData.value
                                color: Theme.text
                                font.family: Theme.mono
                                font.pixelSize: 11
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                            GhostButton {
                                text: "FORGET"
                                compact: true
                                danger: true
                                onClicked: if (backend) backend.forget(modelData.key, modelData.category)
                            }
                        }
                    }
                }

                // ---- worker (read only)
                Heading { text: "BACKGROUND WORKER (READ-ONLY)" }
                Body {
                    objectName: "workerDetail"
                    text: backend.workerState.detail
                }
                Body {
                    text: backend.taskCounts.awaiting + " awaiting approval · " + backend.taskCounts.queued + " queued · " +
                          backend.taskCounts.running + " running · " + backend.taskCounts.done + " done"
                }
                Note {
                    text: "Approval is human-only and happens in a terminal: python bridge\\admin.py approve <id>.\n" +
                          "Pause or resume the worker: python -m jarvis.worker --pause / --resume."
                }

                // ---- diagnostics
                Heading { text: "DIAGNOSTICS" }
                Note { text: "A read-only summary. It contains no messages, task text, keys or account details." }
                ScrollView {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 230
                    clip: true
                    TextArea {
                        id: diag
                        objectName: "diagnosticsText"
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        color: Theme.text
                        font.family: Theme.mono
                        font.pixelSize: 11
                        Accessible.name: "Diagnostics summary"
                        background: Rectangle { color: "#0A0C0F"; radius: 8; border.width: 1; border.color: Theme.border }
                    }
                }
                RowLayout {
                    spacing: 8
                    GhostButton {
                        text: "COPY"
                        onClicked: backend.copyDiagnostics()
                    }
                    GhostButton {
                        text: "REFRESH"
                        onClicked: diag.text = backend.diagnosticsText()
                    }
                }
            }
        }
    }
}
