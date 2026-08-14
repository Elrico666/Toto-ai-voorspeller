import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

API_URL = "https://api.football-data.org/v4"
TZ = ZoneInfo("Europe/Amsterdam")
st.set_page_config(page_title="TOTO AI Voorspeller v6", page_icon="⚽", layout="wide")


def get_key():
    try:
        return str(st.secrets.get("FOOTBALL_DATA_API_KEY", "")).strip()
    except Exception:
        return os.getenv("FOOTBALL_DATA_API_KEY", "").strip()

KEY = get_key()

@st.cache_data(ttl=300)
def get_matches(key, date_from, date_to):
    if not key:
        return [], "Geen API-key."
    try:
        r = requests.get(f"{API_URL}/matches", params={"dateFrom": date_from, "dateTo": date_to}, headers={"X-Auth-Token": key}, timeout=25)
        if r.status_code == 200:
            return r.json().get("matches", []), ""
        return [], f"HTTP {r.status_code}"
    except Exception as e:
        return [], str(e)

@st.cache_data(ttl=300)
def check_key(key):
    if not key:
        return False
    try:
        r = requests.get(f"{API_URL}/competitions", headers={"X-Auth-Token": key}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def recent_form(matches, team_id, today, n):
    games = []
    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        try:
            d = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).date()
        except Exception:
            continue
        if d >= today or team_id not in (m.get("homeTeam", {}).get("id"), m.get("awayTeam", {}).get("id")):
            continue
        games.append(m)
    games.sort(key=lambda x: x.get("utcDate", ""))
    games = games[-n:]
    if len(games) < 5:
        return None
    pts = gf = ga = valid = 0
    for m in games:
        h = m.get("score", {}).get("fullTime", {}).get("home")
        a = m.get("score", {}).get("fullTime", {}).get("away")
        if h is None or a is None:
            continue
        if team_id == m.get("homeTeam", {}).get("id"):
            scored, conceded = h, a
        else:
            scored, conceded = a, h
        gf += scored; ga += conceded
        pts += 3 if scored > conceded else 1 if scored == conceded else 0
        valid += 1
    if valid < 5:
        return None
    return {"ppg": pts / valid, "gf": gf / valid, "ga": ga / valid, "games": valid}


def model(home, away):
    strength = 0.55 * (home["ppg"] - away["ppg"]) / 3 + 0.25 * (home["gf"] - away["gf"]) / 3 + 0.20 * (away["ga"] - home["ga"]) / 3
    strength = max(-0.20, min(0.20, strength))
    p_home = 0.50 + strength
    p_draw = 0.25 - abs(strength) * 0.15
    p_away = 1 - p_home - p_draw
    p = [max(0.02, p_home), max(0.02, p_draw), max(0.02, p_away)]
    s = sum(p)
    return [x / s for x in p]

with st.sidebar:
    st.header("⚙️ Instellingen")
    threshold = st.slider("TOP PICK vanaf", 50, 95, 75)
    lookback = st.slider("Vormperiode", 5, 10, 10)
    if st.button("🔄 Ververs data"):
        st.cache_data.clear(); st.rerun()

today = datetime.now(TZ).date()
connected = check_key(KEY)
matches, error = get_matches(KEY, (today - timedelta(days=120)).isoformat(), today.isoformat())

today_matches = []
for m in matches:
    try:
        dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(TZ)
        if dt.date() == today:
            today_matches.append(m)
    except Exception:
        pass

rows = []
for m in today_matches:
    home_name = m.get("homeTeam", {}).get("name", "Onbekend")
    away_name = m.get("awayTeam", {}).get("name", "Onbekend")
    hf = recent_form(matches, m.get("homeTeam", {}).get("id"), today, lookback)
    af = recent_form(matches, m.get("awayTeam", {}).get("id"), today, lookback)
    if not hf or not af:
        continue
    probs = model(hf, af)
    idx = max(range(3), key=lambda i: probs[i])
    rows.append({
        "Tijd": datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(TZ).strftime("%H:%M"),
        "Wedstrijd": f"{home_name} – {away_name}",
        "Voorspelling": [home_name, "Gelijkspel", away_name][idx],
        "Kans": probs[idx] * 100,
        "PPG thuis": hf["ppg"], "PPG uit": af["ppg"],
        "Goals thuis": hf["gf"], "Goals uit": af["gf"],
        "Tegen thuis": hf["ga"], "Tegen uit": af["ga"],
    })

df = pd.DataFrame(rows)
top = df[df["Kans"] >= threshold].sort_values("Kans", ascending=False) if not df.empty else df

st.title("⚽ TOTO AI Voorspeller v6")
st.caption("Vandaag • vorm • doelpunten • modelkans ≥ ingestelde drempel")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Wedstrijden vandaag", len(today_matches))
c2.metric(f"TOP PICKS ≥ {threshold}%", len(top))
c3.metric("Hoogste modelkans", f"{top['Kans'].max():.1f}%" if not top.empty else "—")
c4.metric("API-status", "🟢 Verbonden" if connected else "🔴 Niet verbonden")

if connected:
    st.success("football-data.org is correct gekoppeld.")
else:
    st.warning("football-data.org is niet gekoppeld. Controleer FOOTBALL_DATA_API_KEY in Secrets.")

st.subheader("⭐ TOP PICKS")
if top.empty:
    st.info(f"Geen wedstrijden halen {threshold}% met voldoende historische data. De app toont geen kunstmatig hoge kansen.")
else:
    for _, r in top.iterrows():
        with st.container(border=True):
            st.caption(f"{r['Tijd']} · historische data beschikbaar")
            st.markdown(f"### {r['Wedstrijd']}")
            st.markdown(f"**Voorspelling: {r['Voorspelling']} — {r['Kans']:.1f}%**")
            st.progress(min(1.0, r["Kans"] / 100), text=f"Modelkans {r['Kans']:.1f}%")
            st.write(f"PPG: {r['PPG thuis']:.2f} vs {r['PPG uit']:.2f} · Goals: {r['Goals thuis']:.2f} vs {r['Goals uit']:.2f} · Tegen: {r['Tegen thuis']:.2f} vs {r['Tegen uit']:.2f}")

with st.expander("📊 Alle wedstrijden met voldoende historische data"):
    if df.empty:
        st.info("Geen wedstrijden met voldoende historische data.")
    else:
        st.dataframe(df.sort_values("Kans", ascending=False), use_container_width=True, hide_index=True)

with st.expander("ℹ️ Over v6"):
    st.write("v6 gebruikt recente resultaten, punten per wedstrijd en doelpunten voor/tegen. Alleen modelkansen boven de ingestelde drempel worden als TOP PICK getoond. Een modelkans is een statistische inschatting en geen garantie op winst.")

st.caption(f"Laatst bijgewerkt: {datetime.now(TZ).strftime('%d-%m-%Y %H:%M:%S')}")
