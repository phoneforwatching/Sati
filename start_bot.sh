#!/bin/bash
# Startup script for Sati bot using uv
# This script unsets any conflicting system environment variables 
# and uses the values from .env file

echo "🧘‍♂️ Starting Sati Bot..."

# Unset any conflicting environment variables
unset BOT_TOKEN
unset GEMINI_API_KEY

# Run the bot with uv
cd "$(dirname "$0")"
uv run python sati_bot.py