#!/usr/bin/env python3
"""
Refresh attorney goals dashboard data from Supabase.

REPOINTED 2026-11-02 (Jon-approved, Option A): goals are now sourced LIVE from
public.carol_attygoals (Carol's maintained sheet, mirrored to Supabase) instead
of a static list baked into data.json. Actuals (signed MTD + voids) still come
from the leads table (dateSigned, TNFG only, canonical is_excluded_lead gate).
Attorney-name drift between leads.attorney and Carol's law_firm is reconciled by
attorney_mapping.json (explicit aliases) + state abbrev reconciliation.

Run manually or via cron to keep data.json current.
"""

import json
import sys as _sys; _sys.path.insert(0, '/Users/claudebot/clawd/scripts')
from lead_exclusions import apply_postgrest
import sys
import re
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.supabase_client import get_client
try:
    from lib.supabase_postgres import execute_sql
    _HAS_DIRECT_PG = True
except ImportError:
    _HAS_DIRECT_PG = False


def _try_execute_sql(query, fetch=False, timeout_ms=300000):
    """Try direct PG with explicit statement timeout; return None on failure so callers can fall back to REST."""
    if not _HAS_DIRECT_PG:
        return None
    try:
        from lib.supabase_postgres import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(f"SET statement_timeout = '{timeout_ms}'")
            cur.execute(query)
            if fetch:
                results = cur.fetchall()
                cur.close()
                conn.close()
                return results
            else:
                conn.commit()
                cur.close()
                conn.close()
                return True
        except Exception as e:
            conn.rollback()
            conn.close()
            raise
    except Exception as e:
        err_str = str(e)
        if 'Connection refused' in err_str or 'connection' in err_str.lower():
            print("Direct PG unavailable, falling back to REST API")
            return None
        if '57014' in err_str or 'statement timeout' in err_str.lower() or 'QueryCanceled' in err_str:
            print(f"Statement timeout on direct PG ({timeout_ms}ms), falling back to REST API")
            return None
        if 'PGRST' in err_str or 'schema cache' in err_str.lower():
            print("PostgREST unavailable, falling back")
            return None
        raise


def load_mapping():
    """Load attorney name mapping."""
    script_dir = Path(__file__).parent
    mapping_file = script_dir / 'attorney_mapping.json'
    if mapping_file.exists():
        with open(mapping_file, 'r') as f:
            return json.load(f)
    return {'mappings': {}, 'ignored': []}


def load_goals(sb):
    """Load goal rows LIVE from public.carol_attygoals (Carol's maintained sheet).

    Returns a list of goal dicts shaped like the dashboard expects:
      {firm_name, state, monthly_goal, gross, voided, net, owed, percent}
    """
    rows = _try_execute_sql(
        "SELECT law_firm, state, goals FROM public.carol_attygoals ORDER BY law_firm, state",
        fetch=True)
    if rows is None:
        print("Using REST API fallback for carol_attygoals goals...")
        res = sb.table('carol_attygoals').select('law_firm,state,goals').execute()
        rows = [(r['law_firm'], r['state'], r['goals']) for r in (res.data or [])]
    goals = []
    for law_firm, state, goal in rows:
        goals.append({
            'firm_name': law_firm,
            'state': state,
            'monthly_goal': float(goal) if goal is not None else 0,
            'gross': 0, 'voided': 0, 'net': 0, 'owed': 0, 'percent': 0,
        })
    return goals


_STATE_ABBREV = {
    'alabama': 'al', 'arizona': 'az', 'arkansas': 'ar', 'california': 'ca',
    'colorado': 'co', 'connecticut': 'ct', 'delaware': 'de',
    'district of columbia': 'dc', 'washington dc': 'dc', 'florida': 'fl',
    'georgia': 'ga', 'illinois': 'il', 'indiana': 'in', 'kansas': 'ks',
    'kentucky': 'ky', 'maine': 'me', 'maryland': 'md', 'massachusetts': 'ma',
    'michigan': 'mi', 'minnesota': 'mn', 'mississippi': 'ms', 'missouri': 'mo',
    'nevada': 'nv', 'new jersey': 'nj', 'new mexico': 'nm', 'new york': 'ny',
    'north carolina': 'nc', 'ohio': 'oh', 'oregon': 'or', 'pennsylvania': 'pa',
    'south carolina': 'sc', 'tennessee': 'tn', 'texas': 'tx', 'utah': 'ut',
    'virginia': 'va', 'washington': 'wa', 'wisconsin': 'wi',
}


