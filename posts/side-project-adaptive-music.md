---
title: "ESP32 + mmWave Radar: Privacy-First People Counting and Adaptive Music for Retail"
date: 2026-03-28
author: Aniket Maithani
tags: [iot, esp32, python, flask, aniket-pet-project, hardware, retail-tech]
description: "How I built a crowd-adaptive music system for retail using an ESP32, an RD-03E mmWave radar, a Flask state API, and a mood engine that maps crowd density to BPM and genre, without a single camera or microphone."
reading_time: 14
status: published
---

## The Brief

The problem is straightforward. A retail store or cafe wants the music to feel right for the crowd inside at any given moment. Empty store at 10am needs something calm and ambient. Same store at 6pm on a Friday with twenty people needs something with energy.

The naive solution is a camera with computer vision running a headcount. That solution has three problems. It captures identifiable video, which means GDPR compliance, consent signage, data retention policies, and customer unease. It requires a GPU or a cloud API to run inference, which adds latency and ongoing cost. And it fails in bad lighting.

The better solution is a 24GHz mmWave radar sensor. It detects blobs, not faces. It knows something is there, moving, at a certain distance. It cannot tell you who. No PII, no consent requirement, no privacy architecture needed.

This post covers the full implementation: ESP32 firmware that reads the RD-03E radar and POSTs state to a local Flask API, a `catalogue.json` mood engine that maps crowd density to music parameters, and a PySide6 player that adapts in real time. The entire stack runs on a single Raspberry Pi or a cheap laptop inside the venue. No cloud dependency.

---

## Why mmWave Over Every Other Option

Before getting into the implementation, it is worth understanding why mmWave is the right sensor for this use case.

A 24GHz mmWave radar emits radio waves in the millimeter-wave spectrum and measures the reflection. From the time-of-flight and Doppler shift of the reflected signal it can extract presence (is something there), distance (how far), and velocity (how fast it is moving). Some sensors in this class also output X/Y coordinates for tracked targets.

The critical property is that it detects physical presence through micro-motion: breathing, heartbeat, the slight sway of a standing person. It does not require visible movement. A person standing still at a counter is detected reliably. A person sitting at a cafe table is detected reliably.

Compared to the alternatives:

| Sensor           | Counts People           | Device-Independent | Works When Still | Privacy     |
| ---------------- | ----------------------- | ------------------ | ---------------- | ----------- |
| mmWave radar     | Partial (density proxy) | Yes                | Yes              | Full        |
| PIR              | No (presence only)      | Yes                | No               | Full        |
| Camera + CV      | Yes (accurate)          | Yes                | Yes              | None        |
| WiFi probe sniff | Approximate             | No                 | Yes              | Partial     |
| Microphone + dB  | No                      | Yes                | No               | Problematic |
| IR beam break    | Yes (doorway only)      | Yes                | Yes              | Full        |

mmWave is the only option that combines all three properties that matter here: no PII, detects still occupants, and gives you a density signal rather than just binary presence.

The RD-03E (EC Buying, available on Amazon India for around 800 rupees) is a single-zone sensor. It does not track multiple targets individually. What it does give you is presence state, distance to the nearest detected object, and velocity. From those signals, it is possible to build a crowd behaviour fingerprint that serves as a reliable density proxy.

---

## The Density Problem: One Sensor, No Headcount

The RD-03E cannot count people. This needs to be stated clearly because it sets the architecture correctly from the start.

What it can do is detect the behavioural signatures that different crowd sizes produce. One person walking a straight path produces a smooth distance curve, low variance, low flicker rate. Eight people in the same space produce a chaotic distance signal: the sensor locks onto different people at different moments, the distance jumps, presence flickers as people occlude each other, and occupancy stays consistently high.

These signals are computable. The firmware collects a rolling window of radar readings and derives four metrics:

**Occupancy percentage:** what fraction of the last N readings had presence detected. A single person walking through briefly produces low occupancy. A crowd lingering produces high occupancy.

**Distance variance:** how wildly the distance value is jumping across the window. One person at a counter produces near-zero variance. Multiple people moving produces high variance as the sensor locks onto different targets.

**Flicker count:** how many times presence toggled on/off within the window. A single person produces near-zero flicker. The edge of a crowd, with people partially in and out of sensor range, produces high flicker.

**Velocity stability:** how consistent the velocity readings are. Calm, single-person movement is smooth. Multiple people moving in different directions produces unstable velocity.

