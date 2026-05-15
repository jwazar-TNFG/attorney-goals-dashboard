# Attorney Goals Dashboard

Live dashboard tracking attorney case goals vs actual signed cases.

**Live URL:** https://jwazar-tnfg.github.io/attorney-goals-dashboard/

## Features

- 📊 Real-time data from Supabase `leads` table
- 🎯 Monthly goals per attorney/state
- 📱 Mobile-friendly design
- 🔄 Auto-refresh every 5 minutes
- 📈 Progress bars and visual indicators
- 🔍 Filter by state, search by firm
- 📅 Month selector (Apr/May/Jun 2026)

## Data Source

- **Goals:** `attorney_goals` table in Supabase
- **Leads:** `leads` table, filtered by `dateLockedDown`
- **Voided:** Leads with `leadStatus` containing 'void', 'dropped', or 'no case'

## Updating Goals

### Method 1: Direct Supabase (Recommended)

Use Supabase dashboard or API to update the `attorney_goals` table:

```sql
-- Update a goal
UPDATE attorney_goals 
SET monthly_goal = 100 
WHERE firm_name = 'Robert Rubenstein' AND state = 'Florida';

-- Add a new attorney
INSERT INTO attorney_goals (firm_name, state, monthly_goal, lead_attorney_names)
VALUES ('New Firm', 'Texas', 50, ARRAY['Lead Attorney Name 1', 'Lead Attorney Name 2']);

-- View all goals
SELECT * FROM attorney_goals ORDER BY state, firm_name;
```

### Method 2: Python Script

```python
from lib.supabase_client import get_client

sb = get_client()

# Update goal
sb.table('attorney_goals').update({'monthly_goal': 100}).eq('firm_name', 'Robert Rubenstein').eq('state', 'Florida').execute()

# Add new attorney
sb.table('attorney_goals').insert({
    'firm_name': 'New Firm',
    'state': 'Texas',
    'monthly_goal': 50,
    'lead_attorney_names': ['Lead Attorney Name 1', 'Lead Attorney Name 2']
}).execute()
```

### Finding Lead Attorney Names

To find what names are used in the leads table:

```python
from lib.supabase_client import get_client
from collections import Counter

sb = get_client()
result = sb.table('leads').select('attorney').neq('office', 'xSTAFF').gte('dateLockedDown', '2026-05-01').execute()
attorneys = Counter([r['attorney'] for r in result.data if r['attorney']])
for name, count in attorneys.most_common(50):
    print(f'{count:4d} | {name}')
```

## Table Schema

```sql
CREATE TABLE attorney_goals (
    id SERIAL PRIMARY KEY,
    firm_name TEXT NOT NULL,
    state TEXT NOT NULL,
    monthly_goal INT NOT NULL DEFAULT 0,
    lead_attorney_names TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(firm_name, state)
);
```

## Exclusions Applied

- `office != 'xSTAFF'` (staff entries)
- `submitterName != 'Katiria Bonilla'` (attorney referrals)

## Tech Stack

- Pure HTML/CSS/JavaScript
- Supabase JS client (CDN)
- GitHub Pages hosting
