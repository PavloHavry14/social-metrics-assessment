# ADB Commands for Device Compliance Checks (Challenge 05)

## 1. Language / Region

### Properties (vary by Android version)

```bash
# Android 8 (API 26) and older -- uses split properties
adb shell getprop persist.sys.language    # e.g., "en"
adb shell getprop persist.sys.country     # e.g., "US"

# Android 9+ (API 28+) -- uses unified BCP-47 locale tag
adb shell getprop persist.sys.locale      # e.g., "en-US"

# Factory default / ROM-baked locale (read-only, never changes)
adb shell getprop ro.product.locale       # e.g., "en-US"

# Older fallbacks (some ROMs)
adb shell getprop ro.product.locale.language  # e.g., "en"
adb shell getprop ro.product.locale.region    # e.g., "US"
```

### Resolution order (how Android resolves the active locale)

1. `persist.sys.locale` -- if non-empty, this wins
2. `persist.sys.language` + `persist.sys.country` -- legacy fallback
3. `ro.product.locale` -- factory default if nothing else is set

### Where properties can disagree

- `persist.sys.language`/`persist.sys.country` may be STALE on Android 9+ devices. The system writes `persist.sys.locale` and stops updating the legacy split properties. Checking only the legacy props gives wrong results.
- `ro.product.locale` reflects the ROM factory setting, NOT the user's chosen locale. A device manufactured for Japan (`ro.product.locale=ja-JP`) with user-set English will show `persist.sys.locale=en-US` but `ro.product.locale=ja-JP`.

### Android version differences

| Android Version | Primary Property | Legacy Properties Available? |
|----------------|-----------------|----------------------------|
| 8 (Oreo) | `persist.sys.language` + `persist.sys.country` | Yes (primary) |
| 9 (Pie) | `persist.sys.locale` | Yes (stale/ignored) |
| 10 | `persist.sys.locale` | Yes (stale/ignored) |
| 11 | `persist.sys.locale` | Yes (stale/ignored) |

---

## 2. Timezone

```bash
# Get current timezone (Olson/IANA format)
adb shell getprop persist.sys.timezone          # e.g., "America/New_York"

# Alternative (settings database)
adb shell settings get global time_zone          # e.g., "America/New_York"

# Check if auto-timezone is enabled (1=auto, 0=manual)
adb shell settings get global auto_time_zone     # "1" or "0"

# Check if auto-time is enabled
adb shell settings get global auto_time          # "1" or "0"
```

### Compliance check logic

If `auto_time_zone=1`, the device timezone is network-provided and can change. For compliance, you may want `auto_time_zone=0` with a specific timezone set, OR verify the auto-detected timezone matches the expected region.

---

## 3. GPS / Location

### Location mode

```bash
# Get location mode (Android 8-9)
adb shell settings get secure location_mode
```

Values:
| Value | Mode | Description |
|-------|------|-------------|
| 0 | Off | Location completely disabled |
| 1 | Sensors only | GPS only, no network location |
| 2 | Battery saving | Network/WiFi location only, no GPS |
| 3 | High accuracy | GPS + Network + WiFi |

**Note:** On Android 10+, `location_mode` is simplified. Only values 0 (off) and 3 (on) are used. The intermediate modes were removed.

### Location providers (Android 8-9)

```bash
# Check which providers are enabled
adb shell settings get secure location_providers_allowed
# Returns comma-separated list: "gps,network" or "gps" or ""
```

### App-level location permissions

```bash
# Check runtime permissions granted to an app
adb shell dumpsys package <package_name> | grep -A 20 "runtime permissions"

# Check via appops (more granular, includes background access)
adb shell cmd appops get <package_name>

# Specifically check location-related appops
adb shell cmd appops get <package_name> android:coarse_location
adb shell cmd appops get <package_name> android:fine_location

# Check background location (Android 10+)
adb shell cmd appops get <package_name> android:mock_location

# Grant/revoke for testing
adb shell pm grant <package_name> android.permission.ACCESS_FINE_LOCATION
adb shell pm grant <package_name> android.permission.ACCESS_BACKGROUND_LOCATION
```

### Android version differences for location

| Android | location_mode values | Background location |
|---------|---------------------|-------------------|
| 8 | 0, 1, 2, 3 | No separate permission |
| 9 | 0, 1, 2, 3 | No separate permission |
| 10 | 0 or 3 only | ACCESS_BACKGROUND_LOCATION introduced |
| 11 | 0 or 3 only | Background location requires separate grant |

---

## 4. WiFi

```bash
# Check if WiFi is enabled (1=on, 0=off)
adb shell settings get global wifi_on

# Get WiFi connection status and details
adb shell dumpsys wifi | grep "mWifiInfo"

# List saved/configured WiFi networks
adb shell dumpsys wifi | grep "WifiConfiguration"

# Alternative: list networks via wpa_cli (requires root on most devices)
adb shell wpa_cli list_networks

# Get current SSID
adb shell dumpsys wifi | grep "SSID"

# Get WiFi MAC address
adb shell cat /sys/class/net/wlan0/address

# Full WiFi state dump (verbose)
adb shell dumpsys wifi

# WiFi config file location (Android 10+)
# /data/misc/apexdata/com.android.wifi/WifiConfigStore.xml
# (requires root to read)
```

