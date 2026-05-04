# Trading Bot Risk-Protected Version

## Setup
1. Fill `.env` with your credentials.
2. Install dependencies:
   `pip install -r requirements.txt`

## Run
- Bot: `python run_bot.py`
- Dashboard: `python run_dashboard.py`

## Notes
- Start the bot first, then the dashboard.
- The bot reads `data/bot_control.json` for the Start/Stop switch.
- Market hours and trading window are defined in `app/config.py`.
