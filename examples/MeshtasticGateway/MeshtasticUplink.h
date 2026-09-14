// A HivewireUplink built on the Meshtastic client API.
//
// This file is the ONLY place Hivewire touches Meshtastic. The library core
// never includes it. Swap this for a LoRaWAN, cellular or satellite
// implementation and the swarm protocol is untouched.
//
// Requires the Meshtastic node's Serial Module set to PROTO mode at 115200
// with rxd/txd pointed at the pins passed to the constructor. PROTO exposes
// the full protobuf client API -- the same one the phone app speaks -- which
// is what lets us pick a channel per message and, crucially, SEE which channel
// an inbound message arrived on.
//
// That last part is the security property. The Serial Module's TEXTMSG mode
// publishes on the primary channel only and never reports the sender's
// channel, so anyone in radio range could issue commands. Here we refuse
// anything that did not arrive on the private channel.
//
// SPDX-License-Identifier: Apache-2.0

#pragma once
#include <Hivewire.h>
#include <Meshtastic.h>

// The library's callbacks are plain C function pointers with no user data, so
// the active instance is held here. One uplink per sketch.
class MeshtasticUplink;
static MeshtasticUplink *g_mtUplink = nullptr;

// Reaching into the library's internals, deliberately. handle_config_complete_id()
// nulls this pointer the moment a handshake completes and then calls it without
// a null check if any later config_complete arrives:
//
//     want_config_id = 0;
//     node_report_callback(NULL, MT_NR_DONE);
//     node_report_callback = NULL;
//   } else {
//     node_report_callback(NULL, MT_NR_INVALID);   // no null check
//
// On RISC-V that is an instruction fetch at 0x0 -- an immediate panic and
// reboot, not a soft failure. It fires whenever the node sends a config dump we
// did not ask for, which a node does every time IT reboots. So the gateway
// reboots too, mid-sentence, spraying a partial UART frame at the node on the
// way down; that desyncs the node's own frame parser and the link stays dead
// until something power-cycles it. One crash, two dead ends.
//
// Re-arming through mt_request_node_report() would transmit another want_config
// and start a handshake loop, so set the pointer directly instead. It has
// external linkage, so no library patch is needed.
extern void (*node_report_callback)(mt_node_t *, mt_nr_progress_t);

// Same reasoning, second defect. mt_protocol_check_packet() has two paths that
// give up on the 512-byte receive buffer without clearing it:
//
//     if (payload_len > PB_BUFSIZE) { ...; return; }          // never resets
//     if (payload_len + 4 > pb_size) { delay(25); return; }   // waits for more
//
// Both leave pb_size where it was. mt_loop() then offers the reader only
// PB_BUFSIZE - pb_size bytes of space, so once the buffer is full of a frame
// that can never be completed, no further byte is ever read and no packet is
// ever parsed again. The link looks alive -- we keep transmitting, the node
// keeps accepting our heartbeats -- but nothing inbound arrives, permanently,
// until a reboot. That is exactly the failure this gateway was showing: it
// received for the first minute after boot and was deaf from then on.
//
// pb_size has external linkage, so the stall is both observable and clearable.
extern size_t pb_size;

class MeshtasticUplink : public HivewireUplink {
 public:
  MeshtasticUplink(int8_t rxPin, int8_t txPin, uint8_t channelIndex,
                   uint32_t baud = 115200)
      : _rx(rxPin), _tx(txPin), _ch(channelIndex), _baud(baud) {}

  bool begin() override {
    g_mtUplink = this;
    _startedAt = millis();
    // A want_config reply is ~2.7 kB arriving back to back at 115200. The
    // library reads the port only once per mt_loop() and sleeps 25 ms inside
    // its own parser whenever a packet is incomplete, so the default 256-byte
    // driver buffer overruns and drops bytes mid-dump. Must precede begin(),
    // which mt_serial_init() calls.
    Serial1.setRxBufferSize(RX_BUFFER_BYTES);
    mt_serial_init(_rx, _tx, _baud);
    set_text_message_callback(&MeshtasticUplink::onText);
    // Register these purely as liveness evidence: any inbound packet, of any
    // kind, proves the node still regards us as a client. Without them we
    // would only notice traffic we happen to care about.
    set_portnum_callback(&MeshtasticUplink::onPortnum);
    set_encrypted_callback(&MeshtasticUplink::onEncrypted);
    handshake();
    return true;
  }

