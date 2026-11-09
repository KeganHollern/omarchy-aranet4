.pragma library

function boundedNumber(value, minimum, maximum) {
    return typeof value === "number" && isFinite(value)
        && value >= minimum && value <= maximum ? value : null
}

function cleanName(value) {
    if (typeof value !== "string") return "Aranet4"
    var name = value.slice(0, 96).replace(/[\u0000-\u001f\u007f-\u009f\u2028-\u202e\u2066-\u2069]/g, "").trim()
    return name || "Aranet4"
}

function parse(raw) {
    if (typeof raw !== "string" || raw.length > 8192) return null
    try {
        var value = JSON.parse(raw)
        return value && typeof value === "object" && !Array.isArray(value) ? value : null
    } catch (error) {
        return null
    }
}

function validStatus(status, address, bootId, uptime) {
    return status && status.schema === 1 && typeof address === "string"
        && /^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(address) && bootId
        && status.address === address
        && status.boot_id === bootId && typeof status.present === "boolean"
        && boundedNumber(status.last_attempt, 0, uptime) !== null
}

function render(snapshot, address, bootId, uptime, status, allowCached) {
    var hidden = { text: "", tooltip: "" }
    if (!snapshot || snapshot.schema !== 1 || typeof address !== "string"
        || !/^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$/.test(address)
        || snapshot.address !== address || !bootId || snapshot.boot_id !== bootId
        || boundedNumber(uptime, 0, 1e12) === null) return hidden

    var seen = boundedNumber(snapshot.seen_boottime, 0, uptime)
    var co2 = boundedNumber(snapshot.co2, 1, 32767)
    if (seen === null || co2 === null || Math.floor(co2) !== co2) return hidden
    if (!allowCached) {
        if (validStatus(status, address, bootId, uptime)) {
            var expires = boundedNumber(status.expires_boottime, status.last_attempt, status.last_attempt + 86400)
            if (!status.present || expires === null || uptime >= expires) return hidden
        } else if (uptime - seen >= 120) return hidden
    }

    var lines = [cleanName(snapshot.name), "CO₂: " + co2 + " ppm"]
    var temperature = boundedNumber(snapshot.temperature, -50, 100)
    if (temperature !== null) {
        lines.push("Temperature: " + temperature.toFixed(1) + " °C / "
            + (temperature * 9 / 5 + 32).toFixed(1) + " °F")
    }
    var fields = [
        ["humidity", "Humidity", "%", 0, 100],
        ["pressure", "Pressure", " hPa", 1, 2000],
        ["battery", "Battery", "%", 0, 100]
    ]
    for (var index = 0; index < fields.length; index++) {
        var field = fields[index]
        var value = boundedNumber(snapshot[field[0]], field[3], field[4])
        if (value !== null) lines.push(field[1] + ": " + value + field[2])
    }
    var sampleAge = boundedNumber(snapshot.sample_age, 0, 65535)
    if (sampleAge !== null) {
        lines.push("Measurement: " + Math.floor(sampleAge + uptime - seen) + " seconds ago")
    }
    return { text: "CO₂ " + co2 + " ppm", tooltip: lines.join("\n") }
}
