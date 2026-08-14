import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

TOTO_URL = "https://sport.toto.nl/wedden/11/voetbal/wedstrijden"
FD_URL = "https://api.football-data.org/v4"
TZ = ZoneInfo("Europe/Amsterdam")

st.set_page_config(page_title="TOTO AI Voorspeller v7", page_icon="⚽", layout="wide")


# ============================================================
# CONFIG / API
# ============================================================
def get_key():
    try:
        return str(st.secrets.get("FOOTBALL_DATA_API_KEY", "")).strip()
    except Exception:
        return os.getenv("FOOTBALL_DATA_API_KEY", "").strip()


KEY = get_key()


@st.cache_data(ttl=300)
def fd_get(path, params=None):
    if not KEY:
        return None, "Geen football-data.org API-key."
    try:
        r = requests.get(
            FD_URL + path,
            params=params or {},
            headers={"X-Auth-Token": KEY},
            timeout=25,
        )
        if r.status_code == 200:
            return r.json(), ""
        return None, f"football-data.org HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return None, str(e)


@st.cache_data(ttl=300)
def test_fd():
    data, err = fd_get("/competitions")
    return data is not None, err


@st.cache_data(ttl=300)
def get_fd_matches(date_from, date_to):
    data, err = fd_get(
        "/matches",
        {"dateFrom": date_from, "dateTo": date_to},
    )
    return (data or {}).get("matches", []), err


# ============================================================
# TOTO DATA
# v7 haalt de wedstrijden van vandaag primair uit TOTO.
# football-data.org wordt alleen gebruikt voor historische vorm.
# ============================================================
@st.cache_data(ttl=180)
def get_toto_text():
    r = requests.get(
        TOTO_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/151 Safari/537.36"
            ),
            "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
        },
        timeout=30,
    )
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser").get_text("\n")


def clean_lines(text):
    return [re.sub(r"\s+", " ", x).strip() for x in text.splitlines() if x.strip()]


def parse_decimal(s):
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return None


def is_odd(s):
    x = parse_decimal(s)
    return x is not None and 1.01 <= x <= 100


def parse_toto_today(text):
    """
    Parseert de 1/X/2-markt uit de server-rendered tekst van TOTO.
    v7 kijkt specifiek naar 'Vandaag' en 1/X/2 + odds.
    """
    lines = clean_lines(text)
    rows = []

    # Patroon in TOTO-pagina's:
    # Team A / Team B / Resultaat / 1 / odd / X / odd / 2 / odd / Vandaag HH:MM
    for i in range(len(lines) - 12):
        window = lines[i:i + 22]
        joined = " | ".join(window)

        if "Vandaag" not in joined:
            continue

        # Zoek een 1-X-2 blok.
        for j in range(i, min(i + 12, len(lines) - 6)):
            if lines[j] != "1":
                continue
            if lines[j + 2] != "X" or lines[j + 4] != "2":
                continue

            o1 = parse_decimal(lines[j + 1])
            ox = parse_decimal(lines[j + 3])
            o2 = parse_decimal(lines[j + 5])
            if not all(is_odd(x) for x in [o1, ox, o2]):
                continue

            after = lines[j + 6:j + 15]
            time_match = next(
                (
                    re.search(r"Vandaag\s+(\d{1,2}:\d{2})", x, re.I)
                    for x in after
                    if re.search(r"Vandaag\s+\d{1,2}:\d{2}", x, re.I)
                ),
                None,
            )
            if not time_match:
                continue

            # Zoek de twee teamnamen kort vóór het 1/X/2 blok.
            candidates = []
            for k in range(max(0, j - 10), j):
                x = lines[k]
                if (
                    x not in {"Wedoptie", "Resultaat", "Resultaat - Vroege uitbetaling"}
                    and "Vandaag" not in x
                    and not is_odd(x)
                    and x not in {"1", "X", "2"}
                ):
                    candidates.append(x)

            if len(candidates) < 2:
                continue

            home, away = candidates[-2], candidates[-1]

            # Vermijd specials zoals "Bologna / No Score".
            if len(home) < 2 or len(away) < 2:
                continue

            raw = [1 / o1, 1 / ox, 1 / o2]
            total = sum(raw)

            rows.append(
                {
                    "home": home,
                    "away": away,
                    "time": time_match.group(1),
                    "o1": o1,
                    "ox": ox,
                    "o2": o2,
                    "market_1": raw[0] / total * 100,
                    "market_x": raw[1] / total * 100,
                    "market_2": raw[2] / total * 100,
                }
            )
            break

    # Dubbelen verwijderen.
    unique = {}
    for r in rows:
        unique[(r["home"], r["away"], r["time"])] = r

    return list(unique.values())