These four metrics feed a scoring function that outputs a crowd level on a five-point scale: empty, quiet, active, busy, crowded.

---

## ESP32 Firmware

The firmware has two jobs. Read the RD-03E via UART and compute the density score. Then POST the result to the Flask API over WiFi every 30 seconds.

The RD-03E communicates over UART at 256000 baud. The frame format is simple: a header byte `0xAA`, a state byte (0 for no presence, 1 for moving target, 2 for static target), a distance byte (in tens of centimeters), a speed byte, and a footer byte `0x55`.

```cpp
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// WiFi credentials
const char* WIFI_SSID     = "your-network";
const char* WIFI_PASSWORD = "your-password";
const char* API_ENDPOINT  = "http://192.168.1.100:5000/state";

// UART2 for radar (TX2=17, RX2=16)
HardwareSerial RadarSerial(2);

// Frame format constants
const uint8_t FRAME_HEADER = 0xAA;
const uint8_t FRAME_FOOTER = 0x55;
const int     FRAME_LEN    = 5;

uint8_t frameBuf[FRAME_LEN];
int     framePos = 0;

// Behaviour window: 200 samples at ~10 reads/sec = last 20 seconds
const int WIN = 200;
int  distWindow[WIN];
bool presWindow[WIN];
int  winIdx = 0;

// Distance smoothing buffer
const int HIST = 6;
int distHistory[HIST] = {0};
int histIdx = 0;

// Post interval
const unsigned long POST_INTERVAL_MS = 30000;
unsigned long lastPost = 0;

// ── Metric helpers ──────────────────────────────────────────────────────────

int occupancyPct() {
  int count = 0;
  for (int i = 0; i < WIN; i++) if (presWindow[i]) count++;
  return (count * 100) / WIN;
}

int distVariance() {
  int sum = 0, n = 0;
  for (int i = 0; i < WIN; i++) {
    if (distWindow[i] > 0) { sum += distWindow[i]; n++; }
  }
  if (n < 2) return 0;
  int mean = sum / n;
  int var = 0;
  for (int i = 0; i < WIN; i++) {
    if (distWindow[i] > 0) var += abs(distWindow[i] - mean);
  }
  return var / n;
}

int flickerCount() {
  int flicks = 0;
  for (int i = 1; i < WIN; i++) {
    if (presWindow[i] != presWindow[i - 1]) flicks++;
  }
  return flicks;
}

// ── Crowd inference ─────────────────────────────────────────────────────────

struct CrowdReading {
  int        score;
  const char* level;
};

CrowdReading inferCrowd() {
  int occ     = occupancyPct();
  int var     = distVariance();
  int flicker = flickerCount();

  // Score is a weighted combination of three signals
  // Each contributes independently to avoid single-signal noise
  int score = 0;

  // Occupancy contribution (0-40 points)
  if (occ > 80) score += 40;
  else if (occ > 60) score += 28;
  else if (occ > 40) score += 18;
  else if (occ > 20) score += 8;

  // Distance variance contribution (0-35 points)
  if (var > 150) score += 35;
  else if (var > 80)  score += 25;
  else if (var > 40)  score += 14;
  else if (var > 15)  score += 6;

  // Flicker contribution (0-25 points)
  if (flicker > 40) score += 25;
  else if (flicker > 20) score += 18;
  else if (flicker > 10) score += 10;
  else if (flicker > 4)  score += 4;

  // Map score to level
  const char* level;
  if      (score == 0)  level = "empty";
  else if (score < 20)  level = "quiet";
  else if (score < 45)  level = "active";
  else if (score < 70)  level = "busy";
  else                  level = "crowded";

  return {score, level};
}

// ── Frame parsing ────────────────────────────────────────────────────────────

bool parseFrame(int& dist, int& speed, bool& present) {
  if (frameBuf[0] != FRAME_HEADER) return false;
  if (frameBuf[4] != FRAME_FOOTER) return false;

  uint8_t state = frameBuf[1];
  present = (state == 1 || state == 2);
  dist    = frameBuf[2] * 10;  // convert to cm
  speed   = frameBuf[3];

  return true;
}

// ── WiFi setup ───────────────────────────────────────────────────────────────

void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(500);
    attempts++;
  }
}

// ── State POST ───────────────────────────────────────────────────────────────

void postState(CrowdReading reading) {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
    return;
  }

  HTTPClient http;
  http.begin(API_ENDPOINT);
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<256> doc;
  doc["level"]      = reading.level;
  doc["score"]      = reading.score;
  doc["occupancy"]  = occupancyPct();
  doc["variance"]   = distVariance();
  doc["flicker"]    = flickerCount();
  doc["timestamp"]  = millis();

  String body;
  serializeJson(doc, body);

  int responseCode = http.POST(body);
  http.end();
}

// ── Setup and loop ────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  RadarSerial.begin(256000, SERIAL_8N1, 16, 17);
  connectWiFi();
}

void loop() {
  // Read bytes from radar, assemble frames
  while (RadarSerial.available()) {
    uint8_t byte = RadarSerial.read();

    if (byte == FRAME_HEADER && framePos == 0) {
      frameBuf[framePos++] = byte;
    } else if (framePos > 0 && framePos < FRAME_LEN) {
      frameBuf[framePos++] = byte;
      if (framePos == FRAME_LEN) {
        int dist, speed;
        bool present;
        if (parseFrame(dist, speed, present)) {
          // Update rolling window
          distWindow[winIdx] = present ? dist : 0;
          presWindow[winIdx] = present;
          winIdx = (winIdx + 1) % WIN;

          // Update smoothing buffer
          distHistory[histIdx] = dist;
          histIdx = (histIdx + 1) % HIST;
        }
        framePos = 0;
      }
    } else {
      framePos = 0;
    }
  }

  // POST state on interval
  unsigned long now = millis();
  if (now - lastPost >= POST_INTERVAL_MS) {
    CrowdReading reading = inferCrowd();
    postState(reading);
    lastPost = now;
  }
}
```

