import csv
from kiteconnect import KiteConnect

# Initialize Kite Connect
kite = KiteConnect(api_key="hd8nfvmrjsb9r8qb")
data = kite.generate_session("FKFSHHCVo4BZNTWNd5Tfb9wEGcZF035G", api_secret="j7mth5xk84xqdz9zwdfpmqtetl65gq3e")
kite.set_access_token(data["access_token"])

print("Access Token:", data["access_token"])

# First 60 days
data1 = kite.historical_data(
    instrument_token=260105,   # Bank Nifty token
    from_date="2026-02-15",
    to_date="2026-04-15",
    interval="minute"
)

# Next 30 days
data2 = kite.historical_data(
    instrument_token=260105,
    from_date="2026-04-16",
    to_date="2026-05-15",
    interval="minute"
)

# Merge both
data = data1 + data2

# Save to CSV
with open("banknifty_1min.csv", "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
    writer.writeheader()
    writer.writerows(data)

print("Saved 90 days of Bank Nifty 1-minute data to banknifty_1min.csv")