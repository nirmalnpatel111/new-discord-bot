import os
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ---------- Firebase ----------
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- Google Calendar ----------
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ================== CONFIG ==================
# How often to run the "extend events" loop
EXTEND_LOOP_SECONDS = 60
# Keep each active event at least this far into the future
ROLLING_HORIZON = timedelta(minutes=15)
# Only patch if fewer than this many minutes remain (reduces API calls)
TOP_UP_THRESHOLD = timedelta(minutes=10)
# Use UTC for simplicity; Calendar gets explicit timeZone = "UTC"
GCAL_TIMEZONE = "UTC"
PLACES = {"ieee", "mcgill", "ev", "home"}

# ================== INIT ====================
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CALENDAR_ID = os.getenv("CALENDAR_ID")

# Service account path (mounted in container)
SA_PATH = os.getenv("SA_PATH", "/app/serviceAccounts.json")

# firebase
cred = credentials.Certificate(SA_PATH)
firebase_admin.initialize_app(cred)
db = firestore.client()
events_ref = db.collection("events")

# Discord
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Google Calendar service account creds
SCOPES = ["https://www.googleapis.com/auth/calendar"]
sa_creds = SACredentials.from_service_account_file(SA_PATH, scopes=SCOPES)

def gcal():
    # Note: no domain-wide delegation needed because the target calendar
    # is explicitly shared with this service account.
    return build("calendar", "v3", credentials=sa_creds, cache_discovery=False)

def now_utc():
    return datetime.now(timezone.utc)

def to_rfc3339(dt: datetime) -> str:
    # Google API expects RFC3339; ensure tz aware and format with 'Z'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

# ------------- Calendar helpers -------------
def insert_calendar_event(summary: str, start_dt: datetime, end_dt: datetime, location_text: str = None) -> str:
    """Insert event and return its eventId."""
    service = gcal()
    body = {
        "summary": summary,
        "location": location_text or "",
        "start": {"dateTime": to_rfc3339(start_dt), "timeZone": GCAL_TIMEZONE},
        "end":   {"dateTime": to_rfc3339(end_dt),   "timeZone": GCAL_TIMEZONE},
        "description": "Auto-created by Discord work-session bot",
    }
    created = service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    return created["id"]

def patch_calendar_event_end(event_id: str, new_end: datetime):
    """Patch only the end time of an event."""
    service = gcal()
    body = {
        "end": {"dateTime": to_rfc3339(new_end), "timeZone": GCAL_TIMEZONE}
    }
    service.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()

# ------------- Session helpers -------------
async def create_event(user, location, guild_id):
    """Create a new active event if none exists + create Google Calendar event."""
    # Guard: no double-starts per guild (or across guilds if guild_id is None)
    q = events_ref.where("user_id", "==", user.id).where("end_time", "==", None)
    if guild_id is not None:
        q = q.where("guild_id", "==", guild_id)
    if list(q.limit(1).stream()):
        return None, "You already have an active event. Send 'stop' first.'"

    start_dt = now_utc()
    initial_end = start_dt + ROLLING_HORIZON

    username = getattr(user, "display_name", str(user))
    summary = f"{username} working at {location}"

    # Create calendar event
    try:
        event_id = insert_calendar_event(summary, start_dt, initial_end, location_text=location)
    except HttpError as e:
        logging.exception("Failed to insert calendar event")
        return None, f"Couldn't create Google Calendar event ({e}). Try again."

    # Persist Firestore doc
    doc_data = {
        "event": "work_session",
        "user_id": user.id,
        "username": str(user),
        "guild_id": guild_id,
        "location": location,
        "start_time": start_dt,
        "end_time": None,                 # becomes timestamp on stop
        "calendar_id": CALENDAR_ID,
        "calendar_event_id": event_id,
        "calendar_end": initial_end,      # what we last told Calendar
        "last_extend_check": start_dt
    }
    doc_ref = events_ref.document()
    doc_ref.set(doc_data)
    return doc_ref.id, None

