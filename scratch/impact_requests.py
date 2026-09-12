"""How many rows of dataset/requests.csv change between INTRADAY_CHECK modes (no output.csv written)."""
import sys; sys.path.insert(0,'code')
from collections import Counter
from buyorwait.intake import Dataset, build_state
from buyorwait.evidence import image_amounts
from buyorwait.plans import decide, request_from_row
import buyorwait.forecast as F
ds=Dataset.load(); fa=image_amounts()
res={}
for mode in (False,'periodic'):
    F.INTRADAY_CHECK=mode
    for row in ds.requests.itertuples():
        req=request_from_row(row)
        st=build_state(ds,req.user_id,req.request_date,fa,request_id=req.request_id)
        d=decide(ds,st,req)
        res[(mode,req.request_id)]=(round(d.safe_today,2),d.status,d.method,str(d.earliest),tuple(d.plan.payments) if d.plan else None,tuple(c.render(str) for c in d.plan.changes) if d.plan else None)
ch=Counter()
for row in ds.requests.itertuples():
    a,b=res[(False,row.request_id)],res[('periodic',row.request_id)]
    for name,x,y in zip(('amount','status','method','earliest','plan','changes'),a,b):
        if x!=y: ch[name]+=1
print('rows', len(ds.requests), dict(ch))
