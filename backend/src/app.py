import json, os, time
from contextlib import asynccontextmanager
from datetime import datetime
import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from .casework_routes import router as operational_cases
from .operations_evidence import router as operations_evidence
from .auth import current_user, issue, password_hash, require
from .db import audit, connect, migrate, wait_for_db
from .schemas import Login, WaveIn, CountIn, SegmentSupplementIn, ForecastIn, ShiftIn, ConfirmIn, ReviewIn
from . import belt_series

r=redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)

def dump(rows): return [dict(row) for row in rows]
@asynccontextmanager
async def lifespan(_):
    wait_for_db(); migrate()
    if os.getenv('SEED_DEMO','true').lower()=='true':
        from scripts.seed_demo import main as seed_main
        seed_main()
    yield
app=FastAPI(title='机场行李带峰值预测', lifespan=lifespan)
app.include_router(operational_cases)
app.include_router(operations_evidence)
@app.middleware('http')
async def guard(request: Request, call_next):
    started=time.monotonic()
    try: response=await call_next(request)
    except HTTPException: raise
    except Exception as exc: return JSONResponse(status_code=500,content={'detail':str(exc)})
    response.headers['X-Request-ID']=os.urandom(8).hex(); response.headers['X-Process-Time']=f'{time.monotonic()-started:.4f}'; response.headers['Cache-Control']='no-store'; return response
@app.get('/health')
def health():
    with connect() as conn: conn.execute('SELECT 1')
    return {'status':'healthy','redis':bool(r.ping())}
@app.post('/login')
def login(data: Login):
    with connect() as conn:
        user=conn.execute('SELECT id,username,display_name,role,password_hash FROM users WHERE username=%s AND active',(data.username,)).fetchone()
        if not user or user['password_hash']!=password_hash(data.password): raise HTTPException(401,'用户名或密码不正确')
        audit(conn,user['id'],'login','user',user['id'],{'role':user['role']}); conn.commit()
        return {'access_token':issue(user),'token_type':'bearer','user':{k:user[k] for k in ('id','username','display_name','role')}}
@app.get('/me')
def me(user=Depends(current_user)): return user
@app.get('/dashboard')
def dashboard(user=Depends(current_user)):
    with connect() as conn:
        return {'waves':conn.execute('SELECT COUNT(*) c FROM flight_waves').fetchone()['c'],'active_waves':conn.execute("SELECT COUNT(*) c FROM flight_waves WHERE status IN ('open','counting','forecasted')").fetchone()['c'],'forecasts_ready':conn.execute("SELECT COUNT(*) c FROM peak_forecasts WHERE status IN ('ready','published')").fetchone()['c'],'blocked_waves':conn.execute("SELECT COUNT(*) c FROM peak_forecasts WHERE status='blocked'").fetchone()['c'],'needs_confirmation':conn.execute("SELECT COUNT(*) c FROM staff_shifts WHERE status='needs_confirmation'").fetchone()['c'],'open_reviews':conn.execute("SELECT COUNT(*) c FROM congestion_reviews WHERE status!='closed'").fetchone()['c']}
