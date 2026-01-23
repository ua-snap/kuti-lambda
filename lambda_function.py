import os
import math
import json
import logging
from datetime import datetime, timedelta
import cfgrib
import xarray as xr
import pytz
import pg8000
import boto3
import requests
from ecmwf.opendata import Client

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Database configuration
DB_HOST = os.environ.get("DB_HOST")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_NAME = os.environ.get("DB_NAME")

# API credentials
SYNOPTIC_API_TOKEN = os.environ.get("SYNOPTIC_API_TOKEN")

# S3 configuration
S3_BUCKET_NAME = "kuti-forecast-data"
S3_CACHE_PREFIX = os.environ.get("S3_CACHE_PREFIX", "ecmwf-cache")

# Configurable parameters
INTENSITY_DURATION = int(os.environ.get("INTENSITY_DURATION", 3))  # hours
ANTECEDENT_PERIOD = int(os.environ.get("ANTECEDENT_PERIOD", 24))  # hours

# Location coordinates (lat, lon) for nearest grid cell lookup
LOCATIONS = {
    "Craig": {"lat": 55.48, "lon": -133.15, "gauge_id": "CSMA2"},
    "Kasaan": {"lat": 55.54, "lon": -132.40, "gauge_id": "SMKAS"},
}

alaska_tz = pytz.timezone("US/Alaska")

s3_client = boto3.client("s3") if S3_BUCKET_NAME else None


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


def get_gauge_precipitation(place_name: str, alaska_tz):
    """
    Retrieve real-time precipitation data from Synoptic API for the given location.
    Returns dict with intensity and antecedent values, or None if data unavailable.
    """
    if not SYNOPTIC_API_TOKEN:
        logger.warning(
            f"SYNOPTIC_API_TOKEN not configured, skipping gauge data for {place_name}"
        )
        return None

    location_info = LOCATIONS.get(place_name)

    gauge_id = location_info["gauge_id"]

    return {
        "intensity_mm": 0.5,
        "antecedent_mm": 0.5,
        "gauge_id": gauge_id,
    }

    # TODO: Figure out what needs to come out of Synoptic when I have
    # access to the API.
    # Calculate time window: need max antecedent period + intensity duration
    lookback_hours = ANTECEDENT_PERIOD + INTENSITY_DURATION

    end_time = datetime.now(alaska_tz)
    start_time = end_time - timedelta(hours=lookback_hours)

    # Synoptic API timeseries endpoint
    url = "https://api.synopticdata.com/v2/stations/timeseries"
    params = {
        "token": SYNOPTIC_API_TOKEN,
        "stid": gauge_id,
        "network": "293",
        "start": start_time.strftime("%Y%m%d%H%M"),
        "end": end_time.strftime("%Y%m%d%H%M"),
        "vars": "precip_accum",
        "units": "metric",
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"Synoptic API response: {data}")

        if data.get("SUMMARY", {}).get("RESPONSE_CODE") != 1:
            logger.error(
                f"Synoptic API error for {gauge_id}: {data.get('SUMMARY', {}).get('RESPONSE_MESSAGE')}"
            )
            return None

        stations = data.get("STATION", [])
        if not stations:
            logger.warning(f"No station data returned for {gauge_id}")
            return None

        observations = stations[0].get("OBSERVATIONS", {})
        precip_data = observations.get("precip_accum_set_1", [])
        timestamps = observations.get("date_time", [])

        if not precip_data or not timestamps:
            logger.warning(f"No precipitation data available for {gauge_id}")
            return None

        # Convert timestamps to datetime objects and pair with precipitation values
        time_series = []
        for ts_str, precip in zip(timestamps, precip_data):
            if precip is not None:
                dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=pytz.UTC
                )
                dt_alaska = dt.astimezone(alaska_tz)
                time_series.append((dt_alaska, float(precip)))

        if not time_series:
            logger.warning(f"No valid precipitation data for {gauge_id}")
            return None

        # Calculate current intensity (last INTENSITY_DURATION hours)
        intensity_start = end_time - timedelta(hours=INTENSITY_DURATION)
        intensity_data = [p for dt, p in time_series if dt >= intensity_start]
        current_intensity = sum(intensity_data) if intensity_data else 0.0

        # Calculate antecedent periods
        antecedent_values = {}
        for period_hr in ANTECEDENT_PERIODS:
            antecedent_start = intensity_start - timedelta(hours=period_hr)
            antecedent_data = [
                p for dt, p in time_series if antecedent_start <= dt < intensity_start
            ]
            antecedent_values[period_hr] = (
                sum(antecedent_data) if antecedent_data else 0.0
            )

        return {
            "intensity_mm": current_intensity,
            "antecedent_24hr_mm": antecedent_values.get(24, 0.0),
            "antecedent_48hr_mm": antecedent_values.get(48, 0.0),
            "antecedent_72hr_mm": antecedent_values.get(72, 0.0),
            "gauge_id": gauge_id,
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching Synoptic data for {gauge_id}: {e}")
        return None
    except Exception as e:
        logger.exception(f"Unexpected error processing Synoptic data for {gauge_id}")
        return None


def check_s3_forecast_cache(forecast_time):
    """Check if ECMWF forecast is cached in S3 for the given forecast time."""
    if not s3_client or not S3_BUCKET_NAME:
        return None

    key = f"{S3_CACHE_PREFIX}/forecast/{forecast_time.strftime('%Y%m%d-%H')}Z.json"

    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)
        cached_data = json.loads(response["Body"].read())
        logger.info(f"Using cached forecast from S3: {key}")
        return cached_data
    except s3_client.exceptions.NoSuchKey:
        logger.info(f"No cached forecast found in S3: {key}")
        return None
    except Exception as e:
        logger.error(f"Error reading S3 cache: {e}")
        return None