---

## 5. IP Validation

```bash
# Get external/public IP from device
adb shell curl -s ifconfig.me
adb shell curl -s icanhazip.com
adb shell curl -s api.ipify.org

# Get local/internal IP
adb shell ip addr show wlan0
adb shell ifconfig wlan0

# Get IP via dumpsys
adb shell dumpsys connectivity | grep "NetworkAgentInfo"
```

**Note:** `curl` may not be available on all Android devices. Alternatives:
```bash
# Using wget (more commonly available)
adb shell wget -qO- ifconfig.me

# Using toybox/busybox
adb shell busybox wget -qO- ifconfig.me
```

---

## 6. SIM / MCC (Mobile Country Code)

### THE STALE MCC TRAP

**CRITICAL: You MUST check SIM state BEFORE reading MCC/MNC values.**

```bash
# STEP 1: Check SIM state FIRST
adb shell getprop gsm.sim.state
```

Possible values:
| Value | Meaning |
|-------|---------|
| `READY` | SIM is present and initialized -- MCC is valid |
| `ABSENT` | No SIM inserted -- MCC is STALE CACHED VALUE |
| `NOT_READY` | SIM is initializing -- MCC may be stale |
| `PIN_REQUIRED` | SIM locked -- MCC may be partially available |
| `PUK_REQUIRED` | SIM locked -- MCC may be partially available |
| `NETWORK_LOCKED` | SIM network locked |
| `UNKNOWN` | State unknown |

```bash
# STEP 2: Only if gsm.sim.state == "READY", read MCC/MNC
adb shell getprop gsm.sim.operator.numeric     # e.g., "310260" (MCC=310, MNC=260)
adb shell getprop gsm.sim.operator.alpha        # e.g., "T-Mobile"
adb shell getprop gsm.operator.numeric          # Current network MCC/MNC (may differ when roaming)
adb shell getprop gsm.operator.alpha            # Current network name

# SIM-specific (home network)
adb shell getprop gsm.sim.operator.iso-country  # e.g., "us"
```

### Why this matters

When a SIM card is removed, Android does NOT clear `gsm.sim.operator.numeric`. It retains the **last known value**. This means:
- Device has a US SIM (MCC=310), user removes SIM
- `gsm.sim.state` changes to `ABSENT`
- `gsm.sim.operator.numeric` STILL returns `310260`
- If you check MCC without checking SIM state first, you get a **false positive** for US presence

### Dual SIM devices

```bash
# SIM slot 1
adb shell getprop gsm.sim.state
adb shell getprop gsm.sim.operator.numeric

# SIM slot 2
adb shell getprop gsm.sim.state2
adb shell getprop gsm.sim.operator.numeric2

# Alternative for multi-SIM
adb shell getprop gsm.sim.state.0
adb shell getprop gsm.sim.state.1
```

### MCC parsing

The MCC is the first 3 digits of `gsm.sim.operator.numeric`:
- `310260` -> MCC=`310` (USA), MNC=`260` (T-Mobile)
- `234015` -> MCC=`234` (UK), MNC=`015` (Vodafone)
- `639007` -> MCC=`639` (Kenya), MNC=`007` (Safaricom)

---

## 7. Device Name

```bash
# Device hostname (used for network identification)
adb shell getprop net.hostname                    # e.g., "android-abc123def456"

# User-set device name (from Settings > About phone > Device name)
adb shell settings get global device_name         # e.g., "John's Phone"

# Device model info (read-only, ROM-set)
adb shell getprop ro.product.model                # e.g., "Pixel 4"
adb shell getprop ro.product.manufacturer         # e.g., "Google"
adb shell getprop ro.product.brand                # e.g., "google"

# Bluetooth name (may differ from device name)
adb shell settings get secure bluetooth_name      # e.g., "John's Pixel"
```

### Notes

- `net.hostname` is often auto-generated from the device serial and may not reflect the user-facing name.
- `device_name` in global settings is the user-visible name set in Settings UI.
- On some Samsung devices, the setting is `settings get system device_name` instead of `global`.

---

## 8. Screen Lock

```bash
# Check lock screen password type
adb shell settings get secure lockscreen.password_type
```

Values for `lockscreen.password_type`:
| Value | Lock Type |
|-------|-----------|
| 0 | None |
| 65536 | Slide (swipe to unlock, no security) |
| 131072 | Pattern |
| 196608 | PIN (numeric) |
| 262144 | Password (alphanumeric) |
| 327680 | Password (complex) |

```bash
# Alternative: check if lock is disabled
adb shell locksettings get-disabled               # "true" or "false"

# Check if lock screen is currently shown
adb shell dumpsys window | grep "mDreamingLockscreen"
adb shell dumpsys window | grep "isStatusBarKeyguard"

# Check lock settings database directly (requires root)
adb shell sqlite3 /data/system/locksettings.db "SELECT * FROM locksettings"
```

