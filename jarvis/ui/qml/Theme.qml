pragma Singleton
import QtQuick

QtObject {
    readonly property color bg: "#07080A"
    readonly property color panel: "#0D0F12"
    readonly property color border: "#1A1F26"
    readonly property color cyan: "#4FD8FF"
    readonly property color ice: "#D6F4FF"
    readonly property color violet: "#9A6BFF"
    readonly property color text: "#E4EBF2"
    readonly property color muted: "#8C97A6"   // >= 6:1 on panel; checked by tests/test_workspace_ui.py

    readonly property string sans: "Segoe UI"
    readonly property string mono: "Cascadia Mono"

    function tone(name) {
        return name === "cyan" ? cyan : name === "ice" ? ice : name === "violet" ? violet : muted
    }
}