The 30-second POST interval is a deliberate choice. Music switching on a 30-second delay feels natural in a retail environment. A 5-second interval would cause constant churn. The window of 200 samples at roughly 10 reads per second gives a 20-second behavioural history, which smooths out transient spikes like a single person walking past.

---

## Flask State API

The API is a thin state store. The ESP32 POSTs to it, the player GETs from it. There is no database, no authentication, no queue. State is written to a JSON file on disk so it survives a Flask restart.

```python
# api.py
import json
import os
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

STATE_FILE = "state.json"

DEFAULT_STATE = {
    "level":     "empty",
    "score":     0,
    "occupancy": 0,
    "variance":  0,
    "flicker":   0,
    "timestamp": None,
    "updated_at": None,
}


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return DEFAULT_STATE.copy()


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


@app.route("/state", methods=["POST"])
def update_state():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "empty body"}), 400

    state = load_state()
    state.update({
        "level":     data.get("level", state["level"]),
        "score":     data.get("score", state["score"]),
        "occupancy": data.get("occupancy", 0),
        "variance":  data.get("variance", 0),
        "flicker":   data.get("flicker", 0),
        "timestamp": data.get("timestamp"),
        "updated_at": datetime.utcnow().isoformat(),
    })
    save_state(state)
    return jsonify({"status": "ok", "level": state["level"]})


@app.route("/state", methods=["GET"])
def get_state():
    return jsonify(load_state())


@app.route("/health", methods=["GET"])
def health():
    state = load_state()
    return jsonify({
        "status": "ok",
        "current_level": state["level"],
        "last_update": state.get("updated_at"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
```

This handles a stale sensor gracefully. If the ESP32 has not POSTed for 10 minutes (WiFi drop, power outage), the state file retains the last known level. The player keeps playing the last mood. No crash, no silence, no exception.

---

## The Music Catalogue

The catalogue is a flat JSON file. Each entry is one track with enough metadata for the mood engine to make decisions.

```json
[
  {
    "title": "Slow Mornings",
    "file": "music/empty/slow-mornings.mp3",
    "mood": "calm",
    "genre": "Ambient",
    "bpm": 68,
    "energy": 1,
    "duration": 214
  },
  {
    "title": "Third Wave",
    "file": "music/quiet/third-wave.mp3",
    "mood": "calm",
    "genre": "Jazz",
    "bpm": 84,
    "energy": 2,
    "duration": 198
  },
  {
    "title": "Cafe Afternoon",
    "file": "music/active/cafe-afternoon.mp3",
    "mood": "mid",
    "genre": "Bossa Nova",
    "bpm": 104,
    "energy": 3,
    "duration": 187
  },
  {
    "title": "Current Thing",
    "file": "music/busy/current-thing.mp3",
    "mood": "hype",
    "genre": "Pop",
    "bpm": 124,
    "energy": 4,
    "duration": 201
  },
  {
    "title": "Peak Floor",
    "file": "music/crowded/peak-floor.mp3",
    "mood": "hype",
    "genre": "Electronic",
    "bpm": 138,
    "energy": 5,
    "duration": 193
  }
]
```

