import asyncio
import csv
import io
import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import quote

import discord
import requests
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from requests import RequestException


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("va-bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
MODERATOR_CHANNEL_ID = int(os.getenv("MODERATOR_CHANNEL_ID", "0"))

GOOGLE_SHEET_ID_VOICE_ACTOR = os.getenv("GOOGLE_SHEET_ID_VOICE_ACTOR", "")
GOOGLE_SHEET_ID_DUB_REQUEST = os.getenv("GOOGLE_SHEET_ID_DUB_REQUEST", "")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Form Responses 1")

# How long fetched sheet rows are cached before re-fetching from Google Sheets.
# Keeps repeated command usage from hammering the public CSV endpoint.
SHEET_CACHE_TTL_SECONDS = int(os.getenv("SHEET_CACHE_TTL_SECONDS", "45"))

# Delay before exiting when Discord rejects us at startup (see main()).
STARTUP_FAILURE_BACKOFF_SECONDS = int(os.getenv("STARTUP_FAILURE_BACKOFF_SECONDS", "300"))

# LLM drafting goes through OpenRouter (https://openrouter.ai), which exposes an
# OpenAI-compatible chat completions API. Pick a model id from
# https://openrouter.ai/models (models ending in ":free" cost nothing but have
# stricter rate limits).
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_TIMEOUT_SECONDS = int(os.getenv("OPENROUTER_TIMEOUT_SECONDS", "60"))


def validate_config() -> None:
    required = {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "MODERATOR_CHANNEL_ID": str(MODERATOR_CHANNEL_ID) if MODERATOR_CHANNEL_ID else "",
        "GOOGLE_SHEET_ID_VOICE_ACTOR": GOOGLE_SHEET_ID_VOICE_ACTOR,
        "GOOGLE_SHEET_ID_DUB_REQUEST": GOOGLE_SHEET_ID_DUB_REQUEST,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
        "OPENROUTER_MODEL": OPENROUTER_MODEL,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {joined}")


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------
#
# These sheets are read via their public CSV export endpoint rather than the
# authenticated Sheets API, so no service account / credentials file is
# needed. This ONLY works as long as both sheets stay shared as "Anyone with
# the link can view" — if that's ever changed to restricted access, these
# requests will start returning an HTML login page instead of CSV data (which
# is detected and raised as an error below, rather than failing silently).

_sheet_cache: dict[str, tuple[float, list[dict]]] = {}
_sheet_cache_lock = asyncio.Lock()


def _sheet_csv_url(sheet_id: str, worksheet_name: str) -> str:
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={quote(worksheet_name)}"
    )


def _fetch_sheet_records_sync(sheet_id: str) -> list[dict]:
    """Blocking network call — always run this via asyncio.to_thread, never
    awaited directly, or it will stall the bot's event loop."""
    url = _sheet_csv_url(sheet_id, GOOGLE_WORKSHEET_NAME)
    response = requests.get(url, timeout=30)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")
    if "csv" not in content_type:
        raise RuntimeError(
            f"Sheet {sheet_id} did not return CSV data (got content-type '{content_type}'). "
            "It likely isn't shared as 'Anyone with the link can view', or the worksheet "
            f"name '{GOOGLE_WORKSHEET_NAME}' doesn't match an actual tab."
        )

    reader = csv.DictReader(io.StringIO(response.text))
    return list(reader)


async def get_sheet_records(sheet_id: str) -> list[dict]:
    """Fetch sheet rows, using a short-lived cache so repeated command usage
    doesn't hammer the endpoint unnecessarily."""
    now = time.monotonic()

    async with _sheet_cache_lock:
        cached = _sheet_cache.get(sheet_id)
        if cached is not None and (now - cached[0]) < SHEET_CACHE_TTL_SECONDS:
            logger.debug("Serving cached sheet records for %s", sheet_id)
            return cached[1]

    try:
        records = await asyncio.to_thread(_fetch_sheet_records_sync, sheet_id)
    except (RequestException, RuntimeError, csv.Error) as exc:
        # Better to serve slightly-stale data than fail outright if we have it.
        cached = _sheet_cache.get(sheet_id)
        if cached is not None:
            logger.warning("Sheet fetch failed (%s); serving stale cache instead", exc)
            return cached[1]
        raise

    async with _sheet_cache_lock:
        _sheet_cache[sheet_id] = (now, records)
    return records


def _normalize(text: str) -> str:
    return " ".join(str(text).lower().split())


def find_value(row: dict, *keywords: str) -> str:
    """Return the value of the first column whose header contains all keywords."""
    for key, value in row.items():
        norm = _normalize(key)
        if all(kw in norm for kw in keywords):
            return str(value).strip()
    return ""


def find_row(records: list[dict], keywords: tuple[str, ...], target_text: str) -> dict | None:
    target = target_text.strip().lower().lstrip("@")
    if not target:
        return None
    # exact match first
    for row in records:
        value = find_value(row, *keywords).strip().lower().lstrip("@")
        if value == target:
            return row
    # fall back to substring match
    for row in records:
        value = find_value(row, *keywords).strip().lower().lstrip("@")
        if value and (target in value or value in target):
            return row
    return None


# ---------------------------------------------------------------------------
# LLM drafting (OpenRouter)
# ---------------------------------------------------------------------------

def _call_llm_sync(prompt: str) -> str:
    """Blocking network call — always run this via asyncio.to_thread."""
    response = requests.post(
        f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": "VA Forum Discord Bot",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=OPENROUTER_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()

    # OpenRouter can return HTTP 200 with an "error" object (e.g. upstream
    # provider failure), so check for it explicitly.
    if "error" in body:
        raise ValueError(f"OpenRouter error: {body['error']}")

    choices = body.get("choices") or []
    content = (choices[0].get("message", {}).get("content") if choices else None) or ""
    return content.strip() or "I could not generate a response right now."


async def call_llm(prompt: str) -> str:
    logger.info("Requesting draft from OpenRouter model '%s'...", OPENROUTER_MODEL)
    start = time.monotonic()
    result = await asyncio.to_thread(_call_llm_sync, prompt)
    logger.info("OpenRouter responded in %.1fs", time.monotonic() - start)
    return result


def build_va_application_prompt(info: dict) -> str:
    return (
        "You are a moderator assistant for a Discord voice-acting community. "
        "Review this new Voice Actor application using ONLY the fields below "
        "(a working sample link, and clear vocal range / character types, are the key signals of quality). "
        "Decide whether it should be approved.\n"
        "- If it should be approved, write a short, friendly Discord message welcoming them to the VA "
        "roster, and explicitly reference 1-2 specific details from their application as the reason "
        "(e.g. their vocal range, character types, or sample link) so it's clear the decision is grounded "
        "in what they submitted, not generic.\n"
        "- If it should NOT be approved yet (e.g. no sample link, or answers are too vague/empty), write "
        "a short, friendly message explaining specifically what's missing or unclear and asking them to "
        "provide it.\n"
        "Keep it under 100 words, no headers or markdown, ready to paste directly into Discord.\n\n"
        f"Applicant: {info['name']} (Discord: {info['discord']})\n"
        f"Age Range: {info['age_range']}\n"
        f"Sample Link: {info['sample_link'] or 'NOT PROVIDED'}\n"
        f"Vocal Type: {info['vocal_type']}\n"
        f"Vocal Range: {info['vocal_range']}\n"
        f"Character Types: {info['character_types']}\n"
        f"Roles Willing To Take: {info['roles']}\n"
        f"Accents: {info['accents']}\n"
    )


def build_va_request_prompt(info: dict) -> str:
    return (
        "You are a helpful Discord community manager. "
        "Write a short, friendly message to a project creator confirming we have received their request for voice actors. "
        "Structure it roughly like: 'Hi [Creator Name], your request for [briefly summarize the project or voice needed] has been taken. We will get back to you soon.' "
        "Keep it under 50 words. Do not include placeholders, markdown headers, or subject lines. "
        "Write it as a final message ready to copy and paste.\n\n"
        f"Creator: {info['creator_name']}\n"
        f"Project: {info['project_desc']}\n"
        f"Voice Needed: {info['voice_needed']}\n"
    )


def build_va_pitch_prompt(info: dict) -> str:
    va_name = info['specific_va'] or "Voice Actor"
    
    return (
        "You are a helpful Discord community manager reaching out to talent. "
        f"Write a short, friendly message to a Voice Actor named {va_name}. "
        "Structure it roughly like: 'Hi [VA Name], we have a new project from [Creator Name]. "
        "[Briefly describe the project and the voice needed in 1-2 sentences]. Would you like to participate?' "
        "Keep it under 75 words. Do not include placeholders, markdown headers, or subject lines. "
        "Write it as a final message ready to copy and paste.\n\n"
        f"Creator: {info['creator_name']}\n"
        f"Project: {info['project_desc']}\n"
        f"Voice Needed: {info['voice_needed']}\n"
    )


# ---------------------------------------------------------------------------
# Moderator review UI
# ---------------------------------------------------------------------------

@dataclass
class ReviewContext:
    kind: str
    subject_name: str
    subject_handle: str
    draft_reply: str


class EditReplyModal(discord.ui.Modal, title="Edit Reply"):
    edited_reply = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    def __init__(self, parent_view: "ModeratorDecisionView"):
        super().__init__()
        self.parent_view = parent_view
        self.edited_reply.default = parent_view.ctx.draft_reply

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.parent_view.ctx.draft_reply = str(self.edited_reply.value)
        await self.parent_view.update_draft_text(
            interaction=interaction,
            status_line=f"Status: edited by {interaction.user.mention} — ready to copy and send",
        )


class ModeratorDecisionView(discord.ui.View):
    """Posts an AI-drafted reply for a moderator to review. The bot never sends
    anything to the applicant/creator itself — these buttons only mark status
    and let a moderator tweak the wording before copying it out manually."""

    def __init__(self, ctx: ReviewContext):
        super().__init__(timeout=86400)
        self.ctx = ctx
        self.moderator_message: discord.Message | None = None

    def _base_text(self) -> str:
        return (
            f"**{self.ctx.kind}** — {self.ctx.subject_name} ({self.ctx.subject_handle})\n\n"
            f"Suggested Reply (copy and send manually):\n{self.ctx.draft_reply}"
        )

    async def update_draft_text(self, interaction: discord.Interaction, status_line: str) -> None:
        content = f"{self._base_text()}\n\n{status_line}"
        if self.moderator_message is not None:
            await self.moderator_message.edit(content=content, view=self)
        if interaction.response.is_done():
            await interaction.followup.send("Updated.", ephemeral=True)
        else:
            await interaction.response.send_message("Updated.", ephemeral=True)

    async def _lock(self, interaction: discord.Interaction, status_line: str) -> None:
        for child in self.children:
            child.disabled = True
        content = f"{self._base_text()}\n\n{status_line}"
        if self.moderator_message is not None:
            await self.moderator_message.edit(content=content, view=self)
        await interaction.response.send_message("Marked.", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Approve (ready to send)", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._lock(
            interaction,
            f"Status: approved by {interaction.user.mention} — copy the reply above and send it manually.",
        )

    @discord.ui.button(label="Edit Wording", style=discord.ButtonStyle.primary)
    async def edit_before_send(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(EditReplyModal(self))

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._lock(interaction, f"Status: rejected by {interaction.user.mention}")


async def post_for_review(
    interaction: discord.Interaction,
    bot: commands.Bot,
    kind: str,
    subject_name: str,
    subject_handle: str,
    draft: str,
) -> None:
    mod_channel = bot.get_channel(MODERATOR_CHANNEL_ID)
    if mod_channel is None:
        await interaction.followup.send(
            "Moderator channel not found. Check MODERATOR_CHANNEL_ID.", ephemeral=True
        )
        return

    ctx = ReviewContext(
        kind=kind,
        subject_name=subject_name,
        subject_handle=subject_handle,
        draft_reply=draft,
    )

    view = ModeratorDecisionView(ctx)
    sent_message = await mod_channel.send(view._base_text(), view=view)
    view.moderator_message = sent_message

    await interaction.followup.send("Draft sent to the moderator channel for review.", ephemeral=True)
    logger.info("Posted %s draft for '%s' to moderator channel", kind, subject_handle)


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

class VABot(commands.Bot):
    async def setup_hook(self) -> None:
        # setup_hook runs once per process start. on_ready fires on every
        # gateway reconnect, so syncing there caused repeated API calls.
        # Set SYNC_COMMANDS=false to skip syncing entirely on boot (e.g. when
        # commands haven't changed) and cut down on requests to Discord.
        if os.getenv("SYNC_COMMANDS", "true").lower() != "true":
            logger.info("SYNC_COMMANDS is not 'true'; skipping slash command sync")
            return
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d command(s) globally", len(synced))
        except discord.DiscordException:
            logger.exception("Slash command sync failed")


def build_bot() -> commands.Bot:
    # Only slash commands are used, so the privileged message_content intent
    # is not needed.
    intents = discord.Intents.default()
    bot = VABot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "unknown")

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        logger.error("Unhandled app command error: %s", type(error).__name__, exc_info=error)
        message = "Something went wrong. Please try again in a bit."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            # Discord/Cloudflare is rejecting us; don't pile on more requests.
            logger.warning("Could not deliver error message to user (Discord rejected the request)")

    @bot.tree.command(
        name="auto_msg_va",
        description="Look up a Voice Actor application and draft a reply for moderator review",
    )
    @app_commands.describe(username="The Discord username exactly as they typed it on the VA application form")
    async def auto_msg_va(interaction: discord.Interaction, username: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            records = await get_sheet_records(GOOGLE_SHEET_ID_VOICE_ACTOR)
        except (RequestException, RuntimeError, csv.Error) as exc:
            logger.warning("Voice Actor sheet read failed: %s", exc)
            await interaction.followup.send(
                "Could not read the Voice Actor sheet right now. Check the sheet sharing settings and try again.",
                ephemeral=True,
            )
            return

        row = find_row(records, ("discord",), username)
        if row is None:
            await interaction.followup.send(f"No VA application found for `{username}`.", ephemeral=True)
            return

        info = {
            "name": find_value(row, "display name") or find_value(row, "va name") or "Unknown",
            "discord": find_value(row, "discord") or username,
            "age_range": find_value(row, "age"),
            "sample_link": find_value(row, "sample"),
            "vocal_type": find_value(row, "vocal presentations") or find_value(row, "presentations"),
            "vocal_range": find_value(row, "vocal range"),
            "character_types": find_value(row, "character"),
            "roles": find_value(row, "kind of roles") or find_value(row, "roles"),
            "accents": find_value(row, "accent"),
        }

        try:
            draft = await call_llm(build_va_application_prompt(info))
        except (RequestException, ValueError) as exc:
            logger.warning("LLM draft failed for VA application '%s': %s", username, exc)
            await interaction.followup.send(
                "Could not generate a draft right now. Please try again shortly.",
                ephemeral=True,
            )
            return

        await post_for_review(
            interaction=interaction,
            bot=bot,
            kind="Voice Actor Application",
            subject_name=info["name"],
            subject_handle=info["discord"],
            draft=draft,
        )

    @bot.tree.command(
        name="auto_msg_request",
        description="Look up a VA request submission and draft a reply for moderator review",
    )
    @app_commands.describe(username="The creator name exactly as they typed it on the VA request form")
    async def auto_msg_request(interaction: discord.Interaction, username: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            records = await get_sheet_records(GOOGLE_SHEET_ID_DUB_REQUEST)
        except (RequestException, RuntimeError, csv.Error) as exc:
            logger.warning("VA Request sheet read failed: %s", exc)
            await interaction.followup.send(
                "Could not read the VA Request sheet right now. Check the sheet sharing settings and try again.",
                ephemeral=True,
            )
            return

        row = find_row(records, ("creator", "name"), username)
        if row is None:
            await interaction.followup.send(f"No VA request found for `{username}`.", ephemeral=True)
            return

        info = {
            "creator_name": find_value(row, "creator", "name") or username,
            "channel_link": find_value(row, "channel"),
            "project_desc": find_value(row, "description"),
            "voice_needed": find_value(row, "kind of voice"),
            "paid": find_value(row, "paid"),
            "requesting_specific": find_value(row, "requesting"),
            "specific_va": find_value(row, "write down who"),
        }

        try:
            # Generate BOTH drafts sequentially
            creator_draft = await call_llm(build_va_request_prompt(info))
            
            # Only generate a pitch draft if they actually asked for a specific VA
            if info['specific_va'] and info['specific_va'].lower() != "none":
                pitch_draft = await call_llm(build_va_pitch_prompt(info))
                final_combined_draft = f"**To the Creator:**\n{creator_draft}\n\n**To the VA:**\n{pitch_draft}"
            else:
                final_combined_draft = f"**To the Creator:**\n{creator_draft}"

        except (RequestException, ValueError) as exc:
            logger.warning("LLM draft failed for VA request '%s': %s", username, exc)
            await interaction.followup.send(
                "Could not generate a draft right now. Please try again shortly.",
                ephemeral=True,
            )
            return

        await post_for_review(
            interaction=interaction,
            bot=bot,
            kind="VA Request",
            subject_name=info["creator_name"],
            subject_handle=info["creator_name"],
            draft=final_combined_draft,
        )

    return bot


def main() -> None:
    validate_config()
    bot = build_bot()
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        logger.critical("Discord login failed — check DISCORD_TOKEN")
        raise
    except discord.HTTPException as exc:
        # Usually a 429 / Cloudflare 1015 IP ban. Exiting immediately would make
        # the host restart us in a tight loop, generating more blocked requests
        # and extending the ban, so wait before exiting.
        logger.critical(
            "Discord HTTP error at startup (status=%s); sleeping %ss before exit to avoid a restart loop",
            exc.status,
            STARTUP_FAILURE_BACKOFF_SECONDS,
        )
        time.sleep(STARTUP_FAILURE_BACKOFF_SECONDS)
        raise


if __name__ == "__main__":
    main()