from flask import Flask, jsonify, request
import requests
import os
import sqlite3
import json
import threading
from datetime import datetime, timedelta
from collections import defaultdict

app = Flask(__name__)

# ─── Load API key from .env ────────────────────────────────
def load_env():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

load_env()
LYFTA_API_KEY = os.environ.get('LYFTA_API_KEY', '')
LYFTA_BASE = 'https://my.lyfta.app'

# ─── Database ─────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), 'workouts.db')

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS workouts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lyfta_key   TEXT UNIQUE NOT NULL,
                date        TEXT NOT NULL,
                title       TEXT,
                total_volume REAL,
                raw_json    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exercise_sets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                workout_id    INTEGER NOT NULL,
                date          TEXT NOT NULL,
                exercise_name TEXT NOT NULL,
                weight        REAL NOT NULL,
                reps          INTEGER NOT NULL,
                e1rm          REAL,
                FOREIGN KEY (workout_id) REFERENCES workouts(id)
            );
            CREATE TABLE IF NOT EXISTS session_context (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                lyfta_key     TEXT UNIQUE NOT NULL,
                workout_date  TEXT NOT NULL,
                workout_title TEXT NOT NULL,
                rpe           INTEGER,
                sleep_quality INTEGER,
                soreness      INTEGER,
                nutrition     TEXT,
                notes         TEXT,
                logged_at     TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS known_1rm (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                exercise_name TEXT NOT NULL,
                date          TEXT NOT NULL,
                weight_lbs    REAL NOT NULL,
                notes         TEXT,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(exercise_name, date)
            );
            CREATE INDEX IF NOT EXISTS idx_sets_exercise ON exercise_sets(exercise_name);
            CREATE INDEX IF NOT EXISTS idx_sets_date     ON exercise_sets(date);
        ''')

def _insert_sets_for_workout(conn, wid, date_only, w):
    for ex in w.get('exercises', []):
        name = ex.get('excercise_name', '')
        if not name:
            continue
        for s in ex.get('sets', []):
            try:
                weight = float(s.get('weight') or 0)
                reps   = int(s.get('reps') or 0)
            except (ValueError, TypeError):
                continue
            if weight > 0 and reps > 0:
                conn.execute(
                    'INSERT INTO exercise_sets '
                    '(workout_id, date, exercise_name, weight, reps, e1rm) '
                    'VALUES (?,?,?,?,?,?)',
                    (wid, date_only, name, weight, reps, epley_1rm(weight, reps))
                )

def rebuild_exercise_sets():
    """Clear and rebuild exercise_sets from stored raw_json. Fixes duplicate-row bugs."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM exercise_sets')
        rows = conn.execute('SELECT id, raw_json FROM workouts').fetchall()
        for wid, raw in rows:
            w = json.loads(raw)
            date_str = w.get('workout_perform_date', '')
            try:
                date_only = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d')
            except ValueError:
                continue
            _insert_sets_for_workout(conn, wid, date_only, w)

def upsert_workouts_to_db(workouts):
    with sqlite3.connect(DB_PATH) as conn:
        for w in workouts:
            date_str  = w.get('workout_perform_date', '')
            title     = w.get('title', '')
            total_vol = w.get('total_volume', 0) or 0
            try:
                dt        = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                date_only = dt.strftime('%Y-%m-%d')
            except ValueError:
                continue
            lyfta_key = f"{date_only}|{title}"
            # INSERT OR IGNORE preserves the existing row ID so the FK in
            # exercise_sets stays valid; UPDATE keeps the data current.
            conn.execute(
                'INSERT OR IGNORE INTO workouts (lyfta_key, date, title, total_volume, raw_json) '
                'VALUES (?,?,?,?,?)',
                (lyfta_key, date_only, title, total_vol, json.dumps(w))
            )
            conn.execute(
                'UPDATE workouts SET date=?, title=?, total_volume=?, raw_json=? WHERE lyfta_key=?',
                (date_only, title, total_vol, json.dumps(w), lyfta_key)
            )
            row = conn.execute('SELECT id FROM workouts WHERE lyfta_key=?', (lyfta_key,)).fetchone()
            if not row:
                continue
            wid = row[0]
            conn.execute('DELETE FROM exercise_sets WHERE workout_id=?', (wid,))
            _insert_sets_for_workout(conn, wid, date_only, w)

def load_workouts_from_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                'SELECT raw_json FROM workouts ORDER BY date ASC'
            ).fetchall()
            return [json.loads(r[0]) for r in rows]
    except Exception:
        return []

init_db()

# ─── In-memory cache ──────────────────────────────────────
_cache = {'workouts': None, 'last_refresh': None, 'error': None}
_lock = threading.Lock()

# ─── Lyfta fetch ──────────────────────────────────────────
def fetch_all_workouts():
    headers = {'Authorization': f'Bearer {LYFTA_API_KEY}'}
    all_workouts = []
    page = 1
    while True:
        resp = requests.get(
            f'{LYFTA_BASE}/api/v1/workouts',
            headers=headers,
            params={'limit': 100, 'page': page},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        all_workouts.extend(data.get('workouts', []))
        if page >= data.get('total_pages', 1):
            break
        page += 1
    return all_workouts

def refresh_cache():
    with _lock:
        try:
            workouts = fetch_all_workouts()
            _cache['workouts'] = workouts
            _cache['last_refresh'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _cache['error'] = None
            upsert_workouts_to_db(workouts)
        except Exception as e:
            _cache['error'] = str(e)

def get_workouts():
    if _cache['workouts'] is None:
        db_workouts = load_workouts_from_db()
        if db_workouts:
            with _lock:
                if _cache['workouts'] is None:
                    _cache['workouts'] = db_workouts
        else:
            refresh_cache()
    return _cache['workouts'] or []

# ─── Data processing ──────────────────────────────────────
def epley_1rm(weight, reps):
    if reps <= 0 or weight <= 0:
        return 0
    if reps == 1:
        return weight
    return round(weight * (1 + reps / 30), 1)

def process_data(workouts):
    sessions = []
    exercise_history = defaultdict(list)  # name -> [{date, weight, reps, e1rm}]
    exercise_stats = {}

    for w in workouts:
        date_str = w.get('workout_perform_date', '')
        title = w.get('title', 'Session')
        total_vol = w.get('total_volume', 0) or 0

        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue

        ex_names = []
        for ex in w.get('exercises', []):
            name = ex.get('excercise_name', '')
            if not name:
                continue
            if name not in ex_names:
                ex_names.append(name)

            for s in ex.get('sets', []):
                try:
                    weight = float(s.get('weight') or 0)
                    reps   = int(s.get('reps') or 0)
                except (ValueError, TypeError):
                    continue

                if weight > 0 and reps > 0:
                    e1rm = epley_1rm(weight, reps)
                    exercise_history[name].append({
                        'date': dt.strftime('%Y-%m-%d'),
                        'date_ts': dt.timestamp(),
                        'weight': weight,
                        'reps': reps,
                        'e1rm': e1rm,
                        'session': title,
                    })

        sessions.append({
            'title': title,
            'date': dt.strftime('%Y-%m-%d'),
            'date_display': dt.strftime('%b %d, %Y'),
            'weekday': dt.strftime('%A'),
            'total_volume': total_vol,
            'exercises': ex_names,
        })

    sessions.sort(key=lambda x: x['date'], reverse=True)

    # Per-exercise aggregates
    for name, records in exercise_history.items():
        max_weight = max(r['weight'] for r in records)
        max_e1rm   = max(r['e1rm']   for r in records)
        best       = max(records, key=lambda r: r['e1rm'])
        last_date  = max(r['date'] for r in records)
        total_sets = len(records)
        total_vol  = sum(r['weight'] * r['reps'] for r in records)
        exercise_stats[name] = {
            'name':         name,
            'max_weight':   max_weight,
            'max_e1rm':     round(max_e1rm, 1),
            'best_set':     f"{best['weight']}lbs x {best['reps']}",
            'last_date':    last_date,
            'total_sets':   total_sets,
            'total_volume': round(total_vol),
        }

    return sessions, exercise_stats, exercise_history

# ─── Routes ───────────────────────────────────────────────
@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    refresh_cache()
    if _cache['error']:
        return jsonify({'ok': False, 'error': _cache['error']}), 500
    return jsonify({'ok': True, 'last_refresh': _cache['last_refresh'],
                    'count': len(_cache['workouts'] or [])})

@app.route('/api/status')
def api_status():
    return jsonify({'last_refresh': _cache['last_refresh'], 'error': _cache['error']})

@app.route('/api/dashboard')
def api_dashboard():
    workouts = get_workouts()
    sessions, exercise_stats, _ = process_data(workouts)

    total_sessions = len(sessions)
    total_sets = 0
    for w in workouts:
        for ex in w.get('exercises', []):
            for s in ex.get('sets', []):
                try:
                    if float(s.get('weight') or 0) > 0 and int(s.get('reps') or 0) > 0:
                        total_sets += 1
                except (ValueError, TypeError):
                    pass

    total_volume   = sum(s['total_volume'] for s in sessions)
    unique_ex      = len(exercise_stats)

    from collections import Counter
    week_counts = Counter()
    for s in sessions:
        dt   = datetime.strptime(s['date'], '%Y-%m-%d')
        week = dt.strftime('%Y-W%W')
        week_counts[week] += 1
    avg_per_week = round(sum(week_counts.values()) / max(len(week_counts), 1), 1)

    top_exercises = sorted(exercise_stats.values(), key=lambda x: x['total_sets'], reverse=True)[:8]

    return jsonify({
        'total_sessions': total_sessions,
        'total_sets':     total_sets,
        'total_volume':   total_volume,
        'unique_exercises': unique_ex,
        'avg_per_week':   avg_per_week,
        'top_exercises':  [{'name': e['name'], 'sets': e['total_sets']} for e in top_exercises],
        'recent_sessions': sessions[:10],
        'last_refresh':   _cache['last_refresh'],
    })

@app.route('/api/goals')
def api_goals():
    workouts = get_workouts()
    _, _, exercise_history = process_data(workouts)

    bench_records = exercise_history.get('Bench Press', [])
    squat_records = exercise_history.get('Full Squat', [])

    def goal_data(records, goal):
        if not records:
            return {'goal': goal, 'max_weight': 0, 'max_e1rm': 0,
                    'pct': 0, 'e1rm_pct': 0, 'timeline': [], 'top_sets': []}
        max_weight = max(r['weight'] for r in records)
        max_e1rm   = max(r['e1rm']   for r in records)
        by_date = defaultdict(list)
        for r in records:
            by_date[r['date']].append(r)
        timeline = []
        for date in sorted(by_date.keys()):
            s_max  = max(r['weight'] for r in by_date[date])
            s_e1rm = max(r['e1rm']   for r in by_date[date])
            timeline.append({'date': date, 'session_max': s_max,
                             'session_e1rm': round(s_e1rm, 1)})
        top_sets = sorted(records, key=lambda r: r['e1rm'], reverse=True)[:5]
        return {
            'goal':       goal,
            'max_weight': max_weight,
            'max_e1rm':   round(max_e1rm, 1),
            'pct':        round(min(max_weight / goal * 100, 100), 1),
            'e1rm_pct':   round(min(max_e1rm   / goal * 100, 100), 1),
            'timeline':   timeline,
            'top_sets':   [{'date': r['date'], 'weight': r['weight'],
                            'reps': r['reps'], 'e1rm': r['e1rm']} for r in top_sets],
        }

    return jsonify({
        'bench': goal_data(bench_records, 225),
        'squat': goal_data(squat_records, 315),
    })

@app.route('/api/exercises')
def api_exercises():
    workouts = get_workouts()
    _, exercise_stats, _ = process_data(workouts)
    stats = sorted(exercise_stats.values(), key=lambda x: x['total_volume'], reverse=True)
    return jsonify(stats)

@app.route('/api/history')
def api_history():
    workouts = get_workouts()
    sessions, _, _ = process_data(workouts)
    return jsonify(sessions)

@app.route('/api/exercise/<name>')
def api_exercise_detail(name):
    workouts = get_workouts()
    _, exercise_stats, exercise_history = process_data(workouts)
    records = sorted(exercise_history.get(name, []), key=lambda r: r['date'])
    return jsonify({
        'name':    name,
        'records': records,
        'stats':   exercise_stats.get(name, {}),
    })

# ─── Analysis engine ──────────────────────────────────────
import numpy as np
from scipy import stats as sp_stats

def _exercise_sessions(exercise_name):
    """Per-session best weight, sets, and volume for one exercise."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('''
            SELECT date,
                   MAX(weight)          AS best_weight,
                   COUNT(*)             AS total_sets,
                   SUM(weight * reps)   AS total_volume
            FROM   exercise_sets
            WHERE  exercise_name = ?
            GROUP  BY date
            ORDER  BY date ASC
        ''', (exercise_name,)).fetchall()
    return [{'date': r[0], 'best_weight': r[1],
             'total_sets': r[2], 'total_volume': r[3]} for r in rows]

def _rolling_max(values, window=3):
    """Smooths noise by taking the max over a rolling window."""
    out = []
    for i in range(len(values)):
        out.append(max(values[max(0, i - window + 1): i + 1]))
    return out

def _linear_trend(dates, values):
    """OLS regression. Returns slope in lbs/week and R²."""
    if len(values) < 3:
        return None
    d0 = datetime.strptime(dates[0], '%Y-%m-%d')
    x  = np.array([(datetime.strptime(d, '%Y-%m-%d') - d0).days for d in dates], dtype=float)
    y  = np.array(values, dtype=float)
    slope, intercept, r, _, _ = sp_stats.linregress(x, y)
    predicted = (intercept + slope * x).tolist()
    return {
        'slope_per_week': round(float(slope) * 7, 2),
        'r_squared':      round(float(r ** 2), 3),
        'intercept':      float(intercept),
        'predicted':      predicted,
    }

def _off_days(sessions, window=4, threshold=0.12):
    """Flag sessions >12 % below the rolling average of the prior N sessions."""
    flagged = []
    weights = [s['best_weight'] for s in sessions]
    for i, s in enumerate(sessions):
        if i < window:
            continue
        avg = np.mean(weights[i - window: i])
        if avg > 0 and (weights[i] - avg) / avg < -threshold:
            flagged.append({
                'date':          s['date'],
                'deviation_pct': round(((weights[i] - avg) / avg) * 100, 1),
            })
    return flagged

def _weekly_volume(exercise_name, weeks=10):
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('''
            SELECT strftime('%Y-W%W', date) AS week,
                   SUM(weight * reps)       AS volume,
                   COUNT(*)                 AS sets
            FROM   exercise_sets
            WHERE  exercise_name = ?
            GROUP  BY week
            ORDER  BY week DESC
            LIMIT  ?
        ''', (exercise_name, weeks)).fetchall()
    return [{'week': r[0], 'volume': round(r[1]), 'sets': r[2]}
            for r in reversed(rows)]

def analyze_exercise(exercise_name):
    sessions = _exercise_sessions(exercise_name)
    if len(sessions) < 4:
        return None

    dates   = [s['date']        for s in sessions]
    weights = [s['best_weight'] for s in sessions]
    smoothed = _rolling_max(weights, window=3)

    trend = _linear_trend(dates, smoothed)

    # Recent 4-week trend (detect stalls even when all-time trend is positive)
    cutoff = (datetime.now() - timedelta(days=28)).strftime('%Y-%m-%d')
    recent_s = [s for s in sessions if s['date'] >= cutoff]
    recent_weights  = [s['best_weight'] for s in recent_s]
    recent_smoothed = _rolling_max(recent_weights, window=3)
    recent_trend = (
        _linear_trend([s['date'] for s in recent_s], recent_smoothed)
        if len(recent_s) >= 3 else None
    )

    return {
        'name':         exercise_name,
        'session_count': len(sessions),
        'all_time_max': max(weights),
        'current_max':  weights[-1],
        'dates':        dates,
        'weights':      weights,
        'smoothed':     smoothed,
        'trend':        trend,
        'recent_trend': recent_trend,
        'off_days':     _off_days(sessions),
        'weekly_volume': _weekly_volume(exercise_name),
    }

def fatigue_indicator():
    """Compare last 4 weeks volume load to prior 4 weeks."""
    with sqlite3.connect(DB_PATH) as conn:
        recent = conn.execute(
            "SELECT COALESCE(SUM(weight*reps),0) FROM exercise_sets "
            "WHERE date >= date('now','-28 days')"
        ).fetchone()[0]
        prior = conn.execute(
            "SELECT COALESCE(SUM(weight*reps),0) FROM exercise_sets "
            "WHERE date >= date('now','-56 days') AND date < date('now','-28 days')"
        ).fetchone()[0]

    pct = round(((recent - prior) / prior * 100), 1) if prior else 0
    level = ('high' if pct > 20 else
             'elevated' if pct > 5 else
             'low' if pct < -20 else 'normal')
    return {'level': level, 'pct_change': pct,
            'recent_4wk': round(recent), 'prev_4wk': round(prior)}

def context_correlations():
    """Correlate sleep/soreness with total session volume (needs ≥5 entries)."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute('''
            SELECT sc.sleep_quality, sc.soreness, sc.nutrition, w.total_volume
            FROM   session_context sc
            JOIN   workouts w ON w.lyfta_key = sc.lyfta_key
            WHERE  sc.sleep_quality IS NOT NULL AND w.total_volume > 0
        ''').fetchall()
    if len(rows) < 5:
        return None

    by_sleep = {}
    for sleep, soreness, nutrition, vol in rows:
        by_sleep.setdefault(sleep, []).append(vol)

    avg_by_sleep = {k: round(np.mean(v)) for k, v in by_sleep.items()}

    # Simple Pearson correlation: sleep vs volume
    sleeps = [r[0] for r in rows]
    vols   = [r[3] for r in rows]
    r_val, p_val = sp_stats.pearsonr(sleeps, vols) if len(set(sleeps)) > 1 else (0, 1)

    return {
        'avg_volume_by_sleep': avg_by_sleep,
        'sleep_volume_r':  round(float(r_val), 3),
        'sleep_volume_p':  round(float(p_val), 3),
        'n': len(rows),
    }

def build_recommendations(analyses, fatigue):
    recs = []

    # Fatigue warning
    if fatigue['level'] == 'high':
        recs.append({
            'type': 'warning',
            'title': 'High fatigue accumulation — consider a deload',
            'detail': (f"Last 4 weeks: {fatigue['recent_4wk']:,} lbs total volume, "
                       f"up {fatigue['pct_change']}% vs prior 4 weeks. "
                       "A lighter week now often precedes a PR."),
        })
    elif fatigue['level'] == 'low':
        recs.append({
            'type': 'info',
            'title': 'Volume is down recently — push harder if feeling fresh',
            'detail': (f"Last 4 weeks volume is {abs(fatigue['pct_change'])}% below "
                       "your prior 4-week average."),
        })

    for name, a in analyses.items():
        if not a:
            continue
        trend        = a.get('trend')
        recent_trend = a.get('recent_trend')
        off          = a.get('off_days', [])

        # Stall detection
        if (trend and recent_trend and
                trend['slope_per_week'] > 0.5 and
                abs(recent_trend['slope_per_week']) < 0.5):
            recs.append({
                'type': 'stall',
                'title': f'{name} has stalled the last 4 weeks',
                'detail': (f"All-time trend: {trend['slope_per_week']:+.1f} lbs/week, "
                           f"recent 4 weeks: {recent_trend['slope_per_week']:+.1f} lbs/week. "
                           "Try a rep scheme change or a planned deload."),
            })
        elif trend and trend['slope_per_week'] >= 1.0:
            recs.append({
                'type': 'progress',
                'title': f'{name} is progressing well',
                'detail': (f"Gaining ~{trend['slope_per_week']:.1f} lbs/week "
                           f"(R²={trend['r_squared']} — "
                           f"{'consistent' if trend['r_squared'] > 0.65 else 'variable'} progress)."),
            })

        # Repeated off-days
        recent_off = [d for d in off
                      if d['date'] >= (datetime.now() - timedelta(days=28)).strftime('%Y-%m-%d')]
        if len(recent_off) >= 2:
            recs.append({
                'type': 'warning',
                'title': f'{name}: {len(recent_off)} low-performance sessions in last 4 weeks',
                'detail': ("Performance dropped >12% below your rolling average on multiple days. "
                           "Log session context (sleep, soreness) to help identify the cause."),
            })

    if not recs:
        recs.append({
            'type': 'info',
            'title': 'Keep training — not enough history for deeper insights yet',
            'detail': 'Patterns become clearer with more sessions in the database.',
        })

    return recs

# ─── Analysis routes ──────────────────────────────────────
@app.route('/api/context', methods=['POST'])
def api_log_context():
    data          = request.get_json() or {}
    lyfta_key     = data.get('lyfta_key', '').strip()
    workout_date  = data.get('workout_date', '').strip()
    workout_title = data.get('workout_title', '').strip()
    rpe           = data.get('rpe')
    sleep_quality = data.get('sleep_quality')
    soreness      = data.get('soreness')
    nutrition     = data.get('nutrition', '')
    notes         = data.get('notes', '')
    if not lyfta_key:
        return jsonify({'ok': False, 'error': 'Missing session key'}), 400
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO session_context '
                '(lyfta_key, workout_date, workout_title, rpe, sleep_quality, soreness, nutrition, notes) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (lyfta_key, workout_date, workout_title, rpe, sleep_quality, soreness, nutrition, notes)
            )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/context', methods=['GET'])
def api_get_contexts():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM session_context ORDER BY workout_date DESC'
            ).fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])

@app.route('/api/known_1rm', methods=['POST'])
def api_log_known_1rm():
    data      = request.get_json() or {}
    exercise  = data.get('exercise_name', '').strip()
    date_val  = data.get('date', '').strip()
    weight    = data.get('weight_lbs')
    notes     = data.get('notes', '')
    if not exercise or not date_val or weight is None:
        return jsonify({'ok': False, 'error': 'Missing required fields'}), 400
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO known_1rm (exercise_name, date, weight_lbs, notes) VALUES (?,?,?,?)',
                (exercise, date_val, float(weight), notes)
            )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/known_1rm', methods=['GET'])
def api_get_known_1rms():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM known_1rm ORDER BY date DESC'
            ).fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception:
        return jsonify([])

@app.route('/api/analysis')
def api_analysis():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            workout_count = conn.execute('SELECT COUNT(*) FROM workouts').fetchone()[0]
            set_count     = conn.execute('SELECT COUNT(*) FROM exercise_sets').fetchone()[0]
            context_count = conn.execute('SELECT COUNT(*) FROM session_context').fetchone()[0]
            rm_count      = conn.execute('SELECT COUNT(*) FROM known_1rm').fetchone()[0]
            # Top exercises by distinct session count (need ≥4 sessions for trend)
            top_ex = conn.execute('''
                SELECT exercise_name, COUNT(DISTINCT date) AS sessions
                FROM   exercise_sets
                GROUP  BY exercise_name
                HAVING sessions >= 4
                ORDER  BY sessions DESC
                LIMIT  8
            ''').fetchall()

        analyses = {name: analyze_exercise(name) for name, _ in top_ex}
        fatigue  = fatigue_indicator()
        recs     = build_recommendations(analyses, fatigue)
        ctx      = context_correlations()

        return jsonify({
            'db_workout_count':  workout_count,
            'db_set_count':      set_count,
            'context_entries':   context_count,
            'known_1rm_entries': rm_count,
            'status':            'active',
            'analyses':          analyses,
            'fatigue':           fatigue,
            'recommendations':   recs,
            'context_insights':  ctx,
        })
    except Exception as e:
        return jsonify({
            'db_workout_count': 0, 'db_set_count': 0,
            'context_entries': 0, 'known_1rm_entries': 0,
            'status': 'error', 'error': str(e),
            'analyses': {}, 'fatigue': {}, 'recommendations': [], 'context_insights': None,
        })

# ─── Frontend ─────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Workout Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d0d0f;
    --surface: #16181c;
    --surface2: #1e2128;
    --border: #2a2d36;
    --accent: #6c63ff;
    --accent2: #00d4aa;
    --accent3: #ff6b6b;
    --accent4: #ffa94d;
    --text: #e8eaf0;
    --text-muted: #7a7f8e;
    --text-dim: #4a4f5e;
    --radius: 12px;
    --radius-sm: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    min-height: 100vh;
  }

  /* Header */
  .header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 28px;
    display: flex;
    align-items: center;
    gap: 24px;
    height: 60px;
    position: sticky;
    top: 0;
    z-index: 100;
  }
  .logo { font-size: 18px; font-weight: 700; color: var(--accent); letter-spacing: -0.5px; white-space: nowrap; }
  .logo span { color: var(--text-muted); font-weight: 400; }
  .tabs { display: flex; gap: 4px; flex: 1; }
  .tab {
    padding: 6px 16px; border-radius: 6px; cursor: pointer;
    color: var(--text-muted); font-size: 13px; font-weight: 500;
    transition: all 0.15s; border: none; background: none; white-space: nowrap;
  }
  .tab:hover { color: var(--text); background: var(--surface2); }
  .tab.active { color: var(--text); background: var(--surface2); box-shadow: inset 0 -2px 0 var(--accent); }

  /* Refresh button */
  .refresh-btn {
    display: flex; align-items: center; gap: 6px;
    padding: 6px 14px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--surface2); color: var(--text-muted); font-size: 12px;
    cursor: pointer; transition: all 0.15s; white-space: nowrap;
  }
  .refresh-btn:hover { color: var(--text); border-color: var(--accent); }
  .refresh-btn.spinning .refresh-icon { animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .refresh-icon { display: inline-block; font-size: 13px; }
  .last-refresh { font-size: 11px; color: var(--text-dim); white-space: nowrap; }

  /* Layout */
  .page { display: none; padding: 28px; max-width: 1400px; margin: 0 auto; }
  .page.active { display: block; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
  .grid-4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }

  /* Cards */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; }
  .card-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); margin-bottom: 12px; font-weight: 600; }

  /* Stat cards */
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px 24px; }
  .stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); margin-bottom: 8px; font-weight: 600; }
  .stat-value { font-size: 36px; font-weight: 700; line-height: 1; letter-spacing: -1px; }
  .stat-sub { font-size: 12px; color: var(--text-muted); margin-top: 6px; }
  .accent-purple { color: var(--accent); }
  .accent-green  { color: var(--accent2); }
  .accent-red    { color: var(--accent3); }
  .accent-orange { color: var(--accent4); }

  /* Progress */
  .progress-wrap { margin: 16px 0; }
  .progress-label { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
  .progress-label-text { font-size: 13px; font-weight: 600; }
  .progress-label-pct { font-size: 20px; font-weight: 700; color: var(--accent2); }
  .progress-bar-bg { height: 10px; background: var(--surface2); border-radius: 99px; overflow: hidden; border: 1px solid var(--border); }
  .progress-bar-fill { height: 100%; border-radius: 99px; transition: width 1s cubic-bezier(.4,0,.2,1); }
  .progress-bar-fill.bench { background: linear-gradient(90deg, #6c63ff, #a78bfa); }
  .progress-bar-fill.squat { background: linear-gradient(90deg, #00d4aa, #34d399); }
  .progress-details { display: flex; gap: 20px; margin-top: 12px; }
  .progress-detail { display: flex; flex-direction: column; gap: 2px; }
  .progress-detail-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-dim); }
  .progress-detail-value { font-size: 16px; font-weight: 700; }

  /* Goal cards */
  .goal-hero { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  .goal-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 28px; position: relative; overflow: hidden; }
  .goal-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3px; }
  .goal-card.bench::before { background: linear-gradient(90deg, #6c63ff, #a78bfa); }
  .goal-card.squat::before { background: linear-gradient(90deg, #00d4aa, #34d399); }
  .goal-name { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--text-muted); margin-bottom: 6px; font-weight: 600; }
  .goal-weight { font-size: 48px; font-weight: 800; letter-spacing: -2px; line-height: 1; }
  .goal-weight .goal-unit { font-size: 20px; font-weight: 600; color: var(--text-muted); letter-spacing: 0; }
  .goal-target { font-size: 13px; color: var(--text-muted); margin-top: 4px; }

  /* Charts */
  .chart-container { position: relative; height: 220px; }
  .chart-container-lg { position: relative; height: 280px; }

  /* Tables */
  .mini-table { width: 100%; border-collapse: collapse; }
  .mini-table th { text-align: left; font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-dim); padding: 0 8px 8px; font-weight: 600; border-bottom: 1px solid var(--border); }
  .mini-table td { padding: 8px; font-size: 13px; border-bottom: 1px solid var(--border); color: var(--text); }
  .mini-table tr:last-child td { border-bottom: none; }
  .mini-table td:last-child { color: var(--accent2); font-weight: 600; }

  .ex-table { width: 100%; border-collapse: collapse; }
  .ex-table th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-muted); padding: 10px 14px; border-bottom: 1px solid var(--border); font-weight: 600; white-space: nowrap; cursor: pointer; user-select: none; }
  .ex-table th:hover { color: var(--text); }
  .ex-table td { padding: 12px 14px; font-size: 13px; border-bottom: 1px solid rgba(42,45,54,0.5); }
  .ex-table tr:hover td { background: var(--surface2); cursor: pointer; }
  .ex-table tr:last-child td { border-bottom: none; }
  .ex-name { font-weight: 600; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 600; background: var(--surface2); color: var(--accent2); border: 1px solid var(--border); }

  /* History */
  .session-list { display: flex; flex-direction: column; gap: 10px; }
  .session-item { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 16px 20px; cursor: pointer; transition: border-color 0.15s; }
  .session-item:hover { border-color: var(--accent); }
  .session-item.open { border-color: var(--accent); }
  .session-header { display: flex; align-items: center; gap: 16px; }
  .session-date-block { text-align: center; min-width: 44px; }
  .session-month { font-size: 10px; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.5px; }
  .session-day { font-size: 22px; font-weight: 700; line-height: 1; }
  .session-info { flex: 1; }
  .session-title { font-weight: 600; font-size: 14px; }
  .session-meta { font-size: 12px; color: var(--text-muted); margin-top: 3px; }
  .session-exercises { display: none; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--border); flex-wrap: wrap; gap: 6px; }
  .session-item.open .session-exercises { display: flex; }
  .ex-pill { display: inline-block; padding: 3px 10px; border-radius: 99px; font-size: 12px; background: var(--surface2); border: 1px solid var(--border); color: var(--text-muted); }

  /* Modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 200; align-items: center; justify-content: center; padding: 20px; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); width: 100%; max-width: 760px; max-height: 80vh; overflow-y: auto; padding: 28px; }
  .modal-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 20px; }
  .modal-title { font-size: 20px; font-weight: 700; }
  .modal-close { cursor: pointer; font-size: 20px; line-height: 1; padding: 4px; border: none; background: none; color: var(--text-muted); }
  .modal-close:hover { color: var(--text); }

  /* Toast */
  .toast {
    position: fixed; bottom: 24px; right: 24px; z-index: 300;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 18px; font-size: 13px;
    display: flex; align-items: center; gap: 8px;
    transform: translateY(80px); opacity: 0;
    transition: transform 0.3s, opacity 0.3s;
  }
  .toast.show { transform: translateY(0); opacity: 1; }
  .toast.success { border-color: var(--accent2); color: var(--accent2); }
  .toast.error   { border-color: var(--accent3); color: var(--accent3); }

  .section-gap { margin-top: 20px; }

  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

  .loading { display: flex; align-items: center; justify-content: center; height: 200px; color: var(--text-muted); font-size: 13px; }

  /* Context / Rating UI */
  .rating-row { display: flex; gap: 6px; flex-wrap: wrap; }
  .rating-btn {
    width: 36px; height: 36px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text-muted); font-size: 13px; font-weight: 600;
    cursor: pointer; transition: all 0.12s;
  }
  .rating-btn:hover { border-color: var(--accent); color: var(--text); }
  .rating-btn.selected { background: var(--accent); border-color: var(--accent); color: #fff; }
  .nutr-btn {
    padding: 8px 22px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text-muted); font-size: 13px; cursor: pointer; transition: all 0.12s;
  }
  .nutr-btn:hover { border-color: var(--accent); color: var(--text); }
  .nutr-btn.selected { border-color: var(--accent2); color: var(--accent2); background: rgba(0,212,170,0.08); }
  .log-ctx-btn {
    padding: 3px 10px; border-radius: 99px;
    border: 1px solid var(--border); background: var(--surface2);
    color: var(--text-dim); font-size: 11px; cursor: pointer; transition: all 0.12s;
    white-space: nowrap;
  }
  .log-ctx-btn:hover { border-color: var(--accent); color: var(--accent); }
  .log-ctx-btn.has-context { border-color: var(--accent2); color: var(--accent2); }

  @media (max-width: 900px) {
    .grid-4 { grid-template-columns: repeat(2, 1fr); }
    .grid-3 { grid-template-columns: 1fr 1fr; }
    .goal-hero, .grid-2 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<div class="header">
  <div class="logo">LIFT<span>TRACK</span></div>
  <div class="tabs">
    <button class="tab active" onclick="showTab('dashboard', this)">Dashboard</button>
    <button class="tab" onclick="showTab('goals', this)">Goals</button>
    <button class="tab" onclick="showTab('exercises', this)">Exercises</button>
    <button class="tab" onclick="showTab('history', this)">History</button>
    <button class="tab" onclick="showTab('analysis', this)">Analysis</button>
  </div>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="last-refresh" id="last-refresh-lbl"></span>
    <button class="refresh-btn" id="refresh-btn" onclick="doRefresh()">
      <span class="refresh-icon" id="refresh-icon">↻</span> Sync Lyfta
    </button>
  </div>
</div>

<!-- DASHBOARD -->
<div id="page-dashboard" class="page active">
  <div id="dash-loading" class="loading">Loading...</div>
  <div id="dash-content" style="display:none">
    <div class="grid-4" id="stat-cards"></div>
    <div class="grid-2 section-gap">
      <div class="card">
        <div class="card-title">Top Exercises by Total Sets</div>
        <div class="chart-container-lg"><canvas id="topExChart"></canvas></div>
      </div>
      <div class="card">
        <div class="card-title">Recent Sessions</div>
        <div id="recent-sessions"></div>
      </div>
    </div>
  </div>
</div>

<!-- GOALS -->
<div id="page-goals" class="page">
  <div id="goals-loading" class="loading">Loading...</div>
  <div id="goals-content" style="display:none">
    <div class="goal-hero">
      <div class="goal-card bench">
        <div class="goal-name">Bench Press Goal</div>
        <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:4px;">
          <div class="goal-weight" id="bench-max">—<span class="goal-unit">lbs</span></div>
          <div style="color:var(--text-muted);font-size:14px;">/ 225 lbs goal</div>
        </div>
        <div class="goal-target" id="bench-remaining"></div>
        <div class="progress-wrap" style="margin-top:20px;">
          <div class="progress-label">
            <span class="progress-label-text">Actual Max</span>
            <span class="progress-label-pct" id="bench-pct">0%</span>
          </div>
          <div class="progress-bar-bg"><div class="progress-bar-fill bench" id="bench-bar" style="width:0%"></div></div>
        </div>
        <div class="progress-wrap">
          <div class="progress-label">
            <span class="progress-label-text" style="color:var(--text-muted)">Est. 1RM</span>
            <span class="progress-label-pct" id="bench-e1rm-pct" style="color:var(--accent)">0%</span>
          </div>
          <div class="progress-bar-bg"><div class="progress-bar-fill" id="bench-e1rm-bar" style="width:0%;background:var(--accent)"></div></div>
        </div>
        <div class="progress-details">
          <div class="progress-detail"><span class="progress-detail-label">Max Lifted</span><span class="progress-detail-value accent-purple" id="bench-max-d">—</span></div>
          <div class="progress-detail"><span class="progress-detail-label">Est. 1RM</span><span class="progress-detail-value accent-purple" id="bench-e1rm-d">—</span></div>
          <div class="progress-detail"><span class="progress-detail-label">Remaining</span><span class="progress-detail-value" id="bench-left-d">—</span></div>
        </div>
      </div>
      <div class="goal-card squat">
        <div class="goal-name">Squat Goal</div>
        <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:4px;">
          <div class="goal-weight" id="squat-max">—<span class="goal-unit">lbs</span></div>
          <div style="color:var(--text-muted);font-size:14px;">/ 315 lbs goal</div>
        </div>
        <div class="goal-target" id="squat-remaining"></div>
        <div class="progress-wrap" style="margin-top:20px;">
          <div class="progress-label">
            <span class="progress-label-text">Actual Max</span>
            <span class="progress-label-pct" id="squat-pct">0%</span>
          </div>
          <div class="progress-bar-bg"><div class="progress-bar-fill squat" id="squat-bar" style="width:0%"></div></div>
        </div>
        <div class="progress-wrap">
          <div class="progress-label">
            <span class="progress-label-text" style="color:var(--text-muted)">Est. 1RM</span>
            <span class="progress-label-pct" id="squat-e1rm-pct" style="color:var(--accent2)">0%</span>
          </div>
          <div class="progress-bar-bg"><div class="progress-bar-fill squat" id="squat-e1rm-bar" style="width:0%"></div></div>
        </div>
        <div class="progress-details">
          <div class="progress-detail"><span class="progress-detail-label">Max Lifted</span><span class="progress-detail-value accent-green" id="squat-max-d">—</span></div>
          <div class="progress-detail"><span class="progress-detail-label">Est. 1RM</span><span class="progress-detail-value accent-green" id="squat-e1rm-d">—</span></div>
          <div class="progress-detail"><span class="progress-detail-label">Remaining</span><span class="progress-detail-value" id="squat-left-d">—</span></div>
        </div>
      </div>
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="card-title">Bench Press — Max Weight Per Session</div>
        <div class="chart-container-lg"><canvas id="benchChart"></canvas></div>
        <div style="margin-top:16px;"><div class="card-title">Top Sets</div><table class="mini-table" id="bench-top-sets"></table></div>
      </div>
      <div class="card">
        <div class="card-title">Full Squat — Max Weight Per Session</div>
        <div class="chart-container-lg"><canvas id="squatChart"></canvas></div>
        <div style="margin-top:16px;"><div class="card-title">Top Sets</div><table class="mini-table" id="squat-top-sets"></table></div>
      </div>
    </div>
  </div>
</div>

<!-- EXERCISES -->
<div id="page-exercises" class="page">
  <div id="ex-loading" class="loading">Loading...</div>
  <div id="ex-content" style="display:none">
    <div class="card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
        <input type="text" id="ex-search" placeholder="Search exercises…"
          style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 14px;color:var(--text);font-size:13px;width:260px;outline:none;"
          oninput="filterExercises()">
        <span style="font-size:12px;color:var(--text-muted)" id="ex-count"></span>
      </div>
      <div style="overflow-x:auto">
        <table class="ex-table">
          <thead><tr>
            <th onclick="sortEx('name')">Exercise</th>
            <th onclick="sortEx('max_weight')">Max Weight</th>
            <th onclick="sortEx('max_e1rm')">Est. 1RM</th>
            <th onclick="sortEx('total_sets')">Sets</th>
            <th onclick="sortEx('total_volume')">Volume (lbs)</th>
            <th onclick="sortEx('last_date')">Last Session</th>
          </tr></thead>
          <tbody id="ex-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- HISTORY -->
<div id="page-history" class="page">
  <div id="hist-loading" class="loading">Loading...</div>
  <div id="hist-content" style="display:none">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px;">
      <input type="text" id="hist-search" placeholder="Search by exercise or session name…"
        style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 14px;color:var(--text);font-size:13px;flex:1;max-width:400px;outline:none;"
        oninput="filterHistory()">
      <span style="font-size:12px;color:var(--text-muted)" id="hist-count"></span>
    </div>
    <div class="session-list" id="session-list"></div>
  </div>
</div>

<!-- ANALYSIS -->
<div id="page-analysis" class="page">
  <div id="analysis-loading" class="loading">Loading...</div>
  <div id="analysis-content" style="display:none">

    <div class="grid-3" id="analysis-stat-cards" style="margin-bottom:20px;"></div>

    <div id="recommendations-card" class="card" style="margin-bottom:20px;display:none;">
      <div class="card-title">Recommendations</div>
      <div id="recommendations-list" style="display:flex;flex-direction:column;gap:10px;"></div>
    </div>

    <div class="grid-2 section-gap" style="margin-bottom:20px;">
      <div class="card" id="fatigue-card">
        <div class="card-title">Fatigue Accumulation</div>
        <div id="fatigue-content" style="padding:8px 0;"></div>
      </div>
      <div class="card" id="context-insights-card" style="display:none;">
        <div class="card-title">Context Insights</div>
        <div id="context-insights-content"></div>
      </div>
    </div>

    <div id="trends-section" style="margin-bottom:20px;display:none;">
      <div class="card-title" style="margin-bottom:14px;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text-muted);font-weight:600;">Strength Trends</div>
      <div id="trends-grid" class="grid-2"></div>
    </div>

    <div class="card" style="margin-bottom:20px;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
        <div class="card-title" style="margin-bottom:0;">Known 1RM Log</div>
        <button onclick="openRmModal()" style="padding:6px 14px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--text-muted);font-size:12px;cursor:pointer;transition:all 0.15s;" onmouseover="this.style.borderColor='var(--accent2)';this.style.color='var(--accent2)'" onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-muted)'">+ Add 1RM</button>
      </div>
      <div id="rm-table-wrap">
        <div style="color:var(--text-muted);font-size:13px;padding:8px 0;">No known 1RMs logged yet.</div>
      </div>
    </div>

    <div class="card">
      <div class="card-title" style="margin-bottom:6px;">Session Context Log</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px;">
        Log context from the History tab — click any session then hit "Log Context."
      </div>
      <div id="context-table-wrap">
        <div style="color:var(--text-muted);font-size:13px;padding:8px 0;">No context logged yet.</div>
      </div>
    </div>

  </div>
</div>

<!-- Exercise detail modal -->
<div class="modal-overlay" id="ex-modal" onclick="closeModal(event)">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="modal-title">Exercise</div>
      <button class="modal-close" onclick="closeExModal()">✕</button>
    </div>
    <div class="grid-3" id="modal-stats" style="margin-bottom:20px;"></div>
    <div class="card-title">Max Weight Over Time</div>
    <div class="chart-container-lg" style="margin-bottom:20px;"><canvas id="modalChart"></canvas></div>
    <div class="card-title">All Sets</div>
    <div style="overflow-x:auto;margin-top:10px;"><table class="mini-table" id="modal-sets-table"></table></div>
  </div>
</div>

<!-- Context log modal -->
<div class="modal-overlay" id="context-modal" onclick="closeContextModal(event)">
  <div class="modal" style="max-width:500px;">
    <div class="modal-header">
      <div>
        <div class="modal-title">Log Session Context</div>
        <div id="context-modal-subtitle" style="font-size:12px;color:var(--text-muted);margin-top:4px;"></div>
      </div>
      <button class="modal-close" onclick="document.getElementById('context-modal').classList.remove('open')">&#x2715;</button>
    </div>
    <input type="hidden" id="ctx-lyfta-key">
    <input type="hidden" id="ctx-workout-date">
    <input type="hidden" id="ctx-workout-title">
    <div style="display:flex;flex-direction:column;gap:22px;">
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Overall RPE — how hard did it feel?</div>
        <div class="rating-row" id="ctx-rpe-row"></div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-dim);margin-top:4px;"><span>Very Easy</span><span>Maximal</span></div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Sleep Quality — night before</div>
        <div class="rating-row" id="ctx-sleep-row"></div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-dim);margin-top:4px;"><span>Terrible</span><span>Great</span></div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Soreness Level</div>
        <div class="rating-row" id="ctx-soreness-row"></div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-dim);margin-top:4px;"><span>None</span><span>Very Sore</span></div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Pre-workout Nutrition</div>
        <div style="display:flex;gap:8px;">
          <button class="nutr-btn" data-val="good"  onclick="selectNutrition('good')">Good</button>
          <button class="nutr-btn" data-val="okay"  onclick="selectNutrition('okay')">Okay</button>
          <button class="nutr-btn" data-val="poor"  onclick="selectNutrition('poor')">Poor</button>
        </div>
      </div>
      <div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Notes (optional)</div>
        <textarea id="ctx-notes" placeholder="e.g. felt tired, skipped warm-up, PR attempt…"
          style="width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:10px 12px;color:var(--text);font-size:13px;resize:vertical;min-height:70px;outline:none;font-family:inherit;"></textarea>
      </div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:24px;">
      <button onclick="document.getElementById('context-modal').classList.remove('open')"
        style="padding:8px 18px;border-radius:6px;border:1px solid var(--border);background:none;color:var(--text-muted);font-size:13px;cursor:pointer;">Cancel</button>
      <button onclick="saveContext()"
        style="padding:8px 18px;border-radius:6px;border:none;background:var(--accent);color:#fff;font-size:13px;font-weight:600;cursor:pointer;">Save Context</button>
    </div>
  </div>
</div>

<!-- Known 1RM modal -->
<div class="modal-overlay" id="rm-modal" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="modal" style="max-width:420px;">
    <div class="modal-header">
      <div class="modal-title">Log Known 1RM</div>
      <button class="modal-close" onclick="document.getElementById('rm-modal').classList.remove('open')">&#x2715;</button>
    </div>
    <div style="display:flex;flex-direction:column;gap:16px;">
      <div>
        <label style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Exercise Name</label>
        <input type="text" id="rm-exercise" placeholder="e.g. Bench Press, Full Squat"
          style="width:100%;margin-top:6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;outline:none;">
      </div>
      <div>
        <label style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Date Tested</label>
        <input type="date" id="rm-date"
          style="width:100%;margin-top:6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;outline:none;">
      </div>
      <div>
        <label style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Weight (lbs)</label>
        <input type="number" id="rm-weight" placeholder="225" min="1"
          style="width:100%;margin-top:6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;outline:none;">
      </div>
      <div>
        <label style="font-size:11px;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.8px;font-weight:600;">Notes (optional)</label>
        <input type="text" id="rm-notes" placeholder="e.g. gym test, competition"
          style="width:100%;margin-top:6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;outline:none;">
      </div>
    </div>
    <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:24px;">
      <button onclick="document.getElementById('rm-modal').classList.remove('open')"
        style="padding:8px 18px;border-radius:6px;border:1px solid var(--border);background:none;color:var(--text-muted);font-size:13px;cursor:pointer;">Cancel</button>
      <button onclick="saveKnownRm()"
        style="padding:8px 18px;border-radius:6px;border:none;background:var(--accent2);color:#000;font-size:13px;font-weight:600;cursor:pointer;">Save 1RM</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
// ─── Toast ───────────────────────────────────────────────
function showToast(msg, type = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  setTimeout(() => t.className = 'toast', 3000);
}

// ─── Refresh ─────────────────────────────────────────────
async function doRefresh() {
  const btn  = document.getElementById('refresh-btn');
  const icon = document.getElementById('refresh-icon');
  btn.disabled = true;
  btn.classList.add('spinning');
  icon.textContent = '↻';
  try {
    const r = await fetch('/api/refresh', { method: 'POST' });
    const d = await r.json();
    if (d.ok) {
      setLastRefresh(d.last_refresh);
      showToast(`✓ Synced ${d.count} workouts from Lyfta`, 'success');
      // Invalidate all loaded tabs and reload current
      Object.keys(loaded).forEach(k => loaded[k] = false);
      const active = document.querySelector('.page.active');
      if (active) {
        const name = active.id.replace('page-', '');
        loaded[name] = true;
        loadTab(name);
      }
    } else {
      showToast('Sync failed: ' + d.error, 'error');
    }
  } catch (e) {
    showToast('Network error during sync', 'error');
  } finally {
    btn.disabled = false;
    btn.classList.remove('spinning');
  }
}

function setLastRefresh(ts) {
  const el = document.getElementById('last-refresh-lbl');
  el.textContent = ts ? 'Synced ' + ts : '';
}

// ─── Routing ─────────────────────────────────────────────
const loaded = {};
function showTab(name, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  el.classList.add('active');
  if (!loaded[name]) { loaded[name] = true; loadTab(name); }
}
function loadTab(name) {
  if (name === 'dashboard') loadDashboard();
  else if (name === 'goals') loadGoals();
  else if (name === 'exercises') loadExercises();
  else if (name === 'history') loadHistory();
  else if (name === 'analysis') loadAnalysis();
}

// ─── Chart helper ─────────────────────────────────────────
function makeChart(id, type, labels, datasets, extra = {}) {
  const ctx = document.getElementById(id);
  if (!ctx) return null;
  if (ctx._chart) ctx._chart.destroy();
  const chart = new Chart(ctx, {
    type, data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e', maxTicksLimit: 8, maxRotation: 30 } },
        y: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e' } }
      },
      ...extra
    }
  });
  ctx._chart = chart;
  return chart;
}

function fmt(n) { return n ? Number(n).toLocaleString() : '—'; }

// ─── Dashboard ───────────────────────────────────────────
async function loadDashboard() {
  document.getElementById('dash-loading').style.display = '';
  document.getElementById('dash-content').style.display = 'none';
  const r = await fetch('/api/dashboard');
  const d = await r.json();
  setLastRefresh(d.last_refresh);

  document.getElementById('dash-loading').style.display = 'none';
  document.getElementById('dash-content').style.display = '';

  const cards = [
    { label: 'Total Workouts',  value: d.total_sessions, sub: 'sessions logged',        cls: 'accent-purple' },
    { label: 'Total Volume',    value: fmt(d.total_volume) + ' lbs', sub: 'all-time lifted', cls: 'accent-green' },
    { label: 'Total Sets',      value: fmt(d.total_sets), sub: 'sets completed',         cls: 'accent-orange' },
    { label: 'Avg / Week',      value: d.avg_per_week, sub: d.unique_exercises + ' unique exercises', cls: 'accent-red' },
  ];
  document.getElementById('stat-cards').innerHTML = cards.map(c => `
    <div class="stat-card">
      <div class="stat-label">${c.label}</div>
      <div class="stat-value ${c.cls}">${c.value}</div>
      <div class="stat-sub">${c.sub}</div>
    </div>`).join('');

  const exLabels = d.top_exercises.map(e => e.name.length > 22 ? e.name.slice(0, 20) + '…' : e.name);
  makeChart('topExChart', 'bar', exLabels,
    [{ data: d.top_exercises.map(e => e.sets),
       backgroundColor: ['#6c63ff','#7c6ffa','#8c7cf5','#9c89f0','#ac96eb','#bca3e6','#ccb0e1','#dcbddc'],
       borderRadius: 6, borderSkipped: false }],
    { indexAxis: 'y', plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e' } },
        y: { grid: { display: false }, ticks: { color: '#e8eaf0', font: { size: 12 } } }
      }
    }
  );

  document.getElementById('recent-sessions').innerHTML = d.recent_sessions.map(s => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border);">
      <div>
        <div style="font-weight:600;font-size:13px;">${s.date_display}</div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:2px;">${s.weekday} · ${s.exercises.length} exercises</div>
      </div>
      <div style="font-size:12px;color:var(--text-muted)">${fmt(s.total_volume)} lbs</div>
    </div>`).join('');
}

// ─── Goals ───────────────────────────────────────────────
async function loadGoals() {
  document.getElementById('goals-loading').style.display = '';
  document.getElementById('goals-content').style.display = 'none';
  const r = await fetch('/api/goals');
  const d = await r.json();

  document.getElementById('goals-loading').style.display = 'none';
  document.getElementById('goals-content').style.display = '';

  function fillGoal(prefix, g) {
    document.getElementById(prefix + '-max').innerHTML = `${g.max_weight}<span class="goal-unit">lbs</span>`;
    const left = Math.max(0, g.goal - g.max_weight);
    document.getElementById(prefix + '-remaining').textContent =
      left === 0 ? '🎯 Goal Achieved!' : `${left} lbs to go`;
    document.getElementById(prefix + '-pct').textContent = g.pct + '%';
    document.getElementById(prefix + '-e1rm-pct').textContent = g.e1rm_pct + '%';
    document.getElementById(prefix + '-max-d').textContent = g.max_weight + ' lbs';
    document.getElementById(prefix + '-e1rm-d').textContent = g.max_e1rm + ' lbs';
    document.getElementById(prefix + '-left-d').textContent = left ? left + ' lbs' : '✓ Done';
    setTimeout(() => {
      document.getElementById(prefix + '-bar').style.width = g.pct + '%';
      document.getElementById(prefix + '-e1rm-bar').style.width = g.e1rm_pct + '%';
    }, 100);
  }
  fillGoal('bench', d.bench);
  fillGoal('squat', d.squat);

  function lineChartOpts(min) {
    return {
      plugins: { legend: { display: true, labels: { color: '#7a7f8e', boxWidth: 12 } } },
      scales: {
        x: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e', maxTicksLimit: 8 } },
        y: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e' }, min }
      }
    };
  }

  const b = d.bench;
  makeChart('benchChart', 'line', b.timeline.map(t => t.date.slice(5)), [
    { label: 'Max Weight', data: b.timeline.map(t => t.session_max),  borderColor: '#6c63ff', backgroundColor: 'rgba(108,99,255,0.1)', tension: 0.3, fill: true, pointRadius: 3 },
    { label: 'Est. 1RM',   data: b.timeline.map(t => t.session_e1rm), borderColor: '#a78bfa', backgroundColor: 'transparent', tension: 0.3, borderDash: [4,3], pointRadius: 0 },
  ], lineChartOpts(80));

  const sq = d.squat;
  makeChart('squatChart', 'line', sq.timeline.map(t => t.date.slice(5)), [
    { label: 'Max Weight', data: sq.timeline.map(t => t.session_max),  borderColor: '#00d4aa', backgroundColor: 'rgba(0,212,170,0.1)', tension: 0.3, fill: true, pointRadius: 3 },
    { label: 'Est. 1RM',   data: sq.timeline.map(t => t.session_e1rm), borderColor: '#34d399', backgroundColor: 'transparent', tension: 0.3, borderDash: [4,3], pointRadius: 0 },
  ], lineChartOpts(100));

  function renderTopSets(id, sets) {
    document.getElementById(id).innerHTML = `
      <thead><tr><th>Date</th><th>Weight</th><th>Reps</th><th>Est. 1RM</th></tr></thead>
      <tbody>${sets.map(s => `<tr>
        <td>${s.date}</td><td>${s.weight} lbs</td><td>${s.reps}</td><td>${s.e1rm.toFixed(1)} lbs</td>
      </tr>`).join('')}</tbody>`;
  }
  renderTopSets('bench-top-sets', b.top_sets);
  renderTopSets('squat-top-sets', sq.top_sets);
}

// ─── Exercises ───────────────────────────────────────────
let allExercises = [], exSortKey = 'total_volume', exSortDir = -1;

async function loadExercises() {
  document.getElementById('ex-loading').style.display = '';
  document.getElementById('ex-content').style.display = 'none';
  const r = await fetch('/api/exercises');
  allExercises = await r.json();
  document.getElementById('ex-loading').style.display = 'none';
  document.getElementById('ex-content').style.display = '';
  renderExercises();
}
function filterExercises() { renderExercises(); }
function sortEx(key) {
  exSortDir = exSortKey === key ? exSortDir * -1 : -1;
  exSortKey = key;
  renderExercises();
}
function renderExercises() {
  const q = (document.getElementById('ex-search')?.value || '').toLowerCase();
  let rows = allExercises.filter(e => e.name.toLowerCase().includes(q));
  rows.sort((a, b) => {
    const av = a[exSortKey] || '', bv = b[exSortKey] || '';
    return (typeof av === 'number' ? av - bv : av.localeCompare(bv)) * exSortDir;
  });
  document.getElementById('ex-count').textContent = rows.length + ' exercises';
  document.getElementById('ex-tbody').innerHTML = rows.map(e => `
    <tr onclick="openExModal('${e.name.replace(/'/g,"\\'")}')">
      <td><span class="ex-name">${e.name}</span></td>
      <td>${e.max_weight} lbs</td>
      <td><span class="badge">${e.max_e1rm} lbs</span></td>
      <td>${e.total_sets}</td>
      <td>${fmt(e.total_volume)}</td>
      <td>${e.last_date}</td>
    </tr>`).join('');
}

// ─── Exercise modal ───────────────────────────────────────
let modalChart = null;
async function openExModal(name) {
  document.getElementById('ex-modal').classList.add('open');
  document.getElementById('modal-title').textContent = name;
  document.getElementById('modal-stats').innerHTML = '<div style="color:var(--text-muted);font-size:13px;grid-column:span 3">Loading…</div>';
  const r = await fetch('/api/exercise/' + encodeURIComponent(name));
  const d = await r.json();
  const s = d.stats;
  document.getElementById('modal-stats').innerHTML = [
    { label: 'Max Weight',    value: s.max_weight + ' lbs', cls: 'accent-purple' },
    { label: 'Est. 1RM',      value: s.max_e1rm + ' lbs',   cls: 'accent-green' },
    { label: 'Total Sets',    value: s.total_sets,           cls: 'accent-orange' },
    { label: 'Total Volume',  value: fmt(s.total_volume) + ' lbs', cls: '' },
    { label: 'Last Session',  value: s.last_date,            cls: '' },
    { label: 'Best Set',      value: s.best_set,             cls: 'accent-purple' },
  ].map(c => `
    <div class="stat-card" style="padding:14px 16px;">
      <div class="stat-label">${c.label}</div>
      <div style="font-size:18px;font-weight:700;" class="${c.cls}">${c.value}</div>
    </div>`).join('');

  const byDate = {};
  d.records.forEach(rec => {
    if (!byDate[rec.date] || byDate[rec.date] < rec.weight) byDate[rec.date] = rec.weight;
  });
  const dates = Object.keys(byDate).sort();
  if (modalChart) modalChart.destroy();
  const ctx = document.getElementById('modalChart');
  modalChart = new Chart(ctx, {
    type: 'line',
    data: { labels: dates.map(d2 => d2.slice(5)), datasets: [{
      data: dates.map(d2 => byDate[d2]),
      borderColor: '#6c63ff', backgroundColor: 'rgba(108,99,255,0.08)',
      tension: 0.3, fill: true, pointRadius: 4, pointBackgroundColor: '#6c63ff',
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e', maxTicksLimit: 10 } },
        y: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e' } }
      }
    }
  });

  const sorted = [...d.records].sort((a, b) => b.date_ts - a.date_ts);
  document.getElementById('modal-sets-table').innerHTML = `
    <thead><tr><th>Date</th><th>Weight</th><th>Reps</th><th>Est. 1RM</th></tr></thead>
    <tbody>${sorted.map(rec => `<tr>
      <td>${rec.date}</td><td>${rec.weight} lbs</td><td>${rec.reps}</td><td>${rec.e1rm.toFixed(1)}</td>
    </tr>`).join('')}</tbody>`;
}
function closeExModal() { document.getElementById('ex-modal').classList.remove('open'); }
function closeModal(e) { if (e.target === document.getElementById('ex-modal')) closeExModal(); }

// ─── History ─────────────────────────────────────────────
let allSessions = [], filteredSessions = [];
async function loadHistory() {
  document.getElementById('hist-loading').style.display = '';
  document.getElementById('hist-content').style.display = 'none';
  const r = await fetch('/api/history');
  allSessions = await r.json();
  document.getElementById('hist-loading').style.display = 'none';
  document.getElementById('hist-content').style.display = '';
  renderHistory();
}
function filterHistory() { renderHistory(); }
function renderHistory() {
  const q = (document.getElementById('hist-search')?.value || '').toLowerCase();
  filteredSessions = allSessions.filter(s =>
    !q || s.title.toLowerCase().includes(q) || s.exercises.some(e => e.toLowerCase().includes(q))
  );
  document.getElementById('hist-count').textContent = filteredSessions.length + ' sessions';
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  document.getElementById('session-list').innerHTML = filteredSessions.map((s, i) => {
    const [,m, day] = s.date.split('-');
    return `
      <div class="session-item" id="sess-${i}" onclick="toggleSession(${i})">
        <div class="session-header">
          <div class="session-date-block">
            <div class="session-month">${months[parseInt(m)-1]}</div>
            <div class="session-day">${day}</div>
          </div>
          <div class="session-info">
            <div class="session-title">${s.title}</div>
            <div class="session-meta">${s.weekday} · ${s.exercises.length} exercises</div>
          </div>
          <div style="display:flex;align-items:center;gap:10px;">
            <div style="font-size:12px;color:var(--text-muted)">${fmt(s.total_volume)} lbs</div>
            <button class="log-ctx-btn" onclick="event.stopPropagation();openContextModal(${i})" title="Log session context">+ Context</button>
          </div>
        </div>
        <div class="session-exercises">
          ${s.exercises.map(e => `<span class="ex-pill">${e}</span>`).join('')}
        </div>
      </div>`;
  }).join('');
}
function toggleSession(i) { document.getElementById('sess-' + i).classList.toggle('open'); }

// ─── Context modal ────────────────────────────────────────
const ctxState = { rpe: null, sleep: null, soreness: null, nutrition: null };

function buildRatingRow(rowId, max, field) {
  const row = document.getElementById(rowId);
  row.innerHTML = '';
  for (let i = 1; i <= max; i++) {
    const btn = document.createElement('button');
    btn.className = 'rating-btn';
    btn.textContent = i;
    btn.onclick = () => {
      ctxState[field] = i;
      row.querySelectorAll('.rating-btn').forEach((b, idx) => b.classList.toggle('selected', idx < i));
    };
    row.appendChild(btn);
  }
}

function openContextModal(idx) {
  const s = filteredSessions[idx];
  if (!s) return;
  ctxState.rpe = null; ctxState.sleep = null; ctxState.soreness = null; ctxState.nutrition = null;
  const lyftaKey = s.date + '|' + s.title;
  document.getElementById('ctx-lyfta-key').value    = lyftaKey;
  document.getElementById('ctx-workout-date').value  = s.date;
  document.getElementById('ctx-workout-title').value = s.title;
  document.getElementById('ctx-notes').value          = '';
  document.getElementById('context-modal-subtitle').textContent = s.title + ' — ' + s.date_display;
  buildRatingRow('ctx-rpe-row',     10, 'rpe');
  buildRatingRow('ctx-sleep-row',    5, 'sleep');
  buildRatingRow('ctx-soreness-row', 5, 'soreness');
  document.querySelectorAll('.nutr-btn').forEach(b => b.classList.remove('selected'));
  document.getElementById('context-modal').classList.add('open');
}

function selectNutrition(val) {
  ctxState.nutrition = val;
  document.querySelectorAll('.nutr-btn').forEach(b => b.classList.toggle('selected', b.dataset.val === val));
}

async function saveContext() {
  const lyftaKey = document.getElementById('ctx-lyfta-key').value;
  const payload = {
    lyfta_key:     lyftaKey,
    workout_date:  document.getElementById('ctx-workout-date').value,
    workout_title: document.getElementById('ctx-workout-title').value,
    rpe:           ctxState.rpe,
    sleep_quality: ctxState.sleep,
    soreness:      ctxState.soreness,
    nutrition:     ctxState.nutrition,
    notes:         document.getElementById('ctx-notes').value,
  };
  try {
    const r = await fetch('/api/context', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const d = await r.json();
    if (d.ok) {
      document.getElementById('context-modal').classList.remove('open');
      showToast('Context saved', 'success');
      if (loaded['analysis']) { loaded['analysis'] = false; }
    } else {
      showToast('Error: ' + d.error, 'error');
    }
  } catch { showToast('Network error', 'error'); }
}

function closeContextModal(e) {
  if (e.target === document.getElementById('context-modal'))
    document.getElementById('context-modal').classList.remove('open');
}

// ─── Known 1RM modal ──────────────────────────────────────
function openRmModal() {
  document.getElementById('rm-exercise').value = '';
  document.getElementById('rm-date').value     = new Date().toISOString().slice(0, 10);
  document.getElementById('rm-weight').value   = '';
  document.getElementById('rm-notes').value    = '';
  document.getElementById('rm-modal').classList.add('open');
}

async function saveKnownRm() {
  const exercise = document.getElementById('rm-exercise').value.trim();
  const date     = document.getElementById('rm-date').value;
  const weight   = parseFloat(document.getElementById('rm-weight').value);
  const notes    = document.getElementById('rm-notes').value;
  if (!exercise || !date || isNaN(weight) || weight <= 0) {
    showToast('Fill in exercise, date, and weight', 'error'); return;
  }
  try {
    const r = await fetch('/api/known_1rm', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ exercise_name: exercise, date, weight_lbs: weight, notes }),
    });
    const d = await r.json();
    if (d.ok) {
      document.getElementById('rm-modal').classList.remove('open');
      showToast('1RM saved', 'success');
      if (loaded['analysis']) { loaded['analysis'] = false; loadAnalysis(); }
    } else {
      showToast('Error: ' + d.error, 'error');
    }
  } catch { showToast('Network error', 'error'); }
}

// ─── Analysis tab ─────────────────────────────────────────
const trendCharts = {};

async function loadAnalysis() {
  document.getElementById('analysis-loading').style.display = '';
  document.getElementById('analysis-content').style.display = 'none';

  const [stats, contexts, rms] = await Promise.all([
    fetch('/api/analysis').then(r => r.json()),
    fetch('/api/context').then(r => r.json()),
    fetch('/api/known_1rm').then(r => r.json()),
  ]);

  document.getElementById('analysis-loading').style.display = 'none';
  document.getElementById('analysis-content').style.display = '';

  // ── Stat cards ──
  const trendCount = Object.values(stats.analyses || {}).filter(Boolean).length;
  document.getElementById('analysis-stat-cards').innerHTML = [
    { label: 'Workouts in DB',    value: stats.db_workout_count  || 0, sub: 'synced from Lyfta',               cls: 'accent-purple' },
    { label: 'Exercises Tracked', value: trendCount,                   sub: 'with enough data for trend lines', cls: 'accent-green'  },
    { label: 'Known 1RMs',        value: stats.known_1rm_entries || 0, sub: 'calibration anchors',             cls: 'accent-orange' },
  ].map(c => `
    <div class="stat-card">
      <div class="stat-label">${c.label}</div>
      <div class="stat-value ${c.cls}">${c.value}</div>
      <div class="stat-sub">${c.sub}</div>
    </div>`).join('');

  // ── Recommendations ──
  const recs = stats.recommendations || [];
  const recCard = document.getElementById('recommendations-card');
  if (recs.length) {
    recCard.style.display = '';
    const typeStyle = {
      warning:  { border: 'var(--accent3)', icon: '⚠', color: 'var(--accent3)' },
      stall:    { border: 'var(--accent4)', icon: '⏸', color: 'var(--accent4)' },
      progress: { border: 'var(--accent2)', icon: '↑',  color: 'var(--accent2)' },
      info:     { border: 'var(--border)',  icon: 'ℹ',  color: 'var(--text-muted)' },
    };
    document.getElementById('recommendations-list').innerHTML = recs.map(r => {
      const s = typeStyle[r.type] || typeStyle.info;
      return `<div style="border:1px solid ${s.border};border-radius:8px;padding:14px 16px;background:var(--surface2);">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
          <span style="color:${s.color};font-size:16px;">${s.icon}</span>
          <span style="font-weight:600;font-size:13px;color:var(--text);">${r.title}</span>
        </div>
        <div style="font-size:12px;color:var(--text-muted);padding-left:26px;line-height:1.6;">${r.detail}</div>
      </div>`;
    }).join('');
  }

  // ── Fatigue indicator ──
  const fat = stats.fatigue || {};
  const fatColors = { high: 'var(--accent3)', elevated: 'var(--accent4)', normal: 'var(--accent2)', low: 'var(--text-muted)', unknown: 'var(--text-dim)' };
  const fatColor  = fatColors[fat.level] || fatColors.unknown;
  const fatPct    = Math.min(Math.max((fat.pct_change || 0) + 50, 0), 100);
  document.getElementById('fatigue-content').innerHTML = `
    <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:10px;">
      <span style="font-size:28px;font-weight:700;color:${fatColor};">${fat.level ? fat.level.charAt(0).toUpperCase() + fat.level.slice(1) : '—'}</span>
      ${fat.pct_change != null ? `<span style="font-size:13px;color:var(--text-muted);">${fat.pct_change > 0 ? '+' : ''}${fat.pct_change}% vs prior 4 wks</span>` : ''}
    </div>
    <div style="height:8px;background:var(--surface2);border-radius:99px;overflow:hidden;border:1px solid var(--border);margin-bottom:10px;">
      <div style="height:100%;width:${fatPct}%;background:${fatColor};border-radius:99px;transition:width 1s;"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim);">
      <span>Last 4 wks: ${fat.recent_4wk != null ? Number(fat.recent_4wk).toLocaleString() + ' lbs' : '—'}</span>
      <span>Prior 4 wks: ${fat.prev_4wk != null ? Number(fat.prev_4wk).toLocaleString() + ' lbs' : '—'}</span>
    </div>`;

  // ── Context insights ──
  const ctx = stats.context_insights;
  const ctxCard = document.getElementById('context-insights-card');
  if (ctx && ctx.n >= 5) {
    ctxCard.style.display = '';
    const sleepMap = ctx.avg_volume_by_sleep;
    const sleepRows = Object.keys(sleepMap).sort((a,b) => a-b).map(k =>
      `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px;">
        <span style="color:var(--text-muted);">Sleep ${k}/5</span>
        <span style="font-weight:600;">${Number(sleepMap[k]).toLocaleString()} lbs avg volume</span>
      </div>`
    ).join('');
    const rLabel = Math.abs(ctx.sleep_volume_r) > 0.4
      ? (ctx.sleep_volume_r > 0 ? 'Sleep strongly predicts higher volume' : 'More sleep correlates with lower volume')
      : 'Weak sleep-volume correlation so far';
    document.getElementById('context-insights-content').innerHTML = `
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px;">${rLabel} (r=${ctx.sleep_volume_r}, n=${ctx.n})</div>
      ${sleepRows}`;
  }

  // ── Exercise trend charts ──
  const analyses = stats.analyses || {};
  const exNames  = Object.keys(analyses).filter(k => analyses[k]);
  const trendsSection = document.getElementById('trends-section');
  const trendsGrid    = document.getElementById('trends-grid');

  if (exNames.length) {
    trendsSection.style.display = '';
    trendsGrid.innerHTML = exNames.map(name => {
      const safeid = name.replace(/[^a-zA-Z0-9]/g, '_');
      return `<div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px;">
          <div style="font-weight:600;font-size:14px;">${name}</div>
          ${renderTrendBadge(analyses[name])}
        </div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px;">
          ${analyses[name].session_count} sessions · max ${analyses[name].all_time_max} lbs
        </div>
        <div class="chart-container"><canvas id="trend-${safeid}"></canvas></div>
        ${analyses[name].off_days && analyses[name].off_days.length ? `<div style="margin-top:8px;font-size:11px;color:var(--accent4);">⚠ ${analyses[name].off_days.length} low-performance session${analyses[name].off_days.length > 1 ? 's' : ''} detected</div>` : ''}
      </div>`;
    }).join('');

    // Draw charts after DOM update
    setTimeout(() => {
      exNames.forEach(name => {
        const a       = analyses[name];
        const safeid  = name.replace(/[^a-zA-Z0-9]/g, '_');
        const canv    = document.getElementById('trend-' + safeid);
        if (!canv) return;
        if (trendCharts[name]) trendCharts[name].destroy();
        const labels = a.dates.map(d => d.slice(5));
        trendCharts[name] = new Chart(canv, {
          type: 'line',
          data: {
            labels,
            datasets: [
              {
                label: 'Session Best',
                data: a.weights,
                borderColor: 'rgba(108,99,255,0.4)',
                backgroundColor: 'transparent',
                pointRadius: 3, pointBackgroundColor: '#6c63ff',
                tension: 0.2, borderWidth: 1,
              },
              {
                label: 'Smoothed',
                data: a.smoothed,
                borderColor: '#6c63ff',
                backgroundColor: 'rgba(108,99,255,0.08)',
                pointRadius: 0, tension: 0.3,
                borderWidth: 2, fill: true,
              },
              ...(a.trend ? [{
                label: 'Trend line',
                data: a.trend.predicted,
                borderColor: 'rgba(0,212,170,0.7)',
                backgroundColor: 'transparent',
                pointRadius: 0, borderDash: [5, 4],
                borderWidth: 1.5,
              }] : []),
            ],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { display: true, labels: { color: '#7a7f8e', boxWidth: 10, font: { size: 11 } } } },
            scales: {
              x: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e', maxTicksLimit: 8 } },
              y: { grid: { color: '#2a2d36' }, ticks: { color: '#7a7f8e' } },
            },
          },
        });
      });
    }, 50);
  }

  // ── Known 1RM table ──
  const rmWrap = document.getElementById('rm-table-wrap');
  rmWrap.innerHTML = rms.length === 0
    ? '<div style="color:var(--text-muted);font-size:13px;padding:8px 0;">No known 1RMs logged yet.</div>'
    : `<table class="mini-table">
        <thead><tr><th>Exercise</th><th>Date</th><th>Weight</th><th>Notes</th></tr></thead>
        <tbody>${rms.map(r2 => `<tr>
          <td style="font-weight:600;">${r2.exercise_name}</td><td>${r2.date}</td>
          <td style="color:var(--accent2);font-weight:600;">${r2.weight_lbs} lbs</td>
          <td style="color:var(--text-muted);">${r2.notes || '—'}</td>
        </tr>`).join('')}</tbody></table>`;

  // ── Context log table ──
  const ctxWrap = document.getElementById('context-table-wrap');
  ctxWrap.innerHTML = contexts.length === 0
    ? '<div style="color:var(--text-muted);font-size:13px;padding:8px 0;">No context logged yet. Go to History and click a session to log context.</div>'
    : `<div style="overflow-x:auto"><table class="mini-table">
        <thead><tr><th>Date</th><th>Session</th><th>RPE</th><th>Sleep</th><th>Soreness</th><th>Nutrition</th><th>Notes</th></tr></thead>
        <tbody>${contexts.map(c => `<tr>
          <td style="white-space:nowrap;">${c.workout_date}</td>
          <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${c.workout_title}</td>
          <td>${c.rpe           != null ? c.rpe           + '/10' : '—'}</td>
          <td>${c.sleep_quality != null ? c.sleep_quality + '/5'  : '—'}</td>
          <td>${c.soreness      != null ? c.soreness      + '/5'  : '—'}</td>
          <td>${c.nutrition || '—'}</td>
          <td style="color:var(--text-muted);font-size:12px;">${c.notes || ''}</td>
        </tr>`).join('')}</tbody></table></div>`;
}

function renderTrendBadge(a) {
  if (!a || !a.trend) return '';
  const slope = a.trend.slope_per_week;
  const r2    = a.trend.r_squared;
  if (slope >= 1)   return `<span style="font-size:11px;font-weight:600;color:var(--accent2);background:rgba(0,212,170,0.1);padding:3px 8px;border-radius:99px;">↑ +${slope} lbs/wk</span>`;
  if (slope <= -1)  return `<span style="font-size:11px;font-weight:600;color:var(--accent3);background:rgba(255,107,107,0.1);padding:3px 8px;border-radius:99px;">↓ ${slope} lbs/wk</span>`;
  return `<span style="font-size:11px;font-weight:600;color:var(--text-muted);background:var(--surface2);padding:3px 8px;border-radius:99px;">— ${slope > 0 ? '+' : ''}${slope} lbs/wk</span>`;
}

// ─── Boot ─────────────────────────────────────────────────
fetch('/api/status').then(r => r.json()).then(d => setLastRefresh(d.last_refresh));
loadTab('dashboard');
loaded['dashboard'] = true;
</script>
</body>
</html>"""

@app.route('/')
def index():
    return HTML

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=False)
