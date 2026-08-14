import re
import difflib
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from html.parser import HTMLParser

import requests
import streamlit as st

# ============================================================
# TOTO AI VOORSPELLER v9
# ============================================================
# TOTO = wedstrijden + 1/X/2 odds
# football-data.org = historische vorm
# Alleen TOP PICKS >= ingestelde drempel worden aanbevolen.
# Let op: een modelkans is een statistische inschatting, geen garantie.
# ============================================================

TOTO_URL = "https://sport.toto.nl/wedden/11/voetbal/wedstrijden"
FD_URL = "https://api.football-data.org/v4"
TZ = ZoneInfo("Europe/Amsterdam")

ALIASES = {
    "fc nordsjaelland": "nordsjaelland",
    "fc nordjsaelland": "nordsjaelland",
    "nordsjaelland": "nordsjaelland",
    "psv eindhoven": "psv",
    "afc ajax": "ajax",
    "ajax amsterdam": "ajax",
    "feyenoord rotterdam": "feyenoord",
    "fc twente": "twente",
    "fc utrecht": "utrecht",
    "fc groningen": "groningen",
    "nec nijmegen": "nec nijmegen",
    "n e c nijmegen": "nec nijmegen",
    "sparta rotterdam": "sparta",
    "az alkmaar": "az",
    "rkc waalwijk": "rkc waalwijk",
    "go ahead eagles": "go ahead eagles",
    "valur reykjavik v": "valur reykjavik",
}

def norm(name):
    s = str(name).lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(fc|cf|sc|afc|ac|fk|bk|sk|kv|club)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)

def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 92
    ta, tb = set(a.split()), set(b.split())
    overlap = len(ta & tb)
    if overlap >= 2:
        return 82
    if overlap == 1:
        return 68
    return int(difflib.SequenceMatcher(None, a, b).ratio() * 100)

def secret_api_key():
    try:
        return st.secrets["FOOTBALL_DATA_API_KEY"]
    except Exception:
        return ""

