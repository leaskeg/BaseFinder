import os
import re
import logging
import random
import asyncio
from typing import List, Dict, Set
from datetime import datetime, timedelta, UTC
from threading import Lock
import nextcord
from nextcord import Interaction, SlashOption
from nextcord.ext import commands
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Constants
LINK_PATTERN = re.compile(r"https://link\.clashofclans\.com/en/\?action=OpenLayout&id=TH(?:15|16|17)%3A[A-Z]+%3A[0-9A-Za-z\-_]+")
BASE_LEVELS = ["TH15", "TH16", "TH17"]
BASE_TYPES = ["War", "CWL", "Legend"]
MAX_RESULTS = 5
MAX_PER_VIDEO = 2
COOLDOWN_SECONDS = 600

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Globals
prev_links: Dict[str, Set[str]] = {}
key_rotation_lock = Lock()
current_key_index = 0

# Load environment
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
api_keys_raw = os.getenv("API_KEYS")
if not DISCORD_TOKEN or not api_keys_raw:
    raise EnvironmentError("DISCORD_TOKEN and API_KEYS must be set in the .env file")
API_KEYS: List[str] = [key.strip() for key in api_keys_raw.split(",") if key.strip()]

# YouTube client creation with persistent key until quota exceeded
def get_youtube_client():
    global current_key_index
    with key_rotation_lock:
        key = API_KEYS[current_key_index]
    logger.info(f"Using API key index {current_key_index}: {key[:6]}...")
    return build("youtube", "v3", developerKey=key, cache_discovery=False)

# Helper to execute YouTube requests with quota rotation
async def safe_execute(callable_fn, *args, **kwargs):
    global current_key_index
    retries = 0
    while retries < len(API_KEYS):
        try:
            return callable_fn(*args, **kwargs).execute()
        except HttpError as e:
            if "quotaExceeded" in str(e):
                logger.warning(f"Quota exceeded on key index {current_key_index}, rotating key.")
                with key_rotation_lock:
                    current_key_index = (current_key_index + 1) % len(API_KEYS)
                # Recreate client for new key
                retries += 1
                continue
            raise
    logger.error("All API keys exhausted.")
    return {}

# Load channels from file
def load_channels() -> List[str]:
    try:
        with open("channels.txt", "r", encoding="utf-8") as f:
            return [line.strip().split("|")[0] for line in f if line.strip()]
    except FileNotFoundError:
        return []
channels = load_channels()

# Core logic
def extract_links(description: str, th_level: str) -> List[str]:
    return [link for link in LINK_PATTERN.findall(description) if f"id={th_level}%3A" in link]

async def get_video_links(video_ids: List[str], th_level: str) -> List[str]:
    youtube = get_youtube_client()
    parts = "snippet"
    params = {'part': parts, 'id': ','.join(video_ids)}
    response = await safe_execute(youtube.videos().list, **params)
    links = []
    for item in response.get('items', []):
        video_links = extract_links(item['snippet'].get('description', ''), th_level)
        links.extend(video_links[:MAX_PER_VIDEO])
    return links

async def search_channel(channel_id: str, th_level: str, base_type: str) -> List[str]:
    logger.info(f"Searching channel {channel_id} for {th_level} {base_type} bases")
    youtube = get_youtube_client()
    published_after = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        'part': 'snippet',
        'channelId': channel_id,
        'type': 'video',
        'order': 'date',
        'maxResults': MAX_RESULTS,
        'q': th_level,
        'publishedAfter': published_after
    }
    response = await safe_execute(youtube.search().list, **params)
    video_ids = [item['id']['videoId'] for item in response.get('items', []) if base_type.lower() in item['snippet']['title'].lower()]
    return await get_video_links(video_ids, th_level)

async def find_base_links(th: str, base_type: str) -> List[str]:
    combo_key = f"{th}_{base_type}"
    used = prev_links.get(combo_key, set())
    results = []
    channels_shuffled = channels[:]
    random.shuffle(channels_shuffled)
    for channel in channels_shuffled:
        if len(results) >= MAX_RESULTS:
            break
        try:
            links = await search_channel(channel, th, base_type)
            for link in links:
                if link not in used and link not in results:
                    results.append(link)
                    if len(results) >= MAX_RESULTS:
                        break
        except Exception as e:
            logger.warning(f"Error on channel {channel}: {e}")
            continue
    prev_links[combo_key] = set(results)
    logger.info(f"Found {len(results)} new links for {th} {base_type}")
    return results

# Discord bot setup
intents = nextcord.Intents.default()
bot = commands.Bot(intents=intents)
class ClashBaseFinder(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @nextcord.slash_command(name="find_bases", description="Get Clash of Clans base links")
    @commands.cooldown(1, COOLDOWN_SECONDS, commands.BucketType.user)
    async def find_bases(
        self,
        interaction: Interaction,
        base_level: str = SlashOption(name="base_level", choices=BASE_LEVELS),
        base_type: str = SlashOption(name="base_type", choices=BASE_TYPES),
    ):
        await interaction.response.defer(ephemeral=True)
        links = await find_base_links(base_level, base_type)
        if not links:
            await interaction.followup.send(f"❌ No new base links found for {base_level} {base_type}", ephemeral=True)
            return
        msg = "\n".join(f"🔗 {link}" for link in links)
        try:
            await interaction.user.send(f"🏰 Bases for {base_level} ({base_type}):\n\n{msg}", suppress_embeds=True)
            await interaction.followup.send("✅ Check your DMs for the base links!", ephemeral=True)
        except nextcord.Forbidden:
            await interaction.followup.send("❌ Unable to DM you. Please check your DM settings.", ephemeral=True)

    async def on_application_command_error(self, interaction: Interaction, error: Exception):
        if isinstance(error, commands.CommandOnCooldown):
            retry = int(error.retry_after)
            m, s = divmod(retry, 60)
            await interaction.response.send_message(f"⏱️ Command cooldown, try again in {m}m{s}s.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Something went wrong.", ephemeral=True)

@bot.event
async def on_application_command_error(interaction: Interaction, error: Exception):
    cog = bot.get_cog("ClashBaseFinder")
    if cog:
        await cog.on_application_command_error(interaction, error)
bot.add_cog(ClashBaseFinder(bot))
bot.run(DISCORD_TOKEN)
