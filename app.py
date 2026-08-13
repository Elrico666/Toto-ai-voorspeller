
import os, re, sqlite3
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

TOTO_URL="https://sport.toto.nl/wedden/11/voetbal/wedstrijden"
API_URL="https://api.football-data.org/v4"
TZ=ZoneInfo("Europe/Amsterdam")
DB="predictions.db"

st.set_page_config(page_title="TOTO AI Voorspeller v3", page_icon="⚽", layout="wide")

def db():
    con=sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS predictions(
      id INTEGER PRIMARY KEY, created_at TEXT, match_date TEXT, match_name TEXT,
      home TEXT, away TEXT, prediction TEXT, probability REAL, market_probability REAL,
      model_probability REAL, odds REAL, result TEXT, source TEXT)""")
    con.commit()
    return con

def save_prediction(r):
    con=db()
    con.execute("""INSERT INTO predictions
      (created_at,match_date,match_name,home,away,prediction,probability,market_probability,model_probability,odds,result,source)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(r))
    con.commit(); con.close()

def load_history():
    con=db(); df=pd.read_sql_query("SELECT * FROM predictions ORDER BY id DESC",con); con.close(); return df

def n(x): return float(str(x).replace(",","."))
def norm(x): return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9 ]","",x.lower())).strip()

@st.cache_data(ttl=180)
def get_toto():
    r=requests.get(TOTO_URL,headers={"User-Agent":"Mozilla/5.0 Chrome/151 Safari/537.36"},timeout=25)
    r.raise_for_status(); return r.text

def parse_toto(html):
    lines=[re.sub(r"\s+"," ",x).strip() for x in BeautifulSoup(html,"html.parser").get_text("\n").splitlines()]
    lines=[x for x in lines if x]; out=[]
    for i in range(len(lines)-7):
        try:
            if lines[i]!="1" or lines[i+2]!="X" or lines[i+4]!="2": continue
            o1,ox,o2=n(lines[i+1]),n(lines[i+3]),n(lines[i+5])
            nearby=lines[i+6:i+11]
            today=next((x for x in nearby if "Vandaag" in x),None)
            if not today: continue
            before=lines[max(0,i-8):i]
            c=[x for x in before if x.lower() not in {"resultaat","wedoptie"}]
            if len(c)<2: continue
            home,away=c[-2],c[-1]
            raw=[1/o1,1/ox,1/o2]; s=sum(raw); p=[x/s for x in raw]
            out.append({"home":home,"away":away,"time":today.replace("Vandaag","").strip(),
                        "o1":o1,"ox":ox,"o2":o2,"p1":p[0],"px":p[1],"p2":p[2]})
        except: pass
    u={}
    for x in out:u[(x["home"],x["away"],x["time"])]=x
    return list(u.values())

@st.cache_data(ttl=900)
def api_matches(key,day):
    if not key:return []
    r=requests.get(f"{API_URL}/matches",params={"dateFrom":day.isoformat(),"dateTo":day.isoformat()},
                   headers={"X-Auth-Token":key},timeout=20)
    return r.json().get("matches",[]) if r.status_code==200 else []

@st.cache_data(ttl=900)
def history(key,team,day):
    if not key:return []
    r=requests.get(f"{API_URL}/teams/{team}/matches",
                   params={"dateFrom":(day-timedelta(days=180)).isoformat(),
                           "dateTo":(day-timedelta(days=1)).isoformat(),
                           "status":"FINISHED","limit":20},
                   headers={"X-Auth-Token":key},timeout=20)
    return r.json().get("matches",[]) if r.status_code==200 else []

def find(t,matches):
    a,b=norm(t["home"]),norm(t["away"]); best=None; score=0
    for m in matches:
        h=norm(m.get("homeTeam",{}).get("name","")); aw=norm(m.get("awayTeam",{}).get("name",""))
        s=(80 if a in h or h in a else 0)+(80 if b in aw or aw in b else 0)
        if s>score:best,score=m,s
    return best if score>=100 else None

def form(ms,tid):
    pts=gf=ga=0;n=0
    for m in ms[-10:]:
        hg=m.get("score",{}).get("fullTime",{}).get("home"); ag=m.get("score",{}).get("fullTime",{}).get("away")
        if hg is None or ag is None:continue
        if tid==m["homeTeam"]["id"]: gf+=hg;ga+=ag;pts+=3 if hg>ag else 1 if hg==ag else 0;n+=1
        elif tid==m["awayTeam"]["id"]:gf+=ag;ga+=hg;pts+=3 if ag>hg else 1 if ag==hg else 0;n+=1
    return None if not n else {"ppg":pts/n,"gf":gf/n,"ga":ga/n}

