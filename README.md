# BaseFinder

**BaseFinder** is a Discord bot built with [nextcord](https://github.com/nextcord/nextcord) that helps Clash of Clans players find base layout links from YouTube videos — including War, CWL, and Legend bases for Town Hall levels 15–17.

## 🛠 Features

- 🎯 Slash command: `/find_bases` to search by Town Hall and base type
- 🔍 Filters recent uploads from hand-picked YouTube channels
- 🔗 Extracts official Clash of Clans layout links from video descriptions
- 🧠 Smart caching and API key rotation to avoid YouTube quota limits
- 📩 Sends results privately via DM to reduce chat spam

## 🚀 Getting Started

### 1. Clone and install dependencies

```bash
git clone https://github.com/leaskeg/BaseFinder.git
cd BaseFinder
pip install -r requirements.txt
````

### 2. Create a `.env` file

```dotenv
DISCORD_TOKEN=your_discord_bot_token
API_KEYS=your_youtube_api_key1,your_youtube_api_key2
```

> You can get YouTube Data API v3 keys from the [Google Cloud Console](https://console.cloud.google.com/).

### 3. Add channel IDs

Edit or create a `channels.txt` file, with one YouTube channel ID per line:

```
UCk7iPlcw-X7_3IMLqN2Vj2w
UCa9iS8-4Yxay_Nd0gBz8i9A
...
```

### 4. Run the bot

```bash
python basefinder.py
```

## ✅ Slash Command Usage

```plaintext
/find_bases base_level: TH16 base_type: War
```

* Returns up to 5 layout links from recent videos matching your criteria.
* Sends them to your DMs to keep the chat clean.

## 📦 Requirements

* Python 3.8+
* nextcord
* python-dotenv
* google-api-python-client

Install everything via:

```bash
pip install -r requirements.txt
```

## 🤝 Contributing

Got a new feature idea or found a bug? Feel free to fork the repo and open a PR!

## 📜 License

MIT License

Made with ❤️ for the Clash of Clans community by [leaskeg](https://github.com/leaskeg)
