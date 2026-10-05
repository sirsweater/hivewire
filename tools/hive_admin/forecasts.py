"""Collect public weather forecasts next to what the garden's own station
measures, and score them - the first step to a forecast of our own.

Every few hours this saves the next 48 hours from several free models
(Open-Meteo: GFS, HRRR, ECMWF, ICON) for the station's location. Once an hour
has happened, the station's own hourly summary (rollup.py) is the truth it is
scored against: mean error and bias per model, per variable, per lead time
(how far ahead it was forecast). That scoreboard is what later corrections
and a blended forecast are built from - nothing here changes any forecast yet.

    forecasts(issued, model, valid, var, value)
      issued  the hour the forecast was fetched (epoch s)
      valid   the hour it is for (epoch s, start of the hour it describes;
              rain is the total of that hour)
      var     temp C | hum % | wind km/h | gust km/h | dir deg | rain mm

The location lives in the hub's config ("forecast": {"lat", "lon"}), never
in this file: this repo is public.
"""
import json
import threading
import time
import urllib.parse
import urllib.request

URL = "https://api.open-meteo.com/v1/forecast"
# gfs_global, not gfs_seamless: the seamless feed blends HRRR in over the US and
# would score as a copy of it.
MODELS = ["gfs_global", "gfs_hrrr", "ecmwf_ifs025", "icon_seamless"]
# Open-Meteo hourly variable -> our name
VARS = {
    "temperature_2m": "temp",
    "relative_humidity_2m": "hum",
    "wind_speed_10m": "wind",
    "wind_gusts_10m": "gust",
    "wind_direction_10m": "dir",
    "precipitation": "rain",
}
HOURS_AHEAD = 48
EVERY_S = 3 * 3600
KEEP_DAYS = 35
HOUR = 3600

# What the station measured, from rollup_hour: var -> (slot, column, scale)
OBSERVED = {
    "temp": (1, "avg", 0.01),
    "hum": (2, "avg", 0.01),
    "wind": (11, "avg", 0.1),
    "gust": (12, "max", 0.1),
    "rain": (14, "sum", 0.2794),
}
LEADS = [(0, 6), (6, 12), (12, 24), (24, 48)]

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts(
  issued INTEGER NOT NULL, model TEXT NOT NULL, valid INTEGER NOT NULL,
  var TEXT NOT NULL, value REAL NOT NULL,
  PRIMARY KEY(issued, model, valid, var)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS forecasts_valid ON forecasts(valid, var);
"""


def hour_floor(t):
    return int(t) // HOUR * HOUR


def parse(body):
    """Open-Meteo JSON (timeformat=unixtime, several models) -> rows of
    (model, valid, var, value). Open-Meteo stamps each hourly value with the
    END of the hour for sums (rain) and the instant for the rest; both are
    stored against the start of the hour they describe."""
    h = body.get("hourly") or {}
    times = h.get("time") or []
    rows = []
    for ovar, var in VARS.items():
        for model in MODELS:
            vals = h.get("%s_%s" % (ovar, model))
            if vals is None and len(MODELS) == 1:
                vals = h.get(ovar)
            if not vals:
                continue
            for t, v in zip(times, vals):
                if v is None:
                    continue
                valid = int(t) - HOUR if var == "rain" else hour_floor(int(t))
                rows.append((model, valid, var, float(v)))
    return rows


class Forecasts:
    def __init__(self, store, cfg, log=None, fetch=None):
        """cfg() -> the "forecast" config dict; fetch(url) -> parsed JSON
        (injected so the test needs no network)."""
        self.store = store
        self.cfg = cfg
        self.log = log or (lambda msg: None)
        self.fetch = fetch or self._fetch
        with store.db() as c:
            c.executescript(SCHEMA)

    @staticmethod
    def _fetch(url):
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())

    def url(self, lat, lon):
        q = {
            "latitude": "%.3f" % lat, "longitude": "%.3f" % lon,
            "hourly": ",".join(VARS), "models": ",".join(MODELS),
            "forecast_hours": str(HOURS_AHEAD), "timeformat": "unixtime",
            "wind_speed_unit": "kmh", "temperature_unit": "celsius", "precipitation_unit": "mm",
        }
        return URL + "?" + urllib.parse.urlencode(q)

    def collect(self, now=None):
        """Fetch once and store. Returns rows stored (0 when not configured)."""
        c = self.cfg() or {}
        if not c.get("enabled", True) or c.get("lat") is None or c.get("lon") is None:
            return 0
        now = int(now or time.time())
        issued = hour_floor(now)
        rows = [(issued, m, v, var, val) for (m, v, var, val) in parse(self.fetch(self.url(c["lat"], c["lon"])))
                if v >= issued]
        with self.store.db() as db:
            db.executemany("INSERT OR REPLACE INTO forecasts VALUES(?,?,?,?,?)", rows)
            db.execute("DELETE FROM forecasts WHERE issued < ?", (now - KEEP_DAYS * 86400,))
        return len(rows)

    def score(self, station_node, days=14, now=None):
        """Mean absolute error and mean bias (forecast - measured) per model,
        variable and lead-time bucket, over hours the station summarised."""
        now = int(now or time.time())
        since = now - days * 86400
        out = {}
        with self.store.db() as db:
            for var, (slot, col, scale) in OBSERVED.items():
                obs = {r[0]: r[1] * scale for r in db.execute(
                    "SELECT ts, %s FROM rollup_hour WHERE node=? AND slot=? AND ts>=? AND %s IS NOT NULL"
                    % (col, col), (station_node, slot, since))}
                if not obs:
                    continue
                for model, valid, issued, value in db.execute(
                        "SELECT model, valid, issued, value FROM forecasts WHERE var=? AND valid>=? AND valid<?",
                        (var, since, now)):
                    if valid not in obs:
                        continue
                    lead = (valid - issued) / HOUR
                    bucket = next(("%d-%dh" % (a, b) for a, b in LEADS if a <= lead < b), None)
                    if not bucket:
                        continue
                    k = out.setdefault(model, {}).setdefault(var, {}).setdefault(bucket, [0, 0.0, 0.0])
                    err = value - obs[valid]
                    k[0] += 1; k[1] += abs(err); k[2] += err
        for model in out:
            for var in out[model]:
                for b, (n, ae, e) in list(out[model][var].items()):
                    out[model][var][b] = {"n": n, "mae": round(ae / n, 2), "bias": round(e / n, 2)}
        return out

    def run_once(self):
        try:
            n = self.collect()
            if n:
                self.log("forecasts: stored %d values from %d models" % (n, len(MODELS)))
        except Exception as e:                     # the network is allowed to fail
            self.log("forecasts: fetch failed: %s" % e)


class ForecastThread(threading.Thread):
    def __init__(self, fc):
        super().__init__(daemon=True, name="forecasts")
        self.fc = fc

    def run(self):
        time.sleep(90)
        while True:
            self.fc.run_once()
            # next fetch a few minutes past a 3-hour mark, when new runs are in
            now = time.time()
            nxt = (int(now) // EVERY_S + 1) * EVERY_S + 300
            time.sleep(max(60, nxt - now))