  void loop() override {
    // Before parsing anything: never let the library hold a null report
    // callback (see the note above the extern). Cheap, and it has to happen on
    // every pass because the library re-nulls it after each completed
    // handshake.
    if (node_report_callback == nullptr)
      node_report_callback = &MeshtasticUplink::onConnected;

    // Note this is NOT a health signal: in serial mode mt_serial_loop() is
    // `return true;` unconditionally, so mt_loop() always reports success and
    // tells us nothing about whether the node is listening. Transmission does
    // not depend on a session either -- mt_send_text() just writes a frame. So
    // treat this as "transport initialised" and keep sending regardless; a
    // gateway that cannot complete a handshake can still get its telemetry
    // out, and going mute would throw that away too. Whether the node is
    // actually feeding us packets is tracked separately, below.
    _ready = mt_loop(millis());

    // Read the clock AFTER mt_loop, not before. That call can block for over a
    // second on a config dump -- the library sleeps 25 ms for every incomplete
    // packet -- and the callbacks it fires stamp _lastInbound with millis() as
    // of then. A `now` sampled beforehand is therefore older than _lastInbound,
    // and the unsigned subtraction below underflows to ~49 days: the staleness
    // check trips at the precise moment traffic is arriving, which is the one
    // time it must not. Elapsed-time maths has to sample the clock after
    // anything that can move the timestamp it is compared against.
    uint32_t now = millis();
    checkParserStall(now);

    // The node only forwards received packets to a client that has completed a
    // want_config handshake. If the NODE reboots it forgets us -- but our
    // heartbeats keep succeeding, so nothing looks wrong: digests still go out
    // and inbound commands silently vanish. A roof-mounted node WILL reboot on
    // a power blip.
    //
    // Recover on EVIDENCE, not on a timer. Silence for longer than a node
    // normally goes without saying anything means the session is probably
    // gone, so re-handshake then -- fast when it matters, and never firing at
    // all when the link is healthy. A blind periodic refresh either wastes
    // handshakes or leaves you broken for most of its interval.
    if (now - lastInboundAt() >= STALE_AFTER_MS &&
        now - _lastHandshake >= HANDSHAKE_MIN_GAP_MS) {
      // Say WHICH kind of silence this is. "Nothing has arrived" covers two
      // very different faults, and from the log alone they are identical:
      // a genuinely quiet mesh, or a node that has stopped driving the wire.
      // Distinguishing them cost a full day here -- the node went on reporting
      // successful sends into a line its own GPS power-down had pulled low, so
      // every layer above looked healthy. One pin read separates them.
      if (rxLineDead()) {
        Serial.println("[uplink] inbound silent AND rx line is low: "
                       "the node is not driving the wire");
        note("rx line dead, not idle");
      } else {
        Serial.println("[uplink] inbound silent, re-establishing session");
        note("link stale, rehandshake");
      }
      _session = false;
      handshake();
    }
  }
  bool ready() override { return _ready; }

  // Whether the node has actually completed a want_config handshake with us,
  // which is what makes it forward received packets. Distinct from ready():
  // we can transmit without this, but we will never hear anything without it.
  bool sessionUp() const { return _session; }

  void send(const char *line) override {
    if (!_ready) return;
    mt_send_text(line, BROADCAST_ADDR, _ch);
  }

  void onCommand(CommandCallback cb) override { _cb = cb; }

  // Optional: route session events into the caller's diagnostic log, so they
  // can be replayed remotely instead of only reaching a USB cable that nobody
  // can attach without resetting the board.
  static const uint8_t LOG_NOTE_MAX = 40;
  typedef void (*LogFn)(const char *);
  void onLog(LogFn fn) { _log = fn; }

  uint8_t channel() const { return _ch; }

 private:
  // Treat the session as suspect after this much inbound silence. The node
  // sends us its own telemetry and any mesh traffic it hears, so a healthy
  // link is rarely quiet this long.
  static const uint32_t STALE_AFTER_MS      = 90000;   // 90 s
  // Floor between handshakes so a genuinely quiet mesh cannot make us spin.
  static const uint32_t HANDSHAKE_MIN_GAP_MS = 60000;  // 60 s
  // Big enough to hold a whole want_config reply without the driver dropping
  // bytes while the library is asleep in its parser.
  static const size_t   RX_BUFFER_BYTES      = 4096;
  // Unchanging non-zero fill for this long means wedged, not busy.
  static const uint32_t PARSER_STALL_MS      = 3000;
  // ~3 ms of sampling, well over 30 bit times at 115200.
  static const uint8_t  RX_LINE_SAMPLES      = 16;
  static const uint32_t RX_LINE_SAMPLE_US    = 200;

  // Treat boot as the first "inbound" so we do not re-handshake immediately.
  uint32_t lastInboundAt() const {
    return _lastInbound ? _lastInbound : _startedAt;
  }