def probs(t,m,key,day):
    market=[t["p1"],t["px"],t["p2"]]
    if not m or not key:return market,0,market
    hf=form(history(key,m["homeTeam"]["id"],day),m["homeTeam"]["id"])
    af=form(history(key,m["awayTeam"]["id"],day),m["awayTeam"]["id"])
    if not hf or not af:return market,0,market
    edge=max(-.18,min(.18,.10*(hf["ppg"]-af["ppg"])/3+.05*(hf["gf"]-hf["ga"]-af["gf"]+af["ga"])/4))
    q=[market[0]*(1+edge),market[1]*(1-abs(edge)*.35),market[2]*(1-edge)]
    s=sum(q);q=[x/s for x in q]
    final=[.75*market[i]+.25*q[i] for i in range(3)]
    return final,1,q

today=datetime.now(TZ).date()

with st.sidebar:
    st.header("Instellingen")
    threshold=st.slider("TOP PICK vanaf",50,95,75)
    key=st.text_input("football-data.org API key",os.getenv("FOOTBALL_DATA_API_KEY",""),type="password")
    if st.button("🔄 Ververs"):st.cache_data.clear();st.rerun()

st.title("⚽ TOTO AI Voorspeller v3")
st.caption("Voorspelling + value + automatische historische scorekaart")

try:toto=parse_toto(get_toto())
except Exception as e:st.error("TOTO-data kon niet worden opgehaald.");st.stop()
api=api_matches(key,today)

rows=[]
for t in toto:
    m=find(t,api)
    p,enriched,_=probs(t,m,key,today)
    k=max(range(3),key=lambda i:p[i]); labels=["1","X","2"]; names=[t["home"],"Gelijkspel",t["away"]]
    odds=[t["o1"],t["ox"],t["o2"]][k]
    market=max(t["p1"],t["px"],t["p2"])*100
    model=p[k]*100
    value=model-market
    rows.append({"home":t["home"],"away":t["away"],"time":t["time"],"prediction":labels[k],
                 "choice":names[k],"prob":model,"market":market,"value":value,"odds":odds,
                 "data":"Ja" if enriched else "Markt"})

df=pd.DataFrame(rows)
top=df[df.prob>=threshold].sort_values("prob",ascending=False)

a,b,c,d=st.columns(4)
a.metric("Wedstrijden",len(df));b.metric("TOP PICKS",len(top));c.metric("Hoogste kans",f"{df.prob.max():.1f}%")
d.metric("Data-analyse","Actief" if api else "Niet gekoppeld")

st.subheader("⭐ TOP PICKS")
if top.empty:st.info(f"Geen picks ≥ {threshold}%.")
for _,r in top.iterrows():
    value=("+" if r.value>=0 else "")+f"{r.value:.1f}%"
    st.markdown(f"""<div style="padding:16px;border:1px solid #ddd;border-radius:15px;margin:8px 0">
    <div style="color:#666">{r.time} · data: {r.data}</div>
    <b>{r.home} – {r.away}</b><br>
    <span style="font-size:25px;font-weight:800">{r.choice} · {r.prob:.1f}%</span><br>
    Markt: {r.market:.1f}% · Value: <b>{value}</b> · TOTO odd: {r.odds:.2f}
    </div>""",unsafe_allow_html=True)
    con=db()
    exists=con.execute("SELECT 1 FROM predictions WHERE match_date=? AND home=? AND away=? AND prediction=?",
                       (today.isoformat(),r.home,r.away,r.prediction)).fetchone()
    con.close()
    if not exists:
        save_prediction((datetime.now(TZ).isoformat(),today.isoformat(),f"{r.home} – {r.away}",
                         r.home,r.away,r.prediction,r.prob,r.market,r.prob,r.odds,"PENDING",r.data))

st.divider()
st.subheader("📊 Historische prestaties")
hist=load_history()
if hist.empty:
    st.info("Nog geen voorspellingen opgeslagen. De scorekaart vult zich automatisch.")
else:
    settled=hist[hist.result.isin(["WON","LOST"])]
    total=len(settled); wins=int((settled.result=="WON").sum())
    x,y,z=st.columns(3)
    x.metric("Beoordeelde picks",total);y.metric("Goed",wins);z.metric("Hit-rate",f"{wins/total*100:.1f}%" if total else "—")
    st.dataframe(hist[["match_date","match_name","prediction","probability","market_probability","odds","result"]].head(100),
                 use_container_width=True,hide_index=True)

st.info("Resultaten worden niet automatisch als waarheid uit odds afgeleid. Voor echte backtesting moet een historische wedstrijdresultaat-feed worden gekoppeld; deze app bewaart alvast elke voorspelling zodat latere uitslagen kunnen worden verwerkt.")

with st.expander("Model"):
    st.write("75% TOTO-marktkans + 25% statistische correctie op recente vorm en doelpunten. Value = modelkans − marktkans. Dit is geen gegarandeerde winrate.")
st.link_button("Open TOTO voetbal",TOTO_URL)
