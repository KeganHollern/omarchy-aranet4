function cleanMessage(value, limit) {
    return typeof value === "string"
        ? value.slice(0, limit).replace(/[\u0000-\u001f\u007f]/g, "").trim() : ""
}

function parseResult(raw, action) {
    if (typeof raw !== "string" || raw.length > 16384) return null
    var value
    try { value = JSON.parse(raw) } catch (error) { return null }
    if (!value || typeof value !== "object" || Array.isArray(value) || typeof value.ok !== "boolean") return null
    if (!value.ok) return { ok: false, error: cleanMessage(value.error, 320) || "Setup could not finish. Please try again." }
    if (action !== "discover") {
        if (typeof value.dependencies_ready !== "boolean") return null
        return { ok: true, dependencies_ready: value.dependencies_ready }
    }
    if (!Array.isArray(value.devices) || value.devices.length > 16) return null
    var devices = []
    var seen = {}
    for (var i = 0; i < value.devices.length; i++) {
        var device = value.devices[i]
        if (!device || typeof device.address !== "string" || !/^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$/.test(device.address)
            || typeof device.readable !== "boolean") return null
        var address = device.address.toUpperCase()
        if (seen[address]) continue
        seen[address] = true
        devices.push({ address: address, name: cleanMessage(device.name, 96) || "Aranet4", readable: device.readable })
    }
    return { ok: true, devices: devices }
}