def save_forecast_to_s3(forecast_time, forecast_data):
    """
    Save parsed forecast data to S3 cache. Data is generated twice
    per day at midnight and noon, so instead of waiting the full
    download time from the ECMWF API, we cache the results for the
    12 hour interval that we can utilize this data.
    """

    key = f"{S3_CACHE_PREFIX}/forecast/{forecast_time.strftime('%Y%m%d-%H')}Z.json"

    try:
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=key,
            Body=json.dumps(forecast_data),
            ContentType="application/json",
        )
        logger.info(f"Saved forecast to S3 cache: {key}")
    except Exception as e:
        logger.error(f"Error saving to S3 cache: {e}")


def get_forecast_precipitation():
    """
    Retrieve 72-hour ECMWF Open Data forecast precipitation for Craig and Kasaan.
    Returns dict with Craig and Kasaan's data or None if unavailable.
    """

    # The forecast update interval is every 12 hours, so we check to see whether
    # .a new forecast is available or not.
    now = datetime.now(pytz.UTC)
    if now.hour >= 12:
        forecast_time = now.replace(hour=12, minute=0, second=0, microsecond=0)
    else:
        forecast_time = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # We save the forecast to S3 to save time on subsequent requests
    # Check for the most recent forecast before downloading it.
    cached_forecast = check_s3_forecast_cache(forecast_time)
    if cached_forecast:
        return cached_forecast

    # If no forecast is cached, we must get the latest one from ECMWF.
    try:
        logger.info(
            f"Requesting ECMWF Open Data forecast for {forecast_time.strftime('%Y-%m-%d %HZ')}"
        )

        client = Client(source="ecmwf")

        # Temporary location for downloaded GRIB file.
        grib_file = f"/tmp/ecmwf_forecast_{forecast_time.strftime('%Y%m%d%H')}.grib2"

        # Client usage documentation: https://github.com/ecmwf/ecmwf-opendata
        client.retrieve(
            date=forecast_time,
            time=forecast_time.hour,
            step=list(range(INTENSITY_DURATION, 75, INTENSITY_DURATION)),
            stream="oper",  # Operational forecast
            type="fc",  # Forecast
            param="tp",  # Total precipitation
            target=grib_file,
        )

        ds = xr.open_dataset(grib_file, engine="cfgrib")

        forecast_data = {}

        for place_name, location_info in LOCATIONS.items():
            lat = location_info["lat"]
            lon = location_info["lon"]

            # Pull the closest data location
            precip_values = ds["tp"].sel(latitude=lat, longitude=lon, method="nearest")

            # Extract all of the times and their total precipitation
            all_times = []
            for time_idx in range(len(precip_values.step)):
                # This converts the step value from nanoseconds to hours
                step_hours = int(precip_values.step[time_idx].values / 3600000000000)
                ts = forecast_time + timedelta(hours=step_hours)
                ts_alaska = ts.astimezone(alaska_tz)

                # Total precipitation is returned in meters
                # We will be converting those values to millimeters.
                precip_m = float(precip_values.isel(step=time_idx).values)
                precip_mm = precip_m * 1000

                all_times.append(
                    {"timestamp": ts_alaska.isoformat(), "precip_mm": precip_mm}
                )

            forecast_data[place_name] = all_times
        ds.close()

        # Save forecast data to S3 for reference within the 12 hour
        # window between model run outputs.
        save_forecast_to_s3(forecast_time, forecast_data)

        return forecast_data

    except Exception as e:
        logger.exception(f"Error retrieving ECMWF Open Data forecast: {e}")
        return None


