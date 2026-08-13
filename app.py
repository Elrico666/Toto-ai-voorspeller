import os, re, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

TOTO_URL = "https://sport.toto.nl/wedden/11/voetbal/wedstrijden"
API_URL = "https://api.football-data.org/v4"
TZ = ZoneInfo("Europe/Amsterdam")
DB = "predictions.db"

st.set_page_config(page_title="TOTO AI Voorspeller v5", page_icon="⚽", layout="wide")


# ---------------- API ----------------
def get_api_key():
    try:
        key = st.secrets.get("FOOTBALL_DATA_API_KEY", "")
    except Exception:
        key = ""
    return str(key).strip() or os.getenv("FOOTBALL_DATA_API_KEY", "").strip()


API_KEY = get_api_key()


@st.cache_data(ttl=300)
def api_status(key):
    """Distinguish: key missing, key invalid, API connected but no free matches today."""
    if not key:
        return {"status": "missing", "message": "Geen API-key gevonden."}

    try:
        r = requests.get(
            f"{API_URL}/competitions",
            headers={"X-Auth-Token": key},
            timeout=20,
        )
        if r.status_code == 200:
            return {"status": "connected", "message": "API-key geldig en API bereikbaar."}
        if r.status_code in (401, 403):
            return {"status": "invalid", "message": f"API-key geweigerd ({r.status_code})."}
        return {"status": "error", "message": f"API antwoordde met HTTP {r.status_code}."}
    except Exception as e:
        return {"status": "error", "message": f"API niet bereikbaar: {e}"}


