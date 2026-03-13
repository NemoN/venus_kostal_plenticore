#!/bin/bash
# Install script for venus_kostal_plenticore on Victron Cerbo GX
# Survives Venus OS updates by keeping everything in /data/

INSTALL_DIR="/data/venus_kostal_plenticore"
SERVICE_DIR="/service/venus_kostal_plenticore"

echo "=== Installing venus_kostal_plenticore ==="

# Create install directory
mkdir -p "$INSTALL_DIR"

# Copy files
cp kostal.py "$INSTALL_DIR/"
cp dbus_inverter.py "$INSTALL_DIR/"
cp plenticoreDataService.py "$INSTALL_DIR/"
cp plenticoreSessionService.py "$INSTALL_DIR/"
cp loggingConfig.py "$INSTALL_DIR/"
cp kill_me.sh "$INSTALL_DIR/"

# Only copy config if it doesn't exist (don't overwrite user config)
if [ ! -f "$INSTALL_DIR/kostal.ini" ]; then
    cp kostal.ini "$INSTALL_DIR/"
    echo "Copied default kostal.ini — EDIT THIS with your IP and password!"
else
    echo "kostal.ini already exists, keeping existing config"
fi

# Install Python dependencies into the install directory (survives updates)
echo "Installing Python dependencies into $INSTALL_DIR..."
pip3 install --target="$INSTALL_DIR/lib" pycryptodomex requests 2>/dev/null || {
    echo "pip3 not available, trying opkg..."
    opkg update 2>/dev/null
    opkg install python3-pip 2>/dev/null
    pip3 install --target="$INSTALL_DIR/lib" pycryptodomex requests 2>/dev/null || {
        echo "ERROR: Could not install dependencies. Install manually:"
        echo "  pip3 install --target=$INSTALL_DIR/lib pycryptodomex requests"
        exit 1
    }
}

# Create wrapper script that sets PYTHONPATH before running
cat > "$INSTALL_DIR/run.py" << 'PYEOF'
#!/usr/bin/env python3
import sys
import os

# Add bundled libraries to path (survives Venus OS updates)
lib_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'lib')
if os.path.isdir(lib_dir):
    sys.path.insert(0, lib_dir)

# Change to install directory so kostal.ini is found
os.chdir(os.path.dirname(os.path.realpath(__file__)))

# Run the main script
exec(open(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'kostal.py')).read())
PYEOF
chmod +x "$INSTALL_DIR/run.py"

# Create daemontools service
mkdir -p "$INSTALL_DIR/service"
cat > "$INSTALL_DIR/service/run" << 'EOF'
#!/bin/sh
exec 2>&1
exec python3 /data/venus_kostal_plenticore/run.py /data/venus_kostal_plenticore/kostal.ini
EOF
chmod +x "$INSTALL_DIR/service/run"

# Create rc.local hook to re-create service symlink after reboot/update
RC_LOCAL="/data/rc.local"
HOOK_LINE="ln -sf /data/venus_kostal_plenticore/service /service/venus_kostal_plenticore"

if [ -f "$RC_LOCAL" ]; then
    if ! grep -q "venus_kostal_plenticore" "$RC_LOCAL"; then
        echo "$HOOK_LINE" >> "$RC_LOCAL"
        echo "Added service hook to $RC_LOCAL"
    else
        echo "Service hook already in $RC_LOCAL"
    fi
else
    echo "#!/bin/bash" > "$RC_LOCAL"
    echo "$HOOK_LINE" >> "$RC_LOCAL"
    chmod +x "$RC_LOCAL"
    echo "Created $RC_LOCAL with service hook"
fi

# Create service symlink now
ln -sf "$INSTALL_DIR/service" "$SERVICE_DIR"

echo ""
echo "=== Installation complete ==="
echo "Service: $SERVICE_DIR"
echo "Config:  $INSTALL_DIR/kostal.ini"
echo "Logs:    $INSTALL_DIR/kostal.log"
echo ""
echo "The service will auto-start after Venus OS updates via /data/rc.local"
echo "Dependencies are stored in $INSTALL_DIR/lib/ (survives updates)"