def calculate_forecast_windows(forecast_time_series, gauge_24hr_precip=None):
    """
    Calculate intensity windows for the full 72-hour forecast period.
    For each INTENSITY_DURATION period (e.g., 3 hours), calculate:
    - intensity_mm: rainfall in that discrete 3-hour period (single timestamp)
    - antecedent_mm: sum of previous INTENSITY_DURATION periods within ANTECEDENT_PERIOD lookback

    Uses gauge 24hr precipitation for early windows when available, then transitions to forecast-only.
    Returns single array with all forecast timesteps.
    Risk is calculated from intensity only.
    """
    if not forecast_time_series:
        return []

    # Convert forecast ISO timestamp strings to datetime objects
    forecast_data = []
    for item in forecast_time_series:
        dt = datetime.fromisoformat(item["timestamp"])
        forecast_data.append((dt, item["precip_mm"]))

    # Sort by time
    forecast_data.sort(key=lambda x: x[0])
    forecast_start = forecast_data[0][0]

    # Calculate how many timesteps fit in antecedent period
    timesteps_in_antecedent = ANTECEDENT_PERIOD // INTENSITY_DURATION

    windows = []

    # Process ALL timesteps
    for i in range(len(forecast_data)):
        window_end_time = forecast_data[i][0]

        # Intensity is ONLY the current 3-hour period (current timestamp)
        intensity_mm = forecast_data[i][1]

        # Calculate antecedent: sum of previous timesteps within lookback window
        if i < timesteps_in_antecedent:
            # Early windows: not enough forecast history
            # Use gauge 24hr precipitation if available, otherwise sum what we have
            if gauge_24hr_precip is not None:
                antecedent_mm = gauge_24hr_precip + sum(
                    precip for dt, precip in forecast_data[:i]
                )
            else:
                # Sum all previous forecast timesteps (partial antecedent)
                antecedent_mm = sum(precip for dt, precip in forecast_data[:i])
        else:
            # Later windows: full antecedent period available in forecast
            # Sum previous timesteps within the lookback window
            lookback_start_index = i - timesteps_in_antecedent
            antecedent_mm = sum(
                precip for dt, precip in forecast_data[lookback_start_index:i]
            )

        # Calculate risk from intensity only
        risk_prob = landslide_probability(intensity_mm)
        risk_level = landslide_risk(intensity_mm)

        # Calculate forecast hour (hours from forecast start)
        forecast_hour = (i + 1) * INTENSITY_DURATION

        windows.append(
            {
                "timestamp": window_end_time.isoformat(),
                "forecast_hour": forecast_hour,
                "intensity_mm": round(intensity_mm, 2),
                "antecedent_mm": round(antecedent_mm, 2),
                "risk_prob": round(risk_prob, 2),
                "risk_level": risk_level,
            }
        )

    return windows


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


def lambda_handler(event, context):
    places_to_run = get_places_from_event(event)

    conn = pg8000.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, database=DB_NAME
    )
    conn.autocommit = True

    now = datetime.now(alaska_tz)
    ts = now

    expires_at = now + timedelta(hours=3)
    expires_at_str = expires_at.isoformat()

    logger.info("Retrieving ECMWF forecast data...")
    forecast_data = get_forecast_precipitation()
    forecast_retrieved_at = now if forecast_data else None

    try:
        with conn.cursor() as cur:
            for place_name in places_to_run:
                # TODO: Implement real-time gauge data retrieval
                # when Synoptic account is approved.
                logger.info(f"Processing {place_name}...")
                gauge_data = get_gauge_precipitation(place_name, alaska_tz)
                if gauge_data:
                    rainfall_mm = gauge_data["intensity_mm"]
                    gauge_id = gauge_data["gauge_id"]
                    realtime_antecedent = gauge_data["antecedent_mm"]
                else:
                    # No gauge data available
                    rainfall_mm = None
                    gauge_id = None
                    realtime_antecedent = None

                # Calculate risk from real-time intensity
                if rainfall_mm is not None:
                    prob = landslide_probability(rainfall_mm)
                    risk = landslide_risk(rainfall_mm)
                else:
                    prob = None
                    risk = None

                # Calculate forecast windows for this location
                forecast_windows = None

                if forecast_data and place_name in forecast_data:
                    current_location_forecast = forecast_data[place_name]

                    windows_array = calculate_forecast_windows(
                        current_location_forecast, realtime_antecedent
                    )

                    # Convert to JSON string for PSQL JSONB column
                    forecast_windows = json.dumps(windows_array)

                place_id = get_place_id(place_name)

                sql = """
                INSERT INTO kuti_test (
                  ts, place_name, place_id, expires_at,
                  rainfall_mm, risk_prob, risk_level,
                  gauge_id, realtime_antecedent_mm,
                  forecast_windows, forecast_retrieved_at
                ) VALUES (
                  %s, %s, %s, %s,
                  %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s
                )
                """
                cur.execute(
                    sql,
                    (
                        ts,
                        place_name,
                        place_id,
                        expires_at_str,
                        rainfall_mm,
                        prob,
                        risk,
                        gauge_id,
                        realtime_antecedent,
                        forecast_windows,
                        forecast_retrieved_at,
                    ),
                )

                logger.info(f"Successfully processed {place_name}")

        return {
            "status": "ok",
            "places_processed": places_to_run,
            "timestamp": ts.isoformat(),
            "forecast_available": forecast_data is not None,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    test_event = {"places": ["Craig", "Kasaan"]}
    result = lambda_handler(test_event, None)
    logger.info(f"Lambda handler result: {result}")