@st.cache_data(ttl=300)
def api_matches(key, day):
    if not key:
        return None, "Geen API-key."

    try:
        r = requests.get(
            f"{API_URL}/matches",
            params={"dateFrom": day.isoformat(), "dateTo": day.isoformat()},
            headers={"X-Auth-Token": key},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("matches", []), ""
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=900)
def team_history(key, team_id, day):
    try:
        r = requests.get(
            f"{API_URL}/teams/{team_id}/matches",
            params={
                "dateFrom": (day - timedelta(days=180)).isoformat(),
                "dateTo": (day - timedelta(days=1)).isoformat(),
                "status": "FINISHED",
                "limit": 20,
            },
            headers={"X-Auth-Token": key},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("matches", [])
    except Exception:
        pass
    return []


# ---------------- TOTO ----------------
def num(x):
    return float(str(x).replace(",", "."))


def norm(x):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", str(x).lower())).strip()


@st.cache_data(ttl=180)
def get_toto_html():
    r = requests.get(
        TOTO_URL,
        headers={"User-Agent": "Mozilla/5.0 Chrome/151 Safari/537.36"},
        timeout=25,
    )
    r.raise_for_status()
    return r.text


def parse_toto(html):
    lines = [
        re.sub(r"\s+", " ", x).strip()
        for x in BeautifulSoup(html, "html.parser").get_text("\n").splitlines()
    ]
    lines = [x for x in lines if x]
    out = []

    for i in range(len(lines) - 7):
        try:
            if lines[i] != "1" or lines[i + 2] != "X" or lines[i + 4] != "2":
                continue

            o1, ox, o2 = num(lines[i + 1]), num(lines[i + 3]), num(lines[i + 5])
            nearby = lines[i + 6:i + 12]
            today = next((x for x in nearby if "Vandaag" in x), None)
            if not today:
                continue

            before = lines[max(0, i - 8):i]
            candidates = [x for x in before if x.lower() not in {"resultaat", "wedoptie"}]
            if len(candidates) < 2:
                continue

            home, away = candidates[-2], candidates[-1]
            raw = [1 / o1, 1 / ox, 1 / o2]
            s = sum(raw)

            out.append({
                "home": home,
                "away": away,
                "time": today.replace("Vandaag", "").strip(),
                "o1": o1,
                "ox": ox,
                "o2": o2,
                "p1": raw[0] / s,
                "px": raw[1] / s,
                "p2": raw[2] / s,
            })
        except Exception:
            continue

    unique = {}
    for x in out:
        unique[(x["home"], x["away"], x["time"])] = x
    return list(unique.values())


# ---------------- MODEL ----------------
def find_api_match(toto_match, api_matches):
    if not api_matches:
        return None

    a = norm(toto_match["home"])
    b = norm(toto_match["away"])
    best, score = None, 0

    for m in api_matches:
        h = norm(m.get("homeTeam", {}).get("name", ""))
        aw = norm(m.get("awayTeam", {}).get("name", ""))
        s = (80 if a in h or h in a else 0) + (80 if b in aw or aw in b else 0)
        if s > score:
            best, score = m, s

    return best if score >= 100 else None


def form_stats(matches, team_id):
    last = matches[-10:]
    if not last:
        return None

    points = gf = ga = games = 0

    for m in last:
        hg = m.get("score", {}).get("fullTime", {}).get("home")
        ag = m.get("score", {}).get("fullTime", {}).get("away")

        if hg is None or ag is None:
            continue

        if team_id == m.get("homeTeam", {}).get("id"):
            gf += hg
            ga += ag
            points += 3 if hg > ag else 1 if hg == ag else 0
            games += 1

        elif team_id == m.get("awayTeam", {}).get("id"):
            gf += ag
            ga += hg
            points += 3 if ag > hg else 1 if ag == hg else 0
            games += 1

    if not games:
        return None

    return {
        "ppg": points / games,
        "gf": gf / games,
        "ga": ga / games,
        "games": games,
    }


def predict(toto_match, api_match, key, day):
    market = [toto_match["p1"], toto_match["px"], toto_match["p2"]]

    if not api_match or not key:
        return market, False, {}

    hf = form_stats(
        team_history(key, api_match["homeTeam"]["id"], day),
        api_match["homeTeam"]["id"],
    )
    af = form_stats(
        team_history(key, api_match["awayTeam"]["id"], day),
        api_match["awayTeam"]["id"],
    )

    if not hf or not af:
        return market, False, {}

    form_edge = (hf["ppg"] - af["ppg"]) / 3
    goal_edge = (
        hf["gf"] - hf["ga"] - af["gf"] + af["ga"]
    ) / 4

    edge = max(-0.18, min(0.18, 0.10 * form_edge + 0.05 * goal_edge))

    adjusted = [
        market[0] * (1 + edge),
        market[1] * (1 - abs(edge) * 0.35),
        market[2] * (1 - edge),
    ]
    s = sum(adjusted)
    adjusted = [x / s for x in adjusted]

    # Market remains the dominant prior; stats make a conservative correction.
    final = [0.75 * market[i] + 0.25 * adjusted[i] for i in range(3)]

    details = {
        "home_ppg": hf["ppg"],
        "away_ppg": af["ppg"],
        "home_gf": hf["gf"],
        "away_gf": af["gf"],
        "home_ga": hf["ga"],
        "away_ga": af["ga"],
        "home_games": hf["games"],
        "away_games": af["games"],
    }

    return final, True, details


# ---------------- HISTORY ----------------
def init_db():
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS predictions(
            id INTEGER PRIMARY KEY,
            created_at TEXT,
            match_date TEXT,
            match_name TEXT,
            home TEXT,
            away TEXT,
            prediction TEXT,
            probability REAL,
            market_probability REAL,
            value REAL,
            odds REAL,
            result TEXT,
            source TEXT
        )"""
    )
    con.commit()
    con.close()


def save_prediction(row):
    con = sqlite3.connect(DB)
    con.execute(
        """INSERT INTO predictions
        (created_at,match_date,match_name,home,away,prediction,probability,
         market_probability,value,odds,result,source)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        row,
    )
    con.commit()
    con.close()


def load_history():
    con = sqlite3.connect(DB)
    df = pd.read_sql_query(
        "SELECT * FROM predictions ORDER BY id DESC", con
    )
    con.close()
    return df


init_db()


# ---------------- UI ----------------
today = datetime.now(TZ).date()

with st.sidebar:
    st.header("Instellingen")
    threshold = st.slider("TOP PICK vanaf", 50, 95, 75)
    st.caption("De API-key wordt automatisch uit Streamlit Secrets gelezen.")

    if st.button("🔄 Ververs data"):
        st.cache_data.clear()
        st.rerun()

st.title("⚽ TOTO AI Voorspeller v5")
st.caption("TOTO-markt + football-data.org + vorm + doelpunten + value")

status = api_status(API_KEY)
api_today, api_error = api_matches(API_KEY, today)

try:
    toto = parse_toto(get_toto_html())
except Exception as e:
    st.error("TOTO-data kon niet worden opgehaald.")
    st.caption(str(e))
    st.stop()

rows = []

for t in toto:
    am = find_api_match(t, api_today or [])
    p, enriched, details = predict(t, am, API_KEY, today)

    k = max(range(3), key=lambda i: p[i])
    labels = ["1", "X", "2"]
    choices = [t["home"], "Gelijkspel", t["away"]]
    odds = [t["o1"], t["ox"], t["o2"]][k]

    model_prob = p[k] * 100
    market_prob = max(t["p1"], t["px"], t["p2"]) * 100
    value = model_prob - market_prob

    rows.append({
        "home": t["home"],
        "away": t["away"],
        "time": t["time"],
        "prediction": labels[k],
        "choice": choices[k],
        "prob": model_prob,
        "market": market_prob,
        "value": value,
        "odds": odds,
        "data": enriched,
        "details": details,
    })

