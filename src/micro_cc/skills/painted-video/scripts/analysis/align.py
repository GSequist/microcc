# Vendored from github.com/ledbetterljoshua/functional-emotions-video (MIT — see ../../LICENSE-lib).
# Needleman-Wunsch alignment of Whisper's transcript against your real lyrics.txt (handles
# mis-hearings/hallucinations). Requires analysis/lyrics.txt for your song. See ../../TIMING.md.
import json, re, numpy as np
raw = json.load(open("analysis/words_raw.json"))["words"]
# dedupe overlapping chunks: chunk c owns [c+2.5, c+27.5)
chunks = sorted(set(w["chunk"] for w in raw)); last=chunks[-1]
ww=[w for w in raw if (w["chunk"]==0 or w["s"]>=w["chunk"]+2.5) and (w["chunk"]==last or w["s"]<w["chunk"]+27.5)]
ww.sort(key=lambda w:w["s"])
norm=lambda s: re.sub(r"[^a-z0-9']","",s.lower().replace("’","'"))
# expand numerals in whisper
W=[]
for w in ww:
    n=norm(w["w"])
    if n in("171",): parts=["one","hundred","seventyone"]
    elif n=="100": parts=["one","hundred"]
    elif n=="71": parts=["seventyone"]
    elif n=="3am": parts=["3","am"]
    else: parts=[n] if n else []
    for i,p in enumerate(parts):
        d=(w["e"]-w["s"])/len(parts); W.append(dict(n=p,s=w["s"]+i*d,e=w["s"]+(i+1)*d))
lines=[l for l in open("analysis/lyrics.txt").read().split("\n") if l.strip()]
C=[]
for li,l in enumerate(lines):
    for tok in l.split():
        n=norm(tok.replace("-",""))
        if not n: continue
        C.append(dict(t=tok,n=n,line=li))
def sim(a,b):
    if a==b: return 2
    import difflib
    r=difflib.SequenceMatcher(None,a,b).ratio()
    return 1 if r>0.75 else -1
# Needleman-Wunsch
n,m=len(C),len(W); gap=-0.6
S=np.zeros((n+1,m+1)); B=np.zeros((n+1,m+1),dtype=np.int8)
S[:,0]=np.arange(n+1)*gap; S[0,:]=np.arange(m+1)*gap
for i in range(1,n+1):
    for j in range(1,m+1):
        opts=(S[i-1,j-1]+sim(C[i-1]["n"],W[j-1]["n"]), S[i-1,j]+gap, S[i,j-1]+gap)
        k=int(np.argmax(opts)); S[i,j]=opts[k]; B[i,j]=k
i,j=n,m; match={}
while i>0 and j>0:
    k=B[i,j]
    if k==0:
        if sim(C[i-1]["n"],W[j-1]["n"])>0: match[i-1]=j-1
        i-=1;j-=1
    elif k==1: i-=1
    else: j-=1
print("matched",len(match),"of",n)
for ci,wj in match.items(): C[ci]["s"]=W[wj]["s"]; C[ci]["e"]=W[wj]["e"]
# interpolate unmatched
idx=sorted(match)
for ci in range(n):
    if "s" in C[ci]: continue
    prev=max([k for k in idx if k<ci],default=None); nxt=min([k for k in idx if k>ci],default=None)
    a=C[prev]["e"] if prev is not None else 0; b=C[nxt]["s"] if nxt is not None else a+1
    span=(nxt if nxt is not None else n)-(prev if prev is not None else -1)
    f=(ci-(prev if prev is not None else -1))/span
    C[ci]["s"]=a+(b-a)*f- (b-a)/span*0.5; C[ci]["e"]=C[ci]["s"]+(b-a)/span*0.8; C[ci]["interp"]=True
out=[dict(t=c["t"],s=round(c["s"],2),e=round(c["e"],2),line=c["line"],**({"i":1} if c.get("interp") else {})) for c in C]
json.dump(dict(lines=lines,words=out),open("analysis/lyrics_timed.json","w"),indent=0)
for li,l in enumerate(lines):
    ws=[w for w in out if w["line"]==li]
    print(f'{ws[0]["s"]:7.2f}-{ws[-1]["e"]:7.2f} {"*" if any("i" in w for w in ws) else " "} {l}')
