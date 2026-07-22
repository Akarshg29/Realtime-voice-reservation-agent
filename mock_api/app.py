from copy import deepcopy
from datetime import datetime
from typing import Optional
from uuid import uuid4
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

app=FastAPI(title="Luma Bistro Reservation API",version="1.0.0")
RESTAURANT={"name":"Luma Bistro","timezone":"America/Los_Angeles","hours":"Tue-Sun 17:00-22:00; Mon closed","slot_minutes":30,"max_standard_party_size":8}
INITIAL={
 "2026-08-14":{"17:30":8,"18:00":4,"18:30":0,"19:00":2,"19:30":8,"20:00":6},
 "2026-08-15":{"17:30":6,"18:00":2,"18:30":4,"19:00":0,"19:30":0,"20:00":8},
 "2026-08-16":{"17:30":4,"18:00":4,"18:30":4,"19:00":4,"19:30":4,"20:00":4}}
capacity=deepcopy(INITIAL)
reservations={"res_existing_4821":{"reservation_id":"res_existing_4821","confirmation_code":"LUMA-4821","name":"Alex Morgan","phone":"+13105550147","date":"2026-08-14","time":"18:00","party_size":2,"notes":"Window seat if available","status":"confirmed"}}
idempotency={}
failures={"2026-08-16":0}
handoffs=[]

def phone(v): return ''.join(c for c in v if c.isdigit() or c=='+')
def slot(d,t):
    if d not in capacity or t not in capacity[d]: raise HTTPException(422,detail={"code":"INVALID_SLOT"})
def alternatives(d,t,n):
    return [{"date":d,"time":s,"remaining_capacity":c} for s,c in sorted(capacity.get(d,{}).items()) if s!=t and c>=n][:3]

class Create(BaseModel):
    name:str=Field(min_length=2,max_length=100); phone:str=Field(min_length=7,max_length=30); date:str; time:str; party_size:int=Field(ge=1,le=8); notes:Optional[str]=None
class Update(BaseModel):
    date:Optional[str]=None; time:Optional[str]=None; party_size:Optional[int]=Field(default=None,ge=1,le=8); notes:Optional[str]=None
class Handoff(BaseModel):
    reason:str; customer_phone:Optional[str]=None; conversation_summary:str

@app.get('/health')
def health(): return {"status":"ok"}
@app.get('/restaurant')
def restaurant(): return RESTAURANT
@app.get('/availability')
def availability(date:str=Query(...),time:str=Query(...),party_size:int=Query(...,ge=1,le=8)):
    slot(date,time)
    if date=='2026-08-16' and failures[date]==0:
        failures[date]+=1
        raise HTTPException(503,detail={"code":"TEMPORARY_UPSTREAM_FAILURE","retry_after_ms":500})
    remaining=capacity[date][time]
    return {"available":remaining>=party_size,"date":date,"time":time,"party_size":party_size,"remaining_capacity":remaining,"alternatives":[] if remaining>=party_size else alternatives(date,time,party_size)}
@app.post('/reservations')
def create(p:Create,idempotency_key:str=Header(...,alias='Idempotency-Key')):
    if idempotency_key in idempotency: return idempotency[idempotency_key]
    slot(p.date,p.time)
    if capacity[p.date][p.time]<p.party_size: raise HTTPException(409,detail={"code":"SLOT_UNAVAILABLE","alternatives":alternatives(p.date,p.time,p.party_size)})
    r={"reservation_id":f"res_{uuid4().hex[:10]}","confirmation_code":f"LUMA-{uuid4().hex[:4].upper()}","name":p.name,"phone":phone(p.phone),"date":p.date,"time":p.time,"party_size":p.party_size,"notes":p.notes,"status":"confirmed","created_at":datetime.utcnow().isoformat()+'Z'}
    capacity[p.date][p.time]-=p.party_size; reservations[r['reservation_id']]=r; idempotency[idempotency_key]=r; return r
@app.get('/reservations/search')
def search(phone_number:Optional[str]=Query(None,alias='phone'),confirmation_code:Optional[str]=None):
    if not phone_number and not confirmation_code: raise HTTPException(422,detail={"code":"SEARCH_CRITERIA_REQUIRED"})
    p=phone(phone_number) if phone_number else None
    out=[r for r in reservations.values() if (p and r['phone']==p) or (confirmation_code and r['confirmation_code'].upper()==confirmation_code.upper())]
    return {"results":out}
@app.patch('/reservations/{reservation_id}')
def update(reservation_id:str,p:Update):
    r=reservations.get(reservation_id)
    if not r: raise HTTPException(404,detail={"code":"NOT_FOUND"})
    if r['status']=='cancelled': raise HTTPException(409,detail={"code":"ALREADY_CANCELLED"})
    nd,nt,np=p.date or r['date'],p.time or r['time'],p.party_size or r['party_size']; slot(nd,nt)
    available=capacity[nd][nt]+(r['party_size'] if nd==r['date'] and nt==r['time'] else 0)
    if available<np: raise HTTPException(409,detail={"code":"SLOT_UNAVAILABLE","alternatives":alternatives(nd,nt,np)})
    capacity[r['date']][r['time']]+=r['party_size']; capacity[nd][nt]-=np
    r.update({"date":nd,"time":nt,"party_size":np,"notes":p.notes if p.notes is not None else r['notes']}); return r
@app.post('/reservations/{reservation_id}/cancel')
def cancel(reservation_id:str):
    r=reservations.get(reservation_id)
    if not r: raise HTTPException(404,detail={"code":"NOT_FOUND"})
    if r['status']!='cancelled': r['status']='cancelled'; capacity[r['date']][r['time']]+=r['party_size']
    return r
@app.post('/handoff')
def handoff(p:Handoff):
    h={"handoff_id":f"handoff_{uuid4().hex[:10]}","status":"queued",**p.model_dump()}; handoffs.append(h); return h
@app.post('/admin/reset')
def reset():
    global capacity,reservations,idempotency,failures,handoffs
    capacity=deepcopy(INITIAL); reservations={"res_existing_4821":{"reservation_id":"res_existing_4821","confirmation_code":"LUMA-4821","name":"Alex Morgan","phone":"+13105550147","date":"2026-08-14","time":"18:00","party_size":2,"notes":"Window seat if available","status":"confirmed"}}; idempotency={}; failures={"2026-08-16":0}; handoffs=[]
    return {"status":"reset"}
