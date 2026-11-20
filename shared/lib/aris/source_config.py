"""Shared source catalog; safe for both collector and read-only deployments."""
import yaml
from aris.paths import CONFIG

def sources():
    config = yaml.safe_load((CONFIG / 'web_sources.yaml').read_text(encoding='utf-8'))
    rows = [{**s, '_seg': seg} for seg in ('substack','rss','standalone') for s in config.get(seg, [])]
    rows.append({'key':'web:semianalysis', 'name':'SemiAnalysis', '_seg':'semi'})
    if len({s['key'] for s in rows}) != len(rows):
        raise ValueError('重复的来源 ID')
    return rows
