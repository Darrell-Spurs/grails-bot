"""Console logging: one readable line per event, coloured by what happened.

The default logging output was `INFO:grails.choice:...` -- nineteen characters
of prefix carrying almost no information, no timestamp at all, and nothing to
separate "a pull happened" from "a pull failed" at a glance.

Every line here is three columns:

    HH:MM:SS  [tag]  action  context

`tag` is where it came from, `action` is what happened and carries the colour,
and `context` is the detail. Lining the first three up means the eye can scan
straight down the tag or the action without reading any of the prose.

Colour goes on the action alone. Colouring whole lines makes a busy console
harder to read, not easier: the eye ends up tracking blocks of colour instead of
the column it wants.
"""
import logging
import os
import re
import sys

# --- ANSI ------------------------------------------------------------------

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

_C = {
    "grey": "\033[90m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "blue": "\033[34m", "magenta": "\033[35m",
    "cyan": "\033[36m", "white": "\033[37m",
    "bright_red": "\033[91m", "bright_green": "\033[92m",
    "bright_yellow": "\033[93m", "bright_blue": "\033[94m",
    "bright_magenta": "\033[95m", "bright_cyan": "\033[96m",
}

# Actions are matched on their first word, so "pull requested", "pull responded"
# and "pull selected" all read as one family and share a colour.
ACTION_COLORS = {
    "user": "bright_blue",       # a command was invoked
    "pull": "bright_magenta",    # the .c flow
    "vinyl": "bright_magenta",
    "mythic": "bright_yellow",   # rare enough to deserve its own colour
    "collection": "bright_green",
    "trade": "cyan",
    "xp": "green",
    "level": "bright_green",
    "register": "bright_green",
    "respond": "grey",           # timings are supporting detail, not events
    "startup": "cyan",
    "emoji": "grey",
    "drop": "yellow",
}

LEVEL_COLORS = {
    "DEBUG": "grey", "INFO": None, "WARNING": "bright_yellow",
    "ERROR": "bright_red", "CRITICAL": "bright_red",
}

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _supports_colour(stream):
    """True when writing ANSI to `stream` will render rather than print escapes."""
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if sys.platform != "win32":
        return True
    # Windows needs virtual-terminal processing switched on explicitly. Modern
    # Terminal and the VS Code console have it; the legacy console does not, and
    # would print the escape codes literally.
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _paint(text, colour, enabled, extra=""):
    if not enabled or not colour:
        return text
    return f"{extra}{_C.get(colour, '')}{text}{RESET}"


# --- Formatter -------------------------------------------------------------

TAG_WIDTH = 14
ACTION_WIDTH = 15


class TagFormatter(logging.Formatter):
    """Renders `HH:MM:SS [tag] action  context`.

    A record carries its action in `extra={"action": ...}`; anything logged the
    ordinary way still formats fine, it just has no action column, which is what
    makes this safe to drop in front of library loggers (discord.py, Flask)
    without touching them.
    """

    def __init__(self, colour=True):
        super().__init__(datefmt="%H:%M:%S")
        self.colour = colour

    def format(self, record):
        action = getattr(record, "action", None)
        context = record.getMessage()

        # The logger name is the tag, minus the project prefix every logger
        # shares. Third-party loggers keep their own name.
        tag = record.name
        if tag.startswith("grails."):
            tag = tag[len("grails."):]
        elif tag == "grails":
            tag = "bot"

        time_col = _paint(self.formatTime(record, self.datefmt), "grey", self.colour)
        tag_col = _paint(f"[{tag}]".ljust(TAG_WIDTH), "cyan", self.colour, DIM)

        if record.levelno >= logging.WARNING:
            # A problem outranks whatever the action was: the level takes the
            # colour so it cannot be lost among ordinary traffic.
            action = action or record.levelname.lower()
            colour = LEVEL_COLORS.get(record.levelname, "bright_red")
            action_col = _paint(action.ljust(ACTION_WIDTH), colour, self.colour, BOLD)
        elif action:
            colour = ACTION_COLORS.get(action.split()[0].lower(), "white")
            action_col = _paint(action.ljust(ACTION_WIDTH), colour, self.colour)
        else:
            action_col = " " * ACTION_WIDTH

        line = f"{time_col} {tag_col} {action_col} {context}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class PlainFormatter(TagFormatter):
    """The same layout with the colour stripped, for the log file."""

    def __init__(self):
        super().__init__(colour=False)

    def format(self, record):
        return _ANSI_RE.sub("", super().format(record))


# --- Call-site helper ------------------------------------------------------

def event(logger, action, context="", *args, level=logging.INFO, **kwargs):
    """Log one event: `event(log, "pull requested", "%s", user)`.

    Keeping the action out of the message is what lets the formatter align and
    colour it, and what stops the same idea being phrased five different ways
    across five call sites.
    """
    extra = kwargs.pop("extra", {})
    extra["action"] = action
    logger.log(level, context, *args, extra=extra, **kwargs)


# --- Setup -----------------------------------------------------------------

def setup_logging(level=logging.INFO, logfile=None, max_bytes=2_000_000, backups=5):
    """Install the formatter on the root logger.

    `logfile` also keeps a rotating plain-text copy. Without one the log only
    ever exists in the console and a restart loses it, which is the thing that
    makes an incident impossible to reconstruct after the fact.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(TagFormatter(colour=_supports_colour(sys.stderr)))
    root.addHandler(console)

    if logfile:
        from logging.handlers import RotatingFileHandler
        os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
        rotating = RotatingFileHandler(logfile, maxBytes=max_bytes,
                                       backupCount=backups, encoding="utf-8")
        rotating.setFormatter(PlainFormatter())
        root.addHandler(rotating)

    # discord.py narrates its own connection lifecycle at INFO, which drowns the
    # bot's own lines on every reconnect.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return root