def fd_get(path, params=None):
    key = secret_api_key()
    if not key:
        return None, "FOOTBALL_DATA_API_KEY ontbreekt in Streamlit Secrets."
    try:
        r = requests.get(
            FD_URL + path,
            params=params or {},
            headers={"X-Auth-Token": key, "User-Agent": "TOTO-AI-Voorspeller/9"},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json(), ""
        return None, f"football-data.org HTTP {r.status_code}: {r.text[:180]}"
    except Exception as e:
        return None, str(e)

@st.cache_data(ttl=900)
def test_fd():
    data, err = fd_get("/areas/2267")
    return bool(data and not err), err

# ---------------- TOTO scraper ----------------

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.current_href = None
        self.current = []
        self.depth = 0
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href", "")
            if "/wedden/wedstrijd/" in href:
                self.current_href = href
                self.current = []
                self.depth = 0
        elif self.current_href:
            self.depth += 1
    def handle_data(self, data):
        if self.current_href:
            self.current.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_href:
            text = re.sub(r"\s+", " ", " ".join(self.current)).strip()
            self.links.append((self.current_href, text))
            self.current_href = None
            self.current = []
            self.depth = 0
        elif self.current_href and self.depth:
            self.depth -= 1

def decimal_numbers(text):
    # Decimal odds in Dutch TOTO are normally written with a dot in HTML.
    vals = []
    for x in re.findall(r"(?<!\d)(\d{1,2}[.,]\d{2})(?!\d)", text):
        v = float(x.replace(",", "."))
        if 1.01 <= v <= 100:
            vals.append(v)
    return vals

def extract_teams_from_text(text):
    # Try common listing formats:
    # "Team A Team B Resultaat 1 1.80 X 3.50 2 4.20"
    clean = re.sub(r"\s+", " ", text).strip()
    clean = re.sub(r"\bVandaag\s+\d{1,2}:\d{2}\b", "", clean, flags=re.I)
    clean = re.sub(r"\b\d{1,2}:\d{2}\b", "", clean)
    clean = re.sub(r"\bResultaat\b", "", clean, flags=re.I)
    clean = re.sub(r"\b1e helft\b|\b2e helft\b", "", clean, flags=re.I)
    clean = re.sub(r"\b\d{1,2}\s+wedopties\b", "", clean, flags=re.I)
    # Remove odds and common labels, preserving team words.
    clean = re.sub(r"\b[1X2]\b", " ", clean)
    clean = re.sub(r"\b\d{1,2}[.,]\d{2}\b", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean

def parse_toto_html(html, today):
    parser = LinkParser()
    parser.feed(html)
    rows = []
    seen = set()

    for href, anchor_text in parser.links:
        if not anchor_text:
            continue
        txt = anchor_text.replace("\xa0", " ")
        odds = decimal_numbers(txt)
        if len(odds) < 3:
            continue

        # Search for an explicit 1/X/2 triplet. The first valid triplet is used.
        market = None
        for i in range(len(odds) - 2):
            tri = odds[i:i+3]
            if all(v > 1.0 for v in tri):
                market = tri
                break
        if not market:
            continue

        # Extract names from the URL slug first; this is generally more stable
        # than trying to infer names from the whole card text.
        slug = href.split("/wedstrijd/")[-1].split("?")[0].strip("/")
        slug = re.sub(r"^\d+/", "", slug)
        parts = re.split(r"-vs-|/vs/|_vs_", slug, flags=re.I)
        if len(parts) != 2:
            # Fallback: use text around the odds.
            before = txt.split(str(market[0]).replace(".", ","))[0]
            before = before.split(str(market[0]))[0]
            names = extract_teams_from_text(before)
            tokens = names.split()
            if len(tokens) < 2:
                continue
            mid = len(tokens) // 2
            home, away = " ".join(tokens[:mid]), " ".join(tokens[mid:])
        else:
            home = re.sub(r"[-_]+", " ", parts[0]).strip()
            away = re.sub(r"[-_]+", " ", parts[1]).strip()

        if len(home) < 2 or len(away) < 2:
            continue

        # Find time if present in the card.
        tm = re.search(r"\b(\d{1,2}:\d{2})\b", txt)
        match_time = tm.group(1) if tm else "—"

        key = (norm(home), norm(away))
        if key in seen:
            continue
        seen.add(key)

        # Convert decimal odds to normalized market probabilities.
        inv = [1 / x for x in market]
        total = sum(inv)
        probs = [x / total * 100 for x in inv]

        rows.append({
            "home": home.title(),
            "away": away.title(),
            "time": match_time,
            "odds_1": market[0],
            "odds_x": market[1],
            "odds_2": market[2],
            "market_1": probs[0],
            "market_x": probs[1],
            "market_2": probs[2],
            "url": "https://sport.toto.nl" + href if href.startswith("/") else href,
        })
    return rows

@st.cache_data(ttl=300)
def get_toto_today(today_iso):
    try:
        r = requests.get(
            TOTO_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
                "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
            },
            timeout=25,
        )
        r.raise_for_status()
        rows = parse_toto_html(r.text, datetime.fromisoformat(today_iso).date())
        return rows, ""
    except Exception as e:
        return [], str(e)

# ---------------- Historical data ----------------

@st.cache_data(ttl=900)
def get_fd_teams():
    data, err = fd_get("/teams", {"limit": 500})
    return (data or {}).get("teams", []), err

def find_team_id(name, teams):
    best_id, best_score = None, 0
    for t in teams:
        score = max(
            sim(name, t.get("name", "")),
            sim(name, t.get("shortName", "")),
            sim(name, t.get("tla", "")),
        )
        if score > best_score:
            best_score, best_id = score, t.get("id")
    return (best_id, best_score) if best_score >= 70 else (None, best_score)

@st.cache_data(ttl=900)
def get_team_history(team_id, date_from, date_to, limit=100):
    data, err = fd_get(
        f"/teams/{team_id}/matches",
        {
            "dateFrom": date_from,
            "dateTo": date_to,
            "status": "FINISHED",
            "limit": limit,
        },
    )
    return (data or {}).get("matches", []), err

def form_from_team_matches(matches, team_id, today, n):
    games = []
    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        try:
            d = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).date()
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
    return {"ppg": points / valid, "gf": gf / valid, "ga": ga / valid, "games": valid}

