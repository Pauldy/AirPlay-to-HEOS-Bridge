# AirPlay-to-HEOS-Bridge
# AirPlay-to-HEOS Bridge: Setup Guide

This guide walks you through setting up an AirPlay receiver on a Linux machine that re-streams audio to a HEOS speaker (Denon/Marantz wireless speaker, soundbar, or HEOS-built-in AVR). The result: any iPhone, iPad, or Mac on your network can AirPlay to a HEOS device that doesn't natively support AirPlay.

## What you get

- AirPlay 1 receiver advertised on the LAN, named whatever you choose (e.g., "GymBridge")
- Audio streams to a single HEOS speaker chosen at config time
- iPhone/Mac volume slider controls HEOS volume (with a perceptual curve, debounced)
- Track metadata parsing (artist/title/album) — note that HEOS firmware does **not** display third-party metadata for arbitrary stream URLs, so the HEOS app will show "URL Stream" regardless. The bridge logs metadata for diagnostic purposes.
- ~1-2% CPU and ~20 MB RAM on the host
- Latency: ~2-3 seconds (fine for music, useless for video sync)

## What you need

- A Linux box (Ubuntu 22.04+ or Debian 12+ recommended) on the same network as the HEOS speaker, or reachable to it via a router that bridges mDNS between subnets
- A HEOS speaker, soundbar, or AVR that's been set up via the HEOS app on a phone
- Network access between the Linux box and the HEOS speaker on:
  - TCP port 1255 (Linux → HEOS, for CLI commands)
  - TCP port 8123 (HEOS → Linux, for fetching the audio stream — port is configurable)
  - UDP port 5353 (multicast mDNS, both directions, for phone discovery)
  - TCP port 5000 + UDP 6001-6011 (phone → Linux, for AirPlay)

If the HEOS lives on an isolated IoT VLAN, you'll need the gateway to bridge mDNS and to allow the relevant L3 traffic between zones. The bridge's audio HTTP server has to be reachable *from* the HEOS speaker.

## Step 1: Install dependencies

```bash
sudo apt update
sudo apt install -y shairport-sync avahi-daemon python3
```

That's the whole dependency list. The Python script uses only the standard library — no pip packages required.

Verify Avahi is running:

```bash
sudo systemctl enable --now avahi-daemon
systemctl status avahi-daemon
```

Should show `active (running)`.

## Step 2: Disable the bundled shairport-sync service

The apt package auto-starts shairport-sync as a daemon, which will hold port 5000 and prevent us from running our own piped instance. Disable it:

```bash
sudo systemctl stop shairport-sync
sudo systemctl disable shairport-sync
```

Verify nothing's holding port 5000:

```bash
sudo ss -tlnp | grep 5000
# Should output nothing
```

## Step 3: Configure shairport-sync metadata

The bridge needs shairport-sync to write track metadata (artist/title/volume) to a named pipe. Edit the config:

```bash
sudo nano /etc/shairport-sync.conf
```