@app.get('/waves')
def waves(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT * FROM flight_waves ORDER BY arrival_start').fetchall())
@app.post('/waves',status_code=201)
def create_wave(data: WaveIn,user=Depends(require('operator','supervisor'))):
    if data.arrival_end<=data.arrival_start: raise HTTPException(400,'到达结束时间必须晚于开始时间')
    with connect() as conn:
        try:
            row=conn.execute('INSERT INTO flight_waves(code,terminal,arrival_start,arrival_end,flight_count,planned_bags,cancellation_state,notes,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,\'open\') RETURNING *',(data.code,data.terminal,data.arrival_start,data.arrival_end,data.flight_count,data.planned_bags,data.cancellation_state,data.notes)).fetchone()
        except Exception as exc: raise HTTPException(409,'航班波次编号已存在或字段不符合约束') from exc
        audit(conn,user['id'],'create','flight_wave',row['id'],{'code':row['code']}); conn.commit(); return row
def lock_wave(conn, wave_id):
    """Serialise reading/supplement writes per wave.

    Counts may be recorded while a supervisor is supplying a meter-swap period.
    Both writers take this row lock first, so the series is always re-derived
    against the other writer's committed rows and a supplement can never be
    stored against a boundary that has just moved.
    """

    return conn.execute('SELECT * FROM flight_waves WHERE id=%s FOR UPDATE', (wave_id,)).fetchone()


def refresh_wave_series(conn, wave_id):
    """Re-derive the wave series and settle its forecasts after a reading change.

    * Pending meter-swap periods pause the wave: any queued/running/ready
      forecast is marked blocked until a supervisor supplies the count.
    * Without pending periods, previously computed forecasts no longer match
      the current period boundaries, so they are re-queued instead of being
      left to mix pre/post-split periods.
    """

    wave, readings, segments = belt_series.load_wave_segments(conn, wave_id)
    fingerprints = belt_series.ready_fingerprints(segments)
    pending = belt_series.pending_segments(segments)
    belt_series.supersede_stale_supplements(conn, wave_id, segments)
    paused = '换表跨段待主管补数，暂停该波次预测'
    if pending:
        blocked = conn.execute(
            "UPDATE peak_forecasts SET status='blocked', failure_reason=%s, updated_at=now() "
            "WHERE wave_id=%s AND status IN ('queued','running','ready','published') RETURNING id",
            (paused, wave_id),
        ).fetchall()
        conn.execute("UPDATE flight_waves SET status='counting' WHERE id=%s AND status='forecasted'", (wave_id,))
        return segments, [], [row['id'] for row in blocked]
    # Pending periods cleared (a supervisor supplied the counts): forecasts
    # paused by the swap resume from the queue instead of staying blocked.
    resume = conn.execute(
        "UPDATE peak_forecasts SET status='queued', failure_reason=NULL, updated_at=now() "
        "WHERE wave_id=%s AND status='blocked' AND failure_reason=%s RETURNING id",
        (wave_id, paused),
    ).fetchall()
    refresh = conn.execute(
        "UPDATE peak_forecasts SET status='queued', failure_reason=NULL, updated_at=now() "
        "WHERE wave_id=%s AND status IN ('ready','published') "
        "AND coalesce(parameters->'input_fingerprints','[]'::jsonb) <> %s::jsonb RETURNING id",
        (wave_id, json.dumps(fingerprints)),
    ).fetchall()
    if requeued := resume + refresh:
        conn.execute("UPDATE flight_waves SET status='counting' WHERE id=%s AND status='forecasted'", (wave_id,))
    return segments, [row['id'] for row in requeued], []


@app.get('/waves/{wave_id}/counts')
def counts(wave_id:int,user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT * FROM belt_counts WHERE wave_id=%s ORDER BY counted_at,id',(wave_id,)).fetchall())
@app.post('/counts',status_code=201)
def create_count(data:CountIn,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        wave=lock_wave(conn,data.wave_id)
        if not wave: raise HTTPException(404,'航班波次不存在')
        try: row=conn.execute('INSERT INTO belt_counts(wave_id,belt_code,counted_at,bag_count,scan_source,reading_kind,meter_code,meter_swap) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',(data.wave_id,data.belt_code,data.counted_at,data.bag_count,data.scan_source,data.reading_kind,data.meter_code,data.meter_swap)).fetchone()
        except Exception as exc: raise HTTPException(409,'同一航班波次、行李带和时间不能重复计数') from exc
        _,requeue,_=refresh_wave_series(conn,data.wave_id)
        conn.execute("UPDATE flight_waves SET status='counting' WHERE id=%s AND status='open'",(data.wave_id,))
        audit(conn,user['id'],'create','belt_count',row['id'],{'wave_id':data.wave_id,'reading_kind':data.reading_kind,'meter_swap':data.meter_swap}); conn.commit()
    for forecast_id in requeue:
        try: r.rpush('airport:forecast',str(forecast_id))
        except Exception: pass
    return row
@app.get('/waves/{wave_id}/series')
def series(wave_id:int,user=Depends(current_user)):
    with connect() as conn:
        wave,readings,segments=belt_series.load_wave_segments(conn,wave_id)
        if not wave: raise HTTPException(404,'航班波次不存在')
        return {'wave':{'id':wave['id'],'code':wave['code']},'segments':[belt_series.public_segment(s) for s in segments],
                'pending':[belt_series.public_pending(s) for s in belt_series.pending_segments(segments)],
                'forecast_points':[{'counted_at':p['counted_at'],'bag_count':p['bag_count']} for p in belt_series.forecast_points(segments)]}
@app.post('/waves/{wave_id}/segments/supplements',status_code=201)
def supplement_segment(wave_id:int,data:SegmentSupplementIn,user=Depends(require('supervisor'))):
    with connect() as conn:
        if not lock_wave(conn,wave_id): raise HTTPException(404,'航班波次不存在')
        wave,readings,segments=belt_series.load_wave_segments(conn,wave_id)
        target=next((s for s in segments if s['fingerprint']==data.fingerprint),None)
        if target is None: raise HTTPException(409,'该跨段已因读数变化重组，请刷新后重新核对')
        if not target['needs_supplement']: raise HTTPException(409,'该时段可由相邻读数折算，不需要补数')
        note=' '.join(data.note.split())
        if len(note)<4: raise HTTPException(422,'请写明补数依据')
        belt_series.supersede_overlapping(conn,wave_id,target['belt_code'],target['fingerprint'],target['start_at'],target['end_at'])
        row=conn.execute("INSERT INTO belt_segment_supplements(wave_id,belt_code,fingerprint,start_at,end_at,bag_count,note,verified_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(wave_id,belt_code,fingerprint) DO UPDATE SET bag_count=EXCLUDED.bag_count,note=EXCLUDED.note,status='verified',verified_by=EXCLUDED.verified_by,verified_at=now(),superseded_at=NULL RETURNING *",(wave_id,target['belt_code'],target['fingerprint'],target['start_at'],target['end_at'],data.bag_count,note,user['id'])).fetchone()
        _,refreshed,_=refresh_wave_series(conn,wave_id)
        audit(conn,user['id'],'supplement','belt_segment',None,{'wave_id':wave_id,'fingerprint':data.fingerprint,'bag_count':data.bag_count}); conn.commit()
    for forecast_id in refreshed:
        try: r.rpush('airport:forecast',str(forecast_id))
        except Exception: pass
    return row
@app.get('/forecasts')
def forecasts(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT f.*,w.code,w.terminal FROM peak_forecasts f JOIN flight_waves w ON w.id=f.wave_id ORDER BY f.id DESC').fetchall())
@app.post('/forecasts',status_code=202)
def create_forecast(data:ForecastIn,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        wave=conn.execute('SELECT * FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'航班波次不存在')
        if wave['cancellation_state']=='cancelled_unmarked':
            raise HTTPException(409,'航班取消或改降尚未标记，禁止发布峰值预测')
        _,readings,segments=belt_series.load_wave_segments(conn,data.wave_id)
        pending=belt_series.pending_segments(segments)
        if pending:
            raise HTTPException(409,'存在换表跨段待主管补数，暂停该波次预测')
        points=belt_series.forecast_points(segments)
        if len(points)<8: raise HTTPException(409,'折算后的有效计数点不足 8 个，无法进行时序分解')
        if any(p['counted_at'] is None or p['bag_count'] is None for p in points): raise HTTPException(409,'折算后的计数序列不完整')
        row=conn.execute("INSERT INTO peak_forecasts(wave_id,status,window_size,created_by,parameters) VALUES (%s,'queued',%s,%s,%s::jsonb) RETURNING *",(data.wave_id,data.window_size,user['id'],json.dumps({'input_fingerprints':belt_series.ready_fingerprints(segments),'input_segments':belt_series.serialise_segments(segments)}))).fetchone(); audit(conn,user['id'],'queue','peak_forecast',row['id'],{'method':'STL + rolling linear','segments':len(segments)}); conn.commit()
    try: r.rpush('airport:forecast',str(row['id']))
    except Exception: pass
    return row
@app.post('/forecasts/{forecast_id}/publish')
def publish(forecast_id:int,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        f=conn.execute('SELECT * FROM peak_forecasts WHERE id=%s',(forecast_id,)).fetchone()
        if not f or f['status'] not in ('ready','published'): raise HTTPException(409,'预测尚未就绪')
        _,readings,segments=belt_series.load_wave_segments(conn,f['wave_id'])
        if belt_series.pending_segments(segments): raise HTTPException(409,'存在换表跨段待主管补数，暂停该波次预测')
        if sorted(f['parameters'].get('input_fingerprints',[]))!=belt_series.ready_fingerprints(segments):
            raise HTTPException(409,'计数或换表跨段已变化，预测结果过期，请重新生成预测')
        shift=conn.execute('SELECT * FROM staff_shifts WHERE wave_id=%s ORDER BY id DESC LIMIT 1',(f['wave_id'],)).fetchone()
        if not shift: raise HTTPException(409,'发布前必须先建立人员班次')
        if float(f['peak_bags'])>shift['capacity']*10 and shift['status']!='confirmed':
            conn.execute("UPDATE staff_shifts SET status='needs_confirmation' WHERE id=%s",(shift['id'],)); conn.commit(); raise HTTPException(409,'预测峰值超过当前岗位容量，需拆分班次并由值班经理确认')
        row=conn.execute("UPDATE peak_forecasts SET status='published',updated_at=now() WHERE id=%s RETURNING *",(forecast_id,)).fetchone(); audit(conn,user['id'],'publish','peak_forecast',forecast_id,{}); conn.commit(); return row
@app.post('/forecasts/{forecast_id}/retry',status_code=202)
def retry(forecast_id:int,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        row=conn.execute("UPDATE peak_forecasts SET status='queued',failure_reason=NULL WHERE id=%s AND status='failed' RETURNING *",(forecast_id,)).fetchone()
        if not row: raise HTTPException(409,'只有失败预测可以重试')
        conn.commit()
    r.rpush('airport:forecast',str(forecast_id)); return row
@app.get('/duty')
def duty(user=Depends(current_user)):
    """Duty-desk view: pending meter-swap periods and the counts feeding the forecast."""
    with connect() as conn:
        waves=conn.execute('SELECT id,code,terminal,status FROM flight_waves ORDER BY arrival_start,code').fetchall()
        output=[]
        for wave in waves:
            _,readings,segments=belt_series.load_wave_segments(conn,wave['id'])
            latest=conn.execute("SELECT * FROM peak_forecasts WHERE wave_id=%s AND status IN ('ready','published','blocked','queued','running') ORDER BY id DESC LIMIT 1",(wave['id'],)).fetchone()
            pending=[belt_series.public_pending(s) for s in belt_series.pending_segments(segments)]
            points=belt_series.forecast_points(segments)
            output.append({
                'wave_id':wave['id'],'code':wave['code'],'terminal':wave['terminal'],'wave_status':wave['status'],
                'pending':pending,
                'forecast_points':[{'counted_at':p['counted_at'],'bag_count':p['bag_count']} for p in points],
                'forecast_target_points':len(points),
                'adopted':None if latest is None else {
                    'forecast_id':latest['id'],'status':latest['status'],'peak_bags':latest['peak_bags'],
                    'forecast_at':latest['forecast_at'],'window_size':latest['window_size'],
                    'segment_count':len(latest['parameters'].get('input_segments',[])),
                    'segments':latest['parameters'].get('input_segments',[]),
                    'stale':sorted(latest['parameters'].get('input_fingerprints',[]))!=belt_series.ready_fingerprints(segments),
                },
            })
        return {'waves':output,'paused':sum(1 for w in output if w['pending'])}
@app.get('/shifts')
def shifts(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT s.*,w.code FROM staff_shifts s JOIN flight_waves w ON w.id=s.wave_id ORDER BY s.start_at').fetchall())
@app.post('/shifts',status_code=201)
def create_shift(data:ShiftIn,user=Depends(require('operator','supervisor'))):
    if data.end_at<=data.start_at: raise HTTPException(400,'班次结束时间必须晚于开始时间')
    with connect() as conn:
        wave=conn.execute('SELECT id FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'波次不存在')
        status='needs_confirmation' if data.planned_staff>data.capacity else 'draft'
        row=conn.execute('INSERT INTO staff_shifts(shift_code,wave_id,start_at,end_at,planned_staff,capacity,status) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *',(data.shift_code,data.wave_id,data.start_at,data.end_at,data.planned_staff,data.capacity,status)).fetchone(); audit(conn,user['id'],'create','staff_shift',row['id'],{'status':status}); conn.commit(); return row
@app.post('/shifts/{shift_id}/confirm')
def confirm_shift(shift_id:int,data:ConfirmIn,user=Depends(require('supervisor'))):
    with connect() as conn:
        row=conn.execute("UPDATE staff_shifts SET status='confirmed',confirmation_note=%s,confirmed_by=%s,confirmed_at=now() WHERE id=%s RETURNING *",(data.note,user['id'],shift_id)).fetchone()
        if not row: raise HTTPException(404,'班次不存在')
        audit(conn,user['id'],'confirm','staff_shift',shift_id,{'note':data.note}); conn.commit(); return row
@app.get('/reviews')
def reviews(user=Depends(current_user)):
    with connect() as conn: return dump(conn.execute('SELECT r.*,w.code FROM congestion_reviews r JOIN flight_waves w ON w.id=r.wave_id ORDER BY r.id DESC').fetchall())
@app.post('/reviews',status_code=201)
def create_review(data:ReviewIn,user=Depends(require('operator','supervisor'))):
    with connect() as conn:
        wave=conn.execute('SELECT id FROM flight_waves WHERE id=%s',(data.wave_id,)).fetchone()
        if not wave: raise HTTPException(404,'波次不存在')
        row=conn.execute('INSERT INTO congestion_reviews(wave_id,forecast_id,observed_peak,queue_minutes,cause,action) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *',(data.wave_id,data.forecast_id,data.observed_peak,data.queue_minutes,data.cause,data.action)).fetchone(); audit(conn,user['id'],'create','congestion_review',row['id'],{}); conn.commit(); return row
@app.post('/reviews/{review_id}/close')
def close_review(review_id:int,user=Depends(require('supervisor'))):
    with connect() as conn:
        row=conn.execute("UPDATE congestion_reviews SET status='closed',reviewed_by=%s,reviewed_at=now() WHERE id=%s RETURNING *",(user['id'],review_id)).fetchone()
        if not row: raise HTTPException(404,'复盘单不存在')
        audit(conn,user['id'],'close','congestion_review',review_id,{}); conn.commit(); return row
