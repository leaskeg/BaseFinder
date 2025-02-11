import logging
import os
import random
import re
import time
import asyncio
from datetime import datetime, timedelta, UTC
from threading import Lock
from typing import List, Dict, Optional, Set
from urllib.parse import unquote
import aiohttp
import nextcord
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from nextcord import Interaction, SlashOption, errors
from nextcord.ext import commands, application_checks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

COOLDOWN_PERIOD = 10 * 60
MAX_RETRIES = 3
QUOTA_LIMIT = 10000
MAX_LINKS_PER_REQUEST = 3
CACHE_DURATION = 3600
MAX_CACHE_ENTRIES = 100
key_rotation_lock = Lock()

link_cache = {}

try:
    load_dotenv()
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    API_KEYS = os.getenv("API_KEYS")

    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN is not set in .env file")
    if not API_KEYS:
        raise ValueError("API_KEYS is not set in .env file")

    API_KEYS = [key.strip() for key in API_KEYS.split(",") if key.strip()]
    if not API_KEYS:
        raise ValueError("No valid API keys found in .env file")

    logger.info(f"Successfully loaded {len(API_KEYS)} API keys")
except Exception as e:
    logger.critical(f"Failed to initialize bot configuration: {str(e)}")
    raise SystemExit(1)

API_KEY_QUOTA = {key: 0 for key in API_KEYS}
current_key_index = 0

BASE_LEVELS = ["TH15", "TH16", "TH17"]

base_link_pattern = re.compile(
    r"https://link\.clashofclans\.com/en/\?action=OpenLayout&id=TH(?:15|16|17)%3A[A-Z]+%3A[0-9A-Za-z\-_]+"
)


def load_channels() -> List[Dict[str, str]]:
    """Load channel data from channels.txt file."""
    try:
        channels = []
        with open("channels.txt", "r", encoding="utf-8") as file:
            for line in file:
                channel_id, name = line.strip().split("|")
                channels.append({"id": channel_id, "name": name})
        logger.info(f"Successfully loaded {len(channels)} channels from channels.txt")
        return channels
    except FileNotFoundError:
        logger.error("channels.txt file not found")
        return []
    except Exception as e:
        logger.error(f"Error loading channels from file: {str(e)}")
        return []


preloaded_channels = load_channels()


def get_youtube_client() -> Optional[object]:
    """Get a YouTube API client with automatic key rotation and quota management."""
    global current_key_index
    retries = 0

    while retries < len(API_KEYS):
        with key_rotation_lock:
            current_key_index = min(
                range(len(API_KEYS)), key=lambda i: API_KEY_QUOTA[API_KEYS[i]]
            )
            selected_key = API_KEYS[current_key_index]

        try:
            client = build(
                "youtube", "v3", developerKey=selected_key, cache_discovery=False
            )
            logger.debug(
                f"Successfully created YouTube client with key index {current_key_index}"
            )
            return client
        except HttpError as e:
            if "quotaExceeded" in str(e):
                with key_rotation_lock:
                    API_KEY_QUOTA[selected_key] = QUOTA_LIMIT
                retries += 1
                logger.warning(
                    f"Quota exceeded for API key {current_key_index}, attempting rotation"
                )
            else:
                logger.error(f"YouTube API error: {str(e)}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error creating YouTube client: {str(e)}")
            raise

    logger.error("All API keys exhausted")
    return None


async def fetch_video_links(
    youtube: object, video_ids: List[str], town_hall_level: str
) -> List[str]:
    """Fetch and extract base links from video descriptions."""
    if not youtube or not video_ids:
        return []

    try:
        API_KEY_QUOTA[API_KEYS[current_key_index]] += len(video_ids)
        video_request = youtube.videos().list(part="snippet", id=",".join(video_ids))
        video_response = video_request.execute()

        results = []
        for item in video_response.get("items", []):
            description = item["snippet"].get("description", "")
            links = extract_links(description, town_hall_level)
            if links:
                results.extend(links)
        return results
    except HttpError as e:
        logger.error(f"YouTube API error while fetching video links: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Error fetching video links: {str(e)}")
        return []


