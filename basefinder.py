import os
import re
import logging
import random
import time
import asyncio
from typing import List, Dict, Optional, Union
from datetime import datetime, timedelta, UTC
from threading import Lock

import nextcord
from nextcord import Interaction, SlashOption
from nextcord.ext import commands
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Constants
LINK_PATTERN = re.compile(r"https://link\\.clashofclans\\.com/en/\\?action=OpenLayout&id=TH(?:15|16|17)%3A[A-Z]+%3A[0-9A-Za-z\-_]+")
BASE_LEVELS = ["TH15", "TH16", "TH17"]
BASE_TYPES = ["War", "CWL", "Legend"]
MAX_RESULTS = 5
MAX_PER_VIDEO = 2
CACHE_DURATION = 3600
COOLDOWN_SECONDS = 600

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Globals
link_cache: Dict[str, Dict[str, Union[List[str], float]]] = {}
key_rotation_lock = Lock()

# Load environment
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
api_keys_raw = os.getenv("API_KEYS")

if not DISCORD_TOKEN or not api_keys_raw:
    raise EnvironmentError("DISCORD_TOKEN and API_KEYS must be set in the .env file")

API_KEYS: List[str] = [key.strip() for key in api_keys_raw.split(",") if key.strip()]
API_KEY_QUOTA = {key: 0 for key in API_KEYS}
current_key_index = 0

# YouTube client with rotation
def get_youtube_client():
    logger.info("Attempting to get YouTube client with API key rotation.")
    global current_key_index
    retries = 0
    while retries < len(API_KEYS):
        with key_rotation_lock:
            current_key_index = min(
                range(len(API_KEYS)), key=lambda i: API_KEY_QUOTA[API_KEYS[i]]
            )
            selected_key = API_KEYS[current_key_index]
        try:
            logger.info(f"Using API key index {current_key_index}: {selected_key[:6]}...")
            return build("youtube", "v3", developerKey=selected_key, cache_discovery=False)
        except HttpError as e:
            if "quotaExceeded" in str(e):
                with key_rotation_lock:
                    API_KEY_QUOTA[selected_key] = 999999
                retries += 1
            else:
                raise
    return None

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

def get_video_links(video_ids: List[str], th_level: str, youtube) -> List[str]:
    if not youtube:
        return []
    API_KEY_QUOTA[API_KEYS[current_key_index]] += len(video_ids)
    response = youtube.videos().list(part="snippet", id=','.join(video_ids)).execute()
    links = []
    for item in response.get("items", []):
        video_links = extract_links(item['snippet'].get('description', ''), th_level)
        if video_links:
            links.extend(video_links[:MAX_PER_VIDEO])
    return links

def search_channel(channel_id: str, th_level: str, base_type: str, youtube) -> List[str]:
    logger.info(f"Searching channel {channel_id} for {th_level} {base_type} bases")
    if not youtube:
        return []
    published_after = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    API_KEY_QUOTA[API_KEYS[current_key_index]] += 1
    response = youtube.search().list(
        part="snippet",
        channelId=channel_id,
        type="video",
        order="date",
        maxResults=5,
        q=th_level,
        publishedAfter=published_after
    ).execute()
    video_ids = [item['id']['videoId'] for item in response.get('items', []) if base_type.lower() in item['snippet']['title'].lower()]
    return get_video_links(video_ids, th_level, youtube)

def cache_key(th: str, base_type: str) -> str:
    return f"{th}_{base_type}"

def get_from_cache(th: str, base_type: str) -> Optional[List[str]]:
    key = cache_key(th, base_type)
    entry = link_cache.get(key)
    if entry and time.time() - entry['timestamp'] < CACHE_DURATION: # type: ignore
        return entry['links'] # type: ignore
    return None

def update_cache(th: str, base_type: str, links: List[str]):
    logger.info(f"Caching {len(links)} links for {th}:{base_type}")
    key = cache_key(th, base_type)
    link_cache[key] = {"links": links, "timestamp": time.time()}

async def find_base_links(th: str, base_type: str) -> List[str]:
    logger.info(f"Initiating search for {th} {base_type}")
    cached = get_from_cache(th, base_type)
    if isinstance(cached, list):
        return random.sample(cached, min(MAX_RESULTS, len(cached)))

    results = []
    shuffled_channels = channels[:]
    random.shuffle(shuffled_channels)

    for channel_id in shuffled_channels:
        if len(results) >= MAX_RESULTS:
            logger.info("Enough links collected, stopping channel iteration.")
            break
        try:
            youtube = get_youtube_client()
            if not youtube:
                logger.warning("No valid YouTube client available.")
                break
            links = await asyncio.to_thread(search_channel, channel_id, th, base_type, youtube)
            results.extend(links)
            if len(results) >= MAX_RESULTS:
                break
        except Exception:
            continue

    final = results[:MAX_RESULTS]
    logger.info(f"Total collected links: {len(results)}, returning top {len(final)}")
    update_cache(th, base_type, final)
    if isinstance(final, list) and final:
        usage_summary = "\n".join(
            f"🔑 Key {i} ({key[:6]}...): {count} calls"
            for i, (key, count) in enumerate(API_KEY_QUOTA.items()) if count > 0
        )
        logger.info("YouTube API usage summary:\n%s", usage_summary)
        return random.sample(final, len(final))
    return []

# Bot setup
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
            await interaction.followup.send(f"❌ No base links found for {base_level} {base_type}", ephemeral=True)
            return

        response = "\n".join(f"🔗 {link}" for link in links)
        try:
            await interaction.user.send(f"🏰 Bases for {base_level} ({base_type}):\n\n{response}", suppress_embeds=True) # type: ignore
            await interaction.followup.send("✅ Check your DMs for the base links!", ephemeral=True)
        except nextcord.Forbidden:
            await interaction.followup.send("❌ Unable to DM you. Please check your DM settings.", ephemeral=True)

    async def on_application_command_error(self, interaction: Interaction, error: Exception):
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = int(error.retry_after)
            minutes, seconds = divmod(retry_after, 60)
            await interaction.response.send_message(
                f"⏱️ This command is on cooldown. Try again in {minutes}m {seconds}s.", ephemeral=True
            )
        else:
            await interaction.response.send_message("⚠️ Something went wrong.", ephemeral=True)

@bot.event
async def on_application_command_error(interaction: Interaction, error: Exception):
    cog = bot.get_cog("ClashBaseFinder")
    if cog:
        await cog.on_application_command_error(interaction, error) # type: ignore

bot.add_cog(ClashBaseFinder(bot))
bot.run(DISCORD_TOKEN)
