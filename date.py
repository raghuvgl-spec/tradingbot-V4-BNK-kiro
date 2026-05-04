from datetime import date, datetime 

today = date.today()
week_day = today.weekday()
day_name = today.strftime("%A")
now = datetime.now().time()
print ("Today is:",now)
print ("Today is:",day_name)
print ("Today's date:",today)
print ("Weekday index (0=Mon,6=Sun):",week_day)