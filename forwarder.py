import os
import re
import logging
from typing import Set, Optional, Dict
from dotenv import load_dotenv

import aiohttp
import discord

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ForwarderBot")

def load_names(file_path: str) -> Set[str]:
    names: Set[str] = set()
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    name = line.strip()
                    if name:
                        names.add(name.lower())
            logger.info(f"Loaded {len(names)} names from {file_path}")
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
    else:
        logger.warning(f"File not found: {file_path}")
    return names


def extract_bot_name_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    # Generalized regex to catch "hatched", "claimed drops", etc.
    # Looks for "Bot [Name] has successfully"
    # Also handles bold markdown (e.g. "**Name**")
    m = re.search(r"Bot\s+(?:\*\*)?([^\s\*]+)(?:\*\*)?\s+has successfully", text, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return None


async def send_to_webhook(url: str, message: discord.Message):
    logger.debug(f"Sending to webhook: {url[:30]}...")
    async with aiohttp.ClientSession() as session:
        webhook = discord.Webhook.from_url(url, session=session)
        try:
            await webhook.send(
                content=message.content or None,
                embeds=message.embeds if message.embeds else None,
                wait=False,
            )
            logger.info("Successfully sent to webhook")
        except Exception as e:
            logger.error(f"Failed to send to webhook: {e}")


class Forwarder(discord.Client):
    def __init__(self, source_channel_id: int, clients_config: Dict[str, dict]):
        """
        clients_config: {
            "CLIENT_NAME": {
                "webhook": "url",
                "names": {"name1", "name2", ...}
            },
            ...
        }
        """
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.source_channel_id = source_channel_id
        self.clients_config = clients_config

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (id={self.user.id})")
        logger.info(f"Listening on channel ID: {self.source_channel_id}")
        logger.info(f"Loaded clients: {list(self.clients_config.keys())}")

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        
        # Log all messages in the channel to debug ID mismatch or other issues
        if message.channel.id != self.source_channel_id:
            return

        logger.info(f"Processing message from {message.author}: {message.content[:50]}...")

        text_candidates = [message.content or ""]
        for e in message.embeds:
            d = e.to_dict()
            if "description" in d and d["description"]:
                text_candidates.append(str(d["description"]))
            if "title" in d and d["title"]:
                text_candidates.append(str(d["title"]))
            # Check footer text
            if "footer" in d and "text" in d["footer"]:
                text_candidates.append(str(d["footer"]["text"]))
            # Check author name
            if "author" in d and "name" in d["author"]:
                text_candidates.append(str(d["author"]["name"]))
            
            for field in d.get("fields", []):
                if "name" in field and field["name"]:
                    text_candidates.append(str(field["name"]))
                if "value" in field and field["value"]:
                    text_candidates.append(str(field["value"]))
        
        bot_name: Optional[str] = None
        for t in text_candidates:
            bot_name = extract_bot_name_from_text(t)
            if bot_name:
                logger.debug(f"Found bot name candidate: {bot_name} in text: {t[:50]}...")
                break
        
        if not bot_name:
            # logger.warning("Could not extract bot name from message") 
            return
        
        lname = bot_name.lower()
        logger.debug(f"Extracted bot name (lower): {lname}")
        
        # Iterate through all configured clients and check matches
        match_found = False
        for client_name, config in self.clients_config.items():
            if lname in config["names"]:
                logger.info(f"Match found in {client_name} list: {lname}")
                if config["webhook"]:
                    for url in config["webhook"]:
                        await send_to_webhook(url, message)
                    match_found = True
                else:
                    logger.warning(f"{client_name} match found but no webhook URL configured")
        
        if not match_found:
            logger.info(f"Bot name {lname} not found in any client list")


def load_clients_from_env() -> Dict[str, dict]:
    """
    Parses environment variables to find pairs of WEBHOOK_{NAME} and LIST_{NAME}.
    Returns a dictionary:
    {
        "NAME": {
            "webhook": "url",
            "names": set(...)
        }
    }
    """
    clients = {}
    
    # Get the directory where the script is located to handle relative paths correctly
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Iterate through all environment variables
    for key, value in os.environ.items():
        if key.startswith("WEBHOOK_"):
            # key is e.g. "WEBHOOK_NIKIFAR" -> client_name is "NIKIFAR"
            client_name = key[8:] 
            list_env_key = f"LIST_{client_name}"
            list_path_raw = os.getenv(list_env_key)
            
            if not list_path_raw:
                logger.warning(f"Found {key} but no corresponding {list_env_key} in .env")
                continue
                
            # Handle relative paths: if path is not absolute, join with base_dir
            if not os.path.isabs(list_path_raw):
                list_path = os.path.join(base_dir, list_path_raw)
            else:
                list_path = list_path_raw
                
            names = load_names(list_path)
            # Split webhooks by comma and strip whitespace
            webhooks = [url.strip() for url in value.split(',') if url.strip()]
            
            clients[client_name] = {
                "webhook": webhooks,
                "names": names
            }
            
    return clients


def main():
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    source_channel_id_str = os.getenv("SOURCE_CHANNEL_ID", "").strip()
    
    if not token or not source_channel_id_str:
        raise RuntimeError("DISCORD_BOT_TOKEN and SOURCE_CHANNEL_ID must be set as environment variables")
    
    source_channel_id = int(source_channel_id_str)
    
    # Load clients dynamically
    clients_config = load_clients_from_env()
    
    if not clients_config:
        logger.warning("No clients configured! Check .env for WEBHOOK_* and LIST_* pairs.")
    
    client = Forwarder(
        source_channel_id=source_channel_id,
        clients_config=clients_config
    )
    client.run(token)


if __name__ == "__main__":
    main()
