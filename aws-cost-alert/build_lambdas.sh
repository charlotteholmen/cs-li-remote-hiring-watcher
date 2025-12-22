#!/bin/bash

# Build script for AWS Cost Alert Lambdas
# Creates two separate deployment packages with dependencies

echo "🏗️  Building AWS Cost Alert Lambda deployment packages..."

# Clean up previous builds
rm -f scheduled_function.zip threshold_function.zip
rm -rf temp_scheduled temp_threshold

# Install dependencies for both packages
echo "📦 Installing Python dependencies..."
pip3 install requests pytz -t ./temp_libs

# Create scheduled notification lambda package
echo "📦 Creating scheduled notification lambda package..."
mkdir -p temp_scheduled
cp scheduled_notification_lambda.py cost_utils.py temp_scheduled/
cp -r temp_libs/* temp_scheduled/
cd temp_scheduled
zip -r ../scheduled_function.zip .
cd ..
rm -rf temp_scheduled

# Create threshold alert lambda package  
echo "📦 Creating threshold alert lambda package..."
mkdir -p temp_threshold
cp threshold_alert_lambda.py cost_utils.py temp_threshold/
cp -r temp_libs/* temp_threshold/
cd temp_threshold
zip -r ../threshold_function.zip .
cd ..
rm -rf temp_threshold

# Clean up temp libs
rm -rf temp_libs

# Verify packages were created
if [ -f "scheduled_function.zip" ] && [ -f "threshold_function.zip" ]; then
    echo "✅ Successfully created both deployment packages:"
    echo "   - scheduled_function.zip ($(du -h scheduled_function.zip | cut -f1))"
    echo "   - threshold_function.zip ($(du -h threshold_function.zip | cut -f1))"
else
    echo "❌ Failed to create deployment packages"
    exit 1
fi

echo "🚀 Ready to deploy with terraform apply!"