df = pd.DataFrame(rows)
top = (
    df[df["prob"] >= threshold]
    .sort_values(["prob", "value"], ascending=False)
    if not df.empty
    else df
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Wedstrijden vandaag", len(df))
c2.metric(f"TOP PICKS ≥ {threshold}%", len(top))
c3.metric(
    "Hoogste modelkans",
    f"{df.prob.max():.1f}%" if not df.empty else "—",
)

if status["status"] == "connected":
    c4.metric("API-status", "🟢 Verbonden")
elif status["status"] == "invalid":
    c4.metric("API-status", "🔴 Ongeldig")
elif status["status"] == "missing":
    c4.metric("API-status", "⚪ Geen key")
else:
    c4.metric("API-status", "🟠 Probleem")

# Important: no longer call a valid key "not connected" just because
# today's free competition list is empty.
if status["status"] == "connected":
    st.success("🟢 football-data.org is correct gekoppeld.")
    if not api_today:
        st.info(
            "De API is gekoppeld, maar football-data.org heeft vandaag geen "
            "wedstrijden teruggegeven binnen de competities/data die jouw gratis "
            "account mag gebruiken."
        )
elif status["status"] == "invalid":
    st.error("De API-key wordt door football-data.org geweigerd. Controleer Secrets.")
elif status["status"] == "missing":
    st.warning("Geen FOOTBALL_DATA_API_KEY gevonden in Streamlit Secrets.")
else:
    st.warning(status["message"])

st.subheader("⭐ TOP PICKS")

if top.empty:
    st.info(f"Geen voorspelling haalt momenteel {threshold}%.")
else:
    for _, r in top.iterrows():
        with st.container(border=True):
            source = "📊 Markt + data-analyse" if r.data else "📈 Alleen TOTO-markt"
            st.caption(f"{r.time} · {source}")
            st.markdown(f"### {r.home} – {r.away}")
            st.markdown(f"**{r.choice} · {r.prob:.1f}%**")

            value_text = f"{r.value:+.1f}%"
            st.write(
                f"Marktkans: **{r.market:.1f}%** · "
                f"Modelkans: **{r.prob:.1f}%** · "
                f"Value: **{value_text}** · "
                f"TOTO odd: **{r.odds:.2f}**"
            )

            if r.data:
                d = r.details
                st.caption(
                    f"Laatste vorm — PPG: {r.home} {d['home_ppg']:.2f} "
                    f"vs {r.away} {d['away_ppg']:.2f} · "
                    f"Doelpunten: {r.home} {d['home_gf']:.2f} voor / "
                    f"{d['home_ga']:.2f} tegen · "
                    f"{r.away} {d['away_gf']:.2f} voor / "
                    f"{d['away_ga']:.2f} tegen"
                )

st.divider()
st.subheader("📈 Historische prestaties")

h = load_history()

if h.empty:
    st.info("Nog geen voorspellingen opgeslagen.")
else:
    settled = h[h.result.isin(["WON", "LOST"])]
    total = len(settled)
    wins = int((settled.result == "WON").sum())

    x, y, z = st.columns(3)
    x.metric("Beoordeelde picks", total)
    y.metric("Goed", wins)
    z.metric(
        "Hit-rate",
        f"{wins / total * 100:.1f}%" if total else "—",
    )

    st.dataframe(
        h[
            [
                "match_date",
                "match_name",
                "prediction",
                "probability",
                "market_probability",
                "value",
                "odds",
                "result",
            ]
        ].head(100),
        use_container_width=True,
        hide_index=True,
    )

with st.expander("ℹ️ Hoe werkt het model?"):
    st.write(
        """
        • De TOTO-marktkans is de basis van de voorspelling.
        • Als de wedstrijd ook in football-data.org beschikbaar is, gebruikt
          het model recente resultaten en doelpunten voor/tegen.
        • De markt weegt 75%; de statistische correctie 25%.
        • Value = modelkans minus marktkans.
        • 75% is een modelmatige kansinschatting en geen bewezen winrate of garantie.
        """
    )

st.caption(
    f"Laatste update: {datetime.now(TZ).strftime('%d-%m-%Y %H:%M:%S')}"
)
st.link_button("Open TOTO voetbal", TOTO_URL)
