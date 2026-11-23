"""Turn belt-meter readings into per-period piece counts.

Baggage belts report *cumulative* register values.  Feeding those values
straight into the forecast counts every earlier period again, so the forecast
must be built from the difference between adjacent readings of the same meter.

Rules implemented here:

* ``reading_kind='cumulative'`` readings are differenced against the previous
  accepted reading on the same belt, as long as both readings belong to the
  same meter (``同一块表按相邻读数折算``).
* A reading marked ``meter_swap`` starts a new register.  The period between
  the last reading of the old meter and the first reading of the new meter
  cannot be derived (``换表跨段``) and has to be supplied by a supervisor.
* ``reading_kind='interval'`` readings are already single-period counts.  When
  one falls inside a derived period it wins over the difference
  (``同一时段已有单段计数就用原数``).
* Readings are always ordered by sampling time, so late records are inserted
  back in their proper place.  Every derived period carries a fingerprint made
  from its boundary readings; if a late record moves a boundary the fingerprint
  changes and an earlier supervisor supplement no longer applies
  (``原补数得重新核对``).

The module is deliberately free of database imports so the derivation can be
unit-tested on plain dictionaries.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

FALLBACK_PERIOD = timedelta(minutes=10)

CUMULATIVE = "cumulative"


def iso(value):
    return value.isoformat() if value is not None else ""


def segment_fingerprint(wave_id, belt_code, start_at, end_at, start_reading_id,
                        end_reading_id, meter_from, meter_to):
    """Stable identity of one derived period.

    Boundary reading ids *and* timestamps are part of the fingerprint: a late
    record that moves either end of the period therefore produces a new
    identity and invalidates any supplement recorded against the old period.
    """

    payload = "|".join([
        str(wave_id), str(belt_code), iso(start_at), iso(end_at),
        str(start_reading_id), str(end_reading_id),
        meter_from or "", meter_to or "",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _field(row, name, default=None):
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _latest_overlapping(supplements, belt_code, start_at, end_at, exclude):
    """Most recent supplement for an overlapping, now-changed period."""

    candidates = [
        row for row in supplements
        if _field(row, "belt_code") == belt_code
        and _field(row, "fingerprint") != exclude
        and _field(row, "start_at") < end_at
        and _field(row, "end_at") > start_at
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (_field(row, "verified_at") or _field(row, "created_at"), _field(row, "id") or 0))


def _supplement_view(row):
    if row is None:
        return None
    return {
        "bag_count": _field(row, "bag_count"),
        "note": _field(row, "note"),
        "status": _field(row, "status"),
        "verified_at": _field(row, "verified_at"),
        "start_at": _field(row, "start_at"),
        "end_at": _field(row, "end_at"),
    }


def _overlay(intervals, start_at, end_at):
    """Single-period count recorded inside ``(start_at, end_at]``."""

    candidates = [row for row in intervals if start_at < _field(row, "counted_at") <= end_at]
    if not candidates:
        return None
    exact = [row for row in candidates if _field(row, "counted_at") == end_at]
    pool = exact or candidates
    return max(pool, key=lambda row: (_field(row, "counted_at"), _field(row, "id") or 0))


def _meter_chain(cumulative):
    """Assign an effective meter to every cumulative reading."""

    first = cumulative[0]
    base = _field(first, "scan_source") or "MTR"
    chain = [{
        "reading": first,
        "meter": _field(first, "meter_code") or base,
        "previous_meter": None,
        "boundary": False,
    }]
    swaps = 0
    for current in cumulative[1:]:
        previous = chain[-1]
        explicit = _field(current, "meter_code")
        marked = bool(_field(current, "meter_swap"))
        boundary = marked
        if explicit is not None:
            if explicit != previous["meter"]:
                boundary = True
            meter = explicit
        else:
            meter = previous["meter"]
        if boundary:
            swaps += 1
            if explicit is None:
                meter = f"{base}-S{swaps}"
        chain.append({
            "reading": current,
            "meter": meter,
            "previous_meter": previous["meter"],
            "boundary": boundary,
        })
    return chain


def _derive_belt(wave_id, belt_code, readings, arrival_start, supplements):
    accepted = [row for row in readings if _field(row, "status") == "accepted"]
    cumulative = sorted(
        (row for row in accepted if _field(row, "reading_kind") == CUMULATIVE),
        key=lambda row: (_field(row, "counted_at"), _field(row, "id") or 0),
    )
    intervals = sorted(
        (row for row in accepted if _field(row, "reading_kind") != CUMULATIVE),
        key=lambda row: (_field(row, "counted_at"), _field(row, "id") or 0),
    )
    by_fingerprint = {
        _field(row, "fingerprint"): row
        for row in supplements
        if _field(row, "belt_code") == belt_code
    }

    segments = []

    # Differencing needs at least two cumulative readings.  With only one (or
    # none) fall back to single-period readings so those are not lost.
    if len(cumulative) >= 2:
        chain = _meter_chain(cumulative)
        for previous, current in zip(chain, chain[1:]):
            start_reading = previous["reading"]
            end_reading = current["reading"]
            start_at = _field(start_reading, "counted_at")
            end_at = _field(end_reading, "counted_at")
            if end_at <= start_at:
                continue
            meter_from = previous["meter"]
            meter_to = current["meter"]
            fingerprint = segment_fingerprint(
                wave_id, belt_code, start_at, end_at,
                _field(start_reading, "id"), _field(end_reading, "id"),
                meter_from, meter_to,
            )
            overlay = _overlay(intervals, start_at, end_at)
            straddles = bool(current["boundary"])
            supplement = by_fingerprint.get(fingerprint)
            segment = {
                "fingerprint": fingerprint,
                "wave_id": wave_id,
                "belt_code": belt_code,
                "start_at": start_at,
                "end_at": end_at,
                "start_reading_id": _field(start_reading, "id"),
                "end_reading_id": _field(end_reading, "id"),
                "meter_from": meter_from,
                "meter_to": meter_to,
                "meter_code": meter_to if meter_from == meter_to else f"{meter_from}->{meter_to}",
                "straddles_meter_swap": straddles or meter_from != meter_to,
                "needs_supplement": False,
                "status": "ready",
                "source": "derived",
                "kind": "derived",
                "bag_count": None,
                "supplement": None,
                "previous_supplement": None,
            }
            if overlay is not None:
                # A single-period count already exists for this period; keep
                # the original number instead of the cumulative difference.
                segment.update(kind="single_segment", source="single_segment",
                               bag_count=_field(overlay, "bag_count"), status="ready")
            elif straddles:
                segment.update(kind="meter_swap", needs_supplement=True)
                if supplement is not None and _field(supplement, "status") == "verified":
                    segment.update(source="supplement", status="ready",
                                   bag_count=_field(supplement, "bag_count"),
                                   supplement=_supplement_view(supplement))
                else:
                    segment.update(source=None, status="pending")
            else:
                delta = _field(end_reading, "bag_count") - _field(start_reading, "bag_count")
                if delta < 0:
                    segment.update(kind="unstable_delta", needs_supplement=True)
                    if supplement is not None and _field(supplement, "status") == "verified":
                        segment.update(source="supplement", status="ready",
                                       bag_count=_field(supplement, "bag_count"),
                                       supplement=_supplement_view(supplement))
                    else:
                        segment.update(source=None, status="pending")
                else:
                    segment.update(source="derived", status="ready", bag_count=delta)
            if segment["status"] == "pending":
                old = _latest_overlapping(supplements, belt_code, start_at, end_at, fingerprint)
                segment["previous_supplement"] = _supplement_view(old)
            segments.append(segment)

    elif intervals:
        # Legacy single-period readings only.  Every reading is a count for the
        # span since the previous reading; the first one starts at the wave
        # window (or one fallback period earlier when sampling began early).
        first_at = _field(intervals[0], "counted_at")
        start_at = arrival_start if arrival_start is not None and arrival_start < first_at else first_at - FALLBACK_PERIOD
        start_reading_id = None
        for reading in intervals:
            end_at = _field(reading, "counted_at")
            if end_at <= start_at:
                start_at = end_at
                continue
            fingerprint = segment_fingerprint(
                wave_id, belt_code, start_at, end_at,
                start_reading_id, _field(reading, "id"), None, None,
            )
            segments.append({
                "fingerprint": fingerprint,
                "wave_id": wave_id,
                "belt_code": belt_code,
                "start_at": start_at,
                "end_at": end_at,
                "start_reading_id": start_reading_id,
                "end_reading_id": _field(reading, "id"),
                "meter_from": None,
                "meter_to": None,
                "meter_code": None,
                "straddles_meter_swap": False,
                "needs_supplement": False,
                "status": "ready",
                "source": "single_segment",
                "kind": "single_segment",
                "bag_count": _field(reading, "bag_count"),
                "supplement": None,
                "previous_supplement": None,
            })
            start_at = end_at
            start_reading_id = _field(reading, "id")

    return segments


def derive_wave_segments(wave, readings, supplements):
    """Derive every period for a wave from its readings and supplements."""

    wave_id = _field(wave, "id")
    arrival_start = _field(wave, "arrival_start")
    by_belt = {}
    for reading in readings:
        by_belt.setdefault(_field(reading, "belt_code"), []).append(reading)

    segments = []
    for belt_code in sorted(by_belt):
        segments.extend(_derive_belt(wave_id, belt_code, by_belt[belt_code], arrival_start, supplements))
    segments.sort(key=lambda segment: (segment["end_at"], segment["belt_code"], segment["fingerprint"]))
    return segments


def pending_segments(segments):
    return [segment for segment in segments if segment["status"] == "pending"]


def forecast_points(segments):
    """Aggregate ready periods into one ordered series for the algorithm."""

    totals = {}
    for segment in segments:
        if segment["status"] != "ready" or segment["bag_count"] is None:
            continue
        key = segment["end_at"]
        totals[key] = totals.get(key, 0) + int(segment["bag_count"])
    return [{"counted_at": at, "bag_count": totals[at]} for at in sorted(totals)]


def serialise_segments(segments):
    """JSON-safe copy of the segments used in a forecast snapshot."""

    output = []
    for segment in segments:
        if segment["status"] != "ready" or segment["bag_count"] is None:
            continue
        output.append({
            "fingerprint": segment["fingerprint"],
            "belt_code": segment["belt_code"],
            "start_at": iso(segment["start_at"]),
            "end_at": iso(segment["end_at"]),
            "kind": segment["kind"],
            "source": segment["source"],
            "bag_count": int(segment["bag_count"]),
            "straddles_meter_swap": segment["straddles_meter_swap"],
            "supplement_note": (segment["supplement"] or {}).get("note"),
        })
    return output


def ready_fingerprints(segments):
    return sorted(segment["fingerprint"] for segment in segments if segment["status"] == "ready")


def public_segment(segment):
    """Serialise a segment (including pending ones) for the duty UI."""

    supplement = segment.get("supplement")
    previous = segment.get("previous_supplement")
    return {
        "fingerprint": segment["fingerprint"],
        "belt_code": segment["belt_code"],
        "start_at": iso(segment["start_at"]),
        "end_at": iso(segment["end_at"]),
        "meter_from": segment["meter_from"],
        "meter_to": segment["meter_to"],
        "meter_code": segment["meter_code"],
        "kind": segment["kind"],
        "source": segment["source"],
        "status": segment["status"],
        "needs_supplement": segment["needs_supplement"],
        "straddles_meter_swap": segment["straddles_meter_swap"],
        "bag_count": int(segment["bag_count"]) if segment["bag_count"] is not None else None,
        "supplement": None if supplement is None else {
            "bag_count": supplement["bag_count"], "note": supplement["note"],
            "status": supplement["status"], "verified_at": iso(supplement["verified_at"]),
        },
        "previous_supplement": None if previous is None else {
            "bag_count": previous["bag_count"], "note": previous["note"],
            "status": previous["status"], "verified_at": iso(previous["verified_at"]),
            "start_at": iso(previous["start_at"]), "end_at": iso(previous["end_at"]),
        },
    }


def public_pending(segment):
    data = public_segment(segment)
    return {
        "fingerprint": data["fingerprint"],
        "belt_code": data["belt_code"],
        "start_at": data["start_at"],
        "end_at": data["end_at"],
        "meter_from": data["meter_from"],
        "meter_to": data["meter_to"],
        "kind": data["kind"],
        "previous_supplement": data["previous_supplement"],
    }


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def load_wave_segments(conn, wave_id):
    """Read one wave and return ``(wave, readings, segments)``."""

    wave = conn.execute("SELECT * FROM flight_waves WHERE id=%s", (wave_id,)).fetchone()
    if not wave:
        return None, [], []
    readings = conn.execute(
        "SELECT * FROM belt_counts WHERE wave_id=%s ORDER BY belt_code,counted_at,id",
        (wave_id,),
    ).fetchall()
    supplements = conn.execute(
        "SELECT * FROM belt_segment_supplements WHERE wave_id=%s ORDER BY id",
        (wave_id,),
    ).fetchall()
    return wave, readings, derive_wave_segments(wave, readings, supplements)


def supersede_stale_supplements(conn, wave_id, segments):
    """Supplements whose period no longer exists must be re-verified."""

    live = [segment["fingerprint"] for segment in segments]
    if live:
        conn.execute(
            """
            UPDATE belt_segment_supplements
               SET status='superseded', superseded_at=now()
             WHERE wave_id=%s AND status='verified' AND NOT (fingerprint = ANY(%s::text[]))
            """,
            (wave_id, live),
        )
    else:
        conn.execute(
            "UPDATE belt_segment_supplements SET status='superseded', superseded_at=now() "
            "WHERE wave_id=%s AND status='verified'",
            (wave_id,),
        )


def supersede_overlapping(conn, wave_id, belt_code, fingerprint, start_at, end_at):
    conn.execute(
        """
        UPDATE belt_segment_supplements
           SET status='superseded', superseded_at=now()
         WHERE wave_id=%s AND belt_code=%s AND fingerprint<>%s AND status='verified'
           AND start_at < %s AND end_at > %s
        """,
        (wave_id, belt_code, fingerprint, end_at, start_at),
    )