def _norm(s):
    return re.sub(r'\s+', ' ', (s or '').lower().replace('.', '').replace(',', '')).strip()


def _state_eq(a, b):
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True
    return _STATE_ABBREV.get(na, na) == _STATE_ABBREV.get(nb, nb)


def get_lockdown_counts(sb, start_date, end_date):
    """Signed counts by attorney from leads (dateSigned + TNFG only). SQL agg, REST fallback."""
    result = _try_execute_sql(f'''
        SELECT attorney, "accidentState", COUNT(*) as cnt
        FROM leads
        WHERE "dateSigned" >= '{start_date}'
          AND "dateSigned" < '{end_date}'
          AND "sourceType" = 'TNFG'
          AND attorney IS NOT NULL
          AND NOT public.is_excluded_lead("intakeSpecialist", "submitterName", "officeLabel", "sourceType", attorney)
        GROUP BY attorney, "accidentState"
    ''', fetch=True)
    if result is not None:
        return {(r[0], r[1] or 'Unknown'): r[2] for r in result}
    print("Using REST API fallback for lockdown counts...")
    rows = _fetch_leads_rest(sb, start_date, end_date)
    from collections import Counter
    counts = Counter()
    for row in rows:
        at = row.get('attorney')
        if at:
            counts[(at, row.get('accidentState') or 'Unknown')] += 1
    return dict(counts)


def get_voided_counts(sb, start_date, end_date):
    """Voided/dropped counts by attorney (dateSigned + TNFG only). SQL agg, REST fallback."""
    result = _try_execute_sql(f'''
        SELECT attorney, "accidentState", COUNT(*) as cnt
        FROM leads
        WHERE "dateSigned" >= '{start_date}'
          AND "dateSigned" < '{end_date}'
          AND "sourceType" = 'TNFG'
          AND attorney IS NOT NULL
          AND NOT public.is_excluded_lead("intakeSpecialist", "submitterName", "officeLabel", "sourceType", attorney)
          AND "dateDropped" IS NOT NULL
        GROUP BY attorney, "accidentState"
    ''', fetch=True)
    if result is not None:
        return {(r[0], r[1] or 'Unknown'): r[2] for r in result}
    print("Using REST API fallback for voided counts...")
    rows = _fetch_leads_rest(sb, start_date, end_date, dropped_only=True)
    from collections import Counter
    counts = Counter()
    for row in rows:
        at = row.get('attorney')
        if at:
            counts[(at, row.get('accidentState') or 'Unknown')] += 1
    return dict(counts)


def _fetch_leads_rest(sb, start_date, end_date, dropped_only=False):
    """REST fallback with keyset pagination + retry."""
    import time
    all_rows, last_id, page = [], 0, 0
    page_size, max_retries = 100, 5
    while True:
        q = sb.table('leads').select('idLead,attorney,accidentState')
        q = q.gte('dateSigned', start_date).lt('dateSigned', end_date)
        q = q.eq('sourceType', 'TNFG').not_.is_('attorney', 'null')
        q = apply_postgrest(q)
        q = q.gt('idLead', str(last_id)).order('idLead').limit(page_size)
        if dropped_only:
            q = q.not_.is_('dateDropped', 'null')
        for attempt in range(max_retries):
            try:
                result = q.execute(); break
            except Exception as e:
                err = str(e)
                if ('57014' in err or 'timeout' in err.lower() or 'schema cache' in err.lower()) and attempt < max_retries - 1:
                    time.sleep(min(2 ** attempt * 2, 30))
                else:
                    raise
        rows = result.data
        page += 1
        if rows:
            all_rows.extend(rows); last_id = rows[-1]['idLead']
        if len(rows) < page_size:
            break
        time.sleep(0.3)
    return all_rows


