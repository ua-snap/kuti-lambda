# Landslide Risk Lambda Insert Functions

## Importing into Lambda

Zip all of the files contained in this directory including the Python libraries that must be included as part of this binary.

```bash
zip -r lambda_package.zip .
```

Update the Lambda function in AWS using the AWS CLI

```bash
aws lambda update-function code --function-name Landslide_Risk_Insert --zip-file fildb://lambda_package.zip
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