def model_probability(home, away, market_probs):
    # Market is the strongest prior; recent form supplies a controlled correction.
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

    final = [
        0.65 * market_probs[0] + 0.35 * form_raw[0],
        0.65 * market_probs[1] + 0.35 * form_raw[1],
        0.65 * market_probs[2] + 0.35 * form_raw[2],
    ]
    s = sum(final)
    return [x / s for x in final]

# ---------------- App ----------------

with st.sidebar:
    st.header("⚙️ Instellingen")
    threshold = st.slider("TOP PICK vanaf (%)", 50, 95, 75)
    lookback = st.slider("Vormperiode", 5, 10, 10)
    max_history_teams = st.slider("Max. wedstrijden met historische analyse", 2, 6, 4)

    if st.button("🔄 Ververs alles"):
        st.cache_data.clear()
        st.rerun()

today = datetime.now(TZ).date()
fd_ok, fd_error = test_fd()
toto_rows, toto_error = get_toto_today(today.isoformat())
fd_teams, fd_teams_error = get_fd_teams() if fd_ok else ([], "")

# The free API is rate-limited. Analyse only the strongest market candidates.
def market_max(r):
    return max(r["market_1"], r["market_x"], r["market_2"])

candidate_order = sorted(
    range(len(toto_rows)),
    key=lambda i: market_max(toto_rows[i]),
    reverse=True,
)
history_indexes = set(candidate_order[:max_history_teams])

results = []
history_debug = []

for idx, t in enumerate(toto_rows):
    hf = af = None
    data_source = "Niet geanalyseerd (API-limiet)"

    if idx in history_indexes and fd_ok and fd_teams:
        home_id, home_score = find_team_id(t["home"], fd_teams)
        away_id, away_score = find_team_id(t["away"], fd_teams)

        if home_id and away_id:
            from_date = (today - timedelta(days=365)).isoformat()
            to_date = today.isoformat()

            home_hist, home_err = get_team_history(home_id, from_date, to_date, 100)
            # Small pause only between requests in the same uncached run.
            time.sleep(0.15)
            away_hist, away_err = get_team_history(away_id, from_date, to_date, 100)

            hf = form_from_team_matches(home_hist, home_id, today, lookback)
            af = form_from_team_matches(away_hist, away_id, today, lookback)

            if hf and af:
                data_source = f"Teamhistorie (match {home_score:.0f}/{away_score:.0f})"
            else:
                data_source = "Team gevonden, maar onvoldoende historische wedstrijden"
        else:
            data_source = f"Team niet betrouwbaar gekoppeld ({home_score:.0f}/{away_score:.0f})"

    if not hf or not af:
        results.append({
            **t,
            "prediction": None,
            "model": None,
            "market": market_max(t),
            "value": None,
            "status": "Onvoldoende historische data",
            "details": None,
            "data_source": data_source,
        })
        continue

    market = [t["market_1"] / 100, t["market_x"] / 100, t["market_2"] / 100]
    probs = model_probability(hf, af, market)
    choices = [t["home"], "Gelijkspel", t["away"]]
    pick_idx = max(range(3), key=lambda i: probs[i])
    model_pct = probs[pick_idx] * 100
    market_pct = market[pick_idx] * 100

    results.append({
        **t,
        "prediction": choices[pick_idx],
        "model": model_pct,
        "market": market_pct,
        "value": model_pct - market_pct,
        "status": "Historische data OK",
        "details": {
            "home_ppg": hf["ppg"], "away_ppg": af["ppg"],
            "home_gf": hf["gf"], "away_gf": af["gf"],
            "home_ga": hf["ga"], "away_ga": af["ga"],
            "home_games": hf["games"], "away_games": af["games"],
        },
        "data_source": data_source,
    })

import pandas as pd
df = pd.DataFrame(results)

if not df.empty:
    picks = df[
        df["model"].notna() & (df["model"] >= threshold)
    ].sort_values(["model", "value"], ascending=False)
else:
    picks = df

st.title("⚽ TOTO AI Voorspeller v9")
st.caption("TOTO vandaag → historische vorm → gecontroleerde modelkans → alleen TOP PICKS ≥ drempel")

c1, c2, c3, c4 = st.columns(4)
c1.metric("TOTO-wedstrijden vandaag", len(toto_rows))
c2.metric(f"TOP PICKS ≥ {threshold}%", len(picks))
c3.metric("Hoogste modelkans", f"{picks['model'].max():.1f}%" if not picks.empty else "—")
c4.metric("football-data.org", "🟢 Verbonden" if fd_ok else "🔴 Niet verbonden")

