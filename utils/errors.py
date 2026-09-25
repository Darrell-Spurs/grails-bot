"""The one place a failed command is turned into a reply.

discord.py tries a command's own `.error` handler first and the bot-wide
on_command_error after it. Every command used to carry its own handler -- 28 of
them, each re-deciding the same few cases -- and the two layers each assumed the
other would answer: on_command_error skipped any command that had a handler,
while the handlers skipped check failures because "the bot-wide handler
explains them". Half of them also ended in `raise error`, which bypasses
on_command_error entirely, so an unexpected failure there got no reply at all
and reached the log only as discord.py's generic "Ignoring exception".

Now on_command_error hands every error to handle_command_error(). The one thing
that really differed per command -- the wording of its usage help -- lives on
the command itself, in `extras`:

    @commands.command(extras={
        "missing_arg": "reply when a required argument is missing",
        "bad_arg": "reply when an argument will not convert",
        "error_reply": "reply for an unexpected failure (it is still logged)",
    })

Every key is optional. Without one the reply is built from the command's own
signature, so a new command gets sensible help without writing any.
"""
import logging

from discord import app_commands
from discord.ext import commands

from utils.command_types import NotRegistered, PrefixNotAllowed, SlashNotAllowed

log = logging.getLogger("grails.errors")

REGISTER_FIRST = "You need an account first — run **/register** to get started."
NOT_ALLOWED = "You can't use that command here."


def _usage(ctx):
    """How the command is invoked, from its own signature."""
    prefix = "/" if ctx.interaction is not None else (ctx.clean_prefix or ".")
    return f"`{prefix}{ctx.command.qualified_name} {ctx.command.signature}`".replace(" `", "`")


def _extra(ctx, key):
    return (getattr(ctx.command, "extras", None) or {}).get(key)


def _reply_for(ctx, error):
    """The reply for an error the user can do something about, or None if the
    error is a bug (or is deliberately answered with silence -- see ignored)."""
    if isinstance(error, commands.MissingRole):
        return f"❌ You need the '{error.missing_role}' role to use this command!"
    if isinstance(error, commands.MissingAnyRole):
        roles = ", ".join(f"'{r}'" for r in error.missing_roles)
        return f"❌ You need one of these roles to use this command: {roles}"
    if isinstance(error, commands.CommandOnCooldown):
        return f"⏳ Cool down! Try again in {round(error.retry_after)}s."
    if isinstance(error, (commands.MissingRequiredArgument, commands.MissingRequiredAttachment)):
        return (_extra(ctx, "missing_arg")
                or f"❌ **Missing argument:** `{error.param.name}`\n**Usage:** {_usage(ctx)}")
    if isinstance(error, (commands.UserInputError, app_commands.TransformerError)):
        return (_extra(ctx, "bad_arg")
                or f"❌ **Invalid argument!** {error}\n**Usage:** {_usage(ctx)}")
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return NOT_ALLOWED
    return None


async def handle_command_error(ctx, error):
    """Answer one command error: explain a user mistake, or log a bug and say so."""
    # A hybrid command used as a slash command wraps errors raised by the
    # app-command layer (option conversion, app-level checks) in this.
    if isinstance(error, commands.HybridCommandError):
        error = error.original

    # Wrong invocation style for a prefix-only or slash-only command is ignored
    # on purpose: the user just used the other form.
    if isinstance(error, (commands.CommandNotFound, PrefixNotAllowed, SlashNotAllowed)):
        return
    if isinstance(error, NotRegistered):
        await _send(ctx, REGISTER_FIRST)
        return
    if ctx.command is None:
        return

    reply = _reply_for(ctx, error)
    if reply is not None:
        await _send(ctx, reply)
        return
    await report_unhandled(log, ctx, error, user_message=_extra(ctx, "error_reply"))


async def _send(ctx, text):
    try:
        await ctx.send(text)
    except Exception:
        # A reply that cannot be delivered (an expired interaction, a channel
        # the bot lost access to) must not turn into a second error.
        log.warning("Could not deliver the error reply for .%s", ctx.command)


async def report_unhandled(cog_log, ctx, error, command=None, user_message=None):
    """Log an unexpected command error with its traceback, and tell the invoker.

    `error` usually arrives wrapped (CommandInvokeError, or an app-command
    CommandInvokeError inside a hybrid one); the innermost exception is the one
    logged, since the wrappers' own tracebacks stop at discord.py's dispatcher.
    """
    original = error
    while getattr(original, "original", None) is not None:
        original = original.original
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
