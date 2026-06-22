import os
import math
import json
import logging
from datetime import datetime, timedelta
import random
import cfgrib
import xarray as xr
import pytz
import pg8000
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
import requests
import time

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Add console handler for local testing
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

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

ANTECEDENT_PERIOD = int(os.environ.get("ANTECEDENT_PERIOD", 24))  # hours

# Location coordinates (lat, lon) for nearest grid cell lookup
LOCATIONS = {
    "Craig": {
        "lat": 55.48,
        "lon": -133.15,
        "gauge_id": "CRGA2",
        "var": "precip_accum_one_hour",
    },
    "Kasaan": {
        "lat": 55.54,
        "lon": -132.40,
        "gauge_id": "PWKA2",
        "var": "precip_interval",
    },
}

alaska_tz = pytz.timezone("US/Alaska")

s3_client = boto3.client("s3") if S3_BUCKET_NAME else None
ecmwf_s3_client = boto3.client(
    "s3", region_name="eu-central-1", config=Config(signature_version=UNSIGNED)
)
ecmwf_bucket = "ecmwf-forecasts"


def landslide_threshold(antecedent_mm: float) -> float:
    m = 14
    b = -0.05

    # If antecedent is zero or negative, return the base threshold value
    if antecedent_mm <= 0.0:
        return m

    # y = m * x ** b
    return m * antecedent_mm**b


def landslide_risk(rainfall_mm: float, threshold_upper: float) -> int:
    threshold_lower = threshold_upper / 2
    if rainfall_mm < threshold_lower:
        return 0
    elif rainfall_mm < threshold_upper:
        return 1
    elif rainfall_mm >= threshold_upper:
        return 2