The `mood` field is the key link between the sensor output and the music selection. The mood engine maps crowd level to mood tag, then the player selects from all catalogue entries matching that mood.

The BPM range by mood:

| Mood | BPM Range | Typical Genres           | Crowd Level   |
| ---- | --------- | ------------------------ | ------------- |
| calm | 60-90     | Ambient, Lo-fi, Acoustic | empty, quiet  |
| mid  | 90-115    | Jazz, Bossa Nova, Indie  | active        |
| hype | 115-145   | Pop, Electronic, Funk    | busy, crowded |

This range is not arbitrary. Psychological research on music and consumer behaviour consistently shows that slower tempos in low-traffic environments increases dwell time and per-item attention. Faster tempos in high-traffic environments increases throughput and energy. The BPM ranges above are derived from what actually works in retail contexts, not what sounds intuitive.

---

## The Mood Engine

The mood engine sits between the state API and the player. It reads the current level from state, maps it to a mood, selects the next track, and decides when to switch.

```python
# mood_engine.py
import json
import random
from dataclasses import dataclass
from typing import Optional


LEVEL_TO_MOOD = {
    "empty":   "calm",
    "quiet":   "calm",
    "active":  "mid",
    "busy":    "hype",
    "crowded": "hype",
}

# BPM guardrails per mood: never play outside this range regardless of catalogue
MOOD_BPM_RANGE = {
    "calm": (55, 92),
    "mid":  (88, 118),
    "hype": (112, 148),
}


@dataclass
class Track:
    title: str
    file: str
    mood: str
    genre: str
    bpm: int
    energy: int
    duration: int


class MoodEngine:
    def __init__(self, catalogue_path: str = "catalogue.json"):
        with open(catalogue_path) as f:
            raw = json.load(f)
        self.catalogue: list[Track] = [Track(**t) for t in raw]
        self.last_played: list[str] = []  # avoid immediate repeats
        self.current_mood: Optional[str] = None

    def level_to_mood(self, level: str) -> str:
        return LEVEL_TO_MOOD.get(level, "calm")

    def get_next_track(self, level: str) -> Optional[Track]:
        mood = self.level_to_mood(level)
        bpm_min, bpm_max = MOOD_BPM_RANGE[mood]

        # Filter by mood and BPM guardrails
        candidates = [
            t for t in self.catalogue
            if t.mood == mood
            and bpm_min <= t.bpm <= bpm_max
            and t.file not in self.last_played[-3:]  # avoid last 3 tracks
        ]

        if not candidates:
            # Relax the recency filter if catalogue is small
            candidates = [
                t for t in self.catalogue
                if t.mood == mood and bpm_min <= t.bpm <= bpm_max
            ]

        if not candidates:
            return None

        # Weight towards matching energy level more precisely
        level_energy_map = {
            "empty": 1, "quiet": 2, "active": 3, "busy": 4, "crowded": 5
        }
        target_energy = level_energy_map.get(level, 3)

        # Sort by proximity to target energy, break ties randomly
        candidates.sort(key=lambda t: (abs(t.energy - target_energy), random.random()))
        selected = candidates[0]

        self.last_played.append(selected.file)
        if len(self.last_played) > 10:
            self.last_played.pop(0)

        self.current_mood = mood
        return selected

    def should_switch_now(self, current_level: str) -> bool:
        """
        Returns True if the current crowd level has shifted mood category.
        The player uses this to decide whether to crossfade at end of track
        or interrupt immediately.
        """
        if self.current_mood is None:
            return True
        new_mood = self.level_to_mood(current_level)
        return new_mood != self.current_mood
```

The `should_switch_now` method is important for UX. If the crowd level changes from `active` to `busy`, both map to `mid` and `hype` respectively. If the mood category changes, the player crossfades immediately. If the level changes within the same mood category (for example, `empty` to `quiet`, both `calm`), the track continues to completion and the next selection reflects the updated level.

This prevents jarring mid-song transitions caused by brief spikes in crowd activity.

---

## PySide6 Player

The player is a PySide6 desktop application running on whatever machine is inside the venue. It polls the Flask API every 30 seconds, checks whether a mood switch is needed, and handles playback via pygame.