# ============================================================
# TEAM MATCHING / HISTORISCHE VORM
# ============================================================
ALIASES = {
    "fc nordsjaelland": "nordsjaelland",
    "fc nordjsaelland": "nordsjaelland",
    "psv eindhoven": "psv",
    "afc ajax": "ajax",
    "ajax amsterdam": "ajax",
    "feyenoord rotterdam": "feyenoord",
    "fc twente": "twente",
    "fc utrecht": "utrecht",
    "fc groningen": "groningen",
    "n e c nijmegen": "nec nijmegen",
    "n.e.c. nijmegen": "nec nijmegen",
}


def norm(name):
    s = str(name).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(fc|cf|sc|afc)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 88
    sa, sb = set(a.split()), set(b.split())
    overlap = len(sa & sb)
    if overlap >= 2:
        return 72
    if overlap == 1 and (len(sa) == 1 or len(sb) == 1):
        return 65
    return 0


def find_fd_match(toto_row, matches):
    best = None
    best_score = 0

    for m in matches:
        h = m.get("homeTeam", {}).get("name", "")
        a = m.get("awayTeam", {}).get("name", "")
        score = sim(toto_row["home"], h) + sim(toto_row["away"], a)

        if score > best_score:
            best_score = score
            best = m

    return best if best_score >= 125 else None


def form(matches, team_id, today, n):
    games = []

    for m in matches:
        if m.get("status") != "FINISHED":
            continue

        try:
            d = datetime.fromisoformat(
                m["utcDate"].replace("Z", "+00:00")
            ).date()
        except Exception:
            continue

        if d >= today:
            continue

        if team_id not in (
            m.get("homeTeam", {}).get("id"),
            m.get("awayTeam", {}).get("id"),
        ):
            continue

        games.append(m)

    games.sort(key=lambda x: x.get("utcDate", ""))
    games = games[-n:]

    if len(games) < 5:
        return None

    points = gf = ga = 0
    valid = 0

    for m in games:
        hs = m.get("score", {}).get("fullTime", {}).get("home")
        aws = m.get("score", {}).get("fullTime", {}).get("away")

        if hs is None or aws is None:
            continue

        if team_id == m.get("homeTeam", {}).get("id"):
            scored, conceded = hs, aws
        else:
            scored, conceded = aws, hs

        gf += scored
        ga += conceded
        points += 3 if scored > conceded else 1 if scored == conceded else 0
        valid += 1

    if valid < 5:
        return None

    return {
        "ppg": points / valid,
        "gf": gf / valid,
        "ga": ga / valid,
        "games": valid,
    }


def model_probability(home, away, market_probs):
    """
    Market is used as a prior; historical form makes a limited correction.
    This prevents the model from claiming extreme certainty from tiny samples.
    """
    strength = (
        0.55 * (home["ppg"] - away["ppg"]) / 3
        + 0.25 * (home["gf"] - away["gf"]) / 3
        + 0.20 * (away["ga"] - home["ga"]) / 3
    )
    strength = max(-0.18, min(0.18, strength))

    form_raw = [
        0.50 + strength,
        0.25 - abs(strength) * 0.12,
        0.25 - strength,
    ]
    form_raw = [max(0.03, x) for x in form_raw]
    s = sum(form_raw)
    form_raw = [x / s for x in form_raw]

    # 65% TOTO market prior + 35% historical form.
    final = [
        0.65 * market_probs[0] + 0.35 * form_raw[0],
        0.65 * market_probs[1] + 0.35 * form_raw[1],
        0.65 * market_probs[2] + 0.35 * form_raw[2],
    ]
    s = sum(final)
    return [x / s for x in final]


