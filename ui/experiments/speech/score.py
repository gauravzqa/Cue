import json, re, sys, unicodedata
import os
# The manifest `run.sh` wrote, or a checked-in one next to the results.
SP = os.environ.get("DAA_SPEECH_WORK") or os.path.dirname(os.path.abspath(sys.argv[1]))
man = json.load(open(os.path.join(SP, "manifest.json")))
res={r["wav"].split("/")[-1]: r for r in json.load(open(sys.argv[1]))}
def norm(s):
    s=unicodedata.normalize("NFKD",s).lower()
    s=s.replace("’","'")
    s=re.sub(r"[^a-z0-9' ]+"," ",s)
    s=re.sub(r"\b(\d+)\b", lambda m:{"10":"ten","3":"three"}.get(m.group(1),m.group(1)), s)
    return re.sub(r"\s+"," ",s).strip()
def lev(a,b):
    d=list(range(len(b)+1))
    for i,x in enumerate(a,1):
        p=d[0]; d[0]=i
        for j,y in enumerate(b,1):
            c=min(d[j]+1, d[j-1]+1, p+(x!=y)); p=d[j]; d[j]=c
    return d[-1]
tot_e=tot_w=0; exact=0; rows=[]
for m in man:
    k=m["wav"].split("/")[-1]
    hyp=norm(res[k]["text"]); ref=norm(m["ref"])
    r=ref.split(); h=hyp.split()
    e=lev(r,h); tot_e+=e; tot_w+=len(r)
    ok = hyp==ref
    exact+= ok
    rows.append((m["i"],m["category"],ok,e,len(r),ref,hyp,res[k]["ms"]))
print(f"utterances       {len(man)}")
print(f"exact match      {exact}/{len(man)} = {100*exact/len(man):.1f}%")
print(f"WER              {tot_e}/{tot_w} = {100*tot_e/tot_w:.1f}%")
lat=sorted(res[m['wav'].split('/')[-1]]['ms'] for m in man)
print(f"latency ms       median {lat[len(lat)//2]}  p90 {lat[int(.9*len(lat))]}  max {lat[-1]}")
print()
print("MISMATCHES")
for i,cat,ok,e,n,ref,hyp,ms in rows:
    if not ok: print(f"  {i:2d} [{cat:16s}] ref={ref!r}\n      hyp={hyp!r}")
# per category
from collections import defaultdict
c=defaultdict(lambda:[0,0,0,0])
for i,cat,ok,e,n,ref,hyp,ms in rows:
    c[cat][0]+=ok; c[cat][1]+=1; c[cat][2]+=e; c[cat][3]+=n
print("\nBY CATEGORY  exact   WER")
for k,v in sorted(c.items()):
    print(f"  {k:18s} {v[0]}/{v[1]}   {100*v[2]/v[3]:5.1f}%")
