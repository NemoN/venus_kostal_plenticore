# Venus Kostal Plenticore plugin

This plugin integrates Kostal Plenticore (or similar) inverters into Venus OS. It reads AC production data, DC tracker data (per PV string), grid frequency, inverter status, energy statistics and device information via the inverter's REST API and publishes them on D-Bus for use in the Venus OS GUI and VRM Portal.

## Features

- **AC data**: Per-phase voltage, current, power and energy (L1/L2/L3) + totals
- **AC frequency**: Grid frequency
- **DC tracker data**: Voltage, current and power per PV string (PV1, PV2, PV3)
- **Inverter status**: Mapped to Venus OS status codes (Startup/Running/Standby/Error)
- **Energy statistics**: Total yield with interpolation (5-min update workaround), daily yield
- **Device info**: Serial number, product name, firmware version, max power — automatically fetched from inverter settings API
- **Custom name**: Editable via Venus OS Remote Console
- **Auto-detection**: Number of PV strings is read from inverter settings, DC/2 paths only registered if 3 strings present
- **Robust**: DC tracker fetch is non-fatal — if it fails, AC data continues to work
- **Reconnect**: Exponential backoff on connection loss

## Compatibility

This plugin should work with all PIKO IQ and PLENTICORE PLUS inverters. Go to `http://your-inverters-ip/api/v1/info/version`, if you get a response like this:   
```
{
  "sw_version": "01.15.04581",
  "api_version": "0.2.0",
  "name": "PUCK RESTful API",
  "hostname": "scb"
}
```
you might be lucky and this plugin works for you. If you're interested in the api itself, have a look at the inverters swagger UI for api documentation at `http://your-inverters-ip/api/v1`. 
If you happen to have another api version and this script does not work anymore - let me know.
If you don't get an response - this plugin won't help you and you should search further ;) 

## Inspiration/code sources:
- https://github.com/schenlap/venus_kostal_pico Thanks to schenlap for his plugin for the (original) pico inverters. I've used his code and ideas on the dbus side to get the data into venus os
- https://github.com/RalfZim/venus.dbus-fronius-smartmeter Thanks to ralfzim for his service configuration 
- https://stackoverflow.com/questions/59053539/api-call-portation-from-java-to-python-kostal-plenticore-inverter thanks to E3EAT for the session token calculation 

## Requirements: 
This plugin does only work on Venus os **LARGE** - so if you're running the 'normal' version make sure to upgrade first

Background (only if you want to know why): For session initialization this script requires AES from the pycryptodomex lib. Pycryptodomex needs gcc and stdlibs during it's installation, these seem to be available on the large venus version, but not on the normal. If you know how to get pycryptodomex installed on normal venus os versions let me know!

## Installation

Connect via ssh as root to your venus os. If you don't have root access jet, see here: https://www.victronenergy.com/live/ccgx:root_access

### Install dependencies:
Important: You might need to reinstall these dependencies after a venus os update to get the plugin running again as the update seems to overwride everything outside the /data dir)

1. install pip (python package manager):
   ```
   opkg update && opkg install python3-pip
   ```
2. install pycryptodomex:
   ```
   pip3 install pycryptodomex
   ```

### Install plugin:

Download all files from this repo and copy them to the new dir `/data/venus_kostal_plenticore`.
If you download the code as .zip from github, make sure to remove the `-main` prefix. 
Create that dir if it does not jet exists. 
Venus OS does not come with git, so I recommend cloning/downloading this repo to your machine, then transfer all files e.g. using scp (`scp -r venus_kostal_plenticore/* root@venusip:/data/venus_kostal_plenticore/`)


### Configure plugin:

1. configure `kostal.ini`: set kostal_name, the inverters ip, the vrm instance, password and refresh interval. 
    ```ini
    [kostal_name]
    ip = http://192.168.178.XXX
    instance = 50
    password = XXX
    interval = 5
    position = 0
    loglevel = INFO
    ``` 

   | Option | Required | Default | Description |
   |--------|----------|---------|-------------|
   | `ip` | yes | — | Inverter URL, format `http://x.x.x.x` |
   | `password` | yes | — | Inverter user password |
   | `interval` | yes | — | Poll interval in seconds |
   | `instance` | no | `50` | VRM device instance number |
   | `position` | no | `0` | AC position: 0=AC input 1, 1=AC output, 2=AC input 2 |
   | `loglevel` | no | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

   The section name (e.g. `[kostal_name]`) is used as the D-Bus service name suffix.

2. set File permissions for run and kill scripts:
   - `chmod 755 /data/venus_kostal_plenticore/service/run`
   - `chmod 744 /data/venus_kostal_plenticore/kill_me.sh`

   
3. Verify that your setup is correct:

   - Run `/data/venus_kostal_plenticore/kostal.py /data/venus_kostal_plenticore/kostal.ini`. If everything works fine you should see your kostals current values printed periodically. If not fix your config/installation and try again.

4. Enable services:
   - `ln -s /data/venus_kostal_plenticore/service /service/venus_kostal_plenticore` The daemon-tools should automatically start this service within seconds.

5. Configure this script to start when venus OS is booted:
   Create rc.local, make it executable:
   ```
   echo -e '#!/bin/bash' >> /data/rc.local
   echo 'ln -s /data/venus_kostal_plenticore/service /service/venus_kostal_plenticore' >> /data/rc.local
   chmod +x /data/rc.local 
   ```   
   If you already have the file `/data/rc.local` only add the line  `ln -s /data/venus_kostal_plenticore/service /service/venus_kostal_plenticore` to it.
   The rc.local file is executed when venus os boots and will create the link in the service directory for you


### Multiple Plenticores

Simply add one section per inverter to `kostal.ini`. Each section gets its own D-Bus service and polling thread automatically — no need to duplicate service directories or run scripts.

```ini
[east-roof]
ip = http://192.168.178.10
instance = 50
password = XXX
interval = 5
position = 0

[west-roof]
ip = http://192.168.178.11
instance = 51
password = XXX
interval = 5
position = 0
```

**Important**:
- Each inverter must have a **unique IP address** — the Kostal API only allows one active session per inverter, so two sections with the same IP will not work.
- Each inverter must have a **unique instance number** and a **unique section name**, otherwise only one will show up in the GUI.


## D-Bus paths

The plugin registers as `com.victronenergy.pvinverter.<name>` and exposes:

| Path | Description |
|------|-------------|
| `/Ac/Power` | Total AC power (W) |
| `/Ac/Current` | Total AC current (A) |
| `/Ac/Voltage` | AC voltage L1 (V) |
| `/Ac/Frequency` | Grid frequency (Hz) |
| `/Ac/Energy/Forward` | Total yield (kWh, interpolated) |
| `/Ac/MaxPower` | Max apparent power from inverter settings (W) |
| `/Ac/L1/Voltage`, `Current`, `Power`, `Energy/Forward` | Phase L1 |
| `/Ac/L2/...`, `/Ac/L3/...` | Phase L2, L3 |
| `/Dc/0/Voltage`, `Current`, `Power` | DC tracker 1 (PV1) |
| `/Dc/1/Voltage`, `Current`, `Power` | DC tracker 2 (PV2) |
| `/Dc/2/Voltage`, `Current`, `Power` | DC tracker 3 (PV3, only if 3 strings) |
| `/StatusCode` | Inverter status (Venus OS codes) |
| `/ProductName` | Auto-detected from inverter |
| `/Serial` | Serial number from inverter settings |
| `/CustomName` | Editable name (writable) |
| `/FirmwareVersion` | Inverter firmware version |






