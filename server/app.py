#!/usr/bin/env python3
import csv
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

INTERFACE = os.getenv("WIFI_INTERFACE", "wlan0")
PORT = int(os.getenv("PORT", "8686"))
DATA_DIR = os.getenv("DATA_DIR", "/data")
OFFLINE_SECONDS = int(os.getenv("OFFLINE_SECONDS", "240"))
BASE_DWELL = int(os.getenv("BASE_DWELL", "8"))
MAX_DWELL = int(os.getenv("MAX_DWELL", "30"))
WATCHDOG_TIMEOUT = int(os.getenv("WATCHDOG_TIMEOUT", "300"))
CHANNEL_HOLD_SECONDS = int(os.getenv("CHANNEL_HOLD_SECONDS", "86400"))
USB_RESET_COOLDOWN = int(os.getenv("USB_RESET_COOLDOWN", "1800"))
USB_RESET_SECONDS = int(os.getenv("USB_RESET_SECONDS", "8"))
USB_RESET_MAX_FAILURES = int(os.getenv("USB_RESET_MAX_FAILURES", "3"))
USB_PORT = os.getenv("USB_PORT", "")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DATA_DIR, "history.db")

with open(os.path.join(APP_DIR, "networks.json"), encoding="utf-8") as source:
    NETWORK_LIST = json.load(source)
NETWORKS = {item["bssid"].lower(): item for item in NETWORK_LIST}
CHANNELS = sorted({item["channel"] for item in NETWORK_LIST})
CHANNEL_SEQUENCE = [channel for channel in (4, 5, 10, 11, 1) if channel in CHANNELS]
CHANNEL_SEQUENCE.extend(channel for channel in CHANNELS if channel not in CHANNEL_SEQUENCE)

lock = threading.RLock()
db_lock = threading.Lock()
process_lock = threading.Lock()
recovery_lock = threading.Lock()
stop_event = threading.Event()
restart_in_progress = threading.Event()
started = time.time()
capture_processes = set()
recent_events = deque(maxlen=10000)
channel_live_activity = defaultdict(int)
channel_interval_activity = defaultdict(int)
channel_learning = {channel: {"avg": 0.0, "samples": 0} for channel in CHANNELS}
eapol_sessions = {}
completed_sessions = {}
last_return_bonus_at = {bssid: 0 for bssid in NETWORKS}
cpu_sample = {"usage": None, "time": None, "percent": 0.0}


def fresh_stats(item):
    return {
        "name": item["name"], "bssid": item["bssid"].lower(), "channel": item["channel"],
        "packets": 0, "beacons": 0, "probes": 0, "data": 0, "control": 0,
        "eapol_frames": 0, "valid_handshakes": 0, "last_seen": 0,
        "signals": deque(maxlen=120),
    }


network_stats = {bssid: fresh_stats(item) for bssid, item in NETWORKS.items()}
state = {
    "channel": CHANNELS[0], "dwell": BASE_DWELL, "capture_ok": False,
    "health": "ok", "errors": 0, "xp": 0, "level": 1,
    "birth": int(time.time()), "last_packet_at": time.time(),
    "last_handshake_at": 0, "last_handshake_network": None,
    "dropped_packets": 0, "captured_packets_last": 0,
    "recoveries_24h": 0, "uptime_24h_awarded": False,
    "channel_started_at": time.time(), "channel_mode": "fixed_24h",
    "channel_position": 0, "recovery_stage": 0, "usb_reset_attempts": 0,
    "last_usb_reset_at": 0, "usb_reset_available": False,
}


def run(*args):
    return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def prepare_monitor():
    run("ip", "link", "set", INTERFACE, "down")
    run("iw", "dev", INTERFACE, "set", "type", "monitor")
    run("ip", "link", "set", INTERFACE, "up")


def capture_filter():
    clauses = []
    for bssid in NETWORKS:
        clauses.append(f"(wlan addr1 {bssid} or wlan addr2 {bssid} "
                       f"or wlan addr3 {bssid} or wlan addr4 {bssid})")
    return "(" + " or ".join(clauses) + ")"


