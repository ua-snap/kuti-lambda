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

## Deploying to Lambda

This function uses a Docker container image for deployment due to the large size of dependencies (>400MB). Be sure to have Docker running locally on your system.

### Step 1: Build Docker Image

Build the Docker image for Linux/AMD64 platform (required for Lambda):

```bash
docker build --platform linux/amd64 -t kuti-landslide:latest .
```

### Step 2: Login to ECR

Authenticate Docker to your Amazon ECR registry:

```bash
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin \
  904506553012.dkr.ecr.us-west-2.amazonaws.com
```

### Step 3: Tag and Push Image

Tag the image and push to ECR:

```bash
docker tag kuti-landslide:latest \
  904506553012.dkr.ecr.us-west-2.amazonaws.com/kuti-landslide:latest

docker push 904506553012.dkr.ecr.us-west-2.amazonaws.com/kuti-landslide:latest
```

### Step 4: Update Lambda Function

Update the Lambda function with the new image:

```bash
aws lambda update-function-code \
  --function-name Kuti_Insert \
  --image-uri 904506553012.dkr.ecr.us-west-2.amazonaws.com/kuti-landslide:latest
```

**NOTE:** The function will automatically use the updated image on the next invocation.

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

If you want to run the Lambda function locally, you can use Micromamba to
create a new environment for this project:

### Step 1: Create Micromamba Environment

Create a new environment with the required dependencies:

```bash
micromamba create -n kuti-lambda python=3.12 -y
micromamba activate kuti-lambda
pip install -r requirements.txt
```

### Step 2: Run the Lambda Function

With the environment activated:

```bash
python lambda_function.py
```