  // Is the wire itself dead, as opposed to merely quiet?
  //
  // A UART transmitter holds its line HIGH between bytes, so an idle healthy
  // link reads high. Traffic only ever pulls it low briefly -- a byte at 115200
  // lasts ~87 us and always ends with a high stop bit -- so a window spanning
  // many bit times that never once reads high means nobody is driving the line
  // at all. That is a dead peer, not a quiet one.
  //
  // digitalRead() works here even though the pin is muxed to the UART: it reads
  // the pad's input register, which the peripheral does not take away. Sampling
  // is read-only and cannot disturb reception, which is why this does not touch
  // the pull resistors -- a pull-down would tell us more (driven high versus
  // floating high) at the cost of corrupting whatever is arriving.
  bool rxLineDead() const {
    if (_rx < 0) return false;
    for (uint8_t i = 0; i < RX_LINE_SAMPLES; i++) {
      if (digitalRead(_rx)) return false;      // a high: the line is alive
      delayMicroseconds(RX_LINE_SAMPLE_US);
    }
    return true;
  }

  // Watch the library's receive buffer for the deadlock described above. Bytes
  // sitting in it that never resolve into a packet mean the parser is wedged;
  // clearing pb_size drops the unparseable fragment and lets reading resume.
  //
  // Only a stuck level counts as a stall. A buffer that is merely busy changes
  // size constantly as packets are consumed, and a genuinely partial frame
  // completes in milliseconds -- a 512-byte frame takes 45 ms on the wire -- so
  // several seconds of a perfectly unchanging non-zero level is not a slow
  // link, it is a buffer that can no longer move.
  void checkParserStall(uint32_t now) {
    if (pb_size == 0 || pb_size != _lastPbSize) {
      _lastPbSize = pb_size;
      _stallSince = now;
      return;
    }
    if (now - _stallSince < PARSER_STALL_MS) return;
    Serial.printf("[uplink] rx parser wedged at %u bytes, resyncing\n",
                  (unsigned)pb_size);
    char m[LOG_NOTE_MAX];
    snprintf(m, sizeof(m), "rx wedge %u, resync", (unsigned)pb_size);
    note(m);
    pb_size = 0;
    _lastPbSize = 0;
    _stallSince = now;
  }

  void handshake() {
    _lastHandshake = millis();
    // Always pass the callback: the library nulls its stored pointer once a
    // handshake completes, and calls it without a null check if another
    // config_complete arrives. Re-arming it every time avoids that.
    mt_request_node_report(&MeshtasticUplink::onConnected);
  }

  void note(const char *msg) { if (_log) _log(msg); }

  static void markInbound() {
    if (g_mtUplink) g_mtUplink->_lastInbound = millis();
  }

  // Registered purely as liveness evidence. Any inbound packet of any kind
  // proves the node still regards us as a registered client; without these we
  // would only notice the traffic we happen to care about, and a mesh that is
  // simply quiet would look identical to a dead session.
  static void onPortnum(uint32_t from, uint32_t to, uint8_t channel,
                        meshtastic_PortNum port,
                        meshtastic_Data_payload_t *payload) {
    markInbound();
  }

  static void onEncrypted(uint32_t from, uint32_t to, uint8_t channel,
                          meshtastic_MeshPacket_public_key_t pubKey,
                          meshtastic_MeshPacket_encrypted_t *payload) {
    markInbound();   // undecodable by us, but still proof of a live session
  }

  // Log every transition rather than announcing once. A session that drops and
  // comes back is exactly the event worth seeing, and a one-shot flag hides it.
  static void onConnected(mt_node_t *node, mt_nr_progress_t progress) {
    markInbound();
    if (!g_mtUplink || progress == MT_NR_IN_PROGRESS) return;
    bool up = (progress == MT_NR_DONE);
    if (up == g_mtUplink->_session) return;
    g_mtUplink->_session = up;
    Serial.printf("[uplink] session %s\n", up ? "established" : "rejected");
    g_mtUplink->note(up ? "mt session up" : "mt session rejected");
  }

  // Refuse anything that did not arrive on our private channel. This is the
  // check no Serial Module mode could perform.
  static void onText(uint32_t from, uint32_t to, uint8_t channel,
                     const char *text) {
    if (!g_mtUplink) return;
    markInbound();   // liveness first: even a refused message proves the session
    if (channel != g_mtUplink->_ch) {
      Serial.printf("[uplink] REFUSED ch=%u (not swarm channel): %s\n",
                    channel, text);
      char m[LOG_NOTE_MAX];
      snprintf(m, sizeof(m), "refused ch=%u", channel);
      g_mtUplink->note(m);
      return;
    }
    Serial.printf("[uplink] cmd ch=%u from=0x%08lx: %s\n", channel,
                  (unsigned long)from, text);
    if (g_mtUplink->_cb) g_mtUplink->_cb(text);
  }

  int8_t _rx, _tx;
  uint8_t _ch;
  uint32_t _baud;
  bool _ready = false;
  bool _session = false;
  uint32_t _lastHandshake = 0;
  size_t   _lastPbSize = 0;
  uint32_t _stallSince = 0;
  uint32_t _lastInbound = 0;
  uint32_t _startedAt = 0;
  CommandCallback _cb = nullptr;
  LogFn _log = nullptr;
};
