#!/bin/bash
# Job Monitor - Setup Script
# Run this once to install dependencies and configure the monitor.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================"
echo "  Job Monitor - Setup"
echo "======================================"
echo ""

# 1. Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv venv
    echo "  ✓ Virtual environment created"
else
    echo "  ✓ Virtual environment already exists"
fi

# 2. Activate and install dependencies
echo "Installing dependencies..."
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "  ✓ Dependencies installed"

# 3. Install Playwright browsers
echo "Installing Playwright Chromium browser (this may take a minute)..."
playwright install chromium
echo "  ✓ Chromium installed"

# 4. Check config
echo ""
echo "======================================"
echo "  Configuration"
echo "======================================"

if grep -q "YOUR_EMAIL" config.json; then
    echo ""
    echo "⚠️  You need to configure your email settings in config.json!"
    echo ""
    echo "Edit: $SCRIPT_DIR/config.json"
    echo ""
    echo "For Gmail, you need an App Password:"
    echo "  1. Go to https://myaccount.google.com/apppasswords"
    echo "  2. Generate a new app password for 'Mail'"
    echo "  3. Put it in config.json under sender_password"
    echo ""
else
    echo "  ✓ Email appears to be configured"
fi

# 5. Run a dry test
echo ""
echo "======================================"
echo "  Test Run"
echo "======================================"
echo ""
echo "Running a dry-run test (no emails will be sent)..."
echo ""
python3 job_monitor.py --dry-run

echo ""
echo "======================================"
echo "  Setup Complete!"
echo "======================================"
echo ""
echo "Usage:"
echo "  source venv/bin/activate"
echo "  python job_monitor.py --dry-run    # Test without sending emails"
echo "  python job_monitor.py              # Run and send email if changes found"
echo "  python job_monitor.py --list       # Show tracked jobs"
echo "  python job_monitor.py --reset      # Clear state and start fresh"
echo ""
echo "To schedule daily checks, run:"
echo "  crontab -e"
echo "Then add this line (runs daily at 8am):"
echo "  0 8 * * * cd $SCRIPT_DIR && ./venv/bin/python job_monitor.py >> $SCRIPT_DIR/cron.log 2>&1"
echo ""
