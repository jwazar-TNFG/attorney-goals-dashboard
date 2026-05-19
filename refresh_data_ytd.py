#!/usr/bin/env python3
"""
Refresh attorney goals dashboard data with YTD monthly breakdown.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib.supabase_client import get_client

def load_mapping():
    """Load attorney name mapping."""
    script_dir = Path(__file__).parent
    mapping_file = script_dir / 'attorney_mapping.json'
    if mapping_file.exists():
        with open(mapping_file, 'r') as f:
            return json.load(f)
    return {'mappings': {}, 'ignored': []}

def get_monthly_counts(sb, year: int, month: int):
    """Get lockdown and void counts for a specific month."""
    start_date = f"{year}-{month:02d}-01"
    if month == 12:
        end_date = f"{year + 1}-01-01"
    else:
        end_date = f"{year}-{month + 1:02d}-01"
    
    # Get lockdowns
    lockdowns = {}
    offset = 0
    batch_size = 1000
    
    while True:
        result = sb.table('leads').select('attorney, accidentState') \
            .gte('dateLockedDown', start_date) \
            .lt('dateLockedDown', end_date) \
            .not_.is_('attorney', 'null') \
            .order('idLead') \
            .range(offset, offset + batch_size - 1) \
            .execute()
        
        for row in result.data:
            attorney = row['attorney']
            state = row.get('accidentState', 'Unknown')
            key = (attorney, state)
            lockdowns[key] = lockdowns.get(key, 0) + 1
        
        if len(result.data) < batch_size:
            break
        offset += batch_size
    
    # Get voids
    voids = {}
    offset = 0
    
    while True:
        result = sb.table('leads').select('attorney, accidentState') \
            .gte('dateLockedDown', start_date) \
            .lt('dateLockedDown', end_date) \
            .not_.is_('attorney', 'null') \
            .not_.is_('dateDropped', 'null') \
            .order('idLead') \
            .range(offset, offset + batch_size - 1) \
            .execute()
        
        for row in result.data:
            attorney = row['attorney']
            state = row.get('accidentState', 'Unknown')
            key = (attorney, state)
            voids[key] = voids.get(key, 0) + 1
        
        if len(result.data) < batch_size:
            break
        offset += batch_size
    
    return lockdowns, voids

def normalize_name(name: str) -> str:
    """Normalize attorney name for matching."""
    import re
    name = name.lower().strip()
    name = re.sub(r'\s*-\s*(fl|ga|tx|ny|nj|il|ma|oh|tn|ar|ut|dc|co|nc|ca)\s*$', '', name, flags=re.IGNORECASE)
    return name

def map_attorney(db_name: str, db_state: str, mapping: dict, goals: list) -> tuple:
    """Map database attorney name to dashboard firm name."""
    db_name_lower = db_name.lower().strip()
    db_state_lower = (db_state or 'unknown').lower().strip()
    
    COMBINED_STATES = {
        'michigan': 'MI/MA/WA/VI',
        'massachusetts': 'MI/MA/WA/VI', 
        'washington': 'MI/MA/WA/VI',
        'virginia': 'MI/MA/WA/VI',
        'maryland': 'MI/MA/WA/VI',
    }
    
    if db_name_lower in mapping.get('mappings', {}):
        mapped_firm = mapping['mappings'][db_name_lower]
        goal_state = COMBINED_STATES.get(db_state_lower, db_state_lower)
        
        for goal in goals:
            if goal['firm_name'].lower() == mapped_firm.lower() and goal['state'].lower() == goal_state.lower():
                return (goal['firm_name'], goal['state'])
        for goal in goals:
            if goal['firm_name'].lower() == mapped_firm.lower() and goal['state'].lower() == db_state_lower:
                return (goal['firm_name'], goal['state'])
        for goal in goals:
            if goal['firm_name'].lower() == mapped_firm.lower():
                return (goal['firm_name'], goal['state'])
    
    for ignored in mapping.get('ignored', []):
        if isinstance(ignored, str) and not ignored.startswith('_'):
            if ignored.lower() in db_name_lower:
                return (None, None)
    
    for goal in goals:
        if goal['firm_name'].lower() == normalize_name(db_name) and goal['state'].lower() == db_state_lower:
            return (goal['firm_name'], goal['state'])
    
    normalized = normalize_name(db_name)
    for goal in goals:
        goal_name_lower = goal['firm_name'].lower()
        if goal['state'].lower() == db_state_lower:
            if normalized in goal_name_lower or goal_name_lower in normalized:
                return (goal['firm_name'], goal['state'])
    
    return ('__unmatched__', db_state)

def refresh_ytd():
    """Refresh with YTD monthly breakdown."""
    script_dir = Path(__file__).parent
    data_file = script_dir / 'data.json'
    
    with open(data_file, 'r') as f:
        current = json.load(f)
    
    mapping = load_mapping()
    sb = get_client()
    
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    
    months = list(range(1, current_month + 1))
    month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    
    # Initialize monthly data for each firm
    for goal in current['data']:
        goal['monthly'] = {}
        goal['ytd_gross'] = 0
        goal['ytd_voided'] = 0
        goal['ytd_net'] = 0
        for m in months:
            goal['monthly'][month_names[m-1]] = {'gross': 0, 'voided': 0, 'net': 0}
    
    # Build lookup
    goal_lookup = {}
    for goal in current['data']:
        key = (goal['firm_name'], goal['state'])
        goal_lookup[key] = goal
    
    # Process each month
    for month in months:
        print(f"Processing {month_names[month-1]} {current_year}...")
        lockdowns, voids = get_monthly_counts(sb, current_year, month)
        
        for (attorney, state), count in lockdowns.items():
            firm_name, firm_state = map_attorney(attorney, state, mapping, current['data'])
            
            if firm_name and firm_name != '__unmatched__':
                key = (firm_name, firm_state)
                if key in goal_lookup:
                    goal = goal_lookup[key]
                    void_count = voids.get((attorney, state), 0)
                    
                    goal['monthly'][month_names[month-1]]['gross'] += count
                    goal['monthly'][month_names[month-1]]['voided'] += void_count
                    goal['monthly'][month_names[month-1]]['net'] += (count - void_count)
                    
                    goal['ytd_gross'] += count
                    goal['ytd_voided'] += void_count
                    goal['ytd_net'] += (count - void_count)
    
    # Calculate YTD acceptance rate
    for goal in current['data']:
        if goal['ytd_gross'] > 0:
            goal['ytd_acceptance'] = round(goal['ytd_net'] / goal['ytd_gross'] * 100, 1)
        else:
            goal['ytd_acceptance'] = 0
        
        # Update current month values (already in data, but refresh)
        current_month_name = month_names[current_month - 1]
        goal['gross'] = goal['monthly'][current_month_name]['gross']
        goal['voided'] = goal['monthly'][current_month_name]['voided']
        goal['net'] = goal['monthly'][current_month_name]['net']
        goal['owed'] = max(0, goal['monthly_goal'] - goal['net'])
        goal['percent'] = round(goal['net'] / goal['monthly_goal'] * 100) if goal['monthly_goal'] > 0 else 0
    
    # Update metadata
    current['updated'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    current['month'] = now.strftime('%B %Y')
    current['ytd_months'] = [month_names[m-1] for m in months]
    
    with open(data_file, 'w') as f:
        json.dump(current, f, indent=2)
    
    print(f"\n✅ Updated {data_file} with YTD data")
    print(f"Months included: {', '.join(current['ytd_months'])}")
    
    # Summary
    total_ytd = sum(g['ytd_net'] for g in current['data'])
    total_ytd_gross = sum(g['ytd_gross'] for g in current['data'])
    print(f"YTD Total: {total_ytd:,} net / {total_ytd_gross:,} gross")

if __name__ == '__main__':
    refresh_ytd()
