"""The drop economy: how choice pulls accrue, cap and are spent.

Separate from utils/odds.py on purpose. That file answers "what do you get when
you pull"; this one answers "may you pull at all". They change for different
reasons -- odds get balanced, the economy gets tuned -- and mixing them would
make either change look like the other.

Everything here is pure arithmetic on numbers and datetimes: no database, no
discord, no I/O. The rules are the part worth testing, and they are testable
without either.
"""
import datetime

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# One charge every three minutes, twenty in the bank. Sixty minutes to refill
# from empty, so a player who leaves for an hour comes back to a full stack and
# a player who is present all evening is never actually stopped.
PULL_REGEN_SECONDS = 180
PULL_CAP = 20

# What one `.c` costs.
PULL_COST = 1


# ---------------------------------------------------------------------------
# Regeneration
# ---------------------------------------------------------------------------

def _utcnow():
    """Naive UTC, matching how the database stores and returns timestamps."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def regenerate(charges, anchor, now=None):
    """Bring a stored (charges, anchor) pair up to date.

    Charges are never ticked by a background task -- they are derived whenever
    someone looks. That costs nothing while a player is idle, survives restarts
    exactly, and cannot drift.

    Returns the new (charges, anchor).

    The subtlety is the anchor. While below the cap it advances by whole
    regenerated charges only, *carrying the remainder*: a player 170 seconds
    into their next charge who checks their balance must still be 170 seconds
    in afterwards. Setting the anchor to `now` on every read would reset that
    progress each time, so a player who checked often would never regenerate at
    all.

    At the cap the remainder is meaningless, so the anchor snaps to `now` --
    the next charge is then a full interval after the stack stops being full,
    not instantly.
    """
    now = now or _utcnow()
    charges = max(int(charges or 0), 0)

    # No anchor means a brand-new or pre-economy account: start them full.
    if anchor is None:
        return min(charges or PULL_CAP, PULL_CAP), now

    if charges >= PULL_CAP:
        return PULL_CAP, now

    elapsed = (now - anchor).total_seconds()
    if elapsed < 0:
        # A clock that went backwards (NTP correction, a restored backup).
        # Re-anchoring is the conservative read: it costs at most one interval
        # and cannot mint charges out of nothing.
        return charges, now

    gained = int(elapsed // PULL_REGEN_SECONDS)
    if gained <= 0:
        return charges, anchor

    charges = min(charges + gained, PULL_CAP)
    if charges >= PULL_CAP:
        return PULL_CAP, now
    return charges, anchor + datetime.timedelta(seconds=gained * PULL_REGEN_SECONDS)


def seconds_to_next(charges, anchor, now=None):
    """Seconds until one more charge lands, or 0 when already at the cap."""
    if charges >= PULL_CAP:
        return 0
    now = now or _utcnow()
    if anchor is None:
        return PULL_REGEN_SECONDS
    elapsed = max((now - anchor).total_seconds(), 0)
    return max(PULL_REGEN_SECONDS - (elapsed % PULL_REGEN_SECONDS), 0)


def seconds_to_full(charges, anchor, now=None):
    """Seconds until the stack is full, or 0 when it already is."""
    if charges >= PULL_CAP:
        return 0
    missing = PULL_CAP - charges
    return seconds_to_next(charges, anchor, now) + (missing - 1) * PULL_REGEN_SECONDS


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------

def format_duration(seconds):
    """'1m 12s' / '18m' / '2h 05m'. Short enough to sit inline in an embed."""
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        # Seconds matter while the wait is short and stop mattering after that.
        return f"{minutes}m {secs}s" if minutes < 10 else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def charge_bar(charges, width=20):
    """A filled/empty bar. Width matches the cap so one block is one charge."""
    filled = max(0, min(int(charges), PULL_CAP))
    scaled = round(filled / PULL_CAP * width)
    return "▰" * scaled + "▱" * (width - scaled)
