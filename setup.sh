#!/bin/bash

# This script is executed by Streamlit Cloud after installing requirements
# It installs Playwright browsers

echo "Installing Playwright browsers..."
playwright install chromium
playwright install-deps chromium

echo "Playwright setup complete!"
