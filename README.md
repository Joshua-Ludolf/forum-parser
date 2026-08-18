# Discord Inquiry Bot (Google Sheets + Ollama)

This bot reads user records from Google Sheets when a user sends an inquiry in Discord, drafts a reply with Ollama, and routes the draft to moderators for review.

Moderators can:
- approve and send the draft as-is
- edit the draft in a modal and then send
- reject the draft

## Requirements

- Python 3.12+
- A Discord bot token
- A Google service account JSON credential file
- A Google Sheet with these headers in row 1:
	- `discord_id`
	- `user_name`
	- `order_status`
	- `notes`
- Ollama running locally (or reachable URL) with model `gemma4:12b-it-qat`

## Install

```bash
pip install -e .
```

## Environment

Create `.env` from `.env.example` and fill values.

## Google Sheet Setup

1. Edit google sheet ids based on yours.

## Run

```bash
python main.py
```

## Usage

In Discord, a user sends:

```text
!inquiry Where is my order?
```

Flow:
1. Bot finds the row where `discord_id == author.id`.
2. Bot generates a draft reply with Ollama.
3. Bot posts draft to `MODERATOR_CHANNEL_ID` with buttons.
4. Moderator approves, edits then sends, or rejects.
