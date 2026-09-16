"""Reporting for errors a command's own handler does not recognise.

discord.py logs "Ignoring exception in command ..." only when a command has no
`.error` handler, or when its handler re-raises. A handler that inspects a few
error types and then simply falls off the end marks the error *handled*, so the
failure vanishes: the user sees nothing and the log stays clean.

That is how a plain NameError in `.c` went unnoticed. Every handler should end
by calling report_unhandled() so anything it did not expect is logged with a
traceback and acknowledged to the user.
"""
import logging

from discord.ext import commands

log = logging.getLogger("grails.errors")


async def report_unhandled(cog_log, ctx, error, command=None, user_message=None):
    """Log an unrecognised command error and tell the invoker.

    `error` is usually a CommandInvokeError wrapping the real exception; the
    `.original` is what gets the traceback, since the wrapper's own traceback
    stops at discord.py's dispatcher.
    """
    # Gate failures (wrong invocation style, missing role, not registered) are
    # already explained by the bot-wide handler, which runs for every command
    # including those with their own .error. Reporting them again would log a
    # non-bug with a traceback and send the user a second, contradictory reply.
    if isinstance(error, commands.CheckFailure):
        return

    original = getattr(error, "original", error)
    name = command or (ctx.command.qualified_name if ctx.command else "command")

    (cog_log or log).error(
        "Unhandled error in .%s invoked by %s (%s): %s",
        name,
        getattr(ctx.author, "id", "?"),
        type(original).__name__,
        original,
        exc_info=original,
    )

    try:
        await ctx.send(
            user_message
            or f"⚠️ Something went wrong running `{name}`. The error has been logged."
        )
    except Exception:
        # A failed send must not mask the error we are trying to report.
        (cog_log or log).warning("Could not deliver the error notice for .%s", name)
