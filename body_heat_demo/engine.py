"""Synthetic pods and the presentation scenario (stands in for Subsystem C).

Thirty wearers: six "pods" (the December prototype count) plus 24 emulated
sources (SAC-03). Every pod measures every 10 s, broadcasts every 1 s, and
decides its own alert from the team's settings. Names never appear in
packets. The scenario is scripted; the presenter can also trigger every
event from the Demo controls screen.

Demo note: the trend window is shortened so an alert appears within a minute
on stage. The real pod uses a 5-min / 31-reading window (Prelim Design C)."""

import random
import threading
import time

from f1_sideline_demo.hub import Rejected

MEASURE_EVERY_S = 10
TREND_WINDOW_S = 60             # demo window (real pod: 300 s)
TREND_LIMIT_C_PER_5MIN = 1.0    # team setting, expressed per 5 min (FR-001)
ABSOLUTE_LIMIT_C = 38.5         # team setting (FR-002)
SESSION_ID = "S-2026-10-22"

FIRST_NAMES = ["Ava", "Ben", "Cora", "Dev", "Ella", "Finn", "Gia", "Hugo", "Iris", "Jon", "Kai", "Lena",
               "Mia", "Noah", "Omar", "Pia", "Quinn", "Rosa", "Sam", "Tess", "Uma", "Vik", "Wes", "Xia",
               "Yara", "Zed", "Ana", "Bo", "Cy", "Dee"]
LAST_NAMES = ["Rivera", "Osei", "Novak", "Kim", "Santos", "Meyer", "Patel", "Brooks", "Lind", "Costa",
              "Haddad", "Okafor", "Ruiz", "Tanaka", "Walsh", "Yusuf", "Adler", "Bauer", "Chen", "Diaz",
              "Evans", "Fiore", "Gupta", "Hale", "Ivers", "Jang", "Kerr", "Lowe", "Moss", "Nash"]
JERSEYS = [2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 33]


def demo_roster():
    roster = []
    for i, j in enumerate(JERSEYS):
        roster.append({"jersey": j, "name": f"{FIRST_NAMES[i][0]}. {LAST_NAMES[i]}",
                       "full_name": f"{FIRST_NAMES[i]} {LAST_NAMES[i]}",
                       "device_id": f"pod-{j:02d}", "participant_id": f"P{j:02d}",
                       "kind": "pod" if i < 6 else "emulated"})
    return roster


class SyntheticPod:
    def __init__(self, device_id, seed, base_skin_c):
        self.device_id = device_id
        self.rng = random.Random(seed)
        self.base_skin_c = base_skin_c
        self.counter = 0
        self.battery_pct = 100.0
        self.silent = False
        self.skin_contact = True
        self.rise_started_at = None     # t when a scripted climb began
        self.rise_cap_c = 1.2
        self._samples = []              # (t, skin_c)
        self._last = None
        self._last_t = -1

    def _skin_c(self, t):
        drift = 0.0
        if self.rise_started_at is not None:
            dt = t - self.rise_started_at
            drift = min(self.rise_cap_c, 0.03 * max(0, dt))        # ~1.8 C/min on stage
        if not self.skin_contact:
            return 27.0 + self.rng.uniform(-0.3, 0.3)              # air-ish reading off the arm
        return self.base_skin_c + drift + self.rng.uniform(-0.04, 0.04)

    def _slope_c_per_5min(self):
        window = [s for s in self._samples if s[0] >= self._samples[-1][0] - TREND_WINDOW_S]
        if len(window) < 3 or (window[-1][0] - window[0][0]) <= 0:
            return 0.0
        n = len(window)
        mx = sum(s[0] for s in window) / n
        my = sum(s[1] for s in window) / n
        num = sum((s[0] - mx) * (s[1] - my) for s in window)
        den = sum((s[0] - mx) ** 2 for s in window)
        return 0.0 if den == 0 else (num / den) * 300.0

    def _decide_alert(self):
        """On-pod rule: absolute from startup; trend needs a full valid window;
        no decisions while off skin (resets the trend window)."""
        if not self.skin_contact or not self._samples:
            return None
        latest = self._samples[-1][1]
        if latest >= ABSOLUTE_LIMIT_C:
            return "absolute"
        if self._samples[-1][0] - self._samples[0][0] >= TREND_WINDOW_S and \
                self._slope_c_per_5min() >= TREND_LIMIT_C_PER_5MIN:
            return "trend"
        return None

    def broadcast(self, t, air_c, humidity_pct):
        if self.silent:
            return None
        self.counter += 1
        self.battery_pct = max(0.0, self.battery_pct - 0.004)
        if t % MEASURE_EVERY_S == 0 or self._last is None:
            skin = self._skin_c(t)
            if self.skin_contact:
                self._samples.append((t, skin))
            else:
                self._samples.clear()
            self._last = {
                "measurement_id": f"{self.device_id}-m{t}",
                "skin_c": round(skin, 2),
                "air_c": round(air_c + self.rng.uniform(-0.2, 0.2), 1),
                "humidity_pct": round(humidity_pct + self.rng.uniform(-1.5, 1.5), 1),
                "battery_pct": round(self.battery_pct, 1),
            }
        pkt = dict(self._last)
        pkt.update({
            "device_id": self.device_id, "session_id": SESSION_ID, "counter": self.counter,
            "motion": round(max(0.0, self.rng.gauss(0.45, 0.2)), 2),   # 1 s motion summary, new ID each second
            "motion_id": f"{self.device_id}-mo{t}",
            "alert": self._decide_alert(),
            "slope_c_per_5min": round(self._slope_c_per_5min(), 2) if self._samples else None,
            "skin_contact": self.skin_contact,
            "tag": "ok",
        })
        return pkt


