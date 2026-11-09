"""Pure scheduling policy for the Aranet4 broadcast listener.

All times are seconds on the caller's suspend-aware monotonic clock. This
module observes no hardware and creates no timers; the listener supplies
current context and executes the returned schedule.
"""

from dataclasses import dataclass


PRESENCE_GRACE = 90


@dataclass(frozen=True)
class Context:
    sleeping: bool = False
    locked: bool = False
    inactive: bool = False
    lid_closed: bool = False
    idle: bool = False
    on_battery: bool = False
    power_saver: bool = False


def pause_reason(context: Context, manual: bool = False) -> str:
    """Return why a scan must wait, or an empty string when permitted.

    Opening the panel can request a scan while the lid is closed on a docked
    laptop or while an idle hint is still clearing. It never overrides sleep,
    screen locking, or an inactive graphical session.
    """
    if context.sleeping:
        return "sleep"
    if context.locked:
        return "locked"
    if context.inactive:
        return "inactive"
    if not manual:
        if context.lid_closed:
            return "lid"
        if context.idle:
            return "idle"
    return ""


def present_interval(context: Context) -> int:
    """Normal refresh cadence while the sensor is believed to be nearby."""
    if context.power_saver:
        return 15 * 60
    if context.on_battery:
        return 10 * 60
    return 5 * 60


def absent_interval(misses: int) -> int:
    """Back off absent-device scans to 15, 30, then at most 60 minutes."""
    return (15 * 60, 30 * 60, 60 * 60)[min(3, max(1, misses)) - 1]


@dataclass
class Schedule:
    present: bool = False
    misses: int = 0
    next_scan: float = 0.0
    last_attempt: float = 0.0
    last_seen: float = 0.0
    expires: float = 0.0

    def record_result(self, found: bool, now: float, context: Context) -> None:
        """Schedule from an actual completed scan, keeping last-good history."""
        self.last_attempt = now
        self.present = found
        if found:
            self.misses = 0
            self.last_seen = now
            self.next_scan = now + present_interval(context)
            self.expires = self.next_scan + PRESENCE_GRACE
        else:
            self.misses += 1
            self.next_scan = now + absent_interval(self.misses)
            self.expires = now

    def request_check(self, now: float, invalidate: bool = False) -> None:
        """Make the next check immediate, optionally discarding old presence.

        Invalidation is appropriate after wake or a session/location change:
        the cached measurement is still history, but does not prove the
        sensor is currently nearby.
        """
        self.next_scan = now
        if invalidate:
            self.present = False
            self.expires = now
            self.misses = 0

    def adjust_power(self, now: float, context: Context) -> None:
        """Rebase a nearby sensor's refresh after a power-context change."""
        if self.present:
            self.next_scan = max(now, self.last_seen + present_interval(context))
            self.expires = self.next_scan + PRESENCE_GRACE
