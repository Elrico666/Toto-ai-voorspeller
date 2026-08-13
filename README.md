# TOTO AI Voorspeller v3

Deze versie voegt toe:
- TOP PICKS vanaf instelbare kans (standaard 75%)
- modelkans versus TOTO-marktkans
- VALUE = modelkans - marktkans
- opslag van voorspellingen in SQLite
- historische hit-rate zodra resultaten als WON/LOST worden verwerkt
- optionele football-data.org verrijking met recente vorm en doelpunten

## Online
Upload `app.py` en `requirements.txt` naar GitHub en deploy `app.py` via Streamlit Community Cloud.

Optioneel: voeg `FOOTBALL_DATA_API_KEY` toe als secret.

## Let op
De app beweert niet dat 75% werkelijk 75% winstkans is. Het is een modelscore. Voor een echte backtest moet een betrouwbare historische odds- en uitslagenbron worden gekoppeld.