def extract_links(description: str, town_hall_level: Optional[str] = None) -> List[str]:
    """Extract and validate base links from text."""
    try:
        all_links = base_link_pattern.findall(description)
        if town_hall_level:
            pattern_encoded = f"id={town_hall_level}%3A"
            return [link for link in all_links if pattern_encoded in link]
        return all_links
    except Exception as e:
        logger.error(f"Error extracting links: {str(e)}")
        return []


async def search_channel_videos(
    youtube: object,
    channel_id: str,
    town_hall_level: str,
    base_type: str,
    max_videos: int = 5,
) -> List[str]:
    """Search for base links in channel videos."""
    if not youtube:
        return []

    try:
        # Format the date in RFC 3339 format without microseconds
        four_days_ago = (datetime.now(UTC) - timedelta(days=4)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        API_KEY_QUOTA[API_KEYS[current_key_index]] += 100
        search_request = youtube.search().list(
            part="snippet",
            channelId=channel_id,
            type="video",
            maxResults=max_videos,
            order="date",
            q=f"{town_hall_level} {base_type}",
            publishedAfter=four_days_ago,
        )
        search_response = search_request.execute()
        video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
        return await fetch_video_links(youtube, video_ids, town_hall_level)
    except HttpError as e:
        logger.error(f"YouTube API error while searching channel videos: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Error searching channel videos: {str(e)}")
        return []


def get_cache_key(town_hall_level: str, base_type: str) -> str:
    """Generate a cache key from search parameters."""
    return f"{town_hall_level}_{base_type}"


def get_cached_links(town_hall_level: str, base_type: str) -> Optional[List[str]]:
    """Get cached links if they exist and are not expired."""
    cache_key = get_cache_key(town_hall_level, base_type)
    if cache_key in link_cache:
        cache_entry = link_cache[cache_key]
        if time.time() - cache_entry["timestamp"] < CACHE_DURATION:
            links = cache_entry["links"]
            if links and len(links) > 0:
                return links
    return None


def cleanup_cache():
    """Remove expired entries and limit cache size."""
    current_time = time.time()

    expired_keys = [
        key
        for key, entry in link_cache.items()
        if current_time - entry["timestamp"] >= CACHE_DURATION
    ]
    for key in expired_keys:
        del link_cache[key]

    if len(link_cache) > MAX_CACHE_ENTRIES:
        sorted_entries = sorted(link_cache.items(), key=lambda x: x[1]["timestamp"])
        entries_to_remove = len(link_cache) - MAX_CACHE_ENTRIES
        for key, _ in sorted_entries[:entries_to_remove]:
            del link_cache[key]


def cache_links(town_hall_level: str, base_type: str, links: List[str]):
    """Cache links with timestamp."""
    cleanup_cache()
    cache_key = get_cache_key(town_hall_level, base_type)
    link_cache[cache_key] = {"links": links, "timestamp": time.time()}


async def search_preloaded_channels(
    town_hall_level: str, base_type: str, max_links: int = 3
) -> List[str]:
    """Search preloaded channels for base links with caching and parallel processing."""
    cached_links = get_cached_links(town_hall_level, base_type)
    if cached_links:
        return cached_links[:max_links]

    found_links: Set[str] = set()
    youtube = get_youtube_client()
    if not youtube:
        logger.error("Failed to get YouTube client")
        return []

    async def process_channel(channel):
        try:
            results = await search_channel_videos(
                youtube, channel["id"], town_hall_level, base_type
            )
            return results
        except Exception as e:
            logger.error(f"Error searching channel {channel['name']}: {str(e)}")
            return []

    tasks = [process_channel(channel) for channel in preloaded_channels]
    results = await asyncio.gather(*tasks)

    for channel_links in results:
        found_links.update(channel_links)

    final_links = list(found_links)[:max_links]

    cache_links(town_hall_level, base_type, final_links)

    return final_links


class BaseFinderBot(commands.Bot):
    def __init__(self):
        intents = nextcord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

    async def on_ready(self):
        """Handle bot startup."""
        try:
            logger.info(f"Bot is ready as {self.user}")
        except Exception as e:
            logger.error(f"Failed during bot startup: {str(e)}")


class BaseFinderCog(commands.Cog):
    def __init__(self, bot: BaseFinderBot):
        self.bot = bot

    @nextcord.slash_command(name="find_bases", description="Find Clash of Clans bases")
    @commands.cooldown(1, COOLDOWN_PERIOD)
    async def find_bases(
        self,
        interaction: nextcord.Interaction,
        base_level: str = SlashOption(
            name="base_level",
            description="Town Hall level (e.g., TH15, TH16, TH17)",
            required=True,
        ),
        base_type: str = SlashOption(
            name="base_type",
            description="Type of base (e.g., CWL, War, Legend)",
            required=True,
        ),
    ):
        """Handle base finding command with comprehensive error handling."""
        await interaction.response.defer(ephemeral=True)

        if base_level not in BASE_LEVELS:
            await interaction.followup.send(
                "❌ Invalid Town Hall level. Please use TH15, TH16, or TH17.",
                ephemeral=True,
            )
            return

        try:
            links = await search_preloaded_channels(
                base_level, base_type, max_links=MAX_LINKS_PER_REQUEST
            )

            if not links:
                await interaction.followup.send(
                    f"❌ No valid links found for **{base_level} ({base_type})** in the last 4 days. Please try again later.",
                    ephemeral=True,
                )
                return

            message = "\n".join(f"🔗 {link}" for link in links)
            try:
                await interaction.user.send(
                    f"🎯 **Clash of Clans Base Links for {base_level} ({base_type}):**\n\n{message}\n\n"
                    "📌 **Please Note:** While these base layouts are provided as-is, we recommend thoroughly inspecting them "
                    "for any potential gaps or issues before use.",
                    suppress_embeds=True,
                )
                await interaction.followup.send(
                    "✅ Links have been sent to your DMs!", ephemeral=True
                )
            except nextcord.Forbidden:
                await interaction.followup.send(
                    "❌ Unable to send you a DM. Please ensure your DMs are open.",
                    ephemeral=True,
                )
        except Exception as e:
            logger.error(f"Error processing find_bases command: {str(e)}")
            await interaction.followup.send(
                "❌ An unexpected error occurred. Please try again later.",
                ephemeral=True,
            )

    @find_bases.on_autocomplete("base_level")
    async def base_level_autocomplete(
        self,
        interaction: nextcord.Interaction,
        query: str,
    ):
        """Provide autocomplete suggestions for base levels."""
        matching_levels = [level for level in BASE_LEVELS if query.upper() in level]
        await interaction.response.send_autocomplete(matching_levels[:25])

    @find_bases.on_autocomplete("base_type")
    async def base_type_autocomplete(
        self,
        interaction: nextcord.Interaction,
        query: str,
    ):
        """Provide autocomplete suggestions for base types."""
        choices = ["CWL", "War", "Legend"]
        matching_types = [
            choice for choice in choices if query.lower() in choice.lower()
        ]
        await interaction.response.send_autocomplete(matching_types[:25])

    @commands.Cog.listener()
    async def on_disconnect(self):
        """Handle disconnection events."""
        logger.warning("Bot disconnected from Discord gateway")

    @commands.Cog.listener()
    async def on_resume(self):
        """Handle successful reconnections."""
        logger.info("Bot successfully resumed connection")

    @commands.Cog.listener()
    async def on_error(self, event, *args, **kwargs):
        """Global error handler for unexpected errors."""
        if isinstance(args[0], aiohttp.ClientConnectionResetError):
            logger.warning("Connection reset detected, attempting to reconnect...")
            return

        logger.error(
            f"Unexpected error occurred in {event}: {str(args[0])}", exc_info=True
        )


bot = BaseFinderBot()
bot.add_cog(BaseFinderCog(bot))

try:
    bot.run(DISCORD_TOKEN)
except Exception as e:
    logger.critical(f"Failed to start bot: {str(e)}")
    raise SystemExit(1)
