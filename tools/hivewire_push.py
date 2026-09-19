#!/usr/bin/env python3
"""Push a firmware image to the Hivewire swarm over the gateway's USB link.

Speaks the exact protocol MeshtasticGateway.ino's "push" command and
HivewireSerialProvider expect, matching what pushtest.py already proved
against real hardware:

  1. send "push <len> <crc32>\n"
  2. wait for "READY"
  3. answer each "MORE" with up to 1024 bytes of the image
  4. keep echoing every other line the gateway prints (status/log/faults)
     until the transfer visibly finishes or stalls

Run this ON THE PI (it owns /dev/ttyACM3), not on the PC -- that's the
only device with direct USB access to the gateway.

Usage:
    python3 hivewire_push.py /path/to/firmware.bin [/dev/ttyACM3]
"""
import os
import re
import struct
import sys
import time
import zlib
import serial

MORE_RE = re.compile(r"^MORE (\d+) (\d+)$")

SUBCHUNK = 1024
PORT_DEFAULT = "/dev/ttyACM3"
BAUD = 115200


def main():
    # Without this, running over ssh with output redirected makes stdout fully
    # buffered, so nothing appears until the process exits -- which for a
    # ten-minute transfer means no way to watch progress, and no way to tell a
    # stall from slow progress except by opening a second connection to the
    # serial port, which corrupts the transfer.
    sys.stdout.reconfigure(line_buffering=True)

    if len(sys.argv) < 2:
        print("usage: hivewire_push.py <image.bin> [port]", file=sys.stderr)
        sys.exit(1)

    img_path = sys.argv[1]
    port = sys.argv[2] if len(sys.argv) > 2 else PORT_DEFAULT

    with open(img_path, "rb") as f:
        img = f.read()
    crc = zlib.crc32(img) & 0xFFFFFFFF
    print("image: %s (%d bytes, crc32=%d)" % (img_path, len(img), crc))

    # timeout is deliberately tiny. With the old 0.3s, every read(4096) waited
    # the FULL timeout because a "MORE" prompt is only 5 bytes and 4096 never
    # arrive -- so each of the ~12 MORE prompts per window cost up to 300ms of
    # pure measurement overhead before the reply even started. That inflates
    # every transfer-rate number this script produces, which matters when the
    # whole question is whether the gateway or the host is the bottleneck.
    s = serial.Serial(port, BAUD, timeout=0.02)
    time.sleep(0.3)
    s.reset_input_buffer()

    # Prime the line: the gateway's command reader only ever flushes its
    # internal buffer on a newline, and something (still under investigation --
    # possibly the gateway's own status output looping back into its input) can
    # leave a stale, unterminated fragment sitting there from earlier traffic.
    # A bare newline flushes that leftover harmlessly (it won't match any real
    # command) so our actual push command starts on a clean line instead of
    # getting glued onto garbage.
    s.write(b"\n")
    s.flush()
    time.sleep(0.5)
    stale = s.read(4096)
    if stale:
        print("(drained %d stale bytes before sending command: %r)" %
              (len(stale), stale[:120]))
    s.reset_input_buffer()

    cmd = "push %d %d\n" % (len(img), crc)
    print("-> %s" % cmd.strip())
    s.write(cmd.encode())
    s.flush()

    sent = 0
    buf = b""
    t0 = time.time()
    last_activity = time.time()
    armed = False
    done = False

    # A full ~1.1MB transfer has never reliably completed in prior burn
    # testing (see README's "notes from bringing this up" -- suspected RF
    # interference on the ESP-NOW swarm link). It has taken 60-80s of retry
    # patience just to get partway before. Give this real room to either
    # finish or visibly plateau, rather than aborting on the first quiet
    # stretch, which cut off a genuinely-progressing transfer last run.
    # Overridable so the burn harness can cycle faster than an interactive
    # run wants to: a shorter idle timeout costs nothing when the failure
    # mode being measured is "gateway goes silent and never comes back".
    HARD_TIMEOUT = int(os.environ.get("HW_HARD_TIMEOUT", "600"))
    IDLE_TIMEOUT = int(os.environ.get("HW_IDLE_TIMEOUT", "90"))

    last_heartbeat = time.time()
    more_times = []
    high_water = 0     # furthest byte the gateway has asked for
    retries = 0        # subchunks it had to ask for again
    dropped = set()    # offsets damaged on purpose (HW_DROP_EVERY)
    push_ended = False # gateway announced "PUSH END"
    malformed = 0      # prompts ignored because they did not parse

    while time.time() - t0 < HARD_TIMEOUT:
        # Take whatever is already buffered immediately; only block (briefly)
        # when the port is genuinely empty.
        avail = s.in_waiting
        d = s.read(avail if avail else 1)
        if d:
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                last_activity = time.time()
                # Wall-clock stamp so this trace can be lined up against
                # serial captures taken on other machines at the same time.
                print("%s <- %s" % (time.strftime("%H:%M:%S") +
                                    ".%03d" % (int(time.time() * 1000) % 1000),
                                    text), flush=True)

                if text == "READY":
                    armed = True
                elif text == "MORE" or text.startswith("MORE "):
                    # Timestamp every prompt. The provider is asked for a whole
                    # window (12288 B) at once and splits it into 12 x 1024B
                    # MOREs, so prompts arrive in BURSTS: a tight burst is the
                    # USB fill, and the long gap after it is the ESP-NOW
                    # send/poll/repair for that window. Separating the two says
                    # which side owns the ~52s-per-window cost, without
                    # touching the gateway's firmware.
                    more_times.append((time.time() - t0, sent))
                    # A MORE proves the transfer is live regardless of whether
                    # READY was ever seen. On one run the READY line went
                    # missing (the same stray-byte problem that garbles command
                    # echoes on this link), and the old code kept armed=False
                    # through 95KB of successful transfer, then blamed the
                    # stall on "no READY" -- pointing at the wrong failure
                    # entirely. Trust what the gateway is doing, not what it
                    # managed to announce.
                    armed = True
                    # "MORE <offset> <len>" names exactly which bytes the
                    # gateway wants. It re-asks for the SAME offset when a
                    # subchunk arrived short (USB FIFO overrun drops bytes with
                    # no error), so answer from the offset it names -- never
                    # from our own running count, which is what let a single
                    # loss shift the rest of the image by 799 bytes. A bare
                    # "MORE" (older gateway firmware) still means "next bytes".
                    #
                    # STRICT: anything that is not exactly "MORE" or exactly
                    # "MORE <int> <int>" is ignored, never guessed at. The old
                    # fallback treated a malformed prompt as "send the next
                    # bytes"; a prompt garbled by another task's output on the
                    # same port, arriving while the gateway was RE-ASKING an
                    # older offset, then got the wrong 1024 bytes -- full
                    # length, silently accepted, image refused at the end as
                    # "crc bad". Ignoring it costs one gateway timeout and a
                    # re-ask; guessing costs the whole transfer.
                    m = MORE_RE.match(text)
                    framed = bool(m)
                    if m:
                        req_off, n = int(m.group(1)), int(m.group(2))
                        if req_off < high_water:
                            retries += 1
                            print("   RETRY: gateway re-asked offset %d (%d bytes)"
                                  % (req_off, n), flush=True)
                        n = max(0, min(n, len(img) - req_off))
                    elif text == "MORE":
                        req_off = sent
                        n = min(SUBCHUNK, len(img) - sent)
                    else:
                        malformed += 1
                        print("   IGNORED malformed prompt: %r" % text, flush=True)
                        continue
                    if n > 0:
                        # Pace the reply instead of blasting 1024 bytes at
                        # once. Timing showed EVERY prompt was followed by
                        # ~4.4s -- the 4000ms timeout in
                        # HivewireSerialProvider::doFeed -- meaning the
                        # gateway never received the full subchunk and sat
                        # waiting for bytes that never came. That is the
                        # documented USB CDC burst overrun on the receive
                        # side, and it, not RF, is what pinned throughput at
                        # ~232 B/s (1024 bytes per 4.4s timeout).
                        blk = int(os.environ.get("HW_WRITE_BLOCK", "128"))
                        gap = float(os.environ.get("HW_WRITE_GAP", "0.002"))
                        chunk = img[req_off:req_off + n]
                        # Trailer the gateway checks: CRC32 over the offset
                        # (4 LE bytes) and the INTENDED data. Computed before
                        # any fault injection, as a real sender would, so a
                        # damaged transfer is caught by the gateway.
                        trailer = (struct.pack("<I", zlib.crc32(struct.pack("<I", req_off) + chunk)
                                               & 0xFFFFFFFF) if framed else b"")
                        # FAULT INJECTION (burn testing only): every Nth fresh
                        # subchunk, drop 50 bytes out of its MIDDLE -- exactly
                        # what a USB FIFO overrun does -- to prove the gateway
                        # notices, discards the misaligned remainder and
                        # re-asks the same offset. Only the first attempt at
                        # an offset is damaged, so the retry can succeed.
                        drop_every = int(os.environ.get("HW_DROP_EVERY", "0"))
                        if (drop_every and req_off not in dropped
                                and len(chunk) > 200
                                and (req_off // SUBCHUNK) % drop_every == drop_every - 1):
                            dropped.add(req_off)
                            chunk = chunk[:100] + chunk[150:]
                            print("   INJECT: dropped 50 bytes from offset %d" % req_off,
                                  flush=True)
                        wire = chunk + trailer
                        for off in range(0, len(wire), blk):
                            s.write(wire[off:off + blk])
                            s.flush()
                            if gap:
                                time.sleep(gap)
                        high_water = max(high_water, req_off + n)
                        sent = high_water
                    if sent % (SUBCHUNK * 10) == 0 or sent == len(img):
                        print("   progress: %d/%d (%.1f%%) at %ds"
                              % (sent, len(img), 100.0 * sent / len(img),
                                 int(time.time() - t0)), flush=True)
                elif text.startswith("PUSH END"):
                    print("gateway says the transfer is over: %s" % text, flush=True)
                    push_ended = True
                    done = True
                    break
                elif "ERR" in text:
                    print("gateway reported an error, stopping", flush=True)
                    done = True
                    break
        if done:
            break

        idle = time.time() - last_activity

        # Heartbeat so a live tail can tell "stalled" from "slow but moving"
        # without opening a second connection to the same port -- doing that
        # is what corrupted the gateway's command buffer on an earlier run.
        if time.time() - last_heartbeat > 15:
            last_heartbeat = time.time()
            print("   ... %ds elapsed, %d/%d fed, %ds since last line"
                  % (int(time.time() - t0), sent, len(img), int(idle)),
                  flush=True)

        # One idle check covering every phase. The earlier version only caught
        # "never armed" and "armed and fully fed", so a stall PARTWAY through
        # a transfer -- the exact failure this is most likely to hit -- matched
        # neither and spun silently until the hard cap, burning ten minutes and
        # reporting nothing about where it died.
        if idle > IDLE_TIMEOUT:
            if not armed:
                print("no READY after %ds idle, aborting" % IDLE_TIMEOUT,
                      flush=True)
            elif sent >= len(img):
                print("fed entire image, %ds quiet -- assuming done"
                      % IDLE_TIMEOUT, flush=True)
            else:
                print("STALLED: armed, but no line for %ds with %d/%d fed "
                      "(%.1f%%) -- gateway stopped asking for bytes"
                      % (IDLE_TIMEOUT, sent, len(img),
                         100.0 * sent / len(img)), flush=True)
            break

    s.close()
    print("\nfed %d/%d bytes (%s) in %ds, %d USB retries, %d malformed prompts"
          % (sent, len(img), "complete" if sent == len(img) else "INCOMPLETE",
             int(time.time() - t0), retries, malformed), flush=True)

    # --- where did the time actually go? ---
    if len(more_times) > 2:
        gaps = [(more_times[i][0] - more_times[i - 1][0])
                for i in range(1, len(more_times))]
        # A gap above this is the ESP-NOW phase between window fills; below it
        # is back-to-back USB feeding within one window.
        THRESH = 1.0
        fill_gaps = [g for g in gaps if g <= THRESH]
        wait_gaps = [g for g in gaps if g > THRESH]
        print("\n--- MORE timing breakdown (%d prompts) ---" % len(more_times))
        print("within-burst gaps (USB fill): n=%d total=%.1fs median=%.3fs"
              % (len(fill_gaps), sum(fill_gaps),
                 sorted(fill_gaps)[len(fill_gaps) // 2] if fill_gaps else 0))
        print("between-burst gaps (ESP-NOW): n=%d total=%.1fs median=%.1fs"
              % (len(wait_gaps), sum(wait_gaps),
                 sorted(wait_gaps)[len(wait_gaps) // 2] if wait_gaps else 0))
        if wait_gaps:
            print("between-burst gap values: %s"
                  % ", ".join("%.1f" % g for g in wait_gaps[:15]))
        # Prompts per burst tells us the window fill size actually used.
        burst, bursts = 1, []
        for g in gaps:
            if g > THRESH:
                bursts.append(burst); burst = 1
            else:
                burst += 1
        bursts.append(burst)
        print("prompts per burst: %s" % bursts[:15])


if __name__ == "__main__":
    main()