# ============================================================
# APP
# ============================================================
with st.sidebar:
    st.header("⚙️ Instellingen")
    threshold = st.slider("TOP PICK vanaf (%)", 50, 95, 75)
    lookback = st.slider("Vormperiode", 5, 10, 10)

    if st.button("🔄 Ververs alles"):
        st.cache_data.clear()
        st.rerun()

today = datetime.now(TZ).date()

fd_ok, fd_error = test_fd()

# TOTO: bron voor wedstrijden van vandaag
toto_rows = []
toto_error = ""

try:
    toto_text = get_toto_text()
    toto_rows = parse_toto_today(toto_text)
except Exception as e:
    toto_error = str(e)

# football-data: alleen historie
fd_matches, fd_matches_error = get_fd_matches(
    (today - timedelta(days=180)).isoformat(),
    today.isoformat(),
)

results = []

for t in toto_rows:
    m = find_fd_match(t, fd_matches)

    if not m:
        results.append(
            {
                **t,
                "prediction": None,
                "model": None,
                "market": max(t["market_1"], t["market_x"], t["market_2"]),
                "value": None,
                "status": "Geen historische koppeling",
                "details": None,
            }
        )
        continue

    hf = form(
        fd_matches,
        m.get("homeTeam", {}).get("id"),
        today,
        lookback,
    )
    af = form(
        fd_matches,
        m.get("awayTeam", {}).get("id"),
        today,
        lookback,
    )

    if not hf or not af:
        results.append(
            {
                **t,
                "prediction": None,
                "model": None,
                "market": max(t["market_1"], t["market_x"], t["market_2"]),
                "value": None,
                "status": "Te weinig historische data",
                "details": None,
            }
        )
        continue

    market = [
        t["market_1"] / 100,
        t["market_x"] / 100,
        t["market_2"] / 100,
    ]

    probs = model_probability(hf, af, market)
    idx = max(range(3), key=lambda i: probs[i])

    choices = [t["home"], "Gelijkspel", t["away"]]
    model_pct = probs[idx] * 100
    market_pct = market[idx] * 100

    results.append(
        {
            **t,
            "prediction": choices[idx],
            "model": model_pct,
            "market": market_pct,
            "value": model_pct - market_pct,
            "status": "Historische data OK",
            "details": {
                "home_ppg": hf["ppg"],
                "away_ppg": af["ppg"],
                "home_gf": hf["gf"],
                "away_gf": af["gf"],
                "home_ga": hf["ga"],
                "away_ga": af["ga"],
                "home_games": hf["games"],
                "away_games": af["games"],
            },
        }
    )

df = pd.DataFrame(results)

if not df.empty:
    picks = df[
        (df["model"].notna())
        & (df["model"] >= threshold)
    ].sort_values(
        ["model", "value"],
        ascending=False,
    )
else:
    picks = df


# ============================================================
# DASHBOARD
# ============================================================
st.title("⚽ TOTO AI Voorspeller v7")
st.caption("TOTO vandaag → historische vorm → modelkans → alleen TOP PICKS ≥ drempel")

c1, c2, c3, c4 = st.columns(4)
c1.metric("TOTO-wedstrijden vandaag", len(toto_rows))
c2.metric(f"TOP PICKS ≥ {threshold}%", len(picks))
c3.metric(
    "Hoogste modelkans",
    f"{picks['model'].max():.1f}%" if not picks.empty else "—",
)
c4.metric(
    "football-data.org",
    "🟢 Verbonden" if fd_ok else "🔴 Niet verbonden",
)

if fd_ok:
    st.success(
        "🟢 football-data.org is gekoppeld. "
        "Deze API wordt in v7 alleen gebruikt voor historische vorm."
    )
