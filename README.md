# Landslide Risk Lambda Insert Functions

## Database and Table Configuration

Database name: landslide_risk

Table Configuration:

```sql
CREATE TABLE landslide_risk (
  id SERIAL PRIMARY KEY,
  ts TIMESTAMP WITH TIME ZONE NOT NULL,
  place_name TEXT NOT NULL,
  place_id TEXT,
  expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  realtime_rainfall_mm NUMERIC,
  realtime_threshold_upper NUMERIC,
  realtime_risk_level INTEGER,
  gauge_id TEXT,
  realtime_antecedent_mm NUMERIC,
  forecast_blocks JSONB
);

-- Indexes
CREATE INDEX idx_landslide_risk_place_ts ON landslide_risk (place_name, ts DESC);
CREATE INDEX idx_landslide_risk_place_id_ts ON landslide_risk (place_id, ts DESC);
CREATE INDEX idx_landslide_risk_expires_at ON landslide_risk (expires_at);
CREATE INDEX idx_landslide_risk_ts ON landslide_risk (ts DESC);

```

## Importing into Lambda

### Step 1: Install Dependencies

```bash
pip install -r requirements.txt -t python_deps/python/
```

### Step 2: Create and Upload Lambda Layer

Zip the dependencies into a Lambda Layer:

```bash
cd python_deps && zip -r ../lambda-layer.zip python/ && cd ..
```

Upload the layer to AWS:

```bash
aws lambda publish-layer-version \
  --layer-name kuti-dependencies \
  --description "Dependencies for Landslide Risk Lambda" \
  --zip-file fileb://lambda-layer.zip \
  --compatible-runtimes python3.11 python3.12
```

Note the `LayerVersionArn` from the output (you'll need it in Step 4).

### Step 3: Update Lambda Function Code

Zip only the function code (without dependencies):

```bash
zip lambda_function.zip lambda_function.py
```

Update the Lambda function:

```bash
aws lambda update-function-code --function-name Landslide_Risk_Insert --zip-file fileb://lambda_function.zip
```

### Step 4: Attach Layer to Function

Replace `<LayerVersionArn>` with the ARN from Step 2:

```bash
aws lambda update-function-configuration \
  --function-name Landslide_Risk_Insert \
  --layers <LayerVersionArn>
```

Example: `arn:aws:lambda:us-west-2:123456789012:layer:kuti-dependencies:1`

## Update Lambda run time

This Lambda function is set to be run once every 3 hours by an EventBridge which can be modified via AWS CLI.

```bash
aws events put-rule --name Landslide_Risk_Event --schedule-expression "rate(2 hours)"
```

Confirm that the change is recognized

```bash
aws events describe-rule --name Landslide_Risk_Event
```

## Running the Lambda function locally

If you want to run the Lambda function locally, you can use the downloaded Python dependencies by running the following command from this directory:

```bash
PYTHONPATH=python_deps/python python lambda_function.py
```
