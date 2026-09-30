"""The "your drops are now fully recharged" ping.

Drops are never ticked by a background task (see utils/economy.py), so nothing
happens on its own when a stack fills. Every spend already knows when that will
be, though, so it books a ping for that moment: the database keeps the due time
and the channel the player pulled in, and this cog keeps the same times in
memory and sleeps until the earliest one.

One task, no polling: it wakes only when a ping is due or a new booking might
be earlier than what it is waiting for. The database is read once at startup,
so bookings survive restarts and deploys, and a ping that came due while the
bot was down goes out as soon as it is back.
"""
import asyncio
import heapq
import logging
from typing import Literal, Optional

import discord
from discord.ext import commands

import db
from utils import economy
from utils.command_types import slash_only
from utils.logsetup import event

log = logging.getLogger("grails.recharge")

# Fire a moment after the computed time so the stack is certainly full when the
# database re-checks it.
GRACE_SECONDS = 1


class RechargeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._due = {}          # user_id -> due time (naive UTC); the current booking
        self._heap = []         # (due, user_id); entries that no longer match _due are skipped
        self._wake = asyncio.Event()
        self._task = None

    async def cog_load(self):
        self._task = asyncio.create_task(self._run())

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    def schedule(self, user_id, due):
        """Book (or move) a user's ping. `due` of None cancels it."""
        user_id = str(user_id)
        if due is None:
            self._due.pop(user_id, None)
        else:
            self._due[user_id] = due
            heapq.heappush(self._heap, (due, user_id))
        self._wake.set()

    async def refresh(self, user_id):
        """Re-read a user's booking from the database, after something other
        than a spend (a refund, a Baja Blast) may have moved or cleared it."""
        self.schedule(user_id, await asyncio.to_thread(db.get_full_notice, user_id))

    async def _run(self):
        try:
            for user_id, due in await asyncio.to_thread(db.pending_full_notices):
                # A spend made while this was loading is newer; keep it.
                if due and str(user_id) not in self._due:
                    self.schedule(user_id, due)
            event(log, "recharge pings", "%d pending", len(self._due))
        except Exception:
            log.exception("Could not load pending recharge pings")
        await self.bot.wait_until_ready()

        while True:
            # Drop entries a later booking replaced.
            while self._heap and self._due.get(self._heap[0][1]) != self._heap[0][0]:
                heapq.heappop(self._heap)

            self._wake.clear()
            if not self._heap:
                await self._wake.wait()
                continue

            due, user_id = self._heap[0]
            delay = (due - economy._utcnow()).total_seconds() + GRACE_SECONDS
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                continue    # woken early or on time: re-read the heap either way

            heapq.heappop(self._heap)
            self._due.pop(user_id, None)
            try:
                await self._fire(user_id)
            except Exception:
                log.exception("Recharge ping failed for %s", user_id)

    async def _fire(self, user_id):
        result = await asyncio.to_thread(db.take_full_notice, user_id)
        if result is None:
            return
        kind, value = result
        if kind == "later":
            if user_id not in self._due:    # a newer booking already covers it
                self.schedule(user_id, value)
            return

        channel_id = int(value)
        channel = self.bot.get_channel(channel_id)
        try:
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            await channel.send(f"<@{user_id}>, your drops are now fully recharged!!",
                               allowed_mentions=discord.AllowedMentions(users=True))
        except (discord.Forbidden, discord.NotFound):
            # Channel deleted or the bot lost access: nothing useful to retry.
            log.warning("Recharge ping for %s could not reach channel %s", user_id, channel_id)
            return
        event(log, "recharge ping", "%s", user_id)

    @commands.hybrid_command(name="notify",
                             description="Turn the \"drops fully recharged\" ping on or off")
    @slash_only()
    async def notify(self, ctx, setting: Optional[Literal["on", "off"]] = None):
        """Turn the "your drops are now fully recharged" ping on or off.

        Usage: /notify (flips it) — or /notify on / /notify off
        """
        on = None if setting is None else setting == "on"
        result = await asyncio.to_thread(db.set_recharge_ping, ctx.author.id, on)
        if result is None:
            await ctx.send("You're not registered yet — use `/register` first.", ephemeral=True)
        elif result:
            await ctx.send("🔔 You'll get pinged when your drops are fully recharged.", ephemeral=True)
        else:
            await ctx.send("🔕 Recharge pings are off. Use `/notify` to turn them back on.",
                           ephemeral=True)

async def setup(bot):
    await bot.add_cog(RechargeCog(bot))
