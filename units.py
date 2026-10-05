from datetime import datetime

KM_TO_MI = 0.621371


def legacy_record(row: dict) -> dict:
    """SI session row -> the pre-SQLite history dict shape the UI, CSV, and Apple Health export consume.
    Speeds are derived from distance/moving time, as the old record builder did; converting the stored
    avg_speed_mps instead drifts by 0.1 mph on some migrated rows."""
    start = datetime.fromisoformat(row["start_time"])
    end = datetime.fromisoformat(row["end_time"]) if row["end_time"] else None
    km = (row["distance_m"] or 0) / 1000
    moving_s = row["moving_s"] or 0
    kmh = km / (max(moving_s, 1) / 3600)
    return {
        "id": row["id"],
        "date": start.strftime("%Y-%m-%d"),
        "start_time": start.strftime("%H:%M:%S"),
        "end_time": end.strftime("%H:%M:%S") if end else None,
        "duration_seconds": int(moving_s),
        "distance_km": round(km, 3),
        "distance_mi": round(km * KM_TO_MI, 3),
        "steps": row["steps"] or 0,
        "calories": round(row["calories_kcal"] or 0),
        "avg_speed_kmh": round(kmh, 1),
        "avg_speed_mph": round(kmh * KM_TO_MI, 1),
        "health_logged": bool(row["health_logged"]),
        "has_samples": row["has_samples"],
    }
