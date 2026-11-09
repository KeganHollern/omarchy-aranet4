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
    readonly property bool setupNeeded: !backend || backend.setupNeeded === true
    readonly property bool setupBusy: !!backend && backend.setupBusy === true
    readonly property var setupState: backend && backend.setupState ? backend.setupState : ({ phase: "checking", devices: [] })
    readonly property string setupPhase: typeof setupState.phase === "string" ? setupState.phase : "checking"
    readonly property var setupDevices: Array.isArray(setupState.devices) ? setupState.devices.filter(function(device) {
        return device && typeof device.address === "string" && /^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/i.test(device.address)
    }).slice(0, 16) : []

    visible: setupNeeded || available || opened
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

    function setupMessage() {
        if (setupPhase === "error" && typeof setupState.message === "string" && setupState.message !== "")
            return setupState.message.slice(0, 500)
        if (setupPhase === "dependencies") return strings.t("setup.dependencies")
        if (setupPhase === "installing") return strings.t("setup.installing")
        if (setupPhase === "scanning") return strings.t("setup.scanning")
        if (setupPhase === "select") return strings.t(setupDevices.length ? "setup.choose" : "setup.noneFound")
        if (setupPhase === "discovery") return strings.t("setup.discovery")
        return strings.t("setup.checking")
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
        tooltipText: root.opened ? "" : strings.t(root.setupNeeded ? "setup.tooltip" : "title")
        onPressed: function(mouseButton) {
            if (mouseButton === Qt.LeftButton && (root.setupNeeded || root.available || root.opened)) root.toggle()
        }
        Rectangle {
            visible: root.setupNeeded
            width: Style.space(3)
            height: width
            radius: width / 2
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.rightMargin: Style.space(3)
            anchors.bottomMargin: Style.space(4)
            color: Commons.Color.accent
            opacity: 0.75
        }
    }

    KeyboardPanel {
        id: panel
        objectName: "aranetDetailsPanel"
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
            onTabRequested: function(direction) {
                if (root.setupNeeded) focusSetupAction(direction)
                else root.switchPanel(direction)
            }
            onMoveRequested: function(dx, dy) { if (root.setupNeeded) focusSetupAction(dy || dx) }
            onActivateRequested: {
                if (!root.setupNeeded) return
                var actions = setupActions()
                for (var i = 0; i < actions.length; i++) {
                    if (actions[i].activeFocus) { actions[i].clicked(); return }
                }
                if (actions.length) actions[0].clicked()
            }

            function setupActions() {
                var actions = []
                for (var i = 0; i < deviceRepeater.count; i++) {
                    var item = deviceRepeater.itemAt(i)
                    if (item && item.enabled && item.visible) actions.push(item)
                }
                if (setupAction.visible && setupAction.enabled) actions.push(setupAction)
                return actions
            }

            function focusSetupAction(direction) {
                var actions = setupActions()
                if (!actions.length) return
                var current = -1
                for (var i = 0; i < actions.length; i++) if (actions[i].activeFocus) current = i
                var next = current < 0 ? (direction < 0 ? actions.length - 1 : 0)
                    : (current + (direction < 0 ? -1 : 1) + actions.length) % actions.length
                actions[next].forceActiveFocus()
            }

            Column {
                id: contentColumn
                objectName: "aranetReadingContent"
                width: parent.width
                spacing: Style.space(14)

                Item {
                    width: parent.width
                    implicitHeight: Math.max(heroIcon.implicitHeight, heading.implicitHeight, root.setupNeeded ? 0 : heroReading.implicitHeight)

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
                        anchors.right: root.setupNeeded ? parent.right : heroReading.left
                        anchors.rightMargin: root.setupNeeded ? 0 : Style.space(12)
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
                            text: strings.t(root.setupNeeded ? "setup.subtitle" : "co2").toUpperCase()
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
                        visible: !root.setupNeeded
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

                Column {
                    id: setupColumn
                    width: parent.width
                    visible: root.setupNeeded
                    spacing: Style.space(12)

                    Text {
                        width: parent.width
                        textFormat: Text.PlainText
                        text: root.setupMessage()
                        color: root.barForeground
                        opacity: 0.75
                        font.family: button.fontFamily
                        font.pixelSize: Style.font.bodySmall
                        wrapMode: Text.WordWrap
                    }

                    Flickable {
                        id: deviceList
                        width: parent.width
                        height: Math.min(deviceColumn.implicitHeight, Style.space(180))
                        visible: root.setupPhase === "select" && root.setupDevices.length > 0
                        contentHeight: deviceColumn.implicitHeight
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds

                        Column {
                            id: deviceColumn
                            width: parent.width
                            spacing: Style.space(6)
                            Repeater {
                                id: deviceRepeater
                                model: root.setupPhase === "select" ? root.setupDevices : []
                                delegate: Button {
                                    required property var modelData
                                    width: deviceColumn.width
                                    text: Snapshot.cleanName(modelData.name)
                                    tooltipText: modelData.address
                                    foreground: root.barForeground
                                    fontFamily: button.fontFamily
                                    leftAlign: true
                                    bordered: true
                                    focusable: true
                                    enabled: !root.setupBusy && !!root.backend && modelData.readable !== false
                                    opacity: enabled ? 1 : 0.5
                                    onActiveFocusChanged: {
                                        if (!activeFocus) return
                                        if (y < deviceList.contentY) deviceList.contentY = y
                                        else if (y + height > deviceList.contentY + deviceList.height)
                                            deviceList.contentY = y + height - deviceList.height
                                    }
                                    onClicked: root.backend.selectDevice(modelData.address)
                                }
                            }
                        }
                    }

                    Text {
                        width: parent.width
                        visible: root.setupPhase === "select" && root.setupDevices.some(function(device) { return device.readable === false })
                        textFormat: Text.PlainText
                        text: strings.t("setup.enableIntegrations")
                        color: root.barForeground
                        opacity: 0.6
                        font.family: button.fontFamily
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.WordWrap
                    }

                    Button {
                        id: setupAction
                        width: parent.width
                        visible: ["dependencies", "discovery", "select", "error"].indexOf(root.setupPhase) >= 0
                        text: strings.t(root.setupPhase === "dependencies" ? "setup.install"
                            : root.setupPhase === "error" ? "setup.retry"
                            : root.setupPhase === "select" ? "setup.scanAgain" : "setup.scan")
                        foreground: root.barForeground
                        fontFamily: button.fontFamily
                        bordered: true
                        focusable: true
                        enabled: !root.setupBusy && !!root.backend
                        opacity: enabled ? 1 : 0.5
                        onClicked: {
                            if (root.setupPhase === "dependencies") root.backend.installDependencies()
                            else if (root.setupPhase === "error") root.backend.retrySetup()
                            else root.backend.discoverDevices()
                        }
                    }
                }

                PanelSeparator { visible: !root.setupNeeded; foreground: root.barForeground }

                Column {
                    width: parent.width
                    visible: !root.setupNeeded
                    spacing: Style.space(9)
                    InfoPair { label: strings.t("temperature"); value: root.metric("temperature", -50, 100, 1, " °C") }
                    InfoPair { label: strings.t("humidity"); value: root.metric("humidity", 0, 100, 0, "%") }
                    InfoPair { label: strings.t("pressure"); value: root.metric("pressure", 1, 2000, 0, " hPa") }
                    InfoPair { label: strings.t("battery"); value: root.metric("battery", 0, 100, 0, "%") }
                }

                PanelSeparator { visible: !root.setupNeeded; foreground: root.barForeground }

                Item {
                    objectName: "aranetReadingFooter"
                    width: parent.width
                    visible: !root.setupNeeded
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
                        objectName: "aranetReadingStatus"
                        anchors.right: parent.right
                        width: Math.min(implicitWidth, parent.width * 0.7)
                        textFormat: Text.PlainText
                        text: root.statusMessage() || root.sampleAge()
                        color: root.barForeground
                        opacity: 0.6
                        font.family: button.fontFamily
                        font.pixelSize: Style.font.caption
                        horizontalAlignment: Text.AlignRight
                        elide: Text.ElideRight
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
