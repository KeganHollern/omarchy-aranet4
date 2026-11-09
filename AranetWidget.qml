pragma ComponentBehavior: Bound

import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Snapshot.js" as Snapshot

Panel {
    id: root

    moduleName: "kegan.aranet4"
    manageIpc: false
    property string fixedEdge: "center"
    readonly property string address: typeof settings.address === "string" ? settings.address.toUpperCase() : ""
    readonly property QtObject backend: bar && bar.shell && typeof bar.shell.serviceFor === "function"
        ? bar.shell.serviceFor(moduleName) : null
    property QtObject registeredBackend: null
    property int viewToken: -1
    property bool destroying: false
    readonly property var snapshot: backend ? backend.snapshot : null
    readonly property var schedulerStatus: backend ? backend.status : null
    readonly property string currentBootId: backend ? backend.bootId : ""
    readonly property real uptime: backend ? backend.uptime : -1
    readonly property var displayState: Snapshot.render(snapshot, address, currentBootId, uptime, schedulerStatus)
    readonly property var cachedState: Snapshot.render(snapshot, address, currentBootId, uptime, schedulerStatus, true)
    readonly property bool available: displayState.text !== ""
    readonly property bool cachedAvailable: cachedState.text !== ""
    readonly property bool hasValidStatus: !!Snapshot.validStatus(schedulerStatus, address, currentBootId, uptime)
    readonly property string airIcon: "\uDB81\uDD9D"
    readonly property string readingText: cachedAvailable ? Commons.I18n.formatNumber(snapshot.co2) : "—"

    visible: available || opened
    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight
    onBackendChanged: syncBackend()
    onSettingsChanged: syncBackend()
    onOpenedChanged: {
        if (registeredBackend && viewToken >= 0)
            registeredBackend.setPanelOpen(viewToken, opened)
    }
    Component.onCompleted: syncBackend()
    Component.onDestruction: {
        destroying = true
        if (registeredBackend && viewToken >= 0)
            registeredBackend.unregisterView(viewToken)
    }

    Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

    function syncBackend() {
        if (destroying) return
        if (registeredBackend !== backend) {
            if (registeredBackend && viewToken >= 0)
                registeredBackend.unregisterView(viewToken)
            registeredBackend = backend
            viewToken = -1
        }
        if (!registeredBackend) return
        if (/^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(address))
            registeredBackend.configure(settings)
        if (viewToken < 0) viewToken = registeredBackend.registerView()
        registeredBackend.setPanelOpen(viewToken, opened)
    }

    function statusMessage() {
        if (hasValidStatus && schedulerStatus.refreshing) return strings.t("status.refreshing")
        if (hasValidStatus && schedulerStatus.pause_reason) return strings.t("status.paused")
        if (hasValidStatus && !schedulerStatus.present)
            return strings.t(cachedAvailable ? "status.unavailableCached" : "status.unavailable")
        if (!cachedAvailable) return strings.t("status.waiting")
        if (!available) return strings.t("status.cached")
        return ""
    }

    function metric(key, minimum, maximum, decimals, unit) {
        var value = cachedAvailable ? Snapshot.boundedNumber(snapshot[key], minimum, maximum) : null
        return value === null ? "—" : Commons.I18n.formatNumber(value, decimals) + unit
    }

    function sampleAge() {
        if (!cachedAvailable) return ""
        var age = Snapshot.boundedNumber(snapshot.sample_age, 0, 65535)
        if (age === null) return ""
        var seconds = Math.max(0, Math.floor(age + uptime - snapshot.seen_boottime))
        if (seconds < 60) return strings.t("sample.justNow")
        var minutes = Math.floor(seconds / 60)
        return strings.tn("sample.minutesAgo", minutes).arg(Commons.I18n.formatNumber(minutes))
    }

    BarIconButton {
        id: button
        anchors.fill: parent
        bar: root.bar
        text: root.airIcon
        slotSize: Style.bar.statusSlot
        tooltipText: root.opened ? "" : strings.t("title")
        onPressed: function(mouseButton) {
            if (mouseButton === Qt.LeftButton && (root.available || root.opened)) root.toggle()
        }
    }

    KeyboardPanel {
        id: panel
        anchorItem: button
        owner: root
        bar: root.bar
        open: root.opened
        focusTarget: panel.loadedContent
        contentWidth: panel.fittedContentWidth(Style.space(310))
        contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

        content: PanelKeyCatcher {
            implicitHeight: contentColumn.implicitHeight
            onCloseRequested: root.close()
            onTabRequested: function(direction) { root.switchPanel(direction) }

            Column {
                id: contentColumn
                width: parent.width
                spacing: Style.space(14)

                Item {
                    width: parent.width
                    implicitHeight: Math.max(heroIcon.implicitHeight, heading.implicitHeight, heroReading.implicitHeight)

                    Text {
                        id: heroIcon
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        textFormat: Text.PlainText
                        text: root.airIcon
                        color: root.barForeground
                        font.family: button.fontFamily
                        font.pixelSize: Style.font.display
                    }

                    Column {
                        id: heading
                        anchors.left: heroIcon.right
                        anchors.leftMargin: Style.space(14)
                        anchors.right: heroReading.left
                        anchors.rightMargin: Style.space(12)
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Style.space(2)

                        Text {
                            width: parent.width
                            textFormat: Text.PlainText
                            text: strings.t("title")
                            color: root.barForeground
                            font.family: button.fontFamily
                            font.pixelSize: Style.font.title
                            font.bold: true
                            elide: Text.ElideRight
                        }
                        Text {
                            width: parent.width
                            textFormat: Text.PlainText
                            text: strings.t("co2").toUpperCase()
                            color: root.barForeground
                            opacity: 0.6
                            font.family: button.fontFamily
                            font.pixelSize: Style.font.caption
                            font.bold: true
                            font.letterSpacing: 1.2
                            elide: Text.ElideRight
                        }
                    }

                    Column {
                        id: heroReading
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: Style.space(2)

                        Text {
                            textFormat: Text.PlainText
                            text: root.readingText
                            color: root.barForeground
                            font.family: button.fontFamily
                            font.pixelSize: Style.font.displayLarge
                            font.bold: true
                        }
                        Text {
                            anchors.right: parent.right
                            textFormat: Text.PlainText
                            text: "ppm"
                            color: root.barForeground
                            opacity: 0.6
                            font.family: button.fontFamily
                            font.pixelSize: Style.font.caption
                        }
                    }
                }

                Text {
                    width: parent.width
                    visible: text !== ""
                    textFormat: Text.PlainText
                    text: root.statusMessage()
                    color: root.barForeground
                    opacity: 0.6
                    font.family: button.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                }

                PanelSeparator { foreground: root.barForeground }

                Column {
                    width: parent.width
                    spacing: Style.space(9)
                    InfoPair { label: strings.t("temperature"); value: root.metric("temperature", -50, 100, 1, " °C") }
                    InfoPair { label: strings.t("humidity"); value: root.metric("humidity", 0, 100, 0, "%") }
                    InfoPair { label: strings.t("pressure"); value: root.metric("pressure", 1, 2000, 0, " hPa") }
                    InfoPair { label: strings.t("battery"); value: root.metric("battery", 0, 100, 0, "%") }
                }

                PanelSeparator { foreground: root.barForeground }

                Item {
                    width: parent.width
                    implicitHeight: Math.max(deviceName.implicitHeight, sampleTime.implicitHeight)
                    Text {
                        id: deviceName
                        anchors.left: parent.left
                        anchors.right: sampleTime.left
                        anchors.rightMargin: Style.space(10)
                        textFormat: Text.PlainText
                        text: Snapshot.cleanName(root.snapshot ? root.snapshot.name : "Aranet4")
                        color: root.barForeground
                        opacity: 0.6
                        font.family: button.fontFamily
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                    }
                    Text {
                        id: sampleTime
                        anchors.right: parent.right
                        textFormat: Text.PlainText
                        text: root.sampleAge()
                        color: root.barForeground
                        opacity: 0.6
                        font.family: button.fontFamily
                        font.pixelSize: Style.font.caption
                    }
                }
            }
        }
    }

    component InfoPair: Item {
        property string label: ""
        property string value: ""
        width: parent.width
        implicitHeight: Math.max(infoLabel.implicitHeight, infoValue.implicitHeight)
        Text {
            id: infoLabel
            textFormat: Text.PlainText
            text: parent.label
            color: root.barForeground
            opacity: 0.6
            font.family: button.fontFamily
            font.pixelSize: Style.font.bodySmall
        }
        Text {
            id: infoValue
            anchors.right: parent.right
            textFormat: Text.PlainText
            text: parent.value
            color: root.barForeground
            font.family: button.fontFamily
            font.pixelSize: Style.font.bodySmall
        }
    }

}