```python
# player.py (core logic, abbreviated)
import sys
import json
import threading
import pygame
import requests
from datetime import datetime
from PySide6.QtWidgets import QApplication, QMainWindow, QLabel, QPushButton
from PySide6.QtCore import Qt, QTimer, Signal, QObject

from mood_engine import MoodEngine

API_BASE = "http://localhost:5000"
POLL_INTERVAL_MS = 30000
CROSSFADE_DURATION_MS = 3000


class Signals(QObject):
    state_updated = Signal(dict)
    track_changed = Signal(str, str)   # title, mood
    log_message   = Signal(str)


class Aniket-pet-projectPlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.engine = MoodEngine("catalogue.json")
        self.signals = Signals()
        self.radar_enabled = False
        self.current_track = None

        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

        self._setup_ui()
        self._connect_signals()

        # Poll timer: fires every 30s when radar engine is active
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_and_adapt)

    def toggle_radar_engine(self, enabled: bool):
        self.radar_enabled = enabled
        if enabled:
            self.poll_timer.start(POLL_INTERVAL_MS)
            self.signals.log_message.emit("Radar engine ON")
            self._poll_and_adapt()  # immediate first check
        else:
            self.poll_timer.stop()
            self.signals.log_message.emit("Radar engine OFF")

    def _poll_and_adapt(self):
        """
        Runs in main thread via QTimer.
        Fetches current radar state, decides whether to switch music.
        """
        def fetch():
            try:
                resp = requests.get(f"{API_BASE}/state", timeout=3)
                resp.raise_for_status()
                state = resp.json()
                self.signals.state_updated.emit(state)
                self._maybe_switch(state["level"])
            except Exception as e:
                self.signals.log_message.emit(f"API error: {e}")

        threading.Thread(target=fetch, daemon=True).start()

    def _maybe_switch(self, level: str):
        immediate = self.engine.should_switch_now(level)

        if immediate:
            # Mood category changed: crossfade now
            self._crossfade_to_next(level)
        else:
            # Same mood: queue next track for when current finishes
            if not pygame.mixer.music.get_busy():
                self._play_next(level)

    def _crossfade_to_next(self, level: str):
        track = self.engine.get_next_track(level)
        if not track:
            return

        # Fade out current
        pygame.mixer.music.fadeout(CROSSFADE_DURATION_MS)

        # Schedule fade in after crossfade duration
        QTimer.singleShot(CROSSFADE_DURATION_MS, lambda: self._load_and_play(track))

    def _load_and_play(self, track):
        try:
            pygame.mixer.music.load(track.file)
            pygame.mixer.music.set_volume(0.0)
            pygame.mixer.music.play()

            # Fade in over 2 seconds
            self._fade_in(target_volume=0.8, duration_ms=2000)

            self.current_track = track
            self.signals.track_changed.emit(track.title, track.mood)
            self.signals.log_message.emit(
                f"Now playing: {track.title} [{track.genre}, {track.bpm} BPM]"
            )
        except Exception as e:
            self.signals.log_message.emit(f"Playback error: {e}")

    def _fade_in(self, target_volume: float, duration_ms: int):
        steps = 20
        step_ms = duration_ms // steps
        step_vol = target_volume / steps
        current_vol = [0.0]

        def step():
            current_vol[0] += step_vol
            if current_vol[0] >= target_volume:
                pygame.mixer.music.set_volume(target_volume)
                return
            pygame.mixer.music.set_volume(current_vol[0])
            QTimer.singleShot(step_ms, step)

        step()

    def _play_next(self, level: str):
        track = self.engine.get_next_track(level)
        if track:
            self._load_and_play(track)
```

The crossfade implementation deserves attention. `pygame.mixer.music.fadeout()` fades the outgoing track. `_fade_in()` uses a recursive QTimer chain to increment volume in 20 steps over 2 seconds. The result is a 3-second crossfade: 1 second of overlap where both tracks play at low volume, which sounds intentional rather than abrupt.

---

## Integrating With the Aniket-pet-project Ad Server

For a production deployment inside the Aniket-pet-project infrastructure, the `catalogue.json` file is replaced by a query against the Aniket-pet-project music library, which stores tracks in Elasticsearch with fields including `bpm`, `genre`, `energylevel`, `defined_search_tags`, and `license`.

The mood engine's `get_next_track` method can be extended to query Elasticsearch directly:

