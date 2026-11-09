pragma ComponentBehavior: Bound

import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import "Snapshot.js" as Snapshot
import "Setup.js" as Setup

// One service per enabled plugin, shared by every monitor's widget.
Item {
    id: root

    property QtObject shell: null
    property var manifest: ({})
    property var settings: ({})
    property var snapshot: null
    property var status: null
    property string bootId: ""
    property real uptime: -1
    property string lastError: ""
    property var views: ({})
    property int nextViewToken: 0
    property double requestToken: 0
    property bool destroying: false
    property bool reconfiguring: false
    property int failures: 0
    property double startedAt: 0
    property string activeSignature: ""
    property var setupState: ({ phase: "checking", devices: [], message: "" })
    property bool dependenciesReady: false
    property string checkedPython: ""
    property string setupAction: ""
    property string setupPython: ""
    property string setupFailure: ""

    readonly property bool setupBusy: setupTask.running
    readonly property bool setupNeeded: setupState.phase !== "ready"

    readonly property string pluginId: manifest && typeof manifest.id === "string" ? manifest.id : "kegan.aranet4"
    readonly property string address: typeof settings.address === "string" ? settings.address.toUpperCase() : ""
    readonly property string home: Quickshell.env("HOME")
    readonly property string pythonPath: typeof settings.pythonPath === "string" && settings.pythonPath !== ""
        ? settings.pythonPath : (Quickshell.env("XDG_DATA_HOME") || home + "/.local/share") + "/omarchy-aranet4/venv/bin/python"
    readonly property string workerPath: decodeURIComponent(String(Qt.resolvedUrl("aranet_bar.py")).replace(/^file:\/\//, ""))
    readonly property string setupPath: decodeURIComponent(String(Qt.resolvedUrl("aranet_setup.py")).replace(/^file:\/\//, ""))
    readonly property string cacheDir: typeof settings.cachePath === "string" && settings.cachePath.lastIndexOf("/") > 0
        ? settings.cachePath.slice(0, settings.cachePath.lastIndexOf("/"))
        : (Quickshell.env("XDG_CACHE_HOME") || home + "/.cache") + "/omarchy-aranet4"
    readonly property bool configured: /^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(address)
        && pythonPath.startsWith("/") && pythonPath.length <= 4096 && cacheDir.startsWith("/")
    readonly property string desiredSignature: [pythonPath, workerPath, address, cacheDir].join("\u0000")
    readonly property bool panelOpen: Object.keys(views).some(function(key) { return views[key] === true })
    readonly property bool running: worker.running

    ShellIpc {
        target: root.pluginId
        function status(): string {
            return JSON.stringify({ running: root.running, state: root.status,
                snapshot: root.snapshot, lastError: root.lastError, setup: root.setupState })
        }
    }

    function configuredEntry() {
        var layout = shell && shell.barConfig ? shell.barConfig.layout : null
        if (!layout || typeof layout !== "object") return null
        var sections = Object.keys(layout)
        for (var i = 0; i < sections.length; i++) {
            var entries = layout[sections[i]]
            if (!Array.isArray(entries)) continue
            for (var j = 0; j < entries.length; j++) {
                var entry = entries[j]
                if (entry && typeof entry === "object" && entry.id === pluginId) return entry
            }
        }
        return null
    }

    function loadSettings() {
        var entry = configuredEntry()
        if (entry) configure(entry)
    }

    function configure(value) {
        if (destroying || !value || typeof value !== "object") return
        settings = Object.assign({}, value)
        checkSetup()
        if (!setupBusy && dependenciesReady) {
            if (configured) setSetupPhase("ready")
            else if (setupState.phase === "ready") setSetupPhase("discovery")
        }
        reconcile()
    }

    function setSetupPhase(phase, message, devices) {
        setupState = { phase: phase, message: message || "", devices: devices || [] }
    }

    function checkSetup() {
        if (destroying || setupBusy || checkedPython === pythonPath) return
        dependenciesReady = false
        runSetup("status")
    }

    function runSetup(action) {
        if (destroying || setupBusy) return
        setupAction = action
        setupPython = pythonPath
        setupFailure = ""
        setupStderr.text = ""
        setSetupPhase(action === "install" ? "installing" : action === "discover" ? "scanning" : "checking")
        setupTask.command = action === "discover"
            ? [pythonPath, setupPath, "discover"]
            : ["/usr/bin/python", setupPath, action, "--python", pythonPath]
        setupTask.running = true
        setupTimeout.interval = action === "install" ? 300000 : action === "discover" ? 70000 : 20000
        setupTimeout.restart()
    }

    function installDependencies() {
        if (!setupBusy) runSetup("install")
    }

    function discoverDevices() {
        if (setupBusy) return
        if (!dependenciesReady) { runSetup("status"); return }
        runSetup("discover")
    }

    function retrySetup() {
        if (setupBusy) return
        if (dependenciesReady && !configured) discoverDevices()
        else { checkedPython = ""; checkSetup() }
    }

    function finishSetup(exitCode) {
        setupTimeout.stop()
        if (destroying) return
        if (setupPython !== pythonPath) { checkedPython = ""; Qt.callLater(checkSetup); return }
        var result = Setup.parseResult(setupStdout.text.trim(), setupAction)
        if (setupFailure || exitCode !== 0 || !result || !result.ok) {
            setSetupPhase("error", setupFailure || (result && result.error)
                || Setup.cleanMessage(setupStderr.text, 320) || "Setup could not finish. Please try again.")
            return
        }
        if (setupAction === "discover") {
            setSetupPhase("select", "", result.devices)
            return
        }
        checkedPython = pythonPath
        dependenciesReady = result.dependencies_ready
        if (!dependenciesReady) setSetupPhase("dependencies")
        else if (configured) { setSetupPhase("ready"); reconcile() }
        else if (setupAction === "install") Qt.callLater(discoverDevices)
        else setSetupPhase("discovery")
    }

    function selectDevice(value) {
        if (setupBusy || !dependenciesReady || typeof value !== "string") return
        var device = setupState.devices.find(function(entry) { return entry.address === value })
        if (!device) return
        if (!device.readable) {
            setSetupPhase("select", "Enable Smart Home integrations for this Aranet4 in the Aranet Home app, close the app, then scan again.", setupState.devices)
            return
        }
        var selected = Object.assign({}, settings, { address: device.address, pythonPath: pythonPath })
        var saved = shell && typeof shell.updateEntryInline === "function"
            && shell.updateEntryInline(pluginId, selected)
        var current = configuredEntry()
        var alreadySaved = current && current.address === device.address && current.pythonPath === pythonPath
        if (!saved && !alreadySaved) {
            setSetupPhase("error", "Could not save the selected device. Please try again.")
            return
        }
        settings = selected
        snapshot = null
        status = null
        setSetupPhase("ready")
        reconcile()
    }

    function unavailable() {
        status = { schema: 1, address: address, boot_id: bootId, present: false,
            refreshing: false, last_attempt: Math.max(0, uptime),
            expires_boottime: Math.max(0, uptime), next_scan: null, pause_reason: "" }
    }

    function reconcile() {
        if (destroying) return
        if (!configured || !dependenciesReady || !bootId) {
            if (worker.running) worker.running = false
            return
        }
        if (worker.running) {
            if (activeSignature !== desiredSignature) {
                failures = 0
                reconfiguring = true
                retry.stop()
                worker.running = false
                stopWatchdog.restart()
            }
            return
        }
        if (!retry.running) startWorker()
    }

    function startWorker() {
        if (destroying || !configured || !dependenciesReady || !bootId || worker.running) return
        reconfiguring = false
        activeSignature = desiredSignature
        startedAt = Date.now()
        unavailable()
        worker.command = [pythonPath, workerPath, "worker", "--address", address, "--cache-dir", cacheDir]
        worker.running = true
    }

    function registerView() {
        if (destroying) return -1
        var token = ++nextViewToken
        var next = Object.assign({}, views)
        next[String(token)] = false
        views = next
        return token
    }

    function unregisterView(token) {
        if (destroying || token < 0) return
        var next = Object.assign({}, views)
        delete next[String(token)]
        views = next
        sendControl()
    }

    function setPanelOpen(token, opened) {
        if (destroying || !Object.prototype.hasOwnProperty.call(views, String(token))) return
        var wasOpen = views[String(token)] === true
        var next = Object.assign({}, views)
        next[String(token)] = opened === true
        views = next
        if (opened === true && !wasOpen) requestRefresh()
        else sendControl()
    }

    function requestRefresh() {
        if (destroying) return
        requestToken = Math.max(Date.now(), requestToken + 1)
        if (!worker.running) {
            failures = 0
            retry.stop()
            reconcile()
        }
        sendControl()
    }

    function sendControl() {
        if (destroying || !worker.running || !configured || !bootId) return
        worker.write(JSON.stringify({ type: "control", schema: 1, address: address,
            boot_id: bootId, idle: idleMonitor.isIdle, panel_open: panelOpen,
            request: requestToken }) + "\n")
    }

    function acceptLine(raw) {
        if (destroying || typeof raw !== "string" || raw.length > 16384) return
        var event
        try { event = JSON.parse(raw) } catch (error) { return }
        if (!event || typeof event !== "object" || Array.isArray(event)) return
        var value = event.type === "snapshot" ? event.snapshot : event.type === "status" ? event.status : null
        if (!value || value.schema !== 1 || value.address !== address || value.boot_id !== bootId) return
        if (event.type === "snapshot") {
            if (Snapshot.boundedNumber(value.co2, 1, 32767) === null || Math.floor(value.co2) !== value.co2
                || Snapshot.boundedNumber(value.seen_boottime, 0, 1e12) === null) return
            snapshot = value
        } else {
            if (typeof value.present !== "boolean" || typeof value.refreshing !== "boolean"
                || Snapshot.boundedNumber(value.last_attempt, 0, 1e12) === null
                || Snapshot.boundedNumber(value.expires_boottime, 0, 1e12) === null) return
            status = value
        }
        lastError = ""
        uptimeFile.reload()
    }

    onShellChanged: loadSettings()
    onManifestChanged: loadSettings()
    onBootIdChanged: reconcile()
    Component.onCompleted: { loadSettings(); checkSetup() }
    Component.onDestruction: {
        destroying = true
        retry.stop()
        stopWatchdog.stop()
        setupTimeout.stop()
        if (setupTask.running) {
            setupTask.stdinEnabled = false
            setupTask.running = false
        }
        if (worker.running) {
            worker.stdinEnabled = false
            worker.running = false
        }
    }

    Connections {
        target: root.shell
        function onBarConfigChanged() { root.loadSettings() }
    }

    Process {
        id: setupTask
        stdinEnabled: true
        stdout: StdioCollector { id: setupStdout; waitForEnd: true }
        stderr: SplitParser {
            onRead: function(data) {
                if (typeof data === "string" && data.length <= 16384)
                    setupStderr.text = data.slice(0, 320)
            }
        }
        onExited: function(exitCode) { root.finishSetup(exitCode) }
    }
    QtObject { id: setupStderr; property string text: "" }
    Timer {
        id: setupTimeout
        onTriggered: {
            root.setupFailure = "Setup took too long. Please try again."
            setupTask.running = false
        }
    }

    Process {
        id: worker
        stdinEnabled: true
        stdout: SplitParser {
            onRead: function(data) { root.acceptLine(data) }
        }
        stderr: SplitParser {
            onRead: function(data) {
                if (!root.destroying && typeof data === "string" && data.length <= 16384)
                    root.lastError = data.slice(0, 240).replace(/[\u0000-\u001f\u007f]/g, "")
            }
        }
        onStarted: root.sendControl()
        onExited: {
            stopWatchdog.stop()
            if (root.destroying) return
            root.unavailable()
            if (Date.now() - root.startedAt >= 30000) root.failures = 0
            if (root.reconfiguring) {
                root.reconfiguring = false
                retry.interval = 1
                retry.restart()
            } else if (root.configured && ++root.failures <= 5) {
                retry.interval = Math.min(60000, 1000 * Math.pow(2, root.failures - 1))
                retry.restart()
            }
        }
    }

    Timer {
        id: stopWatchdog
        interval: 3000
        onTriggered: if (!root.destroying && worker.running) worker.signal(9)
    }
    Timer {
        id: retry
        interval: 1000
        onTriggered: root.startWorker()
    }
    FileView {
        id: bootFile
        path: "/proc/sys/kernel/random/boot_id"
        printErrors: false
        onLoaded: root.bootId = text().trim()
        onLoadFailed: root.bootId = ""
    }
    FileView {
        id: uptimeFile
        path: "/proc/uptime"
        printErrors: false
        onLoaded: {
            var value = Number(text().trim().split(/\s+/)[0])
            root.uptime = isFinite(value) && value >= 0 ? value : -1
        }
        onLoadFailed: root.uptime = -1
    }
    IdleMonitor {
        id: idleMonitor
        timeout: 300
        respectInhibitors: true
        onIsIdleChanged: root.sendControl()
    }
    Timer {
        interval: 5000
        repeat: true
        triggeredOnStart: true
        running: root.configured && (root.panelOpen || (root.snapshot !== null && root.status !== null && root.status.present === true))
            && (!idleMonitor.isIdle || root.panelOpen)
        onTriggered: uptimeFile.reload()
    }
}
