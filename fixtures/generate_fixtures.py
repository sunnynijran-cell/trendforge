"""Generate synthetic YouTube API fixtures (fictional titles and channels)."""
import json, random, sys
from datetime import datetime, timedelta, timezone
random.seed(42)
themes = {
 "primavera p6 project controls": ["Primavera P6 critical path in 10 minutes","P6 baseline mistakes planners still make","Project controls career path 2026","Earned value explained with one chart","DCMA 14 point check walkthrough in P6","Why your P6 float is lying to you","Project controls interview questions answered","Schedule risk analysis Monte Carlo basics"],
 "ai agents automation": ["I built an AI agent team that runs my business","Claude Code agents tutorial for beginners","AI agents replace 5 tools in my workflow","Multi agent systems explained simply","AI automation for consultants that actually works","Building an AI agent with a dashboard","Agentic workflows: hype or real value","How to make AI agents check each other"],
 "small modular reactor nuclear": ["Small modular reactors: when will the first ones run","Rolls-Royce SMR progress update explained","Why nuclear megaprojects run late","SMR costs compared with wind and solar","Nuclear construction schedules: what goes wrong","India opens nuclear sector to private firms","Data centres want nuclear power now","Inside a nuclear plant life extension"],
 "bangkok expat life": ["Bangkok expat mistakes in your first month","Cost of living in Bangkok 2026 honest numbers","Expat visa options in Thailand explained","Funny things only Bangkok expats understand","Moving to Bangkok with kids: real talk","Thai phrases every expat needs","Bangkok condo hunting tips","Working remotely from Bangkok cafes"],
 "decision making leadership": ["Why big decisions take too long","Decision latency is killing your project","Leaders who decide fast and stay right","The cost of waiting for perfect data","How executives should read a schedule","Psychological safety in project teams","Running meetings that end in decisions","Bad news early: a leadership habit"],
}
channels = ["Controls Corner","Planner Pete","Agent Lab","Build With Bots","Atom Weekly","Grid Talk","Bangkok Bites Life","Expat Diaries TH","Lead Better Daily","Project Pulse"]
now = datetime(2026,10,20,12,tzinfo=timezone.utc)
items=[]; n=0
for q,titles in themes.items():
    for t in titles:
        n+=1
        age_h = random.uniform(6, 13*24)
        views = int(random.lognormvariate(10.2,1.3))
        likes = int(views*random.uniform(0.01,0.07))
        comments = int(views*random.uniform(0.0008,0.008))
        desc = f"In this video we cover {t.lower()}. Subscribe for more on {q}."
        if n==3: desc += " The hidden truth about project controls careers nobody tells you about in interviews."
        items.append({"kind":"youtube#video","id":f"fx{n:09d}",
          "snippet":{"publishedAt":(now-timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "title":t,"description":desc,"channelTitle":random.choice(channels),
                     "tags":q.split()+["2026"]},
          "statistics":{"viewCount":str(views),"likeCount":str(likes),"commentCount":str(comments)},
          "contentDetails":{"duration":f"PT{random.randint(0,25)}M{random.randint(0,59)}S"}})
json.dump({"_note":"Synthetic fixture in YouTube Data API v3 videos.list shape. Titles and channels are fictional.","items":items},open(sys.argv[1],"w"),indent=1)
print(len(items))
