"""
memory_manager.py - Cognitive persistent memory (knowledge_base.json).

Stores research notes / user conclusions per domain, preferences and an
execution history, all timestamped. Lookups are fuzzy (token similarity) so
the same topic phrased differently is not researched twice.
"""

import copy
import json
import os
import re
import threading
from datetime import datetime, timezone

import config

HISTORY_LIMIT = 500
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "be", "to", "of", "for", "on", "in",
    "and", "or", "me", "my", "i", "it", "this", "that", "with", "about", "can",
    "you", "please", "what", "how", "should", "do", "does", "now", "today",
    "tell", "give", "show", "analysis", "analyze", "analyse", "research",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts):
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def age_hours(ts):
    dt = _parse(ts)
    if dt is None:
        return float("inf")
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokens(text):
    out = set()
    for tok in re.findall(r"[a-z0-9\.\-\^&]+", normalize(text)):
        tok = tok.strip(".-")
        if not tok or tok in STOPWORDS:
            continue
        out.add(tok[:5] if len(tok) > 5 else tok)
    return out


def similarity(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _default_db():
    ts = now_iso()
    return {
        "meta": {"schema_version": 2, "created": ts, "updated": ts,
                 "description": "Persistent cognitive memory for AutoMind"},
        "preferences": copy.deepcopy(config.DEFAULT_PREFERENCES),
        "profile": {},
        "research": {"stocks": {}, "crypto": {}, "tech": {}, "general": {}},
        "history": [],
    }


class MemoryManager:
    def __init__(self, path=None):
        self.path = path or config.KNOWLEDGE_BASE_PATH
        self._lock = threading.RLock()
        self.db = self._load()

    # ---------------- persistence ----------------
    def _load(self):
        base = _default_db()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("root is not an object")
        except FileNotFoundError:
            self._write(base)
            return base
        except (OSError, ValueError):
            try:  # keep the corrupt file for manual recovery
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            self._write(base)
            return base
        for key, value in base.items():
            if key not in data or not isinstance(data[key], type(value)):
                data[key] = value
        for key, value in base["preferences"].items():
            data["preferences"].setdefault(key, value)
        if not isinstance(data.get("profile"), dict):
            data["profile"] = {}
        for domain in base["research"]:
            if not isinstance(data["research"].get(domain), dict):
                data["research"][domain] = {}
        return data

    def _write(self, db):
        db.setdefault("meta", {})["updated"] = now_iso()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(db, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def save(self):
        with self._lock:
            self._write(self.db)

    # ---------------- research notes ----------------
    def save_research(self, domain, topic, conclusion, provider="", prompt="", tags=None):
        """Create or update a research note. Returns the stored entry."""
        with self._lock:
            bucket = self.db["research"].setdefault(domain, {})
            key = normalize(topic)
            entry = bucket.get(key)
            ts = now_iso()
            if entry is None:
                entry = {"key": key, "topic": topic.strip(), "domain": domain,
                         "created": ts, "hits": 0, "history": []}
                bucket[key] = entry
            else:
                entry["history"].append({"ts": entry.get("updated", ts),
                                         "provider": entry.get("provider", ""),
                                         "conclusion": entry.get("conclusion", "")})
                entry["history"] = entry["history"][-10:]
            entry.update({"conclusion": conclusion.strip(), "provider": provider,
                          "prompt": prompt, "tags": tags or entry.get("tags", []),
                          "updated": ts})
            self._write(self.db)
            return copy.deepcopy(entry)

    def find_similar(self, topic, domain=None, max_age_hours=None, threshold=None):
        """Best matching saved note, or None. Adds 'similarity' to the copy."""
        if threshold is None:
            threshold = float(self.db["preferences"].get("similarity_threshold", 0.6))
        key = normalize(topic)
        best, best_score = None, 0.0
        with self._lock:
            domains = [domain] if domain in self.db["research"] else list(self.db["research"])
            for dom in domains:
                for entry in self.db["research"][dom].values():
                    if not entry.get("conclusion"):
                        continue
                    if max_age_hours is not None and age_hours(entry.get("updated")) > max_age_hours:
                        continue
                    score = 1.0 if entry.get("key") == key else similarity(topic, entry.get("topic", ""))
                    if score > best_score:
                        best, best_score = entry, score
            if best is None or best_score < threshold:
                return None
            best["hits"] = best.get("hits", 0) + 1
            self._write(self.db)
            found = copy.deepcopy(best)
        found["similarity"] = round(best_score, 2)
        return found

    def search(self, text, limit=10):
        results = []
        with self._lock:
            for dom, bucket in self.db["research"].items():
                for entry in bucket.values():
                    hay = entry.get("topic", "") + " " + entry.get("conclusion", "")
                    score = similarity(text, hay)
                    if normalize(text) in normalize(hay):
                        score = max(score, 0.9)
                    if score > 0:
                        results.append((score, copy.deepcopy(entry)))
        results.sort(key=lambda r: r[0], reverse=True)
        return [e for _, e in results[:limit]]

    def list_research(self, domain=None):
        with self._lock:
            out = []
            for dom, bucket in self.db["research"].items():
                if domain and dom != domain:
                    continue
                out.extend(copy.deepcopy(list(bucket.values())))
        out.sort(key=lambda e: e.get("updated", ""), reverse=True)
        return out

    def delete_research(self, topic):
        key = normalize(topic)
        with self._lock:
            for bucket in self.db["research"].values():
                if key in bucket:
                    del bucket[key]
                    self._write(self.db)
                    return True
        return False

    # ---------------- persistent user profile ----------------
    def remember(self, key, value):
        """Save a user-approved fact/preference for later sessions."""
        key = normalize(key).strip(" :")
        value = str(value).strip()
        if not key or not value:
            raise ValueError("Both key and value are required")
        with self._lock:
            old = self.db.setdefault("profile", {}).get(key)
            self.db["profile"][key] = {"value": value, "updated": now_iso()}
            self._write(self.db)
        return old is not None

    def profile_items(self, query=""):
        query = normalize(query)
        with self._lock:
            items = []
            for key, entry in self.db.setdefault("profile", {}).items():
                value = entry.get("value", "") if isinstance(entry, dict) else str(entry)
                if not query or query in key or query in normalize(value):
                    items.append((key, value, entry.get("updated", "") if isinstance(entry, dict) else ""))
            return sorted(items)

    def forget_profile(self, key):
        key = normalize(key).strip(" :")
        with self._lock:
            profile = self.db.setdefault("profile", {})
            if key not in profile:
                return False
            del profile[key]
            self._write(self.db)
            return True

    # ---------------- preferences ----------------
    def get_pref(self, name, default=None):
        return self.db["preferences"].get(name, default)

    def set_pref(self, name, value):
        with self._lock:
            self.db["preferences"][name] = value
            self._write(self.db)

    # ---------------- history ----------------
    def log(self, kind, detail, status="ok"):
        with self._lock:
            self.db["history"].append({"ts": now_iso(), "kind": kind,
                                       "detail": str(detail)[:300], "status": status})
            self.db["history"] = self.db["history"][-HISTORY_LIMIT:]
            self._write(self.db)

    def recent_history(self, n=10):
        with self._lock:
            return copy.deepcopy(self.db["history"][-n:])

    def stats(self):
        with self._lock:
            counts = {d: len(b) for d, b in self.db["research"].items()}
            return {"notes": sum(counts.values()), "by_domain": counts,
                    "history_entries": len(self.db["history"]),
                    "updated": self.db["meta"].get("updated")}
