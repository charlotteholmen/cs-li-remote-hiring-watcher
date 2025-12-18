# Slack Bot Token Setup Guide

## 🤖 Creating a Slack Bot for AWS Cost Alerts

### Step 1: Create a Slack App

1. Go to <https://api.slack.com/apps>
2. Click **"Create New App"** → **"From scratch"**
3. App Name: `AWS Cost Alert Bot`
4. Workspace: Select your workspace (T08FU0Q78BG)

### Step 2: Configure Bot Token Scopes

1. In your app settings, go to **"OAuth & Permissions"**
2. Scroll to **"Scopes"** → **"Bot Token Scopes"**
3. Add these required scopes:
   - `chat:write` - Send messages
   - `chat:write.public` - Send messages to channels the bot isn't in

### Step 3: Install App to Workspace

1. Scroll up to **"OAuth Tokens for Your Workspace"**
2. Click **"Install to Workspace"**
3. Review permissions and click **"Allow"**
4. **Copy the Bot User OAuth Token** (starts with `xoxb-`)

### Step 4: Configure Lambda Environment

Replace the placeholder in main.tf:

```hcl
environment {
  variables = {
    SLACK_BOT_TOKEN = "xoxb-your-actual-token-here"
    SLACK_CHANNEL   = "#aws-cost-alerts"  # or your preferred channel
  }
}
```

### Step 5: Deploy Updated Lambda

```bash
# Package and deploy
zip -r function.zip lambda_function.py
terraform apply -auto-approve

# Or update function directly
aws lambda update-function-code \
  --function-name aws-cost-alert-slack \
  --zip-file fileb://function.zip \
  --region ap-south-1

# Update environment variables
aws lambda update-function-configuration \
  --function-name aws-cost-alert-slack \
  --environment Variables='{SLACK_BOT_TOKEN=xoxb-your-token,SLACK_CHANNEL=#aws-cost-alerts}' \
  --region ap-south-1
```

### Step 6: Invite Bot to Channel (if needed)

If using a private channel:

1. Go to your Slack channel
2. Type `/invite @AWS Cost Alert Bot`

## ✅ Benefits of Bot Token vs Webhooks

- **More Stable**: Bot tokens don't expire like webhooks
- **Better Error Handling**: Detailed API responses
- **Rate Limit Info**: Clear rate limiting feedback
- **Richer Features**: Can add reactions, threads, etc.
- **Audit Trail**: Better logging and monitoring

## 🧪 Testing

After setup, test manually:

```bash
aws lambda invoke \
  --function-name aws-cost-alert-slack \
  --region ap-south-1 \
  /tmp/test-output.json && cat /tmp/test-output.json
```

## 🔐 Security Notes

- Bot token is more secure than webhooks
- Tokens can be regenerated without breaking functionality
- Proper OAuth scopes limit bot permissions
- Environment variables keep tokens out of code
