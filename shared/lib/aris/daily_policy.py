"""Calendar-day policy shared by collector filtering and receiver validation."""
from datetime import date,datetime,timedelta,timezone
import os
from zoneinfo import ZoneInfo

SHANGHAI=ZoneInfo('Asia/Shanghai')

def yesterday(now=None):
    return ((now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).date()-timedelta(days=1)).isoformat()

def publication_day(value):
    if not value:return None
    try:
        if isinstance(value,datetime):parsed=value
        elif len(str(value))==10:return date.fromisoformat(str(value))
        else:parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(SHANGHAI).date()
    except (ValueError,TypeError):return None

def matches(value,target=None):
    target=target or os.getenv('CORPUS_TARGET_DATE')
    if not target:return True  # Parser helpers remain usable without launching a daily run.
    return publication_day(value)==date.fromisoformat(target)

def candidate(value):
    # Missing list metadata may be resolved from the article page. Never save without a date.
    return not os.getenv('CORPUS_TARGET_DATE') or publication_day(value) is None or matches(value)
