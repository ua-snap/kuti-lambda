# Landslide Risk Lambda Insert Functions

## Database and Table Configuration

Database name: landslide_risk

Table Configuration:

```sql
CREATE TABLE precip_risk (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    place_name VARCHAR(32) NOT NULL,
    precip DOUBLE PRECISION NOT NULL,
    precip_inches DOUBLE PRECISION NOT NULL,
    hour VARCHAR(10) NOT NULL,
    risk_prob DOUBLE PRECISION NOT NULL,
    risk_level INTEGER NOT NULL,
    risk_is_elevated_from_previous BOOLEAN,
    precip24hr DOUBLE PRECISION,
    risk24hr INTEGER,
    precip2days DOUBLE PRECISION,
    risk2days INTEGER,
    precip3days DOUBLE PRECISION,
    risk3days INTEGER,
    expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL
);

-- Adds a unique requirement to prevent multiple entries from the same timestamp and place
ALTER TABLE precip_risk
  ADD CONSTRAINT unique_place_time UNIQUE (place_name, ts);

-- Fast lookup by most recent timestamp
CREATE INDEX idx_precip_risk_ts ON precip_risk (ts DESC);

-- Fast lookup of latest data per location
CREATE INDEX idx_precip_risk_place_ts ON precip_risk (place_name, ts DESC);
```

## Importing into Lambda

Download required packages for upload to Lambda.

```bash
pip install pytz -t .
pip install pg8000 -t .
```

Zip all of the files contained in this directory including the Python libraries that must be included as part of this binary.

```bash
zip -r lambda_package.zip .
```

Update the Lambda function in AWS using the AWS CLI

```bash
aws lambda update-function code --function-name Landslide_Risk_Insert --zip-file fileb://lambda_package.zip
```

## Update Lambda run time

This Lambda function is set to be run once every 3 hours by an EventBridge which can be modified via AWS CLI.

```bash
aws events put-rule --name Landslide_Risk_Event --schedule-expression "rate(2 hours)"
```

Confirm that the change is recognized

```bash
aws events describe-rule --name Landslide_Risk_Event
```