if fd_ok:
    st.success("🟢 football-data.org is gekoppeld. De API wordt alleen gebruikt voor historische vorm.")
else:
    st.error("🔴 football-data.org is niet beschikbaar. Controleer FOOTBALL_DATA_API_KEY in Streamlit Secrets.")

if toto_error:
    st.warning("TOTO kon niet rechtstreeks worden uitgelezen: " + toto_error)
elif not toto_rows:
    st.warning("TOTO is bereikbaar, maar er konden geen 1/X/2-wedstrijden uit de pagina worden gehaald. Zie Diagnose.")
else:
    st.info(f"🟢 TOTO levert {len(toto_rows)} wedstrijden die door de parser zijn herkend.")

st.subheader("⭐ TOP PICKS")
if picks.empty:
    st.info(
        f"Geen TOTO-wedstrijd heeft een modelkans van minimaal {threshold}% "
        "én voldoende historische data. De app verzint geen kansen."
    )
else:
    for _, r in picks.iterrows():
        with st.container(border=True):
            st.caption(f"Vandaag {r['time']} · {r['status']}")
            st.markdown(f"### {r['home']} – {r['away']}")
            st.markdown(f"**⭐ Voorspelling: {r['prediction']} · modelkans {r['model']:.1f}%**")
            st.progress(min(1.0, r["model"] / 100), text=f"Modelkans {r['model']:.1f}%")
            st.write(
                f"TOTO marktkans: **{r['market']:.1f}%** · "
                f"Modelkans: **{r['model']:.1f}%** · "
                f"Verschil: **{r['value']:+.1f} procentpunt**"
            )
            d = r["details"]
            st.caption(
                f"Vorm PPG: {r['home']} {d['home_ppg']:.2f} vs {r['away']} {d['away_ppg']:.2f} · "
                f"Goals voor: {d['home_gf']:.2f} vs {d['away_gf']:.2f} · "
                f"Goals tegen: {d['home_ga']:.2f} vs {d['away_ga']:.2f}"
            )

st.divider()
with st.expander("📋 Alle TOTO-wedstrijden van vandaag"):
    if df.empty:
        st.info("Geen TOTO-wedstrijden gevonden.")
    else:
        show = df[
            ["time", "home", "away", "prediction", "market", "model", "value", "status"]
        ].copy()
        show.columns = [
            "Tijd", "Thuis", "Uit", "Voorspelling",
            "Marktkans %", "Modelkans %", "Verschil pp", "Status"
        ]
        st.dataframe(show, use_container_width=True, hide_index=True)

with st.expander("🔎 Diagnose"):
    st.write(f"TOTO URL: {TOTO_URL}")
    st.write(f"TOTO gevonden: {len(toto_rows)} wedstrijden")
    st.write(f"Historische teams beschikbaar: {len(fd_teams)}")
    st.write(f"Historische analyse uitgevoerd voor maximaal: {max_history_teams} wedstrijden")
    if toto_error:
        st.write("TOTO-fout:", toto_error)
    if fd_error:
        st.write("football-data.org-fout:", fd_error)
    if fd_teams_error:
        st.write("Team-directory fout:", fd_teams_error)
    if not df.empty:
        st.write("Historische koppeling:")
        st.dataframe(
            df[["home", "away", "status", "data_source"]],
            use_container_width=True,
            hide_index=True,
        )

with st.expander("ℹ️ Over v9"):
    st.markdown(
        """
        **v9 lost de fout uit v8 op door alle imports en functies volledig in `app.py` te definiëren.**

        - TOTO is de bron voor wedstrijden en 1/X/2-markt.
        - football-data.org is de historische bron.
        - De gratis API wordt bewust beperkt gebruikt.
        - De modelkans is een transparante combinatie van markt en recente vorm.
        - Alleen kansen boven de ingestelde drempel worden als TOP PICK getoond.
        - 75% is een statistische inschatting, geen garantie op winst.
        """
    )

st.caption(f"Laatst bijgewerkt: {datetime.now(TZ).strftime('%d-%m-%Y %H:%M:%S')}")
