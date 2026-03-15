"""
main.py — starts both the trading bot and the web dashboard simultaneously.
The bot runs in a background thread; Flask serves the dashboard.
"""
import threading
import os
from web import app
from bot import run_bot

def start_bot():
    run_bot()

if __name__ == "__main__":
    # Start trading bot in background thread
    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()

    # Start Flask web dashboard
    port = int(os.environ.get("PORT", 5000))
    print(f"🌐 Dashboard running at http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