class Engine:
    """Owns the pods, the clock, the scripted scenario and presenter controls.
    Calls hub.accept for every broadcast and hub.tick once per second."""

    def __init__(self, hub, store, roster, tick_hz=1.0, autoplay=True):
        self.hub, self.store, self.roster = hub, store, roster
        self.by_jersey = {m["jersey"]: m for m in roster}
        self.pods = {}
        self.t = 0
        self.session_started = time.time()
        self.tick_s = 1.0 / tick_hz
        self.autoplay = autoplay
        self.air_c, self.humidity_pct = 31.0, 58.0
        self.log = []                   # presenter-visible event log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        rng = random.Random(22)
        for i, m in enumerate(roster):
            self.pods[m["device_id"]] = SyntheticPod(m["device_id"], seed=100 + i, base_skin_c=round(rng.uniform(35.3, 36.4), 2))
        self.scripted = self._script()

    # -- scripted timeline (session seconds) -----------------------------------
    def _script(self):
        return [
            (5,   "low_battery", 18),
            (10,  "silence", 24),
            (15,  "rise", 11),
            (40,  "detach", 7),
            (110, "attach", 7),
            (150, "replay", 3),
            (152, "altered", 3),
            (154, "unknown", None),
            (170, "plateau", 11),
        ]

    def seed_history(self, minutes=40):
        """Fake back-history so the detail charts look like a real session."""
        rng = random.Random(7)
        start = -minutes * 60
        for m in self.roster:
            pod = self.pods[m["device_id"]]
            for t in range(start, 0, MEASURE_EVERY_S):
                frac = (t - start) / (-start)
                skin = pod.base_skin_c - 0.6 + 0.6 * frac + 0.15 * rng.uniform(-1, 1)
                self.store.add_measurement_set(f"{pod.device_id}-h{t}", pod.device_id, SESSION_ID, {
                    "skin_c": round(skin, 2), "air_c": round(29.5 + 1.5 * frac + rng.uniform(-0.2, 0.2), 1),
                    "humidity_pct": round(60 - 2 * frac + rng.uniform(-1, 1), 1),
                    "motion": round(max(0, rng.gauss(0.4, 0.2)), 2)}, t, t)
        self.store.add_event(0, "session", None, f"session {SESSION_ID} started, {len(self.roster)} wearers", "k.smith")

    # -- presenter controls ------------------------------------------------------
    def control(self, action, jersey=None, user="presenter"):
        with self._lock:
            return self._control(action, jersey, user)

    def _control(self, action, jersey, user):
        m = self.by_jersey.get(jersey) if jersey else None
        pod = self.pods[m["device_id"]] if m else None
        t = self.t
        if action == "rise":
            pod.rise_started_at = t; pod.rise_cap_c = 3.5
        elif action == "plateau":
            pod.rise_cap_c = max(0.0, (pod._samples[-1][1] - pod.base_skin_c) if pod._samples else 0.0)
            pod.rise_cap_c = min(pod.rise_cap_c, 1.0)          # eases back below the setting
        elif action == "recover":
            pod.rise_started_at = None
        elif action == "spike":
            # Tour helper: the pod's next sample reads above the team limit, so the
            # pod itself raises the absolute alert on its next broadcast.
            pod.base_skin_c += 3.0; pod._last = None
        elif action == "unspike":
            pod.base_skin_c -= 3.0; pod._last = None
        elif action == "silence":
            pod.silent = True
        elif action == "resume":
            pod.silent = False
        elif action == "detach":
            pod.skin_contact = False
        elif action == "attach":
            pod.skin_contact = True
        elif action == "low_battery":
            pod.battery_pct = 14.0
        elif action == "replay":
            self._inject(dict(pod._last or {}, **{"device_id": pod.device_id, "session_id": SESSION_ID,
                        "counter": max(0, pod.counter - 5), "motion": 0.1, "alert": None,
                        "skin_contact": True, "tag": "ok"}), t)
        elif action == "altered":
            pkt = pod.broadcast(t, self.air_c, self.humidity_pct) or {}
            pkt["tag"] = "bad"; pkt["skin_c"] = 41.0
            self._inject(pkt, t)
        elif action == "unknown":
            self._inject({k: 0 for k in ("counter", "measurement_id", "skin_c", "air_c", "humidity_pct",
                                         "motion", "battery_pct", "alert", "skin_contact")} |
                         {"device_id": "pod-99", "session_id": SESSION_ID, "tag": "ok"}, t)
        elif action == "storage_fault":
            self.store.set_fault(True)
        elif action == "storage_ok":
            self.store.set_fault(False)
        elif action == "autoplay":
            self.autoplay = not self.autoplay
        else:
            return {"ok": False, "error": f"unknown control {action}"}
        self.log.append({"t": t, "action": action, "jersey": jersey, "user": user})
        return {"ok": True, "t": t, "action": action, "jersey": jersey}

    def _inject(self, pkt, t):
        try:
            self.hub.accept(pkt, t)
            self.log.append({"t": t, "action": "injected packet ACCEPTED (unexpected)", "jersey": None})
        except Rejected as e:
            self.log.append({"t": t, "action": f"hub rejected: {e}", "jersey": None, "user": "hub"})

    # -- clock -------------------------------------------------------------------
    def step(self):
        with self._lock:
            t = self.t
            if self.autoplay:
                for when, action, jersey in self.scripted:
                    if when == t:
                        self._control(action, jersey, "script")
            self.air_c += random.uniform(-0.02, 0.03)
            for dev, pod in self.pods.items():
                pkt = pod.broadcast(t, self.air_c, self.humidity_pct)
                if pkt is None:
                    continue
                try:
                    self.hub.accept(pkt, t)
                except Rejected:
                    pass
            self.hub.tick(t)
            self.t += 1

    def run_forever(self):
        next_t = time.time()
        while not self._stop.is_set():
            self.step()
            next_t += self.tick_s
            time.sleep(max(0.0, next_t - time.time()))

    def start(self):
        threading.Thread(target=self.run_forever, daemon=True).start()

    def stop(self):
        self._stop.set()

    def t_to_iso(self, t):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.session_started + t))