Find the `metadata` section (it's usually commented out near the bottom). Make it look like this:

```
metadata = {
    enabled = "yes";
    include_cover_art = "no";
    pipe_name = "/tmp/shairport-sync-metadata";
    pipe_timeout = 5000;
};
```

`include_cover_art = "no"` because HEOS won't display third-party art anyway, and the JPEG data bloats the pipe traffic. Save and exit.

## Step 4: Install the bridge script

Copy `airplay_heos_bridge.py` to a stable location:

```bash
sudo cp airplay_heos_bridge.py /opt/airplay_heos_bridge.py
sudo chmod +x /opt/airplay_heos_bridge.py
```

Quick smoke test that Python can parse it:

```bash
python3 /opt/airplay_heos_bridge.py --help
```

You should see a usage message with four subcommands: `discover`, `probe`, `list-players`, `bridge`.

## Step 5: Find your HEOS speaker

You need two things: the IP address of the HEOS speaker, and (if you have multiple HEOS devices) the player ID for the one you want to target.

**Option A — Discover automatically:**

```bash
python3 /opt/airplay_heos_bridge.py discover
```

This SSDP-scans the LAN. HEOS devices will be tagged `[HEOS]`. Note their IP addresses.

**Option B — Look in the HEOS app:**

Open the HEOS app on your phone → Settings → My Devices → tap the speaker → look for "IP Address."

**Option C — Use avahi-browse:**

```bash
avahi-browse -art 2>/dev/null | grep _heos-audio._tcp
```

This shows all HEOS devices by name. Re-run with `-r` to get IPs.

Once you have an IP, list all players on that HEOS account:

```bash
python3 /opt/airplay_heos_bridge.py list-players --heos-ip <HEOS_IP>
```

Expected output:

```
Connecting to HEOS CLI at 192.168.1.42:1255 …
  pid=273282473  name='Gym'  model=HEOS 5  ip=192.168.1.42  net=wifi
  pid=918273645  name='Kitchen'  model=HEOS 3  ip=192.168.1.43  net=wifi
```

Pick the player you want to AirPlay to and note its `pid`. If there's only one player, you can skip `--pid` when running the bridge.

## Step 6: First manual test

Before wiring up systemd, run the bridge by hand to make sure everything works:

```bash
shairport-sync -o stdout -M -a "GymBridge" -- | python3 /opt/airplay_heos_bridge.py bridge --heos-ip <HEOS_IP>
```

Replace `<HEOS_IP>` with the actual IP. If you have multiple players, add `--pid <PID>`.

Flag breakdown:
- `-o stdout` — output backend writes PCM to stdout
- `-M` — enable metadata pipe (uses the config file's settings)
- `-a "GymBridge"` — name shown in the iPhone's AirPlay picker
- `--` — separates shairport options from backend options (none here, but harmless)

You should see output like:

```
Using HEOS player pid=273282473 ('Gym')
Serving live audio at http://192.168.1.50:8123/stream.wav
Metadata bridge active (volume_curve=perceptual, forward_tracks=False)
Streaming until interrupted…
  HTTP GET /stream.wav from 192.168.1.42 icy=no
```

Now on your iPhone:

1. Open Control Center → tap and hold the audio card → tap the AirPlay icon
2. Look for "GymBridge" in the speaker list (it'll be in the regular AirPlay section, not AirPlay 2 groups)
3. Tap it and start playing music

Audio should come out of the HEOS speaker within a couple of seconds. Drag the volume slider on your phone — you should see `vol -> HEOS level=N` in the bridge output and hear the volume change.

When a track changes, you'll see:

```
  track -> 'Artist' / 'Song' / 'Album' (icy='Artist - Song')
```

Ctrl-C to stop when you're satisfied.

## Step 7: Wire up systemd

Create the service unit:

```bash
sudo nano /etc/systemd/system/airplay-heos-bridge.service
```

Paste this, substituting your `<HEOS_IP>` and chosen name:

```ini
[Unit]
Description=AirPlay to HEOS bridge
After=network-online.target avahi-daemon.service
Wants=network-online.target

[Service]
Type=simple
User=shairport-sync
Group=shairport-sync
ExecStart=/bin/sh -c '/usr/bin/shairport-sync -o stdout -M -a "GymBridge" | /usr/bin/python3 /opt/airplay_heos_bridge.py bridge --heos-ip <HEOS_IP>'
Restart=always
RestartSec=10
StartLimitBurst=3
StartLimitIntervalSec=60

[Install]
WantedBy=multi-user.target
```

If you need a specific player ID, add ` --pid <PID>` to the end of the python3 invocation before the closing quote.

The `User=shairport-sync` line uses the unprivileged user the apt package created. That user already has access to the audio session permissions shairport-sync needs.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now airplay-heos-bridge
journalctl -u airplay-heos-bridge -f
```

You should see the same startup output as in Step 6. Test AirPlay from your phone again to confirm it works under systemd.

## Step 8: Firewall rules (if applicable)

If you run UFW or firewalld on the Linux box, allow:

```bash
sudo ufw allow 5353/udp           # mDNS
sudo ufw allow 5000/tcp           # AirPlay RTSP
sudo ufw allow 6001:6011/udp      # AirPlay RTP (audio data)
sudo ufw allow 8123/tcp           # Our HTTP server (HEOS pulls audio)
```

If the HEOS speaker is on a different VLAN/subnet, your router's firewall needs to allow:

- HEOS subnet → Linux subnet on TCP 8123 (so the speaker can fetch the audio stream)
- Linux subnet → HEOS subnet on TCP 1255 (so the bridge can send CLI commands)
- mDNS (UDP 5353) reflected between phone subnet and Linux subnet (so the iPhone discovers GymBridge)

mDNS reflection is router-specific. UniFi calls it "mDNS service," OPNsense/pfSense have an `avahi` package, MikroTik calls it "mDNS Repeater," OpenWRT uses `umdns` or `avahi-daemon`.

## Step 9: Verify on reboot

```bash
sudo reboot
```

After reboot, wait 30 seconds, then:

```bash
systemctl status airplay-heos-bridge
journalctl -u airplay-heos-bridge --since "1 minute ago"
```

Should show `active (running)` and the same startup messages. Test AirPlay one more time to confirm.

## Day-to-day operations

**Bridge logs:**
```bash
journalctl -u airplay-heos-bridge -f
```

**Restart after config changes:**
```bash
sudo systemctl restart airplay-heos-bridge
```

**Check what shairport-sync sees:**
```bash
sudo timeout 10 cat /tmp/shairport-sync-metadata | xxd | head -50
```

(While AirPlaying — should show streaming metadata items.)

**Verify HEOS connection from the Linux box:**
```bash
{ printf 'heos://player/get_play_state?pid=<PID>\r\n'; sleep 1; } | nc -q1 <HEOS_IP> 1255
```

Should return JSON showing the player's current state (play/pause/stop).

## Troubleshooting

**"GymBridge" doesn't appear in the iPhone's AirPlay list**
- Check `avahi-browse -art 2>/dev/null | grep -i gymbridge` from any Linux box on the phone's subnet. If it doesn't show up, mDNS reflection between subnets isn't working — fix the router config, or move the phone onto the same subnet as the Linux box for testing.
- Make sure no other shairport-sync instance is holding port 5000: `sudo ss -tlnp | grep 5000`. There should be exactly one (ours).

**iPhone connects to AirPlay but no sound from HEOS**
- Check the journal: `journalctl -u airplay-heos-bridge -f` while AirPlaying. You should see `HTTP GET /stream.wav from <HEOS_IP>` within a second of music starting.
- If you don't see that GET, the HEOS speaker can't reach your Linux box on port 8123. Test from another machine on the same subnet as the speaker: `curl -I http://<linux-ip>:8123/stream.wav` — should return HTTP 200.
- If the GET appears but audio is silent, check whether you're hearing the right speaker (HEOS app → which player is playing?).

**Audio plays but volume slider on phone doesn't affect HEOS**
- Check the journal for `vol -> HEOS level=N` lines as you drag. If they appear, the bridge is sending commands but HEOS isn't applying them — check the HEOS app's "Audio" settings on the speaker.
- If they don't appear, the metadata pipe isn't being read. Confirm: `ls -l /tmp/shairport-sync-metadata` should show a FIFO (starts with `p` in the permission column).

**"Failed to kill control group" warnings on restart**
- Harmless. It's a systemd/cgroup quirk specific to certain kernel versions. Ignore it.

**Bridge keeps restarting**
- `journalctl -u airplay-heos-bridge -n 100` to see why. Most common cause: HEOS speaker briefly unreachable at startup. The `RestartSec=10` and `StartLimitBurst=3` settings keep it from looping forever.

**HEOS shows "URL Stream" instead of track name**
- This is a HEOS firmware limitation, not a bridge bug. HEOS does not display metadata for stream URLs that aren't from its own music-service catalog. The bridge logs the correct metadata; HEOS just won't show it. Live with it, or skip ahead to "Future improvements."

## Future improvements you can explore

These weren't built because they're either firmware-dependent (uncertain payoff) or pure quality-of-life:

1. **DIDL-Lite via UPnP** — instead of using the HEOS CLI, send `SetAVTransportURI` with embedded DIDL-Lite XML metadata. Might trigger HEOS's "now playing" display. The bridge has `--use-upnp` mode as a starting point.
2. **Auto-resume after pause** — if AirPlay pauses for several minutes, the HEOS speaker may disconnect from the stream. Resuming AirPlay would need to re-issue `play_stream`. Currently you have to re-tap the AirPlay picker.
3. **Auto-exit on shairport-sync death** — make the Python script detect EOF on stdin and exit nonzero so systemd restarts the whole pipeline cleanly.
4. **Two-way volume sync** — currently the phone's slider drives HEOS, but adjusting volume on the HEOS app or speaker doesn't update the phone. Requires subscribing to HEOS change events.
5. **AirPlay 2** — requires building shairport-sync 4.x from source plus the `nqptp` daemon. Gives you lower latency, lossless ALAC, and multi-room AirPlay groups.

## Uninstall

```bash
sudo systemctl disable --now airplay-heos-bridge
sudo rm /etc/systemd/system/airplay-heos-bridge.service
sudo rm /opt/airplay_heos_bridge.py
sudo apt remove shairport-sync       # optional; keeps avahi-daemon for the rest of the system
```

