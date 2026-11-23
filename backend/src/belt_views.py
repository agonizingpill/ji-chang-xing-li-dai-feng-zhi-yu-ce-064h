"""Business-verification providers that need the derived belt series.

The cumulative register values cannot be summed directly, so the views that
report bag volume or first-bag timing are computed from the derived periods in
:mod:`belt_series` instead of raw ``belt_counts`` rows.  Each provider returns
plain row tuples in the column order declared next to it.
"""

from .db import connect
from . import belt_series


def _waves_with_segments():
    with connect() as conn:
        waves = conn.execute("SELECT * FROM flight_waves ORDER BY arrival_start,code").fetchall()
        output = []
        for wave in waves:
            readings = conn.execute(
                "SELECT * FROM belt_counts WHERE wave_id=%s ORDER BY belt_code,counted_at,id",
                (wave['id'],),
            ).fetchall()
            supplements = conn.execute(
                "SELECT * FROM belt_segment_supplements WHERE wave_id=%s", (wave['id'],)
            ).fetchall()
            segments = belt_series.derive_wave_segments(wave, readings, supplements)
            output.append((wave, readings, segments))
        return output


def _fmt(value):
    return value.isoformat(sep=' ') if value is not None else None


def coverage_rows():
    rows = []
    for wave, readings, segments in _waves_with_segments():
        accepted = [row for row in readings if row['status'] == 'accepted']
        outside = sum(
            1 for row in accepted
            if row['counted_at'] < wave['arrival_start'] or row['counted_at'] > wave['arrival_end']
        )
        ready = [segment for segment in segments if segment['status'] == 'ready']
        pending = belt_series.pending_segments(segments)
        total = sum(int(segment['bag_count']) for segment in ready if segment['bag_count'] is not None)
        if pending:
            advice = '换表跨段待主管补数'
        elif not accepted:
            advice = '补录计数'
        elif outside:
            advice = '核对采样时段'
        else:
            advice = '窗口内'
        rows.append((wave['code'], wave['terminal'], wave['planned_bags'], len(accepted),
                     outside, total, advice))
    rows.sort(key=lambda row: (-row[4], row[0]))
    return rows


def first_bag_rows():
    rows = []
    for wave, readings, segments in _waves_with_segments():
        belts = sorted({row['belt_code'] for row in readings})
        for belt in belts:
            ready = [s for s in segments if s['belt_code'] == belt and s['status'] == 'ready'
                     and s['bag_count'] is not None and int(s['bag_count']) > 0]
            pending = [s for s in segments if s['belt_code'] == belt and s['status'] == 'pending']
            first_at = min((s['end_at'] for s in ready), default=None)
            if first_at is None:
                advice = '换表跨段待补数' if pending else '待录入首件'
                delta = None
            elif first_at < wave['arrival_start']:
                advice, delta = '核对提前与时钟差', round((first_at - wave['arrival_start']).total_seconds() / 60, 1)
            elif first_at > wave['arrival_end']:
                advice, delta = '首件超出波次窗口', round((first_at - wave['arrival_start']).total_seconds() / 60, 1)
            else:
                advice, delta = '窗口内已有件', round((first_at - wave['arrival_start']).total_seconds() / 60, 1)
            rows.append((wave['code'], belt, wave['arrival_start'], wave['arrival_end'],
                         first_at, delta, advice))
    rows.sort(key=lambda row: (row[0], row[1] or ''))
    return rows


def belt_handoff_rows():
    """Source records for the first-bag handoff casework."""

    rows = []
    for wave, readings, segments in _waves_with_segments():
        belts = sorted({row['belt_code'] for row in readings})
        for belt in belts:
            ready = [s for s in segments if s['belt_code'] == belt and s['status'] == 'ready'
                     and s['bag_count'] is not None and int(s['bag_count']) > 0]
            pending = [s for s in segments if s['belt_code'] == belt and s['status'] == 'pending']
            first_at = min((s['end_at'] for s in ready), default=None)
            delivered = first_at is not None and wave['arrival_start'] <= first_at <= wave['arrival_end']
            if delivered:
                state = 'delivered'
            elif pending:
                state = 'pending'
            else:
                state = 'late'
            summary = f"到达窗口 {wave['arrival_start']} 至 {wave['arrival_end']} · 首件 {_fmt(first_at) or '未登记'}"
            if pending:
                summary += ' · 换表跨段待补数'
            rows.append({
                'source_key': f"{wave['id']}/{belt}",
                'subject': f"{wave['code']} · {belt}",
                'source_summary': summary,
                'readiness': state,
            })
    return rows
