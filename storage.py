"""Atomic JSON storage and rolling backups."""
import datetime
import json
import os
import shutil
import threading
import time

LOCK = threading.RLock()

def read(path, default, backups):
    """Read a JSON data file. A damaged file is kept aside (never silently overwritten)
    and the last good backup is used instead."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, type(default)):
            return data
    except FileNotFoundError:
        return default
    except (UnicodeError, ValueError):
        pass
    with LOCK:
        # A concurrent writer may have repaired the file while we waited.
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, type(default)):
                return data
        except (UnicodeError, ValueError):
            pass
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copyfile(path, path.with_name(f"{path.stem}.damaged-{stamp}{path.suffix}"))
        except OSError:
            pass
        # newest good copy first: the previous save, then the daily backups
        candidates = [path.with_name(path.name + ".bak")] + sorted(backups.glob(f"{path.stem}-*.json"), reverse=True)
        for cand in candidates:
            try:
                data = json.loads(cand.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, ValueError):
                continue
            if not isinstance(data, type(default)):
                continue
            write(path, data, backups, indent=1 if isinstance(data, list) else None, backup=False)
            return data
        return default

def daily_backup(path, backups):
    """Keep one copy per day of each data file in the backups folder (last 30 days)."""
    try:
        backups.mkdir(exist_ok=True)
        today = backups / f"{path.stem}-{datetime.date.today().isoformat()}.json"
        if not today.exists():
            shutil.copyfile(path, today)
            for old in sorted(backups.glob(f"{path.stem}-*.json"))[:-30]:
                old.unlink()
    except OSError:
        pass

def write(path, data, backups, indent=None, backup=True):
    """Save safely: write a temp file, keep the previous version as .bak, then swap in.
    A crash or power cut mid-save can't leave a half-written data file."""
    with LOCK:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
        if path.exists() and backup:
            try:
                shutil.copyfile(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
            daily_backup(path, backups)
        for attempt in range(10):           # Windows: antivirus/backup tools can briefly lock files
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.2)