def build_demo(autoplay=True, seed_history=True, analyzer="deterministic", real_provider=False):
    """Wire store + hub + accounts + engine with the fake roster."""
    from .auth import Accounts
    from .hub import DemoHub
    from .store import Store

    store = Store()
    from analysis_scaffold.replay import build_analyzer
    selected = build_analyzer(analyzer, real=real_provider)
    hub = DemoHub(store, SESSION_ID, analyzer=selected)
    hub.analysis_enabled = analyzer != "relay"
    roster = demo_roster()
    accounts = Accounts()
    accounts.add("k.smith", "demo", "staff", "Krista Smith (athletic trainer)")
    accounts.add("z.elkilany", "demo", "staff", "Zane El Kilany (staff)")
    for m in roster:
        consented = m["jersey"] != 33      # jersey 33 never opted in: shows "Not collecting"
        hub.enroll(m["device_id"], consented=consented)
        store.upsert_participant(m["participant_id"], m["full_name"], m["jersey"],
                                 "consented" if consented else "none", "2026-10-20T17:30:00" if consented else None)
        store.assign_device(m["device_id"], m["participant_id"], SESSION_ID, m["kind"])
        accounts.add(f"athlete{m['jersey']:02d}", "demo", "athlete", m["full_name"], device_id=m["device_id"])
    hub.set_alerts_only("pod-03", True)     # jersey 3 asked for alerts-only (FR-013)
    engine = Engine(hub, store, roster, autoplay=autoplay)
    if seed_history:
        engine.seed_history()
    return store, hub, accounts, roster, engine