```python
from elasticsearch import Elasticsearch

class Aniket-pet-projectMoodEngine(MoodEngine):
    def __init__(self, es_host: str, index: str = "tracks"):
        self.es = Elasticsearch([es_host])
        self.index = index
        self.last_played = []
        self.current_mood = None

    def get_next_track(self, level: str):
        mood = self.level_to_mood(level)
        bpm_min, bpm_max = MOOD_BPM_RANGE[mood]

        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term":  {"status": 1}},
                        {"range": {"bpm": {"gte": bpm_min, "lte": bpm_max}}},
                    ],
                    "must_not": [
                        {"terms": {"filepath": self.last_played[-5:]}}
                    ]
                }
            },
            "sort": [{"_score": "desc"}],
            "size": 10
        }

        # Bias towards matching energy level
        level_energy_map = {
            "empty": 1, "quiet": 2, "active": 3, "busy": 4, "crowded": 5
        }
        target_energy = str(level_energy_map.get(level, 3))
        query["query"]["bool"]["should"] = [
            {"term": {"energylevel": target_energy}}
        ]

        result = self.es.search(index=self.index, body=query)
        hits = result["hits"]["hits"]

        if not hits:
            return None

        # Pick randomly from top 5 results to avoid deterministic rotation
        import random
        selected_source = random.choice(hits[:5])["_source"]

        track = Track(
            title    = selected_source["title"],
            file     = selected_source["filepath"],
            mood     = mood,
            genre    = selected_source.get("genre", ""),
            bpm      = int(selected_source.get("bpm", 100)),
            energy   = int(selected_source.get("energylevel", 3)),
            duration = int(selected_source.get("duration", 180)),
        )

        self.last_played.append(track.file)
        if len(self.last_played) > 15:
            self.last_played.pop(0)

        self.current_mood = mood
        return track
```

This connects the radar hardware directly to the existing Aniket-pet-project music library. The field names (`bpm`, `energylevel`, `filepath`, `status`) map directly to what is in the Elasticsearch index. The BPM guardrails from the mood engine apply on top of whatever the library contains, so a track tagged as Pop with BPM 160 does not get played in an `active` setting even if the genre filter would otherwise permit it.

---

## Deployment Layout

The full system runs on a single machine inside the venue. A Raspberry Pi 4 is sufficient. The ES32 talks to it over the local WiFi network.

```
Venue Network
├── ESP32 (radar sensor, WiFi client)
│     UART → RD-03E mmWave radar
│     HTTP POST → /state every 30s
│
└── Raspberry Pi 4 (music server)
      Flask API (api.py) on port 5000
      MoodEngine (mood_engine.py)
      PySide6 Player (player.py)
      Audio out → venue PA system
```

The Pi connects to the PA system via 3.5mm stereo out or via USB audio interface for better quality. No cloud dependency. No external API calls during normal operation. The entire system runs offline after the initial software setup.

The Flask API and the player are started as systemd services so they restart on reboot:

```ini
# /etc/systemd/system/aniket-pet-project-api.service
[Unit]
Description=Aniket-pet-project State API
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/aniket-pet-project
ExecStart=/home/pi/aniket-pet-project/venv/bin/python api.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable aniket-pet-project-api
sudo systemctl enable aniket-pet-project-player
sudo systemctl start aniket-pet-project-api
sudo systemctl start aniket-pet-project-player
```

---

## Limitations and What Comes Next

The RD-03E is a single-zone, single-target sensor. The density inference is a proxy based on behavioural signals, not an actual headcount. It is reliable enough to distinguish empty from active from crowded, which is all the music engine needs. It is not reliable enough to tell you there are exactly 7 people in the room.

For higher accuracy, the HLK-LD2450 is a direct upgrade path. It outputs X/Y coordinates for up to 3 tracked targets simultaneously, which makes it possible to count individuals directly rather than inferring from behavioural signatures. The UART protocol is different but the firmware structure is identical.

For multi-zone coverage in a large retail floor (entrance, main floor, checkout), multiple ESP32 units with individual sensors feed into the same Flask API with a zone identifier in the POST payload. The mood engine can weight zones differently: checkout zone occupancy matters more for energy level than entrance zone occupancy.

The MLX90640 thermal camera is worth exploring for situations where the RD-03E consistently misses still occupants (people sitting at cafe tables for example). It outputs an 8x8 temperature grid that can distinguish warm body signatures from the ambient environment, and it can count blobs across the grid. Privacy properties are equivalent to mmWave: no identifiable information, no images.

The current implementation is a working proof of concept built to demonstrate the system to stakeholders. The Elasticsearch integration for the full Aniket-pet-project music library is the next concrete step.
