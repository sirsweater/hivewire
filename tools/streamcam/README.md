# A plant on Twitch that viewers water

Three pieces, each doing one job:

| Piece | Runs on | Does |
|---|---|---|
| **GrowNode** (`examples/SoilNode`, `-DHW_WITH_PUMP=1 -DHW_WITH_LIGHT=1`) | an ESP32-C6 at the plant | soil probe, pump, float switch, grow light on its own schedule |
| **Stream page** (`tools/hive_admin/streamplant.py`, `twitch.py`) | the hive's host (the admin) | turns `!water`, channel points and Bits into doses, inside the node's limits; serves the overlay text |
| **streamcam** (`tools/streamcam/streamcam.py`) | any machine with ffmpeg and upload bandwidth | camera → overlay → Twitch; a snapshot for the admin page |

Nothing from Twitch reaches the node except through the admin's queue, and
nothing viewers type reaches the chat model.

## 1. The node

Wiring on a C6 SuperMini (see `examples/SoilNode/SoilNode.ino` for the rest):

| What | Pin |
|---|---|
| Pump: logic-level MOSFET module input | GPIO4 |
| Float switch in the reservoir, to GND | GPIO5 |
| Grow light: MOSFET module input (USB or 12 V lamp, in its ground wire), or a relay box's low-voltage input for a mains lamp | GPIO7 |
| Soil probe AOUT / VCC | GPIO0 / GPIO18 |

Build the GrowNode image (`python3 tools/build_images.py`) and flash it from the
admin's Flash page. Then, on the node's page:

- calibrate the probe (Settings: dry and wet points) and the pump (Calibrate:
  run 30 s into a measuring cup, then set Pump flow);
- set **Max per watering** and **Max per 24 h** for what the pot and reservoir
  can take: they are the last word on every viewer's dose;
- set the float switch mode, so an empty reservoir refuses instead of running
  the pump dry;
- the light runs 06:00–22:00 by default; change **Lights on at / off at**
  (type times like `07:00`). The admin sets the node's clock by itself.

A pot standing above its own reservoir (on a riser in a deep drip tray, the
pump drawing from the tray) recirculates: what drains comes back, and a stuck
pump can't flood the room. Keep the pot's base above the highest level the
tray can reach once the pot has drained, and put a screen on the pump intake.

## 2. The Stream page (the admin)

1. **The plant**: pick the GrowNode, turn viewer watering on, and queue a test
   watering. It goes through the same queue and checks as a viewer's.
2. **Twitch account**: at dev.twitch.tv/console register an application (any
   name, OAuth redirect `http://localhost`, category Other, client type
   **Public**), paste its client ID, and link. The page shows a code to type
   at twitch.tv/activate; no password or secret is typed into the admin.
3. **Rules**: the chat command and its amounts and waits, the channel-point
   reward (create it from the page once the channel is an Affiliate), the Bits
   rate, and the daily amount of free water.
4. **Streaming box**: make an overlay key; the address shown goes into
   streamcam's config. Put streamcam's snapshot address here to see the
   camera on the Stream page and on the plant's own page.

Channel points and Bits need a Twitch Affiliate channel; the chat command
works on any channel.

## 3. The camera (Luckfox Pico Max)

The Pico Max has Ethernet and no Wi-Fi: run a cable to it, or add a USB Wi-Fi
adapter its image supports. Its stock Buildroot image serves the camera over
RTSP, usually at `rtsp://<board>/live/0` (the main stream) and `/live/1`
(a smaller one). Check what it sends from the streaming box:

    ffprobe -rtsp_transport tcp rtsp://<board>/live/0

If the main stream is H.265 at its full resolution and decoding it loads the
streaming box, use the smaller stream, or set the image's main stream to H.264
at 1920x1080 or 1280x720 in its settings (see your image's documentation).
A camera looking straight down usually needs turning: set `"transform"` to
`"transpose=1"` (90° clockwise), `"transpose=2"` (counter-clockwise) or
`"hflip,vflip"` (180°).

## 4. streamcam

Needs Python 3 and ffmpeg 6 or later (the static builds work). With an NVIDIA
GPU it encodes with NVENC and leaves the CPU for decoding the camera.

    mkdir -p ~/.config/streamcam
    cp tools/streamcam/streamcam.example.json ~/.config/streamcam/streamcam.json
    # edit: camera address, the overlay address from the Stream page
    python3 tools/streamcam/streamcam.py --config ~/.config/streamcam/streamcam.json --check

Until it has a stream key it runs in **preview**: everything but the upload,
so the snapshot on the admin page shows the overlay on the camera before
anything goes public. To go live, put the stream key (Twitch Creator
Dashboard → Settings → Stream) in a file only you can read:

    umask 077; cat > ~/.config/streamcam/twitch_key     # paste the key, then Ctrl-D

and restart it. `streamcam.service` runs it as a systemd user service.

What it does when things go wrong:

- **camera unreachable at start**: a "camera offline" slate with the overlay,
  checking for the camera every 15 s and switching back when it answers;
- **camera lost mid-stream**: ffmpeg can wait forever on a silent RTSP
  session, so no new frame for 25 s restarts it, onto the slate;
- **the hive unreachable**: the video carries on with the last overlay text;
- **ffmpeg exiting**: restarted, backing off to a minute between tries.

`http://<box>:8787/status.json` says which of those it is in.

## Embedding the stream

Twitch's player embeds on any HTTPS page with an `iframe`, naming the page's
own domain as `parent`:

    <iframe src="https://player.twitch.tv/?channel=CHANNEL&parent=YOUR.DOMAIN&muted=true"
            allowfullscreen width="640" height="360"></iframe>

## Not yet done on hardware

Built and tested against the admin's simulated swarm, a local RTSP/RTMP
server standing in for the camera and Twitch, and a stand-in EventSub server.
The first run with a real C6, Luckfox and Twitch account is still to come.
