#!/usr/bin/env python3
"""
Refresh attorney goals dashboard data from Supabase.
Run manually or via cron to keep data.json current.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.supabase_client import get_client
from lib.supabase_postgres import execute_sql

def get_payment_status(year: int, month: int) -> dict:
    """Get payment status by attorney from executive.payments table."""
    try:
        result = execute_sql(f'''
            SELECT name, payments_amount, is_active
            FROM executive.payments 
            WHERE year = {year} AND month = {month}
        ''', fetch=True)
        
        status = {}
        for row in result:
            name = row[0].lower().strip()
            amount = row[1] or 0
            is_active = row[2]
            # paid = has payment amount > 0
            status[name] = {
                'paid': amount > 0,
                'amount': float(amount),
                'active': is_active == 1
            }
        return status
    except Exception as e:
        print(f"Warning: Could not fetch payment status: {e}")
        return {}

def load_mapping():
    """Load attorney name mapping."""
    script_dir = Path(__file__).parent
    mapping_file = script_dir / 'attorney_mapping.json'
    
    if mapping_file.exists():
        with open(mapping_file, 'r') as f:
            return json.load(f)
    return {'mappings': {}, 'ignored': []}

def get_lockdown_counts(sb, start_date: str, end_date: str) -> dict:
    """Get signed counts by attorney from leads table (dateSigned + TNFG only)."""
    counts = {}
    offset = 0
    batch_size = 1000
    
    while True:
        result = sb.table('leads').select('attorney, accidentState') \
            .gte('dateSigned', start_date) \
            .lt('dateSigned', end_date) \
            .eq('sourceType', 'TNFG') \
            .not_.is_('attorney', 'null') \
            .order('idLead') \
            .range(offset, offset + batch_size - 1) \
            .execute()
        
        for row in result.data:
            attorney = row['attorney']
            state = row.get('accidentState', 'Unknown')
            key = (attorney, state)
            counts[key] = counts.get(key, 0) + 1
        
        if len(result.data) < batch_size:
            break
        offset += batch_size
    
    return counts

def get_voided_counts(sb, start_date: str, end_date: str) -> dict:
    """Get voided/dropped counts by attorney (dateSigned + TNFG only)."""
    counts = {}
    offset = 0
    batch_size = 1000
    
    while True:
        result = sb.table('leads').select('attorney, accidentState') \
            .gte('dateSigned', start_date) \
            .lt('dateSigned', end_date) \
            .eq('sourceType', 'TNFG') \
            .not_.is_('attorney', 'null') \
            .not_.is_('dateDropped', 'null') \
            .order('idLead') \
            .range(offset, offset + batch_size - 1) \
            .execute()
        
        for row in result.data:
            attorney = row['attorney']
            state = row.get('accidentState', 'Unknown')
            key = (attorney, state)
            counts[key] = counts.get(key, 0) + 1
        
        if len(result.data) < batch_size:
            break
        offset += batch_size
    
    return counts

def normalize_name(name: str) -> str:
    """Normalize attorney name for matching."""
    import re
    # Lowercase and strip
    name = name.lower().strip()
    # Remove common suffixes
    name = re.sub(r'\s*-\s*(fl|ga|tx|ny|nj|il|ma|oh|tn|ar|ut|dc|co|nc|ca)\s*$', '', name, flags=re.IGNORECASE)
    return name

def map_attorney(db_name: str, db_state: str, mapping: dict, goals: list) -> tuple:
    """
    Map database attorney name to dashboard firm name.
    Returns (firm_name, state) or (None, None) if not found.
    """
    db_name_lower = db_name.lower().strip()
    db_state_lower = (db_state or 'unknown').lower().strip()
    
    # Combined state mappings (states that roll up into a single goal row)
    # Founders has a global retainer for these states
    COMBINED_STATES = {
        'michigan': 'MI/MA/WA/VI',
        'massachusetts': 'MI/MA/WA/VI', 
        'washington': 'MI/MA/WA/VI',
        'virginia': 'MI/MA/WA/VI',
        'maryland': 'MI/MA/WA/VI',
    }
    
    # Special state overrides (when DB state is wrong)
    # Key = (attorney_name_lower, db_state_lower), Value = correct_state
    STATE_OVERRIDES = {
        ('scarfone - dc', 'washington'): 'district of columbia',
    }
    
    # Check for state overrides (when DB has wrong state)
    override_key = (db_name_lower, db_state_lower)
    if override_key in STATE_OVERRIDES:
        db_state_lower = STATE_OVERRIDES[override_key]
    
    # Check explicit mapping first
    if db_name_lower in mapping.get('mappings', {}):
        mapped_firm = mapping['mappings'][db_name_lower]
        
        # Check if this state maps to a combined state
        goal_state = COMBINED_STATES.get(db_state_lower, db_state_lower)
        
        # Find matching goal entry
        for goal in goals:
            if goal['firm_name'].lower() == mapped_firm.lower() and goal['state'].lower() == goal_state.lower():
                return (goal['firm_name'], goal['state'])
        # If combined state doesn't match, try original state
        for goal in goals:
            if goal['firm_name'].lower() == mapped_firm.lower() and goal['state'].lower() == db_state_lower:
                return (goal['firm_name'], goal['state'])
        # If state doesn't match exactly, just match by name (first match)
        for goal in goals:
            if goal['firm_name'].lower() == mapped_firm.lower():
                return (goal['firm_name'], goal['state'])
    
    # Check if it's in ignored list
    for ignored in mapping.get('ignored', []):
        if isinstance(ignored, str) and not ignored.startswith('_'):
            if ignored.lower() in db_name_lower:
                return (None, None)  # Ignored attorney
    
    # Try direct match on firm name + state
    for goal in goals:
        if goal['firm_name'].lower() == normalize_name(db_name) and goal['state'].lower() == db_state_lower:
            return (goal['firm_name'], goal['state'])
    
    # Try partial match
    normalized = normalize_name(db_name)
    for goal in goals:
        goal_name_lower = goal['firm_name'].lower()
        if goal['state'].lower() == db_state_lower:
            # Check if one contains the other
            if normalized in goal_name_lower or goal_name_lower in normalized:
                return (goal['firm_name'], goal['state'])
            # Check for word overlap
            db_words = set(normalized.split())
            goal_words = set(goal_name_lower.split())
            if len(db_words & goal_words) >= 1 and len(db_words & goal_words) / len(db_words | goal_words) > 0.3:
                return (goal['firm_name'], goal['state'])
    
    return ('__unmatched__', db_state)

def refresh_data():
    """Main refresh function."""
    script_dir = Path(__file__).parent
    data_file = script_dir / 'data.json'
    
    # Load current data (contains goals)
    with open(data_file, 'r') as f:
        current = json.load(f)
    
    # Load mapping
    mapping = load_mapping()
    
    # Determine date range for current month
    now = datetime.now()
    start_date = now.strftime('%Y-%m-01')
    if now.month == 12:
        end_date = f"{now.year + 1}-01-01"
    else:
        end_date = f"{now.year}-{now.month + 1:02d}-01"
    
    month_name = now.strftime('%B %Y')
    
    print(f"Refreshing data for {month_name}")
    print(f"Date range: {start_date} to {end_date}")
    
    # Get counts from database
    sb = get_client()
    lockdowns = get_lockdown_counts(sb, start_date, end_date)
    voids = get_voided_counts(sb, start_date, end_date)
    
    print(f"Found {len(lockdowns)} attorney/state combinations with lockdowns")
    
    # Reset all counts to 0
    for goal in current['data']:
        goal['gross'] = 0
        goal['voided'] = 0
    
    # Build lookup by firm_name + state
    goal_lookup = {}
    for goal in current['data']:
        key = (goal['firm_name'], goal['state'])
        goal_lookup[key] = goal
    
    # Map database counts to goals
    unmatched = []
    matched_count = 0
    
    for (attorney, state), count in lockdowns.items():
        firm_name, firm_state = map_attorney(attorney, state, mapping, current['data'])
        
        if firm_name == '__unmatched__':
            unmatched.append((attorney, state, count))
        elif firm_name is None:
            # Ignored attorney
            pass
        else:
            key = (firm_name, firm_state)
            if key in goal_lookup:
                goal_lookup[key]['gross'] += count
                void_count = voids.get((attorney, state), 0)
                goal_lookup[key]['voided'] += void_count
                matched_count += count
    
    # Calculate net and percentages
    for goal in current['data']:
        goal['net'] = goal['gross'] - goal['voided']
        goal['owed'] = max(0, goal['monthly_goal'] - goal['net'])
        goal['percent'] = round(goal['net'] / goal['monthly_goal'] * 100) if goal['monthly_goal'] > 0 else 0
    
    # Update metadata
    current['updated'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    current['month'] = month_name
    
    # Save updated data
    with open(data_file, 'w') as f:
        json.dump(current, f, indent=2)
    
    print(f"\n✅ Updated {data_file}")
    print(f"Timestamp: {current['updated']}")
    print(f"Matched: {matched_count} lockdowns")
    
    # Report unmatched attorneys (might need mapping added)
    if unmatched:
        print(f"\n⚠️  Unmatched attorneys ({len(unmatched)}) - add to attorney_mapping.json:")
        for attorney, state, count in sorted(unmatched, key=lambda x: -x[2])[:15]:
            print(f"  {attorney} | {state} | {count} LDs")
    
    # Summary
    total_goal = sum(g['monthly_goal'] for g in current['data'])
    total_net = sum(g['net'] for g in current['data'])
    print(f"\n📊 Summary: {total_net} / {total_goal} ({round(total_net/total_goal*100) if total_goal > 0 else 0}%)")

if __name__ == '__main__':
    refresh_data()
