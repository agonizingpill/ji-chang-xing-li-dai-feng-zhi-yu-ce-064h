import json, os, time
from pathlib import Path
import redis
from .db import connect, migrate, wait_for_db
from .algorithm import stl_rolling_forecast
from . import belt_series

REDIS_URL=os.environ['REDIS_URL']; QUEUE='airport:forecast'
r=redis.Redis.from_url(REDIS_URL, decode_responses=True)

def process(forecast_id):
    with connect() as conn:
        row=conn.execute('SELECT * FROM peak_forecasts WHERE id=%s FOR UPDATE',(forecast_id,)).fetchone()
        if not row or row['status'] not in ('queued','failed'): return
        conn.execute("UPDATE peak_forecasts SET status='running', attempts=attempts+1, updated_at=now() WHERE id=%s", (forecast_id,))
        wave,readings,segments=belt_series.load_wave_segments(conn,row['wave_id'])
        try:
            # Never mix periods from before and after a meter split: a pending
            # swap period blocks the forecast until a supervisor supplies it.
            if belt_series.pending_segments(segments):
                raise ValueError('存在换表跨段待主管补数，暂停该波次预测')
            points=belt_series.forecast_points(segments)
            result=stl_rolling_forecast(points, row['window_size'])
            parameters={
                'method':'STL decomposition + rolling linear regression','seed':1072,'period':result['period'],
                'input_fingerprints':belt_series.ready_fingerprints(segments),
                'input_segments':belt_series.serialise_segments(segments),
            }
            result['input_segments']=parameters['input_segments']
            conn.execute("UPDATE peak_forecasts SET status='ready', peak_bags=%s, forecast_at=now(), seasonal_strength=%s, trend_per_hour=%s, snapshot_hash=%s, parameters=%s::jsonb, result=%s::jsonb, updated_at=now() WHERE id=%s", (result['peak_bags'],result['seasonal_strength'],result['trend_per_hour'],result['snapshot_hash'],json.dumps(parameters),json.dumps(result),forecast_id))
            conn.execute("UPDATE flight_waves SET status='forecasted' WHERE id=%s", (row['wave_id'],))
        except Exception as exc:
            conn.execute("UPDATE peak_forecasts SET status='failed', failure_reason=%s, updated_at=now() WHERE id=%s", (str(exc), forecast_id))
        conn.commit()

def main():
    wait_for_db(); migrate(); Path('/shared/worker-heartbeat').touch()
    while True:
        Path('/shared/worker-heartbeat').touch()
        item=r.blpop(QUEUE, timeout=3)
        if item:
            try: process(int(item[1]))
            except Exception: time.sleep(1)

if __name__=='__main__': main()