### Android version differences

- Android 8-9: `lockscreen.password_type` is reliable
- Android 10+: Some OEMs moved to `locksettings` service; `lockscreen.password_type` may return `null`
- The `locksettings` command-line tool is more reliable on newer Android versions

---

## 9. USB Debugging

```bash
# Check if ADB/USB debugging is enabled (1=enabled, 0=disabled)
adb shell settings get global adb_enabled

# Check developer options state
adb shell settings get global development_settings_enabled   # 1=enabled

# Check if ADB over WiFi is enabled
adb shell getprop service.adb.tcp.port              # -1 or empty = disabled, port number = enabled
```

**Ironic note:** If you can run these ADB commands, USB debugging is obviously enabled. This check is more useful when querying via an on-device agent that can check programmatically, or for audit logging.

---

## 10. App State

### Check if app is installed

```bash
# List all packages matching a pattern
adb shell pm list packages | grep <package_fragment>
# e.g., adb shell pm list packages | grep tiktok

# Check if specific package exists
adb shell pm list packages <full.package.name>
```

### Get app version

```bash
# Get version info
adb shell dumpsys package <package_name> | grep versionName
adb shell dumpsys package <package_name> | grep versionCode

# Example
adb shell dumpsys package com.zhiliaoapp.musically | grep versionName
# versionName=25.0.3
```

### Check app data / shared preferences

```bash
# List shared prefs files (requires root or debuggable app)
adb shell ls /data/data/<package_name>/shared_prefs/
adb shell cat /data/data/<package_name>/shared_prefs/<pref_file>.xml

# For Android 10+ with scoped storage
adb shell ls /data/user/0/<package_name>/shared_prefs/
```

### Check app state (enabled, disabled, force-stopped)

```bash
# Check if app is enabled/disabled
adb shell pm list packages -d | grep <package_name>   # -d = disabled only
adb shell pm list packages -e | grep <package_name>   # -e = enabled only

# Check if app is currently running
adb shell pidof <package_name>

# Get full package info dump
adb shell dumpsys package <package_name>

# Check app permissions
adb shell dumpsys package <package_name> | grep "granted=true"
```

### Common social media package names

| App | Package Name |
|-----|-------------|
| TikTok | `com.zhiliaoapp.musically` |
| TikTok Lite | `com.zhiliaoapp.musically.go` |
| Instagram | `com.instagram.android` |
| Facebook | `com.facebook.katana` |
| Twitter/X | `com.twitter.android` |
| YouTube | `com.google.android.youtube` |
| Snapchat | `com.snapchat.android` |

---

## Quick Reference: All-in-One Compliance Check Script

```bash
#!/bin/bash
# Device compliance snapshot

echo "=== LOCALE ==="
adb shell getprop persist.sys.locale
adb shell getprop persist.sys.language
adb shell getprop persist.sys.country
adb shell getprop ro.product.locale

echo "=== TIMEZONE ==="
adb shell getprop persist.sys.timezone
adb shell settings get global auto_time_zone

echo "=== LOCATION ==="
adb shell settings get secure location_mode

echo "=== WIFI ==="
adb shell settings get global wifi_on

echo "=== PUBLIC IP ==="
adb shell curl -s ifconfig.me 2>/dev/null || echo "curl not available"

echo "=== SIM STATE (CHECK BEFORE MCC) ==="
adb shell getprop gsm.sim.state
adb shell getprop gsm.sim.operator.numeric
adb shell getprop gsm.sim.operator.iso-country

echo "=== DEVICE NAME ==="
adb shell getprop net.hostname
adb shell settings get global device_name

echo "=== SCREEN LOCK ==="
adb shell settings get secure lockscreen.password_type

echo "=== USB DEBUGGING ==="
adb shell settings get global adb_enabled

echo "=== APP CHECK (TikTok) ==="
adb shell pm list packages | grep musically
adb shell dumpsys package com.zhiliaoapp.musically | grep versionName
```

---

## Android Version Compatibility Matrix

| Check | Android 8 | Android 9 | Android 10 | Android 11 |
|-------|-----------|-----------|------------|------------|
| Locale | `persist.sys.language` + `country` | `persist.sys.locale` | `persist.sys.locale` | `persist.sys.locale` |
| Location mode | 0/1/2/3 | 0/1/2/3 | 0 or 3 only | 0 or 3 only |
| Background location | N/A (implicit) | N/A (implicit) | Separate permission | Separate permission + one-time grant |
| `lockscreen.password_type` | Reliable | Reliable | May return null | May return null |
| WiFi config path | `/data/misc/wifi/` | `/data/misc/wifi/` | `/data/misc/apexdata/com.android.wifi/` | `/data/misc/apexdata/com.android.wifi/` |
| SIM props stale trap | Yes | Yes | Yes | Yes |
| `cmd appops` | Available | Available | Available | Available |
| `locksettings` CLI | Available | Available | Available | Available |
