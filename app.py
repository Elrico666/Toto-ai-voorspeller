import os,re,sqlite3
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pandas as pd,requests,streamlit as st
from bs4 import BeautifulSoup

TOTO_URL="https://sport.toto.nl/wedden/11/voetbal/wedstrijden"
API_URL="https://api.football-data.org/v4"; TZ=ZoneInfo("Europe/Amsterdam"); DB="predictions.db"
st.set_page_config(page_title="TOTO AI Voorspeller v4",page_icon="⚽",layout="wide")

def api_key():
    try: k=st.secrets.get("FOOTBALL_DATA_API_KEY","")
    except Exception: k=""
    return str(k).strip() or os.getenv("FOOTBALL_DATA_API_KEY","").strip()
KEY=api_key()

def init():
    c=sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS predictions(
    id INTEGER PRIMARY KEY,created_at TEXT,match_date TEXT,match_name TEXT,home TEXT,away TEXT,
    prediction TEXT,probability REAL,market_probability REAL,value REAL,odds REAL,result TEXT,source TEXT)""")
    c.commit();c.close()
init()

def save(x):
    c=sqlite3.connect(DB);c.execute("""INSERT INTO predictions
    (created_at,match_date,match_name,home,away,prediction,probability,market_probability,value,odds,result,source)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",x);c.commit();c.close()

def hist():
    c=sqlite3.connect(DB);d=pd.read_sql_query("SELECT * FROM predictions ORDER BY id DESC",c);c.close();return d

def num(x): return float(str(x).replace(",",".")) 
def norm(x): return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9 ]","",str(x).lower())).strip()

@st.cache_data(ttl=180)
def toto_html():
    r=requests.get(TOTO_URL,headers={"User-Agent":"Mozilla/5.0 Chrome/151 Safari/537.36"},timeout=25);r.raise_for_status();return r.text

def parse(html):
    L=[re.sub(r"\s+"," ",x).strip() for x in BeautifulSoup(html,"html.parser").get_text("\n").splitlines()]
    L=[x for x in L if x];out=[]
    for i in range(len(L)-7):
        try:
            if L[i]!="1" or L[i+2]!="X" or L[i+4]!="2":continue
            o1,ox,o2=num(L[i+1]),num(L[i+3]),num(L[i+5])
            t=next((x for x in L[i+6:i+12] if "Vandaag" in x),None)
            if not t:continue
            c=[x for x in L[max(0,i-8):i] if x.lower() not in {"resultaat","wedoptie"}]
            if len(c)<2:continue
            raw=[1/o1,1/ox,1/o2];s=sum(raw)
            out.append({"home":c[-2],"away":c[-1],"time":t.replace("Vandaag","").strip(),
                        "o1":o1,"ox":ox,"o2":o2,"p1":raw[0]/s,"px":raw[1]/s,"p2":raw[2]/s})
        except:pass
    u={};[u.update({(x["home"],x["away"],x["time"]):x}) for x in out];return list(u.values())

@st.cache_data(ttl=900)
def matches(key,day):
    if not key:return []
    try:
        r=requests.get(f"{API_URL}/matches",params={"dateFrom":day.isoformat(),"dateTo":day.isoformat()},
                       headers={"X-Auth-Token":key},timeout=20)
        return r.json().get("matches",[]) if r.status_code==200 else []
    except:return []

@st.cache_data(ttl=900)
def team_history(key,tid,day):
    try:
        r=requests.get(f"{API_URL}/teams/{tid}/matches",
          params={"dateFrom":(day-timedelta(days=180)).isoformat(),"dateTo":(day-timedelta(days=1)).isoformat(),
                  "status":"FINISHED","limit":20},headers={"X-Auth-Token":key},timeout=20)
        return r.json().get("matches",[]) if r.status_code==200 else []
    except:return []

def find(t,ms):
    a,b=norm(t["home"]),norm(t["away"]);best=None;score=0
    for m in ms:
        h=norm(m.get("homeTeam",{}).get("name",""));aw=norm(m.get("awayTeam",{}).get("name",""))
        s=(80 if a in h or h in a else 0)+(80 if b in aw or aw in b else 0)
        if s>score:best,score=m,s
    return best if score>=100 else None

def form(ms,tid):
    pts=gf=ga=n=0
    for m in ms[-10:]:
        hg=m.get("score",{}).get("fullTime",{}).get("home");ag=m.get("score",{}).get("fullTime",{}).get("away")
        if hg is None or ag is None:continue
        if tid==m.get("homeTeam",{}).get("id"):gf+=hg;ga+=ag;pts+=3 if hg>ag else 1 if hg==ag else 0;n+=1
        elif tid==m.get("awayTeam",{}).get("id"):gf+=ag;ga+=hg;pts+=3 if ag>hg else 1 if ag==hg else 0;n+=1
    return None if not n else {"ppg":pts/n,"gf":gf/n,"ga":ga/n}

