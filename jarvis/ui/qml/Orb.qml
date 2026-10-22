import QtQuick
import QtQuick.Shapes

// Animated AI orb. mode: "idle" | "thinking" | "speaking"
Item {
    id: root
    property string mode: "idle"
    property bool reduceMotion: false     // accessibility: freeze the animation
    readonly property real speed: mode === "thinking" ? 3.0 : (mode === "speaking" ? 1.8 : 1.0)
    property real pulse: 0

    implicitWidth: 300
    implicitHeight: 300
    Accessible.role: Accessible.Graphic
    Accessible.name: "Assistant status: " + (mode === "thinking" ? "processing" : (mode === "speaking" ? "responding" : "standing by"))

    SequentialAnimation on pulse {
        loops: Animation.Infinite
        running: !root.reduceMotion
        NumberAnimation { to: 1; duration: 2200 / root.speed; easing.type: Easing.InOutSine }
        NumberAnimation { to: 0; duration: 2200 / root.speed; easing.type: Easing.InOutSine }
    }

    component Ring: Shape {
        id: ring
        property real r: 100
        property real start: 0
        property real sweep: 90
        property color col: Theme.cyan
        property real w: 2
        property real period: 12000
        property int dir: 1
        property real speedFactor: 1
        property bool still: false

        anchors.fill: parent
        layer.enabled: true
        layer.samples: 4

        RotationAnimator on rotation {
            from: 0
            to: 360 * ring.dir
            duration: ring.period / ring.speedFactor
            loops: Animation.Infinite
            running: !ring.still
        }
        ShapePath {
            strokeColor: ring.col
            strokeWidth: ring.w
            fillColor: "transparent"
            capStyle: ShapePath.RoundCap
            PathAngleArc {
                centerX: ring.width / 2
                centerY: ring.height / 2
                radiusX: ring.r
                radiusY: ring.r
                startAngle: ring.start
                sweepAngle: ring.sweep
            }
        }
    }

    // soft outer glow
    Shape {
        anchors.fill: parent
        layer.enabled: true
        layer.samples: 4
        opacity: 0.5 + 0.35 * root.pulse
        scale: 0.94 + 0.08 * root.pulse
        ShapePath {
            strokeColor: "transparent"
            fillGradient: RadialGradient {
                centerX: root.width / 2
                centerY: root.height / 2
                centerRadius: root.width / 2
                focalX: root.width / 2
                focalY: root.height / 2
                GradientStop { position: 0.0; color: Qt.rgba(0.31, 0.85, 1.0, 0.38) }
                GradientStop { position: 0.55; color: Qt.rgba(0.60, 0.42, 1.0, 0.12) }
                GradientStop { position: 1.0; color: Qt.rgba(0.60, 0.42, 1.0, 0.0) }
            }
            PathAngleArc {
                centerX: root.width / 2
                centerY: root.height / 2
                radiusX: root.width / 2
                radiusY: root.width / 2
                startAngle: 0
                sweepAngle: 360
            }
        }
    }

    // rotating rings
    Ring { r: root.width * 0.40; start: 10; sweep: 110; col: Theme.cyan; w: 2; period: 14000; dir: 1; speedFactor: root.speed; still: root.reduceMotion }
    Ring { r: root.width * 0.40; start: 200; sweep: 60; col: Theme.cyan; w: 2; period: 14000; dir: 1; speedFactor: root.speed; still: root.reduceMotion }
    Ring { r: root.width * 0.34; start: 60; sweep: 80; col: Theme.violet; w: 2; period: 9000; dir: -1; speedFactor: root.speed; still: root.reduceMotion }
    Ring { r: root.width * 0.34; start: 230; sweep: 40; col: Theme.violet; w: 2; period: 9000; dir: -1; speedFactor: root.speed; still: root.reduceMotion }
    Ring { r: root.width * 0.28; start: 0; sweep: 250; col: Theme.ice; w: 1; period: 20000; dir: 1; speedFactor: root.speed; still: root.reduceMotion }

    // core
    Shape {
        anchors.fill: parent
        layer.enabled: true
        layer.samples: 4
        scale: 0.96 + 0.06 * root.pulse
        ShapePath {
            strokeColor: Qt.rgba(0.31, 0.85, 1.0, 0.6)
            strokeWidth: 1
            fillGradient: RadialGradient {
                centerX: root.width / 2
                centerY: root.height / 2
                centerRadius: root.width * 0.2
                focalX: root.width / 2
                focalY: root.height / 2
                GradientStop { position: 0.0; color: "#F2FDFF" }
                GradientStop { position: 0.35; color: "#7FE6FF" }
                GradientStop { position: 0.75; color: "#1B7EA3" }
                GradientStop { position: 1.0; color: "#2A1B5E" }
            }
            PathAngleArc {
                centerX: root.width / 2
                centerY: root.height / 2
                radiusX: root.width * 0.2
                radiusY: root.width * 0.2
                startAngle: 0
                sweepAngle: 360
            }
        }
    }
}