def open_database():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS network_samples (
        recorded_at INTEGER NOT NULL, bssid TEXT NOT NULL, activity INTEGER NOT NULL,
        packets INTEGER NOT NULL, signal REAL, online INTEGER NOT NULL,
        eapol_frames INTEGER NOT NULL, valid_handshakes INTEGER NOT NULL,
        PRIMARY KEY(recorded_at, bssid))""")
    db.execute("CREATE TABLE IF NOT EXISTS pet_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS recoveries (
        recorded_at INTEGER NOT NULL, reason TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS channel_activity_history (
        channel INTEGER PRIMARY KEY, avg_activity REAL NOT NULL, samples INTEGER NOT NULL)""")
    db.commit()
    return db


def persist_meta(values):
    with db_lock:
        db = open_database()
        db.executemany("INSERT OR REPLACE INTO pet_meta VALUES (?,?)",
                       [(key, str(value)) for key, value in values.items()])
        db.commit()
        db.close()


def calculate_level(xp):
    return int(math.sqrt(max(0, xp) / 50)) + 1


def award_xp(amount, meta=None):
    with lock:
        state["xp"] += amount
        state["level"] = calculate_level(state["xp"])
        xp = state["xp"]
    values = {"xp": xp}
    if meta:
        values.update(meta)
    persist_meta(values)


def initialize_persistence():
    with db_lock:
        db = open_database()
        meta = dict(db.execute("SELECT key,value FROM pet_meta").fetchall())
        learned = db.execute("SELECT channel,avg_activity,samples FROM channel_activity_history").fetchall()
        latest = db.execute("""SELECT n.bssid,n.packets,n.eapol_frames,n.valid_handshakes
                               FROM network_samples n JOIN (
                                 SELECT bssid,MAX(recorded_at) AS t FROM network_samples GROUP BY bssid
                               ) x ON n.bssid=x.bssid AND n.recorded_at=x.t""").fetchall()
        recoveries = db.execute("SELECT COUNT(*) FROM recoveries WHERE recorded_at >= ?",
                                (int(time.time()) - 86400,)).fetchone()[0]
        db.close()
    with lock:
        state["xp"] = int(meta.get("xp", "0"))
        state["level"] = calculate_level(state["xp"])
        state["birth"] = int(meta.get("birth", str(int(time.time()))))
        state["uptime_24h_awarded"] = meta.get("uptime_24h_awarded", "0") == "1"
        state["recoveries_24h"] = recoveries
        state["channel_position"] = int(meta.get("channel_position", "0")) % len(CHANNEL_SEQUENCE)
        state["channel_started_at"] = float(meta.get("channel_started_at", str(time.time())))
        state["last_usb_reset_at"] = float(meta.get("last_usb_reset_at", "0"))
        for bssid in NETWORKS:
            last_return_bonus_at[bssid] = int(meta.get(f"return_bonus:{bssid}", "0"))
        for channel, average, samples in learned:
            if channel in channel_learning:
                channel_learning[channel] = {"avg": float(average), "samples": int(samples)}
        for bssid, packets, eapol_frames, handshakes in latest:
            if bssid in network_stats:
                network_stats[bssid]["packets"] = packets
                network_stats[bssid]["eapol_frames"] = eapol_frames
                network_stats[bssid]["valid_handshakes"] = handshakes
    persist_meta({"birth": state["birth"]})


def register_process(proc):
    with process_lock:
        capture_processes.add(proc)


def unregister_process(proc):
    with process_lock:
        capture_processes.discard(proc)


