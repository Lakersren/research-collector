#!/usr/bin/env python3
"""One-shot pull/import job, called by the daily systemd timer or manually."""
import bootstrap
import argparse
import logging
import uuid
from aris.daily_policy import normalize_target_date
from aris.sync import Puller
from aris.observability import setup_logging,log_context

if __name__=='__main__':
    parser = argparse.ArgumentParser(description='按北京时间的目标发表日同步文章')
    parser.add_argument('date', nargs='?', help='YYYYMMDD 或 YYYY-MM-DD，默认北京时间昨天')
    args = parser.parse_args()
    if args.date is not None:
        try:
            args.date = normalize_target_date(args.date)
        except ValueError:
            parser.error('请输入有效日期（YYYYMMDD 或 YYYY-MM-DD）')
    setup_logging('sync')
    with log_context(job_id=str(uuid.uuid4()),trigger='sync'):
        try:
            Puller().run(args.date)
        except Exception:
            logging.getLogger('corpus.sync').exception('sync_job_failed')
            raise SystemExit(1)