async def stop_event(user, guild_id):
    """Stop the newest active event for the user, patching Calendar to exact stop time."""
    try:
        q = events_ref.where("user_id", "==", user.id).where("end_time", "==", None)
        if guild_id is not None:
            q = q.where("guild_id", "==", guild_id)
        snap = list(q.stream())
        if not snap and guild_id is not None:
            # cross-guild fallback if stopped in DM or different server
            snap = list(events_ref.where("user_id", "==", user.id).where("end_time", "==", None).stream())
        if not snap:
            return None, "You have no active event to stop."

        # pick the most recent by start_time
        def _get_start(doc):
            data = doc.to_dict()
            return data.get("start_time") or datetime.min.replace(tzinfo=timezone.utc)
        doc = max(snap, key=_get_start)
        data = doc.to_dict()

        stop_ts = now_utc()
        event_id = data.get("calendar_event_id")

        # Patch calendar first so UI is correct even if Firestore update races
        if event_id:
            try:
                patch_calendar_event_end(event_id, stop_ts)
            except HttpError:
                logging.exception("Failed to patch calendar event end on stop")

        # Mark Firestore ended
        doc.reference.update({
            "end_time": firestore.SERVER_TIMESTAMP,
            "calendar_end": stop_ts,
            "last_extend_check": stop_ts
        })
        return doc.id, None

    except Exception:
        logging.exception("Error stopping event")
        return None, "Sorry, something went wrong stopping your event."

# ------------- Background extender -------------
@tasks.loop(seconds=EXTEND_LOOP_SECONDS)
async def extend_active_events():
    """Every minute: look at all active sessions and top-up their end to now+15m if needed."""
    try:
        active = list(events_ref.where("end_time", "==", None).stream())
        if not active:
            return

        now = now_utc()
        for doc in active:
            data = doc.to_dict()
            event_id = data.get("calendar_event_id")
            if not event_id:
                continue  # nothing to extend

            current_end = data.get("calendar_end")
            if not isinstance(current_end, datetime):
                current_end = now  # be safe

            # If fewer than TOP_UP_THRESHOLD remain, extend to now + ROLLING_HORIZON
            if current_end - now <= TOP_UP_THRESHOLD:
                new_end = now + ROLLING_HORIZON
                try:
                    patch_calendar_event_end(event_id, new_end)
                    # Update Firestore mirror
                    doc.reference.update({
                        "calendar_end": new_end,
                        "last_extend_check": now
                    })
                except HttpError:
                    logging.exception(f"Failed to extend calendar event {event_id}")

    except Exception:
        logging.exception("Error in extend_active_events loop")

@extend_active_events.before_loop
async def _before_loop():
    await bot.wait_until_ready()

# ------------- Discord events -------------
@bot.event
async def on_ready():
    print(f"We are ready to go in, {bot.user.name}")
    if not extend_active_events.is_running():
        extend_active_events.start()

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    content = message.content.lower().strip()
    guild_id = message.guild.id if message.guild else None

    # START
    if content.startswith("start"):
        parts = content.split()
        if len(parts) == 1:
            await message.channel.send(
                f"{message.author.mention} Mention the location (choose one: {', '.join(sorted(PLACES))})."
            )
            await bot.process_commands(message)
            return

        location = None
        for place in PLACES:
            if place in content:
                location = place
                break

        if not location:
            await message.channel.send("Invalid location! Options: " + ", ".join(sorted(PLACES)))
            await bot.process_commands(message)
            return

        doc_id, err = await create_event(message.author, location, guild_id)
        if err:
            await message.channel.send(f"{message.author.mention} {err}")
        else:
            await message.channel.send(
                f"{message.author.mention} Starting event at **{location}**. "
                f"Calendar created and will keep extending. Event ID: `{doc_id}`"
            )

    # STOP
    elif content.startswith("stop"):
        doc_id, err = await stop_event(message.author, guild_id)
        if err:
            await message.channel.send(f"{message.author.mention} {err}")
        else:
            scope = "this server" if guild_id is not None else "your latest active session"
            await message.channel.send(
                f"{message.author.mention} Stopped {scope} (`{doc_id}`). Final end time recorded on Calendar."
            )

    await bot.process_commands(message)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=handler, log_level=logging.DEBUG)