def get_gauge_precipitation(place_name: str):
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

    try:
        # Get current precipitation
        latest_url = "https://api.synopticdata.com/v2/stations/latest"
        latest_params = {
            "token": SYNOPTIC_API_TOKEN,
            "stid": gauge_id,
            "vars": location_info["var"],
        }

        logger.info(f"Fetching current precipitation for {gauge_id}...")
        latest_response = requests.get(latest_url, params=latest_params, timeout=30)
        latest_response.raise_for_status()
        latest_data = latest_response.json()

        # Extract current precipitation
        current_precip = latest_data["STATION"][0]["OBSERVATIONS"][
            f"{location_info['var']}_value_1"
        ]["value"]
        logger.info(f"Current precipitation for {gauge_id}: {current_precip} mm")

        antecedent_minutes = ANTECEDENT_PERIOD * 60

        # Fetch ANTECEDENT_PERIOD hours of accumulated precipitation
        timeseries_url = "https://api.synopticdata.com/v2/stations/timeseries"
        timeseries_params = {
            "token": SYNOPTIC_API_TOKEN,
            "stid": gauge_id,
            "recent": str(antecedent_minutes),
            "precip": "1",
        }

        logger.info(
            f"Fetching {ANTECEDENT_PERIOD}-hour precipitation for {gauge_id}..."
        )
        timeseries_response = requests.get(
            timeseries_url, params=timeseries_params, timeout=30
        )
        timeseries_response.raise_for_status()
        timeseries_data = timeseries_response.json()

        # Extract ANTECEDENT_PERIOD hours of accumulated precipitation
        antecedent_precip = timeseries_data["STATION"][0]["OBSERVATIONS"][
            "precip_accumulated_set_1d"
        ][-1]
        logger.info(
            f"{ANTECEDENT_PERIOD}-hour accumulated precipitation for {gauge_id}: {antecedent_precip} mm"
        )

        return {
            "current_precip_mm": current_precip,
            "antecedent_precip_mm": antecedent_precip,
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching Synoptic data for {gauge_id}: {e}")
        return None
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Error parsing Synoptic data for {gauge_id}: {e}")
        return None
    except Exception as e:
        logger.exception(f"Unexpected error processing Synoptic data for {gauge_id}")
        return None


def get_historical_ecmwf_precipitation(forecast_time):
    """
    Retrieve historical ECMWF forecast precipitation from public S3 bucket.
    Downloads archived forecast data to cover the time range [historical_start_time, forecast_time).
    Data is available in 3 hour intervals starting at the 00 or 12 initialization of the model.
    Returns dict with Craig and Kasaan's data or None if unavailable.
    """
    historical_start_time = forecast_time - timedelta(hours=ANTECEDENT_PERIOD)

    try:
        logger.info(
            f"Downloading historical ECMWF data from S3: {historical_start_time.strftime('%Y-%m-%d %HZ')} to {forecast_time.strftime('%Y-%m-%d %HZ')}"
        )

        # We choose a historical initialization time earlier than the historical start time
        # so that we have forecast data that cover that period.
        init_time = historical_start_time.replace(minute=0, second=0, microsecond=0)
        if init_time.hour == 12:
            init_time = historical_start_time.replace(
                hour=0
            )  # Use midnight of same day
        else:
            # If historical_start_time is before noon, go back to previous day's noon forecast output
            init_time = (historical_start_time - timedelta(days=1)).replace(hour=12)

        logger.info(
            f"Using forecast initialized at {init_time.strftime('%Y-%m-%d %HZ')} for historical data"
        )

        # ECMWF S3 bucket structure: s3://ecmwf-forecasts/{date}/{time}z/ifs/0p25/oper/
        date_str = init_time.strftime("%Y%m%d")
        time_str = f"{init_time.hour:02d}z"
        base_path = f"{date_str}/{time_str}/ifs/0p25/oper"

        historical_data = {}
        # Not hard coded to allow for future additions of locations
        for place_name in LOCATIONS.keys():
            historical_data[place_name] = []

        # This is the precipitation accumulated up to the previous timestep
        running_precip_m = {place_name: 0.0 for place_name in LOCATIONS.keys()}

        # Generate timesteps at 3 hour intervals, which matches the ECMWF output frequency
        current_time = historical_start_time + timedelta(hours=3)
        while current_time <= forecast_time:
            hours_from_init = int((current_time - init_time).total_seconds() / 3600)

            # Download this specific timestep
            grib_file = f"/tmp/ecmwf_hist_{init_time.strftime('%Y%m%d%H')}_{hours_from_init}h.grib2"
            s3_key = f"{base_path}/{date_str}{init_time.hour:02d}0000-{hours_from_init}h-oper-fc.grib2"

            logger.info(
                f"Downloading historical step {hours_from_init}h from s3://{ecmwf_bucket}/{s3_key}"
            )

            max_retries = 5
            base_retry_delay = 3  # Increased from 2

            # Sometimes the S3 bucket returns SlowDown errors which require
            # retrying the download so that we don't miss a step.
            for attempt in range(max_retries):
                try:
                    ecmwf_s3_client.download_file(ecmwf_bucket, s3_key, grib_file)

                    # Open GRIB file and grab the total precipitation variable
                    ds = xr.open_dataset(
                        grib_file,
                        engine="cfgrib",
                        backend_kwargs={"filter_by_keys": {"shortName": "tp"}},
                    )

                    for place_name, location_info in LOCATIONS.items():
                        lat = location_info["lat"]
                        lon = location_info["lon"]

                        # Pull the closest data location for this timestep
                        precip_values = ds["tp"].sel(
                            latitude=lat, longitude=lon, method="nearest"
                        )

                        # Total precipitation is returned in meters
                        precip_m = float(precip_values.values)

                        # Subtract running precipitation to get incremental precipitation.
                        # Convert to millimeters divided by 3 hour period to get intensity mm/hr
                        precip_mm = (
                            (precip_m - running_precip_m[place_name]) * 1000
                        ) / 3

                        if precip_mm < 0:
                            logger.warning(
                                f"Negative historical precipitation detected for {place_name} at {current_time}: "
                                f"precip_mm={precip_mm:.4f}, precip_m={precip_m:.6f}, running={running_precip_m[place_name]:.6f}. "
                                f"Setting to 0.0"
                            )
                            precip_mm = 0.0

                        # Set running total precipitation for next iteration
                        running_precip_m[place_name] = precip_m

                        # Calculate timestamp in Alaska timezone
                        ts_alaska = current_time.astimezone(alaska_tz)

                        historical_data[place_name].append(
                            {"timestamp": ts_alaska.isoformat(), "precip_mm": precip_mm}
                        )

                    ds.close()

                    # Remove the GRIB file after being processed
                    if os.path.exists(grib_file):
                        os.remove(grib_file)

                    # Add a small delay between successful downloads to avoid rate limiting
                    time.sleep(0.5 + random.uniform(0, 0.5))  # 0.5-1.0 second delay

                    # Break out of loop if we're successful
                    break

                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    if attempt < max_retries - 1:
                        # Check if it's a SlowDown or rate limit error
                        if error_code in ["SlowDown", "ServiceUnavailable", "503"]:
                            # Exponential backoff with jitter
                            retry_delay = base_retry_delay * (2**attempt)
                            jitter = random.uniform(
                                0, retry_delay * 0.3
                            )  # Add up to 30% jitter
                            total_delay = retry_delay + jitter
                            logger.warning(
                                f"SlowDown error on historical step {hours_from_init}h (attempt {attempt + 1}/{max_retries}), "
                                f"retrying in {total_delay:.1f}s..."
                            )
                            time.sleep(total_delay)
                        else:
                            logger.error(
                                f"Error downloading historical step {hours_from_init}h: {e}"
                            )
                            # If we can't get a timestep, abort
                            return None
                    else:
                        logger.error(
                            f"Failed to download historical step {hours_from_init}h after {max_retries} attempts: {e}"
                        )
                        # Clean up partial file if it exists
                        if os.path.exists(grib_file):
                            os.remove(grib_file)
                        # If we can't get a timestep, abort
                        return None
                except Exception as e:
                    error_str = str(e)
                    if attempt < max_retries - 1:
                        # Check if it's a SlowDown error in the error string
                        if "SlowDown" in error_str or "503" in error_str:
                            # Exponential backoff with jitter
                            retry_delay = base_retry_delay * (2**attempt)
                            jitter = random.uniform(0, retry_delay * 0.3)
                            total_delay = retry_delay + jitter
                            logger.warning(
                                f"SlowDown error on historical step {hours_from_init}h (attempt {attempt + 1}/{max_retries}), "
                                f"retrying in {total_delay:.1f}s..."
                            )
                            time.sleep(total_delay)
                        else:
                            logger.error(
                                f"Error processing historical step {hours_from_init}h: {e}"
                            )
                            # If we can't get a timestep, abort
                            return None
                    else:
                        logger.error(
                            f"Failed to download historical step {hours_from_init}h after {max_retries} attempts: {e}"
                        )
                        # Clean up partial file if it exists
                        if os.path.exists(grib_file):
                            os.remove(grib_file)
                        # If we can't get a timestep, abort
                        return None

            current_time += timedelta(hours=3)

        if historical_data and any(historical_data.values()):
            return historical_data
        else:
            logger.error("No historical data retrieved")
            return None

    except Exception as e:
        logger.exception(f"Error retrieving historical ECMWF data from S3: {e}")
        return None


def get_forecast_precipitation(forecast_time, max_forecast_hours):
    """
    Retrieve ECMWF Open Data forecast precipitation for Craig and Kasaan.
    Downloads directly from ECMWF's public S3 bucket to avoid API rate limits.
    Data is available in 3 hour intervals starting at the 00 or 12 initialization of the model.

    For a 3-day (72-hour) forecast:
    - At 00z (midnight): downloads 3h to 72h (24 files)
    - At 12z (noon): downloads 3h to 60h (20 files) - only needs 60h to cover remaining 3 days

    Args:
        forecast_time: Initialization time (00 or 12)
        max_forecast_hours: Maximum forecast hours to download (72 at 00, 60 at 12)

    Returns dict with Craig and Kasaan's data or None if unavailable.
    """

    # Download from ECMWF's public S3 bucket instead of using rate-limited API
    try:
        logger.info(
            f"Downloading ECMWF forecast from S3 bucket for {forecast_time.strftime('%Y-%m-%d %HZ')}"
        )

        # ECMWF S3 bucket structure: s3://ecmwf-forecasts/{date}/{time}z/ifs/0p25/oper/
        # Files: {date}{time}0000-{step}h-oper-fc.grib2
        date_str = forecast_time.strftime("%Y%m%d")
        time_str = f"{forecast_time.hour:02d}z"
        base_path = f"{date_str}/{time_str}/ifs/0p25/oper"

        forecast_data = {}
        for place_name in LOCATIONS.keys():
            forecast_data[place_name] = []

        # This is the precipitation accumulated up to the previous timestep
        # Required to subtract this value to get incremental precipitation.
        running_precip_m = {place_name: 0.0 for place_name in LOCATIONS.keys()}

        for step_hours in range(3, max_forecast_hours + 3, 3):
            grib_file = f"/tmp/ecmwf_fc_{forecast_time.strftime('%Y%m%d%H')}_{step_hours}h.grib2"
            s3_key = f"{base_path}/{date_str}{forecast_time.hour:02d}0000-{step_hours}h-oper-fc.grib2"

            logger.info(
                f"Downloading step {step_hours}h from s3://{ecmwf_bucket}/{s3_key}"
            )

            max_retries = 5
            base_retry_delay = 3  # Increased from 2

            # Sometimes the S3 bucket returns SlowDown errors which require
            # retrying the download so that we don't miss a step.
            for attempt in range(max_retries):
                try:
                    ecmwf_s3_client.download_file(ecmwf_bucket, s3_key, grib_file)

                    # Parse GRIB file for this timestep - filter to only total precipitation
                    # to avoid conflicts from multiple variables at different heights
                    ds = xr.open_dataset(
                        grib_file,
                        engine="cfgrib",
                        backend_kwargs={"filter_by_keys": {"shortName": "tp"}},
                    )

                    for place_name, location_info in LOCATIONS.items():
                        lat = location_info["lat"]
                        lon = location_info["lon"]

                        # Pull the closest data location for this timestep
                        precip_values = ds["tp"].sel(
                            latitude=lat, longitude=lon, method="nearest"
                        )

                        ts = forecast_time + timedelta(hours=step_hours)
                        ts_alaska = ts.astimezone(alaska_tz)

                        # Total precipitation is returned in meters
                        precip_m = float(precip_values.values)

                        # Subtract running precipitation to get incremental precipitation.
                        # Convert to millimeters divided by 3 hour period to get intensity mm/hr
                        precip_mm = (
                            (precip_m - running_precip_m[place_name]) * 1000
                        ) / 3

                        if precip_mm < 0:
                            logger.warning(
                                f"Negative forecast precipitation detected for {place_name} at step {step_hours}h: "
                                f"precip_mm={precip_mm:.4f}, precip_m={precip_m:.6f}, running={running_precip_m[place_name]:.6f}. "
                                f"Setting to 0.0"
                            )
                            precip_mm = 0.0

                        # Set running total for next iteration
                        running_precip_m[place_name] = precip_m

                        forecast_data[place_name].append(
                            {"timestamp": ts_alaska.isoformat(), "precip_mm": precip_mm}
                        )

                    ds.close()

                    # Remove the GRIB file after being processed
                    if os.path.exists(grib_file):
                        os.remove(grib_file)

                    # Add a small delay between successful downloads to avoid rate limiting
                    time.sleep(0.5 + random.uniform(0, 0.5))  # 0.5-1.0 second delay

                    # Break out of loop if we're successful
                    break

                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    if attempt < max_retries - 1:
                        # Check if it's a SlowDown or rate limit error
                        if error_code in ["SlowDown", "ServiceUnavailable", "503"]:
                            # Exponential backoff with jitter
                            retry_delay = base_retry_delay * (2**attempt)
                            jitter = random.uniform(
                                0, retry_delay * 0.3
                            )  # Add up to 30% jitter
                            total_delay = retry_delay + jitter
                            logger.warning(
                                f"SlowDown error on step {step_hours}h (attempt {attempt + 1}/{max_retries}), "
                                f"retrying in {total_delay:.1f}s..."
                            )
                            time.sleep(total_delay)
                        else:
                            logger.error(f"Error downloading step {step_hours}h: {e}")
                            # If we can't get a timestep, abort
                            return None
                    else:
                        logger.error(
                            f"Failed to download step {step_hours}h after {max_retries} attempts: {e}"
                        )
                        # Clean up partial file if it exists
                        if os.path.exists(grib_file):
                            os.remove(grib_file)
                        # If we can't get a timestep, abort
                        return None
                except Exception as e:
                    error_str = str(e)
                    if attempt < max_retries - 1:
                        # Check if it's a SlowDown error in the error string
                        if "SlowDown" in error_str or "503" in error_str:
                            # Exponential backoff with jitter
                            retry_delay = base_retry_delay * (2**attempt)
                            jitter = random.uniform(0, retry_delay * 0.3)
                            total_delay = retry_delay + jitter
                            logger.warning(
                                f"SlowDown error on step {step_hours}h (attempt {attempt + 1}/{max_retries}), "
                                f"retrying in {total_delay:.1f}s..."
                            )
                            time.sleep(total_delay)
                        else:
                            logger.error(f"Error processing step {step_hours}h: {e}")
                            # If we can't get a timestep, abort
                            return None
                    else:
                        logger.error(
                            f"Failed to download step {step_hours}h after {max_retries} attempts: {e}"
                        )
                        # Clean up partial file if it exists
                        if os.path.exists(grib_file):
                            os.remove(grib_file)
                        # If we can't get a timestep, abort
                        return None

        return forecast_data

    except Exception as e:
        logger.exception(f"Error downloading ECMWF forecast from S3: {e}")
        return None


def calculate_forecast_blocks(forecast_data, historical_data):
    """
    Calculate intensity windows for the full 72-hour forecast period.
    For each 3 hour period, calculate:
    - intensity_mm: rainfall in that discrete 3-hour period (single timestamp)
    - antecedent_mm: sum of previous 3 hour periods within ANTECEDENT_PERIOD lookback

    Historical data is prepended to forecast_data to provide
    complete antecedent coverage from the first forecast timestep.
    Returns single array with all forecast timesteps.
    """

    # Combine historical and forecast data
    combined_data = []

    # Add historical data
    for hindcast in historical_data:
        timestamp = datetime.fromisoformat(hindcast["timestamp"])
        combined_data.append((timestamp, hindcast["precip_mm"]))

    # Add forecast data
    for forecast in forecast_data:
        timestamp = datetime.fromisoformat(forecast["timestamp"])
        combined_data.append((timestamp, forecast["precip_mm"]))

    # Sort by time
    combined_data.sort(key=lambda x: x[0])

    # Determine where forecast starts (for calculating forecast_hour)
    historical_count = len(historical_data)

    # Calculate how many timesteps fit in antecedent period
    timesteps_in_antecedent = ANTECEDENT_PERIOD // 3

    forecast_blocks = []

    # Process only the forecast portion of the data
    for i in range(historical_count, len(combined_data)):
        block_end_time = combined_data[i][0]

        # Intensity is ONLY the current 3-hour period
        intensity_mm = combined_data[i][1]

        # Calculate antecedent: sum of previous timesteps within lookback window
        # With historical data prepended, we now have full antecedent coverage
        lookback_start_index = i - timesteps_in_antecedent
        antecedent_mm = sum(
            precip for dt, precip in combined_data[lookback_start_index:i]
        )

        landslide_threshold_upper = landslide_threshold(antecedent_mm)
        landslide_risk_level = landslide_risk(intensity_mm, landslide_threshold_upper)

        # Calculate forecast hour (hours from forecast start, excluding historical)
        forecast_hour = ((i + 1) - historical_count) * 3

        forecast_blocks.append(
            {
                "timestamp": block_end_time.isoformat(),
                "forecast_hour": forecast_hour,
                "intensity_mm": round(intensity_mm, 2),
                "antecedent_mm": round(antecedent_mm, 2),
                "risk_threshold_upper": round(landslide_threshold_upper, 2),
                "risk_level": landslide_risk_level,
            }
        )

    return forecast_blocks


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

    expires_at = now + timedelta(hours=3)
    expires_at_str = expires_at.isoformat()

    # Determine forecast initialization time (midnight or noon)
    if now.hour >= 12:
        forecast_time = now.replace(hour=12, minute=0, second=0, microsecond=0)
        max_forecast_hours = 60
    else:
        forecast_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        max_forecast_hours = 72

    logger.info(
        f"Using forecast initialized at {forecast_time.strftime('%Y-%m-%d %HZ')}"
    )

    # Fetch historical ECMWF data for antecedent period from archived forecasts in S3
    logger.info(
        f"Retrieving historical ECMWF data for {ANTECEDENT_PERIOD}-hour antecedent period from S3 archive..."
    )
    historical_data = get_historical_ecmwf_precipitation(forecast_time)

    if historical_data is None:
        logger.error("No historical ECMWF data available, aborting processing.")
        raise RuntimeError("No historical ECMWF data available")

    logger.info("Retrieving ECMWF forecast data...")
    forecast_data = get_forecast_precipitation(forecast_time, max_forecast_hours)

    if forecast_data is None:
        logger.error("No ECMWF forecast data available, aborting processing.")
        raise RuntimeError("No ECMWF forecast data available")

    try:
        with conn.cursor() as cur:
            for place_name in places_to_run:
                logger.info(f"Processing {place_name}...")

                # Get real-time gauge precipitation data
                gauge_data = get_gauge_precipitation(place_name)

                if gauge_data is None:
                    logger.error(
                        f"No Synoptic gauge data available for {place_name}, aborting processing."
                    )
                    raise RuntimeError(
                        f"No Synoptic gauge data available for {place_name}"
                    )

                realtime_rainfall_mm = gauge_data["current_precip_mm"]
                realtime_antecedent = gauge_data["antecedent_precip_mm"]
                gauge_id = LOCATIONS[place_name]["gauge_id"]
                realtime_threshold_upper = landslide_threshold(realtime_antecedent)
                realtime_risk_level = landslide_risk(
                    realtime_rainfall_mm, realtime_threshold_upper
                )

                # Calculate forecast blocks for this location
                forecast_blocks = None

                if place_name in forecast_data:
                    current_location_forecast = forecast_data[place_name]

                    # Get historical data for this location if available
                    current_location_historical = None
                    if historical_data and place_name in historical_data:
                        current_location_historical = historical_data[place_name]

                    blocks_array = calculate_forecast_blocks(
                        current_location_forecast, current_location_historical
                    )

                    # Calculate current forecast hour based on time since initialization
                    hours_since_init = (now - forecast_time).total_seconds() / 3600
                    current_forecast_hour = math.ceil(hours_since_init / 3) * 3

                    # Filter to rolling 72-hour window starting from current time
                    rolling_72hr_blocks = [
                        block
                        for block in blocks_array
                        if block["forecast_hour"] >= current_forecast_hour
                    ][
                        :24
                    ]  # 24 blocks = 72 hours

                    logger.info(
                        f"Rolling forecast window: {len(rolling_72hr_blocks)} windows from hour {current_forecast_hour}"
                    )
                    if rolling_72hr_blocks:
                        logger.info(
                            f"First window: hour {rolling_72hr_blocks[0]['forecast_hour']} ending at {rolling_72hr_blocks[0]['timestamp']}"
                        )
                        logger.info(
                            f"Last window: hour {rolling_72hr_blocks[-1]['forecast_hour']} ending at {rolling_72hr_blocks[-1]['timestamp']}"
                        )

                    # Convert to JSON string for PSQL JSONB column
                    forecast_blocks = json.dumps(rolling_72hr_blocks)
                place_id = get_place_id(place_name)

                sql = """
                INSERT INTO landslide_risk (
                ts, place_name, place_id, expires_at,
                realtime_rainfall_mm, realtime_threshold_upper, realtime_risk_level,
                gauge_id, realtime_antecedent_mm, antecedent_period,
                forecast_blocks
                ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s
                )
                """
                cur.execute(
                    sql,
                    (
                        now,
                        place_name,
                        place_id,
                        expires_at_str,
                        realtime_rainfall_mm,
                        realtime_threshold_upper,
                        realtime_risk_level,
                        gauge_id,
                        realtime_antecedent,
                        ANTECEDENT_PERIOD,
                        forecast_blocks,
                    ),
                )

                logger.info(f"Successfully processed {place_name}")

        return {
            "status": "ok",
            "places_processed": places_to_run,
            "timestamp": now.isoformat(),
            "forecast_available": forecast_data is not None,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    test_event = {"places": ["Craig", "Kasaan"]}
    result = lambda_handler(test_event, None)
    logger.info(f"Lambda handler result: {result}")
