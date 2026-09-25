"""Controls for which invocation styles a command accepts.

Some commands are meant to be used one way only: the pull commands stay on the
prefix because they are typed dozens of times in a row, while the browsing and
trading commands are slash-only so their arguments come with autocomplete and
descriptions.

Why a check rather than rewriting each as an app_commands.command:
these bodies take a `Context`, and several of them (trade/offer/cancel) drive a
multi-step flow with views and follow-ups. Converting them wholesale would be a
far larger change than the user-visible result warrants -- the prefix form
simply refuses and points at the slash form, which is the same outcome.
"""
from discord.ext import commands


class NotRegistered(commands.CheckFailure):
    """Raised when someone who has never registered runs a player command.

    Registration is deliberately explicit -- it is what hands out the signature
    vinyl welcome gift -- so the bot asks rather than registering silently.
    """


class PrefixNotAllowed(commands.CheckFailure):
    """Raised when a slash-only command is invoked with the prefix."""

    def __init__(self, command_name):
        self.command_name = command_name
        super().__init__(f"{command_name} is available as a slash command only.")


class SlashNotAllowed(commands.CheckFailure):
    """Raised when a prefix-only command is invoked as a slash command."""

    def __init__(self, command_name):
        self.command_name = command_name
        super().__init__(f"{command_name} is available as a prefix command only.")


def slash_only():
    """Silently ignore the dot commands"""
    async def predicate(ctx):
        if ctx.interaction is None:
            raise PrefixNotAllowed(ctx.command.qualified_name)
        return True
    return commands.check(predicate)

