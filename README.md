# Discord Inquiry Bot (Google Sheets + OpenRouter)

This bot reads user records from Google Sheets when a user sends an inquiry in Discord, drafts a reply with OpenRouter (LLM API), and routes the draft to moderators for review.

Moderators can:
- approve and send the draft as-is
- edit the draft in a modal and then send
- reject the draft

## Requirements

- Python 3.12+
- A Discord bot token
- An OpenRouter API key
- A Google Sheet with these headers in row 1:
	- `discord_id`
	- `user_name`
	- `order_status`
	- `notes`

## Install

```bash
pip install -e .
```

## Environment

Create `.env` from `.env.example` and fill values.

## Google Sheet Setup

1. Create two Google Sheets:
   - **Voice Actor Applications** - with columns for Discord username, display name, age, sample link, vocal presentations, vocal range, character types, roles, accents
   - **VA Requests** - with columns for creator name, channel, description, kind of voice, paid, requesting specific VA, specific VA name
2. Share both sheets as "Anyone with the link can view"
3. Copy the Sheet IDs from the URLs and add to `.env`

## Run Locally

```bash
# Run the bot (requires OPENROUTER_API_KEY in .env)
python main.py
```

## Deploy to Railway

This project includes a `railway.json` for easy deployment to Railway with a single service:
- **discord-bot** - The Discord bot (uses OpenRouter API, no local LLM needed)

### Steps:

1. Push this repo to GitHub
2. Create a new Railway project from the GitHub repo
3. Railway will automatically detect `railway.json` and create the service
4. Add environment variables in Railway dashboard:
   - `DISCORD_TOKEN` - Your Discord bot token
   - `MODERATOR_CHANNEL_ID` - Discord channel ID for moderator reviews
   - `GOOGLE_SHEET_ID_VOICE_ACTOR` - Google Sheet ID for VA applications
   - `GOOGLE_SHEET_ID_DUB_REQUEST` - Google Sheet ID for VA requests
   - `GOOGLE_WORKSHEET_NAME` - Worksheet name (default: "Form Responses 1")
   - `OPENROUTER_API_KEY` - Your OpenRouter API key
   - `OPENROUTER_MODEL` - Model to use (default: "google/gemma-3-12b-it:free")
5. The bot will automatically connect to OpenRouter at `https://openrouter.ai/api/v1`

## Usage

In Discord, use slash commands:
- `/auto_msg_va username` - Look up a Voice Actor application and draft a reply
- `/auto_msg_request username` - Look up a VA request and draft a reply

Flow:
1. Bot finds the row matching the username in Google Sheets
2. Bot generates a draft reply with OpenRouter
3. Bot posts draft to `MODERATOR_CHANNEL_ID` with buttons
4. Moderator approves, edits then sends, or rejects the draft
