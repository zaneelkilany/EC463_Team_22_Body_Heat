"""Hub receiver and live state (Subsystem E) for the demo prototype.

Extends the F1 demo Hub with the full ingest order from the Preliminary
Design (5.1): format -> enrollment -> authentication -> consent -> counter,
then live state first, storage second. Adds rejection counters (SAC-05),
battery and contact flags, acknowledgement that never clears an alert
(AC-F7), resume notices (FR-009), alerts-only mode (FR-013), per-measurement
age separate from transmission age, and a storage queue whose failures raise
a visible fault while reception continues.

Demo note: "authentication" here checks a synthetic tag field in the packet.
The real AEAD construction and byte format are [OPEN] (S1 interface sheet)."""

import queue
import threading

from f1_sideline_demo.hub import Hub, Rejected, NO_SIGNAL_AFTER_S, PodState  # noqa: F401
from .store import StorageFault

RECOVERY_PERIOD_S = 60      # team setting (FR-009): how long "resume" shows after an alert clears
LOW_BATTERY_PCT = 20        # stands in for the B+C voltage threshold (FR-010)
REQUIRED_FIELDS = ("device_id", "session_id", "counter", "measurement_id", "skin_c", "air_c",
                   "humidity_pct", "motion", "battery_pct", "alert", "skin_contact", "tag")


