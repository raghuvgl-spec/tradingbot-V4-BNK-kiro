# Live Prep Changes

This package was prepared for stable paper-to-live rollout.

## Main upgrades
- Startup history prewarm from API when local candle history is insufficient.
- ISO-safe restore of `last_update` from bot state.
- ATR / trailing / early-exit settings moved to environment-backed config.
- Pending-entry trail noise suppressed when entry trail step is zero.
- SmartAPI order tags added for better order tracing.
- Runtime state now explicitly includes ATR, pending trade, reentry fields, and live tick timestamps.

## Recommended test settings for tomorrow
- FAST_EMA_PERIOD=9
- SLOW_EMA_PERIOD=21
- ATR_SL_MULTIPLIER=1.5
- ATR_TARGET_MULTIPLIER=2.0
- USE_ATR_TRAILING=True
- ATR_TRAIL_STEP_MULTIPLIER=0.30
- ATR_TRAIL_SL_MULTIPLIER=0.20
- ENABLE_EARLY_EMA_PROTECTION=False
- STARTUP_PREWARM_ENABLED=True
- STARTUP_PREWARM_CANDLES=60

## Rollout path
1. Full-day paper test
2. Tune on Monday
3. Shadow paper-vs-real debug
4. Go live with small size