else:
    st.error(
        "🔴 football-data.org is niet beschikbaar. "
        "Controleer je bestaande API-key in Streamlit Secrets."
    )

if toto_error:
    st.warning(
        "TOTO kon niet rechtstreeks worden uitgelezen: "
        + toto_error
    )
elif len(toto_rows) == 0:
    st.warning(
        "TOTO is bereikbaar, maar v7 kon de 1/X/2-wedstrijden van vandaag "
        "niet uit de pagina halen. Open 'Diagnose' onderaan voor details."
    )
else:
    st.info(
        f"🟢 TOTO levert {len(toto_rows)} voetbalwedstrijden voor vandaag. "
        "Deze wedstrijden zijn de basis van de voorspellingen."
    )

st.subheader("⭐ TOP PICKS")

if picks.empty:
    st.info(
        f"Geen TOTO-wedstrijd heeft een modelkans van minimaal {threshold}% "
        "én voldoende historische data. Dat is bewust: v7 verzint geen kansen."
    )
else:
    for _, r in picks.iterrows():
        with st.container(border=True):
            st.caption(f"Vandaag {r['time']} · {r['status']}")
            st.markdown(f"### {r['home']} – {r['away']}")
            st.markdown(
                f"**⭐ Voorspelling: {r['prediction']} · "
                f"modelkans {r['model']:.1f}%**"
            )
            st.progress(
                min(1.0, r["model"] / 100),
                text=f"Modelkans {r['model']:.1f}%",
            )
            st.write(
                f"TOTO marktkans: **{r['market']:.1f}%** · "
                f"Modelkans: **{r['model']:.1f}%** · "
                f"Modelverschil: **{r['value']:+.1f} procentpunt**"
            )

            d = r["details"]
            st.caption(
                f"Vorm PPG: {r['home']} {d['home_ppg']:.2f} "
                f"vs {r['away']} {d['away_ppg']:.2f} · "
                f"Goals voor: {d['home_gf']:.2f} vs {d['away_gf']:.2f} · "
                f"Goals tegen: {d['home_ga']:.2f} vs {d['away_ga']:.2f}"
            )

st.divider()

with st.expander("📋 Alle TOTO-wedstrijden van vandaag"):
    if df.empty:
        st.info("Geen TOTO-wedstrijden gevonden.")
    else:
        show = df[
            [
                "time",
                "home",
                "away",
                "prediction",
                "market",
                "model",
                "value",
                "status",
            ]
        ].copy()
        show.columns = [
            "Tijd",
            "Thuis",
            "Uit",
            "Voorspelling",
            "Marktkans %",
            "Modelkans %",
            "Verschil pp",
            "Status",
        ]
        st.dataframe(show, use_container_width=True, hide_index=True)

with st.expander("🔎 Diagnose"):
    st.write(f"TOTO URL: {TOTO_URL}")
    st.write(f"TOTO gevonden: {len(toto_rows)} wedstrijden")
    st.write(f"football-data.org historische wedstrijden: {len(fd_matches)}")
    if toto_error:
        st.write("TOTO-fout:", toto_error)
    if fd_matches_error:
        st.write("API-fout:", fd_matches_error)
    if not df.empty:
        st.write("Historische koppeling:")
        st.dataframe(
            df[["home", "away", "status"]],
            use_container_width=True,
            hide_index=True,
        )

with st.expander("ℹ️ Over v7"):
    st.markdown(
        """
**v7 is anders dan v6:**

1. **TOTO is de bron voor wedstrijden van vandaag.**
2. **football-data.org is alleen de historische bron.**
3. TOTO 1/X/2-odds worden omgerekend naar een genormaliseerde marktkans.
4. Recente vorm, punten per wedstrijd en doelpunten voor/tegen geven een beperkte correctie.
5. Alleen een modelkans boven de ingestelde drempel wordt TOP PICK.

Een modelkans van 75% is een statistische inschatting en **geen garantie op winst**.
"""
    )

st.caption(
    f"Laatst bijgewerkt: {datetime.now(TZ).strftime('%d-%m-%Y %H:%M:%S')}"
)
