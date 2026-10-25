"""A local history reset hides prior exchange fills; it never deletes exchange records."""
from datetime import datetime,timezone
import math

def fill_timestamp(value):
    try:
        if isinstance(value,str) and not value.replace('.','',1).isdigit():
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        stamp=float(value)
        if not math.isfinite(stamp):return None
        if stamp>=1e14:stamp/=1e6
        elif stamp>=1e11:stamp/=1000
        return stamp
    except (TypeError,ValueError,OverflowError):return None

def since_reset(rows,cutoff):
    if cutoff is None:return rows
    return [row for row in rows if (stamp:=fill_timestamp(row.get('created_at'))) is not None and stamp>cutoff]