def map_attorney(db_name, db_state, mapping, goals):
    """Map raw leads.attorney/state to a carol_attygoals (firm_name, state).

    Order: ignored -> explicit alias (+state reconcile) -> exact firm+state ->
    firm exact + state reconcile. Returns (firm, state), (None, None) if ignored,
    or ('__unmatched__', db_state) if nothing fits.
    """
    na = _norm(db_name)

    for ig in mapping.get('ignored', []):
        if isinstance(ig, str) and not ig.startswith('_') and _norm(ig) in na:
            return (None, None)

    aliases = mapping.get('mappings', {})
    if na in aliases:
        target = _norm(aliases[na])
        cand = [g for g in goals if _norm(g['firm_name']) == target]
        if cand:
            exact = next((g for g in cand if _state_eq(g['state'], db_state)), None)
            g = exact or cand[0]
            return (g['firm_name'], g['state'])
        return ('__unmatched__', db_state)

    for g in goals:
        if _norm(g['firm_name']) == na and _state_eq(g['state'], db_state):
            return (g['firm_name'], g['state'])
    firm_cand = [g for g in goals if _norm(g['firm_name']) == na]
    if firm_cand:
        exact = next((g for g in firm_cand if _state_eq(g['state'], db_state)), None)
        g = exact or firm_cand[0]
        return (g['firm_name'], g['state'])

    return ('__unmatched__', db_state)


def refresh_data():
    """Main refresh function."""
    script_dir = Path(__file__).parent
    data_file = script_dir / 'data.json'

    mapping = load_mapping()

    now = datetime.now()
    start_date = now.strftime('%Y-%m-01')
    if now.month == 12:
        end_date = f"{now.year + 1}-01-01"
    else:
        end_date = f"{now.year}-{now.month + 1:02d}-01"
    month_name = now.strftime('%B %Y')

    print(f"Refreshing data for {month_name} (MTD)")
    print(f"Date range: {start_date} to {end_date}")

    sb = get_client()

    goals = load_goals(sb)
    print(f"Loaded {len(goals)} goal rows from carol_attygoals")

    lockdowns = get_lockdown_counts(sb, start_date, end_date)
    voids = get_voided_counts(sb, start_date, end_date)
    print(f"Found {len(lockdowns)} attorney/state combinations with signed cases")

    goal_lookup = {(g['firm_name'], g['state']): g for g in goals}

    unmatched, matched_count = [], 0
    for (attorney, state), count in lockdowns.items():
        firm_name, firm_state = map_attorney(attorney, state, mapping, goals)
        if firm_name == '__unmatched__':
            unmatched.append((attorney, state, count))
        elif firm_name is None:
            pass
        else:
            key = (firm_name, firm_state)
            if key in goal_lookup:
                goal_lookup[key]['gross'] += count
                goal_lookup[key]['voided'] += voids.get((attorney, state), 0)
                matched_count += count

    for g in goals:
        g['net'] = g['gross'] - g['voided']
        g['owed'] = max(0, g['monthly_goal'] - g['net'])
        g['percent'] = round(g['net'] / g['monthly_goal'] * 100) if g['monthly_goal'] > 0 else 0

    out = {
        'updated': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'month': month_name,
        'as_of': now.strftime('%Y-%m-%d'),
        'source': 'carol_attygoals',
        'data': goals,
    }
    with open(data_file, 'w') as f:
        json.dump(out, f, indent=2)

    print(f"\n[OK] Updated {data_file}")
    print(f"Timestamp: {out['updated']}  Month: {month_name}  As-of: {out['as_of']}")
    print(f"Matched: {matched_count} signed")

    if unmatched:
        print(f"\n[WARN] Unmatched attorneys ({len(unmatched)}) - add alias to attorney_mapping.json or a goal row to carol_attygoals:")
        for attorney, state, count in sorted(unmatched, key=lambda x: -x[2])[:20]:
            print(f"  {attorney} | {state} | {count} signed")

    total_goal = sum(g['monthly_goal'] for g in goals)
    total_net = sum(g['net'] for g in goals)
    print(f"\n[SUMMARY] {total_net} / {total_goal} ({round(total_net/total_goal*100) if total_goal > 0 else 0}%)")


if __name__ == '__main__':
    refresh_data()