def stop_capture_processes():
    with process_lock:
        active = list(capture_processes)
    for proc in active:
        if proc.poll() is None:
            proc.terminate()
    deadline = time.time() + 5
    for proc in active:
        if proc.poll() is None:
            try:
                proc.wait(max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()


DROPPED_PATTERNS = [
    re.compile(r"packets?\s+dropped:\s*(\d+)", re.I),
    re.compile(r"(\d+)\s+packets?\s+dropped(?:\s+by\s+kernel)?", re.I),
]
CAPTURED_PATTERNS = [
    re.compile(r"(\d+)\s+packets?\s+captured", re.I),
    re.compile(r"packets?\s+captured:\s*(\d+)", re.I),
    re.compile(r"packets?:\s*(\d+)", re.I),
]


def consume_stderr(stream, tool):
    for line in stream:
        clean = line.strip()
        for pattern in DROPPED_PATTERNS:
            match = pattern.search(clean)
            if match:
                with lock:
                    state["dropped_packets"] = int(match.group(1))
                break
        for pattern in CAPTURED_PATTERNS:
            match = pattern.search(clean)
            if match:
                with lock:
                    state["captured_packets_last"] = int(match.group(1))
                break
        if clean and ("error" in clean.lower() or "failed" in clean.lower()
                      or "invalid" in clean.lower() or "permission" in clean.lower()):
            print(f"[{tool}] {clean}", flush=True)


def wait_for_restart():
    while restart_in_progress.is_set() and not stop_event.is_set():
        stop_event.wait(0.5)


def adaptive_hopper():
    while not stop_event.is_set():
        now = time.time()
        with lock:
            position = state["channel_position"]
            channel_started_at = state["channel_started_at"]
        if channel_started_at > now or channel_started_at <= 0:
            channel_started_at = now
        elapsed = now - channel_started_at
        if elapsed >= CHANNEL_HOLD_SECONDS:
            steps = max(1, int(elapsed // CHANNEL_HOLD_SECONDS))
            position = (position + steps) % len(CHANNEL_SEQUENCE)
            channel_started_at += steps * CHANNEL_HOLD_SECONDS
            if channel_started_at > now:
                channel_started_at = now
        channel = CHANNEL_SEQUENCE[position]
        if stop_event.is_set():
            return
        # rtl8xxxu may wedge when the channel changes while its capture socket
        # is open. Reproduce the reliable manual sequence: close -> tune -> open.
        with recovery_lock:
            restart_in_progress.set()
            stop_capture_processes()
            changed = run("iw", "dev", INTERFACE, "set", "channel", str(channel))
            if changed.returncode:
                with lock:
                    state["errors"] += 1
                prepare_monitor()
                changed = run("iw", "dev", INTERFACE, "set", "channel", str(channel))
            with lock:
                state["last_packet_at"] = time.time()
                state["channel"] = channel
                state["dwell"] = CHANNEL_HOLD_SECONDS
                state["channel_position"] = position
                state["channel_started_at"] = channel_started_at
                channel_live_activity[channel] = 0
            restart_in_progress.clear()
        persist_meta({"channel_position": position, "channel_started_at": int(channel_started_at)})
        if changed.returncode:
            stop_event.wait(BASE_DWELL)
            continue
        remaining = max(1, CHANNEL_HOLD_SECONDS - (time.time() - channel_started_at))
        if stop_event.wait(remaining):
            return
        with lock:
            state["channel_position"] = (position + 1) % len(CHANNEL_SEQUENCE)
            state["channel_started_at"] = time.time()


def classify_frame(type_subtype):
    try:
        value = int(type_subtype, 0)
    except (TypeError, ValueError):
        return "control"
    if value == 0x08:
        return "beacons"
    if value in {0x04, 0x05}:
        return "probes"
    if value & 0x30 == 0x20:
        return "data"
    return "control"


def parse_signal(value):
    try:
        signal_value = int(float(value))
        return signal_value if -110 <= signal_value <= 0 else None
    except (TypeError, ValueError):
        return None


def process_packet_row(row):
    row = (row + [""] * 6)[:6]
    bssid, source, destination, type_subtype, signal_text, message = [part.strip().lower() for part in row]
    if bssid not in NETWORKS:
        bssid = source if source in NETWORKS else destination if destination in NETWORKS else None
    if not bssid:
        return
    now = time.time()
    return_bonus = False
    with lock:
        stats = network_stats[bssid]
        was_offline = not stats["last_seen"] or now - stats["last_seen"] > OFFLINE_SECONDS
        stats["last_seen"] = now
        stats["packets"] += 1
        stats[classify_frame(type_subtype)] += 1
        signal_value = parse_signal(signal_text)
        if signal_value is not None:
            stats["signals"].append(signal_value)
        state["last_packet_at"] = now
        state["capture_ok"] = True
        state["health"] = "ok"
        state["recovery_stage"] = 0
        state["usb_reset_attempts"] = 0
        recent_events.append(now)
        channel_live_activity[stats["channel"]] += 1
        channel_interval_activity[stats["channel"]] += 1
        if was_offline and now - last_return_bonus_at[bssid] >= 900:
            last_return_bonus_at[bssid] = int(now)
            return_bonus = True
    if return_bonus:
        award_xp(10, {f"return_bonus:{bssid}": int(now)})
    if message in {"1", "2", "3", "4"}:
        process_eapol(bssid, source, destination, int(message), now)


def process_eapol(bssid, source, destination, message, now):
    station = destination if source == bssid else source
    key = (bssid, station)
    handshake_complete = False
    with lock:
        stats = network_stats[bssid]
        stats["eapol_frames"] += 1
        session = eapol_sessions.setdefault(key, {"messages": set(), "updated": now})
        session["messages"].add(message)
        session["updated"] = now
        if session["messages"] == {1, 2, 3, 4} and now - completed_sessions.get(key, 0) > 300:
            stats["valid_handshakes"] += 1
            completed_sessions[key] = now
            state["last_handshake_at"] = now
            state["last_handshake_network"] = stats["name"]
            handshake_complete = True
    award_xp(5)
    if handshake_complete:
        award_xp(100)


def tshark_capture():
    fields = ["wlan.bssid", "wlan.sa", "wlan.da", "wlan.fc.type_subtype",
              "radiotap.dbm_antsignal", "wlan_rsna_eapol.keydes.msgnr"]
    captures = os.path.join(DATA_DIR, "captures")
    os.makedirs(captures, exist_ok=True)
    while not stop_event.is_set():
        wait_for_restart()
        if stop_event.is_set():
            return
        proc = None
        try:
            # Each channel hop starts a new ring session. Keep no more than the
            # newest nine files before opening the tenth, preserving ~100 MB.
            previous = []
            for name in os.listdir(captures):
                if name.startswith("authorized") and name.endswith((".pcapng", ".pcap")):
                    path = os.path.join(captures, name)
                    try:
                        previous.append((os.path.getmtime(path), path))
                    except OSError:
                        pass
            for _, old_path in sorted(previous, reverse=True)[9:]:
                try:
                    os.unlink(old_path)
                except OSError:
                    pass
            # Some RTL8188ETV/rtl8xxxu combinations stop delivering frames
            # when more than one capture socket listens on the monitor device.
            # One tshark session therefore writes PCAP and emits parsed fields.
            command = ["tshark", "-l", "-i", INTERFACE, "-f", capture_filter(),
                       "-b", "filesize:10000", "-b", "files:10",
                       "-w", os.path.join(captures, "authorized.pcapng"), "-P",
                       "-T", "fields", "-E", "separator=,", "-E", "quote=d",
                       "-E", "occurrence=f"]
            for field in fields:
                command.extend(["-e", field])
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
            register_process(proc)
            threading.Thread(target=consume_stderr, args=(proc.stderr, "tshark"), daemon=True).start()
            for line in proc.stdout:
                if stop_event.is_set() or restart_in_progress.is_set():
                    break
                try:
                    process_packet_row(next(csv.reader([line])))
                except (csv.Error, StopIteration):
                    with lock:
                        state["errors"] += 1
        except Exception as exc:
            print(f"[tshark] {exc}", flush=True)
            with lock:
                state["errors"] += 1
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
            if proc:
                unregister_process(proc)
        stop_event.wait(2)


def record_recovery(reason):
    now = int(time.time())
    with db_lock:
        db = open_database()
        db.execute("INSERT INTO recoveries VALUES (?,?)", (now, reason))
        db.execute("DELETE FROM recoveries WHERE recorded_at < ?", (now - 30 * 86400,))
        db.commit()
        count = db.execute("SELECT COUNT(*) FROM recoveries WHERE recorded_at >= ?",
                           (now - 86400,)).fetchone()[0]
        db.close()
    with lock:
        state["recoveries_24h"] = count


def reset_realtek_usb():
    """Reset only the validated RTL8188ETV USB device exposed by install.sh."""
    if not re.fullmatch(r"\d+-[\d.]+", USB_PORT):
        return False, "porta USB não configurada"
    device_dir = os.path.join("/host-usb-devices", USB_PORT)
    try:
        with open(os.path.join(device_dir, "idVendor"), encoding="ascii") as source:
            vendor = source.read().strip().lower()
        with open(os.path.join(device_dir, "idProduct"), encoding="ascii") as source:
            product = source.read().strip().lower()
    except OSError as exc:
        return False, f"não foi possível validar USB: {exc}"
    if (vendor, product) != ("0bda", "0179"):
        return False, f"USB recusado: {vendor}:{product}"
    unbind = "/host-usb-driver/unbind"
    bind = "/host-usb-driver/bind"
    try:
        with open(unbind, "w", encoding="ascii") as target:
            target.write(USB_PORT)
        time.sleep(USB_RESET_SECONDS)
        with open(bind, "w", encoding="ascii") as target:
            target.write(USB_PORT)
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(f"/sys/class/net/{INTERFACE}"):
            time.sleep(1)
        if not os.path.exists(f"/sys/class/net/{INTERFACE}"):
            return False, "wlan0 não reapareceu depois do reset"
        prepare_monitor()
        with lock:
            channel = state["channel"]
        if run("iw", "dev", INTERFACE, "set", "channel", str(channel)).returncode:
            return False, "não foi possível restaurar o canal"
        return True, "RTL8188ETV reiniciado"
    except OSError as exc:
        # If unbind succeeded but bind failed, a physical reconnect may be needed.
        return False, f"falha no reset USB: {exc}"


def watchdog_loop():
    while not stop_event.wait(30):
        with lock:
            silence = time.time() - state["last_packet_at"]
            stage = state["recovery_stage"]
            attempts = state["usb_reset_attempts"]
            last_usb_reset = state["last_usb_reset_at"]
        if silence <= WATCHDOG_TIMEOUT or restart_in_progress.is_set():
            continue
        if stage >= 1 and attempts >= USB_RESET_MAX_FAILURES:
            with lock:
                state["health"] = "degraded"
                state["capture_ok"] = False
                state["last_packet_at"] = time.time()
            continue
        with recovery_lock:
            if restart_in_progress.is_set():
                continue
            use_usb = stage >= 1 and attempts < USB_RESET_MAX_FAILURES \
                and time.time() - last_usb_reset >= USB_RESET_COOLDOWN
            reason = ("reset USB após segunda falha" if use_usb else
                      f"recuperação leve após {int(silence)}s sem pacotes do canal atual")
            print(f"[watchdog] recuperação: {reason}", flush=True)
            with lock:
                state["health"] = "degraded"
                state["capture_ok"] = False
            record_recovery(reason)
            restart_in_progress.set()
            stop_capture_processes()
            if use_usb:
                ok, detail = reset_realtek_usb()
                print(f"[watchdog] USB: {detail}", flush=True)
                with lock:
                    state["last_usb_reset_at"] = time.time()
                    state["usb_reset_attempts"] += 1
                    state["usb_reset_available"] = ok
                persist_meta({"last_usb_reset_at": int(time.time())})
            else:
                prepare_monitor()
                with lock:
                    channel = state["channel"]
                run("iw", "dev", INTERFACE, "set", "channel", str(channel))
                with lock:
                    state["recovery_stage"] = 1
            with lock:
                state["last_packet_at"] = time.time()
            restart_in_progress.clear()


def persistence_loop():
    last_packets = {bssid: stats["packets"] for bssid, stats in network_stats.items()}
    while not stop_event.wait(60):
        now = int(time.time())
        total_activity = 0
        award_24h = False
        with lock:
            rows = []
            for bssid, stats in network_stats.items():
                activity = max(0, stats["packets"] - last_packets[bssid])
                last_packets[bssid] = stats["packets"]
                total_activity += activity
                signal_avg = round(sum(stats["signals"]) / len(stats["signals"]), 1) if stats["signals"] else None
                online = int(now - stats["last_seen"] <= OFFLINE_SECONDS)
                rows.append((now, bssid, activity, stats["packets"], signal_avg, online,
                             stats["eapol_frames"], stats["valid_handshakes"]))
            healthy_minute = state["capture_ok"]
            if now - started >= 86400 and not state["uptime_24h_awarded"]:
                state["uptime_24h_awarded"] = True
                award_24h = True
            learned_rows = []
            for channel in CHANNELS:
                activity = channel_interval_activity[channel]
                channel_interval_activity[channel] = 0
                item = channel_learning[channel]
                weight = min(item["samples"] + 1, 100)
                item["avg"] += (activity - item["avg"]) / weight
                item["samples"] += 1
                learned_rows.append((channel, item["avg"], item["samples"]))
        with db_lock:
            db = open_database()
            db.executemany("INSERT OR REPLACE INTO network_samples VALUES (?,?,?,?,?,?,?,?)", rows)
            db.executemany("INSERT OR REPLACE INTO channel_activity_history VALUES (?,?,?)", learned_rows)
            db.execute("DELETE FROM network_samples WHERE recorded_at < ?", (now - 30 * 86400,))
            db.execute("DELETE FROM recoveries WHERE recorded_at < ?", (now - 30 * 86400,))
            recoveries = db.execute("SELECT COUNT(*) FROM recoveries WHERE recorded_at >= ?",
                                    (now - 86400,)).fetchone()[0]
            db.commit()
            db.close()
        with lock:
            state["recoveries_24h"] = recoveries
        minute_xp = (1 if healthy_minute else 0) + (total_activity // 100 * 2)
        if minute_xp:
            award_xp(minute_xp)
        if award_24h:
            award_xp(250, {"uptime_24h_awarded": 1})
        cutoff = time.time() - 600
        with lock:
            for key in list(eapol_sessions):
                if eapol_sessions[key]["updated"] < cutoff:
                    del eapol_sessions[key]


def cpu_percent():
    now = time.monotonic()
    usage = None
    try:
        with open("/sys/fs/cgroup/cpu.stat", encoding="utf-8") as source:
            values = dict(line.split() for line in source if line.strip())
        usage = int(values["usage_usec"]) / 1_000_000
    except (OSError, KeyError, ValueError):
        try:
            times = os.times()
            usage = times.user + times.system + times.children_user + times.children_system
        except OSError:
            return 0.0
    with lock:
        previous_usage, previous_time = cpu_sample["usage"], cpu_sample["time"]
        if previous_usage is not None and previous_time is not None and now > previous_time:
            cpu_sample["percent"] = round(max(0, (usage - previous_usage) / (now - previous_time) * 100), 1)
        cpu_sample["usage"], cpu_sample["time"] = usage, now
        return cpu_sample["percent"]


def network_view(stats, now):
    signal_avg = round(sum(stats["signals"]) / len(stats["signals"]), 1) if stats["signals"] else None
    return {
        "name": stats["name"], "bssid": stats["bssid"], "channel": stats["channel"],
        "packets": stats["packets"], "beacons": stats["beacons"], "probes": stats["probes"],
        "data": stats["data"], "control": stats["control"], "signal_avg": signal_avg,
        "online": bool(stats["last_seen"] and now - stats["last_seen"] <= OFFLINE_SECONDS),
        "monitoring": stats["channel"] == state["channel"],
        "last_seen_seconds": None if not stats["last_seen"] else int(now - stats["last_seen"]),
        "eapol_frames": stats["eapol_frames"], "valid_handshakes": stats["valid_handshakes"],
    }


def snapshot():
    now = time.time()
    with lock:
        while recent_events and recent_events[0] < now - 60:
            recent_events.popleft()
        networks = [network_view(stats, now) for stats in network_stats.values()]
        networks.sort(key=lambda item: item["packets"], reverse=True)
        online = sum(item["online"] for item in networks)
        monitored = [item for item in networks if item["monitoring"]]
        monitored_online = sum(item["online"] for item in monitored)
        packets = sum(item["packets"] for item in networks)
        handshakes = sum(item["valid_handshakes"] for item in networks)
        eating = [item["name"] for item in networks if item["channel"] == state["channel"]]
        rate = len(recent_events)
        handshake_age = None if not state["last_handshake_at"] else int(now - state["last_handshake_at"])
        celebrating = handshake_age is not None and handshake_age <= 15
        if celebrating:
            face, mood = "(✧‿✧)", "proud"
            message = f"Capturei uma autenticação completa em {state['last_handshake_network']}!"
        elif state["health"] == "degraded":
            face, mood, message = "(×﹏×)", "sick", "Minha antena ficou doente; estou me recuperando…"
        elif not state["capture_ok"]:
            face, mood, message = "(－_－) zzZ", "waking", "Acordando e procurando sinais…"
        elif monitored and monitored_online < len(monitored) / 2:
            face, mood, message = "(╥﹏╥)", "worried", "Algumas redes sumiram…"
        elif rate > 500:
            face, mood, message = "(✧◡✧)", "excited", "A rede está uma festa!"
        elif rate > 80:
            face, mood, message = "(⌐■‿■)", "happy", "Estou comendo ondas Wi‑Fi!"
        else:
            face, mood, message = "(•‿•)", "calm", "Tudo tranquilo por aqui."
        achievements = []
        if handshakes:
            achievements.append("Primeira autenticação completa")
        if state["uptime_24h_awarded"]:
            achievements.append("24 horas contínuas")
        result = {
            **state, "face": face, "mood": mood, "message": message,
            "uptime": int(now - started), "per_minute": rate, "packets": packets,
            "online": online, "total_networks": len(networks), "valid_handshakes": handshakes,
            "eating": eating, "networks": networks, "last_handshake_seconds_ago": handshake_age,
            "achievements": achievements,
            "channel_learning": {str(ch): round(channel_learning[ch]["avg"], 1) for ch in CHANNELS},
            "channel_seconds_remaining": max(
                0, CHANNEL_HOLD_SECONDS - int(now - state["channel_started_at"])),
            "channel_sequence": CHANNEL_SEQUENCE,
        }
    result["cpu_percent"] = cpu_percent()
    try:
        result["load_average_1m"] = round(os.getloadavg()[0], 2)
    except OSError:
        result["load_average_1m"] = None
    return result


def history(hours):
    since = int(time.time()) - min(max(hours, 1), 720) * 3600
    with db_lock:
        db = open_database()
        rows = db.execute("""SELECT recorded_at,SUM(activity),SUM(online)
                             FROM network_samples WHERE recorded_at >= ?
                             GROUP BY recorded_at ORDER BY recorded_at""", (since,)).fetchall()
        db.close()
    return [{"time": row[0], "activity": row[1], "online": row[2]} for row in rows]


def home_assistant_view():
    data = snapshot()
    return {
        "state": data["mood"], "level": data["level"], "xp": data["xp"],
        "networks_online": data["online"], "networks_total": data["total_networks"],
        "packets": data["packets"], "packets_per_minute": data["per_minute"],
        "channel": data["channel"], "eating": ", ".join(data["eating"]),
        "valid_handshakes": data["valid_handshakes"],
        "last_handshake_seconds_ago": data["last_handshake_seconds_ago"],
        "health": data["health"], "cpu_percent": data["cpu_percent"],
        "recoveries_24h": data["recoveries_24h"], "dropped_packets": data["dropped_packets"],
        "usb_reset_attempts": data["usb_reset_attempts"],
        "channel_seconds_remaining": data["channel_seconds_remaining"],
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            return self.send_json(snapshot())
        if parsed.path == "/api/health":
            data = snapshot()
            return self.send_json({key: data[key] for key in (
                "health", "cpu_percent", "load_average_1m", "recoveries_24h",
                "dropped_packets", "captured_packets_last", "last_packet_at",
                "usb_reset_attempts", "last_usb_reset_at", "usb_reset_available")})
        if parsed.path == "/api/history":
            try:
                hours = int(parse_qs(parsed.query).get("hours", ["24"])[0])
            except ValueError:
                hours = 24
            return self.send_json(history(hours))
        if parsed.path == "/api/homeassistant":
            return self.send_json(home_assistant_view())
        if parsed.path == "/":
            with open(os.path.join(APP_DIR, "index.html"), "rb") as source:
                body = source.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *_):
        pass


def shutdown(*_):
    stop_event.set()
    stop_capture_processes()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    initialize_persistence()
    prepare_monitor()
    workers = [adaptive_hopper, tshark_capture, watchdog_loop, persistence_loop]
    for worker in workers:
        threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    finally:
        shutdown()
