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
}


def norm(name):
    s = str(name).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(fc|cf|sc|afc|ac|fk|bk)\b", " ", s)
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

    return best if best_score >= 105 else None


@st.cache_data(ttl=900)
def get_fd_teams():
    data, err = fd_get("/teams", {"limit": 500})
    return (data or {}).get("teams", []), err


def find_team_id(name, teams):
    best_id = None
    best_score = 0
    for t in teams:
        score = max(
            sim(name, t.get("name", "")),
            sim(name, t.get("shortName", "")),
            sim(name, t.get("tla", "")),
        )
        if score > best_score:
            best_score = score
            best_id = t.get("id")
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

# TOTO blijft de primaire bron voor de wedstrijden van vandaag.
toto_rows = []
toto_error = ""
try:
    toto_text = get_toto_text()
    toto_rows = parse_toto_today(toto_text)
except Exception as e:
    toto_error = str(e)

# Eerste historische poging: één gecombineerde call.
# De matcher in v8 is veel toleranter dan v7.
fd_matches, fd_matches_error = get_fd_matches(
    (today - timedelta(days=365)).isoformat(),
    today.isoformat(),
)

# Tweede bron voor historische data: team-directory + team endpoint.
# We gebruiken deze fallback alleen waar nodig, en eerst bij de wedstrijden
# met de hoogste marktkans. Dit houdt het gratis API-limiet beheersbaar.
fd_teams, fd_teams_error = get_fd_teams()

def market_max(r):
    return max(r["market_1"], r["market_x"], r["market_2"])

candidate_order = sorted(
    range(len(toto_rows)),
    key=lambda i: market_max(toto_rows[i]),
    reverse=True,
)

# Maximaal 6 wedstrijden krijgen de duurdere team-history fallback.
fallback_indexes = set(candidate_order[:6])

results = []

for idx, t in enumerate(toto_rows):
    # Eerst proberen via de gezamenlijke matchset.
    m = find_fd_match(t, fd_matches)
    hf = af = None
    data_source = "Gezamenlijke historische dataset"

    if m:
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

    # v8 fallback: zoek de teams rechtstreeks en vraag hun eigen historie op.
    # Alleen voor de beste kandidaten, om binnen de gratis 10 req/min limiet
    # van football-data.org te blijven.
    if (not hf or not af) and idx in fallback_indexes and fd_ok and fd_teams:
        home_id, home_score = find_team_id(t["home"], fd_teams)
        away_id, away_score = find_team_id(t["away"], fd_teams)

        if home_id and away_id:
            home_hist, home_err = get_team_history(
                home_id,
                (today - timedelta(days=365)).isoformat(),
                today.isoformat(),
                100,
            )
            away_hist, away_err = get_team_history(
                away_id,
                (today - timedelta(days=365)).isoformat(),
                today.isoformat(),
                100,
            )

            hf = form_from_team_matches(
                home_hist, home_id, today, lookback
            )
            af = form_from_team_matches(
                away_hist, away_id, today, lookback
            )

            if hf and af:
                data_source = (
                    f"Teamhistorie (match {home_score:.0f}/{away_score:.0f})"
                )

    if not hf or not af:
        results.append(
            {
                **t,
                "prediction": None,
                "model": None,
                "market": market_max(t),
                "value": None,
                "status": "Onvoldoende historische data",
                "details": None,
                "data_source": data_source,
            }
        )
        continue

    market = [
        t["market_1"] / 100,
        t["market_x"] / 100,
        t["market_2"] / 100,
    ]

    probs = model_probability(hf, af, market)
    choices = [t["home"], "Gelijkspel", t["away"]]
    pick_idx = max(range(3), key=lambda i: probs[i])

    model_pct = probs[pick_idx] * 100
    market_pct = market[pick_idx] * 100

    results.append(
        {
            **t,
            "prediction": choices[pick_idx],
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
            "data_source": data_source,
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
st.title("⚽ TOTO AI Voorspeller v8")
st.caption("TOTO vandaag → verbeterde historische teamkoppeling → modelkans → TOP PICKS ≥ drempel")

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
        "Deze API wordt in v8 alleen gebruikt voor historische vorm en teamhistorie."
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
    st.write(f"football-data.org historische wedstrijden in gecombineerde dataset: {len(fd_matches)}")
    st.write(f"football-data.org teams gevonden: {len(fd_teams)}")
    if fd_teams_error:
        st.write("Team-directory fout:", fd_teams_error)
    if toto_error:
        st.write("TOTO-fout:", toto_error)
    if fd_matches_error:
        st.write("API-fout:", fd_matches_error)
    if not df.empty:
        st.write("Historische koppeling:")
        st.dataframe(
            df[["home", "away", "status", "data_source"]],
            use_container_width=True,
            hide_index=True,
        )

with st.expander("ℹ️ Over v8"):
    st.markdown(
        """
**v8 is anders dan v7:**

1. **TOTO is de bron voor wedstrijden van vandaag.**
2. **football-data.org is alleen de historische bron.**
3. Eerst wordt de gezamenlijke historische dataset gebruikt; als dat niet lukt, zoekt v8 het team rechtstreeks op en haalt de teamhistorie op.
4. Recente vorm, punten per wedstrijd en doelpunten voor/tegen geven een beperkte correctie.
5. Alleen een modelkans boven de ingestelde drempel wordt TOP PICK.

Een modelkans van 75% is een statistische inschatting en **geen garantie op winst**.
"""
    )

st.caption(
    f"Laatst bijgewerkt: {datetime.now(TZ).strftime('%d-%m-%Y %H:%M:%S')}"
)