def predict(t,m,key,day):
    market=[t["p1"],t["px"],t["p2"]]
    if not m or not key:return market,False,{}
    hf=form(team_history(key,m["homeTeam"]["id"],day),m["homeTeam"]["id"])
    af=form(team_history(key,m["awayTeam"]["id"],day),m["awayTeam"]["id"])
    if not hf or not af:return market,False,{}
    edge=max(-.18,min(.18,.10*(hf["ppg"]-af["ppg"])/3+.05*(hf["gf"]-hf["ga"]-af["gf"]+af["ga"])/4))
    q=[market[0]*(1+edge),market[1]*(1-abs(edge)*.35),market[2]*(1-edge)];s=sum(q);q=[x/s for x in q]
    p=[.75*market[i]+.25*q[i] for i in range(3)]
    return p,True,{"home_ppg":hf["ppg"],"away_ppg":af["ppg"],"home_gf":hf["gf"],"away_gf":af["gf"],"home_ga":hf["ga"],"away_ga":af["ga"]}

today=datetime.now(TZ).date()
with st.sidebar:
    st.header("Instellingen");threshold=st.slider("TOP PICK vanaf",50,95,75)
    st.caption("API-key wordt automatisch uit Streamlit Secrets gelezen.")
    if st.button("🔄 Ververs data"):st.cache_data.clear();st.rerun()

st.title("⚽ TOTO AI Voorspeller v4");st.caption("Marktkans + vorm + doelpunten + value + historische scorekaart")
try:toto=parse(toto_html())
except Exception as e:st.error("TOTO-data kon niet worden opgehaald.");st.caption(str(e));st.stop()
api=matches(KEY,today)
rows=[]
for t in toto:
    m=find(t,api) if api else None;p,en,d=predict(t,m,KEY,today);k=max(range(3),key=lambda i:p[i])
    labels=["1","X","2"];choices=[t["home"],"Gelijkspel",t["away"]];odds=[t["o1"],t["ox"],t["o2"]][k]
    mp=max(t["p1"],t["px"],t["p2"])*100;pp=p[k]*100
    rows.append({"home":t["home"],"away":t["away"],"time":t["time"],"prediction":labels[k],"choice":choices[k],
                 "prob":pp,"market":mp,"value":pp-mp,"odds":odds,"data":en,"details":d})
df=pd.DataFrame(rows);top=df[df.prob>=threshold].sort_values(["prob","value"],ascending=False)

a,b,c,d=st.columns(4);a.metric("Wedstrijden vandaag",len(df));b.metric(f"TOP PICKS ≥ {threshold}%",len(top))
c.metric("Hoogste modelkans",f"{df.prob.max():.1f}%" if len(df) else "—");c4="🟢 Actief" if KEY and api else "🔴 Niet gekoppeld";d.metric("Data-analyse",c4)
if KEY and not api:st.warning("API-key is gevonden, maar er kwamen geen API-wedstrijden terug. Controleer de gratis data-dekking later opnieuw.")

st.subheader("⭐ TOP PICKS")
if top.empty:st.info(f"Geen modelvoorspelling haalt {threshold}%.")
for _,r in top.iterrows():
    with st.container(border=True):
        st.caption(f"{r.time} · {'📊 Data-analyse' if r.data else '📈 Alleen TOTO-markt'}")
        st.markdown(f"### {r.home} – {r.away}")
        st.markdown(f"**{r.choice} · {r.prob:.1f}%**")
        st.write(f"Marktkans: **{r.market:.1f}%** · Value: **{'+' if r.value>=0 else ''}{r.value:.1f}%** · TOTO odd: **{r.odds:.2f}**")
        if r.data:
            x=r.details;st.caption(f"Vorm PPG — {r.home}: {x['home_ppg']:.2f} | {r.away}: {x['away_ppg']:.2f} · Doelpunten — {r.home}: {x['home_gf']:.2f} voor / {x['home_ga']:.2f} tegen | {r.away}: {x['away_gf']:.2f} voor / {x['away_ga']:.2f} tegen")
    c=sqlite3.connect(DB);ex=c.execute("SELECT 1 FROM predictions WHERE match_date=? AND home=? AND away=? AND prediction=?",(today.isoformat(),r.home,r.away,r.prediction)).fetchone();c.close()
    if not ex:save((datetime.now(TZ).isoformat(),today.isoformat(),f"{r.home} – {r.away}",r.home,r.away,r.prediction,r.prob,r.market,r.value,r.odds,"PENDING","API+markt" if r.data else "markt"))

st.divider();st.subheader("📈 Historische prestaties");h=hist()
if h.empty:st.info("Nog geen voorspellingen opgeslagen.")
else:
    s=h[h.result.isin(["WON","LOST"])];total=len(s);wins=int((s.result=="WON").sum());x,y,z=st.columns(3)
    x.metric("Beoordeelde picks",total);y.metric("Goed",wins);z.metric("Hit-rate",f"{wins/total*100:.1f}%" if total else "—")
    st.dataframe(h[["match_date","match_name","prediction","probability","market_probability","value","odds","result"]].head(100),use_container_width=True,hide_index=True)

with st.expander("Hoe werkt het model?"):
    st.write("TOTO-marktkans is de basis (75%). De statistische correctie (25%) gebruikt recente vorm en doelpunten. Value = modelkans − marktkans. Een score van 75% is geen bewezen winrate en geen garantie.")
st.caption(f"Laatste update: {datetime.now(TZ).strftime('%d-%m-%Y %H:%M:%S')}")
st.link_button("Open TOTO voetbal",TOTO_URL)
