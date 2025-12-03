import os
import math
from datetime import datetime, timedelta
import pytz
import pg8000

DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]
DB_NAME = os.environ["DB_NAME"]


def landslide_probability(rainfall_mm: float) -> float:
    intercept = -13.7821
    coefficient = 0.4294
    z = intercept + coefficient * rainfall_mm
    return math.exp(z) / (1 + math.exp(z))


def landslide_risk(rainfall_mm: float) -> int:
    prob = landslide_probability(rainfall_mm)
    if prob <= 0.01:
        return 0
    elif prob <= 0.7:
        return 1
    elif prob > 0.7:
        return 2


def get_rainfall_last_3h(place_name: str) -> float:
    # Placeholder: replace with real HTTP request / API logic
    return 1.5


def get_places_from_event(event) -> list[str]:
    if isinstance(event, dict):
        if "place_name" in event and event["place_name"]:
            return [event["place_name"]]
        if "places" in event and isinstance(event["places"], list):
            return event["places"]
    return []


def get_place_id(place_name: str) -> int:
    place_ids = {"Craig": "AK91", "Kasaan": "AK182"}
    return place_ids.get(place_name, None)


def is_risk_elevated_from_previous(
    cursor, place_name: str, current_prob: float
) -> bool:
    sql = """
    SELECT risk_prob FROM precip_risk 
    WHERE place_name = %s 
    ORDER BY ts DESC 
    LIMIT 1
    """
    cursor.execute(sql, (place_name,))
    result = cursor.fetchone()

    if result is None:
        return False

    previous_prob = result[0]
    return current_prob > previous_prob


def lambda_handler(event, context):
    places_to_run = get_places_from_event(event)

    alaska_tz = pytz.timezone("US/Alaska")
    now = datetime.now(alaska_tz)
    ts = now

    expires_at = now + timedelta(hours=3)
    expires_at_str = expires_at

    conn = pg8000.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for place_name in places_to_run:
                rainfall_mm = get_rainfall_last_3h(place_name)

                prob = landslide_probability(rainfall_mm)
                risk = landslide_risk(rainfall_mm)

                risk_is_elevated = is_risk_elevated_from_previous(cur, place_name, prob)

                precip_inches = rainfall_mm / 25.4

                # Place holders for 24hr, 2days, 3days rainfall and risk calculations
                precip24hr = rainfall_mm * 8
                risk24hr = landslide_risk(precip24hr)
                precip2days = rainfall_mm * 16
                risk2days = landslide_risk(precip2days)
                precip3days = rainfall_mm * 24
                risk3days = landslide_risk(precip3days)

                place_id = get_place_id(place_name)

                sql = """
                INSERT INTO precip_risk (
                  ts, place_name, precip, precip_inches, hour,
                  risk_prob, risk_level, risk_is_elevated_from_previous, 
                  precip24hr, risk24hr, precip2days, risk2days, precip3days, risk3days, expires_at, place_id
                ) VALUES (
                  %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """
                cur.execute(
                    sql,
                    (
                        ts,
                        place_name,
                        rainfall_mm,
                        precip_inches,
                        now.strftime("%I%p"),
                        prob,
                        risk,
                        risk_is_elevated,
                        precip24hr,
                        risk24hr,
                        precip2days,
                        risk2days,
                        precip3days,
                        risk3days,
                        expires_at_str,
                        place_id,
                    ),
                )

        return {
            "status": "ok",
            "places_processed": places_to_run,
            "timestamp": ts.isoformat(),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    test_event = {"places": ["Kasaan", "Craig", "Anchorage"]}
    print(lambda_handler(test_event, None))