class DemoHub(Hub):
    def __init__(self, store, session_id="S-2026-10-22", analyzer=None):
        super().__init__()
        self.analyzer = analyzer
        self.analysis_flags = {}
        self.analysis_notices = {}
        self.store = store
        self.session_id = session_id
        self.rejections = {"malformed": 0, "unknown": 0, "altered": 0, "no_consent": 0, "replayed": 0}
        self.accepted = 0
        self.acked = {}             # device_id -> (t, user) for the current alert
        self.alerts_only = set()    # device_ids whose temperature is hidden on the staff screen
        self.recovered_at = {}      # device_id -> t when the last alert cleared
        self.measurement_at = {}    # device_id -> t of the last NEW measurement
        self.low_battery = {}       # device_id -> bool
        self.no_signal_flagged = set()
        self.storage_fault = None   # visible fault text, or None
        self._queue = queue.Queue(maxsize=5000)
        self._writer = threading.Thread(target=self._drain, daemon=True)
        self._writer.start()

    # -- ingest ---------------------------------------------------------------
    def accept(self, packet, t):
        # 1. format
        if not isinstance(packet, dict) or any(k not in packet for k in REQUIRED_FIELDS):
            self.rejections["malformed"] += 1
            raise Rejected("malformed packet")
        dev = packet["device_id"]
        # 2. enrollment
        if dev not in self._enrolled:
            self.rejections["unknown"] += 1
            raise Rejected(f"unknown device {dev}")
        # 3. authentication (synthetic tag; real AEAD is [OPEN])
        if packet["tag"] != "ok" or packet["session_id"] != self.session_id:
            self.rejections["altered"] += 1
            raise Rejected("authentication failed")
        # 4. consent
        if not self._enrolled[dev]:
            self.rejections["no_consent"] += 1
            raise Rejected(f"no consent for {dev}")
        # 5. counter
        state = self._pods[dev]
        if packet["counter"] <= state.last_counter:
            self.rejections["replayed"] += 1
            raise Rejected(f"replayed counter {packet['counter']}")

        # 6. live state first
        prev_alert, prev_contact = state.alert, state.skin_contact
        new_measurement = packet["measurement_id"] != state.last_measurement_id
        super().accept(packet, t)
        self.accepted += 1
        state.slope = packet.get("slope_c_per_5min")
        if new_measurement:
            self.measurement_at[dev] = t
        if dev in self.no_signal_flagged:
            self.no_signal_flagged.discard(dev)
            self._event(t, "signal_back", dev, "transmissions resumed")

        # events the store keeps (FR-018)
        if state.alert != prev_alert:
            if state.alert:
                self._event(t, "alert", dev, state.alert)
                self.recovered_at.pop(dev, None)
            else:
                self._event(t, "alert_clear", dev, f"pod cleared {prev_alert}")
                self.recovered_at[dev] = t
                self.acked.pop(dev, None)   # a new alert will need a new acknowledgement
        if state.skin_contact != prev_contact:
            self._event(t, "contact", dev, "on skin" if state.skin_contact else "not on skin")
        low = packet["battery_pct"] <= LOW_BATTERY_PCT
        if low != self.low_battery.get(dev, False):
            self.low_battery[dev] = low
            self._event(t, "battery", dev, "low" if low else "ok")

        self._analyze(packet, t)

        # storage second: never blocks alert forwarding
        if new_measurement:
            self._enqueue(("measurement", packet["measurement_id"], dev, dict(state.reading), t))
        return True

    def _analyze(self, packet, t):
        """Only reached after hub acceptance; failures never stop reception/storage."""
        if self.analyzer is None:
            return
        dev = packet["device_id"]
        events = getattr(self.analyzer, "events", [])
        before = len(events)
        try:
            for flag in self.analyzer.ingest(dict(packet), t):
                self.analysis_flags[flag.device_id] = {
                    "t": flag.t, "kind": flag.kind, "message": flag.message}
        except Exception:
            # Never expose provider/exception text to the UI.
            self.analysis_notices[dev] = {"t": t, "kind": "analysis unavailable",
                                          "message": "analysis unavailable"}
        for event in events[before:]:
            if event["kind"] not in ("analysis unavailable", "budget_exhausted", "model_deferred"):
                continue
            targets = self._pods if event.get("scope") in ("session", "live_safety") else [event["device_id"]]
            for target in targets:
                self.analysis_notices[target] = {k: event[k] for k in ("t", "kind", "message")}

    def tick(self, t):
        """Once a second: no-signal detection (FR-005, 60-65 s window)."""
        for dev in list(self._pods):
            if self._enrolled.get(dev) and self.no_signal(dev, t) and dev not in self.no_signal_flagged \
                    and self._pods[dev].last_accepted_at is not None:
                self.no_signal_flagged.add(dev)
                self._event(t, "no_signal", dev, f"no accepted transmission for {NO_SIGNAL_AFTER_S} s")

    # -- staff actions (all come through authenticated routes) -----------------
    def acknowledge(self, device_id, user, t):
        super().acknowledge(device_id, user, t)
        if self._pods[device_id].alert:
            self.acked[device_id] = (t, user)
        self._event(t, "ack", device_id, f"acknowledged {self._pods[device_id].alert}", user)

    def set_consent(self, device_id, consented, t, user=None):
        super().set_consent(device_id, consented, t)
        self._event(t, "consent", device_id, "consented" if consented else "withdrawn", user)

    def set_alerts_only(self, device_id, on):
        (self.alerts_only.add if on else self.alerts_only.discard)(device_id)

    def is_consented(self, device_id):
        return bool(self._enrolled.get(device_id))

    def measurement_age_s(self, device_id, t):
        at = self.measurement_at.get(device_id)
        return None if at is None else t - at

    def resume_notice(self, device_id, t):
        """FR-009: the wearer's alert cleared and stayed clear for a while."""
        at = self.recovered_at.get(device_id)
        return at is not None and not self._pods[device_id].alert and (t - at) <= RECOVERY_PERIOD_S

    # -- storage queue --------------------------------------------------------
    def _event(self, t, kind, dev, detail, user=None):
        self._enqueue(("event", t, kind, dev, detail, user))

    def _enqueue(self, item):
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.storage_fault = "storage queue full"

    def _drain(self):
        while True:
            item = self._queue.get()
            try:
                if item[0] == "measurement":
                    _, mid, dev, reading, t = item
                    self.store.add_measurement_set(mid, dev, self.session_id, reading, t, t)
                else:
                    _, t, kind, dev, detail, user = item
                    self.store.add_event(t, kind, dev, detail, user)
                if self.storage_fault:
                    self.storage_fault = None
            except StorageFault as e:
                self.storage_fault = f"Storage fault: {e}. Live screen continues; data since fault not saved."

    def flush(self, timeout=2.0):
        """Tests: wait for the writer to catch up."""
        import time
        end = time.time() + timeout
        while not self._queue.empty() and time.time() < end:
            time.sleep(0.01)
